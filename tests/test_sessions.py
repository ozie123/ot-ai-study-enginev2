from __future__ import annotations

import io
import os
import sqlite3
import stat
import zipfile

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import server, sessions
from backend.core import NoteStore


def fresh_client():
    return TestClient(server.app, base_url="https://testserver", headers={"X-Study-Request": "1"})


def identity(client):
    return sessions.lookup(server.USERS_DIR, client.cookies.get(server.SESSION_COOKIE_NAME))[0]


def pdf_zip():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("private.pdf", b"%PDF-1.4\nprivate")
    return {"file": ("notes.zip", output.getvalue(), "application/zip")}


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/status", {}),
        ("post", "/upload_zip", {"files": pdf_zip()}),
        ("post", "/ingest_user_pdfs", {"json": {}}),
        ("post", "/chat", {"json": {"question": "private?"}}),
        ("post", "/flashcards", {"json": {"topic": "private"}}),
        ("delete", "/session", {}),
        ("delete", "/documents/private.pdf", {}),
    ],
)
def test_all_private_endpoints_require_session(client, method, path, kwargs):
    with fresh_client() as stranger:
        response = getattr(stranger, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_cookie_flags_idempotence_and_persistent_hashed_record(client):
    with fresh_client() as browser:
        response = browser.post("/session")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert all(flag in cookie for flag in ["HttpOnly", "Secure", "SameSite=strict", "Path=/"])
        assert "Domain=" not in cookie
        token = browser.cookies.get(server.SESSION_COOKIE_NAME)
        assert token not in response.text
        first = identity(browser)
        assert browser.post("/session").json() == response.json()
        # An independent client can resume with the existing cookie after a new DB connection.
        with fresh_client() as resumed:
            resumed.cookies.set(server.SESSION_COOKIE_NAME, token)
            assert identity(resumed) == first
            assert resumed.get("/status").status_code == 200
        with sqlite3.connect(server.USERS_DIR / "sessions.sqlite3") as db:
            hashes = [r[0] for r in db.execute("SELECT token_hash FROM sessions")]
        assert token not in hashes
        assert sessions.token_hash(token) in hashes


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode-bit regression")
def test_live_private_storage_restricts_existing_permissions(client):
    owner = identity(client)
    paths = server.user_paths(owner)
    database = server.USERS_DIR / "sessions.sqlite3"
    for directory in [server.USERS_DIR, paths["user_dir"], paths["data_dir"], paths["uploads_dir"]]:
        directory.chmod(0o777)
    database.chmod(0o666)

    assert sessions.lookup(server.USERS_DIR, client.cookies.get(server.SESSION_COOKIE_NAME))
    paths = server.user_paths(owner)

    assert stat.S_IMODE(server.USERS_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    for name in ["user_dir", "data_dir", "uploads_dir"]:
        assert stat.S_IMODE(paths[name].stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_session_database_rejects_symlink(tmp_path):
    root = tmp_path / "users"
    root.mkdir()
    target = tmp_path / "outside.sqlite3"
    target.write_bytes(b"outside")
    (root / "sessions.sqlite3").symlink_to(target)

    with pytest.raises(ValueError, match="Unsafe private storage file"):
        sessions.create(root)
    assert target.read_bytes() == b"outside"


@pytest.mark.parametrize(
    "column,value",
    [("user_id", "../../outside"), ("expires_at", "never")],
)
def test_live_lookup_rejects_malformed_session_records(tmp_path, column, value):
    root = tmp_path / "users"
    token, _, _ = sessions.create(root)
    statement = {
        "user_id": "UPDATE sessions SET user_id = ? WHERE token_hash = ?",
        "expires_at": "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
    }[column]
    with sqlite3.connect(root / "sessions.sqlite3") as connection:
        connection.execute(
            statement,
            (value, sessions.token_hash(token)),
        )

    with pytest.raises(ValueError, match="invalid record"):
        sessions.lookup(root, token)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_user_storage_rejects_symlinked_owner_directory(tmp_path, monkeypatch):
    root = tmp_path / "users"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "s_owner").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(server, "USERS_DIR", root)

    with pytest.raises(HTTPException) as caught:
        server.user_paths("s_owner")
    assert caught.value.status_code == 400
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("token", ["../../outside", "A" * 43, "", "a" * 500])
def test_forged_or_missing_cookie_is_rejected(client, token):
    with fresh_client() as browser:
        browser.cookies.set(server.SESSION_COOKIE_NAME, token)
        assert browser.get("/status").status_code == 401


def test_expired_session_does_not_silently_create_identity_on_private_request(client, monkeypatch):
    before = identity(client)
    now = sessions.time.time()
    monkeypatch.setattr(sessions.time, "time", lambda: now + sessions.SESSION_SECONDS + 1)
    response = client.get("/status")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert client.post("/session").status_code == 200
    assert identity(client) != before


def test_revocation_blocks_replay_and_keeps_files(client):
    user = identity(client)
    assert client.post("/upload_zip", files=pdf_zip()).status_code == 200
    old_token = client.cookies.get(server.SESSION_COOKIE_NAME)
    assert client.delete("/session").json() == {"ended": True}
    with fresh_client() as replay:
        replay.cookies.set(server.SESSION_COOKIE_NAME, old_token)
        assert replay.get("/status").status_code == 401
    assert server.load_store_for_user(user).pending["private.pdf"] == b"%PDF-1.4\nprivate"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"X-Study-Request": ""},
    ],
)
def test_csrf_rejected_before_session_creation_or_upload(client, headers):
    assert client.post("/session", headers=headers).status_code == 403
    assert client.post("/upload_zip", headers=headers, files=pdf_zip()).status_code == 403
    assert client.delete("/session", headers=headers).status_code == 403
    assert client.delete("/documents/private.pdf", headers=headers).status_code == 403


def test_same_origin_allowed_and_legacy_identifier_cannot_claim_data(client):
    assert client.post("/session", headers={"Origin": "https://testserver"}).status_code == 200
    assert client.post("/session", headers={"X-User": "local-default"}).status_code == 400
    assert client.get("/status", headers={"X-User": identity(client)}).status_code == 400


def test_two_sessions_cannot_read_or_overwrite_each_others_notes(client, monkeypatch):
    monkeypatch.setattr(
        server, "read_pdf_pages", lambda *args, **kwargs: [(1, "Private first session notes")]
    )
    monkeypatch.setattr(
        NoteStore, "_embed", lambda self, texts: np.array([[1, 0]] * len(texts), dtype="float32")
    )
    assert client.post("/upload_zip", files=pdf_zip()).status_code == 200
    headers = {"Authorization": "Bearer test-key"}
    assert client.post("/ingest_user_pdfs", json={}, headers=headers).json()["added_chunks"] == 1
    owner = identity(client)
    original = server.load_store_for_user(owner).documents["private.pdf"]
    with fresh_client() as other:
        assert other.post("/session").status_code == 200
        assert identity(other) != owner
        assert other.get("/status").json()["chunks"] == 0
        for path, payload in [
            ("/chat", {"question": "private?"}),
            ("/flashcards", {"topic": "private"}),
        ]:
            result = other.post(path, json=payload, headers=headers)
            assert result.status_code == 200
            assert "Private first session notes" not in result.text
            assert result.json().get("sources", []) == []
        assert other.post("/ingest_user_pdfs", json={}, headers=headers).json()["files"] == 0
        assert (
            other.post("/upload_zip", files=pdf_zip(), headers={"X-User": owner}).status_code == 400
        )
        assert other.post("/upload_zip", files=pdf_zip()).status_code == 200
        assert server.load_store_for_user(identity(other)).pending["private.pdf"] == original
        assert other.delete("/session").status_code == 200
    assert server.load_store_for_user(owner).documents["private.pdf"] == original
    assert client.get("/status").json()["chunks"] == 1
