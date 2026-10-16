"""Settings → Telegram: the bot-setup wizard's endpoints (bot onboarding spec §2).

The token reaches the server once, in ``POST /api/telegram/draft``, and then lives
only in the in-memory :class:`Draft`: never in SQLite, a log or a response. The chat
id never reaches the browser either: the page picks a chat through an opaque
``choice`` handle. Handlers are ``async def`` (the SQLite connection belongs to the
loop thread); the blocking Keychain write runs in ``asyncio.to_thread`` and touches
no SQLite. Error bodies are fixed texts: never Telegram's description, a URL, the
token or the chat id.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict

from diffbell.render import sample_message
from diffbell.secrets import SecretStoreError
from diffbell.telegram import ErrorKind, TelegramError
from diffbell.web.app import TELEGRAM_UNREACHABLE, ApiError, Text, WebContext

log = logging.getLogger(__name__)

DRAFT_TTL = 30 * 60.0  # seconds after the draft's last use (sliding)
CHAT_NAME_KEY = "telegram.chat_name"  # kv: the saved chat's first name (not a secret)
TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{30,}", re.ASCII)
CHAT_ID = re.compile(r"-?\d{1,20}", re.ASCII)  # the CLI's manual rule: an optional minus
MAX_NAME = 64  # characters of a bot's or chat's name kept for the page

MALFORMED = "That doesn't look like a bot token."
REJECTED = "Telegram rejected this token."
WEBHOOK = "This bot has a webhook, or another program reads its updates; enter the chat id instead."
UNREACHABLE = TELEGRAM_UNREACHABLE
REFUSED = "Telegram refused the request; check the token and the chat."
CANT_SEND = "Telegram refused to send to this chat; send /start to the bot and try again."
SAVING = "Saving is already in progress."
KEYCHAIN_FAILED = "Saving to the Keychain failed; try Save again."
UNSAVABLE = "That token or chat id can't be saved; check the token again."
GONE = "This setup expired or was restarted in another tab; check the token again."
NO_CHOICE = "That chat isn't part of this setup; find the chat again."
NOT_A_CHAT_ID = "A chat id is a number, like 123456789 or -1001234567890."

DraftId = Annotated[str, PathParam(max_length=64)]


@dataclass
class Choice:
    chat_id: str = field(repr=False)
    name: str | None  # the chat's first name; None for an id entered by hand


@dataclass
class Draft:
    """One wizard run: the token (never logged: ``repr=False``), the bot's username,
    and the chats found so far behind their opaque handles."""

    id: str
    token: str = field(repr=False)
    bot: str
    created_at: float
    used_at: float
    choices: dict[str, Choice] = field(default_factory=dict, repr=False)
    # Handles of the chats found by polling, in order: kept across polls, since a
    # found chat's update is confirmed (offset) and never returned again.
    detected: list[str] = field(default_factory=list)
    # getUpdates' offset: one past the newest update seen. The bot is dedicated and
    # the wizard is its only reader while a draft exists.
    next_offset: int | None = None

    def choice_for(self, chat_id: str, name: str | None) -> str:
        """The chat's handle: the same one on every poll, so the page's pick stays."""
        for handle, choice in self.choices.items():
            if choice.chat_id == chat_id:
                if name is not None:
                    choice.name = name
                return handle
        handle = secrets.token_urlsafe(8)
        while handle in self.choices:
            handle = secrets.token_urlsafe(8)
        self.choices[handle] = Choice(chat_id, name)
        return handle


class DraftGone(Exception):
    """Unknown, replaced by a newer draft, or expired."""


class Drafts:
    """At most one draft: creating one replaces the previous one. A draft lives
    ``ttl`` seconds after its last use; every endpoint call refreshes it.

    ``saving`` is set while a save runs (before its first await), across all
    drafts: a replaced draft's save may still be running, and two interleaved
    Keychain sequences could leave a trusted, mismatched token and chat id."""

    def __init__(self, clock: Callable[[], float] = time.time, ttl: float = DRAFT_TTL) -> None:
        self._clock = clock
        self._ttl = ttl
        self._draft: Draft | None = None
        self.saving = False

    @property
    def current(self) -> Draft | None:
        return self._draft

    def sweep(self) -> None:
        """Forget an expired draft, so an abandoned token doesn't stay in memory."""
        if self._draft is not None and self._clock() - self._draft.used_at > self._ttl:
            self._draft = None

    def create(self, token: str, bot: str) -> Draft:
        self.sweep()
        now = self._clock()
        self._draft = Draft(secrets.token_urlsafe(16), token, bot, now, now)
        return self._draft

    def use(self, draft_id: str) -> Draft:
        draft = self._draft
        if draft is None or not secrets.compare_digest(draft.id.encode(), draft_id.encode()):
            raise DraftGone
        now = self._clock()
        if now - draft.used_at > self._ttl:
            self._draft = None
            raise DraftGone
        draft.used_at = now
        return draft

    def drop(self, draft_id: str) -> None:
        draft = self._draft
        if draft is not None and secrets.compare_digest(draft.id.encode(), draft_id.encode()):
            self._draft = None


def next_offset(updates: list[dict[str, Any]], current: int | None) -> int | None:
    """One past the newest ``update_id`` returned (confirming them all), or
    ``current`` when there is none."""
    ids: list[int] = []
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            ids.append(update_id)
    if not ids:
        return current
    return max(ids) + 1 if current is None else max(current, max(ids) + 1)


def private_chats(updates: list[dict[str, Any]], since: float) -> list[tuple[str, str]]:
    """(chat id, first name) of each private chat with a message dated at or after
    ``since`` (the draft's creation): a message sent before Check, possibly by someone
    else, never counts. Telegram dates are whole seconds, hence the floor. In order of
    first appearance; a later message only updates the name."""
    found: dict[str, str] = {}
    for update in updates:
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        date = message.get("date")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            continue
        if isinstance(date, bool) or not isinstance(date, int) or date < math.floor(since):
            continue
        chat_id = chat.get("id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            continue
        name = chat.get("first_name") or chat.get("username") or "Telegram user"
        found[str(chat_id)] = str(name)[:MAX_NAME]
    return list(found.items())


def _failure(method: str, exc: TelegramError, auth_message: str) -> ApiError:
    """A fixed text per kind: Telegram's description may quote what was sent."""
    log.warning("telegram setup: %s failed (%s, HTTP %s)", method, exc.kind, exc.status)
    if exc.kind is ErrorKind.RETRYABLE:
        return ApiError(503, UNREACHABLE)
    if exc.kind is ErrorKind.AUTH:
        return ApiError(422, auth_message)
    return ApiError(422, REFUSED)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenBody(_Body):
    token: Text


class ChatIdBody(_Body):
    chat_id: Text


class ChoiceBody(_Body):
    choice: Text


def register(app: FastAPI, ctx: WebContext, drafts: Drafts) -> None:
    """Add the wizard's endpoints and ``GET /api/telegram`` to ``app``."""
    app.state.telegram_drafts = drafts

    def draft_of(draft_id: str) -> Draft:
        try:
            return drafts.use(draft_id)
        except DraftGone:
            raise ApiError(404, GONE) from None

    def choice_of(draft: Draft, handle: str) -> Choice:
        choice = draft.choices.get(handle)
        if choice is None:
            raise ApiError(422, NO_CHOICE)
        return choice

    async def bot_name(token: str) -> str:
        """getMe: the bot's username, or the ApiError for a refused or unreachable call."""
        probe = ctx.make_probe(token, None)
        try:
            me = await probe.get_me()
        except TelegramError as exc:
            raise _failure("getMe", exc, REJECTED) from None
        finally:
            await probe.aclose()
        return str(me.get("username") or "your bot")[:MAX_NAME]

    @app.get("/api/telegram")
    async def telegram_status() -> dict[str, Any]:
        """The Settings card and the dashboard banner: the slot and the outbox, no
        Bot API call and no Keychain read. The chat's name only while configured (it
        names the saved chat); ``reason`` (why not set up) only while not configured,
        from the daemon's cache; ``failing`` from the outbox (settings redesign §7)."""
        drafts.sweep()
        status = ctx.telegram_status()
        configured = bool(status.get("configured"))
        last_ok, last_error = ctx.store.telegram_health()
        failure = ctx.store.telegram_failure()
        return {
            "configured": configured,
            "bot": status.get("bot") if configured else None,
            "chat_name": ctx.store.kv_get(CHAT_NAME_KEY) if configured else None,
            "last_ok": last_ok,
            "last_error": last_error,
            "reason": None if configured else ctx.setup_reason(),
            "failing": None
            if failure is None
            else {
                "kind": failure.kind,
                "since": failure.since,
                "failed": failure.failed,
                "waiting": failure.waiting,
                "error": failure.error,
            },
        }

    @app.post("/api/telegram/draft")
    async def check_token(body: TokenBody) -> dict[str, Any]:
        drafts.sweep()
        token = body.token.strip()
        if not TOKEN.fullmatch(token):
            raise ApiError(422, MALFORMED)  # before any network call
        bot = await bot_name(token)
        draft = drafts.create(token, bot)
        return {"draft": draft.id, "bot": bot}

    @app.get("/api/telegram/draft/{draft_id}/chats")
    async def find_chats(draft_id: DraftId) -> dict[str, Any]:
        draft = draft_of(draft_id)
        probe = ctx.make_probe(draft.token, None)
        try:
            updates = await probe.get_updates(offset=draft.next_offset)
        except TelegramError as exc:
            if exc.status == 409:  # a webhook is set: getUpdates can't be used
                raise ApiError(409, WEBHOOK) from None
            raise _failure("getUpdates", exc, REJECTED) from None
        finally:
            await probe.aclose()
        draft.next_offset = next_offset(updates, draft.next_offset)
        for chat_id, name in private_chats(updates, draft.created_at):
            handle = draft.choice_for(chat_id, name)
            if handle not in draft.detected:
                draft.detected.append(handle)
        return {
            "chats": [
                {"choice": handle, "name": draft.choices[handle].name} for handle in draft.detected
            ]
        }

    @app.post("/api/telegram/draft/{draft_id}/chats")
    async def manual_chat(draft_id: DraftId, body: ChatIdBody) -> dict[str, Any]:
        draft = draft_of(draft_id)
        chat_id = "".join(body.chat_id.split())
        if not CHAT_ID.fullmatch(chat_id):
            raise ApiError(422, NOT_A_CHAT_ID)
        handle = draft.choice_for(chat_id, None)
        return {"choice": handle, "name": draft.choices[handle].name}

    @app.post("/api/telegram/draft/{draft_id}/test")
    async def test_message(draft_id: DraftId, body: ChoiceBody) -> dict[str, Any]:
        draft = draft_of(draft_id)
        choice = choice_of(draft, body.choice)
        client = ctx.make_probe(draft.token, choice.chat_id)
        when = datetime.fromtimestamp(ctx.clock(), ctx.tz)
        try:
            await client.send(sample_message(when), parse_mode="HTML")
        except TelegramError as exc:
            raise _failure("sendMessage", exc, CANT_SEND) from None
        finally:
            await client.aclose()
        return {"ok": True}

    @app.post("/api/telegram/draft/{draft_id}/save")
    async def save(draft_id: DraftId, body: ChoiceBody) -> dict[str, Any]:
        draft = draft_of(draft_id)
        choice = choice_of(draft, body.choice)
        if drafts.saving:  # any draft's: one save at a time
            raise ApiError(409, SAVING)
        drafts.saving = True  # before the first await
        handed_off: list[bool] = []
        try:
            return await save_locked(draft, choice, handed_off)
        finally:
            if not handed_off:  # else the Keychain write's done-callback clears it
                drafts.saving = False

    def write_ended(write: asyncio.Future[None]) -> None:
        """The Keychain write of a cancelled save ended: only now may another save
        start. A failed write may have left the marker: the cached reason says so.
        Its outcome is logged by type only (no daemon switch follows it: the saved
        pair is picked up at the next start, or by saving again)."""
        drafts.saving = False
        if not write.cancelled() and (exc := write.exception()) is not None:
            if isinstance(exc, SecretStoreError | OSError):
                ctx.note_unfinished_save()
            log.warning(
                "telegram setup: a cancelled save's Keychain write failed (%s)", type(exc).__name__
            )

    async def save_locked(draft: Draft, choice: Choice, handed_off: list[bool]) -> dict[str, Any]:
        bot = await bot_name(draft.token)  # the token may have been revoked since Check
        # The thread can't be stopped: a cancelled request (client disconnect) must
        # not end the in-progress mark while it still writes, or a second save could
        # interleave its Keychain sequence with this one.
        write = asyncio.ensure_future(
            asyncio.to_thread(ctx.save_credentials, draft.token, choice.chat_id)
        )
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            handed_off.append(True)
            write.add_done_callback(write_ended)
            raise
        except ValueError:
            # save_credentials refused a value before writing anything. A fixed text:
            # the exception may quote the value.
            log.warning("telegram setup: the Keychain writer refused a value")
            raise ApiError(422, UNSAVABLE) from None
        except (SecretStoreError, OSError) as exc:
            # The type only. The draft stays, so Save can be retried, and the
            # save-pending marker stays until a save succeeds: until then the card
            # says an earlier save didn't finish.
            ctx.note_unfinished_save()
            log.warning("telegram setup: saving to the Keychain failed (%s)", type(exc).__name__)
            raise ApiError(500, KEYCHAIN_FAILED) from None
        await ctx.complete_setup(draft.token, choice.chat_id, bot)
        if choice.name is None:
            ctx.store.kv_delete(CHAT_NAME_KEY)
        else:
            ctx.store.kv_set(CHAT_NAME_KEY, choice.name)
        drafts.drop(draft.id)
        return {"bot": bot}
