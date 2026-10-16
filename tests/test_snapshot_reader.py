"""The hardened reader (content snapshots spec §2.4): the only reader of file
contents for snapshots. It never opens a redacted name, never follows a symlink,
never blocks on a FIFO, and decides size and text on the bytes it read."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from diffbell import diffing
from diffbell.config import BUILTIN_REDACT
from diffbell.diffing import SNAPSHOT_MAX_BYTES, read_capturable


def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path os.open is called with, from now on."""
    paths: list[str] = []
    real = os.open

    def spy(path: Any, *args: Any, **kw: Any) -> int:
        paths.append(os.fspath(path))
        return real(path, *args, **kw)

    monkeypatch.setattr(os, "open", spy)
    return paths


def within(fn: Any, seconds: float = 5.0) -> Any:
    box: list[Any] = []
    worker = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"read did not return within {seconds}s (blocked on open?)")
    return box[0]


def test_a_regular_text_file_is_read_whole(tmp_project: Path) -> None:
    (tmp_project / "package.json").write_bytes(b'{"a": 1}\r\n')
    assert read_capturable(tmp_project, "package.json", BUILTIN_REDACT) == b'{"a": 1}\r\n'


def test_an_empty_file_is_captured_as_empty(tmp_project: Path) -> None:
    (tmp_project / "VERSION").write_bytes(b"")
    assert read_capturable(tmp_project, "VERSION", BUILTIN_REDACT) == b""


def test_a_redacted_name_is_never_opened(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_project / ".env.local").write_text("API_KEY=SECRET\n")
    (tmp_project / "config").mkdir()
    (tmp_project / "config" / "secrets.yaml").write_text("token: SECRET\n")
    paths = opened(monkeypatch)
    assert read_capturable(tmp_project, ".env.local", BUILTIN_REDACT) is None
    assert read_capturable(tmp_project, "config/secrets.yaml", BUILTIN_REDACT) is None
    assert paths == []


def test_a_path_resolving_into_a_redacted_name_is_refused(tmp_project: Path) -> None:
    """A symlinked parent directory: the name looks harmless, its real path is not."""
    secrets = tmp_project / "secrets"
    secrets.mkdir()
    (secrets / "app.json").write_text('{"token": "SECRET"}\n')
    (tmp_project / "cfg").symlink_to("secrets")
    assert read_capturable(tmp_project, "cfg/app.json", BUILTIN_REDACT) is None


def test_a_path_escaping_the_project_is_refused(tmp_project: Path, tmp_dir: Path) -> None:
    outside = tmp_dir / "outside"
    outside.mkdir()
    (outside / "app.json").write_text("{}\n")
    (tmp_project / "vendor").symlink_to(outside)
    assert read_capturable(tmp_project, "vendor/app.json", BUILTIN_REDACT) is None


def test_a_symlink_is_refused(tmp_project: Path) -> None:
    (tmp_project / "real.json").write_text("{}\n")
    (tmp_project / "link.json").symlink_to("real.json")
    assert read_capturable(tmp_project, "link.json", BUILTIN_REDACT) is None


def test_a_hard_link_is_refused(tmp_project: Path) -> None:
    """Another name (e.g. .env.local) may share the content."""
    (tmp_project / ".env.local").write_text("API_KEY=SECRET\n")
    os.link(tmp_project / ".env.local", tmp_project / "package.json")
    assert read_capturable(tmp_project, "package.json", BUILTIN_REDACT) is None


def test_a_fifo_is_refused_without_blocking(tmp_project: Path) -> None:
    os.mkfifo(tmp_project / "deps.lock")
    assert within(lambda: read_capturable(tmp_project, "deps.lock", BUILTIN_REDACT)) is None


def test_a_missing_file_is_refused(tmp_project: Path) -> None:
    assert read_capturable(tmp_project, "gone.json", BUILTIN_REDACT) is None


def test_the_size_cap_is_decided_on_the_bytes_read(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_project / "fits.lock").write_bytes(b"x" * SNAPSHOT_MAX_BYTES)
    (tmp_project / "big.lock").write_bytes(b"x" * (SNAPSHOT_MAX_BYTES + 1))
    asked: list[int] = []
    real = os.read

    def spy(fd: int, n: int) -> bytes:
        asked.append(n)
        return real(fd, n)

    monkeypatch.setattr(os, "read", spy)
    assert read_capturable(tmp_project, "fits.lock", ()) == b"x" * SNAPSHOT_MAX_BYTES
    asked.clear()
    assert read_capturable(tmp_project, "big.lock", ()) is None
    assert sum(asked) <= SNAPSHOT_MAX_BYTES + 1  # a bounded read, never the whole file


def test_a_nul_in_the_first_8_kib_is_binary(tmp_project: Path) -> None:
    (tmp_project / "a.bin").write_bytes(b"ab\0cd")
    late = b"x" * diffing.BINARY_SNIFF_BYTES + b"\0"
    (tmp_project / "late.txt").write_bytes(late)
    assert read_capturable(tmp_project, "a.bin", ()) is None
    assert read_capturable(tmp_project, "late.txt", ()) == late


def test_nfd_and_case_variants_of_a_redacted_name_are_refused(tmp_project: Path) -> None:
    (tmp_project / ".ENV.local").write_text("API_KEY=SECRET\n")
    assert read_capturable(tmp_project, ".ENV.local", BUILTIN_REDACT) is None
