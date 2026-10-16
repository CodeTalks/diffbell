"""Test doubles shared across test modules."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pywatchman

from diffbell.sender_slot import SenderSlot
from diffbell.telegram import TelegramError


class FakeSender:
    """Records sends. Each queued outcome is either a message id or an error to raise.
    ``silent`` holds each send's ``silent`` flag, in the order of ``sent``. Also a
    `sender_slot.SlotClient`: ``get_me`` answers ``username`` (or raises
    ``me_error``), ``aclose`` sets ``closed``."""

    def __init__(self, outcomes: list[int | TelegramError] | None = None) -> None:
        self.outcomes: list[int | TelegramError] = list(outcomes or [])
        self.sent: list[tuple[str, str | None]] = []
        self.silent: list[bool] = []
        self._next_id = 100
        self.username: str | None = "my_bot"
        self.me_error: Exception | None = None
        self.me_calls = 0
        self.closed = False

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        self.sent.append((text, parse_mode))
        self.silent.append(silent)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, TelegramError):
                raise outcome
            return outcome
        self._next_id += 1
        return self._next_id

    async def get_me(self) -> dict[str, Any]:
        self.me_calls += 1
        if self.me_error is not None:
            raise self.me_error
        return {"username": self.username}

    async def aclose(self) -> None:
        self.closed = True


def _no_new_client(token: str, chat_id: str | None) -> FakeSender:
    raise AssertionError("this test does not replace the bot")


def fake_slot(sender: FakeSender | None = None) -> SenderSlot:
    """A configured SenderSlot around ``sender`` (a new FakeSender by default): what
    `Daemon` takes in tests that don't exercise setup."""
    return SenderSlot(sender or FakeSender(), make_client=_no_new_client)


class FakeWatchmanClient:
    """Mimics wmclient._Client's buffering: receive() and query() both append every
    subscription PDU to `subs[name]` and `sub_by_root[root][name]`, and
    getSubscription(remove=True) with the matching root clears both. A unilateral
    error PDU (``{"error": ..., "unilateral": True}``) is stored in
    `unilateral_errors` instead of raising, as _Client.receive() does."""

    def __init__(self) -> None:
        self.subs: dict[str, list[dict[str, Any]]] = {}
        self.sub_by_root: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.incoming: list[dict[str, Any]] = []  # returned by receive()
        self.during_query: list[dict[str, Any]] = []  # arrive while a query waits
        self.queries: list[tuple[Any, ...]] = []
        self.query_error: Exception | None = None
        self.receive_error: Exception | None = None
        self.timeout: float | None = None
        self.query_timeouts: list[float | None] = []  # socket timeout seen by each query
        self.unilateral_errors: list[dict[str, Any]] = []
        self.logs: list[Any] = []

    def _absorb(self, pdu: dict[str, Any]) -> None:
        if "error" in pdu and pdu.get("unilateral"):
            self.unilateral_errors.append(pdu)
        else:
            self._buffer(pdu)

    def _buffer(self, pdu: dict[str, Any]) -> None:
        self.subs.setdefault(pdu["subscription"], []).append(pdu)
        by_name = self.sub_by_root.setdefault(pdu["root"], {})
        by_name.setdefault(pdu["subscription"], []).append(pdu)

    def setTimeout(self, value: float) -> None:
        self.timeout = value

    def query(self, *args: Any) -> dict[str, Any]:
        self.queries.append(args)
        self.query_timeouts.append(self.timeout)
        while self.during_query:
            self._absorb(self.during_query.pop(0))
        if self.query_error is not None:
            err, self.query_error = self.query_error, None
            raise err
        return {"ok": True, "args": list(args)}

    def receive(self) -> dict[str, Any]:
        if self.receive_error is not None:
            err, self.receive_error = self.receive_error, None
            raise err
        if not self.incoming:
            time.sleep(0.005)
            raise pywatchman.SocketTimeout("timed out")
        pdu = self.incoming.pop(0)
        self._absorb(pdu)
        return pdu

    def getSubscription(
        self, name: str, remove: bool = True, root: str | None = None
    ) -> list[dict[str, Any]] | None:
        if root is None:
            data = self.subs.get(name)
            if remove:
                self.subs.pop(name, None)
            return data
        by_name = self.sub_by_root.get(root, {})
        data = by_name.get(name)
        if remove and data is not None:
            del by_name[name]
            self.subs.pop(name, None)
        return data

    def close(self) -> None:
        pass


class VersionedWatchmanClient(FakeWatchmanClient):
    """A FakeWatchmanClient whose connect-time `version` reply carries a version."""

    VERSION = "2026.10.05.00"

    def query(self, *args: Any) -> dict[str, Any]:
        reply = super().query(*args)
        return {"version": self.VERSION} if args == ("version",) else reply


class FakeBot:
    """A Bot API stand-in for the setup wizard: ``client(token, chat_id)`` returns a
    client bound to this bot (WebContext.make_probe). ``errors`` maps a method
    (get_me, get_updates, send) to the error it raises until removed; ``calls``
    records (method, token, chat id) and ``sent`` (chat id, text)."""

    def __init__(self, username: str = "diffbell_test_bot") -> None:
        self.username = username
        self.updates: list[dict[str, Any]] = []
        self.errors: dict[str, TelegramError] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.sent: list[tuple[str | None, str]] = []
        self.closed = 0
        # get_updates' offset per call; `gates` makes a method wait for its event
        # (`waiting` counts the calls waiting).
        self.offsets: list[int | None] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.waiting = 0

    def client(self, token: str, chat_id: str | None) -> FakeBotClient:
        return FakeBotClient(self, token, chat_id)


class FakeBotClient:
    def __init__(self, bot: FakeBot, token: str, chat_id: str | None) -> None:
        self._bot = bot
        self._token = token
        self._chat_id = chat_id

    async def _call(self, method: str) -> None:
        self._bot.calls.append((method, self._token, self._chat_id))
        gate = self._bot.gates.get(method)
        if gate is not None:
            self._bot.waiting += 1
            try:
                await gate.wait()
            finally:
                self._bot.waiting -= 1
        if method in self._bot.errors:
            raise self._bot.errors[method]

    async def get_me(self) -> dict[str, Any]:
        await self._call("get_me")
        return {"id": 1, "is_bot": True, "first_name": "Diffbell", "username": self._bot.username}

    async def get_webhook_info(self) -> dict[str, Any]:
        await self._call("get_webhook_info")
        return {"url": ""}

    async def get_updates(self, *, offset: int | None = None) -> list[dict[str, Any]]:
        """Like Telegram: the oldest 100 updates at or after ``offset``."""
        self._bot.offsets.append(offset)
        await self._call("get_updates")
        pending = [u for u in self._bot.updates if offset is None or u["update_id"] >= offset]
        pending.sort(key=lambda u: int(u["update_id"]))
        return [dict(u) for u in pending[:100]]

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        await self._call("send")
        self._bot.sent.append((self._chat_id, text))
        return 1

    async def aclose(self) -> None:
        self._bot.closed += 1
