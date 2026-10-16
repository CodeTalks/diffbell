"""The baseline's snapshots (content snapshots spec §2.1): read off the loop by the
hardened reader before replace_baseline's transaction, never a redacted file, and
dropped with the PDU when the project's folder changed during the read."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from diffbell import snapshots as snapshots_mod
from diffbell.config import BUILTIN_REDACT, ResolvedProject
from diffbell.diffing import read_capturable
from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.notifier import Notifier, NotifierSettings
from diffbell.snapshots import Snapshotter
from diffbell.store import ClockState, Store
from diffbell.watcher import Watcher
from tests.fakes import FakeSender
from tests.helpers import HAS_GIT, git, init_repo, sync_names
from tests.test_watcher import FakeCaller, entry

WATCHED = ("package.json", "*.lock", ".env*")


def sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class Setup:
    def __init__(self, tmp_dir: Path, reader: Any) -> None:
        self.root = tmp_dir / "proj"
        self.root.mkdir()
        self.store = Store(tmp_dir / "state" / "state.db")
        sync_names(self.store, "p")
        self.snap = Snapshotter(tmp_dir / "state")
        self.project = ResolvedProject("p", self.root, WATCHED, ())
        self.caller = FakeCaller(watch=str(self.root))
        self.watcher = Watcher(
            self.caller, self.store, lambda changes: None, baseline_reader=reader
        )

    def read(self, project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        return self.snap.read_baseline(project.path, hashes, BUILTIN_REDACT)

    async def baseline(self, files: dict[str, bytes]) -> None:
        for name, data in files.items():
            (self.root / name).write_bytes(data)
        await self.watcher.reconcile([self.project])
        await self.watcher.handle({"_diffbell": "connected"})
        await self.watcher.handle(
            {
                "subscription": self.caller.subscribes()[-1][2],
                "root": str(self.root),
                "clock": "c:2",
                "is_fresh_instance": True,
                "files": [entry(name, sha(data)) for name, data in files.items()],
            }
        )


async def test_a_rebaseline_stores_snapshots_read_off_the_loop(tmp_dir: Path) -> None:
    threads: list[bool] = []

    def reader(project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        threads.append(threading.current_thread() is not threading.main_thread())
        return setup.read(project, hashes)

    setup = Setup(tmp_dir, reader)
    await setup.baseline({"package.json": b"{}\n", "yarn.lock": b"lock\n"})
    assert setup.store.get_snapshots("p") == {"package.json": b"{}\n", "yarn.lock": b"lock\n"}
    assert setup.store.get_hashes("p") == {
        "package.json": sha(b"{}\n"),
        "yarn.lock": sha(b"lock\n"),
    }
    assert threads == [True]


async def test_the_baseline_never_opens_a_redacted_file(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(tmp_dir, None)
    setup.watcher = Watcher(setup.caller, setup.store, lambda c: None, baseline_reader=setup.read)
    opened: list[str] = []
    real = os.open

    def spy(path: Any, *args: Any, **kw: Any) -> int:
        opened.append(os.fspath(path))
        return real(path, *args, **kw)

    monkeypatch.setattr(os, "open", spy)
    await setup.baseline({"package.json": b"{}\n", ".env.local": b"API_KEY=SECRET\n"})
    assert not any(".env" in p for p in opened)
    assert setup.store.get_snapshots("p") == {"package.json": b"{}\n"}
    assert set(setup.store.get_hashes("p")) == {"package.json", ".env.local"}


async def test_without_a_reader_the_baseline_has_no_snapshots(tmp_dir: Path) -> None:
    setup = Setup(tmp_dir, None)
    await setup.baseline({"package.json": b"{}\n"})
    assert setup.store.get_snapshots("p") == {}
    assert setup.store.get_hashes("p") == {"package.json": sha(b"{}\n")}


async def test_a_failing_reader_still_records_the_baseline(tmp_dir: Path) -> None:
    def broken(project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        raise OSError("disk gone")

    setup = Setup(tmp_dir, broken)
    await setup.baseline({"package.json": b"{}\n"})
    assert setup.store.get_hashes("p") == {"package.json": sha(b"{}\n")}
    assert setup.store.get_snapshots("p") == {}


async def test_a_sync_during_the_read_drops_the_pdu(tmp_dir: Path) -> None:
    """The name moved to another folder while the baseline was read: the write
    expects the subscription's token, so nothing lands on the other folder."""

    entered, go = threading.Event(), threading.Event()

    def reader(project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        entered.set()
        go.wait(5)
        return setup.read(project, hashes)

    setup = Setup(tmp_dir, reader)
    task = asyncio.create_task(setup.baseline({"package.json": b"{}\n"}))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    setup.store.sync_projects([("p", "/df-test/elsewhere")], 5.0)  # on the loop's thread
    go.set()
    await task
    assert setup.store.get_hashes("p") == {}
    assert setup.store.get_snapshots("p") == {}
    assert setup.store.get_clock("p") is None


def test_read_baseline_fills_in_path_order_within_the_project_budget(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snapshots_mod, "PROJECT_SNAPSHOT_BUDGET", 10)
    root = tmp_dir / "proj"
    root.mkdir()
    files = {"a.lock": b"aaaa\n", "b.lock": b"bbbbbbb\n", "c.lock": b"cc\n"}
    for name, data in files.items():
        (root / name).write_bytes(data)
    (root / "link.lock").symlink_to("a.lock")
    snap = Snapshotter(tmp_dir)
    hashes = {name: sha(data) for name, data in files.items()} | {"link.lock": "link:a.lock"}
    assert snap.read_baseline(root, hashes, BUILTIN_REDACT) == {
        "a.lock": b"aaaa\n",
        "c.lock": b"cc\n",
    }


def test_read_baseline_reads_at_most_the_file_cap(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pump awaits the baseline read: the number of files read is capped, not
    only their bytes. Files past the cap get no snapshot (their first event falls
    back once); redacted files don't count."""
    monkeypatch.setattr(snapshots_mod, "BASELINE_MAX_FILES", 3)
    root = tmp_dir / "proj"
    root.mkdir()
    names = [f"f{i}.lock" for i in range(5)]
    for name in names:
        (root / name).write_text(name)
    (root / ".env.local").write_text("API_KEY=SECRET\n")
    hashes = {name: sha(name.encode()) for name in [*names, ".env.local"]}
    read: list[str] = []

    def reader(root: Path, path: str, *args: Any) -> bytes | None:
        read.append(path)
        return read_capturable(root, path, *args)

    monkeypatch.setattr(snapshots_mod, "read_capturable", reader)
    got = Snapshotter(tmp_dir).read_baseline(root, hashes, BUILTIN_REDACT)
    assert read == ["f0.lock", "f1.lock", "f2.lock"]
    assert sorted(got) == ["f0.lock", "f1.lock", "f2.lock"]


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_a_rebaseline_drops_the_projects_object_format_entry(tmp_dir: Path) -> None:
    root = tmp_dir / "proj"
    root.mkdir()
    init_repo(root)
    git(root, "commit", "-q", "--allow-empty", "-m", "init")
    snap = Snapshotter(tmp_dir)
    assert snap.formats.lookup(root) == "sha1"
    assert snap.formats._by_toplevel != {}
    snap.read_baseline(root, {}, BUILTIN_REDACT)
    assert snap.formats._by_toplevel == {}


# -- a rebaseline never regresses a pending or concurrent snapshot (may fall back once) ----------


def pending(store: Store, path: str) -> list[Change]:
    """A journal row not yet in an event (in the debouncer, or crash-recovered),
    under a clock of another expression: the next subscription re-baselines."""
    state = ClockState("/elsewhere", "", "c:old", "old-expression")
    obs = Observation(changes=[(path, ChangeKind.MODIFIED)])
    return store.record_observation("p", state, obs, now=1.0)


def notifier_for(setup: Setup) -> Notifier:
    return Notifier(
        setup.store,
        FakeSender(),
        projects=lambda name: setup.project if name == "p" else None,
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=lambda: 1000.0,
        snapshots=setup.snap,
    )


async def test_a_flush_during_the_baseline_read_keeps_its_snapshot(tmp_dir: Path) -> None:
    """The reader read the OLDER bytes; a flush committed the newer snapshot while
    the read was in progress (the loop is free). The baseline must not regress it."""
    a0, a1 = b'{"v": 0}\n', b'{"v": 1}\n'
    entered, go = threading.Event(), threading.Event()

    def reader(project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        got = setup.read(project, hashes)
        entered.set()
        go.wait(5)
        return got

    setup = Setup(tmp_dir, reader)
    rows = pending(setup.store, "package.json")
    task = asyncio.create_task(setup.baseline({"package.json": a0}))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    (setup.root / "package.json").write_bytes(a1)
    event_id = await notifier_for(setup).flush("p", rows)
    assert event_id is not None
    assert setup.store.get_snapshots("p") == {"package.json": a1}
    go.set()
    await task
    assert setup.store.get_snapshots("p").get("package.json") != a0
    assert setup.store.get_hashes("p") == {"package.json": sha(a0)}  # the baseline itself


async def test_a_change_pending_across_a_rebaseline_is_not_reported_as_unchanged(
    tmp_dir: Path,
) -> None:
    """A row waits in the debouncer when the expression changes: the baseline reads
    the file's NEW bytes. The flush that follows must not diff them against
    themselves ("no content change" for a real modification)."""
    a1 = b'{"v": 1}\n'
    setup = Setup(tmp_dir, None)
    setup.watcher = Watcher(setup.caller, setup.store, lambda c: None, baseline_reader=setup.read)
    rows = pending(setup.store, "package.json")
    await setup.baseline({"package.json": a1})
    assert "package.json" not in setup.store.get_snapshots("p")
    event_id = await notifier_for(setup).flush("p", rows)
    assert event_id is not None
    row = setup.store.event_contents(event_id)["package.json"]
    assert row.before != row.after
    assert (row.before, row.after) == (None, a1)  # falls back once


async def test_a_subscription_replaced_during_the_read_writes_nothing(tmp_dir: Path) -> None:
    """A reconnect during the read replaced the subscription: the old PDU is dropped
    like a stale one (the new subscription re-baselines)."""
    entered, go = threading.Event(), threading.Event()

    def reader(project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        entered.set()
        go.wait(5)
        return setup.read(project, hashes)

    setup = Setup(tmp_dir, reader)
    task = asyncio.create_task(setup.baseline({"package.json": b"{}\n"}))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    old = setup.caller.subscribes()[-1][2]
    await setup.watcher.handle({"_diffbell": "connected"})  # subscriptions died: a new one
    assert setup.caller.subscribes()[-1][2] != old
    go.set()
    await task
    assert setup.store.get_hashes("p") == {}
    assert setup.store.get_snapshots("p") == {}
    assert setup.store.get_clock("p") is None


async def test_a_reconcile_during_the_read_drops_the_old_baseline_pdu(tmp_dir: Path) -> None:
    """The realistic path: a reload changed the project's patterns (same folder, so
    the token still matches) while the baseline was read. The reconcile replaced the
    subscription; the old PDU, read under the old expression, writes nothing.
    Catches dropping the liveness re-check (the token check alone lets it through)."""
    entered, go = threading.Event(), threading.Event()

    def reader(project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        entered.set()
        go.wait(5)
        return setup.read(project, hashes)

    setup = Setup(tmp_dir, reader)
    task = asyncio.create_task(setup.baseline({"package.json": b"{}\n"}))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    old = setup.caller.subscribes()[-1][2]
    changed = ResolvedProject("p", setup.root, (*WATCHED, "*.json"), ())
    await setup.watcher.reconcile([changed])
    assert setup.caller.subscribes()[-1][2] != old
    go.set()
    await task
    assert setup.store.get_hashes("p") == {}
    assert setup.store.get_snapshots("p") == {}
    assert setup.store.get_clock("p") is None


async def test_the_reader_is_never_called_for_a_catchup_fresh_instance(tmp_dir: Path) -> None:
    """Only an explicit REBASELINE reads contents: a fresh instance in CATCHUP mode
    (a reconnect resuming from the stored clock) is diffed against the stored hashes
    and its paths' events capture their own afters."""
    calls: list[str] = []

    def reader(project: ResolvedProject, hashes: Mapping[str, str]) -> dict[str, bytes]:
        calls.append(project.name)
        return setup.read(project, hashes)

    setup = Setup(tmp_dir, reader)
    await setup.baseline({"package.json": b"{}\n"})
    assert calls == ["p"]
    await setup.watcher.handle({"_diffbell": "disconnected"})
    await setup.watcher.handle({"_diffbell": "connected"})  # resubscribes in CATCHUP mode
    (setup.root / "package.json").write_bytes(b'{"v": 2}\n')
    await setup.watcher.handle(
        {
            "subscription": setup.caller.subscribes()[-1][2],
            "root": str(setup.root),
            "clock": "c:3",
            "is_fresh_instance": True,
            "files": [entry("package.json", sha(b'{"v": 2}\n'))],
        }
    )
    assert calls == ["p"]
    assert setup.store.get_snapshots("p") == {"package.json": b"{}\n"}  # untouched
    assert [c.path for c in setup.store.unflushed_changes()] == ["package.json"]


def test_read_baseline_stops_opening_files_once_the_budget_is_full(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each read is bounded by what is left of the budget, and a full budget reads
    no further file: a file that doesn't fit gets nothing, as before."""
    monkeypatch.setattr(snapshots_mod, "PROJECT_SNAPSHOT_BUDGET", 10)
    root = tmp_dir / "proj"
    root.mkdir()
    files = {"a.lock": b"aaaa\n", "b.lock": b"bbbbbbb\n", "c.lock": b"cc\n", "d.lock": b"ddd\n"}
    for name, data in files.items():
        (root / name).write_bytes(data)
    calls: list[tuple[str, int]] = []

    def reader(root: Path, path: str, redact: Any, max_bytes: int) -> bytes | None:
        calls.append((path, max_bytes))
        return read_capturable(root, path, redact, max_bytes)

    monkeypatch.setattr(snapshots_mod, "read_capturable", reader)
    hashes = {name: sha(data) for name, data in files.items()}
    got = Snapshotter(tmp_dir).read_baseline(root, hashes, BUILTIN_REDACT)
    assert got == {"a.lock": b"aaaa\n", "c.lock": b"cc\n"}
    # a (5) leaves 5, b (8) doesn't fit, c (3) leaves 2, d (4) doesn't fit.
    assert calls == [("a.lock", 10), ("b.lock", 5), ("c.lock", 5), ("d.lock", 2)]
    calls.clear()
    full = {"a.lock": b"aaaa\n", "e.lock": b"eeeee"}
    (root / "e.lock").write_bytes(b"eeeee")
    got = Snapshotter(tmp_dir).read_baseline(
        root, {n: sha(d) for n, d in full.items()} | {"f.lock": "x"}, BUILTIN_REDACT
    )
    assert got == full
    assert [path for path, _ in calls] == ["a.lock", "e.lock"]  # f.lock never opened
