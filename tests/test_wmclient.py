from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
from typing import Any

import pytest
import pywatchman

from diffbell.wmclient import CONNECTED, DISCONNECTED, WatchmanClient, WatchmanUnavailable
from tests.fakes import FakeWatchmanClient, VersionedWatchmanClient


def pdu(name: str, root: str = "/r") -> dict[str, Any]:
    return {"subscription": name, "root": root, "clock": "c:1", "files": []}


def make() -> tuple[WatchmanClient, list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = []
    return WatchmanClient(messages.append, client_factory=FakeWatchmanClient), messages


def submit(wm: WatchmanClient, *args: Any) -> concurrent.futures.Future[Any]:
    fut: concurrent.futures.Future[Any] = concurrent.futures.Future()
    wm._commands.put((args, fut))
    return fut


def test_received_pdu_is_forwarded_and_buffers_drained() -> None:
    wm, messages = make()
    fake = FakeWatchmanClient()
    fake.incoming.append(pdu("diffbell:a"))
    wm.step(fake)
    assert [m["subscription"] for m in messages] == ["diffbell:a"]
    assert fake.subs == {}
    assert fake.sub_by_root == {"/r": {}}


def test_buffers_stay_empty_over_many_pdus() -> None:
    wm, messages = make()
    fake = FakeWatchmanClient()
    fake.incoming.extend(pdu(f"diffbell:{i % 3}", root=f"/r{i % 2}") for i in range(50))
    for _ in range(50):
        wm.step(fake)
    assert len(messages) == 50
    assert fake.subs == {}
    assert all(not by_name for by_name in fake.sub_by_root.values())


def test_pdus_arriving_during_a_query_are_forwarded() -> None:
    wm, messages = make()
    fake = FakeWatchmanClient()
    fake.during_query.append(pdu("diffbell:a"))
    fut = submit(wm, "watch-project", "/x")
    wm.step(fake)
    assert fut.result()["args"] == ["watch-project", "/x"]
    assert [m["subscription"] for m in messages] == ["diffbell:a"]
    assert fake.subs == {}


def test_command_error_fails_only_that_command() -> None:
    wm, _ = make()
    fake = FakeWatchmanClient()
    fake.query_error = pywatchman.CommandError("unable to resolve root")
    bad = submit(wm, "watch-project", "/missing")
    good = submit(wm, "clock", "/r")
    wm.step(fake)
    assert isinstance(bad.exception(), pywatchman.CommandError)
    assert good.result()["args"] == ["clock", "/r"]


def test_error_pdu_from_receive_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    wm, _ = make()
    fake = FakeWatchmanClient()
    fake.receive_error = pywatchman.CommandError("root was deleted")
    wm.step(fake)  # must not raise
    assert "root was deleted" in caplog.text


async def test_call_round_trips_through_the_thread() -> None:
    wm, messages = make()
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while not wm.connected and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        result = await wm.call("clock", "/r")
        assert result["args"] == ["clock", "/r"]
        assert messages[0] == CONNECTED
    finally:
        wm.stop()


async def test_call_while_disconnected_raises() -> None:
    wm, _ = make()
    with pytest.raises(WatchmanUnavailable):
        await wm.call("clock", "/r")


def test_reconnects_after_connection_error() -> None:
    clients: list[FakeWatchmanClient] = []

    def factory() -> FakeWatchmanClient:
        client = FakeWatchmanClient()
        if not clients:
            client.receive_error = pywatchman.WatchmanError("socket closed")
        clients.append(client)
        return client

    messages: list[dict[str, Any]] = []
    wm = WatchmanClient(messages.append, client_factory=factory, min_backoff=0.01)
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while len(messages) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        wm.stop()
    assert messages[:3] == [CONNECTED, DISCONNECTED, CONNECTED]


def test_commands_run_with_the_command_timeout_and_receive_with_the_short_one() -> None:
    # pywatchman uses one socket timeout for query() and receive(); a slow command under
    # the short receive timeout would raise SocketTimeout and be treated as a lost link.
    messages: list[dict[str, Any]] = []
    wm = WatchmanClient(
        messages.append,
        client_factory=FakeWatchmanClient,
        recv_timeout=0.2,
        command_timeout=30.0,
    )
    fake = FakeWatchmanClient()
    submit(wm, "watch-project", "/x")
    wm.step(fake)
    assert fake.query_timeouts == [30.0]
    assert fake.timeout == 0.2


def test_each_connection_gets_private_buffers() -> None:
    # pywatchman.client declares `subs` / `sub_by_root` as class attributes, so every
    # instance shares them unless the owner thread gives each connection its own.
    shared_subs: dict[str, list[dict[str, Any]]] = {"stale": [pdu("stale")]}
    clients: list[FakeWatchmanClient] = []

    def factory() -> FakeWatchmanClient:
        client = FakeWatchmanClient()
        client.subs = shared_subs
        clients.append(client)
        return client

    messages: list[dict[str, Any]] = []
    wm = WatchmanClient(messages.append, client_factory=factory)
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while not wm.connected and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        wm.stop()
    assert clients[0].subs is not shared_subs
    assert messages == [CONNECTED]


class _ScriptedConn:
    """Stands in for pywatchman's codec: receive() pops scripted PDUs, then times out."""

    def __init__(self, pdus: list[dict[str, Any]]) -> None:
        self.pdus = pdus

    def receive(self) -> dict[str, Any]:
        if not self.pdus:
            raise pywatchman.SocketTimeout("timed out")
        return self.pdus.pop(0)

    def send(self, *args: Any) -> None:
        pass

    def setTimeout(self, value: float) -> None:
        pass


def scripted_client(pdus: list[dict[str, Any]]) -> Any:
    from diffbell.wmclient import _Client

    client = _Client()
    client.subs = {}
    client.sub_by_root = {}
    client.recvConn = client.sendConn = _ScriptedConn(pdus)
    client.tport = object()
    client.pid = os.getpid()
    return client


def test_unilateral_error_during_a_query_does_not_desync_commands() -> None:
    # Real pywatchman raises CommandError for any PDU carrying "error", even a unilateral
    # one that arrives while query() waits; the command then fails with someone else's
    # error and every later command reads the previous command's response.
    wm, messages = make()
    client = scripted_client(
        [
            {"error": "unilateral boom", "unilateral": True},
            {"clock": "c:A"},
            {"clock": "c:B"},
            {"clock": "c:C"},
        ]
    )
    futs = [submit(wm, "clock", name) for name in "ABC"]
    try:
        wm.step(client)
    finally:
        client.tport = None
    assert [f.result()["clock"] for f in futs] == ["c:A", "c:B", "c:C"]
    errors = [m for m in messages if m.get("_diffbell") == "error"]
    assert errors == [
        {"_diffbell": "error", "pdu": {"error": "unilateral boom", "unilateral": True}}
    ]


def test_non_unilateral_error_still_fails_the_command() -> None:
    wm, _ = make()
    client = scripted_client([{"error": "unable to resolve root"}, {"clock": "c:B"}])
    bad = submit(wm, "watch-project", "/missing")
    good = submit(wm, "clock", "/r")
    try:
        wm.step(client)
    finally:
        client.tport = None
    assert isinstance(bad.exception(), pywatchman.CommandError)
    assert good.result()["clock"] == "c:B"


def test_unilateral_error_from_the_fake_during_query_is_emitted() -> None:
    wm, messages = make()
    fake = FakeWatchmanClient()
    fake.during_query.append({"error": "root gone", "unilateral": True})
    fut = submit(wm, "clock", "/r")
    wm.step(fake)
    assert fut.result()["args"] == ["clock", "/r"]
    assert {"_diffbell": "error", "pdu": {"error": "root gone", "unilateral": True}} in messages
    assert fake.unilateral_errors == []


def test_error_from_receive_is_emitted_as_a_message() -> None:
    wm, messages = make()
    fake = FakeWatchmanClient()
    err = pywatchman.CommandError("root was deleted")
    fake.receive_error = err
    wm.step(fake)
    assert [m for m in messages if m.get("_diffbell") == "error"] == [
        {"_diffbell": "error", "message": str(err)}
    ]


def test_connection_level_command_failure_is_watchman_unavailable() -> None:
    wm, _ = make()
    fake = FakeWatchmanClient()
    fake.query_error = pywatchman.SocketTimeout("timed out")
    fut = submit(wm, "clock", "/r")
    with pytest.raises(pywatchman.SocketTimeout):
        wm.step(fake)
    exc = fut.exception()
    assert isinstance(exc, WatchmanUnavailable)
    assert isinstance(exc.__cause__, pywatchman.SocketTimeout)


def test_on_message_raising_does_not_kill_the_connection() -> None:
    clients: list[FakeWatchmanClient] = []

    def factory() -> FakeWatchmanClient:
        client = FakeWatchmanClient()
        client.incoming.append(pdu("diffbell:a"))
        clients.append(client)
        return client

    seen: list[dict[str, Any]] = []

    def on_message(msg: dict[str, Any]) -> None:
        seen.append(msg)
        raise RuntimeError("event loop closed")

    wm = WatchmanClient(on_message, client_factory=factory, min_backoff=0.01)
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        assert wm._thread is not None and wm._thread.is_alive()
        assert wm.connected
        assert len(clients) == 1
    finally:
        wm.stop()
    assert [m.get("_diffbell") or m.get("subscription") for m in seen] == [
        "connected",
        "diffbell:a",
    ]


def test_on_message_raising_on_disconnect_keeps_reconnecting() -> None:
    clients: list[FakeWatchmanClient] = []

    def factory() -> FakeWatchmanClient:
        client = FakeWatchmanClient()
        if not clients:
            client.receive_error = pywatchman.WatchmanError("socket closed")
        clients.append(client)
        return client

    def on_message(msg: dict[str, Any]) -> None:
        if msg == DISCONNECTED:
            raise RuntimeError("event loop closed")

    wm = WatchmanClient(on_message, client_factory=factory, min_backoff=0.01)
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while not (len(clients) >= 2 and wm.connected) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert wm._thread is not None and wm._thread.is_alive()
        assert wm.connected
    finally:
        wm.stop()


async def test_call_after_stop_raises_promptly() -> None:
    wm, _ = make()
    wm._connected.set()  # as if the thread were connected but not yet torn down
    wm.stop()
    with pytest.raises(WatchmanUnavailable):
        await asyncio.wait_for(wm.call("clock", "/r"), timeout=1)


def test_start_twice_raises() -> None:
    wm, _ = make()
    wm.start()
    try:
        with pytest.raises(RuntimeError):
            wm.start()
    finally:
        wm.stop()


def test_default_factory_connects_with_the_command_timeout() -> None:
    from diffbell.wmclient import _Client

    wm, _ = make()
    built = wm._default_factory()
    assert isinstance(built, _Client)
    assert built.timeout == 30.0
    assert built.unilateral_errors == []


def test_each_connection_gets_private_logs() -> None:
    shared_logs = ["stale"]
    clients: list[FakeWatchmanClient] = []

    def factory() -> FakeWatchmanClient:
        client = FakeWatchmanClient()
        client.logs = shared_logs
        clients.append(client)
        return client

    wm = WatchmanClient(lambda _m: None, client_factory=factory, recv_timeout=0.05)
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while not wm.connected and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        wm.stop()
    assert clients[0].logs is not shared_logs
    assert clients[0].timeout == 0.05  # receive timeout restored after `version`


def test_version_is_kept_from_the_connect_query_and_cleared_on_disconnect() -> None:
    clients: list[VersionedWatchmanClient] = []

    def factory() -> VersionedWatchmanClient:
        clients.append(VersionedWatchmanClient())
        return clients[-1]

    wm = WatchmanClient(lambda msg: None, client_factory=factory, min_backoff=10.0)
    assert wm.version is None
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while not wm.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert wm.version == VersionedWatchmanClient.VERSION
        # The version comes from the one connect-time query: no extra Watchman call.
        assert clients[0].queries == [("version",)]
        clients[0].receive_error = pywatchman.WatchmanError("socket closed")
        while wm.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert wm.version is None
    finally:
        wm.stop()


def test_version_is_none_when_the_reply_has_no_version() -> None:
    wm, _ = make()
    wm.start()
    try:
        deadline = time.monotonic() + 5
        while not wm.connected and time.monotonic() < deadline:
            time.sleep(0.01)
        assert wm.connected
        assert wm.version is None  # FakeWatchmanClient's reply carries no "version"
    finally:
        wm.stop()
