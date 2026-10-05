from __future__ import annotations

import io
import multiprocessing
import sqlite3
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import server, usage_limits


def reserve_in_worker(root):
    try:
        usage_limits.consume(root, [("shared", 3, 86400)])
        return 200
    except HTTPException as error:
        return error.status_code


def hold_lease_in_worker(root, ready_path, release_path, seconds=60):
    with usage_limits.lease(root, [("shared-lease", 1)], seconds=seconds):
        Path(ready_path).touch()
        deadline = time.monotonic() + 10
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.02)


def pdf_zip(name="private.pdf"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, b"%PDF-1.4\nmock")
    return {"file": ("notes.zip", output.getvalue(), "application/zip")}


def test_independent_workers_cannot_overspend(tmp_path):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(reserve_in_worker, [tmp_path] * 12))
    assert results.count(200) == 3
    assert results.count(429) == 9


def test_atomic_reservation_expiry_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_limits.time, "time", lambda: 100)
    usage_limits.consume(tmp_path, [("owner", 1, 60)])
    with pytest.raises(HTTPException) as blocked:
        usage_limits.consume(tmp_path, [("global", 2, 60), ("owner", 1, 60)])
    assert blocked.value.headers == {"Retry-After": "20"}
    # Every call opens a new connection; rejected batches cannot consume global slots.
    usage_limits.consume(tmp_path, [("global", 2, 60)])
    usage_limits.consume(tmp_path, [("global", 2, 60)])
    with pytest.raises(HTTPException):
        usage_limits.consume(tmp_path, [("global", 2, 60)])
    monkeypatch.setattr(usage_limits.time, "time", lambda: 120)
    usage_limits.consume(tmp_path, [("global", 2, 60), ("owner", 1, 60)])
    with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
        assert sorted(db.execute("SELECT used FROM usage_limits")) == [(1,), (1,)]


def test_concurrency_lease_is_shared_across_workers_and_released(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    worker = context.Process(target=hold_lease_in_worker, args=(tmp_path, ready, release))
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with (
            pytest.raises(HTTPException) as blocked,
            usage_limits.lease(tmp_path, [("shared-lease", 1)], seconds=60),
        ):
            pass
        assert blocked.value.status_code == 429
        assert 1 <= int(blocked.value.headers["Retry-After"]) <= 60
    finally:
        release.touch()
        worker.join(timeout=10)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
    assert worker.exitcode == 0
    with usage_limits.lease(tmp_path, [("shared-lease", 1)], seconds=60):
        pass


def test_failed_concurrency_lease_releases_and_active_lease_renews(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_limits.time, "time", lambda: 100)
    with (
        pytest.raises(RuntimeError),
        usage_limits.lease(tmp_path, [("owner", 1)], seconds=10),
    ):
        raise RuntimeError("simulated provider failure")
    with usage_limits.lease(tmp_path, [("owner", 1)], seconds=10):
        pass

    clock = [100]
    monkeypatch.setattr(usage_limits.time, "time", lambda: clock[0])
    monkeypatch.setattr(usage_limits, "_lease_renew_interval", lambda seconds: 0.01)
    with usage_limits.lease(tmp_path, [("owner", 1)], seconds=10):
        clock[0] = 108
        deadline = time.monotonic() + 2
        expires_at = 0
        while expires_at < 118 and time.monotonic() < deadline:
            with sqlite3.connect(tmp_path / "sessions.sqlite3") as db:
                expires_at = db.execute(
                    "SELECT expires_at FROM usage_leases WHERE scope = 'owner'"
                ).fetchone()[0]
            time.sleep(0.01)
        assert expires_at >= 118
        clock[0] = 111
        with (
            pytest.raises(HTTPException) as blocked,
            usage_limits.lease(tmp_path, [("owner", 1)], seconds=10),
        ):
            pass
        assert blocked.value.status_code == 429


def test_crashed_worker_lease_expires(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = tmp_path / "crash-ready"
    never_release = tmp_path / "never-release"
    worker = context.Process(
        target=hold_lease_in_worker,
        args=(tmp_path, ready, never_release, 1),
    )
    worker.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    worker.terminate()
    worker.join(timeout=5)
    assert not worker.is_alive()
    time.sleep(1.2)
    with usage_limits.lease(tmp_path, [("shared-lease", 1)], seconds=1):
        pass


def test_session_mint_limit_cannot_be_bypassed_with_new_cookies_or_headers(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_NEW_SESSIONS_PER_HOUR", 1)
    # Fixture already minted the one permitted session. Resumption remains available.
    assert client.post("/session").status_code == 200
    with TestClient(server.app, base_url="https://testserver") as other:
        response = other.post(
            "/session", headers={"X-Study-Request": "1", "X-Forwarded-For": "1.2.3.4"}
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        assert "set-cookie" not in response.headers
    with sqlite3.connect(server.USERS_DIR / "sessions.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


@pytest.mark.parametrize(
    "endpoint,payload",
    [("/chat", {"question": "test"}), ("/flashcards", {"topic": "test"}),
     ("/ingest_user_pdfs", {})],
)
def test_all_ai_routes_reject_before_loading_or_provider_work(client, monkeypatch, endpoint, payload):
    monkeypatch.setattr(server, "MAX_AI_REQUESTS_PER_DAY", 1)
    server.reserve_ai_request("another-owner")

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected requests must not load notes or call AI")

    monkeypatch.setattr(server, "load_store_for_user", forbidden)
    monkeypatch.setattr(server, "document_store", forbidden)
    monkeypatch.setattr(server, "create_openai_client", forbidden)
    response = client.post(endpoint, json=payload, headers={"Authorization": "Bearer fake"})
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
    assert int(response.headers["retry-after"]) > 0


def test_session_budget_shared_across_ai_routes_but_not_owners(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_AI_REQUESTS_PER_SESSION_HOUR", 1)
    headers = {"Authorization": "Bearer fake"}
    assert client.post("/chat", json={"question": "test"}, headers=headers).status_code == 200
    assert client.post("/flashcards", json={"topic": "test"}, headers=headers).status_code == 429
    with TestClient(server.app, base_url="https://testserver", headers={"X-Study-Request": "1"}) as other:
        assert other.post("/session").status_code == 200
        assert other.post("/chat", json={"question": "test"}, headers=headers).status_code == 200
    assert client.get("/status").status_code == 200
    assert client.delete("/session").status_code == 200


def test_upload_budget_rejects_before_application_reads_or_decompresses(client, monkeypatch):
    owner = server.sessions.lookup(
        server.USERS_DIR, client.cookies.get(server.SESSION_COOKIE_NAME)
    )[0]
    monkeypatch.setattr(server, "MAX_UPLOADS_PER_SESSION_HOUR", 1)
    server.reserve_upload(owner)

    async def forbidden(*args, **kwargs):
        pytest.fail("A rejected upload must not read or decompress the uploaded file")

    monkeypatch.setattr(server, "read_upload_limited", forbidden)
    response = client.post("/upload_zip", files=pdf_zip())
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_ai_concurrency_rejects_provider_work_but_not_control_routes(client, monkeypatch):
    assert client.post("/upload_zip", files=pdf_zip()).status_code == 200
    owner = server.sessions.lookup(
        server.USERS_DIR, client.cookies.get(server.SESSION_COOKIE_NAME)
    )[0]
    with server.ai_request_slot(owner):
        assert client.get("/status").status_code == 200
        monkeypatch.setattr(server, "read_pdf_pages", lambda *args, **kwargs: [(1, "notes")])

        def forbidden(*args, **kwargs):
            pytest.fail("A concurrency rejection must happen before provider work")

        monkeypatch.setattr(server.NoteStore, "add_chunks", forbidden)
        ingest = client.post(
            "/ingest_user_pdfs", json={}, headers={"Authorization": "Bearer fake"}
        )
        assert ingest.status_code == 429
        assert int(ingest.headers["retry-after"]) > 0
        assert client.get("/status").json()["document_files"][0]["pending"] is True
        assert client.delete("/documents/private.pdf").status_code == 200

        monkeypatch.setattr(
            server, "load_store_for_user", lambda *args, **kwargs: SimpleNamespace(chunks=["x"])
        )
        monkeypatch.setattr(server, "select_hits", forbidden)
        for endpoint, payload in [
            ("/chat", {"question": "test"}),
            ("/flashcards", {"topic": "test"}),
        ]:
            response = client.post(
                endpoint, json=payload, headers={"Authorization": "Bearer fake"}
            )
            assert response.status_code == 429
            assert int(response.headers["retry-after"]) > 0
        assert client.delete("/session").status_code == 200


def test_provider_failures_are_not_refunded(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_AI_REQUESTS_PER_SESSION_HOUR", 1)

    def failed(*args, **kwargs):
        raise HTTPException(503, "Simulated downstream failure")

    monkeypatch.setattr(server, "load_store_for_user", failed)
    kwargs = {"json": {"question": "test"}, "headers": {"Authorization": "Bearer fake"}}
    assert client.post("/chat", **kwargs).status_code == 503
    assert client.post("/chat", **kwargs).status_code == 429


def test_corrupt_accounting_fails_closed_without_leaking_details(tmp_path):
    (tmp_path / "sessions.sqlite3").write_bytes(b"not a database")
    with pytest.raises(HTTPException) as blocked:
        usage_limits.consume(tmp_path, [("global", 5, 60)])
    assert blocked.value.status_code == 503
    assert "database" not in blocked.value.detail


def test_denied_authentication_does_not_consume_ai_budget(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_AI_REQUESTS_PER_DAY", 1)
    assert client.post("/chat", json={"question": "test"}).status_code == 503
    assert client.post("/chat", json={"question": "test"},
                       headers={"Authorization": "Bearer fake", "X-Study-Request": ""}).status_code == 403
    server.reserve_ai_request("valid-owner")
