# Changelog

## PDF parser security update — Unreleased

- Upgrade pypdf from 6.16.1 to 6.19.0 to include the published parser resource-exhaustion fixes.
- Exercise backup creation, verification, and restoration through the CLI, then confirm restored API access and deletion isolation with a synthetic session and mocked AI.

## Backup owner consistency — Unreleased

- Reject backups whose session database references an owner missing from the
  archive, preventing successful verification of an unusable restored session.
- Apply the consistency check during creation, standalone verification, and
  restoration; failed creation removes the incomplete archive.

## End-to-end study flow verification — Unreleased

- Add one deterministic API-level regression covering session startup, upload,
  indexing, grounded answers, flashcards, same-name replacement, and deletion.
- Verify replacement context excludes superseded text and final deletion leaves
  no indexed chunks, documents, tags, or library entries.
- Keep the flow offline with synthetic PDF bytes, deterministic embeddings, and
  mocked Responses API output; no live AI request or deployment is required.

## Portable backup and recovery — Unreleased

- Restrict live POSIX session and per-owner study storage to owner-only modes,
  and reject symlinked session databases or owner directories before use.
- Flush completed backup archives before exclusive publication and flush
  verified restore trees before atomic publication, including parent-directory
  metadata on POSIX filesystems.
- Add a provider-neutral command that creates a complete data-directory archive
  using SQLite's backup API and per-owner storage locks.
- Verify and stream-extract restore data from one open archive, eliminating a
  verification-to-restore replacement window and avoiding whole-file reads.
- Reject case-insensitive archive and legacy-PDF path collisions so backups are
  portable to Windows filesystems.
- Reject Unicode-normalization collisions so recovery paths remain distinct
  across Linux, macOS, and Windows filesystems.
- Create backup archives and restored private files with owner-only permissions;
  restored data-directory roots are accessible only to their owner.
- Bound backup verification and restore expansion by total bytes, member count,
  and manifest size before extraction.
- Verify SHA-256 checksums, safe paths, the session database, and each stored
  FAISS/metadata snapshot before accepting an archive.
- Restore only into a new directory so recovery drills cannot merge with or
  overwrite live study data.
- Document archive sensitivity, off-host retention, and recovery-test steps.

## Abuse and cumulative resource limits — Unreleased

- Pin recovery archives to verified regular-file descriptors and reject
  symlink or path-replacement substitution before verification and restore.
- Use securely randomized private staging paths for backup creation and restore,
  preventing stale or pre-created process-ID paths from blocking recovery.

- Persist atomic fixed-window budgets for new sessions, per-owner AI attempts, and service-wide AI attempts in the session SQLite database.
- Return HTTP 429 with `Retry-After` before paid provider work; fail closed when usage accounting is unavailable.
- Cap distinct active/pending PDF names and total retained indexed chunks per session, in addition to existing byte and per-ingestion limits.
- Make cumulative checks replacement-aware: same-name/non-growing repairs remain possible, rejected mutations preserve the snapshot, and deletion releases capacity.
- Add atomic per-owner and service-wide ZIP upload-frequency budgets before application parsing or decompression.
- Bound simultaneous AI work with expiring per-owner and service-wide SQLite leases shared by local workers; keep status, deletion, and session revocation available.
- Renew live AI leases in the background so long healthy operations retain capacity; preserve expiry-based recovery when a worker dies or accounting remains unavailable.
- Add multi-process request-budget and concurrency-lease tests plus document/chunk quota, failure-preservation, control-route availability, and pre-provider rejection regressions.

## Transactional PDF replacement and deletion — Unreleased

- Flush the study snapshot's containing directory after atomic replacement on
  POSIX, preserving a completed publication while reporting uncertain
  durability if the final directory flush fails.
- Include active and pending PDF bytes in the atomic index snapshot. Stage uploads until the entire ingestion batch succeeds; retain working files and vectors on parse, embedding, or save failures.
- Replace all chunks for a canonical filename without rebuilding unrelated embeddings. Persist deletion of the last document as an empty snapshot.
- Add session-scoped `DELETE /documents/{filename}` and library controls with pending replacement status.
- Serialize API writes using per-owner thread and OS locks; preserve consistency across process crashes and reloads.
- Migrate authenticated sessions' loose uploads into snapshots and resume interrupted cleanup without resurrecting documents. Never adopt browser-supplied owners.
- Count active and pending PDF bytes toward the quota; document whole-snapshot costs, migration, backup, and downgrade restrictions.
- Add failure injection, concurrent worker, crash recovery, real PDF parser, deletion UI, and session isolation regressions.

## Secure anonymous sessions — Unreleased

- Replace client-controlled X-User ownership with random server-issued cookies and persistent hashed session records.
- Enforce ownership across status, upload, ingestion, chat, and flashcards.
- Add 30-day absolute expiry, revocation, HTTPS host-bound HttpOnly cookies, no-store responses, and same-origin write checks.
- Initialize the browser session before study requests and remove legacy local IDs.
- Leave legacy files intact but require re-upload; arbitrary IDs cannot claim old notes.
- Add session isolation, forged-cookie, expiry, persistence, revocation, and CSRF regressions.
- User accounts, recovery, rate limiting, and transactional indexing remain separate work.

## Scoped retrieval — Unreleased

- Rank all chunks in the selected tag before choosing results, so matching notes are not lost behind unrelated global search results.
- Keep chat and flashcard retrieval inside an explicitly selected tag; return no match instead of silently searching other tags.
- Preserve the requested topic when retrieving notes for summaries.
- Honor explicit flashcard tags and only infer a week tag from a standalone week label.
- Add regression coverage for deep-ranked matches, missing tags, original source IDs, summary topics, and flashcard scope.

This batch does not resolve authentication, transactional index persistence, document replacement, or durable storage.

## 1.1.0 — Unreleased

### Security

- Constrain user storage paths and reject traversal identifiers.
- Bound ZIP, PDF, expanded archive, page, text, and ingestion sizes.
- Stop persisting API keys and prototype passwords in browser storage.
- Render untrusted values as text and apply a restrictive content security policy.
- Replace raw OpenAI errors with stable, non-sensitive responses.

### Reliability

- Add explicit OpenAI timeouts, retries, output limits, and embedding batches.
- Add strict structured flashcard output and document-hash deduplication.
- Add package-safe startup imports, readiness checks, atomic index writes, and per-user locks.
- Pin the tested Python runtime and production dependencies.

### User experience

- Replace the misleading local-only login with a polished anonymous study workspace.
- Improve responsive layout, loading/error/empty states, keyboard access, and answer copying.
- Add AI verification and ephemeral-storage notices.

### Tests and documentation

- Add mocked AI, API, upload, path, error, and frontend security tests.
- Add lint and dependency-audit configuration.
- Add setup, architecture, Render, security, troubleshooting, limitation, and roadmap docs.
