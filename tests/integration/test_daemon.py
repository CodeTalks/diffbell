"""End-to-end with real Watchman and a fake Telegram sender (§10 daemon-level rows).

Projects live in a per-test directory under the session's shared watch root, and
the config file sits at the top level of that root (the daemon `watch`es the
config's directory, which then resolves to the shared root too), so these tests
create no Watchman roots of their own."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from diffbell.daemon import Daemon
from diffbell.store import Store
from diffbell.web import config_edit
from diffbell.web.config_edit import ProjectInput
from tests.fakes import FakeSender, fake_slot
from tests.helpers import git, init_repo

pytestmark = pytest.mark.integration


async def wait_for(pred: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def write_config(path: Path, projects: list[Path], quiet: float = 0.3) -> Path:
    lines = [
        "debounce:",
        f"  quiet_seconds: {quiet}",
        f"  max_wait_seconds: {max(quiet, 3)}",
        "projects:",
    ]
    lines += [f"  - path: {p}" for p in projects]
    path.write_text("\n".join(lines) + "\n")
    return path


class Running:
    def __init__(self, config: Path, store: Store, sender: FakeSender, state_dir: Path) -> None:
        self.daemon = Daemon(
            config,
            store,
            fake_slot(sender),
            state_dir=state_dir,
            recv_timeout=0.1,
            maintenance_interval=0.2,
            missing_recheck_interval=0.5,
        )
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self.daemon.run(self.stop))

    async def close(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=10)


@contextlib.asynccontextmanager
async def running(
    config: Path, store: Store, sender: FakeSender, state_dir: Path, projects: list[str]
) -> AsyncIterator[Running]:
    r = Running(config, store, sender, state_dir)
    try:
        await wait_for(lambda: all(store.get_clock(p) is not None for p in projects))
        yield r
    finally:
        await r.close()


@pytest.fixture
def config_path(shared_root: Path, shared_dir: Path) -> Path:
    """A per-test config file at the top level of the shared root (the config
    subscription matches the file name at the root's top level only)."""
    return shared_root / f"{shared_dir.name}.yaml"


@pytest.fixture
def env(tmp_dir: Path, shared_dir: Path, config_path: Path) -> tuple[Path, Path, Store, FakeSender]:
    proj = shared_dir / "proj"
    proj.mkdir()
    config = write_config(config_path, [proj])
    return proj, config, Store(tmp_dir / "state" / "state.db"), FakeSender()


async def test_burst_of_edits_is_one_message(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path
) -> None:
    proj, config, store, sender = env
    async with running(config, store, sender, tmp_dir / "state", ["proj"]):
        (proj / "package.json").write_text("{}\n")
        (proj / "Makefile").write_text("all:\n")
        await wait_for(lambda: len(sender.sent) >= 1)
        await asyncio.sleep(1.0)
    assert len(sender.sent) == 1
    text = sender.sent[0][0]
    assert (
        "package.json" in text and "Makefile" in text and text.splitlines()[1].startswith("2 files")
    )


async def test_git_checkout_is_one_message(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path
) -> None:
    proj, config, store, sender = env
    init_repo(proj)
    (proj / "package.json").write_text('{"v": 1}\n')
    (proj / "Makefile").write_text("v1:\n")
    git(proj, "add", ".")
    git(proj, "commit", "-q", "-m", "v1")
    git(proj, "checkout", "-q", "-b", "feat")
    (proj / "package.json").write_text('{"v": 2}\n')
    (proj / "Makefile").write_text("v2:\n")
    git(proj, "commit", "-q", "-am", "v2")
    git(proj, "checkout", "-q", "main")
    async with running(config, store, sender, tmp_dir / "state", ["proj"]):
        git(proj, "checkout", "-q", "feat")
        await wait_for(lambda: len(sender.sent) >= 1)
        await asyncio.sleep(1.5)
    assert len(sender.sent) == 1
    assert "package.json" in sender.sent[0][0] and "Makefile" in sender.sent[0][0]


async def test_redacted_file_never_leaks(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path
) -> None:
    proj, config, store, sender = env
    async with running(config, store, sender, tmp_dir / "state", ["proj"]):
        (proj / ".env.local").write_text("API_KEY=SECRET-VALUE\n")
        await wait_for(lambda: len(sender.sent) >= 1)
    text = sender.sent[0][0]
    assert ".env.local" in text and "contents hidden" in text
    assert "SECRET-VALUE" not in text


async def test_stop_during_debounce_window_reports_after_restart(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path
) -> None:
    proj, config, store, sender = env
    slow = write_config(config, [proj], quiet=30)
    async with running(slow, store, sender, tmp_dir / "state", ["proj"]):
        (proj / "package.json").write_text("{}\n")
        await wait_for(lambda: len(store.unflushed_changes()) == 1)
    assert sender.sent == []  # stopped inside the 30 s window

    fast = write_config(config, [proj], quiet=0.3)
    async with running(fast, store, sender, tmp_dir / "state", ["proj"]):
        await wait_for(lambda: len(sender.sent) >= 1)
        await asyncio.sleep(1.0)
    assert len(sender.sent) == 1
    assert "package.json" in sender.sent[0][0]


async def test_config_reload_adds_projects_and_rejects_invalid_edits(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path, shared_dir: Path
) -> None:
    proj, config, store, sender = env
    other = shared_dir / "other"
    other.mkdir()
    async with running(config, store, sender, tmp_dir / "state", ["proj"]):
        write_config(config, [proj, other])
        await wait_for(lambda: store.get_clock("other") is not None)
        (other / "package.json").write_text("{}\n")
        await wait_for(lambda: any(t.startswith("<b>🔑 other</b>") for t, _ in sender.sent))

        config.write_text("debounce: [not, a, mapping]\n")
        await wait_for(lambda: any("config rejected" in t for t, _ in sender.sent))

        before = len(sender.sent)
        (proj / "package.json").write_text("{}\n")  # last good config still active
        await wait_for(lambda: len(sender.sent) > before)
        assert sender.sent[-1][0].startswith("<b>🔑 proj</b>")


async def test_symlinked_config_reloads_after_a_web_edit(
    tmp_dir: Path, shared_dir: Path, config_path: Path, watchman_cleanup: list[Path]
) -> None:
    # dotfiles setup: the config path is a symlink into another directory, and a web
    # save (config_edit) rewrites the target there, never the link's directory.
    proj, other = shared_dir / "proj", shared_dir / "other"
    proj.mkdir()
    other.mkdir()
    (shared_dir / "dotfiles").mkdir()
    # The daemon `watch`es the symlink target's directory, a nested root of its own.
    watchman_cleanup.append(shared_dir / "dotfiles")
    real = write_config(shared_dir / "dotfiles" / "real.yaml", [proj])
    config_path.symlink_to(real)
    store, sender = Store(tmp_dir / "state" / "state.db"), FakeSender()
    async with running(config_path, store, sender, tmp_dir / "state", ["proj"]):
        version = config_edit.config_version(config_path)
        config_edit.add_project(config_path, version, ProjectInput(str(other), None, [], [], True))
        assert config_path.is_symlink()
        await wait_for(lambda: store.get_clock("other") is not None)


async def test_status_snapshot_is_written(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path
) -> None:
    _, config, store, sender = env
    async with running(config, store, sender, tmp_dir / "state", ["proj"]):
        status = tmp_dir / "state" / "status.json"
        await wait_for(status.exists)
        await wait_for(lambda: '"subscribed"' in status.read_text())


async def test_missing_project_is_skipped_and_watched_once_it_appears(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path, shared_dir: Path
) -> None:
    proj, config, store, sender = env
    gone = shared_dir / "gone"  # does not exist when the daemon starts
    write_config(config, [proj, gone])
    async with running(config, store, sender, tmp_dir / "state", ["proj"]) as r:
        # A page-only warning (health spec §4.2): recorded, never sent.
        await wait_for(lambda: any(p.key == "missing:gone" for p in r.daemon._health.problems()))
        (proj / "package.json").write_text("{}\n")  # the other project is unaffected
        await wait_for(lambda: any(t.startswith("<b>🔑 proj</b>") for t, _ in sender.sent))

        gone.mkdir()
        await wait_for(lambda: store.get_clock("gone") is not None)
        (gone / "package.json").write_text("{}\n")
        await wait_for(lambda: any(t.startswith("<b>🔑 gone</b>") for t, _ in sender.sent))
        assert not any("does not exist" in t for t, _ in sender.sent)


async def test_a_renamed_project_keeps_its_history_and_its_baseline(
    env: tuple[Path, Path, Store, FakeSender], tmp_dir: Path
) -> None:
    """Stable project ids spec §6: rename a watched project in the config. The reload
    keeps its id: no new baseline, the old event is listed under the new name, and
    the next change is reported under it."""
    proj, config, store, sender = env
    async with running(config, store, sender, tmp_dir / "state", ["proj"]) as r:
        (proj / "package.json").write_text("{}\n")
        await wait_for(lambda: len(sender.sent) >= 1)
        [old], _ = store.query_events(project="proj")
        hashes = store.get_hashes("proj")
        assert "package.json" in hashes
        config.write_text(config.read_text() + "    name: Renamed\n")
        watcher = r.daemon._watcher
        assert watcher is not None

        def renamed() -> dict[str, object] | None:
            rows = {row["name"]: row for row in watcher.status()}
            row = rows.get("Renamed")
            return row if row is not None and row["status"] == "subscribed" else None

        await wait_for(lambda: renamed() is not None)
        row = renamed()
        assert row is not None and row["note"] is None  # resumed, never re-baselined
        assert store.get_hashes("Renamed") == hashes
        assert store.get_event(old.event.id).project == "Renamed"
        assert store.query_events(project="proj") == ([], 0)

        (proj / "Makefile").write_text("all:\n")
        await wait_for(lambda: any(t.startswith("<b>🔑 Renamed</b>") for t, _ in sender.sent))
        events, total = store.query_events(project="Renamed")
        assert total == 2 and events[-1].event.id == old.event.id
