"""§11: prove the pywatchman call shapes the design relies on."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import pywatchman

pytestmark = pytest.mark.integration


def _wait_pdu(client: Any, name: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pdu: dict[str, Any] = client.receive()
        except pywatchman.SocketTimeout:
            continue
        if pdu.get("subscription") == name:
            return pdu
    raise AssertionError(f"no PDU for subscription {name!r} within {timeout}s")


def test_subscribe_receive_unsubscribe(tmp_project: Path, watchman_cleanup: list[Path]) -> None:
    watchman_cleanup.append(tmp_project)
    client = pywatchman.client(timeout=2)
    try:
        root = client.query("watch-project", str(tmp_project))["watch"]
        clock = client.query("clock", root)["clock"]
        sub = client.query(
            "subscribe",
            root,
            "smoke",
            {
                "expression": ["name", "package.json"],
                "fields": ["name", "exists", "content.sha1hex", "mtime_ms", "size"],
                "since": clock,
                "defer_vcs": True,
            },
        )
        assert sub["subscribe"] == "smoke"

        (tmp_project / "package.json").write_text("{}\n")
        pdu = _wait_pdu(client, "smoke")
        assert [f["name"] for f in pdu["files"]] == ["package.json"]
        assert isinstance(pdu["files"][0]["content.sha1hex"], str)

        # receive() also buffers the PDU; draining with pdu["root"] clears both buffers.
        client.getSubscription("smoke", root=pdu["root"], remove=True)
        assert not client.subs.get("smoke")

        assert client.query("unsubscribe", root, "smoke")["deleted"] is True
        assert client.query("unsubscribe", root, "smoke")["deleted"] is False
    finally:
        client.close()
