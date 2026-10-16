"""The daemon's content-snapshot wiring (content snapshots spec §2.5, §3.1): one
Snapshotter for the notifier and the watcher, the start checks (downgrade guard,
work-tree warning, temp sweep), the maintenance mark, the redaction change."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from diffbell import config as config_mod
from diffbell import daemon as daemon_mod
from diffbell.config import ResolvedProject
from diffbell.fingerprint import Observation
from diffbell.notifier import Notifier
from diffbell.snapshots import TEMP_DIRNAME, TEMP_SWEEP_AGE, Snapshotter
from diffbell.store import SNAPSHOTS_SEEN, ClockState, Store
from diffbell.watcher import Watcher
from diffbell.wmclient import WatchmanClient
from tests.helpers import HAS_GIT, init_repo
from tests.test_daemon import make_daemon, wait_for_path

WORKTREE_WARNING = "the state folder is inside a git repository: diffs are approximate"
UNVERIFIED_WARNING = (
    "could not verify the state folder is outside a git repository: diffs are approximate"
)


def put_snapshot(store: Store, project: str, path: str, data: bytes, now: float = 1.0) -> None:
    store._db.execute(
        "INSERT INTO snapshots (project_id, path, content, captured_at) "
        "SELECT id, ?, ?, ? FROM projects WHERE name = ? AND active = 1",
        (path, data, now, project),
    )


def leftover(state: Path, name: str) -> Path:
    root = state / TEMP_DIRNAME
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / name
    path.mkdir()
    old = time.time() - TEMP_SWEEP_AGE - 60
    os.utime(path, (old, old))
    return path


async def test_the_notifier_and_the_watcher_share_one_snapshotter(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class SpyNotifier(Notifier):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["snapshots"] = kwargs.get("snapshots")
            super().__init__(*args, **kwargs)

    class SpyWatcher(Watcher):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen["reader"] = kwargs.get("baseline_reader")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(daemon_mod, "Notifier", SpyNotifier)
    monkeypatch.setattr(daemon_mod, "Watcher", SpyWatcher)
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert isinstance(seen["snapshots"], Snapshotter)
    assert seen["snapshots"] is d._snapshots
    assert seen["reader"] == d._read_baseline


def test_the_baseline_reader_uses_the_current_redaction_list(tmp_dir: Path) -> None:
    d, _ = make_daemon(tmp_dir)
    proj = tmp_dir / "proj"
    (proj / "app.json").write_text("{}\n")
    (proj / "keep.json").write_text("{}\n")
    project = ResolvedProject("proj", proj, ("*.json",), ())
    d._apply_config(config_mod.Config.model_validate({"redact": ["app*"]}), [project])
    got = d._read_baseline(project, {"app.json": "x", "keep.json": "y"})
    assert got == {"keep.json": b"{}\n"}


async def test_start_drops_snapshots_older_code_left_stale(tmp_dir: Path) -> None:
    d, _ = make_daemon(tmp_dir)
    d._clock = lambda: 5000.0
    d._apply_config(*config_mod.load_and_resolve(d._config_path)[:2])
    put_snapshot(d._store, "proj", "a.json", b"old\n")
    d._store.mark_snapshots_seen(1000.0)
    state = ClockState("/w", "", "c:1", "h")
    d._store.record_observation("proj", state, Observation(), now=2000.0)  # older code
    await d._start_snapshots()
    assert d._store.get_snapshots("proj") == {}
    assert d._store.kv_get(SNAPSHOTS_SEEN) == "5000.0"


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
async def test_a_state_dir_in_a_work_tree_is_a_warning_until_it_is_not(tmp_dir: Path) -> None:
    d, _ = make_daemon(tmp_dir)
    init_repo(tmp_dir)  # the state dir (tmp_dir / "state") is now inside a work tree
    await d._start_snapshots()
    assert d._snapshots.approximate is True
    problems = {p.key: p for p in d._health.problems()}
    assert problems["snapshots"].severity == "warning"
    assert problems["snapshots"].message == WORKTREE_WARNING
    assert problems["snapshots"].fix is None
    (tmp_dir / ".git").rename(tmp_dir / "not-git")  # moved out of the repository
    await d._start_snapshots()
    assert d._snapshots.approximate is False
    assert "snapshots" not in {p.key for p in d._health.problems()}


def test_temp_leftovers_are_swept_with_every_prune(tmp_dir: Path) -> None:
    """At start too: run() prunes once before the watcher starts (the run()-level
    order test checks it)."""
    d, _ = make_daemon(tmp_dir)
    second = leftover(tmp_dir / "state", "diff-crashed-again")
    d._prune()
    assert not second.exists()


def test_a_failing_prune_still_sweeps_and_never_raises(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, _ = make_daemon(tmp_dir)
    stale = leftover(tmp_dir / "state", "diff-crashed")

    def broken(self: Store, older_than: float) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "prune", broken)
    d._prune()
    assert not stale.exists()


def test_a_failing_sweep_still_prunes(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d, _ = make_daemon(tmp_dir)
    pruned: list[float] = []

    def broken(self: Snapshotter, now: float) -> int:
        raise OSError("sweep boom")

    monkeypatch.setattr(Snapshotter, "sweep", broken)
    monkeypatch.setattr(d._store, "prune", pruned.append)
    d._prune()
    assert len(pruned) == 1


async def test_a_work_tree_check_that_could_not_run_is_an_honest_warning(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, _ = make_daemon(tmp_dir)
    answer = ["unknown"]
    monkeypatch.setattr(Snapshotter, "check_isolation", lambda self: answer[0])
    await d._start_snapshots()
    problems = {p.key: p for p in d._health.problems()}
    assert problems["snapshots"].severity == "warning"
    assert problems["snapshots"].message == UNVERIFIED_WARNING
    assert problems["snapshots"].fix is None
    answer[0] = "isolated"
    await d._start_snapshots()
    assert "snapshots" not in {p.key for p in d._health.problems()}


async def test_the_downgrade_guard_never_raises(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    d, _ = make_daemon(tmp_dir)

    def broken(self: Store, now: float) -> int:
        raise ValueError("secret-ish detail")

    monkeypatch.setattr(Store, "drop_stale_snapshots", broken)
    await d._start_snapshots()
    assert "ValueError" in caplog.text
    assert "secret-ish detail" not in caplog.text


async def test_each_maintenance_tick_records_the_mark(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marks: list[float] = []
    monkeypatch.setattr(Store, "mark_snapshots_seen", lambda self, now: marks.append(now))
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    deadline = asyncio.get_running_loop().time() + 5.0
    while len(marks) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert len(marks) >= 2


def test_a_grown_redaction_list_deletes_the_covered_contents(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, _ = make_daemon(tmp_dir)
    config, projects = config_mod.load_and_resolve(d._config_path)[:2]
    d._apply_config(config, projects)
    put_snapshot(d._store, "proj", "config/app.json", b"x\n")
    put_snapshot(d._store, "proj", "keep.json", b"k\n")
    calls: list[tuple[str, ...]] = []
    real = Store.forget_redacted

    def spy(self: Store, redact: tuple[str, ...]) -> int:
        calls.append(tuple(redact))
        return real(self, redact)

    monkeypatch.setattr(Store, "forget_redacted", spy)
    d._apply_config(config, projects)  # unchanged: nothing to delete
    assert calls == []
    grown = config.model_copy(update={"redact": ["*app*"]})
    d._apply_config(grown, projects)
    assert len(calls) == 1 and "*app*" in calls[0]
    assert d._store.get_snapshots("proj") == {"keep.json": b"k\n"}


def test_the_first_apply_deletes_what_the_list_covers(tmp_dir: Path) -> None:
    """The list may have grown while the daemon was down."""
    d, _ = make_daemon(tmp_dir)
    config, projects = config_mod.load_and_resolve(d._config_path)[:2]
    d._store.sync_projects(config_mod.project_identities(config), 0.0)
    put_snapshot(d._store, "proj", ".env.local", b"API_KEY=SECRET\n")
    d._apply_config(config, projects)
    assert d._store.get_snapshots("proj") == {}


def test_a_failed_forget_is_retried_by_the_next_apply(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, _ = make_daemon(tmp_dir)
    config, projects = config_mod.load_and_resolve(d._config_path)[:2]
    d._apply_config(config, projects)
    put_snapshot(d._store, "proj", "config/app.json", b"x\n")
    real = Store.forget_redacted
    fail = [True]

    def flaky(self: Store, redact: tuple[str, ...]) -> int:
        if fail[0]:
            raise sqlite3.OperationalError("database is locked")
        return real(self, redact)

    monkeypatch.setattr(Store, "forget_redacted", flaky)
    grown = config.model_copy(update={"redact": ["*app*"]})
    d._apply_config(grown, projects)
    assert d._store.get_snapshots("proj") == {"config/app.json": b"x\n"}
    fail[0] = False
    d._apply_config(grown, projects)  # the list didn't grow again: still retried
    assert d._store.get_snapshots("proj") == {}


async def test_a_failed_forget_is_retried_by_the_maintenance_tick(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = Store.forget_redacted

    def flaky(self: Store, redact: tuple[str, ...]) -> int:
        calls.append(1)
        if len(calls) == 1:  # the first apply's, at start
            raise sqlite3.OperationalError("database is locked")
        return real(self, redact)

    monkeypatch.setattr(Store, "forget_redacted", flaky)
    monkeypatch.setattr(daemon_mod, "FORGET_RETRY_EVERY", 0.0)
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    deadline = asyncio.get_running_loop().time() + 5.0
    while len(calls) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)  # a few more ticks: nothing pending any more
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert len(calls) == 2


async def test_the_tick_retries_a_failed_forget_at_most_once_a_minute(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A database that keeps failing is retried once per FORGET_RETRY_EVERY, not
    every tick, and only the first failure of the run logs a stack."""
    calls: list[float] = []
    now = [1000.0]

    def failing(self: Store, redact: tuple[str, ...]) -> int:
        calls.append(now[0])
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "forget_redacted", failing)
    d, status = make_daemon(tmp_dir)
    d._clock = lambda: now[0]
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    await asyncio.sleep(0.2)  # about ten ticks at a standing clock
    assert calls == [1000.0]  # the start apply's only
    now[0] = 1059.0
    await asyncio.sleep(0.1)
    assert calls == [1000.0]
    now[0] = 1060.0
    deadline = asyncio.get_running_loop().time() + 5.0
    while len(calls) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert calls == [1000.0, 1060.0]
    failures = [r for r in caplog.records if "newly redacted files failed" in r.getMessage()]
    assert [r.exc_info is not None for r in failures] == [True, False]
    assert "OperationalError" in failures[1].getMessage()


# -- run(): the start order, and every snapshot hook isolated ----------------------------------


async def test_run_checks_snapshots_after_reconcile_and_before_the_watcher_starts(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync and reconcile with no await between (CLAUDE.md); the start checks after
    the reconcile, and before wm.start and any flush."""
    order: list[str] = []

    def spy(cls: Any, name: str, *, is_async: bool = False) -> None:
        real = getattr(cls, name)
        if is_async:

            async def wrapped_async(self: Any, *args: Any, **kwargs: Any) -> Any:
                order.append(name)
                return await real(self, *args, **kwargs)

            monkeypatch.setattr(cls, name, wrapped_async)
        else:

            def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
                order.append(name)
                return real(self, *args, **kwargs)

            monkeypatch.setattr(cls, name, wrapped)

    real_sync = Store.sync_projects

    def sync(self: Store, *args: Any, **kwargs: Any) -> Any:
        order.append("sync_projects")
        # Recorded only once the loop runs again: after reconcile's entry iff no await
        # came in between.
        asyncio.get_running_loop().call_soon(order.append, "loop turned")
        return real_sync(self, *args, **kwargs)

    monkeypatch.setattr(Store, "sync_projects", sync)
    spy(Watcher, "reconcile", is_async=True)
    spy(Store, "drop_stale_snapshots")
    spy(Snapshotter, "check_isolation")
    spy(Snapshotter, "sweep")
    spy(Store, "unflushed_changes")
    spy(WatchmanClient, "start")
    spy(Notifier, "flush", is_async=True)

    d, status = make_daemon(tmp_dir)
    d._apply_config(*config_mod.load_and_resolve(d._config_path)[:2])
    put_snapshot(d._store, "proj", "a.json", b"old\n")
    d._store.mark_snapshots_seen(1000.0)
    d._store.record_observation("proj", ClockState("/w", "", "c:1", "h"), Observation(), now=2000.0)
    stale = leftover(tmp_dir / "state", "diff-crashed")
    await asyncio.sleep(0)  # the setup's own "loop turned"
    order.clear()

    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    start = order[: order.index("start") + 1]
    assert start[:2] == ["sync_projects", "reconcile"]  # no await in between
    checks = ["drop_stale_snapshots", "check_isolation", "unflushed_changes", "sweep"]
    assert [step for step in start[2:] if step != "loop turned"] == [*checks, "start"]
    assert "flush" not in start
    assert start.count("sweep") == 1  # one sweep at start, not two
    assert not stale.exists()
    assert d._store.get_snapshots("proj") == {}

    # A reload: sync and reconcile with no await between, too.
    order.clear()
    await d._reload()
    await asyncio.sleep(0)
    assert order[:3] == ["sync_projects", "reconcile", "loop turned"]
    stop.set()
    await asyncio.wait_for(task, timeout=10)


HOOKS: list[tuple[Any, str]] = [
    (Store, "drop_stale_snapshots"),
    (Snapshotter, "check_isolation"),
    (Snapshotter, "sweep"),
    (Store, "forget_redacted"),
    (Store, "mark_snapshots_seen"),
    (Store, "prune"),
]


@pytest.mark.parametrize(("owner", "name"), HOOKS, ids=[name for _, name in HOOKS])
async def test_a_failing_snapshot_hook_never_stops_the_daemon(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, owner: Any, name: str
) -> None:
    calls: list[int] = []

    def broken(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError(f"{name} boom")

    monkeypatch.setattr(owner, name, broken)
    monkeypatch.setattr(daemon_mod, "PRUNE_EVERY", 0.0)  # the maintenance prune too
    ticks: list[int] = []
    real_tick = Watcher.tick

    async def counting_tick(self: Watcher) -> None:
        ticks.append(1)
        await real_tick(self)

    monkeypatch.setattr(Watcher, "tick", counting_tick)
    d, status = make_daemon(tmp_dir)
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    await wait_for_path(status)
    snapshot = json.loads(status.read_text())
    assert snapshot["running"] is True
    assert [p["name"] for p in snapshot["projects"]] == ["proj"]  # reconciled
    deadline = asyncio.get_running_loop().time() + 5.0
    while len(ticks) < 3 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert len(ticks) >= 3 and not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=10)  # a clean stop: no exception
    assert calls  # the hook did run (and fail)
    assert json.loads(status.read_text()).get("clean_shutdown") is True
