import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core import NoteStore, chunk_text, read_pdf_pages, infer_tag_from_filename


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
USERS_DIR = BASE_DIR / "backend" / "users"
USERS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OT AI Study Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def get_user_id(x_user: Optional[str]) -> str:
    return x_user or "local-default"


def user_paths(user_id: str):
    udir = USERS_DIR / user_id
    data_dir = udir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    return {
        "udir": udir,
        "data_dir": data_dir,
        "faiss": data_dir / "faiss.index",
        "chunks": data_dir / "chunks.json",
    }


def get_openai_client(authorization: Optional[str]):
    from openai import OpenAI

    api_key = None

    # Option 1: API key from frontend Bearer token
    if authorization and authorization.lower().startswith("bearer "):
        api_key = authorization.split(" ", 1)[1].strip()

    # Option 2: API key from server .env for deployment
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise HTTPException(status_code=401, detail="Missing OpenAI API key.")

    return OpenAI(api_key=api_key)


def load_store_for_user(user_id: str) -> NoteStore:
    paths = user_paths(user_id)
    store = NoteStore(base_path=paths["data_dir"])

    if paths["faiss"].exists() and paths["chunks"].exists():
        import faiss

        store.index = faiss.read_index(str(paths["faiss"]))

        with open(paths["chunks"], "r", encoding="utf-8") as f:
            payload = json.load(f)

        store.chunks = payload.get("chunks", [])
        store.tags = payload.get("tags", ["untagged"] * len(store.chunks))
        store.meta = payload.get("meta", [{}] * len(store.chunks))
        store.dim = payload.get("dim", None)

    return store


def save_store_for_user(user_id: str, store: NoteStore):
    import faiss

    paths = user_paths(user_id)

    if store.index is None or not store.chunks:
        return

    faiss.write_index(store.index, str(paths["faiss"]))

    payload = {
        "chunks": store.chunks,
        "tags": store.tags,
        "meta": store.meta,
        "dim": store.dim,
    }

    with open(paths["chunks"], "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def build_sources(hits):
    return sorted(
        {
            f'{h[3].get("source", "unknown")} (page {h[3].get("page", "?")})'
            for h in hits
        }
    )


def safe_openai_error(err: Exception):
    msg = str(err)

    if "insufficient_quota" in msg or "quota" in msg.lower():
        raise HTTPException(
            status_code=429,
            detail="OpenAI quota exceeded. Check billing/credits or use another API key.",
        )

    if "invalid_api_key" in msg or "Incorrect API key" in msg:
        raise HTTPException(
            status_code=401,
            detail="Invalid OpenAI API key.",
        )

    raise HTTPException(status_code=500, detail=f"OpenAI error: {msg}")


@app.get("/")
def home():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "app": "OT AI Study Engine"}


@app.get("/status")
def status(x_user: Optional[str] = Header(default=None)):
    user_id = get_user_id(x_user)
    store = load_store_for_user(user_id)

    return {
        "user_id": user_id,
        "chunks": len(store.chunks),
        "tags": sorted(list(set(store.tags))) if store.tags else [],
        "ready": bool(store.chunks),
    }


@app.post("/upload_zip")
async def upload_zip(
    file: UploadFile = File(...),
    x_user: Optional[str] = Header(default=None),
):
    user_id = get_user_id(x_user)
    paths = user_paths(user_id)

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a ZIP file.")

    content = await file.read()

    try:
        z = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file.")

    extracted = 0

    for name in z.namelist():
        if name.lower().endswith(".pdf"):
            extracted += 1
            safe_name = Path(name).name
            out_path = paths["udir"] / safe_name

            with z.open(name) as src, open(out_path, "wb") as dst:
                dst.write(src.read())

    return {"uploaded_pdfs": extracted}


@app.post("/ingest_user_pdfs")
def ingest_user_pdfs(
    payload: Optional[dict] = None,
    x_user: Optional[str] = Header(default=None),
):
    user_id = get_user_id(x_user)
    paths = user_paths(user_id)
    store = load_store_for_user(user_id)

    pdfs = list(paths["udir"].rglob("*.pdf"))

    if not pdfs:
        return {"added_chunks": 0, "files": 0, "message": "No PDFs found."}

    total_chunks = 0

    for pdf in pdfs:
        tag = infer_tag_from_filename(pdf.name)

        try:
            pages = read_pdf_pages(str(pdf))
        except Exception:
            continue

        all_chunks = []
        all_meta = []

        for page_no, page_text in pages:
            for ch in chunk_text(page_text):
                all_chunks.append(ch)
                all_meta.append(
                    {
                        "source": pdf.name,
                        "page": page_no,
                        "is_gold": False,
                    }
                )

        if all_chunks:
            store.add_chunks(all_chunks, tag=tag, meta_list=all_meta)
            total_chunks += len(all_chunks)

    save_store_for_user(user_id, store)

    return {
        "added_chunks": total_chunks,
        "files": len(pdfs),
        "message": "PDFs ingested successfully.",
    }


@app.post("/chat")
def chat_api(
    payload: dict,
    authorization: Optional[str] = Header(default=None),
    x_user: Optional[str] = Header(default=None),
):
    client = get_openai_client(authorization)

    user_id = get_user_id(x_user)
    store = load_store_for_user(user_id)

    question = (payload.get("question") or "").strip()
    tag = payload.get("tag")
    mode = payload.get("mode", "normal")

    if not question:
        raise HTTPException(status_code=400, detail="Missing question.")

    if not store.chunks:
        return {
            "answer": "No notes have been ingested yet. Upload and ingest PDFs first.",
            "sources": [],
        }

    summary_words = [
        "summary",
        "summarise",
        "summarize",
        "overview",
        "main points",
        "key points",
        "recap",
    ]

    if any(word in question.lower() for word in summary_words):
        search_query = "main concepts definitions examples key points overview"
        hits = store.search(search_query, k=8, tag=tag)

        if not hits:
            hits = store.search(search_query, k=8, tag=None)

        if not hits and store.chunks:
            hits = []
            for i, chunk in enumerate(store.chunks[:8]):
                meta = store.meta[i] if i < len(store.meta) else {}
                hits.append((0, 0, chunk, meta))
    else:
        hits = store.search(question, k=4, tag=tag)

        if not hits:
            hits = store.search(question, k=4, tag=None)

    if not hits:
        return {"answer": "Not found in your notes.", "sources": []}

    context = "\n\n---\n\n".join([h[2] for h in hits])
    sources = build_sources(hits)

    system = """You are OT AI Study Engine, a grounded study assistant.

RULES:
- Answer ONLY using the provided notes context.
- If the answer is not in the notes, say: "Not found in your notes."
- Be clear, structured, and useful for revision.
- Include headings or bullet points where helpful.
"""

    if mode == "exam":
        system += "\nEXAM MODE: Use concise bullet points and exam-style wording."

    try:
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"NOTES CONTEXT:\n{context}\n\nQUESTION:\n{question}",
                },
            ],
        )
    except Exception as err:
        safe_openai_error(err)

    return {
        "answer": resp.output_text,
        "sources": sources,
    }


@app.post("/flashcards")
def flashcards_api(
    payload: dict,
    authorization: Optional[str] = Header(default=None),
    x_user: Optional[str] = Header(default=None),
):
    client = get_openai_client(authorization)

    user_id = get_user_id(x_user)
    store = load_store_for_user(user_id)

    topic = (payload.get("topic") or "").strip()
    tag = payload.get("tag")

    if not topic:
        raise HTTPException(status_code=400, detail="Missing topic.")

    if not store.chunks:
        return {
            "flashcards": [
                {
                    "question": "No notes found.",
                    "answer": "Upload and ingest PDFs first.",
                }
            ]
        }

    if topic.lower().startswith("week"):
        tag = topic.lower()
        search_query = "main concepts definitions examples key points overview"
    else:
        search_query = topic

    hits = store.search(search_query, k=8, tag=tag)

    if not hits:
        hits = store.search(search_query, k=8, tag=None)

    if not hits and store.chunks:
        hits = []
        for i, chunk in enumerate(store.chunks[:8]):
            meta = store.meta[i] if i < len(store.meta) else {}
            hits.append((0, 0, chunk, meta))

    if not hits:
        return {"flashcards": []}

    context = "\n\n---\n\n".join([h[2] for h in hits])

    system = """
You are a flashcard generator for a study app.
Create exactly 6 revision flashcards from the notes.
Return ONLY valid JSON.
Format:
[
  {"question": "question here", "answer": "answer here"}
]
Only use the notes.
No markdown.
"""

    try:
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"NOTES:\n{context}\n\nTOPIC:\n{topic}"},
            ],
        )
    except Exception as err:
        safe_openai_error(err)

    raw = resp.output_text.strip()

    try:
        cards = json.loads(raw)
    except Exception:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        cards = json.loads(match.group(0)) if match else []

    return {"flashcards": cards}