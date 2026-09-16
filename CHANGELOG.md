# Changelog

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
