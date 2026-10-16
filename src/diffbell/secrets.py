"""Bot token and chat id in the macOS login Keychain (spec D10, bot onboarding spec).

We shell out to /usr/bin/security rather than using a Python Keychain binding: the
item's access-control list then always names /usr/bin/security, so an upgraded Python
(or the launchd agent's interpreter) never triggers an unanswerable access prompt.

Both writers (the web wizard and `diffbell setup`) go through `save_credentials`:
marker, chat id, token, then the marker is deleted. While the marker exists the pair
is not trusted (`credentials` reads it as not set up).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

DEFAULT_SERVICE = "io.688.diffbell"
# Scratch live checks only (bot onboarding spec §4 "Live checks"): the launchd plist
# never sets it, so the agent always uses DEFAULT_SERVICE.
SERVICE_ENV = "DIFFBELL_KEYCHAIN_SERVICE"
SECURITY = "/usr/bin/security"
ACCOUNT_BOT_TOKEN = "bot-token"
ACCOUNT_CHAT_ID = "chat-id"
ACCOUNT_SAVE_PENDING = "save-pending"
_NOT_FOUND = 44

Runner = Callable[..., subprocess.CompletedProcess[str]]


class SecretStoreError(Exception):
    pass


def service() -> str:
    """The Keychain service: ``DIFFBELL_KEYCHAIN_SERVICE`` or DEFAULT_SERVICE.
    Read on every call. Only DEFAULT_SERVICE or DEFAULT_SERVICE-<suffix> is accepted; anything else is refused,
    so a typo can never read or overwrite another app's item."""
    value = os.environ.get(SERVICE_ENV, DEFAULT_SERVICE)
    if value != DEFAULT_SERVICE and not value.startswith(DEFAULT_SERVICE + "-"):
        raise SecretStoreError(
            f"{SERVICE_ENV} must be {DEFAULT_SERVICE!r} or start with {DEFAULT_SERVICE + '-'!r} (got {value!r})"
        )
    return value


_TIMEOUT_SECONDS = 10


def _run(
    runner: Runner, args: list[str], *, timeout: float = _TIMEOUT_SECONDS, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    # Raised outside any except block: TimeoutExpired carries the partial output,
    # which for a read is the secret itself, so it must not ride along as context.
    # start_new_session: no controlling terminal, so `security ... -w` cannot open
    # /dev/tty (to prompt, or to read a password) and must use stdin; setup runs
    # in an interactive terminal, where it otherwise would bypass our input.
    with contextlib.suppress(subprocess.TimeoutExpired):
        return runner(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            start_new_session=True,
            **kwargs,
        )
    raise SecretStoreError(f"keychain command {args[1]!r} timed out after {timeout:g}s")


def _service(explicit: str | None) -> str:
    """``explicit`` as given (the move's one exception to the guard, rename spec section 3:
    only rename_install passes it, tests/test_secrets.py checks), else service()."""
    return explicit if explicit is not None else service()


def get_secret(
    account: str,
    *,
    service: str | None = None,
    runner: Runner = subprocess.run,
    timeout: float = _TIMEOUT_SECONDS,
) -> str | None:
    """The stored value, or None if the item does not exist (a definitive answer)."""
    result = _run(
        runner,
        [SECURITY, "find-generic-password", "-s", _service(service), "-a", account, "-w"],
        timeout=timeout,
    )
    if result.returncode == _NOT_FOUND:
        return None
    if result.returncode != 0:
        raise SecretStoreError(f"keychain read failed for {account!r} (exit {result.returncode})")
    return result.stdout.rstrip("\n")


def _check_value(value: str) -> None:
    if not value or "\n" in value:
        raise ValueError("secret must be a non-empty single line")


def set_secret(account: str, value: str, *, runner: Runner = subprocess.run) -> None:
    """Create or update the item. The value is passed on stdin (twice: `security`
    asks for it and then for a confirmation), never in argv."""
    _check_value(value)
    result = _run(
        runner,
        [SECURITY, "add-generic-password", "-U", "-s", service(), "-a", account, "-w"],
        input=f"{value}\n{value}\n",
    )
    if result.returncode != 0:
        raise SecretStoreError(f"keychain write failed for {account!r} (exit {result.returncode})")


def delete_secret(
    account: str, *, service: str | None = None, runner: Runner = subprocess.run
) -> None:
    """Delete the item; an item that doesn't exist counts as deleted."""
    result = _run(
        runner, [SECURITY, "delete-generic-password", "-s", _service(service), "-a", account]
    )
    if result.returncode not in (0, _NOT_FOUND):
        raise SecretStoreError(f"keychain delete failed for {account!r} (exit {result.returncode})")


def has_item(account: str, *, service: str | None = None, runner: Runner = subprocess.run) -> bool:
    """Whether the item exists, without reading its value (no ``-w``)."""
    result = _run(
        runner, [SECURITY, "find-generic-password", "-s", _service(service), "-a", account]
    )
    if result.returncode == 0:
        return True
    if result.returncode == _NOT_FOUND:
        return False
    raise SecretStoreError(f"keychain lookup failed for {account!r} (exit {result.returncode})")


@dataclass(frozen=True)
class Credentials:
    token: str
    chat_id: str


@dataclass(frozen=True)
class Missing:
    """Why Telegram is not set up (bot onboarding spec §1, the startup table)."""

    what: Literal["both", "bot token", "chat id", "unfinished save"]

    @property
    def message(self) -> str:
        if self.what == "both":
            return "not set up"
        if self.what == "unfinished save":
            return "not set up: an earlier save didn't finish"
        return f"not set up: the {self.what} is missing"


def credentials(get: Callable[[str], str | None] = get_secret) -> Credentials | Missing:
    """The pair, or why Telegram is not set up. The save-pending marker is read first:
    while it exists the pair is not trusted. Raises SecretStoreError from ``get``: a
    Keychain that can't be read is never taken for a fresh install."""
    if get(ACCOUNT_SAVE_PENDING) is not None:
        return Missing("unfinished save")
    token = get(ACCOUNT_BOT_TOKEN)
    chat_id = get(ACCOUNT_CHAT_ID)
    if token and chat_id:
        return Credentials(token, chat_id)
    if not token and not chat_id:
        return Missing("both")
    return Missing("bot token" if not token else "chat id")


def save_credentials(
    token: str,
    chat_id: str,
    *,
    put: Callable[[str, str], None] = set_secret,
    delete: Callable[[str], None] = delete_secret,
) -> None:
    """The one write order (bot onboarding spec §2): marker, chat id, token, delete
    the marker. Any failure stops the sequence and leaves the marker. Blocking (a
    subprocess per step): the web calls it through ``asyncio.to_thread``."""
    _check_value(token)
    _check_value(chat_id)
    put(ACCOUNT_SAVE_PENDING, str(time.time()))
    put(ACCOUNT_CHAT_ID, chat_id)
    put(ACCOUNT_BOT_TOKEN, token)
    delete(ACCOUNT_SAVE_PENDING)
