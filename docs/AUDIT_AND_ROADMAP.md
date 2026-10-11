# OT AI Study Engine — Audit and Upgrade Roadmap

## 11 October 2026 — descriptor-pinned recovery archives

Backup verification and restore now reject symlinked or non-regular archive
paths and pin the inspected file by descriptor before parsing it. Replacement
between path inspection and open is detected by device/inode identity, while a
replacement after open cannot change the bytes streamed through that descriptor.

Regression coverage rejects both deterministic path replacement and symlinked
archives without publishing a restore destination. This protects the local
recovery boundary from cooperating unprivileged processes; it does not defend
against privileged storage tampering, encrypt archives, or authenticate their
origin.

## 10 October 2026 — private randomized recovery staging

Backup and restore operations now allocate unpredictable owner-private staging
paths in the destination filesystem instead of deriving them from the process
ID. A stale or deliberately pre-created PID-named file or directory therefore
cannot block later recovery work, and existing paths are never removed while a
new operation cleans up only the unique staging path it created.

Regression coverage preserves stale legacy staging paths while independently
creating and restoring a verified backup. Random staging prevents predictable
name collisions and local denial of service; it does not protect against a
privileged process, provide archive encryption, or replace off-host retention.

## 9 October 2026 — crash-durable backup and restore publication

Backup creation now flushes the completed private archive before verification
and exclusive publication, then flushes the destination directory metadata.
Restore flushes every verified staged file and directory from the leaves upward
before its atomic no-replace rename, then flushes the destination parent. A
pre-publication flush failure removes private staging and publishes nothing. If
the final parent-directory flush fails after publication, the completed output
is preserved and the command reports failure because durability is uncertain.

Regression coverage verifies the flush order, pre-publication cleanup, and
post-publication preservation behavior. POSIX filesystems must support file and
directory `fsync`; Windows flushes regular files but does not expose directory
handles through this implementation. This improves local crash recovery but is
not a substitute for encrypted off-host retention, a simultaneous multi-file
snapshot, or target-infrastructure restore drills.

## 8 October 2026 — exclusive restore publication

Restore now publishes its fully verified private staging directory with an
atomic no-replace rename. A destination created by another process after the
initial check is preserved, the restore fails, and its staging directory is
removed. Linux uses `renameat2(RENAME_NOREPLACE)`, macOS uses
`renamex_np(RENAME_EXCL)`, and Windows' exclusive rename behavior is retained.
Platforms without a supported atomic primitive fail closed.

Regression coverage creates a competing empty destination at the publication
boundary and verifies that it is neither overwritten nor removed. This closes
the restore-directory race but does not add archive encryption or off-host
retention. The later 9 October checkpoint adds local publication flushing.

## 7 October 2026 — pre-parser upload body limit

The upload endpoint now rejects a declared oversized multipart request before
FastAPI parses it and counts streamed request chunks when `Content-Length` is
absent. The raw-body ceiling is the ZIP limit plus a configurable 64 KiB
multipart-envelope allowance, while the existing ZIP-byte check remains the
authoritative file limit. Regression coverage proves both preflight rejection
and early termination of an oversized chunked body.

This bounds application-level multipart parsing and temporary spooling. It does
not replace an edge/proxy request-size limit: request bytes still reach the HTTP
server, and infrastructure should enforce a matching cap before application
workers for stronger flood and bandwidth protection.

## 6 October 2026 — verified, exclusive backup publication

Recovered October 5 checkout retained: the earlier fifteen changes are preserved
in recovery commit `a0571f1`, followed by `29d80ed` with pypdf 6.19.0 and an
isolated CLI-to-API restore regression. Do not apply the old series over it.

Backup creation now verifies its private temporary archive before publishing
with an exclusive hard link. A concurrent output file is never overwritten or
removed by failure cleanup. Regression tests cover competing publication and
verification failure. The output filesystem must support hard links; unsupported
filesystems fail without publication. This does not add power-loss durability
or change restore-directory publication, encryption, or off-host retention.

## 2 October 2026 — stable session state during backup collection

Backup creation now takes a second transactional SQLite snapshot after copying
all owner files and compares its validated authentication records with the
initial database snapshot. Session creation, revocation, expiry cleanup, or
credential remapping during owner collection therefore aborts without
publishing an archive instead of mixing authentication and study-data states.

Regression coverage creates a session deterministically while owner data is
being copied and verifies that the unpublished archive is removed. Usage
counters and concurrency leases may continue changing without invalidating a
backup because they do not change which owner data is recoverable. This is a
stable authentication/data boundary, not a simultaneous multi-file snapshot,
archive encryption, distributed locking, or multi-host coordination.

## 1 October 2026 — race-safe session database backup

Backup creation now records the regular SQLite database's device and inode
identity before opening it read-only, then checks that identity after open and
again after SQLite's transactional backup completes. A concurrent local path
replacement or symlink substitution therefore fails closed without publishing
an archive, while ordinary writes to the same database remain compatible with
SQLite's consistent backup snapshot.

Regression coverage deterministically replaces the database immediately after
SQLite opens it and separately rejects a symlinked database where the platform
permits symlink creation. This pins the database used by one backup operation;
it does not create a point-in-time transaction across SQLite and all owner
directories, defend against a privileged process, or encrypt the archive.

## 30 September 2026 — race-safe backup source copying

Backup creation now opens each study file by descriptor and verifies that the
opened regular file has the same device and inode identity as the path inspected
immediately beforehand. Owner, data, and legacy-upload directories are checked
again under the owner storage lock, and symlinked or non-directory storage paths
fail closed. This prevents a concurrent local process from substituting another
file or symlink between validation and copying into a recovery archive.

Regression coverage deterministically replaces a snapshot between inspection
and open, verifies that no archive is published, and rejects symlinked data and
legacy-upload directories where the platform permits symlink creation. This is
local backup hardening, not a multi-file point-in-time transaction, protection
against a privileged process, archive encryption, or multi-host coordination.

## 29 September 2026 — recoverable session-record validation

Backup creation, verification, and restore now validate every persisted session
record before accepting recovery data. Token hashes must retain the expected
SHA-256 representation, owners must use server-generated identifiers, expiry
values must be non-negative SQLite integers, and the expected sessions schema
must remain queryable. A checksum-valid but structurally unusable authentication
database therefore fails closed instead of being published as a verified backup.

Regression coverage exercises malformed token hashes and expiry types during
creation, a missing sessions table during verification and restore, and cleanup
of failed archive and restore outputs. This validates local recovery integrity;
it does not add accounts, cross-device recovery, archive encryption, or a
multi-file point-in-time transaction across live study storage.

## 28 September 2026 — backup owner consistency

Backup validation now requires every owner referenced by the copied session
database to be present in the archive owner list. This prevents a cross-file
snapshot race or incomplete source directory from producing a backup that
passes SQLite and checksum validation but restores an unusable authenticated
session with no corresponding study-data directory.

The check runs during backup creation, standalone verification, and restore.
Failed creation removes the unpublished archive. Orphaned owner directories
that are not referenced by an active session remain preservable for cautious
recovery; this change only rejects missing data for a recorded session.

## 27 September 2026 — deterministic complete-flow regression

Added an API-level regression that follows one anonymous study session through
ZIP upload, indexing, grounded answering, flashcard generation, case-insensitive
same-name replacement, replacement-aware retrieval, and final deletion. It
checks externally visible status at each state transition and inspects the
mocked provider prompt to prove superseded text is no longer retrieved.

The test uses synthetic PDF bytes, deterministic embeddings, and mocked model
responses, so it makes no live AI request and does not validate the Render
network, runtime configuration, persistent disk, or provider credentials. A
deployed preview still needs the corresponding manual end-to-end and isolated
restore drill before production promotion.

## 26 September 2026 — portable Unicode recovery paths

Backup creation and verification now compare paths using Unicode-normalized,
case-folded names. Legacy PDFs or archive members that differ only by Unicode
composition are rejected before publication or extraction, preventing one file
from replacing or hiding another when a backup moves between Linux, macOS, and
Windows filesystems with different normalization and case behavior.

This check improves archive portability but does not replace filename
sanitization in the upload path, archive encryption, or destination-specific
Windows ACL controls.

## 25 September 2026 — bounded backup expansion

Backup verification and restoration now reject archives whose declared expanded
contents exceed a configurable byte budget before any member is extracted. The
default is 10 GiB and the recovery CLI accepts `--max-uncompressed-bytes` for a
lower operational limit. Verification also caps archive member count and
manifest size, preventing a crafted recovery archive from consuming unbounded
disk, memory, or validation time. Failed restores still remove their private
staging directory and never publish a partial destination.

This is a local recovery safety bound, not archive encryption or an off-host
retention policy. Operators must choose a limit above the expected verified
backup size and continue to protect the archive itself.

## 24 September 2026 — private recovery file permissions

Backup creation and restoration now request owner-only permissions instead of
inheriting a potentially permissive process umask. POSIX backup archives and
restored files use mode `0600`; the restored data-directory root uses `0700`.
The restore remains isolated in that private staging root until atomic
publication. Regression coverage checks the published archive, restored root,
session database, PDFs, and index files.

These local permissions do not encrypt backup contents or replace off-host
access controls. Windows deployments must retain equivalent ACL restrictions,
and a future storage design still needs explicit encryption and key management.

## 23 September 2026 — single-pass verified restore

Hardened the portable recovery path so restore verifies and stream-extracts
from one open archive. The exact bytes checked are now the bytes published to
the new destination, removing the earlier verification-to-restore replacement
window and avoiding whole-file reads during restore. Archive paths and legacy
PDF names also reject case-insensitive collisions for Windows portability.

Regression coverage verifies that restore opens the archive only once and that
case-colliding members are rejected. This remains local-filesystem tooling, not
an encrypted off-host backup service or a multi-host consistency mechanism.

## 22 September 2026 — portable verified backup and recovery

Added a provider-neutral command to create, verify, and restore a complete local
data-directory backup. Creation uses SQLite's backup API and the existing
per-owner storage locks, writes a SHA-256 manifest, and validates the resulting
archive before publication. Verification rejects unsafe paths, unlisted files,
checksum mismatches, corrupt SQLite state, and invalid FAISS/metadata snapshots.

Restore is intentionally limited to a new destination and cannot merge with or
overwrite live data. This enables repeatable recovery drills before choosing a
paid disk or managed storage service, but it is not an automated backup schedule
or off-host retention system. Archives contain private PDFs and hashed session
credentials and therefore require encryption, access controls, and a deletion
policy. Multi-host consistent backup remains part of the future durable-storage
design.

## 21 September 2026 — renewable concurrency leases

Preserved the three local abuse-limit commits on the unchanged remote base
`260e40c`; GitHub still had no equivalent remote branch at the start of this
work. AI concurrency leases now run a daemon heartbeat that atomically extends
their expiry while the owning request remains alive. This prevents a legitimate
long ingestion from aging out and overlapping replacement capacity.

Normal completion and handled failures stop the heartbeat and release every
scope immediately. A terminated process cannot renew, so its slots still become
reclaimable after `AI_CONCURRENCY_LEASE_SECONDS`. A prolonged SQLite or storage
outage can prevent every renewal and eventually allow expiry; keep the window
large enough for the intended deployment and use shared durable accounting
before scaling to multiple hosts.

Regression coverage advances a controlled clock beyond the original deadline
and verifies that a live heartbeat still excludes another request. A spawned
worker is also terminated without cleanup and its capacity is verified to
recover after expiry. Final local validation passed 124 Python tests and 5
JavaScript tests, Ruff, mypy, JavaScript syntax, Python compilation, dependency
audit, and whitespace checks. Two upstream Starlette/httpx/AnyIO deprecation
warnings remain. Tests use no live AI provider or user data.

## 20 September 2026 — upload frequency and AI concurrency

Continued the preserved two-commit local series on exact remote base `260e40c`;
a fresh fetch still found no pushed equivalent. Added persistent, atomic hourly
ZIP upload budgets per owner and across the service. The reservation occurs
before application-level file reading, decompression, or staging, and failures
remain charged. Edge body-size and flood controls are still required because the
HTTP stack receives multipart data before route code runs.

AI embedding, retrieval, and generation now acquire per-owner and service-wide
SQLite leases shared by workers using the same data directory. Completion and
handled failures release slots; expiry recovers capacity after worker death.
Status, document deletion, and session revocation remain independent of these
leases. Configure the expiry above the longest accepted ingestion: an operation
that outlives it could overlap replacement capacity. The 21 September heartbeat
work above supersedes this fixed-expiry limitation.

Regression coverage includes spawned-process exclusion/release, expiry and
exception recovery, rejection before provider work, upload rejection before
application parsing, and control-route availability while AI capacity is full.
Tests use mocked providers and synthetic documents; no live AI request, GitHub
write, merge, or deployment is part of this work. Final local validation passed
123 Python tests and 5 JavaScript tests, Ruff, mypy, JavaScript syntax, Python
compilation, dependency audit, and whitespace checks. Two upstream
Starlette/httpx/AnyIO deprecation warnings remain.

Remaining abuse work: trusted-edge flood/per-IP controls and provider-side
monetary budgets. Durable multi-host accounting, document/session storage,
backup recovery, and end-to-end preview verification remain release work.

## 19 September 2026 — cumulative per-session quotas

Continued on the preserved local usage-budget commit `a260d64`, whose exact base
is remote `260e40c`. A fresh fetch confirmed that GitHub still has no equivalent
usage-limit branch, so no patch was reapplied and no remote state was changed.

Added configurable caps for distinct active/pending PDF names and total retained
indexed chunks per anonymous session. Checks execute under the existing owner
lock and before snapshot mutation; the chunk projection runs before embeddings.
Same-name or otherwise non-growing replacements remain possible when an older
library exceeds a newly lowered cap. Rejection preserves active notes and pending
uploads, while deletion releases both document-name and chunk capacity.

Regression coverage verifies atomic mixed replacement/new-document rejection,
lowered-cap repair, pre-provider chunk rejection, snapshot/pending preservation,
and capacity release after deletion. Tests use synthetic PDFs and embeddings;
no live provider requests, user data changes, deployment, or GitHub writes occur.
Final local Linux validation: 119 Python tests and 5 JavaScript tests passed;
Ruff, mypy, JavaScript syntax, Python compilation, dependency audit, and
whitespace checks passed. Two upstream Starlette/httpx/AnyIO deprecation warnings
remain. The expired virtual environment was recreated from the pinned development
requirements; `socksio` was installed only in that environment for its runtime
proxy and no dependency manifest changed.

Remaining abuse work: upload-frequency and in-flight AI concurrency limits,
trusted-edge fairness/flood controls, provider-side monetary budgets, and
multi-host durable accounting. Durable document/session storage and backup
recovery remain release blockers. See the accompanying handoff for final checks.

## 17 September 2026 — PDF replacement and deletion

Recovered the handoff in order on `codex/study-snapshots-20260917`: `618c7ea` is the secure-session patch equivalent to `6a865e9`; `03e8c82` is the atomic-snapshot patch equivalent to `64d7c0b`. Both sit above the unchanged retrieval base `a352f6b`. Earlier checkpoint sections below are historical.

The snapshot now commits active PDFs, pending uploads, vectors, and metadata together. Ingestion replaces all chunks for changed filenames, preserves other vectors, and commits only after every pending PDF has parsed and embedded successfully. Deletion removes active/pending bytes and metadata, including the empty-library case. Per-owner OS locks coordinate cooperating API workers; uploads perform blocking storage work outside the async event loop. Session cookies and same-origin checks protect the new deletion route. The UI exposes pending replacements and deletion, and handles ambiguous browser timeouts accurately.

Authenticated-session migration publishes a document manifest before retiring loose uploads and legacy index copies. Interrupted cleanup resumes before subsequent mutations. Browser-selected legacy owners remain inaccessible. See README for migration, backup, quota, and downgrade implications. Runtime mutation tests use temporary synthetic data and mocked embeddings, including a real PDF parser test and independent processes terminated before/after atomic publication.

Remaining limits: whole-snapshot memory/write costs, local-filesystem-only locks, optional CLI excluded from API concurrency guarantees, no directory fsync or power-loss guarantee, no automated backup recovery, no OCR or durable ingestion jobs. Existing in-flight answers may finish using their earlier snapshot. Recommended next task: session creation and AI usage quotas/rate limits before public use, followed by durable hosting and tested recovery. Nothing is merged or deployed by this work.

Final local Windows validation: 107 Python tests and 5 JavaScript tests passed, including the regression ensuring console errors after publication cannot report a failed deletion. Ruff, mypy, JavaScript syntax, Python compilation, and `git diff --check` passed. One upstream Starlette/httpx deprecation warning remains. Tests made no live AI requests. Handoff extraction files are retained locally and ignored; user PDFs, session stores, and secrets are excluded from this implementation commit.

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
## 18 September 2026 — persistent usage budgets

Fetched `origin/codex/study-snapshots-20260917` at `260e40c`; its secure-session
and snapshot commits are equivalents of the earlier local handoff. Preserved
the old local branch without reapplying either patch. Built this batch in a
separate local worktree on that fetched base.

Added atomic SQLite fixed-window budgets for new sessions, combined per-owner
AI route attempts, and service-wide daily AI route attempts. Rejection occurs
before document loading or provider work; failures are not refunded. Returning
sessions and read/delete operations remain available. HTTP 429 includes a clear
retry message and Retry-After; accounting errors fail closed. Counters survive
worker restarts on the same storage. No IP/proxy trust assumptions are added.

Regression coverage includes independent spawned workers, all-or-none budget
reservation, expiry, cookie/header bypass attempts, owner isolation, all three
AI routes, failure charging, and corrupted accounting. Tests use synthetic data
and no live AI requests. Final local Linux validation: 117 Python tests and 5
JavaScript tests passed; Ruff, mypy, JavaScript syntax, Python compilation, and
whitespace checks passed. Two upstream deprecation warnings remain. Recreated
the expired test environment; installed socksio only in that local environment
for the runtime proxy. No dependency manifest changes were needed.

Remaining: request counts are not dollar caps; fixed-window bursts, upload
frequency, cumulative document/chunk quotas, in-flight concurrency, trusted-edge
fairness and multi-host/durable accounting still need work. Next bounded batch:
cumulative document/chunk limits with atomic replacement-aware quota checks.
No pushes, GitHub writes, merges, or deployments were performed by this batch.
