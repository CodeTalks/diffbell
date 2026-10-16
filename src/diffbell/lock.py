"""Singleton guard so `run` and the launchd agent never both send (spec §3)."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(Exception):
    pass


class LockUnreadable(Exception):
    """The lock file exists but can't be read, so who holds it can't be determined."""

    def __init__(self, path: Path, error: OSError) -> None:
        self.path = path
        self.strerror = error.strerror or type(error).__name__
        super().__init__(f"cannot read {path} ({self.strerror})")


def _read_pid(fd: int) -> int | None:
    raw = os.pread(fd, 32, 0).decode(errors="replace").strip()
    return int(raw) if raw.isdigit() else None


@contextmanager
def singleton_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid = _read_pid(fd)
        os.close(fd)
        raise AlreadyRunning(f"diffbell is already running (pid {pid or '?'})") from None
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
        yield
    finally:
        os.ftruncate(fd, 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def holder_pid(path: Path) -> int | None:
    """PID of the running daemon, or None if nobody holds the lock. Raises
    LockUnreadable when the lock file exists but can't be opened or read."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise LockUnreadable(path, exc) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_pid(fd)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    except OSError as exc:
        raise LockUnreadable(path, exc) from None
    finally:
        os.close(fd)


def is_held(path: Path) -> bool:
    """Whether anyone holds the lock, whatever its pid file says: singleton_lock holds
    it with an empty file at its start and at its shutdown, where holder_pid answers
    None. False when the file is missing. Raises LockUnreadable like holder_pid."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise LockUnreadable(path, exc) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError as exc:
        raise LockUnreadable(path, exc) from None
    finally:
        os.close(fd)
