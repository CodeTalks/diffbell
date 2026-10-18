"""Daemon wiring with fakes: no Watchman (the connection never comes up), no Telegram."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

import pytest

from diffbell import __version__, selfupdate
from diffbell import config as config_mod
from diffbell import daemon as daemon_mod
from diffbell import secrets as secrets_mod
from diffbell.daemon import Daemon
from diffbell.fingerprint import Observation
from diffbell.health import SETUP_KEY, UPDATE_KEY, Health
from diffbell.liveness import HEARTBEAT_KEY
from diffbell.models import ChangeKind
from diffbell.notifier import Notifier
from diffbell.pausing import HELD_FOREVER, Pause, QuietHours, pause_until
from diffbell.secrets import Missing
from diffbell.sender_slot import SenderSlot
from diffbell.store import TELEGRAM_SAVED_AT, ClockState, Problem, Snooze, Store
from diffbell.telegram import ErrorKind, TelegramError
from diffbell.watcher import Watcher
from diffbell.web import config_edit
from tests.fakes import FakeSender, FakeWatchmanClient, VersionedWatchmanClient, fake_slot
from tests.helpers import sync_names


def no_watchman() -> NoReturn:
    raise OSError("watchman is not running (test)")


def make_daemon(
    tmp_dir: Path, sender: FakeSender | None = None, **options: Any
) -> tuple[Daemon, Path]:
    proj = tmp_dir / "proj"
    proj.mkdir()
    config = tmp_dir / "cfg" / "config.yaml"
    config.parent.mkdir()
    config.write_text(f"projects:\n  - path: {proj}\n")
    state = tmp_dir / "state"
    store = Store(state / "state.db")
    d = Daemon(
        config,
        store,
        fake_slot(sender),
        state_dir=state,
        wm_factory=no_watchman,
        recv_timeout=0.05,
        maintenance_interval=0.02,
        **options,
    )
    return d, state / "status.json"


async def wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        assert asyncio.get_running_loop().time() < deadline, f"{path} never appeared"
        await asyncio.sleep(0.01)


async def test_clean_shutdown_writes_a_not_running_snapshot(tmp_dir: Path) -> None:
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    assert json.loads(status.read_text())["running"] is True
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    snapshot = json.loads(status.read_text())
    assert snapshot["running"] is False
    assert "pid" in snapshot and "updated_at" in snapshot


class NewInstall:
    """A SelfUpdate stand-in: a newer version appears after `after` checks."""

    def __init__(self, after: int = 2, fail: bool = False) -> None:
        self.after, self.fail, self.checks = after, fail, 0

    def __call__(self, running: str) -> NewInstall:
        return self

    def check(self, now: float) -> str | None:
        self.checks += 1
        if self.fail:
            raise OSError("plist unreadable")
        return "9.9.9" if self.checks > self.after else None


async def test_a_newer_install_stops_the_daemon_cleanly(tmp_dir: Path) -> None:
    """ADR 0022: the daemon stops by itself; launchd's KeepAlive starts the new one."""
    watch = NewInstall()
    d, status = make_daemon(tmp_dir, self_update=watch)
    await asyncio.wait_for(d.run(asyncio.Event()), timeout=10)  # nobody sets stop
    assert watch.checks == 3
    assert json.loads(status.read_text())["running"] is False  # a normal stop


async def test_a_failing_install_check_never_stops_the_daemon(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    watch = NewInstall(fail=True)
    d, _ = make_daemon(tmp_dir, self_update=watch)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    while watch.checks < 3:
        await asyncio.sleep(0.01)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert "maintenance: checking for a newer install failed" in caplog.text


@pytest.mark.usefixtures("nameless_mac")
async def test_a_start_after_a_version_change_sends_the_update_notice_silently(
    tmp_dir: Path,
) -> None:
    sender = FakeSender()
    d, _ = make_daemon(tmp_dir, sender, self_update=NewInstall(after=10**9))
    d._store.kv_set(selfupdate.RUNNING_VERSION_KEY, "0.0.1")
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    text = f"⬆️ Diffbell updated: 0.0.1 → {__version__}"
    while text not in [t for t, _ in sender.sent]:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert sender.silent[[t for t, _ in sender.sent].index(text)] is True
    assert d._store.kv_get(selfupdate.RUNNING_VERSION_KEY) == __version__
    assert d._store.get_problem(UPDATE_KEY) is None  # sent once, then gone


async def test_a_start_names_the_mac_in_its_notices(tmp_dir: Path) -> None:
    """ADR 0023: resolved at start (the belt's Computer Name), before the notices."""
    sender = FakeSender()
    d, _ = make_daemon(tmp_dir, sender, self_update=NewInstall(after=10**9))
    d._store.kv_set(selfupdate.RUNNING_VERSION_KEY, "0.0.1")
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    text = f"⬆️ Diffbell (Test Mac) updated: 0.0.1 → {__version__}"
    while text not in [t for t, _ in sender.sent]:
        await asyncio.sleep(0.01)
    assert d.machine_name == "Test Mac"
    assert d._notifier_settings().machine == "Test Mac"
    stop.set()
    await asyncio.wait_for(task, timeout=10)


async def test_every_reload_resolves_the_mac_again(tmp_dir: Path) -> None:
    sender = FakeSender()
    d, _ = make_daemon(tmp_dir, sender)
    stubbed(d, StubWatcher())
    base = d._config_path.read_text()
    seen = []
    for notify in ('notify:\n  machine: "Work  MBP"\n', 'notify:\n  machine: ""\n', ""):
        d._config_path.write_text(base + notify)
        await d._reload()
        settings = d._notifier_settings()
        d._health.open("watchman", "serious", "it broke")
        await d._health.tick(d._clock())
        d._health.forget("watchman")
        seen.append((d.machine_name, settings.machine, sender.sent[-1][0]))
    assert seen == [
        ("Work MBP", "Work MBP", "⚠️ Diffbell (Work MBP): it broke"),
        (None, None, "⚠️ Diffbell: it broke"),
        ("Test Mac", "Test Mac", "⚠️ Diffbell (Test Mac): it broke"),
    ]
    assert d._heartbeat.text(d._clock()).startswith("✅ Diffbell (Test Mac) · watching")


async def test_one_failing_maintenance_step_does_not_skip_the_others(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken_retry(self: Notifier) -> None:
        raise RuntimeError("retry boom")

    ticks: list[int] = []

    async def counting_tick(self: Watcher) -> None:
        ticks.append(1)

    monkeypatch.setattr(Notifier, "retry_due", broken_retry)
    monkeypatch.setattr(Watcher, "tick", counting_tick)
    monkeypatch.setattr(daemon_mod, "PRUNE_EVERY", 0.0)
    pruned: list[float] = []
    d, status = make_daemon(tmp_dir)
    monkeypatch.setattr(d._store, "prune", pruned.append)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    while len(ticks) < 2 or len(pruned) < 2:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert "maintenance: retrying due events failed" in caplog.text


async def test_failing_status_write_still_ticks(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ticks: list[int] = []

    async def counting_tick(self: Watcher) -> None:
        ticks.append(1)

    def broken_write(self: Daemon, watcher: Watcher, *, running: bool = True) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Watcher, "tick", counting_tick)
    monkeypatch.setattr(Daemon, "_write_status", broken_write)
    d, _ = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    while len(ticks) < 3:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert "maintenance: writing status.json failed" in caplog.text
    assert "writing the final status snapshot failed" in caplog.text


@pytest.mark.parametrize("outcome", ["raise", "return"])
async def test_dead_background_task_stops_the_daemon_with_an_error(
    tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    async def dying_pump(self: Daemon, watcher: Any, inbox: Any) -> None:
        await asyncio.sleep(0.05)
        if outcome == "raise":
            raise ValueError("pump boom")

    monkeypatch.setattr(Daemon, "_pump", dying_pump)
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as exc:
        await asyncio.wait_for(d.run(stop), timeout=10)
    assert str(exc.value).startswith("background task pump stopped: ")
    if outcome == "raise":
        assert "ValueError: pump boom" in str(exc.value)
        assert "pump boom" in caplog.text
    else:
        assert "returned unexpectedly" in str(exc.value)
    assert stop.is_set()
    assert json.loads(status.read_text())["running"] is False


def make_two_project_daemon(tmp_dir: Path, sender: FakeSender) -> tuple[Daemon, Path, Path]:
    proj, gone = tmp_dir / "proj", tmp_dir / "gone"
    proj.mkdir()
    config = tmp_dir / "cfg" / "config.yaml"
    config.parent.mkdir()
    config.write_text(f"projects:\n  - path: {proj}\n  - path: {gone}\n")
    state = tmp_dir / "state"
    d = Daemon(
        config,
        Store(state / "state.db"),
        fake_slot(sender),
        state_dir=state,
        wm_factory=no_watchman,
        recv_timeout=0.05,
        maintenance_interval=0.02,
        missing_recheck_interval=0.05,
    )
    return d, state / "status.json", gone


def status_rows(status: Path) -> dict[str, dict[str, Any]]:
    try:
        snapshot = json.loads(status.read_text())
    except (OSError, ValueError):
        return {}
    return {row["name"]: row for row in snapshot["projects"]}


async def wait_until(pred: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


def problem_keys(d: Daemon) -> list[str]:
    return [p.key for p in d._health.problems()]


async def test_missing_project_is_skipped_reported_and_picked_up_when_it_appears(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sender = FakeSender()
    d, status, gone = make_two_project_daemon(tmp_dir, sender)
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(d.run(stop))
        await wait_until(lambda: "gone" in status_rows(status))
        rows = status_rows(status)
        assert rows["gone"]["status"] == "missing"
        assert rows["gone"]["path"] == str(gone)
        assert rows["proj"]["status"] != "missing"
        [missing] = [p for p in d._health.problems() if p.key == "missing:gone"]
        # Advice, not a shell command: it is in the message; `fix` is only ever copied
        # to a terminal.
        assert (missing.severity, missing.project, missing.fix) == ("warning", "gone", None)
        assert missing.message == (
            f"project gone: path {gone} does not exist; skipped until it reappears. "
            "Reconnect the drive or fix the path"
        )
        await asyncio.sleep(0.3)  # periodic re-checks while it is still missing
        assert not any("gone" in t for t, _ in sender.sent)  # a warning is page-only

        gone.mkdir()
        await wait_until(lambda: status_rows(status).get("gone", {}).get("status") == "pending")
        assert "missing:gone" not in problem_keys(d)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert f"project gone: path {gone} does not exist" in caplog.text
    assert "project gone: path reappeared" in caplog.text
    assert not any("gone" in t for t, _ in sender.sent)


class StubWatcher:
    """Just enough of a Watcher for the reload / missing-path logic."""

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.statuses = statuses or {}
        self.reconciled: list[list[str]] = []
        self.connected = False

    async def reconcile(self, projects: list[Any]) -> None:
        self.reconciled.append([p.name for p in projects])

    def status(self) -> list[dict[str, Any]]:
        return [{"name": n, "status": s} for n, s in self.statuses.items()]


def stubbed(d: Daemon, watcher: StubWatcher) -> StubWatcher:
    d._watcher = watcher  # type: ignore[assignment]
    return watcher


async def test_a_missing_folder_back_across_a_restart_is_resolved_at_start(
    tmp_dir: Path,
) -> None:
    """A problem saved before a restart that has cleared since is resolved by the
    first check after the start, not left open forever."""
    sender = FakeSender()
    d, _, gone = make_two_project_daemon(tmp_dir, sender)
    stubbed(d, StubWatcher())
    d._health.open("missing:gone", "warning", "project gone: path gone", project="gone")
    gone.mkdir()  # back while the daemon was down; the new one starts with no memory
    await d._reload()
    assert problem_keys(d) == []
    await d._health.tick(d._clock())
    assert sender.sent == []  # a warning resolves silently


async def test_a_reappeared_project_resolves_its_warning_silently(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sender = FakeSender()
    d, _, gone = make_two_project_daemon(tmp_dir, sender)
    # Missing from the start: never watched, so no lost watch (only the warning).
    watcher = stubbed(d, StubWatcher({"proj": "subscribed", "gone": "pending"}))
    await d._reload()
    assert problem_keys(d) == ["missing:gone"]
    gone.mkdir()
    with caplog.at_level(logging.INFO, logger="diffbell.daemon"):
        await d._reload(recheck=True)
    assert watcher.reconciled[-1] == ["proj", "gone"]
    assert problem_keys(d) == []
    await d._health.tick(d._clock())
    assert sender.sent == []
    assert "project gone: path reappeared" in caplog.text


@pytest.mark.parametrize("status", ["subscribed", "error"])
@pytest.mark.usefixtures("nameless_mac")
async def test_a_watched_folder_gone_at_a_reload_is_a_lost_watch(
    tmp_dir: Path, status: str
) -> None:
    """The reload unsubscribes a project whose folder is gone before the watcher's
    tick can notice: the daemon records the lost watch itself, so its ⚠️ is not
    swallowed. The watcher's subscribe, once the folder is back, resolves it."""
    sender = FakeSender()
    d, _, gone = make_two_project_daemon(tmp_dir, sender)
    gone.mkdir()
    stubbed(d, StubWatcher({"proj": "subscribed", "gone": status}))
    await d._reload()
    assert problem_keys(d) == []
    gone.rmdir()
    await d._reload()
    lost = [p for p in d._health.problems() if p.key == "project:gone"]
    assert [(p.severity, p.project, p.message) for p in lost] == [
        ("serious", "gone", "project gone: watch lost: project folder removed")
    ]
    await d._reload(recheck=True)  # still missing: nothing new
    await d._health.tick(d._clock())
    assert [t for t, _ in sender.sent] == [
        "⚠️ Diffbell: project gone: watch lost: project folder removed"
    ]


async def test_a_folder_missing_while_pending_is_no_lost_watch(tmp_dir: Path) -> None:
    sender = FakeSender()
    d, _, gone = make_two_project_daemon(tmp_dir, sender)
    gone.mkdir()
    stubbed(d, StubWatcher({"proj": "subscribed", "gone": "pending"}))
    await d._reload()
    gone.rmdir()
    await d._reload()
    assert problem_keys(d) == ["missing:gone"]


def test_the_telegram_warning_opens_and_resolves_from_the_outbox(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, _ = make_daemon(tmp_dir)
    outbox: list[tuple[float | None, str | None]] = [(None, "sendMessage: 502")]
    monkeypatch.setattr(d._store, "telegram_health", lambda: outbox[-1])
    d._check_telegram()
    [problem] = d._health.problems()
    assert (problem.key, problem.severity, problem.message) == (
        "telegram",
        "warning",
        "Telegram failing: sendMessage: 502",
    )
    outbox.append((1.0, None))
    d._check_telegram()
    assert d._health.problems() == []
    assert d._store.all_problems() == []  # a warning resolves without a message


async def test_a_failing_telegram_check_does_not_skip_the_alert_tick(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken_check(self: Daemon) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    ticks: list[float] = []

    async def counting_tick(self: Any, now: float, *, sending: bool = True) -> None:
        ticks.append(now)

    monkeypatch.setattr(Daemon, "_check_telegram", broken_check)
    monkeypatch.setattr(Health, "tick", counting_tick)
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    try:
        await wait_until(lambda: len(ticks) >= 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert "maintenance: checking Telegram's health failed" in caplog.text


async def test_a_removed_project_drops_its_problems_without_a_message(tmp_dir: Path) -> None:
    sender = FakeSender()
    d, _, _ = make_two_project_daemon(tmp_dir, sender)
    stubbed(d, StubWatcher())
    d._health.open("project:proj", "serious", "project proj: watch lost", project="proj")
    await d._reload()
    assert sorted(problem_keys(d)) == ["missing:gone", "project:proj"]
    d._config_path.write_text("projects: []\n")
    await d._reload()
    assert problem_keys(d) == []
    await d._health.tick(d._clock())
    assert sender.sent == []


@pytest.mark.usefixtures("nameless_mac")
async def test_a_rejected_config_is_a_serious_problem_until_a_valid_one(tmp_dir: Path) -> None:
    sender = FakeSender()
    d, _, _ = make_two_project_daemon(tmp_dir, sender)
    stubbed(d, StubWatcher())
    good = d._config_path.read_text()
    d._config_path.write_text("debounce: [not, a, mapping]\n")
    await d._reload()
    [problem] = d._health.problems()
    assert (problem.key, problem.severity) == ("config", "serious")
    assert problem.message.startswith("config rejected, keeping the last good config: ")
    await d._health.tick(d._clock())
    d._config_path.write_text(good)
    await d._reload()
    await d._health.tick(d._clock())
    assert [t.split(" (")[0] for t, _ in sender.sent] == [
        f"⚠️ Diffbell: {problem.message}".split(" (")[0],
        "✅ Diffbell: the config is valid again",
    ]


async def test_recheck_of_an_unchanged_invalid_config_does_not_report_again(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sender = FakeSender()
    d, _, _ = make_two_project_daemon(tmp_dir, sender)
    stubbed(d, StubWatcher())
    d._config_path.write_text("debounce: [not, a, mapping]\n")

    def rejections() -> int:
        return sum(
            r.levelno >= logging.WARNING and "config rejected" in r.getMessage()
            for r in caplog.records
        )

    with caplog.at_level(logging.DEBUG, logger="diffbell"):
        await d._reload()  # the self-watch reload rejects it and opens a problem
        assert rejections() == 2  # the error log + the problem's own log line
        await d._reload(recheck=True)
        await d._reload(recheck=True)
        assert rejections() == 2  # unchanged file: debug only, no new problem
        assert any(
            r.levelno == logging.DEBUG and "config rejected" in r.getMessage()
            for r in caplog.records
        )
        # A different invalid file, with a different error, is reported again. (One
        # with the same error words the same problem: only the error log repeats.)
        d._config_path.write_text("nope: 1\n")
        await d._reload(recheck=True)
        assert rejections() == 4


async def test_snapshot_has_watchman_version_and_web_block(tmp_dir: Path) -> None:
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    snapshot = json.loads(status.read_text())
    assert snapshot["watchman_version"] is None  # never connected (no Watchman here)
    assert snapshot["web"] == {
        "enabled": False,
        "running": False,
        "port": 8765,
        "last_error": None,
        "secret": None,
    }
    live = d.status_snapshot()
    assert live["running"] is True and live["web"] == snapshot["web"]
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert status.stat().st_mode & 0o777 == 0o600


async def test_snapshot_reports_the_version_after_connected(tmp_dir: Path) -> None:
    proj = tmp_dir / "proj"
    proj.mkdir()
    config = tmp_dir / "cfg" / "config.yaml"
    config.parent.mkdir()
    config.write_text(f"projects:\n  - path: {proj}\n")
    fakes: list[VersionedWatchmanClient] = []

    def factory() -> VersionedWatchmanClient:
        fakes.append(VersionedWatchmanClient())
        return fakes[-1]

    d = Daemon(
        config,
        Store(tmp_dir / "state" / "state.db"),
        fake_slot(),
        state_dir=tmp_dir / "state",
        wm_factory=factory,
        recv_timeout=0.05,
        maintenance_interval=0.02,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_until(
        lambda: d.status_snapshot()["watchman_version"] == VersionedWatchmanClient.VERSION
    )
    # The version came from the connect-time query only.
    assert sum(q == ("version",) for q in fakes[0].queries) == 1
    stop.set()
    await asyncio.wait_for(task, timeout=10)


async def test_status_json_is_rewritten_once_connected_and_subscribed(tmp_dir: Path) -> None:
    """`install` waits for status.json to say connected with no project pending: the
    daemon publishes it when the connection comes up, not at the next maintenance
    tick (an hour away here)."""
    proj = tmp_dir / "proj"
    proj.mkdir()
    config = tmp_dir / "cfg" / "config.yaml"
    config.parent.mkdir()
    config.write_text(f"projects:\n  - path: {proj}\n")
    go = threading.Event()

    def factory() -> FakeWatchmanClient:
        go.wait(10)  # the connection comes up only after the first snapshots
        return FakeWatchmanClient()

    d = Daemon(
        config,
        Store(tmp_dir / "state" / "state.db"),
        fake_slot(),
        state_dir=tmp_dir / "state",
        wm_factory=factory,
        recv_timeout=0.05,
        maintenance_interval=3600,
    )
    status = tmp_dir / "state" / "status.json"
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_for_path(status)
        await asyncio.sleep(0.1)  # the start's own writes and the first tick are done
        first = json.loads(status.read_text())
        assert first["connected"] is False
        assert [p["status"] for p in first["projects"]] == ["pending"]
        go.set()

        def published() -> bool:
            snap = json.loads(status.read_text())
            return snap["connected"] is True and all(
                p["status"] != "pending" for p in snap["projects"]
            )

        await wait_until(published)
    finally:
        go.set()
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_status_tmp_is_owner_only_before_anything_is_written(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover status.json.tmp from an older version may be world-readable:
    narrow it right after opening, before the snapshot (with the web secret) lands."""
    d, status = make_daemon(tmp_dir)
    tmp = status.parent / "status.json.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("{}")
    tmp.chmod(0o644)
    modes: list[int] = []
    real_fdopen = os.fdopen

    def recording_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        modes.append(os.fstat(fd).st_mode & 0o777)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", recording_fdopen)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert modes and set(modes) == {0o600}


async def test_reload_records_the_applied_and_rejected_config_versions(tmp_dir: Path) -> None:
    sender = FakeSender()
    d, _, _ = make_two_project_daemon(tmp_dir, sender)
    stubbed(d, StubWatcher())
    good = d._config_path.read_bytes() + b"# edited\n"
    d._config_path.write_bytes(good)
    await d._reload()
    applied = config_edit.version_of(good)
    # The same hash the editor returns for these bytes.
    assert applied == config_edit.config_version(d._config_path)
    snap = d.status_snapshot()
    assert (snap["config_version"], snap["config_rejected_version"]) == (applied, None)
    bad = b"debounce: [not, a, mapping]\n"
    d._config_path.write_bytes(bad)
    await d._reload()
    snap = d.status_snapshot()
    assert snap["config_version"] == applied  # still the last good one
    assert snap["config_rejected_version"] == config_edit.version_of(bad)
    fixed = good + b"# fixed\n"
    d._config_path.write_bytes(fixed)
    await d._reload()
    snap = d.status_snapshot()
    # A later good reload clears the rejection: it no longer describes the file.
    assert (snap["config_version"], snap["config_rejected_version"]) == (
        config_edit.version_of(fixed),
        None,
    )


async def test_a_file_rewritten_while_it_is_parsed_records_no_version(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bytes parsed are not the bytes on disk any more: neither version is known,
    so the recorded one stays the previous (the next reload records its own)."""
    d, _, _ = make_two_project_daemon(tmp_dir, FakeSender())
    stubbed(d, StubWatcher())
    good = d._config_path.read_bytes() + b"# edited\n"
    d._config_path.write_bytes(good)
    await d._reload()
    applied = config_edit.version_of(good)
    assert d.status_snapshot()["config_version"] == applied
    original = config_mod.load_and_resolve

    def rewriting(path: Path) -> Any:
        result = original(path)
        path.write_bytes(good + b"# rewritten mid-parse\n")
        return result

    monkeypatch.setattr(daemon_mod, "load_and_resolve", rewriting)
    d._config_path.write_bytes(good + b"# second edit\n")
    before = d._file_version()
    assert before is not None
    assert d._stable_version(before) == before  # unchanged bytes: the version stands
    await d._reload()
    assert d._stable_version(before) is None  # the file changed after it was parsed
    snap = d.status_snapshot()
    assert (snap["config_version"], snap["config_rejected_version"]) == (applied, None)


async def test_startup_records_the_applied_config_version(tmp_dir: Path) -> None:
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    snapshot = json.loads(status.read_text())
    assert snapshot["config_version"] == config_edit.config_version(d._config_path)
    assert snapshot["config_rejected_version"] is None
    stop.set()
    await asyncio.wait_for(task, timeout=10)


def test_notifier_settings_carry_the_link_port_and_the_quiet_hours(tmp_dir: Path) -> None:
    d, _ = make_daemon(tmp_dir)
    off = d._notifier_settings()
    assert (off.web_enabled, off.web_port, off.quiet) == (False, None, None)
    d._config = config_mod.Config.model_validate(
        {"web": {"enabled": True, "port": 9000}, "notify": {"quiet_hours": "23:00-08:00"}}
    )
    # Enabled in the config, but the server is not running (yet, or it failed to
    # start): no link and no "see web UI" hint (review fix 2).
    idle = d._notifier_settings()
    assert (idle.web_enabled, idle.web_port, idle.quiet) == (
        False,
        None,
        QuietHours(23 * 60, 8 * 60),
    )
    d._web = SimpleNamespace(running=True, stopping=False, port=9000)  # type: ignore[assignment]
    on = d._notifier_settings()
    assert (on.web_enabled, on.web_port, on.quiet) == (True, 9000, QuietHours(23 * 60, 8 * 60))


def test_a_configured_project_keeps_its_rules_while_its_folder_is_missing(
    tmp_dir: Path,
) -> None:
    """The notifier resolves notify_on from the configured projects by name, so a
    project whose folder is missing is neither unruled nor "(removed)" (review fix 12)."""
    d, _ = make_daemon(tmp_dir)
    d._config = config_mod.Config.model_validate(
        {"projects": [{"name": "gone", "path": str(tmp_dir / "nope"), "notify_on": ["deleted"]}]}
    )
    d._projects = {}
    assert d._configured_notify_on("gone") == ("deleted",)
    assert d._configured_notify_on("unknown") is None


def test_the_link_follows_the_running_server_not_the_config(tmp_dir: Path) -> None:
    """Settings are read per delivery, so a server that starts, moves or stops is
    followed at once; the port is the one it is bound to."""
    d, _ = make_daemon(tmp_dir)
    d._config = config_mod.Config.model_validate({"web": {"enabled": True, "port": 9000}})
    server = SimpleNamespace(running=True, stopping=False, port=9001)  # moved to a new port
    d._web = server  # type: ignore[assignment]
    assert (d._notifier_settings().web_enabled, d._notifier_settings().web_port) == (True, 9001)
    server.running = False  # it stopped (or crashed)
    assert (d._notifier_settings().web_enabled, d._notifier_settings().web_port) == (False, None)
    d._config = config_mod.Config.model_validate({"web": {"enabled": False, "port": 9000}})
    server.running, server.stopping = True, True  # still winding down after being disabled
    # A link sent now would be dead by the time it is tapped.
    assert (d._notifier_settings().web_enabled, d._notifier_settings().web_port) == (False, None)


async def test_maintenance_sends_catch_ups_and_survives_a_failing_one(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []

    async def broken_catch_up(self: Notifier) -> None:
        calls.append(1)
        raise RuntimeError("catch-up boom")

    ticks: list[int] = []

    async def counting_tick(self: Watcher) -> None:
        ticks.append(1)

    monkeypatch.setattr(Notifier, "catch_up_due", broken_catch_up)
    monkeypatch.setattr(Watcher, "tick", counting_tick)
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    while len(calls) < 2 or len(ticks) < 2:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert "maintenance: sending catch-up messages failed" in caplog.text


def hold(store: Store, project: str, reason: str, until: float) -> int:
    obs = Observation(changes=[("a", ChangeKind.MODIFIED)])
    changes = store.record_observation(project, ClockState("/w", "", "c", "h"), obs, now=1.0)
    event_id = store.create_event(project, changes, [], now=1.0)
    assert event_id is not None
    store.mark_held(event_id, until, reason)
    return event_id


def test_new_quiet_hours_make_every_held_event_due(tmp_dir: Path) -> None:
    d, _ = make_daemon(tmp_dir)
    d._clock = lambda: 500.0
    sync_names(d._store, "proj")
    quiet = hold(d._store, "proj", "quiet", 10_000.0)
    snoozed = hold(d._store, "proj", "snooze", 10_000.0)
    config = config_mod.Config.model_validate({"notify": {"quiet_hours": "22:00-07:00"}})
    d._apply_config(config, [])
    assert [e.id for e in d._store.due_held(500.0)] == [quiet, snoozed]
    d._clock = lambda: 600.0
    d._apply_config(config, [])  # unchanged: nothing is released again
    assert d._store.get_event(quiet).next_attempt_at == 500.0
    assert d._store.get_event(snoozed).next_attempt_at == 500.0


def test_the_first_config_releases_held_events(tmp_dir: Path) -> None:
    """Quiet hours removed (or changed) while the daemon was down: the first config
    applied counts as a change, so events held until the old end are due at start."""
    d, _ = make_daemon(tmp_dir)
    d._clock = lambda: 500.0
    sync_names(d._store, "proj")
    held = hold(d._store, "proj", "quiet", 10_000.0)
    d._apply_config(config_mod.Config.model_validate({}), [])  # no quiet hours now
    assert [e.id for e in d._store.due_held(500.0)] == [held]
    d._store.reschedule_held([held], 10_000.0, "quiet")
    d._apply_config(config_mod.Config.model_validate({}), [])  # a reload, unchanged
    assert d._store.due_held(500.0) == []


def test_a_failed_start_release_is_retried_on_the_next_config(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start release counts as done only once it succeeded: a store error in the
    first apply leaves the flag unset, so the next (unchanged) config releases."""
    d, _ = make_daemon(tmp_dir)
    d._clock = lambda: 500.0
    sync_names(d._store, "proj")
    held = hold(d._store, "proj", "quiet", 10_000.0)
    real_release = d._store.release_held

    def broken_release(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(d._store, "release_held", broken_release)
    d._apply_config(config_mod.Config.model_validate({}), [])
    assert d._config_applied is False
    assert d._store.due_held(500.0) == []
    monkeypatch.setattr(d._store, "release_held", real_release)
    d._apply_config(config_mod.Config.model_validate({}), [])  # unchanged config
    assert d._config_applied is True
    assert [e.id for e in d._store.due_held(500.0)] == [held]


def test_a_quiet_hours_change_releases_an_event_held_as_snoozed_until_the_quiet_end(
    tmp_dir: Path,
) -> None:
    """Quiet 09-18, a 1 h snooze at 10:00: pause_until says "snooze" (a snooze
    applies) but the quiet end (18:00) won. Quiet hours turned off at 12:00 must
    not leave the event held until 18:00."""
    d, _ = make_daemon(tmp_dir)
    ten = datetime(2026, 10, 9, 10, 0, tzinfo=UTC).timestamp()
    d._clock = lambda: ten
    project = {"path": str(tmp_dir / "proj")}
    quiet = config_mod.Config.model_validate(
        {"notify": {"quiet_hours": "09:00-18:00"}, "projects": [project]}
    )
    d._apply_config(quiet, [])
    snooze = d._store.set_snooze("proj", ten, ten + 3600)
    pause = pause_until(snooze, QuietHours.parse("09:00-18:00"), ten, ZoneInfo("UTC"))
    assert pause == Pause(datetime(2026, 10, 9, 18, 0, tzinfo=UTC).timestamp(), "snooze")
    held = hold(d._store, "proj", pause.reason, pause.until)
    noon = ten + 2 * 3600
    d._clock = lambda: noon
    d._apply_config(config_mod.Config.model_validate({"projects": [project]}), [])
    assert d._store.get_snooze("proj") is not None  # still configured: kept
    assert [e.id for e in d._store.due_held(noon)] == [held]


def test_a_removed_project_loses_its_snooze_on_reload(tmp_dir: Path) -> None:
    d, _ = make_daemon(tmp_dir)
    d._clock = lambda: 500.0
    proj = tmp_dir / "proj"
    both = [{"path": str(proj)}, {"path": str(tmp_dir / "gone")}]
    d._apply_config(config_mod.Config.model_validate({"projects": both}), [])
    d._store.set_snooze("proj", 1.0, None)
    d._store.set_snooze("gone", 1.0, None)
    held = hold(d._store, "gone", "snooze", 253402300799.0)
    config = config_mod.Config.model_validate({"projects": [{"path": str(proj)}]})
    d._apply_config(config, [])
    assert d._store.get_snooze("proj") is not None
    assert d._store.get_snooze("gone") is None
    assert [e.id for e in d._store.due_held(500.0)] == [held]


def test_a_snoozed_project_renamed_in_the_config_stays_snoozed(tmp_dir: Path) -> None:
    """Stable project ids plan, Task 2b C: the reload's snooze cleanup never treats
    a rename as a removal."""
    d, _ = make_daemon(tmp_dir)
    d._clock = lambda: 500.0
    proj = tmp_dir / "proj"
    d._apply_config(config_mod.Config.model_validate({"projects": [{"path": str(proj)}]}), [])
    d._store.set_snooze("proj", 1.0, 10_000.0)
    renamed = {"projects": [{"path": str(proj), "name": "Renamed"}]}
    d._apply_config(config_mod.Config.model_validate(renamed), [])
    assert list(d._store.active_snoozes(500.0)) == ["Renamed"]
    assert d._store.get_snooze("Renamed") == Snooze("Renamed", 1.0, 10_000.0)
    assert d._store.get_snooze("proj") is None  # the old name is no project now


def test_apply_config_syncs_the_projects_before_anything_uses_a_name(tmp_dir: Path) -> None:
    """Stable project ids spec §2, §4: a renamed project keeps its id, so its snooze
    is not dropped as "removed" and its held event is listed under the new name."""
    d, _ = make_daemon(tmp_dir)
    d._clock = lambda: 500.0
    proj = tmp_dir / "proj"
    d._apply_config(config_mod.Config.model_validate({"projects": [{"path": str(proj)}]}), [])
    d._store.set_snooze("proj", 1.0, None)
    held = hold(d._store, "proj", "snooze", 253402300799.0)
    renamed = {"projects": [{"path": str(proj), "name": "Renamed"}]}
    d._apply_config(config_mod.Config.model_validate(renamed), [])
    assert d._store.get_snooze("Renamed") is not None
    assert d._store.get_event(held).project == "Renamed"
    assert d._store.due_held(500.0) == []  # still snoozed: nothing released


# -- Telegram not set up (bot onboarding spec §1) ------------------------------------


BOTH_MISSING = Missing("both")


def make_unset_daemon(
    tmp_dir: Path,
    missing: Missing | None = BOTH_MISSING,
    *,
    clock: Callable[[], float] = time.time,
) -> tuple[Daemon, Path, list[FakeSender]]:
    """A daemon started without credentials; the list collects each client a swap
    makes."""
    proj = tmp_dir / "proj"
    proj.mkdir()
    config = tmp_dir / "cfg" / "config.yaml"
    config.parent.mkdir()
    config.write_text(f"projects:\n  - path: {proj}\n")
    state = tmp_dir / "state"
    made: list[FakeSender] = []

    def make_client(token: str, chat_id: str | None) -> FakeSender:
        made.append(FakeSender())
        return made[-1]

    d = Daemon(
        config,
        Store(state / "state.db"),
        SenderSlot(None, make_client=make_client),
        state_dir=state,
        wm_factory=no_watchman,
        recv_timeout=0.05,
        maintenance_interval=0.02,
        clock=clock,
        setup_missing=missing,
    )
    return d, state / "status.json", made


async def test_not_set_up_starts_and_opens_the_setup_warning(tmp_dir: Path) -> None:
    d, status, _ = make_unset_daemon(tmp_dir, Missing("chat id"))
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_for_path(status)
        problem = d._store.get_problem(SETUP_KEY)
        assert problem is not None
        assert (problem.severity, problem.fix) == ("warning", "diffbell setup")
        assert problem.message == (
            "Telegram not set up: the chat id is missing. "
            "Set up Telegram in Settings → Telegram, or run the command."
        )
        assert json.loads(status.read_text())["telegram_configured"] is False
        assert d.status_snapshot()["telegram_configured"] is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_an_unfinished_save_is_named_in_the_setup_warning(tmp_dir: Path) -> None:
    d, status, _ = make_unset_daemon(tmp_dir, Missing("unfinished save"))
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_for_path(status)
        problem = d._store.get_problem(SETUP_KEY)
        assert problem is not None and "an earlier save didn't finish" in problem.message
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_a_start_with_credentials_resolves_a_saved_setup_warning(tmp_dir: Path) -> None:
    d, status = make_daemon(tmp_dir)
    d._health.open(SETUP_KEY, "warning", "Telegram not set up.", fix="diffbell setup")
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_for_path(status)
        assert d._store.get_problem(SETUP_KEY) is None
        assert json.loads(status.read_text())["telegram_configured"] is True
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_the_bot_username_is_fetched_in_the_background(tmp_dir: Path) -> None:
    sender = FakeSender()
    sender.username = "my_bot"
    d, _ = make_daemon(tmp_dir, sender)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_until(lambda: d._sender.bot_username == "my_bot")
        assert sender.me_calls == 1
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_a_failing_username_fetch_is_harmless(tmp_dir: Path) -> None:
    sender = FakeSender()
    sender.me_error = TelegramError(ErrorKind.RETRYABLE, "getMe: network error (ConnectError)")
    d, _ = make_daemon(tmp_dir, sender)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_until(lambda: sender.me_calls == 1)
        await asyncio.sleep(0.1)  # a few maintenance ticks
        assert d._sender.bot_username is None
        assert not [k for k in problem_keys(d) if k.startswith("telegram")]
        assert not task.done()  # not supervised: watching carries on
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


async def test_maintenance_mutes_health_while_not_set_up(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    async def recording_tick(self: Health, now: float, *, sending: bool = True) -> None:
        seen.append(sending)

    monkeypatch.setattr(Health, "tick", recording_tick)
    d, _, _ = make_unset_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_until(lambda: len(seen) >= 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert set(seen) == {False}


def test_the_telegram_warning_is_resolved_while_not_set_up(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, _, _ = make_unset_daemon(tmp_dir)
    d._health.open("telegram", "warning", "Telegram failing: sendMessage: 502")
    monkeypatch.setattr(d._store, "telegram_health", lambda: (None, "sendMessage: 502"))
    d._check_telegram()
    assert d._store.get_problem("telegram") is None


async def test_complete_setup_swaps_resolves_rearms_and_releases(tmp_dir: Path) -> None:
    now = datetime(2026, 10, 9, 23, 0).timestamp()  # local time, past the 09:00 heartbeat
    d, _, made = make_unset_daemon(tmp_dir, clock=lambda: now)
    sync_names(d._store, "proj")
    held = hold(d._store, "proj", "setup", HELD_FOREVER)
    snoozed = hold(d._store, "proj", "snooze", HELD_FOREVER)
    d._health.open(SETUP_KEY, "warning", "Telegram not set up.", fix="diffbell setup")
    d._store.put_problem(
        Problem(
            "watchman",
            "serious",
            None,
            "Watchman is unreachable",
            None,
            now - 600,
            attempts=2,
            next_try_at=now + 3600,
        )
    )
    await d.complete_setup("123:tok", "42", "my_bot")
    assert d._sender.configured and d._sender.bot_username == "my_bot"
    assert len(made) == 1
    assert d._store.get_problem(SETUP_KEY) is None
    watchman = d._store.get_problem("watchman")
    assert watchman is not None and (watchman.attempts, watchman.next_try_at) == (0, now)
    assert d._store.kv_get(HEARTBEAT_KEY) == "2026-10-09 09:00"
    assert d._store.get_event(held).next_attempt_at == now
    assert d._store.get_event(snoozed).next_attempt_at == HELD_FOREVER
    await d._sender.send("hello")
    assert made[0].sent == [("hello", "HTML")]


def setup_fixture(tmp_dir: Path, *, missing: Missing | None = BOTH_MISSING) -> Any:
    """An unset daemon past today's 09:00 heartbeat with one setup-held event."""
    now = datetime(2026, 10, 9, 23, 0).timestamp()
    d, _, made = make_unset_daemon(tmp_dir, missing, clock=lambda: now)
    sync_names(d._store, "proj")
    held = hold(d._store, "proj", "setup", HELD_FOREVER)
    return SimpleNamespace(d=d, made=made, now=now, held=held)


async def test_replacing_a_bot_does_not_mark_todays_heartbeat(tmp_dir: Path) -> None:
    f = setup_fixture(tmp_dir, missing=None)
    await f.d._sender.swap("1:old", "42", bot_username="old_bot")  # already configured
    await f.d.complete_setup("123:tok", "42", "my_bot")
    assert f.d._sender.bot_username == "my_bot" and len(f.made) == 2
    assert f.d._store.kv_get(HEARTBEAT_KEY) is None  # a failed heartbeat still goes out


async def test_complete_setup_twice_is_harmless(tmp_dir: Path) -> None:
    f = setup_fixture(tmp_dir)
    f.d._health.open(SETUP_KEY, "warning", "Telegram not set up.", fix="diffbell setup")
    await f.d.complete_setup("123:tok", "42", "my_bot")
    await f.d.complete_setup("123:tok", "42", "my_bot")
    assert f.d._sender.configured and len(f.made) == 2
    assert f.d._store.get_problem(SETUP_KEY) is None
    assert f.d._store.kv_get(HEARTBEAT_KEY) == "2026-10-09 09:00"
    assert f.d._store.get_event(f.held).next_attempt_at == f.now


async def test_a_failing_step_of_complete_setup_still_releases_held(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    f = setup_fixture(tmp_dir)

    def boom(*args: Any, **kwargs: Any) -> NoReturn:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(f.d._health, "resolve", boom)
    monkeypatch.setattr(f.d._health, "rearm", boom)
    with caplog.at_level(logging.ERROR, logger="diffbell"):
        await f.d.complete_setup("123:tok", "42", None)
    assert f.d._sender.configured
    assert f.d._store.get_event(f.held).next_attempt_at == f.now
    assert "after Telegram setup: rearming alerts failed" in caplog.text


async def test_health_ticks_send_once_setup_completes(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    async def recording_tick(self: Health, now: float, *, sending: bool = True) -> None:
        seen.append(sending)

    monkeypatch.setattr(Health, "tick", recording_tick)
    d, _, _ = make_unset_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_until(lambda: False in seen)
        await d.complete_setup("123:tok", "42", None)
        await wait_until(lambda: seen[-1] is True)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)


def test_an_empty_slot_without_a_reason_opens_the_plain_setup_warning(tmp_dir: Path) -> None:
    d, _, _ = make_unset_daemon(tmp_dir, None)
    d._record_setup()
    problem = d._store.get_problem(SETUP_KEY)
    assert problem is not None
    assert problem.message == (
        "Telegram not set up. Set up Telegram in Settings → Telegram, or run the command."
    )


async def test_changes_held_for_setup_arrive_as_one_catch_up_from_stored_contents(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the daemon's own wiring (bot onboarding spec §1, §2): not
    set up, a key-file change is held with reason "setup" and its contents captured
    (content snapshots); the wizard's complete_setup releases it, and the next
    maintenance tick sends one catch-up, rendered from the stored contents (a later
    edit on disk doesn't show), headed "held until Telegram was set up"."""
    notifiers: list[Notifier] = []

    class Recording(Notifier):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            notifiers.append(self)

    monkeypatch.setattr(daemon_mod, "Notifier", Recording)
    now = datetime(2026, 10, 9, 23, 0).timestamp()  # past the 09:00 heartbeat
    d, status, made = make_unset_daemon(tmp_dir, clock=lambda: now)
    key_file = tmp_dir / "proj" / "package.json"
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    try:
        await wait_for_path(status)
        (notifier,) = notifiers
        before = b'{\n  "v": 1\n}\n'
        hashes = {"package.json": hashlib.sha1(before).hexdigest()}
        d._store.replace_baseline(
            "proj",
            ClockState("/w", "", "c:1", "h"),
            hashes,
            now,
            snapshots={"package.json": before},
        )
        key_file.write_text('{\n  "v": 2\n}\n')
        obs = Observation(changes=[("package.json", ChangeKind.MODIFIED)])
        changes = d._store.record_observation(
            "proj", ClockState("/w", "", "c:2", "h"), obs, now=now
        )
        event_id = await notifier.flush("proj", changes)
        assert event_id is not None
        event = d._store.get_event(event_id)
        assert (event.status, event.next_attempt_at) == ("held", HELD_FOREVER)
        assert d._store.event_contents(event_id)["package.json"].after == b'{\n  "v": 2\n}\n'
        key_file.write_text('{\n  "v": 3\n}\n')  # after the event: not in the catch-up
        await asyncio.sleep(0.1)  # maintenance ticks: nothing is due while not set up
        assert made == []

        await d.complete_setup("123:tok", "42", "my_bot")
        (client,) = made

        def catch_ups() -> list[str]:
            return [text for text, _ in client.sent if "Catch-up" in text]

        await wait_until(lambda: len(catch_ups()) >= 1)
        await asyncio.sleep(0.1)  # more ticks: still one
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    (text,) = catch_ups()
    assert text.startswith("<b>🔕 Catch-up · proj</b> · held until Telegram was set up (")
    assert "-  &quot;v&quot;: 1" in text and "+  &quot;v&quot;: 2" in text
    assert "&quot;v&quot;: 3" not in text
    assert d._store.get_event(event_id).status == "sent"


# -- the cached setup reason and the save time (settings redesign spec §7) -----------


def test_the_setup_reason_is_cached_from_the_start(tmp_dir: Path) -> None:
    d, _, _ = make_unset_daemon(tmp_dir, Missing("chat id"))
    assert d.setup_reason() == "chat id"


def test_no_reason_at_start_reads_as_both(tmp_dir: Path) -> None:
    d, _, _ = make_unset_daemon(tmp_dir, None)
    assert d.setup_reason() == "both"


def test_a_configured_daemon_has_no_setup_reason(tmp_dir: Path) -> None:
    d, _ = make_daemon(tmp_dir)
    assert d.setup_reason() is None
    d.note_unfinished_save()  # a failed replacement: the old bot still sends
    assert d.setup_reason() is None


async def test_a_failed_save_then_complete_setup_clears_the_reason(tmp_dir: Path) -> None:
    d, _, _ = make_unset_daemon(tmp_dir)
    d.note_unfinished_save()
    assert d.setup_reason() == "unfinished save"
    await d.complete_setup("123:tok", "42", "my_bot")
    assert d._setup_reason is None and d.setup_reason() is None


def test_the_setup_reason_never_reads_the_keychain(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_keychain(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("setup_reason ran a Keychain command")

    monkeypatch.setattr(secrets_mod, "_run", no_keychain)
    d, _, _ = make_unset_daemon(tmp_dir, Missing("bot token"))
    assert d._web_context(8765, b"s" * 32).setup_reason() == "bot token"


async def test_complete_setup_records_the_save_time_and_hides_older_failures(
    tmp_dir: Path,
) -> None:
    now = 1_000_000.0
    d, _, _ = make_unset_daemon(tmp_dir, clock=lambda: now)
    sync_names(d._store, "proj")
    old = hold(d._store, "proj", "quiet", 4102444800.0)
    d._store.mark_batch_failed([old], "sendMessage: Unauthorized", old, kind="auth", at=now - 5)
    assert d._store.telegram_failure() is not None
    await d.complete_setup("123:tok", "42", "my_bot")
    assert d._store.kv_get(TELEGRAM_SAVED_AT) == repr(now)
    assert d._store.telegram_failure() is None
    fresh = hold(d._store, "proj", "quiet", 4102444800.0)
    d._store.mark_batch_retry([fresh], "sendMessage: 502", now + 60, kind="retryable", at=now + 5)
    failure = d._store.telegram_failure()
    assert failure is not None and (failure.kind, failure.waiting) == ("retryable", 1)


async def test_a_failed_save_time_write_never_fails_complete_setup(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    d, _, made = make_unset_daemon(tmp_dir)
    kv_set = d._store.kv_set

    def failing(key: str, value: str) -> None:
        if key == TELEGRAM_SAVED_AT:
            raise sqlite3.OperationalError("database is locked")
        kv_set(key, value)

    monkeypatch.setattr(d._store, "kv_set", failing)
    await d.complete_setup("123:tok", "42", "my_bot")
    assert d._sender.configured and len(made) == 1
    assert d._store.kv_get(TELEGRAM_SAVED_AT) is None
    assert d.setup_reason() is None
    assert "recording the save time failed (OperationalError)" in caplog.text


async def test_a_rejected_config_alert_is_worded_without_the_file(tmp_dir: Path) -> None:
    """The alert (Telegram, the page) says field + reason, as a save's 422 does: never
    the config's path, the file's contents or the validation library's URL."""
    sender = FakeSender()
    d, _, _ = make_two_project_daemon(tmp_dir, sender)
    stubbed(d, StubWatcher())
    good = d._config_path.read_text()
    d._config_path.write_text(good + "live_check_invalid: secret-looking-value\n")
    await d._reload()
    [problem] = d._health.problems()
    assert problem.message == (
        "config rejected, keeping the last good config: live_check_invalid: unknown setting"
    )
    d._config_path.write_text("projects: [\n")
    await d._reload()
    [problem] = d._health.problems()
    assert problem.message.startswith(
        "config rejected, keeping the last good config: invalid YAML at line "
    )
    for leak in (str(d._config_path), "pydantic.dev", "secret-looking-value", "projects: ["):
        assert leak not in problem.message, leak


async def test_replace_bot_resolves_the_old_bots_telegram_warning_at_once(
    tmp_dir: Path,
) -> None:
    """The header's "Telegram failing" (the `telegram` problem) is the replaced bot's:
    complete_setup resolves it without waiting for the next maintenance tick."""
    now = 1_000_000.0
    d, _, _ = make_unset_daemon(tmp_dir, clock=lambda: now)
    sync_names(d._store, "proj")
    await d.complete_setup("123:old", "42", "my_bot")
    old = hold(d._store, "proj", "quiet", 4102444800.0)
    d._store.mark_batch_failed([old], "sendMessage: Unauthorized", old, kind="auth", at=now + 1)
    now += 10
    d._check_telegram()
    assert "telegram" in problem_keys(d)
    now += 10
    await d.complete_setup("123:new", "42", "my_bot")
    assert "telegram" not in problem_keys(d)
