from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import faiss
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

try:
    from .ai_service import (
        EMBEDDING_MODEL,
        RESPONSE_MODEL,
        create_openai_client,
        generate_answer,
        generate_flashcards,
        raise_openai_http_error,
        resolve_api_key,
    )
    from .core import NoteStore, chunk_text, infer_tag_from_filename, read_pdf_pages
except ImportError:  # Supports `uvicorn server:app --app-dir backend`.
    from ai_service import (  # type: ignore[no-redef]
        EMBEDDING_MODEL,
        RESPONSE_MODEL,
        create_openai_client,
        generate_answer,
        generate_flashcards,
        raise_openai_http_error,
        resolve_api_key,
    )
    from core import (  # type: ignore[no-redef]
        NoteStore,
        chunk_text,
        infer_tag_from_filename,
        read_pdf_pages,
    )


APP_VERSION = "1.1.0"
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def resolve_data_root(value: str | None) -> Path:
    override = (value or "").strip()
    return (Path(override).expanduser() if override else BASE_DIR / "backend" / "users").resolve()


USERS_DIR = resolve_data_root(os.getenv("OT_DATA_DIR"))

MAX_UPLOAD_BYTES = _env_int(
    "MAX_UPLOAD_BYTES", 25 * 1024 * 1024, minimum=1024, maximum=100 * 1024 * 1024
)
MAX_ZIP_MEMBERS = _env_int("MAX_ZIP_MEMBERS", 50, minimum=1, maximum=500)
MAX_PDF_BYTES = _env_int("MAX_PDF_BYTES", 12 * 1024 * 1024, minimum=1024, maximum=50 * 1024 * 1024)
MAX_TOTAL_UNCOMPRESSED_BYTES = _env_int(
    "MAX_TOTAL_UNCOMPRESSED_BYTES",
    50 * 1024 * 1024,
    minimum=1024,
    maximum=250 * 1024 * 1024,
)
MAX_USER_STORAGE_BYTES = _env_int(
    "MAX_USER_STORAGE_BYTES", 100 * 1024 * 1024, minimum=1024, maximum=1024 * 1024 * 1024
)
MAX_PDF_PAGES = _env_int("MAX_PDF_PAGES", 300, minimum=1, maximum=2000)
MAX_PDF_TEXT_CHARS = _env_int("MAX_PDF_TEXT_CHARS", 2_000_000, minimum=1000, maximum=10_000_000)
MAX_INGEST_CHUNKS = _env_int("MAX_INGEST_CHUNKS", 2500, minimum=1, maximum=10_000)
MAX_COMPRESSION_RATIO = 100
READ_CHUNK_BYTES = 1024 * 1024

USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

logger = logging.getLogger("ot_ai_study_engine")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestRequest(StrictRequest):
    tag: str | None = Field(default=None, min_length=1, max_length=32)


class ChatRequest(StrictRequest):
    question: str = Field(min_length=1, max_length=2000)
    tag: str | None = Field(default=None, min_length=1, max_length=32)
    mode: Literal["normal", "exam"] = "normal"


class FlashcardRequest(StrictRequest):
    topic: str = Field(min_length=1, max_length=500)
    tag: str | None = Field(default=None, min_length=1, max_length=32)


app = FastAPI(
    title="OT AI Study Engine",
    version=APP_VERSION,
    description="Grounded question answering and flashcards for uploaded study notes.",
)

allowed_origins = [
    origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-User", "X-Request-ID"],
    )

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.middleware("http")
async def add_security_and_request_headers(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "")
    request_id = (
        supplied_request_id
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied_request_id)
        else str(uuid.uuid4())
    )
    started = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    logger.info(
        "request_complete method=%s path=%s status=%s duration_ms=%d request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        int((time.monotonic() - started) * 1000),
        request_id,
    )
    return response


_locks_guard = threading.Lock()
_user_locks: dict[str, threading.Lock] = {}


@contextmanager
def user_lock(user_id: str) -> Iterator[None]:
    with _locks_guard:
        lock = _user_locks.setdefault(user_id, threading.Lock())
    with lock:
        yield


def get_user_id(x_user: str | None) -> str:
    user_id = (x_user or "local-default").strip()
    if not USER_ID_PATTERN.fullmatch(user_id):
        raise HTTPException(status_code=400, detail="Invalid X-User identifier.")
    return user_id


def validate_tag(tag: str | None) -> str | None:
    if tag is None:
        return None
    tag = tag.strip().lower()
    if not TAG_PATTERN.fullmatch(tag):
        raise HTTPException(
            status_code=400,
            detail="Tags may contain only letters, numbers, underscores, and hyphens.",
        )
    return tag


def user_paths(user_id: str) -> dict[str, Path]:
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    users_root = USERS_DIR.resolve()
    user_dir = (users_root / user_id).resolve()
    try:
        user_dir.relative_to(users_root)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid user storage path.") from error

    data_dir = user_dir / "data"
    uploads_dir = user_dir / "uploads"
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return {
        "user_dir": user_dir,
        "data_dir": data_dir,
        "uploads_dir": uploads_dir,
        "faiss": data_dir / "faiss.index",
        "chunks": data_dir / "chunks.json",
    }


def load_store_for_user(user_id: str, api_key: str | None = None) -> NoteStore:
    paths = user_paths(user_id)
    store = NoteStore(base_path=paths["data_dir"], api_key=api_key)

    if paths["faiss"].exists() and paths["chunks"].exists():
        try:
            index = faiss.read_index(str(paths["faiss"]))
            store.index = index
            with paths["chunks"].open("r", encoding="utf-8") as file_handle:
                payload = json.load(file_handle)
            store.chunks = payload.get("chunks", [])
            store.tags = payload.get("tags", ["untagged"] * len(store.chunks))
            store.meta = payload.get("meta", [{}] * len(store.chunks))
            store.dim = payload.get("dim")
            if not (len(store.chunks) == len(store.tags) == len(store.meta) == index.ntotal):
                raise ValueError("Study index files are inconsistent")
        except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
            logger.error("study_index_load_failed user_id=%s", user_id)
            raise HTTPException(
                status_code=500,
                detail="This study index could not be loaded. Re-ingest the source PDFs.",
            ) from error
    return store


def save_store_for_user(user_id: str, store: NoteStore) -> None:
    if store.index is None or not store.chunks:
        return

    paths = user_paths(user_id)
    index_temp = paths["faiss"].with_suffix(".index.tmp")
    chunks_temp = paths["chunks"].with_suffix(".json.tmp")
    payload = {
        "chunks": store.chunks,
        "tags": store.tags,
        "meta": store.meta,
        "dim": store.dim,
    }
    try:
        faiss.write_index(store.index, str(index_temp))
        with chunks_temp.open("w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, ensure_ascii=False)
        os.replace(index_temp, paths["faiss"])
        os.replace(chunks_temp, paths["chunks"])
    finally:
        index_temp.unlink(missing_ok=True)
        chunks_temp.unlink(missing_ok=True)


def build_sources(hits) -> list[str]:
    sources: set[str] = set()
    for hit in hits:
        source = str(hit[3].get("source", "unknown"))
        page = hit[3].get("page")
        sources.add(f"{source} (page {page})" if page else source)
    return sorted(sources)


def select_hits(store: NoteStore, query: str, *, k: int, tag: str | None):
    # An explicit study scope must never fall back to unrelated notes.
    return store.search(query, k=k, tag=tag)


async def read_upload_limited(file: UploadFile) -> bytes:
    content = bytearray()
    while chunk := await file.read(READ_CHUNK_BYTES):
        content.extend(chunk)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"ZIP exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
            )
    return bytes(content)


def safe_pdf_name(member_name: str) -> str:
    name = Path(member_name.replace("\\", "/")).name.strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    if not name or not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="ZIP contains an invalid PDF filename.")
    stem = Path(name).stem[:170].rstrip(". ") or "document"
    return f"{stem}.pdf"


def extract_valid_pdfs(content: bytes) -> list[tuple[str, bytes]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=400, detail="Invalid ZIP file.") from error

    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > MAX_ZIP_MEMBERS:
            raise HTTPException(
                status_code=413,
                detail=f"ZIP contains more than {MAX_ZIP_MEMBERS} files.",
            )

        pdf_members = [item for item in members if item.filename.lower().endswith(".pdf")]
        if not pdf_members:
            raise HTTPException(status_code=400, detail="ZIP does not contain any PDF files.")

        total_size = sum(item.file_size for item in pdf_members)
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise HTTPException(status_code=413, detail="Expanded PDFs exceed the safety limit.")

        seen_names: set[str] = set()
        extracted: list[tuple[str, bytes]] = []
        for member in pdf_members:
            if member.flag_bits & 0x1:
                raise HTTPException(
                    status_code=400, detail="Encrypted ZIP members are not supported."
                )
            if member.file_size > MAX_PDF_BYTES:
                raise HTTPException(
                    status_code=413, detail="A PDF exceeds the per-file size limit."
                )
            if (
                member.compress_size > 0
                and member.file_size > READ_CHUNK_BYTES
                and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise HTTPException(status_code=413, detail="ZIP compression ratio is unsafe.")

            filename = safe_pdf_name(member.filename)
            normalized = filename.casefold()
            if normalized in seen_names:
                raise HTTPException(status_code=400, detail="ZIP contains duplicate PDF filenames.")
            seen_names.add(normalized)

            try:
                with archive.open(member) as source:
                    pdf_bytes = source.read(MAX_PDF_BYTES + 1)
            except (zipfile.BadZipFile, EOFError, OSError) as error:
                raise HTTPException(status_code=400, detail="Invalid or corrupt ZIP file.") from error
            if len(pdf_bytes) > MAX_PDF_BYTES:
                raise HTTPException(
                    status_code=413, detail="A PDF exceeds the per-file size limit."
                )
            if b"%PDF-" not in pdf_bytes[:1024]:
                raise HTTPException(status_code=400, detail=f"{filename} is not a valid PDF file.")
            extracted.append((filename, pdf_bytes))

    return extracted


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


@app.get("/")
def home():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "app": "OT AI Study Engine", "version": APP_VERSION}


@app.get("/ready")
def readiness():
    if not (FRONTEND_DIR / "index.html").is_file():
        return JSONResponse(status_code=503, content={"ready": False})
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    if not os.access(USERS_DIR, os.W_OK):
        return JSONResponse(status_code=503, content={"ready": False})
    return {"ready": True}


@app.get("/config")
def public_config():
    return {
        "server_managed_ai": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "response_model": RESPONSE_MODEL,
        "embedding_model": EMBEDDING_MODEL,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
    }


@app.get("/status")
def status(x_user: str | None = Header(default=None)):
    user_id = get_user_id(x_user)
    store = load_store_for_user(user_id)
    sources = sorted({str(item.get("source", "unknown")) for item in store.meta})
    return {
        "chunks": len(store.chunks),
        "documents": len(sources),
        "tags": sorted(set(store.tags)) if store.tags else [],
        "ready": bool(store.chunks),
    }


@app.post("/upload_zip")
async def upload_zip(file: UploadFile = File(...), x_user: str | None = Header(default=None)):
    user_id = get_user_id(x_user)
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a ZIP file.")

    try:
        content = await read_upload_limited(file)
    finally:
        await file.close()
    pdfs = extract_valid_pdfs(content)
    paths = user_paths(user_id)

    with user_lock(user_id):
        existing_sizes = {
            path.name.casefold(): path.stat().st_size
            for path in paths["uploads_dir"].glob("*.pdf")
            if path.is_file()
        }
        retained_size = sum(existing_sizes.values())
        for filename, _ in pdfs:
            retained_size -= existing_sizes.get(filename.casefold(), 0)
        projected_size = retained_size + sum(len(pdf_bytes) for _, pdf_bytes in pdfs)
        if projected_size > MAX_USER_STORAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="This study session has reached its document storage limit.",
            )

        for filename, pdf_bytes in pdfs:
            destination = paths["uploads_dir"] / filename
            temporary = destination.with_suffix(".pdf.part")
            try:
                temporary.write_bytes(pdf_bytes)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    return {"uploaded_pdfs": len(pdfs), "filenames": [name for name, _ in pdfs]}


@app.post("/ingest_user_pdfs")
def ingest_user_pdfs(
    payload: IngestRequest | None = None,
    authorization: str | None = Header(default=None),
    x_user: str | None = Header(default=None),
):
    user_id = get_user_id(x_user)
    requested_tag = validate_tag(payload.tag if payload else None)
    api_key = resolve_api_key(authorization)
    paths = user_paths(user_id)

    with user_lock(user_id):
        store = load_store_for_user(user_id, api_key=api_key)
        pdfs = sorted(paths["user_dir"].rglob("*.pdf"))
        if not pdfs:
            return {"added_chunks": 0, "files": 0, "message": "No PDFs found."}

        known_hashes = {
            str(item.get("document_sha256")) for item in store.meta if item.get("document_sha256")
        }
        all_chunks: list[str] = []
        all_tags: list[str] = []
        all_meta: list[dict] = []
        ingested_files = 0
        duplicate_files = 0
        skipped_files = 0

        for pdf in pdfs:
            document_hash = file_sha256(pdf)
            if document_hash in known_hashes:
                duplicate_files += 1
                continue

            try:
                pages = read_pdf_pages(
                    str(pdf),
                    max_pages=MAX_PDF_PAGES,
                    max_chars=MAX_PDF_TEXT_CHARS,
                )
            except Exception:
                skipped_files += 1
                logger.warning("pdf_extract_failed filename=%s", pdf.name)
                continue

            document_chunks: list[str] = []
            document_meta: list[dict] = []
            for page_number, page_text in pages:
                for text_chunk in chunk_text(page_text):
                    document_chunks.append(text_chunk)
                    document_meta.append(
                        {
                            "source": pdf.name,
                            "page": page_number,
                            "is_gold": False,
                            "document_sha256": document_hash,
                        }
                    )

            if not document_chunks:
                skipped_files += 1
                continue
            if len(all_chunks) + len(document_chunks) > MAX_INGEST_CHUNKS:
                raise HTTPException(
                    status_code=413,
                    detail=f"Ingestion exceeds the {MAX_INGEST_CHUNKS}-chunk safety limit.",
                )

            document_tag = requested_tag or infer_tag_from_filename(pdf.name)
            all_chunks.extend(document_chunks)
            all_tags.extend([document_tag] * len(document_chunks))
            all_meta.extend(document_meta)
            ingested_files += 1
            known_hashes.add(document_hash)

        if all_chunks:
            try:
                # Add by tag so existing NoteStore metadata remains backward-compatible.
                start = 0
                while start < len(all_chunks):
                    tag = all_tags[start]
                    end = start + 1
                    while end < len(all_chunks) and all_tags[end] == tag:
                        end += 1
                    store.add_chunks(all_chunks[start:end], tag=tag, meta_list=all_meta[start:end])
                    start = end
                save_store_for_user(user_id, store)
            except HTTPException:
                raise
            except Exception as error:
                raise_openai_http_error(error)

    return {
        "added_chunks": len(all_chunks),
        "files": ingested_files,
        "already_ingested": duplicate_files,
        "skipped_files": skipped_files,
        "message": "PDFs ingested successfully." if all_chunks else "No new PDFs to ingest.",
    }


@app.post("/chat")
def chat_api(
    payload: ChatRequest,
    authorization: str | None = Header(default=None),
    x_user: str | None = Header(default=None),
):
    user_id = get_user_id(x_user)
    tag = validate_tag(payload.tag)
    api_key = resolve_api_key(authorization)
    store = load_store_for_user(user_id, api_key=api_key)

    if not store.chunks:
        return {
            "status": "not_ready",
            "answer": "No notes have been ingested yet. Upload and ingest PDFs first.",
            "sources": [],
        }

    summary_terms = (
        "summary",
        "summarise",
        "summarize",
        "overview",
        "main points",
        "key points",
        "recap",
    )
    is_summary = any(term in payload.question.lower() for term in summary_terms)
    query = payload.question
    try:
        hits = select_hits(store, query, k=8 if is_summary else 4, tag=tag)
    except Exception as error:
        raise_openai_http_error(error)
    if not hits:
        return {"status": "not_found", "answer": "Not found in your notes.", "sources": []}

    context = "\n\n---\n\n".join(hit[2] for hit in hits)
    try:
        answer = generate_answer(
            create_openai_client(api_key),
            context=context,
            question=payload.question.strip(),
            mode=payload.mode,
        )
    except Exception as error:
        raise_openai_http_error(error)
    if answer.strip().casefold() == "not found in your notes.":
        return {"status": "not_found", "answer": answer, "sources": []}
    return {"status": "answered", "answer": answer, "sources": build_sources(hits)}


@app.post("/flashcards")
def flashcards_api(
    payload: FlashcardRequest,
    authorization: str | None = Header(default=None),
    x_user: str | None = Header(default=None),
):
    user_id = get_user_id(x_user)
    tag = validate_tag(payload.tag)
    api_key = resolve_api_key(authorization)
    store = load_store_for_user(user_id, api_key=api_key)

    if not store.chunks:
        return {"flashcards": [], "message": "Upload and ingest PDFs first."}

    topic = payload.topic.strip()
    query = topic
    if tag is None and re.fullmatch(r"week\s*\d{1,2}", topic, flags=re.IGNORECASE):
        tag = validate_tag(re.sub(r"\s+", "", topic.lower()))
        query = "main concepts definitions examples key points overview"
    try:
        hits = select_hits(store, query, k=8, tag=tag)
    except Exception as error:
        raise_openai_http_error(error)
    if not hits:
        return {"flashcards": [], "message": "No matching notes were found."}

    context = "\n\n---\n\n".join(hit[2] for hit in hits)
    try:
        cards = generate_flashcards(
            create_openai_client(api_key),
            context=context,
            topic=topic,
        )
    except Exception as error:
        raise_openai_http_error(error)
    return {"flashcards": cards, "sources": build_sources(hits)}
