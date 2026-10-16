"""Stored diffs end to end on real Watchman (one shared root, UI refine spec §7):
a real edit is delivered (fake Telegram), /api/events/{id} returns the stored
diff with the changed line, and a redacted file's marker appears nowhere: not in
any API response and not in a raw byte scan of state.db and its WAL."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from diffbell.daemon import Daemon
from diffbell.store import Store
from diffbell.web import auth
from tests.fakes import FakeSender, fake_slot
from tests.helpers import free_port, git, init_repo
from tests.integration.test_daemon import wait_for

pytestmark = pytest.mark.integration

MARKER = "REDACTED-MARKER-9b3e71"


def events_with(store: Store, path: str) -> list[int]:
    rows, _ = store.query_events(q=path)
    return [r.event.id for r in rows if any(c.path == path for c in r.changes)]


async def test_a_real_edit_is_stored_and_served_without_the_redacted_marker(
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
    bodies: list[str] = []
    try:
        await wait_for(lambda: store.get_clock("proj") is not None)
        await wait_for(lambda: daemon.status_snapshot()["web"]["running"] is True)
        (proj / "package.json").write_text('{\n  "version": "1.0.1"\n}\n')
        (proj / ".env.local").write_text(f"API_TOKEN={MARKER}\n")
        await wait_for(lambda: bool(events_with(store, "package.json")))
        await wait_for(lambda: bool(events_with(store, ".env.local")))
        await wait_for(lambda: store.status_counts()["pending"] == 0)
        assert sender.sent and all(MARKER not in text for text, _ in sender.sent)
        secret = bytes.fromhex(daemon.status_snapshot()["web"]["secret"])
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            token = auth.issue_login(store, daemon._clock(), secret)
            assert (await client.get(f"/login?t={token}")).status_code == 303
            client.headers[auth.CSRF_HEADER] = (await client.get("/api/session")).json()["csrf"]

            (package_event,) = events_with(store, "package.json")
            detail = await client.get(f"/api/events/{package_event}")
            bodies.append(detail.text)
            assert detail.status_code == 200
            body = detail.json()
            assert body["status"] == "sent" and body["branch"] == "main"
            assert body["telegram_lines"] == 20 and body["telegram_files"] >= 1
            package = next(r for r in body["reports"] if r["path"] == "package.json")
            assert '+  "version": "1.0.1"' in package["lines"]
            assert '-  "version": "1.0.0"' in package["lines"]
            assert (body["added"], body["removed"]) == (1, 1)

            for event_id in events_with(store, ".env.local"):
                response = await client.get(f"/api/events/{event_id}")
                bodies.append(response.text)
                env = next(r for r in response.json()["reports"] if r["path"] == ".env.local")
                assert env["redacted"] is True and env["lines"] == []

            for response in (
                await client.get("/api/events"),
                await client.get("/api/dashboard"),
                await client.post("/api/browse", json={"path": str(proj)}),
                await client.post("/api/preview", json={"path": str(proj)}),
            ):
                assert response.status_code == 200, response.text
                bodies.append(response.text)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        store.close()
        config.unlink(missing_ok=True)
    assert bodies and all(MARKER not in body for body in bodies)
    for name in ("state.db", "state.db-wal", "state.db-shm"):
        path = state / name
        if path.exists():
            assert MARKER.encode() not in path.read_bytes(), name
