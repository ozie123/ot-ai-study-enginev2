from __future__ import annotations

import pytest

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
        headers={},
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
        headers={"Authorization": "Bearer test-token"},
        json={"question": "Hello", "mode": "normal", "unexpected": True},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "endpoint,payload",
    [("/chat", {"question": " \t\n "}), ("/flashcards", {"topic": " \t\n "})],
)
def test_blank_ai_input_is_rejected_before_budget_or_provider_work(
    client, monkeypatch, endpoint, payload
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Blank input must not reserve usage or reach provider work")

    monkeypatch.setattr(server, "reserve_ai_request", forbidden)
    monkeypatch.setattr(server, "load_store_for_user", forbidden)
    monkeypatch.setattr(server, "create_openai_client", forbidden)

    response = client.post(endpoint, json=payload, headers={"Authorization": "Bearer test-key"})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "model,field,value,expected",
    [
        (server.ChatRequest, "question", "  useful question  ", "useful question"),
        (server.FlashcardRequest, "topic", "  useful topic  ", "useful topic"),
    ],
)
def test_ai_input_is_normalized_before_route_work(model, field, value, expected):
    assert getattr(model(**{field: value}), field) == expected


def test_security_headers_are_present(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-request-id"]


def test_empty_data_directory_override_uses_safe_default():
    assert server.resolve_data_root("") == (server.BASE_DIR / "backend" / "users").resolve()
    assert server.resolve_data_root("   ") != server.BASE_DIR.resolve()
