"""Snooze and catch-up end to end on real Watchman (one shared root, notify
controls spec §8): a snoozed project's edits are held and nothing is sent; after
Resume exactly one catch-up message goes out, both events share its message id,
and it carries the deep link."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from diffbell.daemon import Daemon
from diffbell.store import EventRow, Store
from diffbell.web import auth
from tests.fakes import FakeSender, fake_slot
from tests.helpers import free_port, git, init_repo
from tests.integration.test_daemon import wait_for

pytestmark = pytest.mark.integration


def events_of(store: Store, project: str) -> list[EventRow]:
    rows, _ = store.query_events(project=project)
    return sorted((r.event for r in rows), key=lambda e: e.id)


@pytest.mark.usefixtures("nameless_mac")
async def test_a_snoozed_project_is_held_then_caught_up_in_one_message(
    shared_root: Path, shared_dir: Path, tmp_dir: Path
) -> None:
    proj = shared_dir / "proj"
    proj.mkdir()
    init_repo(proj)
    (proj / "package.json").write_text('{\n  "version": "1.0.0"\n}\n')
    git(proj, "add", ".")
    git(proj, "commit", "-q", "-m", "init")
    port = free_port()
    config = shared_root / f"{shared_dir.name}.yaml"
    config.write_text(
        f"debounce:\n  quiet_seconds: 0.3\n  max_wait_seconds: 3\n"
        f"web:\n  enabled: true\n  port: {port}\n"
        f"projects:\n  - path: {proj}\n"
    )
    state = tmp_dir / "state"
    store = Store(state / "state.db")
    sender = FakeSender()
    daemon = Daemon(
        config,
        store,
        fake_slot(sender),
        state_dir=state,
        recv_timeout=0.1,
        maintenance_interval=0.2,
        preview_home=shared_root,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    try:
        await wait_for(lambda: store.get_clock("proj") is not None)
        await wait_for(lambda: daemon.status_snapshot()["web"]["running"] is True)
        secret = bytes.fromhex(daemon.status_snapshot()["web"]["secret"])
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            token = auth.issue_login(store, daemon._clock(), secret)
            assert (await client.get(f"/login?t={token}")).status_code == 303
            client.headers[auth.CSRF_HEADER] = (await client.get("/api/session")).json()["csrf"]
            snoozed = await client.post("/api/projects/proj/snooze", json={"until": None})
            assert snoozed.status_code == 200, snoozed.text

            # 1. Snoozed: the edit becomes a held event and nothing is sent.
            (proj / "package.json").write_text('{\n  "version": "1.0.1"\n}\n')
            await wait_for(lambda: [e.status for e in events_of(store, "proj")] == ["held"])
            (first,) = events_of(store, "proj")
            assert first.held_reason == "snooze"

            # 2. Another edit, held too; then Resume: one catch-up for both.
            (proj / "package.json").write_text('{\n  "version": "1.0.2"\n}\n')
            await wait_for(lambda: len(events_of(store, "proj")) == 2)
            await wait_for(lambda: {e.status for e in events_of(store, "proj")} == {"held"})
            assert sender.sent == []
            resumed = await client.delete("/api/projects/proj/snooze")
            assert resumed.status_code == 204
            await wait_for(lambda: {e.status for e in events_of(store, "proj")} == {"sent"})
            await asyncio.sleep(0.5)  # a second tick must not send it again
            first, second = events_of(store, "proj")
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            try:
                store.close()
            finally:
                config.unlink(missing_ok=True)
    assert len(sender.sent) == 1
    text, mode = sender.sent[0]
    assert mode == "HTML"
    assert text.startswith("<b>🔕 Catch-up · proj</b> · <code>main</code> · held ")
    assert text.splitlines()[0].endswith("(snoozed)")
    assert text.splitlines()[1].startswith("2 changes in 1 file")
    assert "+  &quot;version&quot;: &quot;1.0.2&quot;" in text
    assert text.splitlines()[-1] == (
        f'🔗 <a href="http://127.0.0.1:{port}/?event={second.id}">Open #{second.id} (+1 more)</a>'
    )
    assert first.telegram_message_id == second.telegram_message_id is not None
    assert first.batch_id == second.batch_id == first.id
