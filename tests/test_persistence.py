import json
import zipfile

import faiss
import numpy as np
import pytest
from fastapi import HTTPException

from backend import core, server
from backend.core import NoteStore


def populated(path, text="original"):
    store = NoteStore(path)
    store.index = faiss.IndexFlatIP(2)
    store.index.add(np.array([[1, 0]], dtype="float32"))
    store.dim = 2
    store.chunks = [text]
    store.tags = ["week1"]
    store.meta = [{"source": "notes.pdf", "page": 1}]
    return store


def legacy(store):
    faiss.write_index(store.index, str(store.faiss_path))
    store.chunks_path.write_text(json.dumps({
        "chunks": store.chunks, "tags": store.tags, "meta": store.meta, "dim": store.dim,
    }))


def test_snapshot_roundtrip_and_search(tmp_path, monkeypatch):
    store = populated(tmp_path)
    store.save()
    loaded = NoteStore(tmp_path)
    assert loaded.load()
    monkeypatch.setattr(loaded, "_embed", lambda _: np.array([[1, 0]], dtype="float32"))
    assert loaded.search("question", tag="week1")[0][2:] == ("original", store.meta[0])


def test_snapshot_flushes_parent_after_atomic_publication(tmp_path, monkeypatch):
    events = []
    replace = core.os.replace

    def tracked_replace(source, destination):
        replace(source, destination)
        events.append("replace")

    def tracked_directory_sync(path):
        assert path == tmp_path
        assert (path / "study.snapshot").is_file()
        events.append("directory-fsync")

    monkeypatch.setattr(core.os, "replace", tracked_replace)
    monkeypatch.setattr(core, "_fsync_directory", tracked_directory_sync)
    populated(tmp_path).save()
    assert events == ["replace", "directory-fsync"]


def test_directory_flush_failure_preserves_published_snapshot(tmp_path, monkeypatch):
    store = populated(tmp_path)
    store.save()
    store.chunks = ["updated"]

    def fail(_path):
        raise OSError("injected directory flush failure")

    monkeypatch.setattr(core, "_fsync_directory", fail)
    with pytest.raises(OSError, match="directory flush failure"):
        store.save()

    loaded = NoteStore(tmp_path)
    assert loaded.load() and loaded.chunks == ["updated"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["study.snapshot"]


@pytest.mark.parametrize("failure", ["serialize", "metadata", "fsync", "replace"])
def test_failed_save_preserves_previous_snapshot(tmp_path, monkeypatch, failure):
    store = populated(tmp_path)
    store.save()
    original = store.snapshot_path.read_bytes()
    store.chunks = ["updated"]

    def fail(*args, **kwargs):
        raise OSError("injected write failure")

    module, name = {
        "serialize": (core.faiss, "serialize_index"),
        "metadata": (core.json, "dumps"),
        "fsync": (core.os, "fsync"),
        "replace": (core.os, "replace"),
    }[failure]
    with monkeypatch.context() as patch:
        patch.setattr(module, name, fail)
        with pytest.raises(OSError):
            store.save()
    assert store.snapshot_path.read_bytes() == original
    loaded = NoteStore(tmp_path)
    assert loaded.load() and loaded.chunks == ["original"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["study.snapshot"]


def test_legacy_migration_and_clear_do_not_resurrect_old_notes(tmp_path):
    store = populated(tmp_path)
    legacy(store)
    loaded = NoteStore(tmp_path)
    assert loaded.load()
    loaded.chunks = ["new"]
    loaded.save()
    assert store.faiss_path.exists()  # Migration does not delete user files.
    assert store.load() and store.chunks == ["new"]
    store.clear()
    assert not NoteStore(tmp_path).load()


def test_corrupt_snapshot_never_falls_back_to_legacy(tmp_path):
    store = populated(tmp_path)
    legacy(store)
    store.snapshot_path.write_bytes(b"broken")
    with pytest.raises(zipfile.BadZipFile):
        store.load()
    assert store.chunks == ["original"]


def test_restart_ignores_unpublished_partial_files(tmp_path):
    populated(tmp_path).save()
    (tmp_path / "tmp-interrupted-save").write_bytes(b"incomplete candidate")
    loaded = NoteStore(tmp_path)
    assert loaded.load() and loaded.chunks == ["original"]


def test_invalid_save_does_not_publish(tmp_path):
    store = populated(tmp_path)
    store.save()
    original = store.snapshot_path.read_bytes()
    store.tags = []
    with pytest.raises(ValueError):
        store.save()
    assert store.snapshot_path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("chunks", []), ("tags", [4]), ("meta", ["bad"]), ("dim", 3),
])
def test_invalid_metadata_does_not_replace_in_memory_state(tmp_path, field, value):
    store = populated(tmp_path)
    legacy(store)
    payload = json.loads(store.chunks_path.read_text())
    payload[field] = value
    store.chunks_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        store.load()
    assert store.chunks == ["original"]
    assert store.dim == 2


def test_partial_legacy_index_is_not_treated_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "USERS_DIR", tmp_path)
    paths = server.user_paths("test-session")
    paths["chunks"].write_text("{}")
    with pytest.raises(HTTPException) as caught:
        server.load_store_for_user("test-session")
    assert caught.value.status_code == 500
    assert "backup" in caught.value.detail


def test_server_uses_snapshot_and_checks_store_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "USERS_DIR", tmp_path)
    store = populated(server.user_paths("owner")["data_dir"])
    server.save_store_for_user("owner", store)
    assert server.load_store_for_user("owner").chunks == ["original"]
    with pytest.raises(ValueError):
        server.save_store_for_user("other", store)
