"""Test helpers shared across test modules."""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

from diffbell.store import Store

HAS_WATCHMAN = shutil.which("watchman") is not None
HAS_GIT = shutil.which("git") is not None

# A file whose lines each hold a character str.splitlines() would break on, placed so
# a split line would read as a file header, a hunk header or a fake "+"/"-" line
# (diff output is split on "\n" only: diffing.split_lines). Written as escapes: the
# characters themselves are invisible in an editor.
HOSTILE_LINES = (
    "a\u2028diff --git x",
    "b\x85@@ -1 +1 @@",
    "c\x0cd\x0be",
    "f\u2029+fake",
    "g\x1c-fake\x1dh\x1ei",
    "crlf\r",
    "mid\rlone",
)
HOSTILE_BEFORE = b"head\ngone\n"
HOSTILE_AFTER = ("head\n" + "".join(f"{line}\n" for line in HOSTILE_LINES) + "tail\n").encode()
HOSTILE_CHANGED = ("-gone", *(f"+{line}" for line in HOSTILE_LINES), "+tail")


def git(repo: Path, *args: str) -> str:
    """Run git with a throwaway identity; return stdout."""
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def init_repo(path: Path) -> Path:
    git(path, "init", "-q", "-b", "main")
    return path


def free_port() -> int:
    """A port that was free a moment ago (good enough for tests)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def sync_names(store: Store, *names: str, now: float = 0.0) -> None:
    """Configure test projects by name, each at its own made-up folder
    (``/df-test/<name>``), as the daemon's sync does before it stores anything for
    them (stable project ids spec §2). Every other project becomes unconfigured."""
    store.sync_projects([(name, f"/df-test/{name}") for name in names], now)


def error_kind(store: Store, event_id: int) -> str | None:
    """events.last_error_kind of one event (EventRow doesn't carry it)."""
    row = store._db.execute(
        "SELECT last_error_kind FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    assert row is not None, event_id
    return None if row[0] is None else str(row[0])


def error_at(store: Store, event_id: int) -> float | None:
    """events.last_error_at of one event."""
    row = store._db.execute("SELECT last_error_at FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row is not None, event_id
    return None if row[0] is None else float(row[0])
