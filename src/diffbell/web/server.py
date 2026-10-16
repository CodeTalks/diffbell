"""Run the web app with uvicorn on the daemon's event loop (web UI spec §3, W6).

A fresh ``uvicorn.Server`` per start (``serve()`` is single-use). The listening
socket is bound here, before uvicorn starts, so a port in use is a clean
``WebStartError`` instead of uvicorn's ``sys.exit(1)``. uvicorn's signal handlers
are disabled: SIGINT/SIGTERM belong to the daemon (``loop.add_signal_handler``),
and uvicorn's ``signal.signal`` calls would replace them while the server runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import weakref
from collections.abc import Iterator

import uvicorn
from starlette.types import ASGIApp

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
START_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0


class WebStartError(Exception):
    """The server could not start (e.g. the port is in use). User-facing message."""


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The daemon owns SIGINT/SIGTERM; never install uvicorn's handlers.
        yield


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(128)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        raise WebStartError(exc.strerror or str(exc)) from None
    return sock


async def _serve(server: _Server, sock: socket.socket) -> None:
    try:
        await server.serve(sockets=[sock])
    except SystemExit as exc:
        # uvicorn calls sys.exit() on startup failures; a SystemExit escaping a task
        # would stop the daemon's whole event loop.
        raise RuntimeError(f"uvicorn exited (code {exc.code})") from None
    finally:
        # After a crash uvicorn never ran its shutdown: close its listeners too, or
        # the loop keeps a reader registered on the closed socket's descriptor.
        for listener in getattr(server, "servers", []):
            listener.close()
        sock.close()
        # Cancelled before uvicorn's shutdown ran (e.g. a stop whose caller was
        # cancelled): drop the open connections and their request tasks too, or an
        # idle keep-alive connection keeps the port (and a descriptor) in use.
        for task in list(server.server_state.tasks):
            task.cancel()
        for connection in list(server.server_state.connections):
            transport = getattr(connection, "transport", None)
            if transport is not None:
                transport.close()


class WebServer:
    """Start/stop one uvicorn server at a time on the running loop."""

    def __init__(
        self, *, start_timeout: float = START_TIMEOUT, stop_timeout: float = STOP_TIMEOUT
    ) -> None:
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._server: _Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.port: int | None = None
        # True from stop() until the stopped task has ended (even when the caller of
        # stop() was cancelled).
        self.stopping = False
        # Every task stop() was called for: lets a done-callback tell a requested stop
        # from a crash whatever order the callbacks and the cleanup run in.
        self._stop_requested: weakref.WeakSet[asyncio.Task[None]] = weakref.WeakSet()
        # The task a cancelled stop() left winding down: its done-callback clears the
        # handle, so the port and the task are never orphaned.
        self._abandoned: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self._server is not None
            and self._server.started
        )

    async def start(self, app: ASGIApp, port: int) -> asyncio.Task[None]:
        """Bind 127.0.0.1:<port> and serve ``app``. Returns the server task once it is
        accepting connections. Raises WebStartError if it can't start."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("web server already running")
        sock = _bind(port)
        self.stopping = False  # any earlier stop has finished: its task is done
        config = uvicorn.Config(
            app,
            log_config=None,  # the daemon configures logging
            access_log=False,  # login URLs carry a token: never log them
            lifespan="off",
            ws="none",
            server_header=False,
            timeout_graceful_shutdown=2,
        )
        server = _Server(config)
        task = asyncio.create_task(_serve(server, sock), name="web")
        self._server, self._task, self.port = server, task, sock.getsockname()[1]
        task.add_done_callback(self._on_task_done)
        try:
            deadline = asyncio.get_running_loop().time() + self._start_timeout
            while not server.started and not task.done():
                if asyncio.get_running_loop().time() > deadline:
                    await self.stop()
                    raise WebStartError("timed out waiting for the server to start")
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            # The caller was cancelled mid-startup: never leave a half-started server
            # holding the port.
            await self.stop()
            raise
        if task.done():
            exc = None if task.cancelled() else task.exception()
            self._server = self._task = None
            self.port = None
            raise WebStartError(f"server stopped during startup: {exc}")
        log.info("web UI listening on http://%s:%d", HOST, self.port)
        return task

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        if task is not self._task:
            return
        # A crashed (or externally cancelled) server no longer listens anywhere.
        self.port = None
        if task is self._abandoned:
            # The stop() that cancelled it was itself cancelled: finish its cleanup.
            self._server = self._task = self._abandoned = None
            self.stopping = False

    def stop_requested(self, task: asyncio.Task[None]) -> bool:
        """Whether ``task`` ended because stop() was called for it (not a crash)."""
        return task in self._stop_requested

    async def stop(self) -> None:
        """Ask uvicorn to exit, then wait (bounded) before cancelling it. Idempotent,
        and safe after a crash or after the task was cancelled from outside. Re-raises
        CancelledError only when the task calling stop() is itself being cancelled; the
        server is then force-cancelled and cleaned up when its task ends, never left
        running without a handle."""
        server, task = self._server, self._task
        if server is None or task is None:
            return
        current = asyncio.current_task()
        # Cancellations already pending on the caller when stop() began (e.g. start()
        # cleaning up after its caller was cancelled) are not new ones.
        baseline = current.cancelling() if current is not None else 0
        self.stopping = True
        self._stop_requested.add(task)
        abandoned = False
        try:
            if not task.done():
                server.should_exit = True
                try:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), self._stop_timeout)
                    except TimeoutError:
                        log.warning(
                            "web server did not stop in %.0fs; cancelling it", self._stop_timeout
                        )
                        server.force_exit = True
                        task.cancel()
                        await task
                except asyncio.CancelledError:
                    if self._caller_cancelled(task, current, baseline):
                        abandoned = True
                        server.force_exit = True
                        task.cancel()
                        raise
                except Exception:
                    pass  # crashed while stopping: reported below
            if task.done() and not task.cancelled() and task.exception() is not None:
                # The daemon's done-callback already reported the crash.
                log.debug("web server had crashed: %r", task.exception())
        finally:
            if abandoned and not task.done():
                self._abandoned = task  # its done-callback finishes the cleanup
            elif task is self._task:
                self._server = self._task = self._abandoned = None
                self.port = None
                self.stopping = False

    @staticmethod
    def _caller_cancelled(
        task: asyncio.Task[None], current: asyncio.Task[object] | None, baseline: int
    ) -> bool:
        """Inside ``except CancelledError``: False when the web task itself was
        cancelled (swallow it); True when the caller of stop() is being cancelled."""
        return not task.done() or (current is not None and current.cancelling() > baseline)
