"""Interactive `diffbell setup` (spec §9).

The bot token is read with hidden input and is never echoed, printed or logged.
"""

from __future__ import annotations

import getpass
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from diffbell import BRAND, machine
from diffbell.config import ConfigError, load_config, write_default_config
from diffbell.secrets import SecretStoreError, save_credentials, service
from diffbell.telegram import TelegramError


class SetupClient(Protocol):
    async def get_me(self) -> dict[str, Any]: ...
    async def get_webhook_info(self) -> dict[str, Any]: ...
    async def get_updates(self, *, offset: int | None = None) -> list[dict[str, Any]]: ...
    async def send(self, text: str, *, parse_mode: str | None = "HTML") -> int: ...
    async def aclose(self) -> None: ...


@dataclass
class SetupIO:
    ask_secret: Callable[[str], str] = field(default=getpass.getpass)
    ask: Callable[[str], str] = field(default=input)
    say: Callable[[str], None] = field(default=print)


# A daemon reads the Keychain once, at start, and its snapshot can't tell which bot
# it holds: whenever one runs, say so (bot onboarding spec §1).
RESTART_HINT = (
    f"Saved; the running {BRAND} keeps its previous bot (or none) until you run `diffbell install`."
)


async def run_setup(
    io: SetupIO,
    *,
    make_client: Callable[[str, str | None], SetupClient],
    config_path: Path,
    save: Callable[[str, str], None] = save_credentials,
    daemon_running: Callable[[], bool],
) -> int:
    """``save(token, chat_id)`` writes the pair in the one order both writers use
    (marker, chat id, token, delete marker). ``daemon_running`` is asked only after a
    successful save."""
    token = io.ask_secret("Bot token from @BotFather (input hidden): ").strip()
    if not token:
        io.say("No token entered.")
        return 1
    probe = make_client(token, None)
    try:
        try:
            me = await probe.get_me()
        except TelegramError as exc:
            io.say(f"Token rejected by Telegram: {exc}")
            return 1
        username = str(me.get("username", "?"))
        if io.ask(f"Token belongs to @{username}. Use this bot? [y/N] ").strip().lower() != "y":
            io.say("Aborted; nothing was saved.")
            return 1
        chat_id = await _detect_chat_id(probe, io, username)
    finally:
        await probe.aclose()
    if chat_id is None:
        io.say("No chat id; nothing was saved.")
        return 1

    try:
        save(token, chat_id)
    except (SecretStoreError, ValueError) as exc:
        # Neither error carries the secret value, only the account name.
        io.say(f"Saving to the Keychain failed: {exc}")
        return 1
    io.say(f"Saved the bot token and chat id to the Keychain (service {service()}).")
    if daemon_running():
        io.say(RESTART_HINT)
    if write_default_config(config_path):
        io.say(f"Wrote a default config: {config_path}")
    else:
        io.say(f"Config already exists, left unchanged: {config_path}")

    signature = machine.signature(configured_machine(config_path))
    client = make_client(token, chat_id)
    try:
        await client.send(
            f"✅ {signature} is set up. Key-file changes will be reported here.", parse_mode=None
        )
    except TelegramError as exc:
        io.say(f"Test message failed: {exc}")
        return 1
    finally:
        await client.aclose()
    io.say("Test message sent. Next: add projects to the config, then `diffbell install`.")
    return 0


def configured_machine(config_path: Path) -> str | None:
    """This Mac's name as the config says it (ADR 0023); a config that can't be read
    is the Computer Name, as for a config without ``notify.machine``."""
    try:
        configured = load_config(config_path).notify.machine
    except ConfigError:
        configured = None
    return machine.resolve(configured)


async def _detect_chat_id(client: SetupClient, io: SetupIO, username: str) -> str | None:
    try:
        webhook = await client.get_webhook_info()
    except TelegramError as exc:
        io.say(f"Checking the bot's webhook failed: {exc}")
        return _manual_chat_id(io)
    if webhook.get("url"):
        io.say(
            "This bot has a webhook set, so updates can't be read here. "
            "The webhook was left untouched."
        )
        return _manual_chat_id(io)
    try:
        prompt = f"Open Telegram, send any message to @{username}, then press Enter… "
        chats = await _private_chats(client, io, prompt)
        if not chats:
            io.say(
                f"No message found yet — send any message to @{username} now, "
                "then press Enter to retry"
            )
            chats = await _private_chats(client, io, "")
    except TelegramError as exc:
        io.say(f"Reading the bot's chats failed: {exc}")
        return _manual_chat_id(io)
    if not chats:
        io.say("Still no private message to the bot was found.")
        return _manual_chat_id(io)
    chat = chats[-1]
    chat_id = str(chat["id"])
    who = chat.get("username") or chat.get("first_name") or chat_id
    answer = io.ask(f"Found a chat with {who} (id {chat_id}). Use it? [Y/n] ").strip().lower()
    if answer in {"", "y"}:
        return chat_id
    return _manual_chat_id(io)


async def _private_chats(client: SetupClient, io: SetupIO, prompt: str) -> list[dict[str, Any]]:
    """Wait for Enter, then read the private chats (TelegramError propagates)."""
    io.ask(prompt)
    updates = await client.get_updates()
    return [
        u["message"]["chat"]
        for u in updates
        if isinstance(u.get("message"), dict)
        and isinstance(u["message"].get("chat"), dict)
        and u["message"]["chat"].get("type") == "private"
        and "id" in u["message"]["chat"]
    ]


MANUAL_ATTEMPTS = 3


def _manual_chat_id(io: SetupIO) -> str | None:
    for attempt in range(MANUAL_ATTEMPTS):
        chat_id = "".join(io.ask("Enter the chat id manually (blank to abort): ").split())
        if not chat_id:
            return None
        digits = chat_id.removeprefix("-")
        if digits.isascii() and digits.isdigit():
            return chat_id
        left = MANUAL_ATTEMPTS - attempt - 1
        io.say(f"A chat id is a number (like 123456789 or -1001234567890); {left} attempt(s) left.")
    return None
