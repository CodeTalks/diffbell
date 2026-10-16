"""The real connection-owner thread against a real Watchman: commands round-trip and
pywatchman's PDU buffers are drained after every PDU (spec §3).

Both tests work in a directory under the session's shared watch root (subscribing
with relative_root), so they create no Watchman roots of their own."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import pywatchman

from diffbell.wmclient import CONNECTED, WatchmanClient

pytestmark = pytest.mark.integration


async def test_real_thread_forwards_pdus_and_drains_buffers(shared_dir: Path) -> None:
    created: list[Any] = []

    def factory() -> Any:
        client = pywatchman.client(timeout=0.2)
        created.append(client)
        return client

    messages: list[dict[str, Any]] = []
    wm = WatchmanClient(messages.append, client_factory=factory)
    wm.start()
    try:
        deadline = time.monotonic() + 10
        while not wm.connected and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert messages[0] == CONNECTED

        res = await wm.call("watch-project", str(shared_dir))
        root = res["watch"]
        clock = (await wm.call("clock", root))["clock"]
        sub = await wm.call(
            "subscribe",
            root,
            "diffbell:real",
            {
                "expression": ["name", "package.json"],
                "fields": ["name", "exists"],
                "since": clock,
                "relative_root": res["relative_path"],
            },
        )
        assert sub["subscribe"] == "diffbell:real"

        (shared_dir / "package.json").write_text("{}\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            names = [
                f["name"]
                for m in messages
                if m.get("subscription") == "diffbell:real"
                for f in m.get("files", [])
            ]
            if "package.json" in names:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("no PDU for package.json reached on_message")

        await wm.call("unsubscribe", root, "diffbell:real")
    finally:
        wm.stop()  # the thread only exits between steps, i.e. after a drain

    client = created[0]
    assert not client.subs
    assert all(not by_name for by_name in client.sub_by_root.values())


async def test_default_factory_connects_and_runs_commands(shared_dir: Path) -> None:
    messages: list[dict[str, Any]] = []
    wm = WatchmanClient(messages.append)  # the default _Client factory
    wm.start()
    try:
        deadline = time.monotonic() + 10
        while not wm.connected and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert messages[0] == CONNECTED
        root = (await wm.call("watch-project", str(shared_dir)))["watch"]
        assert "clock" in await wm.call("clock", root)
        with pytest.raises(pywatchman.CommandError):
            await wm.call("clock", str(shared_dir / "not-watched"))
        assert "clock" in await wm.call("clock", root)  # still in sync after an error
    finally:
        wm.stop()
    assert wm._thread is not None and not wm._thread.is_alive()
