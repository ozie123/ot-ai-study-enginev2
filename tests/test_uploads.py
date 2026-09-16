from __future__ import annotations

import io
import zipfile

from backend import server


def make_zip(files: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def post_zip(client, content: bytes):
    return client.post(
        "/upload_zip",
        files={"file": ("notes.zip", content, "application/zip")},
    )


def test_valid_pdf_zip_is_saved_inside_user_directory(client, tmp_path):
    response = post_zip(client, make_zip({"nested/week1.pdf": b"%PDF-1.4\nmock"}))
    assert response.status_code == 200
    assert response.json()["uploaded_pdfs"] == 1
    user_id = server.sessions.lookup(server.USERS_DIR, client.cookies.get(server.SESSION_COOKIE_NAME))[0]
    assert (tmp_path / "users" / user_id / "uploads" / "week1.pdf").is_file()


def test_invalid_zip_is_rejected(client):
    response = post_zip(client, b"not-a-zip")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid ZIP file."


def test_corrupt_zip_member_is_rejected_with_controlled_error(client):
    content = bytearray(make_zip({"notes.pdf": b"%PDF-1.4\nmock"}, compression=zipfile.ZIP_STORED))
    marker = content.find(b"%PDF-1.4\nmock")
    assert marker >= 0
    content[marker + len(b"%PDF-1.4\nmock") - 1] ^= 0x01

    response = post_zip(client, bytes(content))

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or corrupt ZIP file."


def test_disguised_non_pdf_is_rejected(client):
    response = post_zip(client, make_zip({"notes.pdf": b"plain text"}))
    assert response.status_code == 400
    assert "not a valid PDF" in response.json()["detail"]


def test_request_size_limit_is_enforced(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 20)
    response = post_zip(client, make_zip({"notes.pdf": b"%PDF-1.4\nmock"}))
    assert response.status_code == 413


def test_pdf_member_size_limit_is_enforced(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_PDF_BYTES", 10)
    response = post_zip(client, make_zip({"notes.pdf": b"%PDF-1.4\nmock"}))
    assert response.status_code == 413


def test_duplicate_flattened_filenames_are_rejected(client):
    response = post_zip(
        client,
        make_zip({"week1/notes.pdf": b"%PDF-1.4\none", "week2/notes.pdf": b"%PDF-1.4\ntwo"}),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "ZIP contains duplicate PDF filenames."


def test_high_compression_ratio_is_rejected(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_PDF_BYTES", 3 * 1024 * 1024)
    monkeypatch.setattr(server, "MAX_TOTAL_UNCOMPRESSED_BYTES", 3 * 1024 * 1024)
    content = b"%PDF-1.4\n" + (b"A" * (2 * 1024 * 1024))
    response = post_zip(client, make_zip({"notes.pdf": content}))
    assert response.status_code == 413
    assert response.json()["detail"] == "ZIP compression ratio is unsafe."


def test_per_session_storage_quota_is_enforced(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_USER_STORAGE_BYTES", 10)
    response = post_zip(client, make_zip({"notes.pdf": b"%PDF-1.4\nmock"}))
    assert response.status_code == 413
    assert response.json()["detail"] == "This study session has reached its document storage limit."
