from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import server  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "USERS_DIR", tmp_path / "users")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(server.app) as test_client:
        yield test_client
