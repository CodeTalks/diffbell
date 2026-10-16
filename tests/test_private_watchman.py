"""The private test Watchman's helpers (Plan 6 spec §2), without a real Watchman."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.private_watchman import (
    PrivateWatchman,
    TestWatchmanError,
    UserWatchman,
    compare,
    private_session,
    start_argv,
    start_private,
    user_watchman,
)

TESTS = Path(__file__).parent


class FakeRun:
    """Records each call's argv and keyword arguments; returns one canned result."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.result = (returncode, stdout, stderr)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, *self.result)


def test_the_user_watch_list_is_read_without_spawning_and_without_the_private_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WATCHMAN_SOCK", "/private/sock")
    run = FakeRun(0, '{"version": "2026.10.05.00", "roots": ["/a", "/b"]}')
    assert user_watchman(run) == UserWatchman(True, frozenset({"/a", "/b"}))
    [(argv, kwargs)] = run.calls
    assert argv == ["watchman", "--no-spawn", "--no-local", "--no-pretty", "watch-list"]
    assert "WATCHMAN_SOCK" not in kwargs["env"]
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 10


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, "", ""),  # --no-local without a server: exit 1, no output
        (
            1,
            '{"error":"watchman::CommandValidationError: failed to validate command: '
            'command watch-list not available in this mode","version":"2026.10.05.00"}',
            '[cli] send_error_response: ["watch-list"], failed: ...',
        ),
        (1, "", "unable to talk to your watchman on /x/sock! (Connection refused)"),
        (1, "", "unable to talk to your watchman on /x/sock! (No such file or directory)"),
    ],
)
def test_no_user_watchman_reads_as_not_running(returncode: int, stdout: str, stderr: str) -> None:
    assert user_watchman(FakeRun(returncode, stdout, stderr)) == UserWatchman(False, frozenset())


def test_any_other_watch_list_failure_raises_with_the_output() -> None:
    with pytest.raises(TestWatchmanError, match="exited 2: something odd"):
        user_watchman(FakeRun(2, "", "something odd"))
    with pytest.raises(TestWatchmanError, match="unreadable watch-list reply"):
        user_watchman(FakeRun(0, "not json"))


def test_a_watch_list_that_hangs_raises() -> None:
    def hangs(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(TestWatchmanError, match="watch-list` did not answer in 10 s"):
        user_watchman(hangs)


RUNNING = UserWatchman(True, frozenset({"/Users/u/projects/shop", "/Users/u/projects/website"}))


def test_an_unchanged_watchman_passes() -> None:
    assert compare(RUNNING, UserWatchman(True, frozenset(RUNNING.roots))) is None
    assert compare(UserWatchman(False, frozenset()), UserWatchman(False, frozenset())) is None


def test_a_changed_root_set_names_what_was_added_and_removed_with_both_lists() -> None:
    after = UserWatchman(True, frozenset({"/Users/u/projects/website", "/private/var/t/watched0"}))
    assert compare(RUNNING, after) == (
        "the user's Watchman changed during the test session\n"
        "root set changed: added ['/private/var/t/watched0'] / "
        "removed ['/Users/u/projects/shop']\n"
        "before: running, roots ['/Users/u/projects/shop', '/Users/u/projects/website']\n"
        "after:  running, roots ['/Users/u/projects/website', '/private/var/t/watched0']"
    )


def test_a_daemon_started_during_the_run_says_so_at_a_glance() -> None:
    assert compare(UserWatchman(False, frozenset()), RUNNING) == (
        "the user's Watchman changed during the test session\n"
        "running state changed: not running → running\n"
        "before: not running\n"
        "after:  running, roots ['/Users/u/projects/shop', '/Users/u/projects/website']"
    )


def test_a_daemon_stopped_during_the_run_says_so_at_a_glance() -> None:
    message = compare(RUNNING, UserWatchman(False, frozenset()))
    assert message is not None
    assert "running state changed: running → not running\n" in message
    assert message.endswith("after:  not running")


def test_the_start_argv_uses_private_paths_and_logs_to_stdout(tmp_dir: Path) -> None:
    assert start_argv("watchman", tmp_dir) == [
        "watchman",
        "--foreground",
        f"--unix-listener-path={tmp_dir}/sock",
        f"--statefile={tmp_dir}/state",
        f"--pidfile={tmp_dir}/pid",
        "--logfile=-",
    ]


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A short temp folder: pytest's own tmp paths are too long for a socket path."""
    path = Path(tempfile.mkdtemp(prefix="dft-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def fake_binary(tmp_dir: Path, body: str) -> str:
    script = tmp_dir / "fake-watchman"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o700)
    return str(script)


def test_a_watchman_that_refuses_to_start_fails_quoting_its_output(short_dir: Path) -> None:
    binary = fake_binary(
        short_dir,
        'echo "refusing to start: nice value 10 is above min_acceptable_nice_value 0"\n'
        'echo "config: $WATCHMAN_CONFIG_FILE" >&2\nexit 1',
    )
    with pytest.raises(TestWatchmanError) as caught:
        start_private(binary, base=short_dir)
    message = str(caught.value)
    assert message.startswith("test Watchman did not start (exit 1); its output:")
    assert "refusing to start: nice value 10" in message
    assert "/watchman.json" in message  # the private config, passed in the environment
    assert [p.name for p in short_dir.iterdir()] == ["fake-watchman"]  # its directory is gone


def test_a_watchman_that_never_answers_is_killed_and_fails(short_dir: Path) -> None:
    binary = fake_binary(short_dir, 'echo "starting"\nexec sleep 60')
    with pytest.raises(TestWatchmanError, match=r"did not start within 2 s; its output:\nstarting"):
        # 2 s, not 0.5: under a full `make check` the fake's shell can take longer than
        # half a second to print "starting" (a flake seen once on 2026-10-10).
        start_private(binary, base=short_dir, timeout=2.0)
    assert [p.name for p in short_dir.iterdir()] == ["fake-watchman"]


def test_the_private_environment_drops_the_socket_variable(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WATCHMAN_SOCK", "/somewhere/else")
    binary = fake_binary(short_dir, 'echo "sock=[${WATCHMAN_SOCK:-unset}]"\nexit 3')
    with pytest.raises(TestWatchmanError, match=r"sock=\[unset\]"):
        start_private(binary, base=short_dir)


def test_a_socket_path_over_the_macos_limit_fails_clearly(tmp_dir: Path) -> None:
    deep = tmp_dir / ("d" * 90)
    deep.mkdir()
    with pytest.raises(TestWatchmanError, match="macOS allows 103; set TMPDIR"):
        start_private("watchman", base=deep)
    assert list(deep.iterdir()) == []


class FakePrivate(PrivateWatchman):
    """Records its stop instead of stopping a process."""

    def __init__(self, directory: Path, log: list[str]) -> None:
        super().__init__(directory, None)  # type: ignore[arg-type]
        self.log = log

    def stop(self) -> None:
        self.log.append("stop")


def test_the_session_reads_starts_stops_reads_and_leaves_the_socket_variable_on_the_dead_instance(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WATCHMAN_SOCK", "/before/the/session")
    log: list[str] = []

    def read() -> UserWatchman:
        log.append("read")
        return RUNNING

    def start() -> PrivateWatchman:
        log.append("start")
        return FakePrivate(tmp_dir, log)

    with private_session(start, read) as private:
        assert os.environ["WATCHMAN_SOCK"] == str(tmp_dir / "sock")
        log.append("tests")
    assert private.sock == tmp_dir / "sock"
    assert log == ["read", "start", "tests", "stop", "read"]
    # Never restored or unset: a leaked client must not fall back to the default socket.
    assert os.environ["WATCHMAN_SOCK"] == str(tmp_dir / "sock")


def test_the_session_fails_with_guard_2s_message_after_stopping(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WATCHMAN_SOCK", raising=False)
    log: list[str] = []
    reads = iter([UserWatchman(False, frozenset()), RUNNING])

    with (
        pytest.raises(TestWatchmanError, match="running state changed: not running → running"),
        private_session(lambda: FakePrivate(tmp_dir, log), lambda: next(reads)),
    ):
        pass
    assert log == ["stop"]
