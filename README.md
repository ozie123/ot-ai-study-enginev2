# OT AI Study Engine

OT AI Study Engine is a source-grounded study workspace. A learner uploads a ZIP containing PDF notes, the backend extracts and embeds the text into a per-session FAISS index, and the interface generates cited answers and revision flashcards from the retrieved material.

The application is intentionally a compact FastAPI service with a framework-free frontend. Release 1 focuses on production safety, reliability, accessible user feedback, and test coverage without changing the AI provider or requiring an external authentication vendor. Anonymous session ownership is stored in a local SQLite database.

## Features

- ZIP upload with file-count, compressed-size, expanded-size, PDF-signature, and filename checks.
- PDF text extraction and chunked OpenAI embeddings.
- FAISS similarity search with optional topic/week tags.
- Source-grounded Q&A in detailed or exam mode.
- Six-card structured flashcard generation.
- Page-level source labels for generated material.
- Server-issued anonymous sessions with private, expiring cookies and server-side ownership.
- Clear loading, success, empty, timeout, and failure states.
- Keyboard-accessible, responsive interface with no untrusted HTML injection.
- Health and readiness endpoints for deployment checks.

AI-generated material can be wrong or incomplete. Important answers should always be verified against the cited PDF pages.

## Architecture

```mermaid
flowchart LR
    Browser["Browser UI"] -->|"ZIP and session cookie"| API["FastAPI web service"]
    API --> Uploads["Validated per-session PDFs"]
    API --> Extract["pypdf extraction and chunking"]
    Extract --> Embed["OpenAI text-embedding-3-small"]
    Embed --> Index["FAISS index and chunk metadata"]
    Browser -->|"Question or topic"| API
    API --> Index
    Index --> Context["Retrieved note context"]
    Context --> Response["OpenAI gpt-4.1-mini Responses API"]
    Response -->|"Answer, cards, sources"| Browser
```

The API key is resolved for each AI request. A server-managed `OPENAI_API_KEY` is preferred for deployment; an optional bearer key can be entered for a single browser-tab development session. Browser-entered keys are never persisted by the application.

## Technology stack

| Layer | Technology |
|---|---|
| Web API | Python 3.12, FastAPI, Uvicorn |
| AI | OpenAI Python SDK, Responses API, Embeddings API |
| Retrieval | NumPy and FAISS cosine similarity |
| Documents | pypdf and Python ZIP handling |
| Frontend | Semantic HTML, CSS, and vanilla JavaScript |
| Tests | pytest, FastAPI TestClient, mocked OpenAI SDK responses |
| Hosting | Render web service |

## Repository structure

```text
backend/
  ai_service.py       OpenAI client configuration, prompts, output contracts, error mapping
  core.py             PDF chunking, embeddings, FAISS store, and optional legacy CLI
  server.py           FastAPI routes, validation, upload safety, persistence boundaries
  requirements.txt    Pinned production dependencies
frontend/
  index.html          Semantic application structure
  styles.css          Responsive visual system
  app.js              API client and safe DOM rendering
tests/                API, upload, AI-mock, and frontend safety tests
docs/
  AUDIT_AND_ROADMAP.md Professional audit and controlled release roadmap
.env.example          Placeholder configuration only
.python-version       Tested Render/Python runtime
requirements.txt      Root production install entry point
requirements-dev.txt  Local test, lint, and audit tools
```

## Local setup

Requirements: Python 3.12 and Git. No API key is needed for the mocked test suite.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

### macOS or Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

For localhost HTTP, set `OT_SESSION_COOKIE_SECURE=false` in your copied `.env`. Production HTTPS must keep it `true`.

Leave `OPENAI_API_KEY=` empty to run the interface and mocked tests without live AI. When a separate development key is available, set it only in the ignored local `.env` file:

```dotenv
OPENAI_API_KEY=
```

Do not commit `.env`, a browser-entered key, or a Render secret.

## Run the application

From the repository root:

```bash
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`. The app shell, upload validation, status endpoint, and missing-key behavior work without an API key. Live ingestion, search, answers, and flashcards require an OpenAI key because both indexing and retrieval create embeddings.

The optional CLI remains available:

```bash
python -m backend.core
```

## Environment variables

| Variable | Required | Default | Purpose |
|---|---:|---|---|
| `OPENAI_API_KEY` | For live server-managed AI | none | OpenAI credential; configure as a secret |
| `OPENAI_RESPONSE_MODEL` | No | `gpt-4.1-mini` | Answer and flashcard model |
| `OPENAI_EMBEDDING_MODEL` | No | `text-embedding-3-small` | Index and query embedding model |
| `OPENAI_TIMEOUT_SECONDS` | No | `45` | SDK request timeout |
| `OPENAI_MAX_RETRIES` | No | `1` | SDK retry count for transient failures |
| `CORS_ORIGINS` | No | empty | Non-credentialed public reads only; study sessions are same-origin |
| `OT_DATA_DIR` | No | `backend/users` | Runtime storage root |
| `MAX_UPLOAD_BYTES` | No | `26214400` | Maximum ZIP request size |
| `MAX_ZIP_MEMBERS` | No | `50` | Maximum files in one ZIP |
| `MAX_PDF_BYTES` | No | `12582912` | Maximum expanded size per PDF |
| `MAX_TOTAL_UNCOMPRESSED_BYTES` | No | `52428800` | Maximum expanded PDFs per ZIP |
| `MAX_USER_STORAGE_BYTES` | No | `104857600` | Maximum stored PDFs per session |
| `MAX_PDF_PAGES` | No | `300` | Maximum pages per ingested PDF |
| `MAX_PDF_TEXT_CHARS` | No | `2000000` | Maximum extracted characters per PDF |
| `MAX_INGEST_CHUNKS` | No | `2500` | Maximum new chunks in one ingestion |
| `LOG_LEVEL` | No | `INFO` | Application log level |

Changing the embedding model requires rebuilding every FAISS index because vector dimensions and semantics can differ.

## How the AI integration works

1. **Ingestion:** `POST /ingest_user_pdfs` extracts PDF text into approximately 900-character chunks. Chunks are embedded in batches of 64 with `text-embedding-3-small` and normalized into a FAISS inner-product index.
2. **Retrieval:** Q&A and flashcard requests embed the question/topic and retrieve the closest note chunks, optionally filtered by tag.
3. **Answers:** `gpt-4.1-mini` receives a grounding prompt, untrusted note context, and the learner’s question through the Responses API. Output is plain text plus server-derived source labels.
4. **Flashcards:** the same model receives a strict JSON Schema requiring exactly six question/answer objects. The server validates the decoded structure again before returning it.
5. **Failures:** authentication, permissions, rate limits, timeouts, connection failures, provider errors, and malformed outputs map to stable messages without returning provider payloads.

The SDK is configured for a 45-second timeout and one retry. Output-token limits are 900 for answers and 1,600 for flashcards.

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Application UI |
| `GET` | `/health` | Lightweight liveness check |
| `GET` | `/ready` | Frontend and storage readiness check |
| `GET` | `/config` | Non-secret public client configuration |
| `POST` | `/session` | Start or resume a server-issued anonymous session |
| `DELETE` | `/session` | Revoke the current session; keep stored files |
| `GET` | `/status` | Current study-index counts and tags |
| `POST` | `/upload_zip` | Validate and store a ZIP of PDFs |
| `POST` | `/ingest_user_pdfs` | Extract and embed new PDFs |
| `POST` | `/chat` | Generate a grounded answer |
| `POST` | `/flashcards` | Generate six structured flashcards |

All study endpoints require a valid server-issued cookie. `X-User` is rejected; neither a legacy bucket ID nor an OpenAI API key proves ownership. Start a session with `POST /session` and `X-Study-Request: 1`, retain the response cookie, and include that header on every state-changing request. `DELETE /session` revokes access without deleting uploaded files. Cross-origin writes are rejected.

Sessions use random 256-bit tokens, stored only as SHA-256 hashes in `OT_DATA_DIR/sessions.sqlite3`. The database maps each token to a separately generated study bucket. Cookies are HttpOnly, SameSite=Strict, and Secure by default, with the `__Host-` prefix and no Domain attribute. Absolute expiry is 30 days; reloading does not extend it. Private responses use `Cache-Control: no-store`. Session records survive restarts only when the data directory survives.

For **local HTTP development only**, set `OT_SESSION_COOKIE_SECURE=false` in `.env`; this uses a distinct development cookie name. Keep the default `true` on HTTPS deployments. Behind a reverse proxy, configure trusted proxy handling so the application sees the correct public scheme/host; do not weaken origin checks. Serve the study frontend from the API origin. `CORS_ORIGINS` does not enable credentialed cross-origin study access.

**Migration:** old uploads remain on disk but are not claimed using browser-local IDs. Re-upload notes to a new session; any future migration must independently verify ownership. Do not expose an endpoint that adopts a supplied legacy ID. Clearing cookies, expiry, or revocation loses browser access; these are anonymous sessions, not recoverable accounts. Shared-browser users share the cookie. Persist and protect the session database together with the note files.

## Tests and checks

```bash
python -m pytest -q
node --test tests/frontend/*.test.mjs
python -m ruff check backend tests
python -m mypy backend
node --check frontend/app.js
python -m pip_audit -r backend/requirements.txt
python -m compileall -q backend
```

The test suite does not make live OpenAI requests. It mocks successful Responses API output, structured flashcards, embedding batches, malformed output, authentication failure, rate limiting, timeouts, connection failure, and provider unavailability.

## Render deployment

The current Render dashboard configuration is not stored in this repository, so its live build/start values cannot be proven from Git alone. The repository-compatible values are:

- Runtime: Python 3 (`.python-version` pins `3.12.13`).
- Build command: `pip install -r requirements.txt`.
- Start command: `uvicorn backend.server:app --host 0.0.0.0 --port $PORT`.
- Health check path: `/health`.
- Required secret for live AI: `OPENAI_API_KEY`.
- Root directory: repository root.

No frontend build step, database migration, or static-site service is required. FastAPI serves the frontend itself.

Before changing the live service, compare these values with **Render Dashboard → Settings**, use a preview service or manual branch deploy, verify `/health`, `/ready`, upload, mocked failure states, and a restricted development-key integration test, then promote only after approval.

### Storage on Render

Render’s default web-service filesystem is ephemeral. PDFs, FAISS indexes, and chunk metadata under `backend/users` can disappear on a restart or deployment and are not safe durable storage. They are also local to one process, so horizontal scaling is unsupported.

A later persistence release can use either:

- a paid Render persistent disk with `OT_DATA_DIR` set to its mount path; or
- object storage plus a database/vector store after cost, privacy, deletion, and migration design is approved.

Do not add a disk or database to the live service without a migration and retention plan.

## Security and privacy

- Uploaded documents are treated as untrusted content and cannot override the system prompt.
- Archive and PDF limits reduce ZIP-bomb and memory-exhaustion risk.
- User IDs are constrained to safe opaque identifiers and checked against the storage root.
- AI/provider error bodies are not returned to users.
- API keys are never rendered by `/config`, logged, or saved by the frontend.
- Untrusted sources, answers, and flashcards are inserted with `textContent`, not HTML.
- Same-origin requests are the default; cross-origin access is opt-in.
- Security headers deny framing, external scripts, and unnecessary browser permissions.

Important limitation: cookie-based anonymous ownership is enforced, but there are no user accounts, recovery, or cross-device identity. Anyone with the session cookie can act as that session. Public-launch blockers still include abuse controls, durable storage, and transactional index recovery.

The public Git history previously included uploaded study documents even though they were later deleted from the current branch. See [the audit](docs/AUDIT_AND_ROADMAP.md) for the required history-cleanup procedure. Do not add new user data to Git.

## Troubleshooting

### `ModuleNotFoundError: core`

Run the package-safe command from the repository root:

```bash
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000
```

### “AI service is not configured”

This is expected when `OPENAI_API_KEY` is empty and no temporary browser-session key is supplied. The UI and mocked tests still work.

### Upload accepted but no text is indexed

The PDF may be image-only, encrypted, malformed, or beyond a safety limit. OCR is not implemented. Check safe application logs for the filename-only extraction warning.

### Notes disappear on Render

This is expected on an ephemeral filesystem after service restart or deployment. Persistence requires a separately approved disk or datastore plan.

### Existing index will not load

FAISS and `chunks.json` must have matching entry counts and must have been produced with the configured embedding model. Re-upload and re-ingest the source PDFs.

## Known limitations

- No user accounts, password reset, session recovery, or secure cross-device identity.
- Runtime files are ephemeral on a default Render web service.
- No OCR for scanned/image-only PDFs.
- No durable job queue; large ingestion requests occupy one worker.
- In-memory locking does not coordinate multiple server processes.
- No citation spans or highlighted source passages yet.
- No live OpenAI integration test is run in CI.
- Retrieval quality has no labelled evaluation dataset yet.

## Roadmap

1. Production safety and stability — implemented on `codex/ot-ai-next-level`; history purge remains an owner-approved operation.
2. UX and visual polish — responsive study workspace, export options, and stronger progress feedback.
3. AI study quality — retrieval evaluation, source excerpts, difficulty controls, and carefully selected quiz modes.
4. Accounts and persistence — only after an approved privacy, cost, retention, and migration design.
5. Portfolio readiness — screenshots, demo-safe sample material, automated CI, release tags, and product metrics.

See [Audit and roadmap](docs/AUDIT_AND_ROADMAP.md) for priorities and deployment risk.
