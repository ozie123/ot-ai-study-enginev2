import os
import stat

import pytest

from backend.storage_lock import storage_lock


def test_storage_lock_uses_owner_only_regular_file(tmp_path):
    lock_path = tmp_path / "storage.lock"

    with storage_lock(lock_path):
        assert lock_path.is_file()
        if os.name != "nt":
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires Windows privileges")
def test_storage_lock_rejects_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"private data")
    lock_path = tmp_path / "storage.lock"
    lock_path.symlink_to(target)

    with pytest.raises(
        ValueError, match="Unsafe study storage lock"
    ), storage_lock(lock_path):
        pytest.fail("unsafe lock unexpectedly acquired")

    assert target.read_bytes() == b"private data"


@pytest.mark.skipif(os.name == "nt", reason="FIFO support is POSIX-specific")
def test_storage_lock_rejects_non_regular_file_without_blocking(tmp_path):
    lock_path = tmp_path / "storage.lock"
    os.mkfifo(lock_path)

    with pytest.raises(
        ValueError, match="Unsafe study storage lock"
    ), storage_lock(lock_path):
        pytest.fail("unsafe lock unexpectedly acquired")
