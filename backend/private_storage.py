"""Owner-private local storage primitives for live application data."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def ensure_private_directory(path: Path) -> None:
    """Create or restrict a directory without accepting a substituted entry."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        return

    before = path.lstat()
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError("Unsafe private storage directory")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError("Unsafe private storage directory")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Private storage directory changed while opening")
        os.fchmod(descriptor, 0o700)
    except OSError as error:
        raise ValueError("Unsafe private storage directory") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def open_private_regular_file(path: Path, flags: int) -> int:
    """Open one owner-private regular file and return its pinned descriptor."""
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Unsafe private storage file")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
            current = path.lstat()
            if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ValueError("Private storage file changed while opening")
        result = descriptor
        descriptor = -1
        return result
    except OSError as error:
        raise ValueError("Unsafe private storage file") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
