"""A private Watchman for the integration tests (Plan 6 spec §2, ADR 0014).

The integration tests never use the user's Watchman: a session fixture starts one of
their own on a socket in a short private temp directory and points ``WATCHMAN_SOCK``
at it, which pywatchman (and so ``WatchmanClient``) honours. The ``watchman`` CLI
does **not** honour ``WATCHMAN_SOCK`` (verified 2026-10-10), so every fixture command
goes through pywatchman on the private socket. The one CLI call left is guard 2's
read-only ``watchman --no-spawn --no-local --no-pretty watch-list`` against the
user's default socket, before and after the session (``--no-local``: without a
server it exits 1 instead of answering in client mode).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pywatchman

START_TIMEOUT = 10.0  # seconds for the private instance to answer `version`
STOP_TIMEOUT = 5.0  # seconds after SIGTERM before SIGKILL
SOCK_MAX = 103  # macOS sun_path holds 104 bytes, the terminating NUL included
OUTPUT_TAIL = 2000  # characters of the daemon's output quoted in a failure
# With --no-local, no server is a non-zero exit with no output. Kept as a fallback
# (an older client without --no-local semantics): the client refuses the command
# locally, or the connection fails.
NOT_RUNNING = (
    "not available in this mode",
    "unable to talk to your watchman",
    "Connection refused",
    "No such file or directory",
)
USER_WATCH_LIST = ["watchman", "--no-spawn", "--no-local", "--no-pretty", "watch-list"]

Runner = Callable[..., subprocess.CompletedProcess[str]]


class TestWatchmanError(Exception):
    """The private Watchman could not be started, or the user's could not be read."""

    __test__ = False  # not a test class, despite the name


@dataclass(frozen=True, slots=True)
class UserWatchman:
    """The user's Watchman as guard 2 sees it: running or not, and its roots."""

    running: bool
    roots: frozenset[str]

    def describe(self) -> str:
        if not self.running:
            return "not running"
        return f"running, roots {sorted(self.roots)}"


def _without_sock() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("WATCHMAN_SOCK", None)
    return env


def user_watchman(runner: Runner = subprocess.run) -> UserWatchman:
    """Read the user's watch list through the CLI's default socket, never spawning."""
    try:
        result = runner(
            USER_WATCH_LIST,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
            env=_without_sock(),
        )
    except subprocess.TimeoutExpired:
        raise TestWatchmanError(
            "`watchman --no-spawn --no-local watch-list` did not answer in 10 s"
        ) from None
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 and not output.strip():
        return UserWatchman(False, frozenset())  # --no-local: no server
    if result.returncode == 0:
        try:
            roots = json.loads(result.stdout)["roots"]
        except (ValueError, KeyError, TypeError):
            raise TestWatchmanError(f"unreadable watch-list reply: {output.strip()}") from None
        return UserWatchman(True, frozenset(str(root) for root in roots))
    if any(marker in output for marker in NOT_RUNNING):
        return UserWatchman(False, frozenset())
    raise TestWatchmanError(
        f"`watchman --no-spawn --no-local watch-list` exited {result.returncode}: {output.strip()}"
    )


def compare(before: UserWatchman, after: UserWatchman) -> str | None:
    """None if the user's Watchman is unchanged, else guard 2's failure message."""
    if before == after:
        return None
    if before.running != after.running:
        kind = (
            "running state changed: "
            f"{'running' if before.running else 'not running'} → "
            f"{'running' if after.running else 'not running'}"
        )
    else:
        added = sorted(after.roots - before.roots) or "nothing"
        removed = sorted(before.roots - after.roots) or "nothing"
        kind = f"root set changed: added {added} / removed {removed}"
    return (
        "the user's Watchman changed during the test session\n"
        f"{kind}\n"
        f"before: {before.describe()}\n"
        f"after:  {after.describe()}"
    )


@dataclass
class PrivateWatchman:
    """A running private Watchman: its directory, socket and process."""

    directory: Path
    proc: subprocess.Popen[bytes]

    @property
    def sock(self) -> Path:
        return self.directory / "sock"

    @property
    def output(self) -> Path:
        return self.directory / "output.log"

    def client(self, timeout: float = 5.0) -> Any:
        """A pywatchman client on the private socket (never resolved via the CLI)."""
        return pywatchman.client(sockpath=str(self.sock), timeout=timeout)

    def query(self, *args: Any) -> Any:
        client = self.client()
        try:
            return client.query(*args)
        finally:
            client.close()

    def tail(self) -> str:
        try:
            return self.output.read_text(errors="replace")[-OUTPUT_TAIL:]
        except OSError:
            return ""

    def stop(self) -> None:
        """Terminate the process this fixture started (never `shutdown-server`), kill
        it after STOP_TIMEOUT, and remove the directory."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        shutil.rmtree(self.directory, ignore_errors=True)


def start_argv(binary: str, directory: Path) -> list[str]:
    return [
        binary,
        "--foreground",
        f"--unix-listener-path={directory / 'sock'}",
        f"--statefile={directory / 'state'}",
        f"--pidfile={directory / 'pid'}",
        "--logfile=-",
    ]


def start_private(
    binary: str = "watchman", *, base: Path | None = None, timeout: float = START_TIMEOUT
) -> PrivateWatchman:
    """Start a private Watchman and wait until it answers `version`.

    Raises TestWatchmanError (quoting the daemon's output) if the socket path is too
    long, the process exits, or it does not answer within ``timeout``. There is no
    fallback to the user's Watchman."""
    directory = Path(tempfile.mkdtemp(prefix="dfwm-", dir=base))
    sock = directory / "sock"
    if len(os.fsencode(sock)) > SOCK_MAX:
        shutil.rmtree(directory, ignore_errors=True)
        raise TestWatchmanError(
            f"test Watchman did not start: its socket path {sock} is "
            f"{len(os.fsencode(sock))} bytes, macOS allows {SOCK_MAX}; "
            "set TMPDIR to a shorter folder"
        )
    # Watchman refuses to start under a nice value above 0 (a niced shell, a CI
    # runner) unless its config allows it; only this private daemon reads the file.
    (directory / "watchman.json").write_text(json.dumps({"min_acceptable_nice_value": 19}))
    env = _without_sock()
    env["WATCHMAN_CONFIG_FILE"] = str(directory / "watchman.json")
    # --logfile=- sends the log to stdout/stderr; a file (not a pipe nobody reads,
    # which would block the daemon once full) captures it.
    with open(directory / "output.log", "wb") as output:
        proc = subprocess.Popen(
            start_argv(binary, directory),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
        )
    private = PrivateWatchman(directory, proc)
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            tail = private.tail()
            private.stop()
            raise TestWatchmanError(
                f"test Watchman did not start (exit {proc.returncode}); its output:\n{tail}"
            )
        try:
            private.query("version")
            return private
        except (pywatchman.WatchmanError, OSError):
            pass
        if time.monotonic() > deadline:
            tail = private.tail()
            private.stop()
            raise TestWatchmanError(
                f"test Watchman did not start within {timeout:g} s; its output:\n{tail}"
            )
        time.sleep(0.05)


@contextlib.contextmanager
def private_session(
    start: Callable[[], PrivateWatchman] = start_private,
    read: Callable[[], UserWatchman] = user_watchman,
) -> Iterator[PrivateWatchman]:
    """The test session's Watchman, with guard 2 around it.

    Order: read the user's watch list, start the private instance, point
    ``WATCHMAN_SOCK`` at it; at the end stop it, read the user's list again and
    raise TestWatchmanError (guard 2's message) if it changed.

    ``WATCHMAN_SOCK`` is **never restored or unset**: it keeps pointing at the dead
    private socket for the rest of the process. A leaked ``WatchmanClient`` thread
    reconnecting with backoff then fails against that path; without the variable,
    pywatchman would run ``watchman get-sockname``, which reaches (or spawns) the
    user's daemon."""
    before = read()
    private = start()
    os.environ["WATCHMAN_SOCK"] = str(private.sock)
    try:
        yield private
    finally:
        private.stop()
        problem = compare(before, read())
        if problem is not None:
            raise TestWatchmanError(problem)
