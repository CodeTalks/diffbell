"""Watcher against real Watchman (§10 integration matrix, watcher-level rows).

Projects are directories under the session's shared watch root (so every project is
subscribed with a relative_root); one test uses a root of its own to cover the
project-is-the-root case."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from diffbell.config import ResolvedProject
from diffbell.models import Change, ChangeKind
from diffbell.store import Store
from diffbell.watcher import Watcher
from diffbell.wmclient import WatchmanClient

pytestmark = pytest.mark.integration

DEFAULT_WATCHED = ("package.json", ".env*", "*.lock", "Makefile")


def resolved(
    path: Path, watched: tuple[str, ...] = DEFAULT_WATCHED, name: str = "p"
) -> ResolvedProject:
    return ResolvedProject(name, path, watched, ())


async def wait_for(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


class Harness:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.changes: list[Change] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self, projects: list[ResolvedProject]) -> None:
        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_message(m: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(inbox.put_nowait, m)

        self.wm = WatchmanClient(on_message, recv_timeout=0.1)
        self.watcher = Watcher(self.wm, self.store, self.changes.extend)
        # As the daemon does before it subscribes (stable project ids spec §2).
        self.store.sync_projects([(p.name, str(p.path)) for p in projects], 0.0)
        await self.watcher.reconcile(projects)
        self.wm.start()

        async def pump() -> None:
            while True:
                await self.watcher.handle(await inbox.get())

        self._task = asyncio.create_task(pump())
        await self.ready(projects)

    async def ready(self, projects: list[ResolvedProject]) -> None:
        def subscribed() -> bool:
            states = {s["name"]: s for s in self.watcher.status()}
            return all(
                states.get(p.name, {}).get("status") == "subscribed"
                and self.store.get_clock(p.name) is not None
                for p in projects
            )

        await wait_for(subscribed)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.wm.stop()

    def kinds(self, path: str) -> list[ChangeKind]:
        return [c.kind for c in self.changes if c.path == path]


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    return Store(tmp_dir / "state.db")


@pytest.fixture
def project(shared_dir: Path) -> Path:
    return shared_dir


async def test_created_modified_deleted_recreated(
    store: Store, tmp_project: Path, watchman_cleanup: list[Path]
) -> None:
    """Also the one project that is a watch root itself (relative_root "")."""
    watchman_cleanup.append(tmp_project)
    h = Harness(store)
    await h.start([resolved(tmp_project)])
    try:
        f = tmp_project / "package.json"
        f.write_text("{}\n")
        await wait_for(lambda: h.kinds("package.json") == [ChangeKind.CREATED])
        f.write_text('{"a": 1}\n')
        await wait_for(lambda: len(h.kinds("package.json")) == 2)
        f.unlink()
        await wait_for(lambda: len(h.kinds("package.json")) == 3)
        f.write_text("{}\n")
        await wait_for(lambda: len(h.kinds("package.json")) == 4)
        assert h.kinds("package.json") == [
            ChangeKind.CREATED,
            ChangeKind.MODIFIED,
            ChangeKind.DELETED,
            ChangeKind.CREATED,
        ]
        assert "package.json" in store.get_hashes("p")
        clock = store.get_clock("p")
        assert clock is not None
        assert (clock.watch_root, clock.relative_root) == (str(tmp_project), "")
    finally:
        await h.stop()


async def test_touch_with_same_content_is_ignored(store: Store, project: Path) -> None:
    h = Harness(store)
    await h.start([resolved(project)])
    try:
        f = project / "Makefile"
        f.write_text("all:\n")
        await wait_for(lambda: len(h.changes) == 1)
        os.utime(f)
        await asyncio.sleep(1.5)
        assert len(h.changes) == 1
    finally:
        await h.stop()


async def test_non_key_and_excluded_paths_are_ignored(store: Store, project: Path) -> None:
    h = Harness(store)
    await h.start([resolved(project)])
    try:
        for rel in [
            "README.md",
            "node_modules/x/package.json",
            ".worktrees/f/package.json",
            "a/dist/package.json",
            "a/b/node_modules/package.json",
        ]:
            target = project / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n")
        await asyncio.sleep(1.5)
        assert h.changes == []
    finally:
        await h.stop()


async def test_symlink_touch_is_ignored(store: Store, project: Path) -> None:
    (project / "target.txt").write_text("x\n")
    h = Harness(store)
    await h.start([resolved(project, ("*.lock",))])
    try:
        link = project / "deps.lock"
        link.symlink_to("target.txt")
        await wait_for(lambda: h.kinds("deps.lock") == [ChangeKind.CREATED])
        os.utime(link, follow_symlinks=False)
        await asyncio.sleep(1.5)
        assert h.kinds("deps.lock") == [ChangeKind.CREATED]
    finally:
        await h.stop()


async def test_changes_while_down_are_reported_once_after_restart(
    store: Store, project: Path
) -> None:
    f, gone = project / "package.json", project / "Makefile"
    first = Harness(store)
    await first.start([resolved(project)])
    f.write_text("{}\n")
    gone.write_text("all:\n")
    await wait_for(lambda: len(first.changes) == 2)
    await first.stop()

    f.write_text('{"while": "down"}\n')
    gone.unlink()

    second = Harness(store)
    await second.start([resolved(project)])
    try:
        await wait_for(lambda: len(second.changes) == 2)
        await asyncio.sleep(1.0)
        assert sorted((c.path, c.kind) for c in second.changes) == [
            ("Makefile", ChangeKind.DELETED),
            ("package.json", ChangeKind.MODIFIED),
        ]
    finally:
        await second.stop()


async def test_pattern_change_rebaselines_silently(store: Store, project: Path) -> None:
    (project / "package.json").write_text("{}\n")
    (project / "Makefile").write_text("all:\n")
    h = Harness(store)
    narrow = resolved(project, ("package.json",))
    wide = resolved(project, ("package.json", "Makefile"))
    await h.start([narrow])
    try:
        await h.watcher.reconcile([wide])
        await h.ready([wide])
        await asyncio.sleep(1.0)
        await h.watcher.reconcile([narrow])
        await h.ready([narrow])
        await asyncio.sleep(1.0)
        assert h.changes == []  # no spurious created/deleted from the pattern edits
    finally:
        await h.stop()


async def test_nested_project_under_watched_ancestor(
    store: Store, shared_root: Path, shared_dir: Path
) -> None:
    """The ancestor (the shared root) is already watched: the project is subscribed
    under it with a multi-level relative_root, and reports project-relative paths."""
    child = shared_dir / "par" / "child"
    (child / "sub").mkdir(parents=True)
    h = Harness(store)
    await h.start([resolved(child, ("sub/package.json",))])
    try:
        (child / "sub" / "package.json").write_text("{}\n")
        await wait_for(lambda: len(h.changes) == 1)
        assert h.changes[0].path == "sub/package.json"
        clock = store.get_clock("p")
        assert clock is not None
        assert (clock.watch_root, clock.relative_root) == (
            str(shared_root),
            f"{shared_dir.name}/par/child",
        )
    finally:
        await h.stop()
