# OT AI Study Engine — Audit and Upgrade Roadmap

## 17 September 2026 — atomic index snapshot checkpoint

Built on preserved local secure-session commit `6a865e9d21532065ff44410c82e528ff140137f5`, whose base remains remote `a352f6b10c963ba0de1050b6f086eee39c26532b` (fetched this run). No prior patch was reapplied and no remote was written.

API and CLI now share one persistence implementation: `study.snapshot` contains both FAISS vectors and JSON metadata, flushed before a single atomic replacement. Failed writes leave the previous snapshot available; uncommitted temporary files are ignored on restart. Metadata types, counts, and dimensions are checked before publishing or mutating memory. Partial legacy pairs and corrupt snapshots fail closed. Complete legacy pairs remain readable and are retained when the next save migrates to a snapshot; they are never used as a fallback after migration. API saves also check the store's owner directory.

Validation: 82 Python tests and 2 JavaScript startup tests, Ruff, mypy, JavaScript syntax, and whitespace checks pass. Regression tests inject serialization, metadata-write, fsync, and replace failures and cover restart, migration, malformed metadata, partial legacy state, and ownership. No live AI calls, deployment, or user data modifications were performed.

Limits: filesystem atomic replacement is assumed; this is not multi-process coordination or a power-loss/backup guarantee (directory fsync is not implemented). Interrupted temporary files may remain on disk. Corruption requires a verified backup; no automatic rollback to stale legacy data. Clearing notes is not a multi-file transaction. Document replacement/deletion consistency, usage limits, durable hosting, and recovery UX remain next. Retain the secure-session patch before this patch in all recovery handoffs.

## 16 September 2026 — session ownership checkpoint

The `codex/secure-sessions` batch builds on pushed retrieval commit `a352f6b10c963ba0de1050b6f086eee39c26532b`. It replaces client-selected buckets with server-issued anonymous cookies, hashed SQLite session records, absolute expiry, revocation, and same-origin write checks. Status, uploads, ingestion, chat, and flashcards require ownership. The frontend initializes its session before study requests. Validation: 67 Python tests, 2 JavaScript startup tests, Ruff, mypy, and JavaScript syntax checks pass. Tests use mocked AI; no live provider or deployment verification was performed.

This closes the arbitrary `X-User` ownership path but does not add accounts or cross-device recovery. Legacy uploads remain stored and must be re-uploaded; never auto-adopt a client-supplied old ID. Local HTTP requires `OT_SESSION_COOKIE_SECURE=false`; production retains secure cookies and must preserve the data directory, including `sessions.sqlite3`.

Next: transactional index snapshots/recovery, document replacement and deletion consistency, session-creation and AI usage limits, durable storage, then recoverable accounts. Existing single-process index locking remains a scaling constraint. Push, merge, and deployment remain with the owner in Codex.

The original audit below is historical; its unauthenticated-bucket findings describe the older baseline.

Audit date: 6 August 2026

Baseline commit: `949271a` on `main`

Implementation branch: `codex/ot-ai-next-level`

## Current system summary

The baseline was a single-process FastAPI application serving one 800-line HTML file. A browser-generated UUID selected a folder under `backend/users`. ZIP uploads were flattened into that folder, PDFs were extracted with pypdf, note chunks were embedded with OpenAI, and a FAISS index plus JSON metadata was written to local disk. Questions and topics were embedded for retrieval; selected chunks were sent to `gpt-4.1-mini` for answers or JSON-like flashcards.

The first-time journey was: create a browser-only username/password, enter an OpenAI key, upload a ZIP, click ingest, ask a question, or generate flashcards. The “account” existed only in browser `localStorage`; it provided no server authentication.

The repository had no README, `.env.example`, test suite, Render manifest, pinned runtime, root dependency entry point, CI, or durable-storage configuration. The live Render dashboard values are external state and cannot be recovered from Git.

## Baseline architecture and behavior

1. **User journey:** local-only login → key entry → ZIP upload → PDF ingestion → grounded Q&A/flashcards.
2. **Stack:** Python, FastAPI, Uvicorn, OpenAI SDK, pypdf, NumPy, FAISS, and vanilla HTML/CSS/JavaScript.
3. **AI:** `text-embedding-3-small` for document/query vectors; `gpt-4.1-mini` through `client.responses.create` for answers and flashcards.
4. **Prompts:** answer-only-from-notes instructions; optional concise exam mode; flashcard instruction requesting six JSON objects.
5. **Expected responses:** `response.output_text` for answers; a JSON array for flashcards, with a regex fallback when parsing failed.
6. **Timeout/retries:** no app-level configuration. The installed OpenAI SDK defaulted to a roughly ten-minute request timeout and two retries for several transient failures.
7. **Deployment:** no repository-owned Render configuration. The server only imported reliably when the process working directory or `--app-dir` happened to expose `backend/core.py` as `core`.
8. **Permanent vs temporary data:** local PDF/index files persist on a developer machine until removed; on default Render storage they are temporary and disappear on restart/deploy. Browser UUIDs, plaintext prototype passwords, and API keys persisted in local storage.
9. **Complete features:** same-origin UI serving, ZIP-to-PDF extraction, PDF text extraction, FAISS retrieval, grounded answer call, flashcard call, and health endpoint.
10. **Fragile/incomplete features:** browser-only accounts, BYOK propagation to embeddings, output parsing, duplicate ingestion, upload safety, error mapping, persistence, deployment reproduction, tests, and documentation.

## Major audit findings

### Critical

- **Uploaded user material remains in public Git history.** A later commit deleted it from the branch tip, but historical PDFs, extracted chunks, and FAISS indexes remain retrievable. No data is reproduced here.
- **Filesystem boundary used untrusted `X-User`.** Path traversal could redirect storage outside the intended user root.
- **The UI stored passwords and OpenAI API keys in plaintext `localStorage`.** The passwords did not protect backend data.
- **Uploads were unbounded.** The server read an entire ZIP into memory and accepted unlimited member sizes/counts, allowing memory exhaustion or ZIP bombs.
- **Untrusted model/source values were inserted through `innerHTML`.** This created a browser XSS route.

### High

- Request-scoped bearer keys were used for response generation but not reliably passed to embedding calls, so the advertised BYOK flow depended on the server key.
- Raw OpenAI exception strings were returned to clients, potentially exposing provider details.
- The SDK’s default timeout/retry policy was unsuitable for an interactive Render request.
- Flashcards used prompt-only JSON plus regex recovery and had no shape validation.
- Repeated ingestion duplicated chunks and increased retrieval noise and API cost.
- Render’s ephemeral filesystem made uploaded notes and indexes non-durable.
- There was no authentication/authorization, rate limit, abuse control, or durable job queue.
- The root package import failed, making the start command sensitive to an undocumented working directory.

### Medium

- Dependency and Python versions were unpinned.
- A single large HTML file mixed structure, styling, state, auth simulation, and API calls.
- Errors during PDF extraction were silently swallowed.
- There was no request correlation, security-header policy, readiness check, or configurable CORS allowlist.
- Ingestion had no page/text/chunk cap and embedded every chunk in one SDK call.
- There were no automated tests, lint rules, build checks, or dependency audit.
- The user could not copy an answer and flashcards were mouse-centric clickable containers.

### Low

- No favicon, social metadata, version, changelog, screenshots, demo-safe data, or contribution guidance.
- The optional CLI contains functionality not surfaced in the web app and should eventually be separated or retired.

## AI model assessment

| Option | Quality | Speed/cost posture | Migration impact | Recommendation |
|---|---|---|---|---|
| `gpt-4.1-mini` (current) | Strong non-reasoning instruction following for focused extraction and grounded study output | Fast, cost-conscious small model | None | Keep for Release 1 and establish evals first |
| GPT-5 mini | Newer model intended for well-defined tasks and potentially stronger synthesis | Small-model tier, but latency/cost must be measured for this workload | Prompt/eval and production cost comparison required | Candidate for a controlled later experiment |
| `gpt-4.1` | Higher capability than the mini variant for nuanced synthesis | Slower and more expensive | Model-only switch, but needs output/cost evaluation | Use only if labelled evals show a material gain |
| `text-embedding-3-small` (current) | Appropriate baseline retrieval quality | Lower embedding cost and index footprint | None | Keep |
| `text-embedding-3-large` | Potential retrieval improvement | Higher cost and storage/memory use | Full re-index required; dimensions/semantics differ | Test only against a retrieval benchmark |

No model was changed in Release 1. Structured Outputs, prompt-injection boundaries, bounded output tokens, batching, and validation improve reliability without changing product economics.

## Prioritised upgrade plan

| Issue or opportunity | Current impact | Recommended solution | Priority | Complexity | Deployment risk | Files/systems | Release 1? |
|---|---|---|---|---|---|---|---|
| User documents in Git history | Privacy exposure persists after deletion | Owner-approved history purge, force-push coordination, cache review | Critical | Large | High | GitHub history/clones | Partly: documented only |
| Path traversal via `X-User` | Cross-boundary filesystem access | Validate opaque IDs and enforce resolved containment | Critical | Small | Low | API/storage | Yes |
| Plaintext local passwords/key | Credential exposure and false security | Remove fake auth; preserve UUID; keep optional key in memory only | Critical | Medium | Medium | Frontend | Yes |
| Unbounded ZIP/PDF input | Denial of service | Stream cap and archive/member/PDF limits | Critical | Medium | Low | Upload API | Yes |
| DOM XSS | Script execution in user browser | Construct DOM with `textContent`; strict CSP | Critical | Medium | Low | Frontend/headers | Yes |
| Raw provider errors | Internal detail leakage and poor UX | Typed error mapping and stable messages | High | Small | Low | AI service/API | Yes |
| Long SDK defaults | Requests can hang through Render timeouts | 45-second timeout, one retry, client-side abort | High | Small | Low | AI service/frontend | Yes |
| Fragile flashcard parsing | Invalid UI payloads and 500s | Strict JSON Schema plus server validation | High | Medium | Low | AI service/tests | Yes |
| BYOK not passed to embeddings | Ingestion/retrieval failure without server key | Resolve once per request and inject into store/client | High | Small | Low | API/core | Yes |
| Duplicate ingestion | Cost and retrieval degradation | Hash documents and skip existing content | High | Medium | Low | Metadata/ingestion | Yes |
| Ephemeral Render data | Notes disappear | Approve disk or external persistence architecture | High | Large | High | Render/data | No |
| No authentication | Bucket IDs do not enforce ownership | Privacy design, secure auth, per-user authorization | High | Large | High | Product/data/API | No |
| Render config absent from Git | Reproduction and startup risk | Package-safe command, runtime pin, docs, optional later Blueprint | High | Small | Low | Deploy/docs | Yes |
| No tests | Regressions undetected | Mocked API/upload/security suite | High | Medium | None | Tests | Yes |
| No rate limiting | Abuse/cost exposure | Auth-aware server-side quotas and proxy-aware limits | High | Medium | Medium | API/infra | No |
| Monolithic frontend | Maintenance/accessibility issues | Separate semantic HTML, CSS, JS | Medium | Medium | Medium | Frontend | Yes |
| Single-process file locks | Multi-worker corruption risk | Keep one worker now; redesign persistence before scaling | Medium | Medium | Medium | Runtime/storage | Partly: documented |
| No OCR | Scanned PDFs yield no text | Add opt-in OCR with cost/resource limits | Medium | Large | Medium | Ingestion | No |
| No retrieval evals | Quality changes cannot be measured | Curated, non-private question/source benchmark | Medium | Medium | None | Tests/AI | No |
| No export | Study output is harder to reuse | Copy/download accessible formats | Medium | Small | Low | Frontend | Partly: copy answer |
| Portfolio metadata/docs | Prototype presentation | README, metadata, favicon, changelog, screenshots | Low | Small | None | Docs/frontend | Partly |

## Controlled release roadmap

### Release 1 — Production safety and stability

Implemented locally on the feature branch:

- safe user storage boundaries;
- bounded ZIP/PDF/text/chunk processing;
- document-hash deduplication;
- explicit OpenAI timeout/retry/output limits;
- typed provider error handling;
- strict flashcard schema and response validation;
- prompt-injection instructions and untrusted-note delimiters;
- embedding batching and correct request-scoped credential propagation;
- safe frontend DOM rendering and removal of credential persistence;
- same-origin default CORS policy and security headers;
- package-safe startup, readiness check, runtime/dependency pins;
- tests, lint configuration, dependency auditing, environment template, and docs.

Still requires owner action: purge historical user files after coordination; verify actual Render dashboard commands; run one restricted-key integration pass before merge/deploy.

### Release 2 — User experience and visual polish

- Add upload/index progress backed by an asynchronous job endpoint.
- Add answer and flashcard export (Markdown/CSV) and session reset/delete controls.
- Add source excerpt expansion and clearer tag management.
- Add screenshots, cross-browser checks, and automated accessibility scanning.
- Preserve the current dark visual identity and responsive workspace.

### Release 3 — Improved AI study functionality

- Build a labelled retrieval and grounded-answer evaluation set using non-private demo notes.
- Add source passage previews and relevance thresholds.
- Evaluate GPT-5 mini against `gpt-4.1-mini` for quality, latency, and cost before any switch.
- Add one carefully scoped quiz format with strict schema, adjustable difficulty, and regeneration.
- Add token/usage telemetry that never logs note text or keys.
- Evaluate overlap-aware chunking for long documents.

### Release 4 — Accounts and persistence

Proceed only after approving operational cost and privacy requirements:

- real authentication and authorization;
- durable object storage for PDFs;
- database records for sessions, materials, results, and deletion state;
- retention periods, export/deletion workflow, backups, and breach response;
- migration from anonymous bucket IDs;
- multi-instance-safe indexing/job architecture.

A Render disk is simpler but paid, binds storage to one service instance, disables horizontal scaling for that service, and changes zero-downtime characteristics. A managed database/object-store architecture costs more engineering effort but scales and governs data better.

### Release 5 — Portfolio and product readiness

- CI checks and protected-branch rules;
- tagged releases and screenshots using demo-safe material;
- architecture decision records and privacy policy;
- measurable quality/latency/cost results;
- portfolio case study, demo script, and known-limitations page;
- preview deployments for every pull request.

## Git history privacy remediation

The following is intentionally not performed automatically because it rewrites shared history:

1. Identify every historical user-upload path and confirm what must be removed without opening or redistributing the content.
2. Notify collaborators and pause pushes.
3. Create an offline backup and a fresh mirror clone.
4. Use `git filter-repo` path rules to remove the uploaded PDFs, extracted JSON, and indexes from every ref.
5. Run a credential-pattern scan and repository-size check on the rewritten mirror.
6. Force-push rewritten branches/tags only with explicit approval.
7. Ask collaborators to fresh-clone; delete stale forks/artifacts where possible.
8. Contact GitHub Support if cached sensitive views remain.
9. Review whether the exposed study documents require notification or replacement.

Current silent scans found no API-key-shaped value and no tracked `.env` filename. That does not make the historical document exposure safe and is not a substitute for a dedicated secret scanner.

## Remaining production risks

- The live Render dashboard configuration has not been read or changed.
- The live service may use a different Python runtime or command today; the next deploy will honor `.python-version` if no higher-precedence `PYTHON_VERSION` is set.
- Existing Render files are ephemeral and may already be lost/recreated between deployments.
- Existing client bucket IDs are preserved, but they are not authorization credentials.
- In-memory locks protect one process only; run one Uvicorn worker until persistence is redesigned.
- No real OpenAI request has been made on this branch.
- The pinned FastAPI/Starlette TestClient currently emits an upstream deprecation warning about future `httpx2`; tests still pass.
