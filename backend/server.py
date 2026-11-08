from __future__ import annotations

import hashlib
import io
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

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:
    from . import sessions, usage_limits
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
    from .private_storage import ensure_private_directory
    from .storage_lock import storage_lock
except ImportError:  # Supports `uvicorn server:app --app-dir backend`.
    import sessions  # type: ignore[no-redef]
    import usage_limits  # type: ignore[no-redef]
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
    from private_storage import ensure_private_directory  # type: ignore[no-redef]
    from storage_lock import storage_lock  # type: ignore[no-redef]


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
MAX_UPLOAD_REQUEST_OVERHEAD_BYTES = _env_int(
    "MAX_UPLOAD_REQUEST_OVERHEAD_BYTES", 64 * 1024, minimum=1024, maximum=1024 * 1024
)
MAX_JSON_REQUEST_BYTES = _env_int(
    "MAX_JSON_REQUEST_BYTES", 16 * 1024, minimum=1024, maximum=1024 * 1024
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
MAX_DOCUMENTS_PER_SESSION = _env_int(
    "MAX_DOCUMENTS_PER_SESSION", 50, minimum=1, maximum=500
)
MAX_PDF_PAGES = _env_int("MAX_PDF_PAGES", 300, minimum=1, maximum=2000)
MAX_PDF_TEXT_CHARS = _env_int("MAX_PDF_TEXT_CHARS", 2_000_000, minimum=1000, maximum=10_000_000)
MAX_INGEST_CHUNKS = _env_int("MAX_INGEST_CHUNKS", 2500, minimum=1, maximum=10_000)
MAX_INDEXED_CHUNKS_PER_SESSION = _env_int(
    "MAX_INDEXED_CHUNKS_PER_SESSION", 10_000, minimum=1, maximum=100_000
)
MAX_COMPRESSION_RATIO = 100
MAX_NEW_SESSIONS_PER_HOUR = _env_int(
    "MAX_NEW_SESSIONS_PER_HOUR", 100, minimum=1, maximum=100_000
)
MAX_AI_REQUESTS_PER_SESSION_HOUR = _env_int(
    "MAX_AI_REQUESTS_PER_SESSION_HOUR", 60, minimum=1, maximum=100_000
)
MAX_AI_REQUESTS_PER_DAY = _env_int(
    "MAX_AI_REQUESTS_PER_DAY", 500, minimum=1, maximum=1_000_000
)
MAX_UPLOADS_PER_SESSION_HOUR = _env_int(
    "MAX_UPLOADS_PER_SESSION_HOUR", 20, minimum=1, maximum=100_000
)
MAX_UPLOADS_PER_HOUR = _env_int(
    "MAX_UPLOADS_PER_HOUR", 200, minimum=1, maximum=1_000_000
)
MAX_CONCURRENT_UPLOADS_PER_SESSION = _env_int(
    "MAX_CONCURRENT_UPLOADS_PER_SESSION", 1, minimum=1, maximum=1000
)
MAX_CONCURRENT_UPLOADS = _env_int(
    "MAX_CONCURRENT_UPLOADS", 4, minimum=1, maximum=10_000
)
UPLOAD_CONCURRENCY_LEASE_SECONDS = _env_int(
    "UPLOAD_CONCURRENCY_LEASE_SECONDS", 600, minimum=60, maximum=86_400
)
MAX_CONCURRENT_AI_REQUESTS_PER_SESSION = _env_int(
    "MAX_CONCURRENT_AI_REQUESTS_PER_SESSION", 1, minimum=1, maximum=1000
)
MAX_CONCURRENT_AI_REQUESTS = _env_int(
    "MAX_CONCURRENT_AI_REQUESTS", 4, minimum=1, maximum=10_000
)
AI_CONCURRENCY_LEASE_SECONDS = _env_int(
    "AI_CONCURRENCY_LEASE_SECONDS", 3600, minimum=60, maximum=86_400
)
READ_CHUNK_BYTES = 1024 * 1024

SESSION_COOKIE_SECURE = os.getenv("OT_SESSION_COOKIE_SECURE", "true").lower()
if SESSION_COOKIE_SECURE not in {"true", "false"}:
    raise RuntimeError("OT_SESSION_COOKIE_SECURE must be true or false")
SESSION_COOKIE_NAME = "__Host-ot_session" if SESSION_COOKIE_SECURE == "true" else "ot_session_dev"
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

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question must not be blank")
        return value


class FlashcardRequest(StrictRequest):
    topic: str = Field(min_length=1, max_length=500)
    tag: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Topic must not be blank")
        return value


class _RequestBodyTooLarge(Exception):
    pass


def _upload_limit_detail() -> str:
    return f"ZIP exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit."


def _json_limit_detail() -> str:
    return "JSON request body exceeds the configured safety limit."


class RequestBodyLimitMiddleware:
    """Bound selected request bodies before Starlette parses or spools them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        path = scope.get("path")
        if path == "/upload_zip":
            limit = MAX_UPLOAD_BYTES + MAX_UPLOAD_REQUEST_OVERHEAD_BYTES
            detail = _upload_limit_detail()
        elif path in {"/chat", "/flashcards", "/ingest_user_pdfs"}:
            limit = MAX_JSON_REQUEST_BYTES
            detail = _json_limit_detail()
        else:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_length = int(value)
            except ValueError:
                break
            if declared_length > limit:
                response = JSONResponse(status_code=413, content={"detail": detail})
                await response(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            response = JSONResponse(status_code=413, content={"detail": detail})
            await response(scope, receive, send)


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
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

app.add_middleware(RequestBodyLimitMiddleware)

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
    if request.url.path.startswith("/documents") or request.url.path in {
        "/session",
        "/status",
        "/upload_zip",
        "/ingest_user_pdfs",
        "/chat",
        "/flashcards",
    }:
        response.headers["Cache-Control"] = "no-store"
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
        try:
            with storage_lock(user_paths(user_id)["user_dir"] / "storage.lock"):
                yield
        except TimeoutError as error:
            raise HTTPException(503, "Study storage is busy. Retry shortly.") from error


def get_user_id(request: Request) -> str:
    if "x-user" in request.headers:
        raise HTTPException(400, "X-User is no longer supported. Start a browser session.")
    session = sessions.lookup(USERS_DIR, request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        raise HTTPException(
            401, "Study session missing or expired. Refresh to start a new session."
        )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        sessions.require_same_origin_write(request)
    return session[0]


@app.post("/session")
def start_session(request: Request, response: Response):
    sessions.require_same_origin_write(request)
    if "x-user" in request.headers:
        raise HTTPException(400, "Legacy study identifiers cannot claim a session.")
    existing = sessions.lookup(USERS_DIR, request.cookies.get(SESSION_COOKIE_NAME))
    if existing:
        user_id, expires_at = existing
    else:
        usage_limits.consume(USERS_DIR, [("session:global:hour", MAX_NEW_SESSIONS_PER_HOUR, 3600)])
        token, user_id, expires_at = sessions.create(USERS_DIR)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=sessions.SESSION_SECONDS,
            httponly=True,
            secure=SESSION_COOKIE_SECURE == "true",
            samesite="strict",
            path="/",
        )
    return {"session_label": user_id[:10], "expires_at": expires_at}


@app.delete("/session")
def end_session(request: Request, response: Response, user_id: str = Depends(get_user_id)):
    sessions.revoke(USERS_DIR, request.cookies[SESSION_COOKIE_NAME])
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=SESSION_COOKIE_SECURE == "true",
        samesite="strict",
    )
    return {"ended": True}


def reserve_ai_request(user_id: str) -> None:
    usage_limits.consume(
        USERS_DIR,
        [
            ("ai:global:day", MAX_AI_REQUESTS_PER_DAY, 86400),
            (f"ai:owner:{user_id}:hour", MAX_AI_REQUESTS_PER_SESSION_HOUR, 3600),
        ],
    )


def reserve_upload(user_id: str) -> None:
    usage_limits.consume(
        USERS_DIR,
        [
            ("upload:global:hour", MAX_UPLOADS_PER_HOUR, 3600),
            (f"upload:owner:{user_id}:hour", MAX_UPLOADS_PER_SESSION_HOUR, 3600),
        ],
    )


@contextmanager
def upload_request_slot(user_id: str) -> Iterator[None]:
    try:
        with usage_limits.lease(
            USERS_DIR,
            [
                ("upload-concurrency:global", MAX_CONCURRENT_UPLOADS),
                (
                    f"upload-concurrency:owner:{user_id}",
                    MAX_CONCURRENT_UPLOADS_PER_SESSION,
                ),
            ],
            seconds=UPLOAD_CONCURRENCY_LEASE_SECONDS,
        ):
            yield
    except HTTPException as error:
        if error.status_code == 429:
            raise HTTPException(
                429,
                "Upload capacity is busy. Try again shortly.",
                headers=error.headers,
            ) from error
        raise


@contextmanager
def ai_request_slot(user_id: str) -> Iterator[None]:
    with usage_limits.lease(
        USERS_DIR,
        [
            ("ai-concurrency:global", MAX_CONCURRENT_AI_REQUESTS),
            (f"ai-concurrency:owner:{user_id}", MAX_CONCURRENT_AI_REQUESTS_PER_SESSION),
        ],
        seconds=AI_CONCURRENCY_LEASE_SECONDS,
    ):
        yield


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
    try:
        ensure_private_directory(USERS_DIR)
        users_root = USERS_DIR.resolve(strict=True)
        user_candidate = users_root / user_id
        user_candidate.relative_to(users_root)
        ensure_private_directory(user_candidate)
        user_dir = user_candidate.resolve(strict=True)
        user_dir.relative_to(users_root)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid user storage path.") from error

    data_dir = user_dir / "data"
    uploads_dir = user_dir / "uploads"
    try:
        ensure_private_directory(data_dir)
        ensure_private_directory(uploads_dir)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="Study storage preparation failed.") from error
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

    try:
        store.load()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile) as error:
        logger.error("study_index_load_failed user_id=%s", user_id)
        raise HTTPException(
            status_code=500,
            detail="This study index could not be loaded. Restore a verified backup before retrying.",
        ) from error
    return store


def save_store_for_user(user_id: str, store: NoteStore) -> None:
    if store.base_path.resolve() != user_paths(user_id)["data_dir"].resolve():
        raise ValueError("Study store does not belong to this session")
    store.save()


def document_store(user_id: str, api_key: str | None = None) -> NoteStore:
    """Call under user_lock. Migrate once, then retire legacy copies before edits."""
    try:
        return _document_store(user_id, api_key)
    except OSError as error:
        logger.error("document_migration_failed user_id=%s", user_id)
        raise HTTPException(
            500, "Study storage preparation failed. Retry before changing documents."
        ) from error


def _document_store(user_id: str, api_key: str | None = None) -> NoteStore:
    store = load_store_for_user(user_id, api_key)
    paths = user_paths(user_id)
    # Only API snapshot temporaries; the owner lock rules out a live API writer.
    for temporary in paths["data_dir"].glob("snapshot-*.tmp"):
        temporary.unlink()
    legacy_pdfs = sorted(paths["uploads_dir"].glob("*.pdf"))
    if store.documents is None:
        store.documents = {}
        seen = set()
        for pdf in legacy_pdfs:
            name = safe_pdf_name(pdf.name).casefold()
            if name in seen:
                raise HTTPException(
                    409, "Legacy PDF names conflict. Resolve them before migration."
                )
            seen.add(name)
            content = pdf.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            matching = [m for m in store.meta if str(m.get("source", "")).casefold() == name]
            if matching and all(m.get("document_sha256") == digest for m in matching):
                store.documents[name] = content
            else:
                store.pending[name] = content
        # The manifest is the authority after this atomic commit, even if cleanup
        # is interrupted. A later request resumes cleanup, never reimports files.
        save_store_for_user(user_id, store)
    for path in [*legacy_pdfs, store.faiss_path, store.chunks_path]:
        path.unlink(missing_ok=True)
    return store


def commit_documents(user_id: str, store: NoteStore) -> None:
    try:
        save_store_for_user(user_id, store)
    except (OSError, ValueError, RuntimeError) as error:
        logger.error("document_save_failed user_id=%s", user_id)
        raise HTTPException(
            500, "Changes could not be saved. Previous study data is intact."
        ) from error


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
                detail=_upload_limit_detail(),
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
                raise HTTPException(
                    status_code=400, detail="Invalid or corrupt ZIP file."
                ) from error
            if len(pdf_bytes) > MAX_PDF_BYTES:
                raise HTTPException(
                    status_code=413, detail="A PDF exceeds the per-file size limit."
                )
            if b"%PDF-" not in pdf_bytes[:1024]:
                raise HTTPException(status_code=400, detail=f"{filename} is not a valid PDF file.")
            extracted.append((filename, pdf_bytes))

    return extracted


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
    try:
        ensure_private_directory(USERS_DIR)
    except (OSError, ValueError):
        return JSONResponse(status_code=503, content={"ready": False})
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
def status(user_id: str = Depends(get_user_id)):
    store = load_store_for_user(user_id)
    sources = sorted({str(item.get("source", "unknown")) for item in store.meta})
    indexed = {name.casefold() for name in sources if name.lower().endswith(".pdf")}
    names = indexed | set(store.documents or {}) | set(store.pending)
    return {
        "chunks": len(store.chunks),
        "documents": len(sources),
        "tags": sorted(set(store.tags)) if store.tags else [],
        "ready": bool(store.chunks),
        "document_files": [
            {"name": name, "indexed": name in indexed, "pending": name in store.pending}
            for name in sorted(names)
        ],
    }


@app.post("/upload_zip")
async def upload_zip(file: UploadFile = File(...), user_id: str = Depends(get_user_id)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a ZIP file.")

    reserve_upload(user_id)
    with upload_request_slot(user_id):
        try:
            content = await read_upload_limited(file)
        finally:
            await file.close()
        pdfs = extract_valid_pdfs(content)
        await run_in_threadpool(stage_pdfs, user_id, pdfs)
    return {"uploaded_pdfs": len(pdfs), "filenames": [name for name, _ in pdfs]}


def stage_pdfs(user_id: str, pdfs: list[tuple[str, bytes]]) -> None:
    with user_lock(user_id):
        store = document_store(user_id)
        assert store.documents is not None
        current_names = set(store.documents) | set(store.pending)
        pending = {**store.pending, **{name.casefold(): content for name, content in pdfs}}
        projected_names = set(store.documents) | set(pending)
        if (
            len(projected_names) > MAX_DOCUMENTS_PER_SESSION
            and len(projected_names) > len(current_names)
        ):
            raise HTTPException(
                413, "This study session has reached its document-count limit."
            )
        # Count active AND staged bytes; rollback needs both versions on disk.
        projected_size = sum(map(len, store.documents.values())) + sum(map(len, pending.values()))
        if projected_size > MAX_USER_STORAGE_BYTES:
            raise HTTPException(413, "This study session has reached its document storage limit.")
        store.pending = pending
        commit_documents(user_id, store)


@app.delete("/documents/{filename}")
def delete_document(filename: str, user_id: str = Depends(get_user_id)):
    if safe_pdf_name(filename) != filename:
        raise HTTPException(400, "Invalid PDF filename.")
    name = filename.casefold()
    with user_lock(user_id):
        store = document_store(user_id)
        assert store.documents is not None
        exists = (
            name in store.documents
            or name in store.pending
            or any(str(meta.get("source", "")).casefold() == name for meta in store.meta)
        )
        if not exists:
            raise HTTPException(404, "PDF not found in this study session.")
        store.remove_documents({name})
        store.documents.pop(name, None)
        store.pending.pop(name, None)
        commit_documents(user_id, store)
    return {"deleted": filename}


@app.post("/ingest_user_pdfs")
def ingest_user_pdfs(
    payload: IngestRequest | None = None,
    authorization: str | None = Header(default=None),
    user_id: str = Depends(get_user_id),
):
    requested_tag = validate_tag(payload.tag if payload else None)
    api_key = resolve_api_key(authorization)
    reserve_ai_request(user_id)
    with user_lock(user_id):
        store = document_store(user_id, api_key)
        assert store.documents is not None
        all_chunks: list[str] = []
        changed = set()
        duplicate_files = 0
        batches = []
        # Parse the whole batch before altering any candidate vectors. Any bad
        # PDF fails the batch; pending uploads remain available for retry/delete.
        for name, content in store.pending.items():
            digest = hashlib.sha256(content).hexdigest()
            matching = [m for m in store.meta if str(m.get("source", "")).casefold() == name]
            if (
                store.documents.get(name) == content
                and matching
                and all(m.get("document_sha256") == digest for m in matching)
            ):
                duplicate_files += 1
                continue
            try:
                pages = read_pdf_pages(
                    io.BytesIO(content), max_pages=MAX_PDF_PAGES, max_chars=MAX_PDF_TEXT_CHARS
                )
                chunks = []
                metadata = []
                for page, text in pages:
                    for chunk in chunk_text(text):
                        chunks.append(chunk)
                        metadata.append(
                            {
                                "source": name,
                                "page": page,
                                "is_gold": False,
                                "document_sha256": digest,
                            }
                        )
                if not chunks:
                    raise ValueError("No extractable text")
            except Exception as error:
                raise HTTPException(
                    422, f"Could not read {name}. Previous study data is intact."
                ) from error
            all_chunks.extend(chunks)
            if len(all_chunks) > MAX_INGEST_CHUNKS:
                raise HTTPException(
                    413, f"Ingestion exceeds the {MAX_INGEST_CHUNKS}-chunk safety limit."
                )
            changed.add(name)
            batches.append((chunks, requested_tag or infer_tag_from_filename(name), metadata))
        retained_chunks = sum(
            str(metadata.get("source", "")).casefold() not in changed
            for metadata in store.meta
        )
        projected_chunks = retained_chunks + len(all_chunks)
        if (
            projected_chunks > MAX_INDEXED_CHUNKS_PER_SESSION
            and projected_chunks > len(store.chunks)
        ):
            raise HTTPException(
                413, "This study session has reached its indexed-chunk limit."
            )
        store.remove_documents(changed)
        try:
            if batches:
                with ai_request_slot(user_id):
                    for chunks, tag, metadata in batches:
                        store.add_chunks(chunks, tag=tag, meta_list=metadata)
        except Exception as error:
            if isinstance(error, HTTPException):
                raise
            raise_openai_http_error(error)
        if store.pending:
            store.documents.update(store.pending)
            store.pending = {}
            commit_documents(user_id, store)
    return {
        "added_chunks": len(all_chunks),
        "files": len(changed),
        "already_ingested": duplicate_files,
        "skipped_files": 0,
        "message": "PDFs ingested successfully." if all_chunks else "No new PDFs to ingest.",
    }


@app.post("/chat")
def chat_api(
    payload: ChatRequest,
    authorization: str | None = Header(default=None),
    user_id: str = Depends(get_user_id),
):
    tag = validate_tag(payload.tag)
    api_key = resolve_api_key(authorization)
    reserve_ai_request(user_id)
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
        with ai_request_slot(user_id):
            hits = select_hits(store, query, k=8 if is_summary else 4, tag=tag)
            if not hits:
                return {"status": "not_found", "answer": "Not found in your notes.", "sources": []}
            context = "\n\n---\n\n".join(hit[2] for hit in hits)
            answer = generate_answer(
                create_openai_client(api_key),
                context=context,
                question=payload.question,
                mode=payload.mode,
            )
    except Exception as error:
        if isinstance(error, HTTPException):
            raise
        raise_openai_http_error(error)
    if answer.strip().casefold() == "not found in your notes.":
        return {"status": "not_found", "answer": answer, "sources": []}
    return {"status": "answered", "answer": answer, "sources": build_sources(hits)}


@app.post("/flashcards")
def flashcards_api(
    payload: FlashcardRequest,
    authorization: str | None = Header(default=None),
    user_id: str = Depends(get_user_id),
):
    tag = validate_tag(payload.tag)
    api_key = resolve_api_key(authorization)
    reserve_ai_request(user_id)
    store = load_store_for_user(user_id, api_key=api_key)

    if not store.chunks:
        return {"flashcards": [], "message": "Upload and ingest PDFs first."}

    topic = payload.topic
    query = topic
    if tag is None and re.fullmatch(r"week\s*\d{1,2}", topic, flags=re.IGNORECASE):
        tag = validate_tag(re.sub(r"\s+", "", topic.lower()))
        query = "main concepts definitions examples key points overview"
    try:
        with ai_request_slot(user_id):
            hits = select_hits(store, query, k=8, tag=tag)
            if not hits:
                return {"flashcards": [], "message": "No matching notes were found."}
            context = "\n\n---\n\n".join(hit[2] for hit in hits)
            cards = generate_flashcards(
                create_openai_client(api_key),
                context=context,
                topic=topic,
            )
    except Exception as error:
        if isinstance(error, HTTPException):
            raise
        raise_openai_http_error(error)
    return {"flashcards": cards, "sources": build_sources(hits)}
