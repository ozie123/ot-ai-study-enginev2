import os
import re
import json
import shlex
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import faiss
from dotenv import load_dotenv
from openai import OpenAI
from pypdf import PdfReader

# -----------------------
# Setup
# -----------------------
load_dotenv()

SYSTEM = """You are a study assistant.
RULES:
- If NOTES are provided, answer ONLY using the notes context.
- If the answer is not in the notes, say: "Not found in your notes."
- Be clear and structured.
"""


# -----------------------
# Helpers
# -----------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + max_chars])
        i += max_chars
    return chunks


def read_pdf_pages(path: str) -> list[tuple[int, str]]:
    """Returns list of (page_number_1based, text)."""
    reader = PdfReader(path)
    out: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        t = (page.extract_text() or "").strip()
        if t:
            out.append((i, t))
    return out


def safe_path(p: str) -> str:
    p = p.strip().strip('"').strip("'")
    return os.path.abspath(p)


def infer_tag_from_filename(filename: str) -> str:
    name = filename.lower()
    m = re.search(r"\b(?:week|wk)\s*([0-9]{1,2})\b", name)
    if m:
        return f"week{m.group(1)}"
    return "untagged"


def iter_pdfs(folder: str) -> list[Path]:
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        return []
    return sorted(p.rglob("*.pdf"))


def safe_remove(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def get_openai_client(api_key: Optional[str] = None) -> OpenAI:
    """
    Server mode: pass api_key from header.
    CLI mode: leave None and it uses OPENAI_API_KEY from .env.
    """
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "No OpenAI API key found. Set OPENAI_API_KEY in your .env (CLI) "
            "or pass api_key in server requests (Bearer token)."
        )
    return OpenAI(api_key=key)


# -----------------------
# Embeddings + FAISS store (PER USER)
# -----------------------
class NoteStore:
    def __init__(self, base_path: Path, api_key: Optional[str] = None):
        # where THIS user's data lives (backend/users/<id>/data)
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

        # per-user file paths
        self.faiss_path = self.base_path / "faiss.index"
        self.chunks_path = self.base_path / "chunks.json"
        self.stats_path = self.base_path / "stats.json"
        self.history_path = self.base_path / "history.jsonl"

        # OpenAI client (optional, can be supplied later)
        self._api_key = api_key
        self._client: Optional[OpenAI] = None

        # in-memory data
        self.chunks: list[str] = []
        self.tags: list[str] = []
        self.meta: list[dict] = []  # {"source": "...", "page": 1, "is_gold": bool}
        self.index = None
        self.dim: Optional[int] = None

    # ---------- per-user stats + history ----------
    def load_stats(self) -> dict:
        if not self.stats_path.exists():
            return {}
        try:
            with open(self.stats_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_stats(self, stats: dict):
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.stats_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    def log_event(self, obj: dict):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---------- embeddings ----------
    def set_api_key(self, api_key: str):
        self._api_key = api_key
        self._client = None  # reset so it recreates with new key

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = get_openai_client(self._api_key)
        return self._client

    def _embed(self, texts: list[str]) -> np.ndarray:
        client = self._get_client()
        resp = client.embeddings.create(model="text-embedding-3-small", input=texts)
        return np.array([d.embedding for d in resp.data], dtype="float32")

    # ---------- add / search ----------
    def add_chunks(self, chunks: list[str], tag: str, meta_list: list[dict]):
        if not chunks:
            print("No text found to add.")
            return
        if len(chunks) != len(meta_list):
            raise ValueError("chunks and meta_list must be same length")

        vectors = self._embed(chunks)
        self.dim = vectors.shape[1]

        if self.index is None:
            self.index = faiss.IndexFlatIP(self.dim)
            self.chunks = []
            self.tags = []
            self.meta = []

        # cosine similarity via normalized dot product
        faiss.normalize_L2(vectors)
        self.index.add(vectors)

        self.chunks.extend(chunks)
        self.tags.extend([tag] * len(chunks))
        self.meta.extend(meta_list)

        print(f"✅ Added {len(chunks)} chunks (tag: {tag}). Total chunks: {len(self.chunks)}")

    def add_notes(self, raw_text: str, tag: str = "untagged"):
        new_chunks = chunk_text(raw_text)
        if not new_chunks:
            print("No text found to add.")
            return
        meta_list = [{"source": "manual", "page": None, "is_gold": False} for _ in new_chunks]
        self.add_chunks(new_chunks, tag=tag, meta_list=meta_list)

    def add_gold(self, raw_text: str):
        new_chunks = chunk_text(raw_text)
        if not new_chunks:
            print("No text found to add.")
            return
        meta_list = [{"source": "GOLD", "page": None, "is_gold": True} for _ in new_chunks]
        self.add_chunks(new_chunks, tag="gold", meta_list=meta_list)

    def search(self, query: str, k: int = 4, tag: str | None = None, widen: int | None = None):
        """
        Returns list of (chunk_id, score, chunk_text, meta)
        score is cosine similarity (since vectors are normalized)
        """
        if self.index is None or not self.chunks:
            return []

        widen = widen or max(k * 10, 20)

        qv = self._embed([query])
        faiss.normalize_L2(qv)

        scores, idxs = self.index.search(qv, widen)

        results = []
        for score, i in zip(scores[0], idxs[0]):
            if i == -1:
                continue
            i = int(i)
            if tag is not None and self.tags[i] != tag:
                continue
            results.append((i, float(score), self.chunks[i], self.meta[i]))
        return results[:k]

    # ---------- persistence ----------
    def save(self):
        if self.index is None or not self.chunks:
            print("Nothing to save (no notes loaded).")
            return

        faiss.write_index(self.index, str(self.faiss_path))

        payload = {
            "chunks": self.chunks,
            "tags": self.tags,
            "meta": self.meta,
            "dim": self.dim,
        }
        with open(self.chunks_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        print(f"💾 Saved to {self.base_path}")

    def load(self) -> bool:
        if not (self.faiss_path.exists() and self.chunks_path.exists()):
            return False

        self.index = faiss.read_index(str(self.faiss_path))

        with open(self.chunks_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        self.chunks = payload.get("chunks", [])
        self.tags = payload.get("tags", ["untagged"] * len(self.chunks))
        self.meta = payload.get("meta", [{}] * len(self.chunks))
        self.dim = payload.get("dim", None)
        return True

    def clear(self, delete_files: bool = True):
        # clear memory
        self.index = None
        self.chunks = []
        self.tags = []
        self.meta = []
        self.dim = None

        # delete saved files
        if delete_files:
            safe_remove(str(self.faiss_path))
            safe_remove(str(self.chunks_path))
            safe_remove(str(self.stats_path))
            # keep history by default; uncomment if you want to wipe history too:
            # safe_remove(str(self.history_path))


# -----------------------
# App (factory)
# -----------------------
def get_store_for_user(user_id: str, api_key: Optional[str] = None) -> NoteStore:
    user_data_path = Path(__file__).parent / "users" / user_id / "data"
    user_data_path.mkdir(parents=True, exist_ok=True)

    store = NoteStore(base_path=user_data_path, api_key=api_key)
    store.load()
    return store


# -----------------------
# CLI Chat (optional)
# -----------------------
def chat():
    store = get_store_for_user("local-default")  # CLI user bucket
    MODE = "normal"  # normal | exam
    stats = store.load_stats()

    last_hit_ids: list[int] = []
    last_sources: list[str] = []
    last_question: str | None = None
    last_answer: str | None = None
    last_tag_filter: str | None = None
    last_top_score: float | None = None

    # tuning knobs
    FEEDBACK_ALPHA = 0.15
    GOLD_BONUS = 2.0
    CONFIDENCE_GATE = 0.25

    print("\nAI Study Assistant (RAG Notes Mode + Citations + Feedback + Teach)")
    print("Commands:")
    print("  /add --tag week4                 -> paste notes (end with /end)")
    print("  /addpdf <path> --tag week4       -> add notes from a PDF (captures page)")
    print("  /addfolder <folder>              -> add ALL PDFs from a folder (auto-tag)")
    print("  /teach <correction>              -> save GOLD knowledge (retrieved first)")
    print("  /good                            -> boost sources used in last answer")
    print("  /bad                             -> downweight sources used in last answer")
    print("  /status                          -> show what's loaded")
    print("  /mode normal|exam                -> switch answer style")
    print("  /clear                           -> clear notes (deletes saved files)")
    print("  /exit                            -> quit\n")

    while True:
        user_input = input("You: ").strip()

        if user_input.lower() in {"/exit", "exit", "quit"}:
            break

        try:
            parts = shlex.split(user_input)
        except ValueError:
            print("Invalid command formatting. If your path has spaces, wrap it in quotes.\n")
            continue

        if not parts:
            continue

        cmd = parts[0].lower()
        is_command = cmd.startswith("/")

        def get_flag_value(flag: str, default=None):
            if flag in parts:
                i = parts.index(flag)
                if i + 1 < len(parts):
                    return parts[i + 1]
            return default

        # -----------------------
        # Commands
        # -----------------------
        if is_command and cmd == "/status":
            total = len(store.chunks)
            counts: dict[str, int] = {}
            for t in store.tags:
                counts[t] = counts.get(t, 0) + 1

            print(f"\n📦 Loaded: {total} chunks")
            if total:
                print("🏷️  Tags:")
                for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
                    print(f"  - {t}: {c}")
            print(f"🧠 Mode: {MODE}")
            print(f"📊 Feedback entries: {len(stats)}\n")
            continue

        if is_command and cmd == "/mode":
            if len(parts) < 2 or parts[1].lower() not in {"normal", "exam"}:
                print("Usage: /mode normal|exam\n")
                continue
            MODE = parts[1].lower()
            print(f"✅ Mode set to: {MODE}\n")
            continue

        if is_command and cmd == "/clear":
            store.clear(delete_files=True)
            stats = {}
            last_hit_ids = []
            last_sources = []
            last_question = None
            last_answer = None
            last_tag_filter = None
            last_top_score = None

            print("🧹 Notes cleared (memory + saved files). Stats cleared.\n")
            store.log_event({"type": "clear", "ts": now_iso()})
            continue

        if is_command and cmd == "/add":
            tag = get_flag_value("--tag", "untagged")
            print("\nPaste your notes now. When finished, type /end on its own line.\n")
            lines = []
            while True:
                line = input()
                if line.strip() == "/end":
                    break
                lines.append(line)

            raw = "\n".join(lines)
            store.add_notes(raw, tag=tag)
            store.save()
            print()
            store.log_event({"type": "add_manual", "ts": now_iso(), "tag": tag, "chars": len(raw)})
            continue

        if is_command and cmd == "/teach":
            taught = user_input[len("/teach") :].strip()
            if not taught:
                print("Usage: /teach <your correction>\n")
                continue

            store.add_gold(taught)
            store.save()
            print("✅ Saved as GOLD knowledge.\n")
            store.log_event({"type": "teach", "ts": now_iso(), "text": taught})
            continue

        if is_command and cmd in {"/good", "/bad"}:
            if not last_hit_ids:
                print("No previous answer to rate.\n")
                continue

            rating = "good" if cmd == "/good" else "bad"
            for cid in last_hit_ids:
                key = str(cid)
                if key not in stats:
                    stats[key] = {"good": 0, "bad": 0}
                stats[key][rating] += 1

            store.save_stats(stats)
            print("✅ Feedback saved.\n")

            store.log_event(
                {
                    "type": "feedback",
                    "ts": now_iso(),
                    "rating": rating,
                    "chunk_ids": last_hit_ids,
                    "question": last_question,
                    "sources": last_sources,
                }
            )
            continue

        if is_command and cmd == "/addpdf":
            if len(parts) < 2:
                print("Usage: /addpdf docs/yourfile.pdf --tag week4\n")
                continue

            tag = get_flag_value("--tag", "untagged")
            pdf_path = safe_path(parts[1])

            if not os.path.exists(pdf_path):
                print("File not found. Check the path.\n")
                continue

            try:
                pages = read_pdf_pages(pdf_path)
            except Exception as e:
                print(f"Failed to read PDF: {e}\n")
                continue

            if not pages:
                print("No extractable text found in that PDF.\n")
                continue

            filename = os.path.basename(pdf_path)
            all_chunks = []
            all_meta = []

            for page_no, page_text in pages:
                for ch in chunk_text(page_text):
                    all_chunks.append(ch)
                    all_meta.append({"source": filename, "page": page_no, "is_gold": False})

            store.add_chunks(all_chunks, tag=tag, meta_list=all_meta)
            store.save()
            print(f"✅ Added PDF: {filename}\n")

            store.log_event({"type": "add_pdf", "ts": now_iso(), "tag": tag, "file": filename, "pages": len(pages)})
            continue

        if is_command and cmd == "/addfolder":
            if len(parts) < 2:
                print("Usage: /addfolder docs --tag week4\n")
                continue

            folder = safe_path(parts[1])
            forced_tag = get_flag_value("--tag", None)

            pdfs = iter_pdfs(folder)
            if not pdfs:
                print("No PDFs found in that folder.\n")
                continue

            added_files = 0
            for pdf in pdfs:
                tag = forced_tag or infer_tag_from_filename(pdf.name)

                try:
                    pages = read_pdf_pages(str(pdf))
                except Exception as e:
                    print(f"⚠️ Skipped {pdf.name} (read error): {e}")
                    continue

                if not pages:
                    print(f"⚠️ Skipped {pdf.name} (no extractable text)")
                    continue

                all_chunks = []
                all_meta = []
                for page_no, page_text in pages:
                    for ch in chunk_text(page_text):
                        all_chunks.append(ch)
                        all_meta.append({"source": pdf.name, "page": page_no, "is_gold": False})

                store.add_chunks(all_chunks, tag=tag, meta_list=all_meta)
                added_files += 1

            store.save()
            print(f"\n✅ Done. Added {added_files}/{len(pdfs)} PDFs from {folder}\n")

            store.log_event(
                {
                    "type": "add_folder",
                    "ts": now_iso(),
                    "folder": folder,
                    "forced_tag": forced_tag,
                    "files_added": added_files,
                    "files_found": len(pdfs),
                }
            )
            continue

        if is_command:
            print("Unknown command.\n")
            continue

        # -----------------------
        # Normal question
        # -----------------------
        tag_filter = None
        question = user_input

        # parse tag prefix: "week4: explain ..."
        if ":" in user_input:
            prefix, rest = user_input.split(":", 1)
            prefix = prefix.strip().lower()
            if prefix in set(store.tags):
                tag_filter = prefix
                question = rest.strip()

        candidates = store.search(question, k=40, tag=tag_filter)
        if not candidates:
            print("\nAssistant: Not found in your notes.\n")
            continue

        def adjusted_score(hit):
            cid, score, _chunk, meta = hit
            fb = stats.get(str(cid), {"good": 0, "bad": 0})
            w = fb["good"] - fb["bad"]
            adj = score + FEEDBACK_ALPHA * w
            if meta.get("is_gold"):
                adj += GOLD_BONUS
            return adj

        reranked = sorted(candidates, key=adjusted_score, reverse=True)
        hits = reranked[:4]
        top_score = hits[0][1] if hits else None

        if top_score is not None and top_score < CONFIDENCE_GATE:
            print("\nAssistant: Not found in your notes.\n")
            store.log_event(
                {
                    "type": "qa_refused_low_conf",
                    "ts": now_iso(),
                    "question": question,
                    "tag_filter": tag_filter,
                    "top_score": top_score,
                }
            )
            continue

        last_hit_ids = [cid for cid, _score, _chunk, _meta in hits]
        last_tag_filter = tag_filter
        last_top_score = top_score

        context = "\n\n---\n\n".join([chunk for _cid, _score, chunk, _meta in hits])

        sources = []
        for _cid, _score, _chunk, meta in hits:
            src = meta.get("source", "unknown")
            page = meta.get("page", "?")
            sources.append(f"{src} (page {page})")

        extra = ""
        if MODE == "exam":
            extra = (
                "\nEXAM MODE:\n"
                "- Answer in bullet points.\n"
                "- Be concise.\n"
                "- If unsure, say: Not found in your notes.\n"
            )

        client = get_openai_client()  # CLI uses .env key
        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": SYSTEM + extra},
                {"role": "user", "content": f"NOTES CONTEXT:\n{context}\n\nQUESTION:\n{question}"},
            ],
            temperature=0.2,
        )

        answer_text = resp.output_text.strip()

        if answer_text.lower() == "not found in your notes.":
            last_question = question
            last_answer = answer_text
            last_sources = []
            print("\nAssistant: Not found in your notes.\n")
            store.log_event(
                {
                    "type": "qa_refused_model",
                    "ts": now_iso(),
                    "mode": MODE,
                    "tag_filter": tag_filter,
                    "question": question,
                    "top_score": top_score,
                    "chunk_ids": last_hit_ids,
                }
            )
            continue

        last_question = question
        last_answer = answer_text
        last_sources = sorted(set(sources))

        print(f"\nAssistant: {answer_text}\n")
        print("Sources:")
        for s in last_sources:
            print(f"- {s}")
        print()

        store.log_event(
            {
                "type": "qa",
                "ts": now_iso(),
                "mode": MODE,
                "tag_filter": tag_filter,
                "question": question,
                "answer": answer_text,
                "sources": last_sources,
                "chunk_ids": last_hit_ids,
                "top_score": top_score,
            }
        )


if __name__ == "__main__":
    chat()
