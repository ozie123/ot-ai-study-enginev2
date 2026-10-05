from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np

from backend import server
from backend.core import NoteStore
from tests.test_uploads import make_zip, post_zip


def test_complete_study_flow_preserves_replacement_and_deletion_consistency(
    client, monkeypatch
):
    cards = [
        {"question": f"Question {number}?", "answer": f"Answer {number}."}
        for number in range(1, 7)
    ]
    provider_calls = []

    class FakeResponses:
        def create(self, **kwargs):
            provider_calls.append(kwargs)
            if "text" in kwargs:
                return SimpleNamespace(output_text=json.dumps({"flashcards": cards}))
            return SimpleNamespace(output_text="The notes support the requested answer.")

    fake_client = SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(
        NoteStore,
        "_embed",
        lambda self, texts: np.array([[1, 0]] * len(texts), dtype="float32"),
    )
    monkeypatch.setattr(
        server,
        "read_pdf_pages",
        lambda stream, **kwargs: [(1, stream.read().decode().split("\n", 1)[1])],
    )
    monkeypatch.setattr(server, "create_openai_client", lambda api_key: fake_client)

    assert client.get("/status").json()["ready"] is False

    first_upload = post_zip(
        client,
        make_zip({"Week1.pdf": b"%PDF-1.4\nOriginal occupational therapy notes."}),
    )
    assert first_upload.status_code == 200
    assert client.get("/status").json()["document_files"] == [
        {"name": "week1.pdf", "indexed": False, "pending": True}
    ]

    ingest = client.post(
        "/ingest_user_pdfs", json={}, headers={"Authorization": "Bearer test-key"}
    )
    assert ingest.status_code == 200
    assert ingest.json()["files"] == 1
    assert client.get("/status").json() == {
        "chunks": 1,
        "documents": 1,
        "tags": ["week1"],
        "ready": True,
        "document_files": [
            {"name": "week1.pdf", "indexed": True, "pending": False}
        ],
    }

    answer = client.post(
        "/chat",
        json={"question": "What do the notes say?", "mode": "normal"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert answer.status_code == 200
    assert answer.json()["status"] == "answered"
    assert answer.json()["sources"] == ["week1.pdf (page 1)"]

    flashcards = client.post(
        "/flashcards",
        json={"topic": "occupational therapy", "tag": "week1"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert flashcards.status_code == 200
    assert flashcards.json()["flashcards"] == cards

    replacement = post_zip(
        client,
        make_zip({"WEEK1.pdf": b"%PDF-1.4\nReplacement participation notes."}),
    )
    assert replacement.status_code == 200
    assert client.get("/status").json()["document_files"] == [
        {"name": "week1.pdf", "indexed": True, "pending": True}
    ]
    assert (
        client.post(
            "/ingest_user_pdfs", json={}, headers={"Authorization": "Bearer test-key"}
        ).status_code
        == 200
    )

    replacement_answer = client.post(
        "/chat",
        json={"question": "Summarize the replacement notes.", "mode": "normal"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert replacement_answer.status_code == 200
    latest_prompt = provider_calls[-1]["input"][1]["content"]
    assert "Replacement participation notes." in latest_prompt
    assert "Original occupational therapy notes." not in latest_prompt

    deletion = client.delete("/documents/week1.pdf")
    assert deletion.status_code == 200
    assert client.get("/status").json() == {
        "chunks": 0,
        "documents": 0,
        "tags": [],
        "ready": False,
        "document_files": [],
    }
    assert client.delete("/documents/week1.pdf").status_code == 404
