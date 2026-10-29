import json
import os
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


def replace_snapshot_metadata(snapshot, **updates):
    with zipfile.ZipFile(snapshot) as archive:
        files = {member.filename: archive.read(member) for member in archive.infolist()}
    payload = json.loads(files["metadata"])
    payload.update(updates)
    files["metadata"] = json.dumps(payload).encode()
    with zipfile.ZipFile(snapshot, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


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


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires Windows privileges")
def test_load_rejects_symlinked_snapshot_without_touching_target(tmp_path):
    store = populated(tmp_path)
    store.save()
    target = tmp_path / "real.snapshot"
    store.snapshot_path.replace(target)
    store.snapshot_path.symlink_to(target)

    with pytest.raises(ValueError, match="Unsafe private storage file"):
        NoteStore(tmp_path).load()

    assert target.is_file()
    assert store.snapshot_path.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="inode identity is POSIX-specific")
def test_load_rejects_snapshot_replacement_during_open(tmp_path, monkeypatch):
    store = populated(tmp_path)
    store.save()
    replacement = tmp_path / "replacement.snapshot"
    replacement.write_bytes(store.snapshot_path.read_bytes())
    real_open = core.os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, **kwargs):
        nonlocal replaced
        descriptor = real_open(path, flags, mode, **kwargs)
        if not replaced and path == store.snapshot_path and flags & os.O_ACCMODE == os.O_RDONLY:
            replaced = True
            os.replace(replacement, store.snapshot_path)
        return descriptor

    monkeypatch.setattr(core.os, "open", replacing_open)
    loaded = populated(tmp_path, text="in-memory")
    with pytest.raises(ValueError, match="changed while opening"):
        loaded.load()

    assert replaced is True
    assert loaded.chunks == ["in-memory"]


def test_load_rejects_unexpected_snapshot_member(tmp_path):
    store = populated(tmp_path)
    store.save()
    with zipfile.ZipFile(store.snapshot_path, "a") as archive:
        archive.writestr("unexpected", b"hidden data")

    loaded = populated(tmp_path, text="in-memory")
    with pytest.raises(ValueError, match="Invalid study snapshot archive"):
        loaded.load()
    assert loaded.chunks == ["in-memory"]


def test_load_rejects_unexpected_member_before_deserializing_index(
    tmp_path, monkeypatch
):
    store = populated(tmp_path)
    store.save()
    with zipfile.ZipFile(store.snapshot_path, "a") as archive:
        archive.writestr("unexpected", b"hidden data")

    def unexpected_deserialize(*_args, **_kwargs):
        raise AssertionError("index must not be deserialized before layout validation")

    monkeypatch.setattr(core.faiss, "deserialize_index", unexpected_deserialize)
    with pytest.raises(ValueError, match="Invalid study snapshot archive"):
        NoteStore(tmp_path).load()


@pytest.mark.parametrize("field,value", [("unexpected", "value"), ("dim", True)])
def test_load_rejects_invalid_metadata_schema_before_deserializing_index(
    tmp_path, monkeypatch, field, value
):
    store = populated(tmp_path)
    store.save()
    replace_snapshot_metadata(store.snapshot_path, **{field: value})

    def unexpected_deserialize(*_args, **_kwargs):
        raise AssertionError("index must not be deserialized before metadata validation")

    monkeypatch.setattr(core.faiss, "deserialize_index", unexpected_deserialize)
    with pytest.raises(ValueError, match="Invalid study index metadata"):
        NoteStore(tmp_path).load()


@pytest.mark.parametrize(
    "metadata",
    [
        [{"source": 7, "page": 1}],
        [{"source": "notes.pdf", "page": True}],
        [{"source": "notes.pdf", "page": 1, "is_gold": 0}],
        [{"source": "notes.pdf", "page": 1, "document_sha256": "invalid"}],
        [{"source": "notes.pdf", "page": 1, "document_sha256": 7}],
        [{"source": "notes.pdf", "page": 1, "unexpected": "value"}],
    ],
)
def test_load_rejects_invalid_chunk_metadata_before_deserializing_index(
    tmp_path, monkeypatch, metadata
):
    store = populated(tmp_path)
    store.documents = {"notes.pdf": b"%PDF document"}
    store.save()
    replace_snapshot_metadata(store.snapshot_path, meta=metadata)

    def unexpected_deserialize(*_args, **_kwargs):
        raise AssertionError("index must not be deserialized before metadata validation")

    monkeypatch.setattr(core.faiss, "deserialize_index", unexpected_deserialize)
    with pytest.raises(ValueError, match="Invalid study chunk metadata"):
        NoteStore(tmp_path).load()


def test_invalid_chunk_metadata_save_preserves_previous_snapshot(tmp_path):
    store = populated(tmp_path)
    store.documents = {"notes.pdf": b"%PDF document"}
    store.save()
    original = store.snapshot_path.read_bytes()
    store.meta = [{"source": "notes.pdf", "page": False}]

    with pytest.raises(ValueError, match="Invalid study chunk metadata"):
        store.save()

    assert store.snapshot_path.read_bytes() == original


def test_load_rejects_duplicate_snapshot_member(tmp_path):
    store = populated(tmp_path)
    store.save()
    with (
        zipfile.ZipFile(store.snapshot_path, "a") as archive,
        pytest.warns(UserWarning, match="Duplicate name"),
    ):
        archive.writestr("metadata", b"{}")

    loaded = populated(tmp_path, text="in-memory")
    with pytest.raises(ValueError, match="Invalid study snapshot archive"):
        loaded.load()
    assert loaded.chunks == ["in-memory"]


def test_load_rejects_snapshot_expansion_before_reading_members(tmp_path, monkeypatch):
    snapshot = tmp_path / "study.snapshot"
    with zipfile.ZipFile(snapshot, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index", bytes(range(256)) * 40)
        archive.writestr("metadata", b"{}")
    with zipfile.ZipFile(snapshot) as archive:
        expanded = sum(member.file_size for member in archive.infolist())
    stored = snapshot.stat().st_size
    assert stored < expanded

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("snapshot members must not be read before expansion validation")

    monkeypatch.setattr(core.zipfile.ZipFile, "read", unexpected_read)
    with pytest.raises(ValueError, match="configured expansion limit"):
        NoteStore(tmp_path, max_snapshot_bytes=(stored + expanded) // 2).load()


def test_load_rejects_unsafe_snapshot_compression_before_reading_members(
    tmp_path, monkeypatch
):
    snapshot = tmp_path / "study.snapshot"
    with zipfile.ZipFile(
        snapshot, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.writestr("index", b"x" * 1_000_000)
        archive.writestr("metadata", b"{}")
    with zipfile.ZipFile(snapshot) as archive:
        index = archive.getinfo("index")
        assert index.file_size > index.compress_size * core.MAX_SNAPSHOT_COMPRESSION_RATIO

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("snapshot members must not be read before ratio validation")

    monkeypatch.setattr(core.zipfile.ZipFile, "read", unexpected_read)
    with pytest.raises(ValueError, match="unsafe compression ratio"):
        NoteStore(tmp_path).load()


def test_load_rejects_unsupported_snapshot_compression_before_reading_members(
    tmp_path, monkeypatch
):
    snapshot = tmp_path / "study.snapshot"
    with zipfile.ZipFile(snapshot, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("index", b"index")
        archive.writestr("metadata", b"{}")

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("snapshot members must not be read before format validation")

    monkeypatch.setattr(core.zipfile.ZipFile, "read", unexpected_read)
    with pytest.raises(ValueError, match="unsupported compression method"):
        NoteStore(tmp_path).load()


def test_load_rejects_encrypted_snapshot_member_before_reading_it(tmp_path, monkeypatch):
    populated(tmp_path).save()
    infolist = core.zipfile.ZipFile.infolist

    def encrypted_members(archive):
        members = infolist(archive)
        members[0].flag_bits |= 0x1
        return members

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("snapshot members must not be read before encryption validation")

    monkeypatch.setattr(core.zipfile.ZipFile, "infolist", encrypted_members)
    monkeypatch.setattr(core.zipfile.ZipFile, "read", unexpected_read)
    with pytest.raises(ValueError, match="encrypted members"):
        NoteStore(tmp_path).load()


def test_load_rejects_excess_snapshot_members_before_reading_them(tmp_path, monkeypatch):
    populated(tmp_path).save()

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("snapshot members must not be read before count validation")

    monkeypatch.setattr(core.zipfile.ZipFile, "read", unexpected_read)
    with pytest.raises(ValueError, match="too many members"):
        NoteStore(tmp_path, max_snapshot_members=1).load()


def test_oversized_save_preserves_previous_snapshot(tmp_path):
    store = populated(tmp_path)
    store.save()
    original = store.snapshot_path.read_bytes()
    store.max_snapshot_bytes = len(original) + 100
    store.chunks = ["updated" * 10_000]

    with pytest.raises(ValueError, match="configured size limit"):
        store.save()

    assert store.snapshot_path.read_bytes() == original
    loaded = NoteStore(tmp_path)
    assert loaded.load() and loaded.chunks == ["original"]
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
