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

try:
    from .private_storage import ensure_private_directory, open_private_regular_file
except ImportError:  # Supports `uvicorn server:app --app-dir backend`.
    from private_storage import (  # type: ignore[no-redef]
        ensure_private_directory,
        open_private_regular_file,
    )

SESSION_SECONDS = 30 * 24 * 60 * 60
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
USER_ID_PATTERN = re.compile(r"^s_[0-9a-f]{32}$")


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
    ensure_private_directory(root)
    path = root / "sessions.sqlite3"
    # Restrict existing databases too; never truncate one while preparing it.
    descriptor = open_private_regular_file(path, os.O_CREAT | os.O_WRONLY)
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
    if row is None:
        return None
    user_id, expires_at = row
    if (
        not isinstance(user_id, str)
        or not USER_ID_PATTERN.fullmatch(user_id)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
    ):
        raise ValueError("Session database contains an invalid record")
    return user_id, expires_at


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
