"""Backup database handles must not depend on garbage collection for cleanup."""

import sqlite3

import pytest

from backend import backups, sessions


@pytest.fixture
def retained_connections(monkeypatch):
    real_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(backups.sqlite3, "connect", connect)
    try:
        yield connections
    finally:
        for connection in connections:
            connection.close()


def assert_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("failure", [None, "copy", "destination-open"])
def test_database_copy_closes_connections(tmp_path, retained_connections, failure):
    root = tmp_path / "live"
    sessions.create(root)
    source = root / backups.DATABASE_NAME
    destination = tmp_path / "copied.sqlite3"
    if failure == "copy":
        source.write_bytes(b"not a SQLite database")
    elif failure == "destination-open":
        destination.mkdir()

    if failure:
        with pytest.raises(sqlite3.DatabaseError):
            backups._copy_database(source, destination)
    else:
        backups._copy_database(source, destination)
        assert backups._session_records(destination) == backups._session_records(source)

    assert_closed(retained_connections)


@pytest.mark.parametrize("failure", [None, "record", "schema", "integrity"])
def test_session_validation_closes_connections(tmp_path, retained_connections, failure):
    root = tmp_path / "live"
    token, owner, expiry = sessions.create(root)
    if failure in {"record", "schema"}:
        with sessions.database(root) as connection:
            if failure == "record":
                connection.execute("UPDATE sessions SET user_id = 'invalid-owner'")
            else:
                connection.execute("DROP TABLE sessions")
    database = root / backups.DATABASE_NAME
    if failure == "integrity":
        database.write_bytes(b"not a SQLite database")

    if failure:
        with pytest.raises(ValueError, match="Session database"):
            backups._session_records(database)
    else:
        assert backups._session_records(database) == {(sessions.token_hash(token), owner, expiry)}

    assert_closed(retained_connections)


def test_recovery_closes_databases_before_publication(tmp_path, monkeypatch, retained_connections):
    root = tmp_path / "live"
    token, owner, expiry = sessions.create(root)
    (root / owner).mkdir()
    publish = backups._publish_path_exclusive
    publications = []

    def publish_after_closing(staging, destination):
        assert_closed(retained_connections)
        publications.append(destination)
        publish(staging, destination)

    monkeypatch.setattr(backups, "_publish_path_exclusive", publish_after_closing)
    archive = tmp_path / "backup.otai"
    destination = tmp_path / "restored"
    manifest = backups.create_backup(root, archive)
    assert backups.verify_backup(archive) == manifest
    assert backups.restore_backup(archive, destination) == manifest
    assert sessions.lookup(destination, token) == (owner, expiry)
    assert publications == [archive, destination]
    assert_closed(retained_connections)
