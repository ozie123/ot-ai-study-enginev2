from __future__ import annotations

import io
import sqlite3
import zipfile

import numpy as np
import pytest
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
