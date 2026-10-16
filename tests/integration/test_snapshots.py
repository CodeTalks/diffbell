"""Content snapshots end to end on the private Watchman (content snapshots spec §6):
a project without git shows real lines; a change committed before the event is
processed shows its lines, "committed" (the `+0 −0` case); a held event released
later shows its own burst, not a later edit."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from diffbell.store import Store
from tests.fakes import FakeSender
from tests.helpers import git, init_repo
from tests.integration.test_daemon import running, wait_for, write_config

pytestmark = pytest.mark.integration


def project(shared_dir: Path, files: dict[str, str]) -> Path:
    proj = shared_dir / "proj"
    proj.mkdir()
    for name, text in files.items():
        (proj / name).write_text(text)
    return proj


async def test_a_project_without_git_shows_real_lines(
    shared_root: Path, shared_dir: Path, tmp_dir: Path
) -> None:
    proj = project(shared_dir, {"Makefile": "all:\n\techo one\n"})
    config = write_config(shared_root / f"{shared_dir.name}.yaml", [proj])
    store, sender = Store(tmp_dir / "state" / "state.db"), FakeSender()
    try:
        async with running(config, store, sender, tmp_dir / "state", ["proj"]):
            await wait_for(lambda: store.get_snapshots("proj") != {})
            (proj / "Makefile").write_text("all:\n\techo two\n")
            await wait_for(lambda: len(sender.sent) >= 1)
    finally:
        config.unlink(missing_ok=True)
        store.close()
    text = sender.sent[0][0]
    assert "-\techo one" in text and "+\techo two" in text
    assert "✏️ <code>Makefile</code> +1 −1" in text


async def test_a_change_committed_before_the_event_shows_its_lines(
    shared_root: Path, shared_dir: Path, tmp_dir: Path
) -> None:
    """The live `+0 −0` case: a merge, pull or `commit -a` lands before the debounce
    flush. The diff is the burst's, with the note "committed"."""
    proj = project(shared_dir, {"package.json": '{\n  "version": "1.0.0"\n}\n'})
    init_repo(proj)
    git(proj, "add", ".")
    git(proj, "commit", "-q", "-m", "init")
    config = write_config(shared_root / f"{shared_dir.name}.yaml", [proj])
    store, sender = Store(tmp_dir / "state" / "state.db"), FakeSender()
    try:
        async with running(config, store, sender, tmp_dir / "state", ["proj"]):
            await wait_for(lambda: store.get_snapshots("proj") != {})
            (proj / "package.json").write_text('{\n  "version": "1.1.0"\n}\n')
            git(proj, "commit", "-q", "-am", "bump")
            await wait_for(lambda: len(sender.sent) >= 1)
            (event,) = store.recent_events()
            stored = store.get_event_report(event.id)
    finally:
        config.unlink(missing_ok=True)
        store.close()
    text = sender.sent[0][0]
    assert text.startswith("<b>🔑 proj</b> · <code>main</code>")
    assert "✏️ <code>package.json</code> +1 −1" in text
    assert stored is not None
    assert stored.reports[0]["lines"] == ['-  "version": "1.0.0"', '+  "version": "1.1.0"']
    assert stored.reports[0]["note"] == "committed"


async def test_a_held_event_released_later_shows_its_burst_not_later_edits(
    shared_root: Path, shared_dir: Path, tmp_dir: Path
) -> None:
    proj = project(shared_dir, {"Makefile": "v1:\n"})
    # A long quiet window: the later edit is still debouncing when the hold ends.
    config = write_config(shared_root / f"{shared_dir.name}.yaml", [proj], quiet=2.0)
    store, sender = Store(tmp_dir / "state" / "state.db"), FakeSender()
    try:
        async with running(config, store, sender, tmp_dir / "state", ["proj"]):
            await wait_for(lambda: store.get_snapshots("proj") != {})
            store.set_snooze("proj", time.time(), None)
            (proj / "Makefile").write_text("v2:\n")
            await wait_for(lambda: [e.status for e in store.recent_events()] == ["held"])
            (proj / "Makefile").write_text("v3:\n")  # after the burst: not the hold's
            store.delete_snooze("proj", time.time())
            await wait_for(lambda: len(sender.sent) >= 1)
            await wait_for(lambda: len(sender.sent) >= 2)  # then v3, as its own event
            await asyncio.sleep(0.3)
    finally:
        config.unlink(missing_ok=True)
        store.close()
    catch_up, later = sender.sent[0][0], sender.sent[1][0]
    assert catch_up.startswith("<b>🔕 Catch-up · proj</b>")
    assert "-v1:" in catch_up and "+v2:" in catch_up and "v3" not in catch_up
    assert later.startswith("<b>🔑 proj</b>")
    assert "-v2:" in later and "+v3:" in later
