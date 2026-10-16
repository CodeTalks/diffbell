"""The single thread that owns the Watchman connection (spec §3).

pywatchman is synchronous, has no lock and uses one socket, so every call goes through
this thread. Callers submit commands and await futures. Subscription PDUs (and
connected/disconnected/error markers) are passed to ``on_message`` from this thread; the
daemon bridges them into asyncio with ``loop.call_soon_threadsafe``.

Known residual: a SocketTimeout that fires mid-PDU leaves a partial frame on the socket.
The next read would be garbage, so the catch-all in ``_run`` drops the connection and
reconnects; nothing tries to resynchronise the stream in place.

Late-reply hazard: the protocol has no request ids, so a command that times out while
its reply is still in flight would hand that reply to the *next* ``query()``. A command
timeout therefore must reset the connection (``step`` re-raises it, and ``_run``
reconnects). It is also why the web UI's file preview never uses this connection: a
slow query would block every subscription behind it (see the web UI spec, W10).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import queue
import threading
from collections.abc import Callable
from typing import Any

import pywatchman

log = logging.getLogger(__name__)

CONNECTED: dict[str, Any] = {"_diffbell": "connected"}
DISCONNECTED: dict[str, Any] = {"_diffbell": "disconnected"}

Command = tuple[tuple[Any, ...], "concurrent.futures.Future[Any]"]


class WatchmanUnavailable(Exception):
    """No Watchman connection is up (or it dropped while the command was queued)."""


class _Client(pywatchman.client):  # type: ignore[misc]
    """pywatchman.client whose receive() does not raise for a *unilateral* error PDU.

    The stock receive() raises CommandError for any PDU carrying "error" before query()
    gets to skip unilateral PDUs, so a unilateral error arriving mid-query fails that
    command and leaves its real response to be read by the next one. Here such PDUs are
    stored in ``unilateral_errors`` and returned, so query() keeps reading.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.unilateral_errors: list[Any] = []

    def receive(self) -> Any:
        self._connect()
        result = self.recvConn.receive()
        if self._hasprop(result, "error") and self.isUnilateralResponse(result):
            self.unilateral_errors.append(result)
            return result
        # The rest is pywatchman.client.receive() verbatim.
        if self._hasprop(result, "error"):
            raise pywatchman.CommandError(result["error"])

        if self._hasprop(result, "log"):
            self.logs.append(result["log"])

        if self._hasprop(result, "subscription"):
            sub = result["subscription"]
            if sub not in self.subs:
                self.subs[sub] = []
            self.subs[sub].append(result)

            root = os.path.normpath(os.path.normcase(result["root"]))
            if root not in self.sub_by_root:
                self.sub_by_root[root] = {}
            if sub not in self.sub_by_root[root]:
                self.sub_by_root[root][sub] = []
            self.sub_by_root[root][sub].append(result)

        return result


def _version_of(reply: Any) -> str | None:
    version = reply.get("version") if isinstance(reply, dict) else None
    return version if isinstance(version, str) else None


class WatchmanClient:
    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        *,
        client_factory: Callable[[], Any] | None = None,
        recv_timeout: float = 0.2,
        command_timeout: float = 30.0,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self._on_message = on_message
        self._factory = client_factory or self._default_factory
        self._recv_timeout = recv_timeout
        self._command_timeout = command_timeout
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._commands: queue.Queue[Command] = queue.Queue()
        self._stop = threading.Event()
        self._connected = threading.Event()
        # The connect-time `version` reply. Written on the connection thread and read
        # on the loop thread; a plain attribute assignment is atomic under the GIL.
        self._version: str | None = None
        # Makes "connected and not stopping -> enqueue" atomic with "disconnect -> drain",
        # so no command can be enqueued after the drain and wait forever.
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _default_factory(self) -> Any:
        # Connect and run `version` under the long timeout; _run lowers it afterwards.
        return _Client(timeout=self._command_timeout)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def version(self) -> str | None:
        """Watchman's version from the current connection, None while disconnected."""
        return self._version

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("WatchmanClient already started")
        self._thread = threading.Thread(target=self._run, name="watchman-conn", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    async def call(self, *args: Any) -> Any:
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()
        with self._lock:
            if self._stop.is_set():
                raise WatchmanUnavailable("watchman client is stopping")
            if not self._connected.is_set():
                raise WatchmanUnavailable("watchman is not connected")
            self._commands.put((args, fut))
        return await asyncio.wrap_future(fut)

    # -- connection thread --------------------------------------------------

    def _emit(self, msg: dict[str, Any]) -> None:
        """Hand one message to on_message; its failures must never reach this thread."""
        try:
            self._on_message(msg)
        except Exception:
            log.exception(
                "on_message failed for %r", msg.get("_diffbell") or msg.get("subscription")
            )

    def _run(self) -> None:
        backoff = self._min_backoff
        while not self._stop.is_set():
            client: Any = None
            was_connected = False
            try:
                client = self._factory()
                # pywatchman declares these buffers as class attributes, shared by every
                # client instance; give each connection its own so nothing leaks across
                # reconnects.
                client.subs = {}
                client.sub_by_root = {}
                client.logs = []
                # Forces the socket connection; the reply is kept for the status snapshot.
                self._version = _version_of(client.query("version"))
                client.setTimeout(self._recv_timeout)
                self._connected.set()
                was_connected = True
                backoff = self._min_backoff
                self._emit(dict(CONNECTED))
                while not self._stop.is_set():
                    self.step(client)
            except (pywatchman.WatchmanError, OSError) as exc:
                log.warning("watchman connection lost: %s", exc)
            except Exception:
                log.exception("watchman connection thread failed; reconnecting")
            finally:
                with self._lock:
                    self._version = None  # before clear(): never connected=False + a version
                    self._connected.clear()
                    self._fail_pending()
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.close()
            if self._stop.is_set():
                break
            if was_connected:
                self._emit(dict(DISCONNECTED))
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    def step(self, client: Any) -> None:
        """One iteration: run queued commands, wait briefly for a PDU, forward buffers."""
        timeout_raised = False
        while True:
            try:
                args, fut = self._commands.get_nowait()
            except queue.Empty:
                break
            if not fut.set_running_or_notify_cancel():
                continue
            if not timeout_raised:
                # query() shares the socket timeout with receive(); a short one would
                # turn a slow command into a SocketTimeout, i.e. a lost connection.
                client.setTimeout(self._command_timeout)
                timeout_raised = True
            try:
                fut.set_result(client.query(*args))
            except pywatchman.CommandError as exc:
                fut.set_exception(exc)
            except BaseException as exc:
                lost = WatchmanUnavailable(f"watchman connection lost: {exc}")
                lost.__cause__ = exc
                fut.set_exception(lost)
                raise  # connection-level failure: reconnect
            # query() buffers PDUs that arrive while it waits for its response.
            self.forward_buffered(client)
        if timeout_raised:
            client.setTimeout(self._recv_timeout)
        try:
            client.receive()
        except pywatchman.SocketTimeout:
            pass
        except pywatchman.CommandError as exc:
            log.error("watchman reported an error: %s", exc)
            self._emit({"_diffbell": "error", "message": str(exc)})
        self.forward_buffered(client)

    def forward_buffered(self, client: Any) -> None:
        """Forward and clear every buffered PDU and unilateral error. Drain with
        pdu["root"] verbatim: the buffer key is normpath(normcase(root)), and a mismatch
        silently returns None."""
        pdus = [pdu for name in list(client.subs) for pdu in list(client.subs.get(name) or [])]
        for pdu in pdus:
            client.getSubscription(pdu["subscription"], root=pdu["root"], remove=True)
        errors: list[Any] = getattr(client, "unilateral_errors", None) or []
        stored = list(errors)
        errors.clear()
        for err in stored:
            log.error("watchman reported an error: %s", err.get("error"))
            self._emit({"_diffbell": "error", "pdu": err})
        for pdu in pdus:
            self._emit(pdu)

    def _fail_pending(self) -> None:
        while True:
            try:
                _, fut = self._commands.get_nowait()
            except queue.Empty:
                return
            if fut.set_running_or_notify_cancel():
                fut.set_exception(WatchmanUnavailable("watchman connection lost"))
