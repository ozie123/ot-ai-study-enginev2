"""Atomic fixed-window request budgets shared by workers on one local filesystem."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

try:
    from .sessions import database
except ImportError:
    from sessions import database  # type: ignore[no-redef]


def consume(root: Path, budgets: list[tuple[str, int, int]]) -> None:
    """Reserve all budgets or none. Attempts are not refunded on provider failure."""
    now = int(time.time())
    try:
        with database(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_limits "
                "(scope TEXT PRIMARY KEY, expires_at INTEGER NOT NULL, used INTEGER NOT NULL)"
            )
            connection.execute("DELETE FROM usage_limits WHERE expires_at <= ?", (now,))
            reservations = []
            retry_after = 0
            for scope, limit, seconds in budgets:
                if limit < 1 or seconds < 1:
                    raise ValueError("Usage budgets must be positive")
                row = connection.execute(
                    "SELECT expires_at, used FROM usage_limits WHERE scope = ?", (scope,)
                ).fetchone()
                expires, used = row if row else ((now // seconds + 1) * seconds, 0)
                if used >= limit:
                    retry_after = max(retry_after, expires - now)
                reservations.append((scope, expires, used + 1))
            if retry_after:
                raise HTTPException(
                    429,
                    f"Study usage limit reached. Try again in {retry_after} seconds.",
                    headers={"Retry-After": str(retry_after)},
                )
            connection.executemany(
                "INSERT OR REPLACE INTO usage_limits VALUES (?, ?, ?)", reservations
            )
    except (sqlite3.Error, OSError) as error:
        # Never continue to a paid provider when accounting is unavailable.
        raise HTTPException(
            503, "Usage controls are unavailable. Retry shortly.", headers={"Retry-After": "10"}
        ) from error


def _lease_renew_interval(seconds: int) -> float:
    return max(0.1, seconds / 3)


def _renew_lease(
    root: Path, lease_id: str, seconds: int, stop: threading.Event
) -> None:
    while not stop.wait(_lease_renew_interval(seconds)):
        try:
            with database(root) as connection:
                connection.execute("BEGIN IMMEDIATE")
                renewed = connection.execute(
                    "UPDATE usage_leases SET expires_at = ? WHERE lease_id = ?",
                    (int(time.time()) + seconds, lease_id),
                )
                if renewed.rowcount == 0:
                    return
        except (sqlite3.Error, OSError):
            # Keep trying while the original lease remains valid. A prolonged
            # accounting outage eventually fails safe by allowing expiry.
            continue


def _release_lease(root: Path, lease_id: str) -> None:
    try:
        with database(root) as connection:
            connection.execute("DELETE FROM usage_leases WHERE lease_id = ?", (lease_id,))
    except (sqlite3.Error, OSError):
        # Do not replace a completed provider response with a cleanup error.
        # The bounded lease expiry safely recovers the slot after a crash/failure.
        pass


@contextmanager
def lease(root: Path, limits: list[tuple[str, int]], *, seconds: int) -> Iterator[None]:
    """Hold every requested slot, renewing until exit and expiring after a crash."""
    if not limits or seconds < 1 or any(limit < 1 for _, limit in limits):
        raise ValueError("Concurrency limits and lease duration must be positive")
    now = int(time.time())
    lease_id = uuid.uuid4().hex
    expires_at = now + seconds
    try:
        with database(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage_leases "
                "(scope TEXT NOT NULL, lease_id TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                "PRIMARY KEY (scope, lease_id))"
            )
            connection.execute("DELETE FROM usage_leases WHERE expires_at <= ?", (now,))
            retry_after = 0
            for scope, limit in limits:
                row = connection.execute(
                    "SELECT COUNT(*), MIN(expires_at) FROM usage_leases WHERE scope = ?",
                    (scope,),
                ).fetchone()
                active, earliest_expiry = int(row[0]), row[1]
                if active >= limit:
                    retry_after = max(retry_after, max(1, int(earliest_expiry) - now))
            if retry_after:
                raise HTTPException(
                    429,
                    "AI capacity is busy. Try again shortly.",
                    headers={"Retry-After": str(retry_after)},
                )
            connection.executemany(
                "INSERT INTO usage_leases VALUES (?, ?, ?)",
                [(scope, lease_id, expires_at) for scope, _ in limits],
            )
    except (sqlite3.Error, OSError) as error:
        # Never continue to a paid provider if concurrency accounting is unavailable.
        raise HTTPException(
            503, "Usage controls are unavailable. Retry shortly.", headers={"Retry-After": "10"}
        ) from error

    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_renew_lease,
        args=(root, lease_id, seconds, stop),
        name="usage-lease-heartbeat",
        daemon=True,
    )
    try:
        heartbeat.start()
    except RuntimeError as error:
        _release_lease(root, lease_id)
        raise HTTPException(
            503, "Usage controls are unavailable. Retry shortly.", headers={"Retry-After": "10"}
        ) from error

    try:
        yield
    finally:
        stop.set()
        heartbeat.join(timeout=1)
        _release_lease(root, lease_id)
