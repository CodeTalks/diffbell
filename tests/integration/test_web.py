"""The web UI end to end on a real daemon and real Watchman (one shared root).

Login, a preview of real files (computed locally), saving a new project through
the API, and that project becoming `subscribed`, without creating any Watchman
root besides the session's shared one."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pywatchman

from diffbell.daemon import Daemon
from diffbell.store import Store
from diffbell.web import auth
from tests.fakes import fake_slot
from tests.helpers import free_port, port_is_free
from tests.integration.test_daemon import wait_for

pytestmark = pytest.mark.integration


def watch_list() -> list[str]:
    """The private test Watchman's roots (pywatchman follows WATCHMAN_SOCK; the
    `watchman` CLI would ask the user's Watchman instead)."""
    client = pywatchman.client(timeout=5)
    try:
        return [str(root) for root in client.query("watch-list")["roots"]]
    finally:
        client.close()


def under_root(roots: list[str], root: str) -> list[str]:
    """The watched roots equal to ``root`` or nested inside it (not mere prefixes)."""
    return [r for r in roots if r == root or r.startswith(root + "/")]


async def test_login_preview_save_and_subscribe(
    shared_root: Path, shared_dir: Path, tmp_dir: Path
) -> None:
    proj = shared_dir / "proj"
    proj.mkdir()
    other = shared_dir / "other"
    other.mkdir()
    (other / "package.json").write_text('{"secret": "SECRET-IN-FILE"}\n')
    (other / ".env.local").write_text("TOKEN=SECRET-IN-ENV\n")
    (other / "node_modules").mkdir()
    (other / "node_modules" / "package.json").write_text("{}\n")
    port = free_port()
    config = shared_root / f"{shared_dir.name}.yaml"  # top level of the shared root
    config.write_text(
        f"debounce:\n  quiet_seconds: 0.3\n  max_wait_seconds: 3\n"
        f"web:\n  enabled: true\n  port: {port}\n"
        f"projects:\n  - path: {proj}\n"
    )
    # No earlier test in the session may leave a nested root behind (each one
    # watch-dels what it creates), so the shared root is the only root under it.
    assert under_root(watch_list(), str(shared_root)) == [str(shared_root)]
    state = tmp_dir / "state"
    store = Store(state / "state.db")
    daemon = Daemon(
        config,
        store,
        fake_slot(),
        state_dir=state,
        recv_timeout=0.1,
        maintenance_interval=0.2,
        preview_home=shared_root,  # previews must stay under "home"
    )
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    bodies: list[str] = []

    def seen(response: httpx.Response) -> httpx.Response:
        bodies.append(response.text)
        return response

    try:
        await wait_for(lambda: store.get_clock("proj") is not None)
        await wait_for(lambda: daemon.status_snapshot()["web"]["running"] is True)
        secret_hex = daemon.status_snapshot()["web"]["secret"]
        assert isinstance(secret_hex, str)
        secret = bytes.fromhex(secret_hex)
        # The CLI reads the same secret from status.json (owner-only).
        await wait_for(
            lambda: json.loads((state / "status.json").read_text())["web"]["secret"] == secret_hex
        )
        # A real HTTP client against the real socket; never a proxy from the env.
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10
        ) as client:
            # A token minted for another server start is refused.
            stray = auth.issue_login(store, daemon._clock(), b"\x00" * 32)
            assert seen(await client.get(f"/login?t={stray}")).status_code == 400

            token = auth.issue_login(store, daemon._clock(), secret)
            assert seen(await client.get(f"/login?t={token}")).status_code == 303
            page = seen(await client.get("/"))
            assert page.status_code == 200 and "<html" in page.text.lower()
            session = seen(await client.get("/api/session"))
            client.headers[auth.CSRF_HEADER] = session.json()["csrf"]

            preview = seen(await client.post("/api/preview", json={"path": str(other)}))
            assert preview.status_code == 200
            assert preview.json()["files"] == [
                {"path": ".env.local", "redacted": True},
                {"path": "package.json", "redacted": False},
            ]
            assert preview.json()["total"] == 2
            assert "SECRET-IN" not in preview.text  # file contents never leave the disk

            listed = seen(await client.get("/api/projects"))
            version = listed.json()["config_version"]
            saved = seen(
                await client.post(
                    "/api/projects", json={"config_version": version, "path": str(other)}
                )
            )
            assert saved.status_code == 200

            async def status_of(name: str) -> str | None:
                body = seen(await client.get("/api/dashboard")).json()
                return next((p["status"] for p in body["projects"] if p["name"] == name), None)

            deadline = asyncio.get_running_loop().time() + 15
            while await status_of("other") != "subscribed":
                assert asyncio.get_running_loop().time() < deadline, "never subscribed"
                await asyncio.sleep(0.2)
            assert await status_of("proj") == "subscribed"
            assert seen(await client.get("/healthz")).status_code == 200
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        store.close()
        config.unlink(missing_ok=True)
    assert port_is_free(port)  # the daemon's shutdown released the real socket
    # The per-start secret never appears in any HTTP response.
    assert bodies and all(secret_hex not in body for body in bodies)
    # Neither the preview nor the new project created a Watchman root: the shared
    # root is still the only root under it.
    assert under_root(watch_list(), str(shared_root)) == [str(shared_root)]
