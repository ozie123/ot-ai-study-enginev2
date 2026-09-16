"""Persistent anonymous sessions. Cookie possession authorizes one study bucket."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, Request

SESSION_SECONDS = 30 * 24 * 60 * 60
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def require_same_origin_write(request: Request) -> None:
    # A custom header makes even multipart submissions non-simple browser requests.
    # Credentialed cross-origin CORS is intentionally not enabled.
    if request.headers.get("X-Study-Request") != "1":
        raise HTTPException(403, "A same-origin study request is required.")
    origin = request.headers.get("origin")
    if origin is not None and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin study requests are not allowed.")
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        raise HTTPException(403, "Cross-origin study requests are not allowed.")


@contextmanager
def database(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "sessions.sqlite3"
    # Request restrictive permissions at creation; never truncate an existing DB.
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions "
            "(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at INTEGER NOT NULL)"
        )
        with connection:
            yield connection
    finally:
        connection.close()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lookup(root: Path, token: str | None) -> tuple[str, int] | None:
    if not token or not TOKEN_PATTERN.fullmatch(token):
        return None
    with database(root) as connection:
        row = connection.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash(token), int(time.time())),
        ).fetchone()
    return (str(row[0]), int(row[1])) if row else None


def create(root: Path) -> tuple[str, str, int]:
    token = secrets.token_urlsafe(32)
    user_id = "s_" + secrets.token_hex(16)
    expires_at = int(time.time()) + SESSION_SECONDS
    with database(root) as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)", (token_hash(token), user_id, expires_at)
        )
    return token, user_id, expires_at


def revoke(root: Path, token: str) -> None:
    with database(root) as connection:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
