from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import backups, server, sessions
from backend.core import NoteStore


def populated(root: Path, owner: str = "s_" + "a" * 32) -> tuple[Path, str]:
    token, generated_owner, _ = sessions.create(root)
    with sqlite3.connect(root / "sessions.sqlite3") as connection:
        connection.execute(
            "UPDATE sessions SET user_id = ? WHERE user_id = ?", (owner, generated_owner)
        )
    data = root / owner / "data"
    store = NoteStore(data)
    store.index = __import__("faiss").IndexFlatIP(2)
    store.index.add(np.array([[1, 0]], dtype="float32"))
    store.dim = 2
    store.chunks = ["private notes"]
    store.tags = ["week1"]
    store.meta = [{"source": "notes.pdf", "page": 1, "is_gold": False}]
    store.documents = {"notes.pdf": b"%PDF private"}
    store.save()
    return root, token


def test_create_verify_and_restore_round_trip(tmp_path):
    source, token = populated(tmp_path / "live")
    backup = tmp_path / "backup.otai"
    manifest = backups.create_backup(source, backup)
    assert manifest["owners"] == ["s_" + "a" * 32]
    assert backups.verify_backup(backup)["format"] == 1

    restored = tmp_path / "restored"
    backups.restore_backup(backup, restored)
    assert sessions.lookup(restored, token)[0] == "s_" + "a" * 32
    store = NoteStore(restored / ("s_" + "a" * 32) / "data")
    assert store.load() is True
    assert store.chunks == ["private notes"]
    assert store.documents == {"notes.pdf": b"%PDF private"}
    assert backups.verify_backup(backup) == manifest

    if os.name != "nt":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(restored.stat().st_mode) == 0o700
        for path in restored.rglob("*"):
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_create_verifies_before_publication_and_preserves_racing_output(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    output = tmp_path / "backup.otai"
    verify = backups.verify_backup

    def competing_writer(path, limit):
        assert not output.exists()
        result = verify(path, limit)
        output.write_bytes(b"another writer's backup")
        return result

    monkeypatch.setattr(backups, "verify_backup", competing_writer)
    with pytest.raises(FileExistsError):
        backups.create_backup(source, output)
    assert output.read_bytes() == b"another writer's backup"
    assert not list(tmp_path.glob(".backup.otai.*.tmp"))


def test_exclusive_publication_moves_file_without_replacing_destination(tmp_path):
    source = tmp_path / "staged.otai"
    destination = tmp_path / "backup.otai"
    source.write_bytes(b"verified archive")

    backups._publish_path_exclusive(source, destination)

    assert not source.exists()
    assert destination.read_bytes() == b"verified archive"

    competing_source = tmp_path / "competing.otai"
    competing_source.write_bytes(b"new archive")
    with pytest.raises(FileExistsError, match="already exists"):
        backups._publish_path_exclusive(competing_source, destination)
    assert competing_source.read_bytes() == b"new archive"
    assert destination.read_bytes() == b"verified archive"


def test_create_failed_verification_never_publishes_or_removes_other_output(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    output = tmp_path / "backup.otai"

    def failing_verification(path, limit):
        assert path != output
        assert not output.exists()
        output.write_bytes(b"independent file")
        raise ValueError("verification failed")

    monkeypatch.setattr(backups, "verify_backup", failing_verification)
    with pytest.raises(ValueError, match="verification failed"):
        backups.create_backup(source, output)
    assert output.read_bytes() == b"independent file"
    assert not list(tmp_path.glob(".backup.otai.*.tmp"))


def test_backup_ignores_and_preserves_stale_pid_staging_file(tmp_path):
    source, _ = populated(tmp_path / "live")
    output = tmp_path / "backup.otai"
    stale = tmp_path / f".backup.otai.{os.getpid()}.tmp"
    stale.write_bytes(b"stale private staging file")

    backups.create_backup(source, output)

    assert backups.verify_backup(output)["format"] == backups.FORMAT_VERSION
    assert stale.read_bytes() == b"stale private staging file"
    assert list(tmp_path.glob(".backup.otai.*.tmp")) == [stale]


def test_backup_flushes_archive_before_publication_and_parent_after(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    output = tmp_path / "backup.otai"
    real_file_sync = backups._fsync_file
    real_directory_sync = backups._fsync_directory
    events = []

    def tracked_file_sync(path):
        assert path.name.startswith(".backup.otai.")
        assert not output.exists()
        events.append("archive")
        real_file_sync(path)

    def tracked_directory_sync(path):
        assert output.exists()
        events.append("parent")
        real_directory_sync(path)

    monkeypatch.setattr(backups, "_fsync_file", tracked_file_sync)
    monkeypatch.setattr(backups, "_fsync_directory", tracked_directory_sync)
    backups.create_backup(source, output)

    assert events == ["archive", "parent"]


def test_backup_sync_failure_before_publication_leaves_no_output(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    output = tmp_path / "backup.otai"

    def failing_sync(path):
        raise OSError("simulated archive sync failure")

    monkeypatch.setattr(backups, "_fsync_file", failing_sync)
    with pytest.raises(OSError, match="archive sync failure"):
        backups.create_backup(source, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".backup.otai.*.tmp"))


def test_backup_parent_sync_failure_preserves_published_output(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    output = tmp_path / "backup.otai"

    def failing_sync(path):
        raise OSError("simulated parent sync failure")

    monkeypatch.setattr(backups, "_fsync_directory", failing_sync)
    with pytest.raises(OSError, match="parent sync failure"):
        backups.create_backup(source, output)

    assert backups.verify_backup(output)["format"] == backups.FORMAT_VERSION
    assert not list(tmp_path.glob(".backup.otai.*.tmp"))


def test_cli_restore_retains_api_access_and_isolates_later_deletion(tmp_path, monkeypatch):
    source, token = populated(tmp_path / "source")
    backup = tmp_path / "backup.otai"
    restored = tmp_path / "restored"
    owner = sessions.lookup(source, token)[0]
    snapshot = source / owner / "data" / "study.snapshot"
    original_snapshot = snapshot.read_bytes()
    commands = [
        ["create", "--data-dir", str(source), "--output", str(backup)],
        ["verify", "--backup", str(backup)],
        ["restore", "--backup", str(backup), "--destination", str(restored)],
    ]
    for command in commands:
        result = subprocess.run(
            [sys.executable, "-m", "backend.backups", *command],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert f"Backup {command[0]} succeeded" in result.stdout

    monkeypatch.setattr(server, "USERS_DIR", restored)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        NoteStore,
        "_embed",
        lambda self, texts: np.array([[1, 0]] * len(texts), dtype="float32"),
    )
    prompts = []

    def response(**kwargs):
        prompts.append(kwargs)
        return SimpleNamespace(output_text="The restored notes are available.")

    provider = SimpleNamespace(responses=SimpleNamespace(create=response))
    monkeypatch.setattr(server, "create_openai_client", lambda api_key: provider)
    with TestClient(
        server.app, base_url="https://testserver", headers={"X-Study-Request": "1"}
    ) as client:
        assert client.get("/status").status_code == 401
        client.cookies.set(server.SESSION_COOKIE_NAME, token)
        status = client.get("/status")
        assert status.status_code == 200
        assert status.json()["document_files"] == [
            {"name": "notes.pdf", "indexed": True, "pending": False}
        ]
        answer = client.post(
            "/chat",
            json={"question": "What do my restored notes say?"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert answer.status_code == 200
        assert answer.json()["sources"] == ["notes.pdf (page 1)"]
        assert "private notes" in prompts[-1]["input"][1]["content"]
        assert client.delete("/documents/notes.pdf").status_code == 200
        assert client.get("/status").json()["documents"] == 0

    # Restored mutations persist but cannot change the source or recovery archive.
    reloaded = NoteStore(restored / owner / "data")
    assert reloaded.load()
    assert reloaded.documents == {}
    assert snapshot.read_bytes() == original_snapshot
    assert sessions.lookup(source, token)[0] == owner
    assert backups.verify_backup(backup)["owners"] == [owner]


def test_backup_refuses_overwrite_nested_output_and_existing_restore(tmp_path):
    source, _ = populated(tmp_path / "live")
    existing = tmp_path / "existing.otai"
    existing.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        backups.create_backup(source, existing)
    assert existing.read_bytes() == b"keep"
    with pytest.raises(ValueError, match="outside"):
        backups.create_backup(source, source / "backup.otai")

    backup = tmp_path / "valid.otai"
    backups.create_backup(source, backup)
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        backups.restore_backup(backup, destination)


def test_restore_preserves_destination_created_during_publication(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "valid.otai"
    backups.create_backup(source, backup)
    destination = tmp_path / "destination"
    publish = backups._publish_path_exclusive

    def competing_publish(staging, target):
        target.mkdir()
        publish(staging, target)

    monkeypatch.setattr(backups, "_publish_path_exclusive", competing_publish)
    with pytest.raises(FileExistsError, match="already exists"):
        backups.restore_backup(backup, destination)

    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".destination.restore-*"))


def test_restore_ignores_and_preserves_stale_pid_staging_directory(tmp_path):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "valid.otai"
    backups.create_backup(source, backup)
    destination = tmp_path / "destination"
    stale = tmp_path / f".destination.restore-{os.getpid()}"
    stale.mkdir()
    marker = stale / "keep.txt"
    marker.write_text("stale private staging directory")

    backups.restore_backup(backup, destination)

    assert (destination / backups.DATABASE_NAME).is_file()
    assert marker.read_text() == "stale private staging directory"
    assert list(tmp_path.glob(".destination.restore-*")) == [stale]


def test_restore_flushes_staging_tree_before_publication(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "valid.otai"
    backups.create_backup(source, backup)
    destination = tmp_path / "destination"
    real_tree_sync = backups._fsync_tree
    real_directory_sync = backups._fsync_directory
    events = []

    def tracked_tree_sync(path):
        assert path.name.startswith(".destination.restore-")
        assert not destination.exists()
        events.append("tree")
        real_tree_sync(path)

    def tracked_directory_sync(path):
        if path == destination.parent:
            assert destination.exists()
            events.append("parent")
        real_directory_sync(path)

    monkeypatch.setattr(backups, "_fsync_tree", tracked_tree_sync)
    monkeypatch.setattr(backups, "_fsync_directory", tracked_directory_sync)
    backups.restore_backup(backup, destination)

    assert events == ["tree", "parent"]


def test_restore_parent_sync_failure_preserves_published_destination(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "valid.otai"
    backups.create_backup(source, backup)
    destination = tmp_path / "destination"
    real_directory_sync = backups._fsync_directory

    def failing_sync(path):
        if path == destination.parent:
            raise OSError("simulated parent sync failure")
        real_directory_sync(path)

    monkeypatch.setattr(backups, "_fsync_directory", failing_sync)
    with pytest.raises(OSError, match="parent sync failure"):
        backups.restore_backup(backup, destination)

    assert destination.is_dir()
    assert (destination / backups.DATABASE_NAME).is_file()
    assert not list(tmp_path.glob(".destination.restore-*"))


def test_backup_rejects_session_owner_missing_from_archive(tmp_path):
    source, _ = populated(tmp_path / "live")
    with sqlite3.connect(source / "sessions.sqlite3") as connection:
        connection.execute("UPDATE sessions SET user_id = ?", ("s_" + "b" * 32,))

    backup = tmp_path / "inconsistent.otai"
    with pytest.raises(ValueError, match="study data missing from the backup"):
        backups.create_backup(source, backup)
    assert not backup.exists()


def test_verify_and_restore_reject_session_owner_missing_from_archive(tmp_path):
    source, _ = populated(tmp_path / "live")
    valid = tmp_path / "valid.otai"
    backups.create_backup(source, valid)
    with zipfile.ZipFile(valid) as archive:
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}

    database = tmp_path / "sessions.sqlite3"
    database.write_bytes(files[backups.DATABASE_NAME])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET user_id = ?", ("s_" + "b" * 32,))
    files[backups.DATABASE_NAME] = database.read_bytes()
    manifest = json.loads(files[backups.MANIFEST_NAME])
    database_entry = next(
        item for item in manifest["files"] if item["path"] == backups.DATABASE_NAME
    )
    database_entry["size"] = len(files[backups.DATABASE_NAME])
    database_entry["sha256"] = __import__("hashlib").sha256(
        files[backups.DATABASE_NAME]
    ).hexdigest()
    files[backups.MANIFEST_NAME] = json.dumps(manifest).encode()

    inconsistent = tmp_path / "inconsistent.otai"
    with zipfile.ZipFile(inconsistent, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)

    with pytest.raises(ValueError, match="study data missing from the backup"):
        backups.verify_backup(inconsistent)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="study data missing from the backup"):
        backups.restore_backup(inconsistent, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("token_hash", "not-a-token-hash"),
        ("expires_at", "not-an-integer"),
    ],
)
def test_backup_rejects_invalid_session_records(tmp_path, column, value):
    source, _ = populated(tmp_path / "live")
    with sqlite3.connect(source / "sessions.sqlite3") as connection:
        connection.execute(f"UPDATE sessions SET {column} = ?", (value,))

    backup = tmp_path / "invalid-session.otai"
    with pytest.raises(ValueError, match="invalid record"):
        backups.create_backup(source, backup)
    assert not backup.exists()


def test_verify_and_restore_reject_invalid_session_schema(tmp_path):
    source, _ = populated(tmp_path / "live")
    valid = tmp_path / "valid.otai"
    backups.create_backup(source, valid)
    with zipfile.ZipFile(valid) as archive:
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}

    database = tmp_path / "sessions.sqlite3"
    database.write_bytes(files[backups.DATABASE_NAME])
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE sessions RENAME TO unexpected_sessions")
    files[backups.DATABASE_NAME] = database.read_bytes()
    manifest = json.loads(files[backups.MANIFEST_NAME])
    database_entry = next(
        item for item in manifest["files"] if item["path"] == backups.DATABASE_NAME
    )
    database_entry["size"] = len(files[backups.DATABASE_NAME])
    database_entry["sha256"] = __import__("hashlib").sha256(
        files[backups.DATABASE_NAME]
    ).hexdigest()
    files[backups.MANIFEST_NAME] = json.dumps(manifest).encode()

    invalid = tmp_path / "invalid-schema.otai"
    with zipfile.ZipFile(invalid, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)

    with pytest.raises(ValueError, match="schema is invalid"):
        backups.verify_backup(invalid)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="schema is invalid"):
        backups.restore_backup(invalid, destination)
    assert not destination.exists()


def rewrite_archive(source: Path, destination: Path, mutator):
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as changed:
        for info in original.infolist():
            data = original.read(info.filename)
            name, data = mutator(info.filename, data)
            changed.writestr(name, data)


def test_invalid_manifest_is_rejected_before_any_recovery_file_is_written(tmp_path):
    source, _ = populated(tmp_path / "live")
    valid = tmp_path / "valid.otai"
    backups.create_backup(source, valid)
    with zipfile.ZipFile(valid) as archive:
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}

    manifest = json.loads(files[backups.MANIFEST_NAME])
    manifest["files"].append(
        {"path": "not-allowed", "size": 0, "sha256": "0" * 64}
    )
    files[backups.MANIFEST_NAME] = json.dumps(manifest).encode()
    malformed = tmp_path / "malformed.otai"
    with zipfile.ZipFile(malformed, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)

    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(ValueError, match="manifest is invalid"):
        backups._extract_verified(
            malformed, staging, backups.DEFAULT_MAX_UNCOMPRESSED_BYTES
        )
    assert list(staging.rglob("*")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update({"unexpected": "field"}),
        lambda manifest: manifest.update({"format": True}),
        lambda manifest: manifest.update({"created_at": "not-a-timestamp"}),
        lambda manifest: manifest["files"][0].update({"unexpected": "field"}),
        lambda manifest: manifest["files"][0].update({"size": True}),
    ],
    ids=["top-level-field", "boolean-format", "timestamp", "file-field", "boolean-size"],
)
def test_backup_manifest_schema_is_strict_before_extraction(tmp_path, mutate):
    source, _ = populated(tmp_path / "live")
    valid = tmp_path / "valid.otai"
    backups.create_backup(source, valid)
    with zipfile.ZipFile(valid) as archive:
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}

    manifest = json.loads(files[backups.MANIFEST_NAME])
    mutate(manifest)
    files[backups.MANIFEST_NAME] = json.dumps(manifest).encode()
    malformed = tmp_path / "malformed.otai"
    with zipfile.ZipFile(malformed, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)

    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(ValueError, match="manifest is invalid"):
        backups._extract_verified(
            malformed, staging, backups.DEFAULT_MAX_UNCOMPRESSED_BYTES
        )
    assert list(staging.rglob("*")) == []


def test_checksum_failure_is_rejected_before_recovery_staging_is_written(tmp_path):
    source, _ = populated(tmp_path / "live")
    valid = tmp_path / "valid.otai"
    backups.create_backup(source, valid)

    corrupted = tmp_path / "corrupted.otai"

    def corrupt_database(name, data):
        if name != backups.DATABASE_NAME:
            return name, data
        changed = bytearray(data)
        changed[-1] ^= 0x01
        return name, bytes(changed)

    rewrite_archive(valid, corrupted, corrupt_database)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ValueError, match="checksum failed"):
        backups._extract_verified(
            corrupted, staging, backups.DEFAULT_MAX_UNCOMPRESSED_BYTES
        )
    assert list(staging.rglob("*")) == []


def test_unsupported_compression_is_rejected_before_recovery_staging(tmp_path):
    source, _ = populated(tmp_path / "live")
    valid = tmp_path / "valid.otai"
    backups.create_backup(source, valid)
    with zipfile.ZipFile(valid) as archive:
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}

    unsupported = tmp_path / "unsupported.otai"
    with zipfile.ZipFile(unsupported, "w", compression=zipfile.ZIP_BZIP2) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ValueError, match="unsupported compression method"):
        backups._extract_verified(
            unsupported, staging, backups.DEFAULT_MAX_UNCOMPRESSED_BYTES
        )
    assert list(staging.rglob("*")) == []


def test_encrypted_member_flag_is_rejected_during_structural_preflight():
    encrypted = zipfile.ZipInfo(backups.MANIFEST_NAME)
    encrypted.flag_bits |= 0x1
    archive = SimpleNamespace(infolist=lambda: [encrypted])

    with pytest.raises(ValueError, match="encrypted files"):
        backups._safe_members(archive, backups.DEFAULT_MAX_UNCOMPRESSED_BYTES)


def test_high_compression_ratio_is_rejected_before_recovery_staging(tmp_path):
    compressed = tmp_path / "compressed.otai"
    with zipfile.ZipFile(
        compressed, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.writestr(backups.MANIFEST_NAME, b"A" * 200_000)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ValueError, match="unsafe compression ratio"):
        backups._extract_verified(
            compressed, staging, backups.DEFAULT_MAX_UNCOMPRESSED_BYTES
        )
    assert list(staging.rglob("*")) == []


def test_verify_rejects_checksum_failure_extra_and_traversal(tmp_path):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "backup.otai"
    backups.create_backup(source, backup)

    corrupt = tmp_path / "corrupt.otai"
    rewrite_archive(
        backup,
        corrupt,
        lambda name, data: (name, data + b"changed")
        if name.endswith("study.snapshot")
        else (name, data),
    )
    with pytest.raises(ValueError, match="checksum"):
        backups.verify_backup(corrupt)

    extra = tmp_path / "extra.otai"
    rewrite_archive(backup, extra, lambda name, data: (name, data))
    with zipfile.ZipFile(extra, "a") as archive:
        archive.writestr("unexpected", b"data")
    with pytest.raises(ValueError, match="do not match"):
        backups.verify_backup(extra)

    traversal = tmp_path / "traversal.otai"
    rewrite_archive(
        backup,
        traversal,
        lambda name, data: ("../escape", data) if name == "sessions.sqlite3" else (name, data),
    )
    with pytest.raises(ValueError, match="unsafe"):
        backups.verify_backup(traversal)

    case_collision = tmp_path / "case-collision.otai"
    rewrite_archive(backup, case_collision, lambda name, data: (name, data))
    with zipfile.ZipFile(case_collision, "a") as archive:
        archive.writestr("SESSIONS.SQLITE3", b"duplicate on Windows")
    with pytest.raises(ValueError, match="unsafe or duplicate"):
        backups.verify_backup(case_collision)


def test_backup_rejects_unicode_normalization_collisions(tmp_path):
    source, _ = populated(tmp_path / "live")
    owner = "s_" + "a" * 32
    uploads = source / owner / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.pdf").write_bytes(b"%PDF composed")
    (uploads / "cafe\N{COMBINING ACUTE ACCENT}.pdf").write_bytes(b"%PDF decomposed")

    with pytest.raises(ValueError, match="Legacy PDF names conflict"):
        backups.create_backup(source, tmp_path / "colliding-source.otai")

    clean_source, _ = populated(tmp_path / "clean")
    backup = tmp_path / "backup.otai"
    backups.create_backup(clean_source, backup)
    collision = tmp_path / "unicode-collision.otai"
    rewrite_archive(backup, collision, lambda name, data: (name, data))
    with zipfile.ZipFile(collision, "a") as archive:
        archive.writestr(f"{owner}/uploads/caf\N{LATIN SMALL LETTER E WITH ACUTE}.pdf", b"one")
        archive.writestr(f"{owner}/uploads/cafe\N{COMBINING ACUTE ACCENT}.pdf", b"two")
    with pytest.raises(ValueError, match="unsafe or duplicate"):
        backups.verify_backup(collision)


def test_backup_rejects_source_replacement_during_copy(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    owner = "s_" + "a" * 32
    target = source / owner / "data" / "study.snapshot"
    replacement = tmp_path / "replacement.snapshot"
    replacement.write_bytes(b"different regular file")
    real_open = backups.os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, **kwargs):
        nonlocal replaced
        if (
            not replaced
            and Path(path) == target
            and flags & os.O_ACCMODE == os.O_RDONLY
        ):
            replaced = True
            os.replace(replacement, target)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(backups.os, "open", replacing_open)
    backup = tmp_path / "raced.otai"
    with pytest.raises(ValueError, match="Unsafe study data path"):
        backups.create_backup(source, backup)
    assert replaced is True
    assert not backup.exists()


def test_backup_rejects_session_database_replacement_after_open(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    target = source / backups.DATABASE_NAME
    replacement = tmp_path / "replacement.sqlite3"
    with sqlite3.connect(replacement) as connection:
        connection.execute("CREATE TABLE replacement (value TEXT)")

    real_connect = backups.sqlite3.connect
    replaced = False

    def replacing_connect(database, *args, **kwargs):
        nonlocal replaced
        connection = real_connect(database, *args, **kwargs)
        if not replaced and kwargs.get("uri") is True:
            replaced = True
            os.replace(replacement, target)
        return connection

    monkeypatch.setattr(backups.sqlite3, "connect", replacing_connect)
    backup = tmp_path / "raced-database.otai"
    with pytest.raises(ValueError, match="Missing or unsafe sessions.sqlite3"):
        backups.create_backup(source, backup)
    assert replaced is True
    assert not backup.exists()


def test_backup_rejects_session_changes_while_copying_owners(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    real_copy = backups._copy_owner_files
    changed = False

    def changing_copy(root, staging, owner):
        nonlocal changed
        real_copy(root, staging, owner)
        if not changed:
            changed = True
            sessions.create(root)

    monkeypatch.setattr(backups, "_copy_owner_files", changing_copy)
    backup = tmp_path / "mixed-session-state.otai"
    with pytest.raises(ValueError, match="Session state changed"):
        backups.create_backup(source, backup)
    assert changed is True
    assert not backup.exists()


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires Windows privileges")
def test_backup_rejects_symlinked_session_database(tmp_path):
    source, _ = populated(tmp_path / "live")
    target = source / backups.DATABASE_NAME
    moved = tmp_path / backups.DATABASE_NAME
    target.rename(moved)
    target.symlink_to(moved)

    backup = tmp_path / "symlinked-database.otai"
    with pytest.raises(ValueError, match="Missing or unsafe sessions.sqlite3"):
        backups.create_backup(source, backup)
    assert not backup.exists()


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires Windows privileges")
@pytest.mark.parametrize("directory", ["data", "uploads"])
def test_backup_rejects_symlinked_owner_subdirectories(tmp_path, directory):
    source, _ = populated(tmp_path / "live")
    owner = "s_" + "a" * 32
    target = source / owner / directory
    target.mkdir(exist_ok=True)
    moved = tmp_path / f"real-{directory}"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)

    backup = tmp_path / f"symlinked-{directory}.otai"
    with pytest.raises(ValueError, match="Unsafe .* directory"):
        backups.create_backup(source, backup)
    assert not backup.exists()


def test_verify_rejects_invalid_snapshot_even_with_matching_hash(tmp_path):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "backup.otai"
    backups.create_backup(source, backup)
    invalid = tmp_path / "invalid.otai"

    with zipfile.ZipFile(backup) as original:
        files = {info.filename: original.read(info.filename) for info in original.infolist()}
    snapshot_path = next(name for name in files if name.endswith("study.snapshot"))
    files[snapshot_path] = b"not a snapshot"
    manifest = json.loads(files["manifest.json"])
    for item in manifest["files"]:
        if item["path"] == snapshot_path:
            item["size"] = len(files[snapshot_path])
            item["sha256"] = __import__("hashlib").sha256(files[snapshot_path]).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(invalid, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="invalid for owner"):
        backups.verify_backup(invalid)


def test_restore_verifies_and_streams_from_one_open_archive(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "backup.otai"
    backups.create_backup(source, backup)
    real_open = backups._open_backup
    opens = 0

    def tracked_open(path):
        nonlocal opens
        opens += 1
        return real_open(path)

    monkeypatch.setattr(backups, "_open_backup", tracked_open)
    restored = tmp_path / "restored"
    backups.restore_backup(backup, restored)
    assert opens == 1
    assert (restored / "sessions.sqlite3").is_file()


def test_verify_rejects_archive_replacement_during_open(tmp_path, monkeypatch):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "backup.otai"
    backups.create_backup(source, backup)
    replacement = tmp_path / "replacement.otai"
    replacement.write_bytes(backup.read_bytes())
    real_open = backups.os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == backup and flags & os.O_ACCMODE == os.O_RDONLY:
            replaced = True
            os.replace(replacement, backup)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(backups.os, "open", replacing_open)
    with pytest.raises(ValueError, match="unsafe backup archive"):
        backups.verify_backup(backup)
    assert replaced is True


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires Windows privileges")
def test_verify_and_restore_reject_symlinked_archive(tmp_path):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "backup.otai"
    backups.create_backup(source, backup)
    link = tmp_path / "backup-link.otai"
    link.symlink_to(backup)

    with pytest.raises(ValueError, match="unsafe backup archive"):
        backups.verify_backup(link)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="unsafe backup archive"):
        backups.restore_backup(link, destination)
    assert not destination.exists()


def test_verify_and_restore_enforce_uncompressed_size_limit(tmp_path):
    source, _ = populated(tmp_path / "live")
    backup = tmp_path / "backup.otai"
    backups.create_backup(source, backup)
    with zipfile.ZipFile(backup) as archive:
        expanded_size = sum(info.file_size for info in archive.infolist())

    with pytest.raises(ValueError, match="uncompressed size limit"):
        backups.verify_backup(backup, expanded_size - 1)

    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="uncompressed size limit"):
        backups.restore_backup(backup, destination, expanded_size - 1)
    assert not destination.exists()

    with pytest.raises(ValueError, match="must be positive"):
        backups.verify_backup(backup, 0)


def test_verify_rejects_oversized_manifest_before_reading_it(tmp_path, monkeypatch):
    backup = tmp_path / "oversized-manifest.otai"
    monkeypatch.setattr(backups, "MAX_MANIFEST_BYTES", 8)
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("manifest.json", b"{}" * 5)

    with pytest.raises(ValueError, match="manifest is too large"):
        backups.verify_backup(backup)

    monkeypatch.setattr(backups, "MAX_BACKUP_MEMBERS", 0)
    with pytest.raises(ValueError, match="too many files"):
        backups.verify_backup(backup)
