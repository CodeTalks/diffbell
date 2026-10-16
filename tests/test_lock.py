from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from diffbell.lock import AlreadyRunning, LockUnreadable, holder_pid, is_held, singleton_lock


def test_second_lock_is_refused_with_pid(tmp_dir: Path) -> None:
    path = tmp_dir / "state" / "daemon.lock"
    with singleton_lock(path):
        with pytest.raises(AlreadyRunning, match=f"pid {os.getpid()}"), singleton_lock(path):
            pass
        assert holder_pid(path) == os.getpid()


def test_lock_is_released_on_exit(tmp_dir: Path) -> None:
    path = tmp_dir / "daemon.lock"
    with singleton_lock(path):
        pass
    assert holder_pid(path) is None
    with singleton_lock(path):
        pass


def test_unreadable_lock_cannot_tell_who_holds_it(tmp_dir: Path) -> None:
    path = tmp_dir / "daemon.lock"
    path.write_text("123")
    path.chmod(0)
    try:
        with pytest.raises(LockUnreadable) as caught:
            holder_pid(path)
    finally:
        path.chmod(0o600)
    assert caught.value.path == path
    assert caught.value.strerror == "Permission denied"


def test_missing_lock_has_no_holder(tmp_dir: Path) -> None:
    assert holder_pid(tmp_dir / "nope" / "daemon.lock") is None


@contextmanager
def held_without_pid(path: Path) -> Iterator[None]:
    """The lock as singleton_lock holds it at its start and shutdown: an empty file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_is_held_sees_a_lock_with_no_pid(tmp_dir: Path) -> None:
    path = tmp_dir / "daemon.lock"
    with held_without_pid(path):
        assert holder_pid(path) is None  # what is_held exists for
        assert is_held(path)
    assert not is_held(path)


def test_is_held_follows_singleton_lock(tmp_dir: Path) -> None:
    path = tmp_dir / "daemon.lock"
    assert not is_held(path)  # missing
    with singleton_lock(path):
        assert is_held(path)
    assert not is_held(path)
    assert not is_held(path)  # the shared probe released its lock


def test_is_held_raises_on_an_unreadable_lock(tmp_dir: Path) -> None:
    path = tmp_dir / "daemon.lock"
    path.write_text("")
    path.chmod(0)
    try:
        with pytest.raises(LockUnreadable):
            is_held(path)
    finally:
        path.chmod(0o600)
