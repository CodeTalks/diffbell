from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from typing import Any

import httpx
import pytest
from starlette.types import Receive, Scope, Send

from diffbell.web import server as server_mod
from diffbell.web.server import WebServer, WebStartError
from tests.helpers import free_port, port_is_free


async def hello(scope: Scope, receive: Receive, send: Send) -> None:
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"hello"})


async def get(port: int) -> str:
    async with httpx.AsyncClient() as client:
        return (await client.get(f"http://127.0.0.1:{port}/")).text


async def test_start_serves_and_stop_frees_the_port() -> None:
    web = WebServer()
    port = free_port()
    task = await web.start(hello, port)
    assert web.running and web.port == port and not task.done()
    assert await get(port) == "hello"
    await web.stop()
    assert task.done() and not web.running
    assert port_is_free(port)


async def test_each_start_uses_a_fresh_server() -> None:
    web = WebServer()
    port = free_port()
    await web.start(hello, port)
    first = web._server
    await web.stop()
    await web.start(hello, port)  # restart on the same port works right away
    assert web._server is not first
    assert await get(port) == "hello"
    await web.stop()


async def test_port_in_use_is_a_clean_start_error() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        web = WebServer()
        with pytest.raises(WebStartError, match="in use"):
            await web.start(hello, port)
        assert not web.running


async def test_start_twice_is_refused() -> None:
    web = WebServer()
    await web.start(hello, free_port())
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await web.start(hello, free_port())
    finally:
        await web.stop()


async def test_stop_is_idempotent() -> None:
    web = WebServer()
    await web.stop()
    await web.start(hello, free_port())
    await web.stop()
    await web.stop()


async def test_server_does_not_install_signal_handlers() -> None:
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    web = WebServer()
    await web.start(hello, free_port())
    try:
        assert {sig: signal.getsignal(sig) for sig in before} == before
    finally:
        await web.stop()


async def test_a_stuck_server_is_cancelled_after_the_stop_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_exits(self: Any, sockets: Any = None) -> None:
        self.started = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod._Server, "serve", never_exits)
    web = WebServer(stop_timeout=0.1)
    task = await web.start(hello, free_port())
    await web.stop()
    assert task.cancelled() and not web.running


async def test_a_crash_ends_the_task_with_its_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    async def crash(self: Any) -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    real_main_loop = server_mod._Server.main_loop
    monkeypatch.setattr(server_mod._Server, "main_loop", crash)
    web = WebServer()
    port = free_port()
    task = await web.start(hello, port)
    with pytest.raises(RuntimeError, match="boom"):
        await task
    assert not web.running
    assert port_is_free(port)  # the listening socket is closed
    await web.stop()  # still safe after a crash
    # uvicorn's asyncio listeners were closed too: a stale reader left on the old
    # descriptor would swallow connections, so a restart on the port would hang.
    monkeypatch.setattr(server_mod._Server, "main_loop", real_main_loop)
    await web.start(hello, port)
    try:
        assert await asyncio.wait_for(get(port), 3) == "hello"
    finally:
        await web.stop()
    assert port_is_free(port)


async def test_uvicorn_sys_exit_never_escapes_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exits(self: Any, sockets: Any = None) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(server_mod._Server, "startup", exits)
    web = WebServer()
    with pytest.raises(WebStartError, match="uvicorn exited"):
        await web.start(hello, free_port())


async def test_stop_after_the_task_was_cancelled_externally_returns_cleanly() -> None:
    web = WebServer()
    port = free_port()
    task = await web.start(hello, port)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await web.stop()  # no CancelledError leaks out of stop()
    assert not web.running and web.port is None
    assert port_is_free(port)


async def test_stop_while_the_task_is_cancelled_mid_wait_returns_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ignores_exit(self: Any, sockets: Any = None) -> None:
        self.started = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod._Server, "serve", ignores_exit)
    web = WebServer(stop_timeout=5)
    task = await web.start(hello, free_port())
    asyncio.get_running_loop().call_later(0.05, task.cancel)
    await asyncio.wait_for(web.stop(), 2)
    assert task.cancelled() and not web.running and web.port is None


async def test_stop_reraises_when_the_caller_itself_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ignores_exit(self: Any, sockets: Any = None) -> None:
        self.started = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod._Server, "serve", ignores_exit)
    web = WebServer(stop_timeout=5)
    task = await web.start(hello, free_port())
    stopper = asyncio.create_task(web.stop())
    await asyncio.sleep(0.05)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_port_is_reset_after_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    async def crash(self: Any) -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    monkeypatch.setattr(server_mod._Server, "main_loop", crash)
    web = WebServer()
    task = await web.start(hello, free_port())
    with pytest.raises(RuntimeError):
        await task
    await asyncio.sleep(0)  # let done-callbacks run
    assert web.port is None


async def test_stop_logs_a_swallowed_crash_at_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def crash(self: Any) -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    monkeypatch.setattr(server_mod._Server, "main_loop", crash)
    web = WebServer()
    task = await web.start(hello, free_port())
    with pytest.raises(RuntimeError):
        await task
    with caplog.at_level(logging.DEBUG, logger=server_mod.__name__):
        await web.stop()
    assert any("boom" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)


async def test_a_cancelled_start_stops_the_half_started_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_starts(self: Any, sockets: Any = None) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod._Server, "serve", never_starts)
    web = WebServer(stop_timeout=0.1)
    port = free_port()
    starter = asyncio.create_task(web.start(hello, port))
    await asyncio.sleep(0.05)
    server_task = web._task
    assert server_task is not None
    starter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starter
    assert server_task.done() and web._task is None and web.port is None
    assert port_is_free(port)


async def test_access_log_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    web = WebServer()
    port = free_port()
    await web.start(hello, port)
    try:
        with caplog.at_level(logging.DEBUG):
            async with httpx.AsyncClient() as client:
                await client.get(f"http://127.0.0.1:{port}/login?token=tok-SECRET-123")
    finally:
        await web.stop()
    # The client side (httpx/httpcore) logs its own requests; the server must not.
    server_side = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert not any("tok-SECRET-123" in r.getMessage() for r in server_side)
    assert not any("/login" in r.getMessage() for r in server_side)


async def test_binds_only_to_loopback() -> None:
    web = WebServer()
    await web.start(hello, free_port())
    try:
        assert web._server is not None
        hosts = {s.getsockname()[0] for srv in web._server.servers for s in srv.sockets}
        assert hosts == {"127.0.0.1"}
    finally:
        await web.stop()


async def test_a_cancelled_stop_still_stops_the_server_and_frees_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM during a restart cancels the caller of stop(): the server must not be
    orphaned (handle lost, port held) and its end must still read as a requested
    stop to the done-callbacks registered after start()."""
    ended = asyncio.Event()

    async def slow_to_exit(self: Any, sockets: Any = None) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()  # ignores should_exit
        finally:
            await asyncio.sleep(0.1)  # a slow teardown once cancelled
            ended.set()

    monkeypatch.setattr(server_mod._Server, "serve", slow_to_exit)
    web = WebServer(stop_timeout=5)
    port = free_port()
    task = await web.start(hello, port)
    seen: list[bool] = []
    task.add_done_callback(lambda t: seen.append(web.stop_requested(t)))
    stopper = asyncio.create_task(web.stop())
    await asyncio.sleep(0.05)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    assert not task.done() and web.stopping  # still tracked while it winds down
    await asyncio.wait_for(web.stop(), 1)  # a second stop() waits for it, cleanly
    assert task.done() and ended.is_set()
    assert seen == [True]  # recognised as a requested stop, not a crash
    assert web._task is None and web.port is None and not web.stopping
    assert port_is_free(port)


async def test_a_cancelled_stop_cleans_up_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ignores_exit(self: Any, sockets: Any = None) -> None:
        self.started = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod._Server, "serve", ignores_exit)
    web = WebServer(stop_timeout=5)
    port = free_port()
    task = await web.start(hello, port)
    seen: list[bool] = []
    task.add_done_callback(lambda t: seen.append(web.stop_requested(t)))
    stopper = asyncio.create_task(web.stop())
    await asyncio.sleep(0.05)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    await asyncio.wait({task}, timeout=1)  # ends promptly, without another stop()
    assert task.done()
    await asyncio.sleep(0)  # let the done-callbacks run
    assert seen == [True]
    assert web._task is None and web.port is None and not web.stopping
    assert port_is_free(port)
    await asyncio.wait_for(web.stop(), 1)  # a later stop() is a clean no-op


async def test_a_stop_right_after_a_cancelled_stop_is_still_a_requested_stop() -> None:
    """The reviewer's repro: real uvicorn with a keep-alive connection; the caller of
    stop() is cancelled and stop() is called again before the task's done-callbacks
    have run."""
    web = WebServer()
    port = free_port()
    task = await web.start(hello, port)
    seen: list[bool] = []
    task.add_done_callback(lambda t: seen.append(web.stop_requested(t)))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        await reader.read(200)
        stopper = asyncio.create_task(web.stop())
        await asyncio.sleep(0.05)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        await asyncio.wait_for(web.stop(), 1)
        await asyncio.wait({task}, timeout=1)
        await asyncio.sleep(0)
        assert task.done() and seen == [True]
        assert web._task is None and web.port is None and not web.stopping
        server_mod._bind(port).close()  # nothing listens (the port may be in TIME_WAIT)
        # The open keep-alive connection was dropped too, not leaked.
        await asyncio.wait_for(reader.read(), 1)  # reaches EOF
    finally:
        writer.close()
