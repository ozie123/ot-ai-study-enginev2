"""Create, verify, and safely restore portable local data backups.

Backups contain private study material and hashed session credentials. Keep them
encrypted and access-controlled outside this application.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

try:
    from .core import NoteStore
    from .storage_lock import storage_lock
except ImportError:  # Supports direct script execution.
    from core import NoteStore  # type: ignore[no-redef]
    from storage_lock import storage_lock  # type: ignore[no-redef]

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "sessions.sqlite3"
OWNER_PATTERN = re.compile(r"^s_[0-9a-f]{32}$")
TOKEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DATA_FILES = {"study.snapshot", "faiss.index", "chunks.json", "stats.json", "history.jsonl"}
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BACKUP_MEMBERS = 100_000
MAX_BACKUP_COMPRESSION_RATIO = 100
DEFAULT_MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024
SUPPORTED_COMPRESSION_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class FileEntry(TypedDict):
    path: str
    size: int
    sha256: str


class BackupManifest(TypedDict):
    format: int
    created_at: str
    owners: list[str]
    files: list[FileEntry]


SessionRecord = tuple[str, str, int]


def _fsync_file(path: Path) -> None:
    """Flush one completed regular file before it can be published."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """Flush directory metadata where the platform exposes that primitive."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Flush restored files and directories from leaves to the staging root."""
    paths = list(root.rglob("*"))
    for path in paths:
        if path.is_file():
            _fsync_file(path)
    directories = [path for path in paths if path.is_dir()]
    directories.sort(key=lambda path: len(path.parts), reverse=True)
    for path in directories:
        _fsync_directory(path)
    _fsync_directory(root)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_name(value: str) -> str:
    """Canonicalize names for case- and normalization-insensitive filesystems."""
    return unicodedata.normalize("NFC", value.casefold())


def _database_identity(source: Path) -> tuple[int, int]:
    """Return a regular database file's stable filesystem identity."""
    try:
        inspected = source.lstat()
    except OSError as error:
        raise ValueError(f"Missing or unsafe {DATABASE_NAME}") from error
    if not stat.S_ISREG(inspected.st_mode):
        raise ValueError(f"Missing or unsafe {DATABASE_NAME}")
    return inspected.st_dev, inspected.st_ino


def _copy_database(source: Path, destination: Path) -> None:
    inspected_identity = _database_identity(source)
    source_uri = f"{source.as_uri()}?mode=ro"
    # SQLite's transaction context does not close its connection. Release both
    # handles before staging cleanup/publication, including failed opens/copies.
    with (
        closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as live,
        closing(sqlite3.connect(destination)) as backup,
        live,
        backup,
    ):
        if _database_identity(source) != inspected_identity:
            raise ValueError(f"Missing or unsafe {DATABASE_NAME}")
        live.backup(backup)
        if _database_identity(source) != inspected_identity:
            raise ValueError(f"Missing or unsafe {DATABASE_NAME}")
        result = backup.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise ValueError("Session database integrity check failed")


def _session_records(database_path: Path) -> set[SessionRecord]:
    """Read and validate the recoverable authentication state in one snapshot."""
    try:
        with closing(
            sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("Session database integrity check failed")
            records: set[SessionRecord] = set()
            for token_hash, owner, expires_at in connection.execute(
                "SELECT token_hash, user_id, expires_at FROM sessions"
            ):
                if (
                    not isinstance(token_hash, str)
                    or not TOKEN_HASH_PATTERN.fullmatch(token_hash)
                    or not isinstance(owner, str)
                    or not OWNER_PATTERN.fullmatch(owner)
                    or not isinstance(expires_at, int)
                    or isinstance(expires_at, bool)
                    or expires_at < 0
                ):
                    raise ValueError("Session database contains an invalid record")
                records.add((token_hash, owner, expires_at))
    except sqlite3.DatabaseError as error:
        raise ValueError("Session database schema is invalid") from error
    return records


def _copy_regular_file(source: Path, destination: Path, unsafe_message: str) -> None:
    """Copy the exact regular file inspected, rejecting source replacement races."""
    try:
        inspected = source.lstat()
        if not stat.S_ISREG(inspected.st_mode):
            raise ValueError(unsafe_message)
        source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except (FileNotFoundError, OSError) as error:
        raise ValueError(unsafe_message) from error

    try:
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (inspected.st_dev, inspected.st_ino)
        ):
            raise ValueError(unsafe_message)
        destination_descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with (
            os.fdopen(source_descriptor, "rb") as input_file,
            os.fdopen(destination_descriptor, "wb") as output_file,
        ):
            source_descriptor = -1
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)


def _copy_owner_files(root: Path, staging: Path, owner: str) -> None:
    owner_dir = root / owner
    if owner_dir.is_symlink() or not owner_dir.is_dir():
        raise ValueError(f"Unsafe owner directory: {owner}")
    with storage_lock(owner_dir / "storage.lock"):
        if owner_dir.is_symlink() or not owner_dir.is_dir():
            raise ValueError(f"Unsafe owner directory: {owner}")
        data_dir = owner_dir / "data"
        if data_dir.is_symlink() or (data_dir.exists() and not data_dir.is_dir()):
            raise ValueError(f"Unsafe study data directory for owner {owner}")
        for name in sorted(DATA_FILES):
            source = data_dir / name
            if source.exists() or source.is_symlink():
                destination = staging / owner / "data" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_regular_file(
                    source,
                    destination,
                    f"Unsafe study data path: {owner}/data/{name}",
                )
        uploads_dir = owner_dir / "uploads"
        if uploads_dir.is_symlink() or (uploads_dir.exists() and not uploads_dir.is_dir()):
            raise ValueError(f"Unsafe legacy upload directory for owner {owner}")
        if uploads_dir.is_dir():
            uploads = sorted(
                path
                for path in uploads_dir.iterdir()
                if path.is_file() and path.suffix.casefold() == ".pdf"
            )
            if len({_portable_name(path.name) for path in uploads}) != len(uploads):
                raise ValueError(f"Legacy PDF names conflict for owner {owner}")
            for source in uploads:
                destination = staging / owner / "uploads" / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_regular_file(
                    source, destination, f"Unsafe legacy upload: {source.name}"
                )


def create_backup(
    root: Path,
    output: Path,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> BackupManifest:
    """Create a new verified archive without changing live application data."""
    root = root.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Backup already exists: {output}")
    if not root.is_dir():
        raise ValueError(f"Data directory does not exist: {root}")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("Backup output must be outside the data directory")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary_output = Path(temporary_name)
    try:
        with tempfile.TemporaryDirectory(prefix="ot-ai-backup-") as temporary:
            staging = Path(temporary)
            _copy_database(root / DATABASE_NAME, staging / DATABASE_NAME)
            initial_sessions = _session_records(staging / DATABASE_NAME)
            owners = sorted(
                path.name
                for path in root.iterdir()
                if path.is_dir() and not path.is_symlink() and OWNER_PATTERN.fullmatch(path.name)
            )
            for owner in owners:
                _copy_owner_files(root, staging, owner)
            final_database = staging / ".sessions-after.sqlite3"
            _copy_database(root / DATABASE_NAME, final_database)
            try:
                if _session_records(final_database) != initial_sessions:
                    raise ValueError("Session state changed during backup creation")
            finally:
                final_database.unlink(missing_ok=True)

            files: list[FileEntry] = []
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(staging).as_posix()
                    files.append(
                        {"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)}
                    )
            manifest: BackupManifest = {
                "format": FORMAT_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "owners": owners,
                "files": files,
            }
            descriptor = temporary_descriptor
            temporary_descriptor = -1
            with (
                os.fdopen(descriptor, "w+b") as output_file,
                zipfile.ZipFile(
                    output_file, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                ) as archive,
            ):
                archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
                for item in files:
                    archive.write(staging / str(item["path"]), str(item["path"]))
            _fsync_file(temporary_output)
            verify_backup(temporary_output, max_uncompressed_bytes)
            # Both paths share a filesystem. A native no-replace rename publishes
            # atomically and fails if another writer claimed the destination.
            _publish_path_exclusive(temporary_output, output)
            _fsync_directory(output.parent)
        return manifest
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        temporary_output.unlink(missing_ok=True)


def _safe_members(
    archive: zipfile.ZipFile, max_uncompressed_bytes: int
) -> dict[str, zipfile.ZipInfo]:
    if max_uncompressed_bytes <= 0:
        raise ValueError("Backup expansion limit must be positive")
    members: dict[str, zipfile.ZipInfo] = {}
    canonical_paths: set[str] = set()
    infos = archive.infolist()
    if len(infos) > MAX_BACKUP_MEMBERS:
        raise ValueError("Backup contains too many files")
    total_size = 0
    for info in infos:
        path = Path(info.filename)
        canonical = _portable_name(info.filename)
        if info.flag_bits & 0x1:
            raise ValueError("Backup contains encrypted files")
        if info.compress_type not in SUPPORTED_COMPRESSION_METHODS:
            raise ValueError("Backup uses an unsupported compression method")
        if (
            info.file_size > 0
            and info.file_size
            > info.compress_size * MAX_BACKUP_COMPRESSION_RATIO
        ):
            raise ValueError("Backup contains an unsafe compression ratio")
        if (
            info.is_dir()
            or info.filename in members
            or canonical in canonical_paths
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != info.filename
        ):
            raise ValueError("Backup contains an unsafe or duplicate path")
        total_size += info.file_size
        if total_size > max_uncompressed_bytes:
            raise ValueError("Backup exceeds the uncompressed size limit")
        members[info.filename] = info
        canonical_paths.add(canonical)
    manifest = members.get(MANIFEST_NAME)
    if manifest is not None and manifest.file_size > MAX_MANIFEST_BYTES:
        raise ValueError("Backup manifest is too large")
    return members


def _read_manifest(
    archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]
) -> BackupManifest:
    if MANIFEST_NAME not in members:
        raise ValueError("Backup manifest is missing")
    try:
        manifest = json.loads(archive.read(MANIFEST_NAME))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Backup manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise ValueError("Unsupported backup format")
    format_value = manifest.get("format")
    if not isinstance(format_value, int) or isinstance(format_value, bool):
        raise ValueError("Backup manifest is invalid")
    if format_value != FORMAT_VERSION:
        raise ValueError("Unsupported backup format")
    if set(manifest) != {"format", "created_at", "owners", "files"}:
        raise ValueError("Backup manifest is invalid")
    created_at = manifest.get("created_at")
    try:
        parsed_created_at = (
            datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
        )
    except ValueError as error:
        raise ValueError("Backup manifest is invalid") from error
    owners = manifest.get("owners")
    files = manifest.get("files")
    if (
        parsed_created_at is None
        or parsed_created_at.tzinfo is None
        or parsed_created_at.utcoffset() is None
        or not isinstance(owners, list)
        or not all(isinstance(owner, str) and OWNER_PATTERN.fullmatch(owner) for owner in owners)
        or owners != sorted(set(owners))
        or not isinstance(files, list)
    ):
        raise ValueError("Backup manifest is invalid")
    return cast(BackupManifest, manifest)


def _allowed_path(path: str, owners: set[str]) -> bool:
    if path == DATABASE_NAME:
        return True
    parts = Path(path).parts
    if len(parts) == 3 and parts[0] in owners and parts[1] == "data":
        return parts[2] in DATA_FILES
    return (
        len(parts) == 3
        and parts[0] in owners
        and parts[1] == "uploads"
        and parts[2].lower().endswith(".pdf")
    )


def _member_sha256(archive: zipfile.ZipFile, path: str) -> str:
    """Hash one member without writing untrusted bytes to recovery staging."""
    digest = hashlib.sha256()
    with archive.open(path) as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_staged_data(staging: Path, manifest: BackupManifest) -> None:
    database_path = staging / DATABASE_NAME
    session_owners = {record[1] for record in _session_records(database_path)}
    missing_owners = session_owners - set(manifest["owners"])
    if missing_owners:
        raise ValueError("Session database references study data missing from the backup")

    for owner in manifest["owners"]:
        data_dir = staging / owner / "data"
        store = NoteStore(data_dir)
        if any((data_dir / name).exists() for name in ("study.snapshot", "faiss.index", "chunks.json")):
            try:
                if not store.load():
                    raise ValueError("Study data is missing")
            except (OSError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile) as error:
                raise ValueError(f"Study data is invalid for owner {owner}") from error


@contextmanager
def _open_backup(path: Path) -> Iterator[zipfile.ZipFile]:
    """Pin one regular archive by descriptor and reject path substitution."""
    unsafe_message = "Missing or unsafe backup archive"
    try:
        inspected = path.lstat()
        if not stat.S_ISREG(inspected.st_mode):
            raise ValueError(unsafe_message)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except (FileNotFoundError, OSError) as error:
        raise ValueError(unsafe_message) from error

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (inspected.st_dev, inspected.st_ino)
        ):
            raise ValueError(unsafe_message)
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            with zipfile.ZipFile(source) as archive:
                yield archive
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _extract_verified(
    backup: Path, staging: Path, max_uncompressed_bytes: int
) -> BackupManifest:
    """Stream one archive into staging while verifying the exact extracted bytes."""
    with _open_backup(backup) as archive:
        members = _safe_members(archive, max_uncompressed_bytes)
        manifest = _read_manifest(archive, members)
        listed: set[str] = set()
        owners = set(manifest["owners"])
        validated_files: list[FileEntry] = []
        for item in manifest["files"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "size", "sha256"}
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("size"), int)
                or isinstance(item.get("size"), bool)
                or item["size"] < 0
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or item["path"] in listed
                or item["path"] not in members
                or not _allowed_path(item["path"], owners)
            ):
                raise ValueError("Backup file manifest is invalid")
            if members[item["path"]].file_size != item["size"]:
                raise ValueError(f"Backup checksum failed: {item['path']}")
            listed.add(item["path"])
            validated_files.append(cast(FileEntry, item))
        if set(members) != listed | {MANIFEST_NAME} or DATABASE_NAME not in listed:
            raise ValueError("Backup contents do not match the manifest")

        # Do not write even temporary recovery data until every manifest entry,
        # the exact member set, and every descriptor-pinned byte stream pass.
        for item in validated_files:
            if _member_sha256(archive, item["path"]) != item["sha256"]:
                raise ValueError(f"Backup checksum failed: {item['path']}")

        # Recheck hashes while writing: another process could mutate the opened
        # regular archive in place between the preflight and extraction passes.
        for item in validated_files:
            destination = staging / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            with archive.open(item["path"]) as source:
                descriptor = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise ValueError(f"Backup checksum failed: {item['path']}")
        _validate_staged_data(staging, manifest)
    return manifest


def verify_backup(
    backup: Path,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> BackupManifest:
    """Validate paths, hashes, SQLite contents, and every stored study index."""
    backup = Path(os.path.abspath(backup))
    with tempfile.TemporaryDirectory(prefix="ot-ai-verify-") as temporary:
        return _extract_verified(backup, Path(temporary), max_uncompressed_bytes)


def _publish_path_exclusive(staging: Path, destination: Path) -> None:
    """Atomically publish a file or directory without replacing a destination."""
    if sys.platform == "win32":
        # Windows rename is already no-replace when the destination exists.
        os.rename(staging, destination)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(staging)
    target = os.fsencode(destination)
    result: int
    if sys.platform.startswith("linux"):
        try:
            rename_no_replace = libc.renameat2
        except AttributeError as error:
            raise OSError(errno.ENOTSUP, "Atomic exclusive publication is unavailable") from error
        rename_no_replace.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename_no_replace(-100, source, -100, target, 1)
    elif sys.platform == "darwin":
        try:
            rename_no_replace = libc.renamex_np
        except AttributeError as error:
            raise OSError(errno.ENOTSUP, "Atomic exclusive publication is unavailable") from error
        rename_no_replace.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename_no_replace(source, target, 4)
    else:
        raise OSError(errno.ENOTSUP, "Atomic exclusive publication is unavailable")

    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, "Destination already exists", destination)
    raise OSError(error_number, os.strerror(error_number), destination)


def restore_backup(
    backup: Path,
    destination: Path,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> BackupManifest:
    """Restore to a new path only; never merge into or overwrite existing data."""
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("Restore destination already exists")
    backup = Path(os.path.abspath(backup))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent)
    )
    try:
        os.chmod(staging, 0o700)
        manifest = _extract_verified(backup, staging, max_uncompressed_bytes)
        os.chmod(staging / DATABASE_NAME, 0o600)
        _fsync_tree(staging)
        _publish_path_exclusive(staging, destination)
        _fsync_directory(destination.parent)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="create and verify a backup")
    create_parser.add_argument("--data-dir", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    create_parser.add_argument(
        "--max-uncompressed-bytes",
        type=int,
        default=DEFAULT_MAX_UNCOMPRESSED_BYTES,
        help="maximum expanded archive size accepted during verification",
    )
    verify_parser = subparsers.add_parser("verify", help="verify a backup without restoring it")
    verify_parser.add_argument("--backup", type=Path, required=True)
    verify_parser.add_argument(
        "--max-uncompressed-bytes",
        type=int,
        default=DEFAULT_MAX_UNCOMPRESSED_BYTES,
        help="maximum expanded archive size accepted during verification",
    )
    restore_parser = subparsers.add_parser("restore", help="restore into a new directory")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    restore_parser.add_argument(
        "--max-uncompressed-bytes",
        type=int,
        default=DEFAULT_MAX_UNCOMPRESSED_BYTES,
        help="maximum expanded archive size accepted during restoration",
    )
    args = parser.parse_args()
    if args.command == "create":
        manifest = create_backup(
            args.data_dir, args.output, args.max_uncompressed_bytes
        )
    elif args.command == "verify":
        manifest = verify_backup(args.backup, args.max_uncompressed_bytes)
    else:
        manifest = restore_backup(
            args.backup, args.destination, args.max_uncompressed_bytes
        )
    print(
        f"Backup {args.command} succeeded: {len(manifest['owners'])} owners, "
        f"{len(manifest['files'])} files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
