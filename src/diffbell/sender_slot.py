"""The daemon's one Telegram sender, possibly empty (bot onboarding spec §1).

`cli.cmd_run` fills it from the Keychain at start (or leaves it empty: "not set up");
the web wizard's save `swap`s in a new client. `Daemon`, `Notifier`, `Health` and
`Heartbeat` depend only on `telegram.Sender`; `configured` is read by the pause
(hold), `Health.tick` (mute), `Heartbeat.tick` (skip) and `_check_telegram`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from diffbell.telegram import NotSetUp

log = logging.getLogger(__name__)


class SlotClient(Protocol):
    """What the slot needs of a client: `telegram.TelegramClient`, or a test fake."""

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int: ...

    async def get_me(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class SenderSlot:
    """Implements `telegram.Sender`. Holds a client or nothing; never a secret in
    anything it exposes (``bot_username`` is public)."""

    def __init__(
        self,
        client: SlotClient | None,
        make_client: Callable[[str, str | None], SlotClient],
    ) -> None:
        self._client = client
        self._make_client = make_client
        self._bot_username: str | None = None
        # In-flight sends per client (by identity) and clients to close once idle.
        self._inflight: dict[int, int] = {}
        self._retiring: dict[int, SlotClient] = {}

    @property
    def configured(self) -> bool:
        return self._client is not None

    @property
    def bot_username(self) -> str | None:
        return self._bot_username

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        client = self._client
        if client is None:
            raise NotSetUp()
        key = id(client)
        self._inflight[key] = self._inflight.get(key, 0) + 1
        try:
            return await client.send(text, parse_mode=parse_mode, silent=silent)
        finally:
            left = self._inflight[key] - 1
            if left:
                self._inflight[key] = left
            else:
                del self._inflight[key]
                retired = self._retiring.pop(key, None)
                if retired is not None:
                    await self._close(retired)

    async def _retire(self, client: SlotClient) -> None:
        """Close ``client`` now, or when its last in-flight send finishes."""
        if id(client) in self._inflight:
            self._retiring[id(client)] = client
        else:
            await self._close(client)

    @staticmethod
    async def _close(client: SlotClient) -> None:
        try:
            await client.aclose()
        except Exception as exc:
            log.warning("closing the previous Telegram client failed (%s)", type(exc).__name__)

    async def swap(self, token: str, chat_id: str, *, bot_username: str | None) -> None:
        """Install a client for the new bot, then close the old one. The new client
        is installed (and with it ``configured``) with no ``await`` in between, on
        the loop thread; a failing close of the old one is logged, never raised."""
        new = self._make_client(token, chat_id)
        old, self._client = self._client, new
        self._bot_username = bot_username
        if old is not None:
            await self._retire(old)

    async def fetch_username(self) -> None:
        """One getMe, in the background after start. Any failure leaves the username
        None and is logged by type name only (the message may carry the URL); it
        touches neither Health nor readiness."""
        client = self._client
        if client is None:
            return
        try:
            me = await client.get_me()
        except Exception as exc:
            log.info("could not read the bot's username (%s)", type(exc).__name__)
            return
        username = me.get("username")
        # A swap during the call installed another bot: its username wins.
        if client is self._client and isinstance(username, str) and username:
            self._bot_username = username

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await self._retire(client)
