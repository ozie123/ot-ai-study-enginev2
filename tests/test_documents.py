import hashlib
import io
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from backend import core, server
from backend.core import NoteStore
from tests.test_sessions import fresh_client, identity
from tests.test_uploads import make_zip, post_zip

KEY = {"Authorization": "Bearer test-key"}


@pytest.fixture(autouse=True)
def synthetic_provider(monkeypatch):
    monkeypatch.setattr(
        NoteStore, "_embed", lambda self, texts: np.array([[1, 0]] * len(texts), dtype="float32")
    )
    monkeypatch.setattr(
        server,
        "read_pdf_pages",
        lambda stream, **kwargs: [(1, stream.read().decode().split("\n", 1)[1])],
    )


def upload(client, files):
    return post_zip(
        client, make_zip({name: b"%PDF-1.4\n" + text.encode() for name, text in files.items()})
    )


def ingest(client):
    return client.post("/ingest_user_pdfs", json={}, headers=KEY)


def loaded(client):
    return server.load_store_for_user(identity(client))


def seed(client):
    assert upload(client, {"week1.pdf": "old " * 500, "week2.pdf": "unrelated"}).status_code == 200
    assert ingest(client).status_code == 200


def fail(*args, **kwargs):
    raise OSError("injected failure")


def test_replacement_removes_every_old_chunk_retains_other_vectors_and_reloads(client):
    seed(client)
    before = loaded(client)
    other = before.index.reconstruct(len(before.chunks) - 1)
    assert upload(client, {"WEEK1.pdf": "replacement"}).status_code == 200
    staged = loaded(client)
    assert staged.documents == before.documents
    assert staged.chunks == before.chunks
    assert ingest(client).json()["files"] == 1
    after = loaded(client)
    assert after.chunks == ["unrelated", "replacement"]
    assert after.tags == ["week2", "week1"]
    np.testing.assert_array_equal(after.index.reconstruct(0), other)
    assert after.pending == {}
    assert after.documents["week1.pdf"].endswith(b"replacement")
    assert all("old " not in hit[2] for hit in after.search("question", k=20))
    # Repeat ingestion and same-content upload are idempotent per filename.
    assert ingest(client).json()["added_chunks"] == 0
    assert upload(client, {"week1.pdf": "replacement"}).status_code == 200
    assert ingest(client).json()["already_ingested"] == 1
    assert loaded(client).chunks == after.chunks


def test_same_bytes_in_distinct_documents_are_independently_deletable(client):
    assert upload(client, {"one.pdf": "same", "two.pdf": "same"}).status_code == 200
    assert ingest(client).json()["files"] == 2
    assert client.delete("/documents/one.pdf").status_code == 200
    store = loaded(client)
    assert store.chunks == ["same"]
    assert store.meta[0]["source"] == "two.pdf"
    assert set(store.documents) == {"two.pdf"}


def test_delete_active_pending_and_last_document_survives_reload(client):
    seed(client)
    assert upload(client, {"week1.pdf": "pending replacement"}).status_code == 200
    assert client.delete("/documents/week1.pdf").status_code == 200
    store = loaded(client)
    assert store.chunks == ["unrelated"]
    assert set(store.documents) == {"week2.pdf"}
    assert store.pending == {}
    assert client.delete("/documents/week2.pdf").status_code == 200
    store = loaded(client)
    assert store.chunks == store.meta == store.tags == []
    assert store.documents == store.pending == {}
    assert store.index.ntotal == 0
    assert store.search("question") == []
    assert not client.get("/status").json()["ready"]
    assert ingest(client).json()["files"] == 0
    assert client.delete("/documents/week2.pdf").status_code == 404
    assert upload(client, {"new.pdf": "new notes"}).status_code == 200
    assert ingest(client).status_code == 200
    assert loaded(client).chunks == ["new notes"]


@pytest.mark.parametrize("failure", ["parse", "empty", "embed", "serialize", "fsync", "replace"])
def test_failed_replacement_preserves_active_file_and_searchable_index(
    client, monkeypatch, failure
):
    seed(client)
    assert upload(client, {"week1.pdf": "new notes", "extra.pdf": "extra"}).status_code == 200
    before = loaded(client)
    original = before.snapshot_path.read_bytes()
    with monkeypatch.context() as patch:
        if failure == "parse":
            patch.setattr(server, "read_pdf_pages", fail)
        elif failure == "empty":
            patch.setattr(server, "read_pdf_pages", lambda *args, **kwargs: [])
        elif failure == "embed":
            patch.setattr(NoteStore, "_embed", fail)
        else:
            module, name = {
                "serialize": (core.faiss, "serialize_index"),
                "fsync": (core.os, "fsync"),
                "replace": (core.os, "replace"),
            }[failure]
            patch.setattr(module, name, fail)
        assert ingest(client).status_code >= 400
    assert before.snapshot_path.read_bytes() == original
    after = loaded(client)
    assert after.documents == before.documents
    assert after.chunks == before.chunks
    assert after.pending == before.pending
    assert after.search("question", tag="week1")[0][2].startswith("old ")
    assert ingest(client).status_code == 200
    assert "new notes" in loaded(client).chunks


@pytest.mark.parametrize("operation", ["upload", "delete"])
def test_failed_mutation_keeps_entire_snapshot(client, monkeypatch, operation):
    seed(client)
    original = loaded(client).snapshot_path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(core.os, "replace", fail)
        response = (
            upload(client, {"week1.pdf": "new", "new.pdf": "new"})
            if operation == "upload"
            else client.delete("/documents/week1.pdf")
        )
        assert response.status_code == 500
    assert loaded(client).snapshot_path.read_bytes() == original


def test_console_failure_after_commit_does_not_report_failed_deletion(client, monkeypatch):
    seed(client)
    monkeypatch.setattr(core, "print", fail, raising=False)
    assert client.delete("/documents/week1.pdf").status_code == 200
    assert loaded(client).chunks == ["unrelated"]


def test_invalid_upload_and_chunk_limit_preserve_previous_data(client, monkeypatch):
    seed(client)
    original = loaded(client).snapshot_path.read_bytes()
    assert post_zip(client, make_zip({"week1.pdf": b"bad"})).status_code == 400
    assert loaded(client).snapshot_path.read_bytes() == original
    assert upload(client, {"week1.pdf": "new " * 1000}).status_code == 200
    original = loaded(client).snapshot_path.read_bytes()
    monkeypatch.setattr(server, "MAX_INGEST_CHUNKS", 1)
    assert ingest(client).status_code == 413
    assert loaded(client).snapshot_path.read_bytes() == original


def test_delete_and_replace_cannot_cross_session_boundary(client):
    seed(client)
    before = loaded(client).snapshot_path.read_bytes()
    with fresh_client() as other:
        assert other.post("/session").status_code == 200
        assert other.delete("/documents/week1.pdf").status_code == 404
        assert (
            other.delete("/documents/week1.pdf", headers={"X-User": identity(client)}).status_code
            == 400
        )
        assert upload(other, {"week1.pdf": "other session"}).status_code == 200
        assert ingest(other).status_code == 200
        assert other.delete("/documents/week1.pdf").status_code == 200
    assert loaded(client).snapshot_path.read_bytes() == before


def test_concurrent_ingest_delete_and_upload_are_serialized(client, monkeypatch):
    seed(client)
    assert upload(client, {"week1.pdf": "replacement"}).status_code == 200
    entered, release = Event(), Event()

    def delayed_embed(self, texts):
        entered.set()
        assert release.wait(10)
        return np.array([[1, 0]] * len(texts), dtype="float32")

    monkeypatch.setattr(NoteStore, "_embed", delayed_embed)
    with ThreadPoolExecutor(max_workers=3) as pool:
        build = pool.submit(ingest, client)
        assert entered.wait(10)
        delete = pool.submit(client.delete, "/documents/week1.pdf")
        new = pool.submit(upload, client, {"third.pdf": "third"})
        # Readers see the complete old snapshot while the writer is working.
        assert loaded(client).chunks[0].startswith("old ")
        release.set()
        assert build.result(timeout=10).status_code == 200
        assert delete.result(timeout=10).status_code == 200
        assert new.result(timeout=10).status_code == 200
    store = loaded(client)
    assert store.chunks == ["unrelated"]
    assert set(store.documents) == {"week2.pdf"}
    assert set(store.pending) == {"third.pdf"}


def test_pending_only_deletion_and_quota_include_rollback_copy(client, monkeypatch):
    assert upload(client, {"one.pdf": "original"}).status_code == 200
    assert ingest(client).status_code == 200
    store = loaded(client)
    monkeypatch.setattr(server, "MAX_USER_STORAGE_BYTES", len(store.documents["one.pdf"]) + 1)
    assert upload(client, {"one.pdf": "replacement"}).status_code == 413
    assert loaded(client).documents == store.documents
    assert client.delete("/documents/one.pdf").status_code == 200
    monkeypatch.setattr(server, "MAX_USER_STORAGE_BYTES", 1000)
    assert upload(client, {"pending.pdf": "pending"}).status_code == 200
    assert client.get("/status").json()["document_files"] == [
        {"name": "pending.pdf", "pending": True, "indexed": False}
    ]
    assert client.delete("/documents/pending.pdf").status_code == 200
    assert loaded(client).pending == {}


def test_partial_embedding_batch_failure_does_not_publish_any_documents(client, monkeypatch):
    seed(client)
    assert upload(client, {"week1.pdf": "replacement", "new.pdf": "new"}).status_code == 200
    original = loaded(client).snapshot_path.read_bytes()
    calls = []

    def embed(self, texts):
        calls.append(texts)
        if len(calls) == 2:
            raise RuntimeError("provider failed on second document")
        return np.array([[0, 1]] * len(texts), dtype="float32")

    monkeypatch.setattr(NoteStore, "_embed", embed)
    assert ingest(client).status_code == 502
    assert len(calls) == 2
    assert loaded(client).snapshot_path.read_bytes() == original


def test_legacy_migration_cleanup_resumes_without_resurrecting_deleted_pdf(client, monkeypatch):
    owner = identity(client)
    paths = server.user_paths(owner)
    pdf = paths["uploads_dir"] / "old.pdf"
    content = b"%PDF-1.4\nlegacy"
    pdf.write_bytes(content)
    store = NoteStore(paths["data_dir"])
    store.add_chunks(
        ["legacy"],
        tag="old",
        meta_list=[{"source": "old.pdf", "document_sha256": hashlib.sha256(content).hexdigest()}],
    )
    store.save()
    unlink = Path.unlink

    def interrupted_cleanup(path, *args, **kwargs):
        if path == pdf:
            raise OSError("interrupted legacy cleanup")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted_cleanup)
        assert client.delete("/documents/old.pdf").status_code == 500
    # Migration already committed; old contents remain logically intact.
    assert loaded(client).documents == {"old.pdf": content}
    assert loaded(client).chunks == ["legacy"]
    assert pdf.exists()
    assert client.delete("/documents/old.pdf").status_code == 200
    assert not pdf.exists()
    assert ingest(client).json()["files"] == 0
    assert loaded(client).documents == {}
    assert loaded(client).chunks == []


def test_legacy_migration_save_failure_preserves_loose_pdf_and_index(client, monkeypatch):
    paths = server.user_paths(identity(client))
    pdf = paths["uploads_dir"] / "old.pdf"
    pdf.write_bytes(b"%PDF-1.4\nlegacy")
    store = NoteStore(paths["data_dir"])
    store.add_chunks(["legacy"], tag="old", meta_list=[{"source": "old.pdf"}])
    store.save()
    original = store.snapshot_path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(core.os, "replace", fail)
        assert client.delete("/documents/old.pdf").status_code == 500
    assert store.snapshot_path.read_bytes() == original
    assert pdf.read_bytes() == b"%PDF-1.4\nlegacy"


def test_abandoned_candidate_is_ignored_then_cleaned_before_delete(client):
    seed(client)
    store = loaded(client)
    abandoned = store.base_path / "snapshot-interrupted.tmp"
    abandoned.write_bytes(store.snapshot_path.read_bytes())
    assert loaded(client).chunks == store.chunks
    assert client.delete("/documents/week1.pdf").status_code == 200
    assert not abandoned.exists()


def _worker_stage(root, owner, started):
    server.USERS_DIR = Path(root)
    started.set()
    server.stage_pdfs(owner, [("child.pdf", b"%PDF-1.4\nchild")])


def test_os_lock_serializes_independent_processes_and_cold_reload(client):
    seed(client)
    owner = identity(client)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    worker = context.Process(target=_worker_stage, args=(str(server.USERS_DIR), owner, started))
    try:
        with server.user_lock(owner):
            worker.start()
            assert started.wait(15)
            store = loaded(client)
            assert store.pending == {}
            store.pending["parent.pdf"] = b"%PDF-1.4\nparent"
            server.save_store_for_user(owner, store)
        worker.join(timeout=20)
        assert worker.exitcode == 0
        # Child read the parent's commit from disk under the OS lock.
        assert set(loaded(client).pending) == {"parent.pdf", "child.pdf"}
        assert loaded(client).chunks == store.chunks
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)


def _worker_interrupted_publish(root, owner, reached, point):
    server.USERS_DIR = Path(root)
    replace = core.os.replace

    def interrupted(source, target):
        if point == "after":
            replace(source, target)
        reached.set()
        # The test terminates this process, simulating a crash holding the lock.
        Event().wait(30)

    core.os.replace = interrupted
    server.stage_pdfs(owner, [("child.pdf", b"%PDF-1.4\nchild")])


@pytest.mark.parametrize("point", ["before", "after"])
def test_worker_crash_has_one_commit_point_and_releases_lock(client, point):
    seed(client)
    context = multiprocessing.get_context("spawn")
    reached = context.Event()
    worker = context.Process(
        target=_worker_interrupted_publish,
        args=(str(server.USERS_DIR), identity(client), reached, point),
    )
    try:
        worker.start()
        assert reached.wait(15)
        worker.terminate()
        worker.join(timeout=5)
        store = loaded(client)
        assert ("child.pdf" in store.pending) == (point == "after")
        assert store.chunks[0].startswith("old ")
        # A dead worker's OS lock must not block this new mutation.
        assert client.delete("/documents/week1.pdf").status_code == 200
        assert loaded(client).chunks == ["unrelated"]
        assert not list(store.base_path.glob("snapshot-*.tmp"))
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)


def test_real_pdf_parser_reads_snapshot_bytes_and_rejects_bad_replacement(client, monkeypatch):
    monkeypatch.setattr(server, "read_pdf_pages", core.read_pdf_pages)
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (Real PDF notes) Tj ET")
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    assert post_zip(client, make_zip({"real.pdf": output.getvalue()})).status_code == 200
    assert ingest(client).status_code == 200
    assert loaded(client).chunks == ["Real PDF notes"]
    assert upload(client, {"real.pdf": "not a real PDF body"}).status_code == 200
    original = loaded(client).snapshot_path.read_bytes()
    assert ingest(client).status_code == 422
    assert loaded(client).snapshot_path.read_bytes() == original
