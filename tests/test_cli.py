from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from diffbell import BRAND, __version__, cli, launchd, liveness
from diffbell.cli import configure_logging, main, status_report
from diffbell.config import (
    DEFAULT_CONFIG_TEXT,
    Config,
    MissingProject,
    ResolvedProject,
    load_config,
)
from diffbell.daemon import Daemon
from diffbell.lock import singleton_lock
from diffbell.secrets import (
    ACCOUNT_BOT_TOKEN,
    ACCOUNT_CHAT_ID,
    ACCOUNT_SAVE_PENDING,
    Credentials,
    Missing,
    SecretStoreError,
    credentials,
)
from diffbell.sender_slot import SenderSlot
from diffbell.store import EventRow, MigrationError, Store
from diffbell.web import config_edit
from tests.fakes import FakeSender, fake_slot
from tests.test_store_migration import make_v5


@pytest.fixture(autouse=True)
def isolated(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every location at tmp, forbid the real Keychain, and restore logging."""
    monkeypatch.setenv("DIFFBELL_STATE_DIR", str(tmp_dir / "state"))
    monkeypatch.setenv("DIFFBELL_CONFIG", str(tmp_dir / "cfg" / "config.yaml"))
    monkeypatch.setenv("DIFFBELL_LOG_DIR", str(tmp_dir / "logs"))

    def no_keychain(account: str, **kwargs: Any) -> str | None:
        raise AssertionError("tests must not read the real Keychain")

    def no_sleep(seconds: float) -> None:
        raise AssertionError("unexpected keychain retry sleep")

    monkeypatch.setattr(cli, "get_secret", no_keychain)
    monkeypatch.setattr(cli, "_sleep", no_sleep)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    noisy = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield tmp_dir
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in noisy.items():
        logging.getLogger(name).setLevel(level)


def creds(monkeypatch: pytest.MonkeyPatch, value: str | None = "x") -> None:
    """Both items ``value`` (None: absent); never a save-pending marker."""
    monkeypatch.setattr(
        cli,
        "get_secret",
        lambda account, **kwargs: None if account == ACCOUNT_SAVE_PENDING else value,
    )


def telegram_says(monkeypatch: pytest.MonkeyPatch, line: str) -> None:
    """``status``'s Telegram line is ``line``; the gate reads "not set up"."""
    monkeypatch.setattr(cli, "_credential_state", lambda: Missing("both"))
    monkeypatch.setattr(cli, "_telegram_check", lambda gate: line)


def broken_keychain(account: str, **kwargs: Any) -> str | None:
    raise SecretStoreError(f"keychain read failed for {account!r} (exit 1)")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert f"{BRAND} {__version__}" in capsys.readouterr().out


def test_logging_never_shows_httpx_request_urls(tmp_dir: Path) -> None:
    configure_logging(tmp_dir / "x.log", verbose=True)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ({}, Missing("both")),
        ({ACCOUNT_BOT_TOKEN: "t"}, Missing("chat id")),
        ({ACCOUNT_CHAT_ID: "42"}, Missing("bot token")),
        (
            {ACCOUNT_SAVE_PENDING: "1.5", ACCOUNT_BOT_TOKEN: "t", ACCOUNT_CHAT_ID: "42"},
            Missing("unfinished save"),
        ),
        ({ACCOUNT_BOT_TOKEN: "t", ACCOUNT_CHAT_ID: "42"}, Credentials("t", "42")),
    ],
)
def test_run_starts_with_the_keychain_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dir: Path,
    items: dict[str, str],
    expected: Credentials | Missing,
) -> None:
    """Bot onboarding spec §1: absent items or the marker start "not set up"."""
    monkeypatch.setattr(cli, "get_secret", lambda account, **kwargs: items.get(account))
    started: list[Credentials | Missing] = []

    async def fake_daemon(state: Credentials | Missing) -> int:
        started.append(state)
        return 0

    monkeypatch.setattr(cli, "_run_daemon", fake_daemon)
    assert main(["run"]) == 0
    assert started == [expected]
    assert not (tmp_dir / "state" / "status.json").exists()  # not a failed start


def test_run_with_unreadable_keychain_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_secret", broken_keychain)
    monkeypatch.setattr(cli, "_sleep", lambda seconds: None)
    assert main(["run"]) == 2


def test_run_refuses_when_already_running(monkeypatch: pytest.MonkeyPatch, tmp_dir: Path) -> None:
    creds(monkeypatch)
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["run"]) == 3


def test_run_with_invalid_config_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_dir: Path) -> None:
    creds(monkeypatch)
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("projects: 5\n")
    assert main(["run"]) == 2


def failure(tmp_dir: Path) -> dict[str, Any]:
    status = json.loads((tmp_dir / "state" / "status.json").read_text())
    assert status["running"] is True and "clean_shutdown" not in status
    return dict(status)


def test_a_failed_start_is_recorded_in_status_json(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    """Health spec §5.1: the first start that succeeds reports it once."""
    creds(monkeypatch)
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("projects: 5\n")
    assert main(["run"]) == 2
    assert "projects" in failure(tmp_dir)["last_failure"]


def test_a_failed_migration_is_recorded_in_status_json(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)

    async def failing(state: Credentials | Missing) -> int:
        raise MigrationError("database migration failed: disk I/O error")

    monkeypatch.setattr(cli, "_run_daemon", failing)
    assert main(["run"]) == 2
    assert failure(tmp_dir)["last_failure"] == "database migration failed: disk I/O error"


@pytest.mark.parametrize(
    ("value", "configured", "missing"), [(None, False, Missing("both")), ("x", True, None)]
)
def test_run_hands_the_daemon_a_slot_and_why_it_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    configured: bool,
    missing: Missing | None,
) -> None:
    creds(monkeypatch, value)
    seen: list[tuple[bool, Missing | None]] = []

    class Recording:
        def __init__(
            self,
            config_path: Path,
            store: Store,
            sender: SenderSlot,
            *,
            state_dir: Path,
            setup_missing: Missing | None = None,
        ) -> None:
            seen.append((sender.configured, setup_missing))

        async def run(self, stop: asyncio.Event) -> None:
            return None

    monkeypatch.setattr(cli, "Daemon", Recording)
    assert main(["run"]) == 0
    assert seen == [(configured, missing)]


def test_an_unexpected_start_failure_is_recorded_and_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)

    def corrupt(path: Path) -> Store:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(cli, "Store", corrupt)
    with pytest.raises(sqlite3.DatabaseError):
        main(["run"])
    assert failure(tmp_dir)["last_failure"] == "DatabaseError: file is not a database"


def test_a_failure_inside_daemon_run_before_its_first_snapshot_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("projects: []\n")

    def broken(self: Daemon, *args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Daemon, "_apply_config", broken)
    with pytest.raises(OSError):
        main(["run"])
    assert failure(tmp_dir)["last_failure"] == "OSError: disk full"


def started_daemon(
    tmp_dir: Path, *, after: Exception | None = None, stop_itself: bool = True
) -> Any:
    """A `cli.Daemon` stand-in: the real Daemon without Watchman or Telegram. It
    stops normally after its first snapshot (unless ``stop_itself`` is False), then
    raises ``after`` (if given), as a failure in `run` after the start would."""
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("projects: []\n")

    def no_watchman() -> Any:
        raise OSError("watchman is not running (test)")

    class StartedDaemon:
        def __init__(
            self,
            config_path: Path,
            store: Store,
            _: Any,
            *,
            state_dir: Path,
            setup_missing: Missing | None = None,
        ) -> None:
            self._state_dir = state_dir
            self._daemon = Daemon(
                config_path,
                store,
                fake_slot(),
                state_dir=state_dir,
                wm_factory=no_watchman,
                recv_timeout=0.05,
                maintenance_interval=0.02,
            )

        async def run(self, stop: asyncio.Event) -> None:
            async def stop_after_first_snapshot() -> None:
                while not (self._state_dir / "status.json").exists():
                    await asyncio.sleep(0.01)
                stop.set()

            stopper = asyncio.create_task(stop_after_first_snapshot()) if stop_itself else None
            try:
                await self._daemon.run(stop)
            finally:
                if stopper is not None:
                    stopper.cancel()
            if after is not None:
                raise after

    return StartedDaemon


def next_start_says(tmp_dir: Path) -> str:
    status = liveness.read_status(tmp_dir / "state")
    message = liveness.restart_message(status, time.time() + 60)
    assert message is not None
    return message


def test_a_dead_background_task_keeps_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    """Health spec §5.1: a task that dies after the start is an unexpected stop."""
    creds(monkeypatch)
    monkeypatch.setattr(cli, "Daemon", started_daemon(tmp_dir, stop_itself=False))

    async def dying(self: Daemon, *args: Any) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(Daemon, "_maintenance", dying)
    assert main(["run"]) == 1
    status = json.loads((tmp_dir / "state" / "status.json").read_text())
    reason = "background task maintenance stopped: ValueError: boom"
    assert status["last_failure"] == reason
    assert "failed_start" not in status and "clean_shutdown" not in status
    message = next_start_says(tmp_dir)
    assert message.startswith("⚠️ Diffbell restarted after an unexpected stop (down ")
    assert message.endswith(f"): {reason}")


def test_an_exception_after_the_start_is_an_unexpected_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)
    monkeypatch.setattr(cli, "Daemon", started_daemon(tmp_dir, after=OSError("late")))
    with pytest.raises(OSError):
        main(["run"])
    status = json.loads((tmp_dir / "state" / "status.json").read_text())
    assert "failed_start" not in status and "clean_shutdown" not in status
    message = next_start_says(tmp_dir)
    assert message.startswith("⚠️ Diffbell restarted after an unexpected stop (down ")
    assert message.endswith("): OSError: late")


def test_a_runtime_error_after_the_start_is_an_unexpected_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)
    monkeypatch.setattr(cli, "Daemon", started_daemon(tmp_dir, after=RuntimeError("late")))
    assert main(["run"]) == 1
    message = next_start_says(tmp_dir)
    assert message.startswith("⚠️ Diffbell restarted after an unexpected stop (down ")
    assert message.endswith("): late")


def test_a_runtime_error_before_the_start_is_a_failed_start(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)

    async def dying(state: Credentials | Missing) -> int:
        raise RuntimeError("background task pump stopped: ValueError: boom")

    monkeypatch.setattr(cli, "_run_daemon", dying)
    assert main(["run"]) == 1
    assert failure(tmp_dir)["last_failure"] == "background task pump stopped: ValueError: boom"
    assert next_start_says(tmp_dir).startswith("⚠️ Diffbell could not start earlier (")


def test_a_failure_before_the_start_after_an_earlier_run_is_a_failed_start(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    """One process, two runs (as in tests): the first run's start marker must not
    make the second run's config error look like an unexpected stop."""
    creds(monkeypatch)
    monkeypatch.setattr(cli, "Daemon", started_daemon(tmp_dir))
    assert main(["run"]) == 0
    (tmp_dir / "cfg" / "config.yaml").write_text("projects: 5\n")
    assert main(["run"]) == 2
    assert failure(tmp_dir)["failed_start"] is True
    assert next_start_says(tmp_dir).startswith("⚠️ Diffbell could not start earlier (")


@pytest.mark.parametrize("which", ["config", "migration", "other"])
def test_failures_before_the_start_still_read_could_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, which: str
) -> None:
    creds(monkeypatch)
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("projects: 5\n" if which == "config" else "projects: []\n")
    if which == "migration":

        async def failing(state: Credentials | Missing) -> int:
            raise MigrationError("database migration failed: disk I/O error")

        monkeypatch.setattr(cli, "_run_daemon", failing)
    if which == "other":

        def broken(self: Daemon, *args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Daemon, "_apply_config", broken)
        with pytest.raises(OSError):
            main(["run"])
    else:
        assert main(["run"]) == 2
    assert next_start_says(tmp_dir).startswith("⚠️ Diffbell could not start earlier (")


def test_a_running_daemons_status_json_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)
    (tmp_dir / "state").mkdir()
    status = tmp_dir / "state" / "status.json"
    status.write_text('{"running": true, "pid": 1}')
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["run"]) == 3
        monkeypatch.setattr(cli, "get_secret", lambda account, **kwargs: None)
        assert main(["run"]) == 3  # not set up still starts, and meets the lock
    assert status.read_text() == '{"running": true, "pid": 1}'


def test_run_with_a_missing_project_path_keeps_running(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    creds(monkeypatch)
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"projects:\n  - path: {tmp_dir / 'gone'}\n")
    sender = FakeSender()
    problems: list[str] = []

    def no_watchman() -> Any:
        raise OSError("watchman is not running (test)")

    class ShortDaemon:
        """The real Daemon without Watchman or Telegram; stops after its first snapshot."""

        def __init__(
            self,
            config_path: Path,
            store: Store,
            _: Any,
            *,
            state_dir: Path,
            setup_missing: Missing | None = None,
        ) -> None:
            self._state_dir = state_dir
            self._daemon = Daemon(
                config_path,
                store,
                fake_slot(sender),
                state_dir=state_dir,
                wm_factory=no_watchman,
                recv_timeout=0.05,
                maintenance_interval=0.02,
            )

        async def run(self, stop: asyncio.Event) -> None:
            async def stop_after_first_snapshot() -> None:
                while not (self._state_dir / "status.json").exists():
                    await asyncio.sleep(0.01)
                stop.set()

            stopper = asyncio.create_task(stop_after_first_snapshot())
            await self._daemon.run(stop)
            await stopper
            problems.extend(p.key for p in self._daemon._health.problems())

    monkeypatch.setattr(cli, "Daemon", ShortDaemon)
    assert main(["run"]) == 0
    assert "missing:gone" in problems  # a page-only warning: nothing is sent
    assert not any("gone" in text for text, _ in sender.sent)


def test_status_report_lists_missing_projects() -> None:
    text = status_report(
        daemon_pid=None,
        watchman_version=None,
        telegram="no credentials",
        projects=[],
        missing=[MissingProject("gone", Path("/p/gone"))],
        config_error=None,
        snapshot=None,
        counts={},
        failures=[],
    )
    assert "  gone  missing  /p/gone" in text
    assert "(none configured)" not in text


def test_status_report_shows_effective_patterns_and_failures() -> None:
    project = ResolvedProject("myapp", Path("/p/myapp"), ("package.json", ".env*"), ("app.json",))
    failure = EventRow(12, "myapp", 0.0, "failed", 1, 0.0, "sendMessage: Unauthorized", None)
    text = status_report(
        daemon_pid=4242,
        watchman_version="2026.10.05.00",
        telegram="@my_bot ok",
        projects=[project],
        config_error=None,
        snapshot={
            "pid": 4242,
            "running": True,
            "updated_at": time.time(),
            "projects": [
                {"name": "myapp", "status": "subscribed", "note": "first run: baseline recorded"}
            ],
        },
        counts={"pending": 0, "sent": 3, "failed": 1},
        failures=[failure],
    )
    assert "daemon:   running (pid 4242)" in text
    assert "watchman: 2026.10.05.00" in text
    assert "telegram: @my_bot ok" in text
    assert "myapp  subscribed  /p/myapp" in text
    assert "watched:  package.json, .env*" in text
    assert "excluded: app.json" in text
    assert "note: first run: baseline recorded" in text
    assert "pending 0 · sent 3 · failed 1" in text
    assert "#12 myapp: sendMessage: Unauthorized" in text


@pytest.mark.parametrize(
    ("daemon_pid", "snapshot_pid", "running"),
    [(None, 4242, True), (None, 4242, False), (4242, 4242, False), (5555, 4242, True)],
)
def test_status_report_ignores_a_stale_snapshot(
    daemon_pid: int | None, snapshot_pid: int, running: bool
) -> None:
    project = ResolvedProject("demo", Path("/p/demo"), ("package.json",), ())
    text = status_report(
        daemon_pid=daemon_pid,
        watchman_version=None,
        telegram="no credentials",
        projects=[project],
        config_error=None,
        snapshot={
            "pid": snapshot_pid,
            "running": running,
            "updated_at": time.time(),
            "projects": [{"name": "demo", "status": "subscribed", "note": "old note"}],
        },
        counts={},
        failures=[],
    )
    assert "snapshot: from a stopped daemon" in text
    assert "demo  unknown  /p/demo" in text
    assert "subscribed" not in text and "old note" not in text


def test_run_exits_1_when_a_background_task_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    creds(monkeypatch)

    async def dying(state: Credentials | Missing) -> int:
        raise RuntimeError("background task pump stopped: ValueError: boom")

    monkeypatch.setattr(cli, "_run_daemon", dying)
    assert main(["run"]) == 1


def test_run_exits_2_when_the_migration_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)

    async def failing(state: Credentials | Missing) -> int:
        raise MigrationError("database migration failed: disk I/O error")

    monkeypatch.setattr(cli, "_run_daemon", failing)
    assert main(["run"]) == 2
    assert "ERROR diffbell: database migration failed: disk I/O error" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["status"], ["web", "--logout"]])
def test_status_and_web_report_a_failed_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dir: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    (tmp_dir / "state").mkdir()
    (tmp_dir / "state" / "state.db").touch()

    def failing(path: Path) -> Store:
        raise MigrationError("database migration failed: disk I/O error")

    monkeypatch.setattr(cli, "Store", failing)
    assert main(argv) == 2
    assert capsys.readouterr().out.strip().endswith("database migration failed: disk I/O error")


def test_status_command_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = tmp_dir / "proj"
    proj.mkdir()
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"projects:\n  - path: {proj}\n    name: demo\n    ignore: ['x/**']\n")
    state = tmp_dir / "state"
    state.mkdir()
    (state / "status.json").write_text(
        json.dumps(
            {"updated_at": time.time(), "projects": [{"name": "demo", "status": "subscribed"}]}
        )
    )
    monkeypatch.setattr(cli, "_watchman_version", lambda: "2026.1")
    telegram_says(monkeypatch, "@bot ok")
    with singleton_lock(state / "daemon.lock"):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "daemon:   running (pid " in out
    assert f"demo  subscribed  {proj}" in out
    assert "excluded: x/**" in out
    assert "pending 0 · sent 0 · failed 0" in out


def test_status_command_shows_a_missing_project(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"projects:\n  - path: {tmp_dir / 'gone'}\n")
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "no credentials")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"gone  missing  {tmp_dir / 'gone'}" in out and "INVALID" not in out


def test_status_command_names_the_mac(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0023: right after the daemon line, the name messages carry."""
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "no credentials")
    seen = []
    for text in ("", 'notify:\n  machine: " Work  MBP "\n', 'notify:\n  machine: ""\n'):
        cfg.write_text(text)
        assert main(["status"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("daemon:")
        seen.append(lines[1])
    assert seen == ["machine:  Test Mac", "machine:  Work MBP", "machine:  (not named)"]


def test_status_report_without_a_name_says_not_named() -> None:
    text = status_report(
        daemon_pid=None,
        watchman_version=None,
        telegram="no credentials",
        projects=[],
        config_error=None,
        snapshot=None,
        counts={},
        failures=[],
    )
    assert text.splitlines()[1] == "machine:  (not named)"


def test_status_with_invalid_config_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "no credentials")
    assert main(["status"]) == 1
    out = capsys.readouterr().out
    assert "config:   INVALID" in out and "watchman: unreachable" in out


def test_telegram_check_reports_keychain_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_secret", broken_keychain)
    assert cli._telegram_check(cli._read_gate()).startswith("keychain error")


class FakeClient:
    sent: ClassVar[list[str]] = []
    modes: ClassVar[list[str | None]] = []

    def __init__(self, token: str, chat_id: str | None) -> None:
        pass

    async def send(self, text: str, *, parse_mode: str | None = "HTML") -> int:
        FakeClient.sent.append(text)
        FakeClient.modes.append(parse_mode)
        return 1

    async def aclose(self) -> None:
        pass


def test_test_command_sends_a_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    creds(monkeypatch)
    FakeClient.sent = []
    FakeClient.modes = []
    monkeypatch.setattr(cli, "TelegramClient", FakeClient)
    assert main(["test"]) == 0
    assert len(FakeClient.sent) == 1
    text = FakeClient.sent[0]
    assert "package.json" in text
    assert ".env.local" in text and "contents hidden" in text
    assert FakeClient.modes == ["HTML"]
    # The redacted file is named once, and nothing of it is in the quote.
    assert "🔒 <code>.env.local</code> · contents hidden" in text.splitlines()
    assert text.count(".env.local") == 1
    assert ".env.local" not in text.split("<blockquote expandable>")[1]


def test_test_command_without_credentials_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    creds(monkeypatch, None)
    assert main(["test"]) == 2


def keychain(monkeypatch: pytest.MonkeyPatch, items: dict[str, str]) -> None:
    """``cli._credential_state`` through the real gate (``secrets.credentials``)
    over a fake Keychain holding ``items``."""
    monkeypatch.setattr(cli, "_credential_state", lambda: credentials(get=items.get))


UNFINISHED = {ACCOUNT_BOT_TOKEN: "t", ACCOUNT_CHAT_ID: "1", ACCOUNT_SAVE_PENDING: "1.0"}


class NoClient:
    def __init__(self, token: str, chat_id: str | None) -> None:
        raise AssertionError("no Telegram call while not set up")


def test_test_command_sends_nothing_after_an_unfinished_save(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keychain(monkeypatch, UNFINISHED)
    monkeypatch.setattr(cli, "TelegramClient", NoClient)
    assert main(["test"]) == 2
    out = capsys.readouterr().out
    assert cli.NO_CREDENTIALS in out and "Settings → Telegram" in out
    assert "(not set up: an earlier save didn't finish)" in out
    assert "Keychain:" not in out


def test_no_credentials_names_the_web_setup() -> None:
    assert "Settings → Telegram" in cli.NO_CREDENTIALS
    assert "`diffbell setup`" in cli.NO_CREDENTIALS


@pytest.mark.parametrize(
    ("items", "line"),
    [
        ({}, "not set up (Settings → Telegram, or `diffbell setup`)"),
        (
            {ACCOUNT_BOT_TOKEN: "t"},
            "not set up: the chat id is missing (Settings → Telegram, or `diffbell setup`)",
        ),
        (
            UNFINISHED,
            "not set up: an earlier save didn't finish (Settings → Telegram, or `diffbell setup`)",
        ),
    ],
)
def test_telegram_check_reports_not_set_up_without_calling_telegram(
    monkeypatch: pytest.MonkeyPatch, items: dict[str, str], line: str
) -> None:
    keychain(monkeypatch, items)
    monkeypatch.setattr(cli, "TelegramClient", NoClient)
    assert cli._telegram_check(cli._read_gate()) == line


def live_status(tmp_dir: Path, **extra: Any) -> None:
    """A live snapshot of this process's daemon (the test holds the lock)."""
    state = tmp_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    snapshot = {"pid": os.getpid(), "running": True, "updated_at": time.time(), "projects": []}
    (state / "status.json").write_text(json.dumps({**snapshot, **extra}))


RESTART_LINE = (
    f"          saved, but the running {BRAND} started before that: run `diffbell install`"
)


@pytest.mark.parametrize(
    ("configured", "state", "hint"),
    [
        (False, Credentials("t", "1"), True),  # saved after the daemon started
        (True, Credentials("t", "1"), False),
        (False, Missing("both"), False),  # really not set up
        (None, Credentials("t", "1"), False),  # an older daemon: no such key
    ],
)
def test_status_hints_a_restart_when_the_daemon_predates_the_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dir: Path,
    capsys: pytest.CaptureFixture[str],
    configured: bool | None,
    state: Credentials | Missing,
    hint: bool,
) -> None:
    write_config(tmp_dir, projects=False)
    live_status(tmp_dir, **({} if configured is None else {"telegram_configured": configured}))
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "@bot ok")
    monkeypatch.setattr(cli, "_credential_state", lambda: state)
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert (RESTART_LINE in lines) is hint
    if hint:
        assert lines[lines.index(RESTART_LINE) - 1] == "telegram: @bot ok"


class BotOk:
    def __init__(self, token: str, chat_id: str | None) -> None:
        pass

    async def get_me(self) -> dict[str, Any]:
        return {"username": "bot"}

    async def aclose(self) -> None:
        pass


def test_status_reads_the_credentials_gate_once(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Telegram line and the restart hint judge the same Keychain read: a save
    landing between two reads can't make them disagree."""
    reads: list[int] = []

    def gate() -> Credentials | Missing:
        reads.append(1)
        return Credentials("t", "1")

    write_config(tmp_dir, projects=False)
    live_status(tmp_dir, telegram_configured=False)
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    monkeypatch.setattr(cli, "_credential_state", gate)
    monkeypatch.setattr(cli, "TelegramClient", BotOk)
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(reads) == 1
    assert lines[lines.index(RESTART_LINE) - 1] == "telegram: @bot ok"


def test_status_with_an_unreadable_keychain_shows_the_error_and_no_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_dir, projects=False)
    live_status(tmp_dir, telegram_configured=False)
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    monkeypatch.setattr(cli, "get_secret", broken_keychain)
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "telegram: keychain error: keychain read failed" in out
    assert RESTART_LINE not in out.splitlines()


def test_status_report_places_the_restart_hint_under_the_telegram_line() -> None:
    text = status_report(
        daemon_pid=1,
        watchman_version=None,
        telegram="@bot ok",
        projects=[],
        config_error=None,
        snapshot=None,
        counts=None,
        failures=[],
        telegram_restart=True,
    )
    lines = text.splitlines()
    assert lines[3:5] == ["telegram: @bot ok", RESTART_LINE]  # after daemon, machine, watchman


def test_setup_passes_whether_a_daemon_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    seen: list[bool] = []

    async def fake_run_setup(io: Any, **kwargs: Any) -> int:
        assert "save" not in kwargs  # the default: secrets.save_credentials
        seen.append(kwargs["daemon_running"]())
        return 0

    monkeypatch.setattr(cli, "run_setup", fake_run_setup)
    assert main(["setup"]) == 0
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["setup"]) == 0
    assert seen == [False, True]


UV_TOOL_EXE = Path.home() / ".local/share/uv/tools/diffbell/bin/diffbell"


def write_config(tmp_dir: Path, projects: bool = True) -> Path:
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if projects:
        proj = tmp_dir / "proj"
        proj.mkdir(exist_ok=True)
        cfg.write_text(f"projects:\n  - path: {proj}\n", encoding="utf-8")
    else:
        cfg.write_text("debounce:\n  quiet_seconds: 10\n", encoding="utf-8")
    return cfg


INSTALL_NOW = 1_800_000_000.0


def ready_status(**changes: Any) -> dict[str, Any]:
    """status.json as the restarted daemon writes it once it is up."""
    status: dict[str, Any] = {
        "pid": 4242,
        "running": True,
        "last_started_at": INSTALL_NOW + 1,
        "connected": True,
        "projects": [
            {"name": "proj", "status": "subscribed"},
            {"name": "other", "status": "subscribed"},
            {"name": "gone", "status": "missing"},
        ],
        "web": {
            "enabled": True,
            "running": True,
            "port": 8765,
            "last_error": None,
            "secret": "ab" * 32,
        },
    }
    status.update(changes)
    return status


def stub_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dir: Path,
    exe: Path,
    agent: int | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A fake launchd install whose "agent" writes ``status`` (default: a ready
    daemon) to status.json, on a fake clock: the readiness wait never really sleeps."""
    seen: dict[str, Any] = {}
    clock = {"t": 50.0}

    def fake_install(executable: Path, *, log_dir: Path) -> Path:
        seen.update(executable=executable, log_dir=log_dir)
        liveness.write_status_file(tmp_dir / "state", ready_status() if status is None else status)
        return tmp_dir / "agent.plist"

    def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds
        seen["slept"] = seen.get("slept", 0.0) + seconds

    monkeypatch.setattr(launchd, "find_executable", lambda: exe)
    monkeypatch.setattr(launchd, "install", fake_install)
    monkeypatch.setattr(launchd, "agent_pid", lambda: agent)  # never the real launchctl
    monkeypatch.setattr(cli, "_install_now", lambda: INSTALL_NOW)
    monkeypatch.setattr(cli, "_install_clock", lambda: clock["t"])
    monkeypatch.setattr(cli, "_install_sleep", fake_sleep)
    return seen


def install_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dir: Path,
    capsys: pytest.CaptureFixture[str],
    status: dict[str, Any],
) -> tuple[int, list[str]]:
    creds(monkeypatch)
    write_config(tmp_dir)
    stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE, status=status)
    code = main(["install"])
    return code, capsys.readouterr().out.splitlines()


def test_install_never_looks_at_the_real_old_install_in_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from diffbell import rename_install

    monkeypatch.setattr(rename_install, "run", lambda *a, **k: pytest.fail("moved"))
    monkeypatch.setattr(rename_install.Tools, "real", lambda: pytest.fail("real tools"))
    code, lines = install_output(monkeypatch, tmp_dir, capsys, ready_status())
    assert code == 0  # the autouse overrides make for_install() None
    assert lines[0] == (
        "A DIFFBELL_* override is set: not looking for an old dev-watchman install."  # old-name
    )


def test_the_real_move_refuses_under_the_test_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real run() and its guard: the CLI overrides refuse the switchover before
    any runner, and nothing of the install runs."""
    from diffbell import rename_install

    monkeypatch.setattr(
        rename_install, "for_install", lambda: (rename_install.OldInstall.real(), True)
    )
    monkeypatch.setattr(
        rename_install.Tools,
        "real",
        lambda: rename_install.Tools(
            launchctl=lambda *a, **k: pytest.fail("launchctl"),
            security=lambda *a, **k: pytest.fail("security"),
        ),
    )
    monkeypatch.setattr(cli, "_install", lambda args: pytest.fail("installed"))
    assert main(["install"]) == 1
    out = capsys.readouterr().out
    assert "Move failed:" in out and "while DIFFBELL_" in out
    assert not (tmp_dir / "cfg").exists() and not (tmp_dir / "state").exists()


def fake_move(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, error: str | None = None
) -> dict[str, Any]:
    """for_install() names a scratch old install; run() records its call, then runs
    the install callable (or raises ``error``). Never the real runners."""
    from diffbell import rename_install

    old = rename_install.OldInstall.scratch(tmp_dir / "old")
    tools = rename_install.Tools(
        launchctl=lambda *a, **k: pytest.fail("launchctl"),
        security=lambda *a, **k: pytest.fail("security"),
    )
    seen: dict[str, Any] = {}

    def fake_run(
        o: rename_install.OldInstall,
        *,
        switchover: bool,
        install: Any,
        tools: rename_install.Tools,
        new: Any = None,
    ) -> int:
        seen.update(old=o, switchover=switchover, tools=tools, new=new)
        seen["config_before"] = (tmp_dir / "cfg" / "config.yaml").exists()
        if error is not None:
            raise rename_install.RenameError(error)
        seen["installed"] = install()
        return 7

    monkeypatch.setattr(rename_install, "for_install", lambda: (old, True))
    monkeypatch.setattr(rename_install.Tools, "real", lambda: tools)
    monkeypatch.setattr(rename_install, "run", fake_run)
    seen["expect"] = (old, tools)
    return seen


def test_install_moves_the_old_install_first_then_runs_the_install(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = fake_move(monkeypatch, tmp_dir)
    creds(monkeypatch)
    stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE, status=ready_status())
    code = main(["install"])
    out = capsys.readouterr().out
    assert code == 7  # run()'s exit code
    assert (seen["old"], seen["tools"]) == seen["expect"]
    assert seen["switchover"] is True and seen["new"] is None
    assert seen["config_before"] is False  # nothing written before the move
    assert seen["installed"] == 0  # the install callable is the whole old body
    assert "Wrote a default config" in out and "Installed and started" in out


def test_a_refused_move_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = fake_move(monkeypatch, tmp_dir, error="step 1: the old agent didn't stop")
    stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE, status=ready_status())
    assert main(["install"]) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == ["Install refused: step 1: the old agent didn't stop"]
    assert "installed" not in seen
    assert not (tmp_dir / "cfg" / "config.yaml").exists()


def test_install_waits_for_the_daemon_and_reports_it(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines = install_output(monkeypatch, tmp_dir, capsys, ready_status())
    assert code == 0
    assert lines[-4:] == [
        f"Installed and started {launchd.LABEL} ({tmp_dir / 'agent.plist'}).",
        f"Logs: {tmp_dir / 'logs' / 'diffbell.log'}",
        "Daemon running (pid 4242), watching 2 project(s).",
        "Web UI on http://127.0.0.1:8765",
    ]
    out = "\n".join(lines)
    assert "ab" * 32 not in out and "login" not in out  # never the secret or a login URL


def test_install_without_the_web_ui_prints_no_web_line(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    web = {"enabled": False, "running": False, "port": 8765, "last_error": None}
    code, lines = install_output(monkeypatch, tmp_dir, capsys, ready_status(web=web))
    assert code == 0
    assert lines[-1] == "Daemon running (pid 4242), watching 2 project(s)."
    assert not any("Web UI" in line for line in lines)


def test_install_does_not_take_the_previous_runs_status_for_the_new_one(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = ready_status(last_started_at=INSTALL_NOW - 3600)
    code, lines = install_output(monkeypatch, tmp_dir, capsys, old)
    assert code == 1
    assert lines[-1] == (
        "Warning: the daemon did not report ready within 30 s: no new start reported. "
        "Run `diffbell doctor` or see the log."
    )
    assert not any(line.startswith("Daemon running") for line in lines)


def test_install_times_out_naming_watchman_and_pending_subscriptions(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = ready_status(connected=False, projects=[{"name": "proj", "status": "pending"}])
    code, lines = install_output(monkeypatch, tmp_dir, capsys, status)
    assert code == 1
    assert lines[-1] == (
        "Warning: the daemon did not report ready within 30 s: "
        "not connected to Watchman (is it running?); subscriptions not done yet: proj. "
        "Run `diffbell doctor` or see the log."
    )


def test_install_times_out_when_the_web_ui_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    web = {"enabled": True, "running": False, "port": 8765, "last_error": "address in use"}
    code, lines = install_output(monkeypatch, tmp_dir, capsys, ready_status(web=web))
    assert code == 1
    assert "web UI not running on port 8765 (address in use)" in lines[-1]


def test_install_polls_until_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    write_config(tmp_dir)
    seen = stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE, status=ready_status(connected=False))

    def sleep_then_ready(seconds: float) -> None:
        seen["slept"] = seen.get("slept", 0.0) + seconds
        liveness.write_status_file(tmp_dir / "state", ready_status())

    monkeypatch.setattr(cli, "_install_sleep", sleep_then_ready)
    assert main(["install"]) == 0
    assert seen["slept"] == 0.5
    assert "Daemon running (pid 4242)" in capsys.readouterr().out


def test_install_uses_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_dir: Path) -> None:
    creds(monkeypatch)
    write_config(tmp_dir)
    seen = stub_install(monkeypatch, tmp_dir, Path("/u/bin/diffbell"))
    assert main(["install"]) == 0
    assert seen == {"executable": Path("/u/bin/diffbell"), "log_dir": tmp_dir / "logs"}


def test_install_refuses_an_invalid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    cfg = write_config(tmp_dir)
    cfg.write_text("projects: [", encoding="utf-8")
    seen = stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE)
    assert main(["install"]) == 2
    out = capsys.readouterr().out
    assert "invalid YAML" in out and "fix the config first" in out
    assert seen == {}


def test_install_writes_the_default_config_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh install may reach `install` without `setup` (bot onboarding §1)."""
    creds(monkeypatch)  # configured: the config is left exactly as written
    seen = stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE)
    assert main(["install"]) == 0
    cfg = tmp_dir / "cfg" / "config.yaml"
    assert cfg.read_text() == DEFAULT_CONFIG_TEXT
    assert config_edit.web_enabled_set(cfg) is False
    out = capsys.readouterr().out
    assert f"Wrote a default config: {cfg}" in out and "nothing to watch yet" in out
    assert seen  # installed


def test_install_warns_when_no_projects(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    cfg = write_config(tmp_dir, projects=False)
    seen = stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE)
    assert main(["install"]) == 0
    out = capsys.readouterr().out
    assert "nothing to watch yet" in out and str(cfg) in out
    assert seen  # still installed


def test_install_warns_about_missing_project_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    cfg = write_config(tmp_dir)
    cfg.write_text(
        f"projects:\n  - path: {tmp_dir / 'proj'}\n  - path: {tmp_dir / 'gone'}\n",
        encoding="utf-8",
    )
    seen = stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE)
    assert main(["install"]) == 0
    out = capsys.readouterr().out
    assert f"Warning: project gone: path {tmp_dir / 'gone'} does not exist" in out
    assert "nothing to watch yet" not in out
    assert seen  # still installed


def test_install_warns_when_a_foreground_daemon_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    write_config(tmp_dir)
    seen = stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE)
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["install"]) == 0
    out = capsys.readouterr().out
    assert f"(pid {os.getpid()})" in out and "exit code 3" in out
    assert seen  # still installed


def test_install_does_not_warn_when_the_lock_holder_is_the_agent_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    write_config(tmp_dir)
    seen = stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE, agent=os.getpid())
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        assert main(["install"]) == 0
    assert "exit code 3" not in capsys.readouterr().out
    assert seen  # re-installed (bootout + bootstrap restarts the agent)


def test_install_does_not_warn_without_a_running_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    write_config(tmp_dir)
    stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE)
    assert main(["install"]) == 0
    assert "exit code 3" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "text"),
    [
        ("run", "run the daemon in the foreground"),
        ("setup", "store bot credentials in the Keychain"),
        ("status", "delivery status"),
        ("test", "send a sample notification"),
        ("install", "install and start the launchd agent"),
        ("uninstall", "stop and remove the launchd agent"),
    ],
)
def test_subcommand_help_has_a_description(
    command: str, text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([command, "--help"])
    assert text in capsys.readouterr().out


def test_install_warns_about_a_non_uv_tool_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    write_config(tmp_dir)
    exe = Path("/work/.worktrees/x/.venv/bin/diffbell")
    seen = stub_install(monkeypatch, tmp_dir, exe)
    assert main(["install"]) == 0
    out = capsys.readouterr().out
    assert f'will run "{exe}"' in out and "uv tool install ." in out
    assert seen


def test_install_does_not_warn_for_a_uv_tool_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds(monkeypatch)
    write_config(tmp_dir)
    stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE)
    assert main(["install"]) == 0
    assert "uv tool install" not in capsys.readouterr().out


# -- install while Telegram isn't set up (bot onboarding spec §1) ---------------------


class Opener:
    """Fake ``cli._open_page``: records each call, returns ``error``."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Path, str | None]] = []

    def __call__(self, config_path: Path, *, next_: str | None) -> str | None:
        self.calls.append((config_path, next_))
        return self.error


NOT_SET_UP = Missing("both")


def install_not_set_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dir: Path,
    *,
    state: Credentials | Missing = NOT_SET_UP,
    opener: Opener | None = None,
    status: dict[str, Any] | None = None,
) -> Opener:
    monkeypatch.setattr(cli, "_credential_state", lambda: state)
    fake = opener or Opener()
    monkeypatch.setattr(cli, "_open_page", fake)
    stub_install(monkeypatch, tmp_dir, UV_TOOL_EXE, status=status)
    return fake


def config_with_web(tmp_dir: Path, enabled: bool) -> Path:
    cfg = write_config(tmp_dir)
    cfg.write_text(cfg.read_text() + f"web:\n  enabled: {str(enabled).lower()}\n")
    return cfg


def test_install_turns_the_web_on_and_opens_the_setup_page(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No config at all: the default is written, then web.enabled (absent) turned on."""
    opener = install_not_set_up(monkeypatch, tmp_dir)
    assert main(["install"]) == 0
    cfg = tmp_dir / "cfg" / "config.yaml"
    assert "  # enabled: false" in cfg.read_text()  # the template's comment is kept
    assert load_config(cfg).web.enabled is True
    assert config_edit.web_enabled_set(cfg) is True
    assert opener.calls == [(cfg, "telegram")]
    lines = capsys.readouterr().out.splitlines()
    assert f"Wrote a default config: {cfg}" in lines
    assert lines[-2:] == [
        cli.WEB_TURNED_ON,
        "Opened the Telegram setup page in your browser (the login link is valid for 60 s).",
    ]
    assert cli.WEB_TURNED_ON == (
        "Turned on the web UI to set up Telegram (`diffbell web --disable` turns it off)."
    )


def test_install_opens_the_setup_page_when_the_web_is_already_on(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config_with_web(tmp_dir, True)
    before = cfg.read_bytes()
    opener = install_not_set_up(monkeypatch, tmp_dir, state=Missing("chat id"))
    assert main(["install"]) == 0
    assert cfg.read_bytes() == before
    assert opener.calls == [(cfg, "telegram")]
    assert cli.WEB_TURNED_ON not in capsys.readouterr().out


def test_install_never_overrides_a_stated_web_enabled_false(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config_with_web(tmp_dir, False)
    before = cfg.read_bytes()
    opener = install_not_set_up(monkeypatch, tmp_dir)
    assert main(["install"]) == 0
    assert cfg.read_bytes() == before and opener.calls == []
    assert capsys.readouterr().out.splitlines()[-1] == cli.SETUP_HINT
    assert cli.SETUP_HINT == (
        "Telegram isn't set up: run `diffbell web` to open the setup page, or `diffbell setup`."
    )


def test_install_when_configured_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_dir)
    before = cfg.read_bytes()
    opener = install_not_set_up(monkeypatch, tmp_dir, state=Credentials("t", "1"))
    assert main(["install"]) == 0
    assert cfg.read_bytes() == before and opener.calls == []
    out = capsys.readouterr().out
    assert cli.SETUP_HINT not in out and cli.WEB_TURNED_ON not in out


def test_install_with_a_keychain_error_opens_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read error is never taken for a fresh install (a locked Keychain at login
    would otherwise show the wizard to a user who has a bot)."""
    cfg = write_config(tmp_dir)
    before = cfg.read_bytes()
    opener = install_not_set_up(monkeypatch, tmp_dir)

    def locked() -> Credentials | Missing:
        raise SecretStoreError("keychain command 'find-generic-password' timed out after 10s")

    monkeypatch.setattr(cli, "_credential_state", locked)
    assert main(["install"]) == 0
    assert cfg.read_bytes() == before and opener.calls == []
    out = capsys.readouterr().out
    assert "Keychain error: keychain command 'find-generic-password' timed out" in out
    assert (
        "Telegram setup was not checked; if it isn't set up, run `diffbell web` to "
        "open the setup page, or `diffbell setup`."
    ) in out
    assert cli.WEB_TURNED_ON not in out


@pytest.mark.parametrize(
    "error",
    [
        "The web UI did not answer on http://127.0.0.1:8765 within 15s; see x.log",
        "Port 8765 is served by another process; refusing to send a login link.",
        "Could not open the browser.",
    ],
)
def test_install_with_a_failed_open_prints_a_hint_and_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_dir: Path,
    capsys: pytest.CaptureFixture[str],
    error: str,
) -> None:
    write_config(tmp_dir)
    install_not_set_up(monkeypatch, tmp_dir, opener=Opener(error))
    assert main(["install"]) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == [error, cli.OPEN_HINT]
    assert cli.OPEN_HINT == "Run `diffbell web` to open the setup page."


def test_install_reads_the_keychain_before_launchd(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    write_config(tmp_dir)
    install_not_set_up(monkeypatch, tmp_dir)
    order: list[str] = []
    fake_install = launchd.install

    def state() -> Credentials | Missing:
        order.append("keychain")
        return Missing("both")

    def install(executable: Path, *, log_dir: Path) -> Path:
        order.append("launchd")
        return fake_install(executable, log_dir=log_dir)

    monkeypatch.setattr(cli, "_credential_state", state)
    monkeypatch.setattr(launchd, "install", install)
    assert main(["install"]) == 0
    assert order == ["keychain", "launchd"]


def test_install_that_never_gets_ready_opens_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    cfg = write_config(tmp_dir)
    before = cfg.read_bytes()
    opener = install_not_set_up(
        monkeypatch, tmp_dir, status=ready_status(last_started_at=INSTALL_NOW - 3600)
    )
    assert main(["install"]) == 1
    assert opener.calls == [] and cfg.read_bytes() == before


def test_install_survives_a_store_that_cannot_open(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real _open_page: a database error after a good install is a hint, not a
    traceback, and never names a path or a token."""
    config_with_web(tmp_dir, True)
    real_open_page = cli._open_page
    install_not_set_up(monkeypatch, tmp_dir)
    monkeypatch.setattr(cli, "_open_page", real_open_page)
    monkeypatch.setattr(cli, "_wait_for_our_server", lambda base, state, wait: bytes(32))

    def broken_store(path: Path) -> Store:
        raise sqlite3.OperationalError(f"unable to open database file {path}")

    monkeypatch.setattr(cli, "Store", broken_store)
    assert main(["install"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2:] == ["Could not create a login link (OperationalError).", cli.OPEN_HINT]
    assert "state.db" not in lines[-2]


def test_install_with_a_config_that_cannot_be_updated_prints_the_setup_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_dir)  # web.enabled unstated: install would turn it on
    opener = install_not_set_up(monkeypatch, tmp_dir)

    def conflict(path: Path, enabled: bool) -> bool:
        raise config_edit.ConfigConflict("the config changed on disk; try again")

    monkeypatch.setattr(config_edit, "set_web_enabled", conflict)
    assert main(["install"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == cli.SETUP_HINT and opener.calls == []
    assert cli.WEB_TURNED_ON not in out


def test_uninstall_reports_removal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launchd, "uninstall", lambda: True)
    assert main(["uninstall"]) == 0
    assert "Stopped and removed" in capsys.readouterr().out


def test_uninstall_when_not_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launchd, "uninstall", lambda: False)
    assert main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert "diffbell agent was not installed." in out and "Stopped" not in out


# -- run: keychain retries ---------------------------------------------------------


class FlakyKeychain:
    """Fake get_secret: the first ``failures`` calls time out, then values are returned."""

    def __init__(self, failures: int, value: str | None = "x") -> None:
        self.failures = failures
        self.value = value
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, account: str, **kwargs: Any) -> str | None:
        self.calls.append((account, kwargs))
        if len(self.calls) <= self.failures:
            raise SecretStoreError("keychain command 'find-generic-password' timed out after 30s")
        return None if account == ACCOUNT_SAVE_PENDING else self.value


class Records(logging.Handler):
    """Collects diffbell records (run's configure_logging replaces root handlers,
    which would detach caplog)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def records() -> Iterator[Records]:
    handler = Records()
    logger = logging.getLogger("diffbell")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def keychain_and_sleeps(monkeypatch: pytest.MonkeyPatch, keychain: FlakyKeychain) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(cli, "get_secret", keychain)
    monkeypatch.setattr(cli, "_sleep", sleeps.append)
    return sleeps


def test_run_retries_a_slow_keychain_then_starts(
    monkeypatch: pytest.MonkeyPatch, records: Records
) -> None:
    keychain = FlakyKeychain(failures=1)
    sleeps = keychain_and_sleeps(monkeypatch, keychain)
    started: list[Credentials | Missing] = []

    async def fake_daemon(state: Credentials | Missing) -> int:
        started.append(state)
        return 0

    monkeypatch.setattr(cli, "_run_daemon", fake_daemon)
    assert main(["run"]) == 0
    assert started == [Credentials("x", "x")]
    assert sleeps == [5]
    assert all(kwargs.get("timeout") == 30 for _, kwargs in keychain.calls)
    assert [r.levelname for r in records.records if "attempt" in r.getMessage()] == ["WARNING"]


def test_run_gives_up_after_three_keychain_failures(
    monkeypatch: pytest.MonkeyPatch, records: Records
) -> None:
    keychain = FlakyKeychain(failures=100)
    sleeps = keychain_and_sleeps(monkeypatch, keychain)
    assert main(["run"]) == 2
    assert len(keychain.calls) == 3  # one (failing) read per attempt
    assert sleeps == [5, 5]
    levels = [r.levelname for r in records.records]
    assert levels.count("WARNING") == 3 and levels[-1] == "ERROR"


def test_run_does_not_retry_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    keychain = FlakyKeychain(failures=0, value=None)
    sleeps = keychain_and_sleeps(monkeypatch, keychain)
    started: list[Credentials | Missing] = []

    async def fake_daemon(state: Credentials | Missing) -> int:
        started.append(state)
        return 0

    monkeypatch.setattr(cli, "_run_daemon", fake_daemon)
    assert main(["run"]) == 0  # not set up: the daemon starts holding
    assert started == [Missing("both")]
    assert sleeps == []
    assert len(keychain.calls) == 3  # marker, bot token and chat id, read once each


def test_other_commands_keep_the_default_keychain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keychain = FlakyKeychain(failures=0, value=None)
    monkeypatch.setattr(cli, "get_secret", keychain)
    assert main(["test"]) == 2
    assert all("timeout" not in kwargs for _, kwargs in keychain.calls)


# -- entrypoint: hard exit for run ---------------------------------------------------


class HardExit(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def record_exit(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace os._exit and logging.shutdown with recorders; _hard_exit raises HardExit."""
    events: list[str] = []

    def fake_hard_exit(code: int) -> None:
        events.append(f"exit {code}")
        raise HardExit(code)

    monkeypatch.setattr(cli, "_hard_exit", fake_hard_exit)
    monkeypatch.setattr(logging, "shutdown", lambda: events.append("logging.shutdown"))
    return events


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_entrypoint_hard_exits_run_with_its_code(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    events = record_exit(monkeypatch)
    monkeypatch.setitem(cli.COMMANDS, "run", lambda args: code)
    with pytest.raises(HardExit) as exc:
        cli.entrypoint(["run"])
    assert exc.value.code == code
    assert events == ["logging.shutdown", f"exit {code}"]


def test_entrypoint_logs_an_unexpected_run_error_then_hard_exits_1(
    monkeypatch: pytest.MonkeyPatch, records: Records
) -> None:
    events = record_exit(monkeypatch)

    def boom(args: Any) -> int:
        raise ValueError("kaboom")

    monkeypatch.setitem(cli.COMMANDS, "run", boom)
    with pytest.raises(HardExit) as exc:
        cli.entrypoint(["run"])
    assert exc.value.code == 1
    assert events == ["logging.shutdown", "exit 1"]
    errors = [r for r in records.records if r.levelname == "ERROR"]
    assert errors and errors[-1].exc_info is not None
    assert "kaboom" in str(errors[-1].exc_info[1])


def test_entrypoint_keeps_normal_exit_for_other_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    events = record_exit(monkeypatch)
    monkeypatch.setitem(cli.COMMANDS, "status", lambda args: 1)
    with pytest.raises(SystemExit) as exc:
        cli.entrypoint(["status"])
    assert exc.value.code == 1
    assert events == []


def live_snapshot(web: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"pid": 4242, "running": True, "updated_at": time.time(), "web": web, **extra}


@pytest.mark.parametrize(
    ("web", "line"),
    [
        (
            {"enabled": True, "running": True, "port": 8765, "last_error": None},
            "web:      running on http://127.0.0.1:8765",
        ),
        (
            {"enabled": True, "running": False, "port": 8765, "last_error": "OSError: in use"},
            "web:      down (OSError: in use)",
        ),
        (
            {"enabled": False, "running": False, "port": 8765, "last_error": None},
            "web:      disabled (enable with `diffbell web`)",
        ),
    ],
)
def test_status_report_shows_the_web_block(web: dict[str, Any], line: str) -> None:
    text = status_report(
        daemon_pid=4242,
        watchman_version="2026.1",
        telegram="ok",
        projects=[],
        config_error=None,
        snapshot=live_snapshot(web),
        counts={},
        failures=[],
    )
    assert line in text.splitlines()


def test_status_takes_the_watchman_version_from_a_live_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("projects: []\n")
    state = tmp_dir / "state"
    state.mkdir()
    snap = live_snapshot(
        {"enabled": False, "running": False, "port": 8765, "last_error": None},
        pid=os.getpid(),
        watchman_version="2026.10.05.00",
    )
    (state / "status.json").write_text(json.dumps(snap))

    def no_subprocess() -> str | None:
        raise AssertionError("the live snapshot already has the version")

    monkeypatch.setattr(cli, "_watchman_version", no_subprocess)
    telegram_says(monkeypatch, "@bot ok")
    with singleton_lock(state / "daemon.lock"):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "watchman: 2026.10.05.00" in out
    assert "web:      disabled" in out


def test_status_report_never_shows_the_web_secret() -> None:
    secret = "ab" * 32
    web = {
        "enabled": True,
        "running": True,
        "port": 8765,
        "last_error": None,
        "secret": secret,
    }
    text = status_report(
        daemon_pid=4242,
        watchman_version="2026.1",
        telegram="ok",
        projects=[],
        config_error=None,
        snapshot=live_snapshot(web),
        counts={},
        failures=[],
    )
    assert "web:      running on http://127.0.0.1:8765" in text.splitlines()
    assert secret not in text and secret[:16] not in text


def test_watchman_version_never_answers_from_the_client_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status` must not report a version while no server runs: with --no-local the
    client exits 1 instead of printing its own version."""
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert cli._watchman_version() is None
    assert calls == [["watchman", "--no-spawn", "--no-local", "version"]]


def test_status_syncs_the_projects_like_the_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    """Stable project ids spec section 2: `status` opens the Store, so it syncs too."""
    proj = tmp_dir / "proj"
    proj.mkdir()
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"projects:\n  - path: {proj}\n    name: demo\n")
    (tmp_dir / "state").mkdir()
    Store(tmp_dir / "state" / "state.db").close()
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "no credentials")
    assert main(["status"]) == 0
    raw = sqlite3.connect(tmp_dir / "state" / "state.db")
    rows = raw.execute("SELECT name, path, active FROM projects").fetchall()
    raw.close()
    assert rows == [("demo", str(proj), 1)]


def test_a_config_the_daemon_would_reject_is_not_synced(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    config = Config.model_validate({"projects": [{"path": "relative/x"}]})
    cli._sync_projects(store, config)
    assert store._db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    store.close()


def has_project_ids(db: Path) -> bool:
    raw = sqlite3.connect(db)
    try:
        return any(row[1] == "project_id" for row in raw.execute("PRAGMA table_info(events)"))
    finally:
        raw.close()


def test_status_never_migrates_under_a_running_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-v6 database while the (older) daemon runs: opening the Store would
    migrate it under that daemon, which then fails every per-project query."""
    proj = tmp_dir / "proj"
    proj.mkdir()
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"projects:\n  - path: {proj}\n    name: demo\n")
    state = tmp_dir / "state"
    state.mkdir()
    make_v5(state / "state.db")
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "no credentials")

    def no_store(path: Path) -> Store:
        raise AssertionError("status must not open the Store")

    monkeypatch.setattr(cli, "Store", no_store)
    with singleton_lock(state / "daemon.lock"):
        assert main(["status"]) == 1  # broken until the daemon restarts, as doctor says
    out = capsys.readouterr().out
    assert "daemon:   running (pid " in out
    assert "pending 0" not in out
    assert "the database needs migrating: restart the daemon (`diffbell install`)" in out
    assert not has_project_ids(state / "state.db")
    assert not list(state.glob("state-pre-v6-*.db"))


def test_status_without_a_daemon_still_migrates(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = tmp_dir / "proj"
    proj.mkdir()
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"projects:\n  - path: {proj}\n    name: demo\n")
    state = tmp_dir / "state"
    state.mkdir()
    make_v5(state / "state.db")
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "no credentials")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "needs migrating" not in out and "pending 1 · sent 18" in out
    assert has_project_ids(state / "state.db")


def test_status_carries_on_when_the_sync_finds_the_database_locked(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Locked past the busy timeout (the daemon mid-write): no sync, no alarm."""
    proj = tmp_dir / "proj"
    proj.mkdir()
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"projects:\n  - path: {proj}\n    name: demo\n")
    (tmp_dir / "state").mkdir()
    Store(tmp_dir / "state" / "state.db").close()
    monkeypatch.setattr(cli, "_watchman_version", lambda: None)
    telegram_says(monkeypatch, "no credentials")

    def locked(self: Store, projects: Any, now: float) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "sync_projects", locked)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pending 0 · sent 0 · failed 0" in out
    assert "locked" not in out and "Traceback" not in out


def test_install_without_watchman_refuses_before_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from diffbell import rename_install

    monkeypatch.setattr(launchd, "find_watchman", lambda: None)
    monkeypatch.setattr(rename_install, "for_install", lambda: pytest.fail("the move ran"))
    monkeypatch.setattr(launchd, "install", lambda *a, **k: pytest.fail("installed"))
    code = main(["install"])
    out = capsys.readouterr().out
    assert code == 2
    folders = ", ".join(launchd.LAUNCHD_PATH.split(":"))
    assert out.strip() == (
        f"Watchman isn't installed where the agent looks ({folders}): "
        "`brew install watchman`, then run `diffbell install` again."
    )
    assert not (tmp_dir / "cfg" / "config.yaml").exists()  # nothing written
