from __future__ import annotations

from backend import server


def test_health_and_readiness(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "ok": True,
        "app": "OT AI Study Engine",
        "version": "1.1.0",
    }
    assert client.get("/ready").json() == {"ready": True}


def test_public_config_does_not_expose_a_key(client):
    response = client.get("/config")
    assert response.status_code == 200
    assert response.json()["server_managed_ai"] is False
    assert "OPENAI_API_KEY" not in response.text


def test_missing_api_key_is_clear_and_safe(client):
    response = client.post(
        "/chat",
        headers={"X-User": "test-user"},
        json={"question": "What is occupational therapy?", "mode": "normal"},
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "AI service is not configured. Set OPENAI_API_KEY or provide a session key."
    }


def test_path_traversal_user_id_is_rejected(client, tmp_path):
    response = client.get("/status", headers={"X-User": "../../outside"})
    assert response.status_code == 400
    assert not (tmp_path / "outside").exists()


def test_request_validation_rejects_unknown_fields(client):
    response = client.post(
        "/chat",
        headers={"X-User": "test-user", "Authorization": "Bearer test-token"},
        json={"question": "Hello", "mode": "normal", "unexpected": True},
    )
    assert response.status_code == 422


def test_security_headers_are_present(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-request-id"]


def test_empty_data_directory_override_uses_safe_default():
    assert server.resolve_data_root("") == (server.BASE_DIR / "backend" / "users").resolve()
    assert server.resolve_data_root("   ") != server.BASE_DIR.resolve()
