"""A lost watch end to end on real Watchman (one shared root, health spec §8): remove
a watched project folder and exactly one "watch lost" message goes out; re-create
it and exactly one "watching again" follows. The fake sender stands in for Telegram.

Below the shared root, Watchman does not cancel the subscription when the folder
goes: the watcher's tick notices the folder is gone, and its retry subscribes it
again once the folder is back (sped up here from 60 s)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from diffbell import BRAND
from diffbell import watcher as watcher_mod
from diffbell.store import Store
from tests.fakes import FakeSender
from tests.integration.test_daemon import running, wait_for, write_config

pytestmark = pytest.mark.integration

# Noticed by the tick ("project folder removed"), or by Watchman should it cancel the
# watch after all: the same problem either way, so one message.
LOST = "⚠️ Diffbell: project proj: watch lost: "
BACK = "✅ Diffbell: project proj: watching again (down "


async def test_a_removed_project_folder_is_reported_once_and_its_return_once(
    shared_root: Path, shared_dir: Path, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watcher_mod, "RETRY_ERRORS_EVERY", 0.5)
    proj = shared_dir / "proj"
    proj.mkdir()
    (proj / "package.json").write_text("{}\n")
    config = write_config(shared_root / f"{shared_dir.name}.yaml", [proj])
    store, sender = Store(tmp_dir / "state" / "state.db"), FakeSender()

    def count(prefix: str) -> int:
        return sum(1 for text, _ in sender.sent if text.startswith(prefix))

    async with running(config, store, sender, tmp_dir / "state", ["proj"]) as r:
        shutil.rmtree(proj)
        await wait_for(lambda: count(LOST) == 1)
        assert [p.key for p in r.daemon._health.problems()] == ["project:proj"]
        await asyncio.sleep(1.5)  # more ticks and retries while it is gone: no repeat
        proj.mkdir()
        await wait_for(lambda: count(BACK) == 1)
        await asyncio.sleep(1.5)  # more ticks and retries once it is back
        assert r.daemon._health.problems() == []
    assert (count(LOST), count(BACK)) == (1, 1)
    alerts = [mode for text, mode in sender.sent if f"{BRAND}:" in text]
    assert alerts and all(mode is None for mode in alerts)
