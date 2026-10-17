import json
import os
import re
import shlex
import tempfile
import zipfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import faiss
import numpy as np
from openai import OpenAI
from pypdf import PdfReader

try:
    from .ai_service import EMBEDDING_MODEL, create_openai_client
    from .private_storage import open_private_regular_file
except ImportError:  # Supports the optional `python core.py` CLI workflow.
    from ai_service import EMBEDDING_MODEL, create_openai_client  # type: ignore[no-redef]
    from private_storage import open_private_regular_file  # type: ignore[no-redef]

# -----------------------
# Setup
# -----------------------
SYSTEM = """You are a study assistant.
RULES:
- If NOTES are provided, answer ONLY using the notes context.
- If the answer is not in the notes, say: "Not found in your notes."
- Be clear and structured.
"""

DEFAULT_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_SNAPSHOT_MEMBERS = 1002


# -----------------------
# Helpers
# -----------------------
def now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


def read_pdf_pages(
    path: str | BinaryIO,
    max_pages: int | None = None,
    max_chars: int | None = None,
) -> list[tuple[int, str]]:
    """Returns list of (page_number_1based, text)."""
    reader = PdfReader(path)
    if max_pages is not None and len(reader.pages) > max_pages:
        raise ValueError(f"PDF exceeds the {max_pages}-page limit")
    out: list[tuple[int, str]] = []
    total_chars = 0
    for i, page in enumerate(reader.pages, start=1):
        t = (page.extract_text() or "").strip()
        if t:
            total_chars += len(t)
            if max_chars is not None and total_chars > max_chars:
                raise ValueError("PDF contains too much extractable text")
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


def _configured_limit(
    name: str,
    supplied: int | None,
    default: int,
    *,
    maximum: int,
) -> int:
    raw = os.getenv(name, "").strip() if supplied is None else str(supplied)
    try:
        value = int(raw) if raw else default
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not 1 <= value <= maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}")
    return value


def _fsync_directory(path: Path) -> None:
    """Flush directory metadata where the platform exposes that primitive."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def get_openai_client(api_key: str | None = None) -> OpenAI:
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
    return create_openai_client(key)


# -----------------------
# Embeddings + FAISS store (PER USER)
# -----------------------
class NoteStore:
    def __init__(
        self,
        base_path: Path,
        api_key: str | None = None,
        *,
        max_snapshot_bytes: int | None = None,
        max_snapshot_members: int | None = None,
    ):
        # where THIS user's data lives (backend/users/<id>/data)
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

        # per-user file paths
        self.faiss_path = self.base_path / "faiss.index"
        self.chunks_path = self.base_path / "chunks.json"
        self.snapshot_path = self.base_path / "study.snapshot"
        self.stats_path = self.base_path / "stats.json"
        self.history_path = self.base_path / "history.jsonl"

        # OpenAI client (optional, can be supplied later)
        self._api_key = api_key
        self._client: OpenAI | None = None
        self.max_snapshot_bytes = _configured_limit(
            "MAX_SNAPSHOT_BYTES",
            max_snapshot_bytes,
            DEFAULT_MAX_SNAPSHOT_BYTES,
            maximum=8 * 1024 * 1024 * 1024,
        )
        self.max_snapshot_members = _configured_limit(
            "MAX_SNAPSHOT_MEMBERS",
            max_snapshot_members,
            DEFAULT_MAX_SNAPSHOT_MEMBERS,
            maximum=20_000,
        )

        # in-memory data
        self.chunks: list[str] = []
        self.tags: list[str] = []
        self.meta: list[dict] = []  # {"source": "...", "page": 1, "is_gold": bool}
        self.index: faiss.Index | None = None
        self.dim: int | None = None
        # None marks a pre-document snapshot, requiring controlled API migration.
        self.documents: dict[str, bytes] | None = None
        self.pending: dict[str, bytes] = {}

    # ---------- per-user stats + history ----------
    def load_stats(self) -> dict:
        if not self.stats_path.exists():
            return {}
        try:
            with open(self.stats_path, encoding="utf-8") as f:
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
        embeddings: list[list[float]] = []
        for start in range(0, len(texts), 64):
            batch = texts[start : start + 64]
            response = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            embeddings.extend(item.embedding for item in response.data)
        return np.array(embeddings, dtype="float32")

    # ---------- add / search ----------
    def add_chunks(self, chunks: list[str], tag: str, meta_list: list[dict]):
        if not chunks:
            print("No text found to add.")
            return
        if len(chunks) != len(meta_list):
            raise ValueError("chunks and meta_list must be same length")

        vectors = self._embed(chunks)
        self.dim = vectors.shape[1]

        if self.index is None or self.index.ntotal == 0:
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

        print(f"Added {len(chunks)} chunks (tag: {tag}). Total chunks: {len(self.chunks)}")

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
        if self.index is None or not self.chunks or k <= 0:
            return []

        # Filter before ranking: a small global shortlist can omit every
        # matching chunk even though the requested tag exists in the index.
        scoped_ids = None
        if tag is not None:
            scoped_ids = [i for i, value in enumerate(self.tags) if value == tag]
            if not scoped_ids:
                return []

        qv = self._embed([query])
        faiss.normalize_L2(qv)

        if scoped_ids is not None:
            vectors = np.array([self.index.reconstruct(i) for i in scoped_ids], dtype="float32")
            scoped_scores = vectors @ qv[0]
            order = np.argsort(-scoped_scores, kind="stable")[:k]
            return [
                (
                    scoped_ids[position],
                    float(scoped_scores[position]),
                    self.chunks[scoped_ids[position]],
                    self.meta[scoped_ids[position]],
                )
                for position in order
            ]

        scores, idxs = self.index.search(qv, min(max(k, widen or k), len(self.chunks)))

        results = []
        for score, i in zip(scores[0], idxs[0], strict=True):
            if i == -1:
                continue
            i = int(i)
            if tag is not None and self.tags[i] != tag:
                continue
            results.append((i, float(score), self.chunks[i], self.meta[i]))
        return results[:k]

    # ---------- persistence ----------
    def save(self):
        # Persist an empty index too: deleting the last document must survive reload.
        index = self.index if self.index is not None else faiss.IndexFlatIP(self.dim or 1)

        payload = {
            "chunks": self.chunks,
            "tags": self.tags,
            "meta": self.meta,
            "dim": self.dim if self.index is not None else index.d,
        }
        self._validate_snapshot(index, payload)
        if self.documents is not None:
            payload["documents"] = list(self.documents)
            payload["pending"] = list(self.pending)
        # A single replacement publishes vectors and metadata together. Never
        # overwrite the active snapshot until both have been flushed to disk,
        # then flush the directory entry so publication survives power loss.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.base_path, prefix="snapshot-", suffix=".tmp", delete=False
            ) as f:
                temporary = Path(f.name)
                with zipfile.ZipFile(f, "w") as archive:
                    archive.writestr("index", faiss.serialize_index(index).tobytes())
                    archive.writestr("metadata", json.dumps(payload, ensure_ascii=False))
                    if self.documents is not None:
                        for group, files in (
                            ("documents", self.documents),
                            ("pending", self.pending),
                        ):
                            for number, content in enumerate(files.values()):
                                archive.writestr(f"{group}/{number}", content)
                f.flush()
                with zipfile.ZipFile(f) as archive:
                    self._validate_snapshot_archive_limits(
                        archive, stored_bytes=os.fstat(f.fileno()).st_size
                    )
                os.fsync(f.fileno())
            os.replace(temporary, self.snapshot_path)
            _fsync_directory(self.base_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

        # Publication already succeeded; a closed/limited console must not turn
        # a committed mutation into a reported save failure.
        with suppress(OSError, UnicodeError):
            print(f"Saved to {self.base_path}")

    def load(self) -> bool:
        documents = None
        pending = {}
        if self.snapshot_path.exists() or self.snapshot_path.is_symlink():
            descriptor = open_private_regular_file(self.snapshot_path, os.O_RDONLY)
            try:
                stored_bytes = os.fstat(descriptor).st_size
                if stored_bytes > self.max_snapshot_bytes:
                    raise ValueError("Study snapshot exceeds configured size limit")
                with os.fdopen(descriptor, "rb") as snapshot:
                    descriptor = -1
                    with zipfile.ZipFile(snapshot) as archive:
                        members = self._validate_snapshot_archive_limits(
                            archive, stored_bytes=stored_bytes
                        )
                        member_names = [member.filename for member in members]
                        if (
                            len(member_names) != len(set(member_names))
                            or not {"index", "metadata"}.issubset(member_names)
                        ):
                            raise ValueError("Invalid study snapshot archive")
                        index = faiss.deserialize_index(
                            np.frombuffer(archive.read("index"), dtype="uint8").copy()
                        )
                        payload = json.loads(archive.read("metadata"))
                        expected_members = {"index", "metadata"}
                        if isinstance(payload, dict) and "documents" in payload:
                            groups = []
                            for group in ("documents", "pending"):
                                names = payload.get(group)
                                if (
                                    not isinstance(names, list)
                                    or not all(
                                        isinstance(name, str)
                                        and name.lower().endswith(".pdf")
                                        and "/" not in name
                                        and "\\" not in name
                                        for name in names
                                    )
                                    or len({name.casefold() for name in names}) != len(names)
                                ):
                                    raise ValueError("Invalid document manifest")
                                expected_members.update(
                                    f"{group}/{i}" for i in range(len(names))
                                )
                                groups.append(
                                    {
                                        name: archive.read(f"{group}/{i}")
                                        for i, name in enumerate(names)
                                    }
                                )
                            documents, pending = groups
                        if set(member_names) != expected_members:
                            raise ValueError("Invalid study snapshot archive")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        elif self.faiss_path.exists() or self.chunks_path.exists():
            # Read-only legacy migration: the next save publishes a snapshot;
            # retain the old pair, but never fall back once a snapshot exists.
            if not (self.faiss_path.exists() and self.chunks_path.exists()):
                raise ValueError("Incomplete legacy study index")
            index = faiss.read_index(str(self.faiss_path))
            with self.chunks_path.open(encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                payload.setdefault("tags", ["untagged"] * len(payload.get("chunks", [])))
                payload.setdefault("meta", [{}] * len(payload.get("chunks", [])))
                payload.setdefault("dim", index.d)
        else:
            return False

        self._validate_snapshot(index, payload)
        self.index = index
        self.chunks = payload["chunks"]
        self.tags = payload["tags"]
        self.meta = payload["meta"]
        self.dim = payload["dim"]
        self.documents = documents
        self.pending = pending
        return True

    def remove_documents(self, names: set[str]) -> None:
        """Remove all versions by canonical filename, retaining unrelated vectors."""
        keep = [
            i
            for i, meta in enumerate(self.meta)
            if str(meta.get("source", "")).casefold() not in names
        ]
        if len(keep) == len(self.chunks):
            return
        index = faiss.IndexFlatIP(self.dim or 1)
        if keep:
            assert self.index is not None
            vectors = np.array([self.index.reconstruct(i) for i in keep], dtype="float32")
            index.add(vectors)
        self.index = index if keep else None
        self.chunks = [self.chunks[i] for i in keep]
        self.tags = [self.tags[i] for i in keep]
        self.meta = [self.meta[i] for i in keep]

    @staticmethod
    def _validate_snapshot(index, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid study index metadata")
        for key, item_type in (("chunks", str), ("tags", str), ("meta", dict)):
            values = payload.get(key)
            if not isinstance(values, list) or len(values) != index.ntotal:
                raise ValueError("Study index files are inconsistent")
            if not all(isinstance(value, item_type) for value in values):
                raise ValueError("Invalid study index metadata entries")
        if payload.get("dim") != index.d:
            raise ValueError("Study index dimensions are inconsistent")

    def _validate_snapshot_archive_limits(
        self, archive: zipfile.ZipFile, *, stored_bytes: int
    ) -> list[zipfile.ZipInfo]:
        if stored_bytes > self.max_snapshot_bytes:
            raise ValueError("Study snapshot exceeds configured size limit")
        members = archive.infolist()
        if len(members) > self.max_snapshot_members:
            raise ValueError("Study snapshot contains too many members")
        expanded_bytes = 0
        for member in members:
            if member.file_size < 0:
                raise ValueError("Invalid study snapshot archive")
            expanded_bytes += member.file_size
            if expanded_bytes > self.max_snapshot_bytes:
                raise ValueError("Study snapshot exceeds configured expansion limit")
        return members

    def clear(self, delete_files: bool = True):
        if delete_files:
            # Remove legacy files first so they cannot reappear after clearing
            # a migrated snapshot. Propagate failures instead of claiming success.
            self.faiss_path.unlink(missing_ok=True)
            self.chunks_path.unlink(missing_ok=True)
            self.snapshot_path.unlink(missing_ok=True)
        # clear memory
        self.index = None
        self.chunks = []
        self.tags = []
        self.meta = []
        self.dim = None
        self.documents = None
        self.pending = {}

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
def get_store_for_user(user_id: str, api_key: str | None = None) -> NoteStore:
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

        def get_flag_value(flag: str, default=None, command_parts=parts):
            if flag in command_parts:
                i = command_parts.index(flag)
                if i + 1 < len(command_parts):
                    return command_parts[i + 1]
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

            store.log_event(
                {
                    "type": "add_pdf",
                    "ts": now_iso(),
                    "tag": tag,
                    "file": filename,
                    "pages": len(pages),
                }
            )
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

        def adjusted_score(hit, current_stats=stats):
            cid, score, _chunk, meta = hit
            fb = current_stats.get(str(cid), {"good": 0, "bad": 0})
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
