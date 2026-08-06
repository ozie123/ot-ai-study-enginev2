# Changelog

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
