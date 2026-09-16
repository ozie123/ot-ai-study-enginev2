from __future__ import annotations

import faiss
import numpy as np
import pytest

from backend import server
from backend.core import NoteStore


def make_store(tmp_path, monkeypatch):
    store = NoteStore(tmp_path)
    # Matching notes sit below the old global shortlist of 40 results.
    vectors = np.array([[1, 0]] * 50 + [[0, 1], [0.6, 0.8]], dtype="float32")
    store.index = faiss.IndexFlatIP(2)
    store.index.add(vectors)
    store.chunks = [f"Passage {i}" for i in range(len(vectors))]
    store.tags = ["week1"] * 50 + ["week2"] * 2
    store.meta = [{"source": f"{tag}.pdf", "page": i + 1} for i, tag in enumerate(store.tags)]
    monkeypatch.setattr(store, "_embed", lambda texts: np.array([[1, 0]], dtype="float32"))
    return store


def test_tag_search_ranks_all_matching_notes_with_original_ids(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    hits = store.search("topic", k=4, tag="week2", widen=1)
    assert [hit[0] for hit in hits] == [51, 50]
    assert [hit[1] for hit in hits] == pytest.approx([0.6, 0])
    assert [hit[2] for hit in hits] == ["Passage 51", "Passage 50"]
    assert [hit[3]["page"] for hit in hits] == [52, 51]


@pytest.mark.parametrize("tag,k", [("absent", 4), (None, 0), ("week2", -1)])
def test_empty_search_does_not_call_provider(tmp_path, monkeypatch, tag, k):
    store = make_store(tmp_path, monkeypatch)

    def unexpected_embedding(texts):
        pytest.fail("An empty search must not call the embedding provider")

    monkeypatch.setattr(store, "_embed", unexpected_embedding)
    assert store.search("topic", k=k, tag=tag) == []


def test_unscoped_search_retains_global_ranking(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    hits = store.search("topic", k=4)
    assert len(hits) == 4
    assert all(store.tags[hit[0]] == "week1" for hit in hits)
    assert all(hit[1] == pytest.approx(1) for hit in hits)


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("/chat", {"question": "Explain participation", "tag": "missing"}),
        ("/flashcards", {"topic": "participation", "tag": "missing"}),
    ],
)
def test_missing_scope_never_falls_back_or_generates(
    client, tmp_path, monkeypatch, endpoint, payload
):
    store = make_store(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "load_store_for_user", lambda *args, **kwargs: store)

    def unexpected_client(*args, **kwargs):
        pytest.fail("No generation should occur without matching notes")

    monkeypatch.setattr(server, "create_openai_client", unexpected_client)
    response = client.post(
        endpoint, headers={"Authorization": "Bearer test-key"}, json=payload
    )
    assert response.status_code == 200
    if endpoint == "/chat":
        assert response.json()["status"] == "not_found"
        assert response.json()["sources"] == []
    else:
        assert response.json()["flashcards"] == []


@pytest.mark.parametrize(
    "endpoint,payload,expected_query,expected_tag,expected_k",
    [
        (
            "/chat",
            {"question": "Summarise photosynthesis", "tag": "week2"},
            "Summarise photosynthesis",
            "week2",
            8,
        ),
        ("/flashcards", {"topic": "week1", "tag": "week2"}, "week1", "week2", 8),
        ("/flashcards", {"topic": "week 2 photosynthesis"}, "week 2 photosynthesis", None, 8),
        (
            "/flashcards",
            {"topic": "Week 2"},
            "main concepts definitions examples key points overview",
            "week2",
            8,
        ),
    ],
)
def test_query_keeps_topic_and_explicit_scope(
    client, tmp_path, monkeypatch, endpoint, payload, expected_query, expected_tag, expected_k
):
    store = make_store(tmp_path, monkeypatch)
    calls = []

    def search(query, k, tag):
        calls.append((query, tag, k))
        return []

    monkeypatch.setattr(store, "search", search)
    monkeypatch.setattr(server, "load_store_for_user", lambda *args, **kwargs: store)
    response = client.post(
        endpoint, headers={"Authorization": "Bearer test-key"}, json=payload
    )
    assert response.status_code == 200
    assert calls == [(expected_query, expected_tag, expected_k)]
