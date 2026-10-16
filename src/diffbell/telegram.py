"""Minimal Telegram Bot API client with the error taxonomy from spec §8."""

from __future__ import annotations

import logging
import math
import re
from enum import StrEnum
from typing import Any, Protocol

import httpx

API_BASE = "https://api.telegram.org"

_TOKEN_IN_URL = re.compile(r"bot\d+:[^/\s\"']+")


class _RedactTokenFilter(logging.Filter):
    """Rewrites ``bot<id>:<secret>`` in a record's message to ``bot<redacted>``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record: let logging report it as usual
            return True
        redacted = _TOKEN_IN_URL.sub("bot<redacted>", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def _guard_http_loggers() -> None:
    # Defense in depth: httpx logs every request URL at INFO, and the Bot API URL
    # contains the token. Raise the level so INFO is dropped by default, and add a
    # redacting filter because a caller (or a test's caplog) can lower it again.
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)
        if not any(isinstance(f, _RedactTokenFilter) for f in logger.filters):
            logger.addFilter(_RedactTokenFilter())


_guard_http_loggers()


class ErrorKind(StrEnum):
    RETRYABLE = "retryable"  # 429, 5xx, 3xx, network, non-API reply
    AUTH = "auth"  # 401/403/404: bad token or bot blocked
    BAD_REQUEST = "bad_request"  # any other 4xx, e.g. malformed HTML
    NOT_SET_UP = "not_set_up"  # no bot yet (SenderSlot empty): nothing was sent


class TelegramError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        description: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(description)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


class NotSetUp(TelegramError):
    """A send while Telegram isn't set up (bot onboarding spec §1). No network call
    was made; the notifier holds instead of failing."""

    def __init__(self) -> None:
        super().__init__(ErrorKind.NOT_SET_UP, "Telegram isn't set up yet")


class Sender(Protocol):
    """What the notifier, health and the heartbeat need. Lets tests use a fake.
    ``silent`` sends without a sound (the heartbeat)."""

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int: ...


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str | None,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = API_BASE,
        timeout: float = 15.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        network_error: str | None = None
        invalid_request: str | None = None
        try:
            resp = await self._http.post(url, json=payload)
        except httpx.HTTPError as exc:
            network_error = type(exc).__name__
        except (httpx.InvalidURL, UnicodeEncodeError) as exc:
            # A token with control characters, or text with a lone surrogate:
            # the request cannot be built, and retrying will not change that.
            invalid_request = type(exc).__name__
        # Raised outside the except block so they have no __context__: the httpx
        # exception's message can contain the URL, and with it the token.
        if network_error is not None:
            raise TelegramError(ErrorKind.RETRYABLE, f"{method}: network error ({network_error})")
        if invalid_request is not None:
            raise TelegramError(
                ErrorKind.BAD_REQUEST, f"{method}: invalid request ({invalid_request})"
            )

        status = resp.status_code
        try:
            body: Any = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            # Not a Bot API reply (captive portal, proxy, HTML error page): retry
            # rather than drop the message as a permanent failure.
            raise TelegramError(
                ErrorKind.RETRYABLE, f"{method}: unexpected reply (HTTP {status})", status=status
            )
        if status == 200 and body.get("ok"):
            return body.get("result")

        description = f"{method}: {body.get('description') or f'HTTP {status}'}"
        if status == 429:
            params = body.get("parameters")
            raw = params.get("retry_after") if isinstance(params, dict) else None
            raise TelegramError(
                ErrorKind.RETRYABLE, description, status=status, retry_after=_as_float(raw)
            )
        if status >= 500 or 300 <= status < 400:
            raise TelegramError(ErrorKind.RETRYABLE, description, status=status)
        if status in (401, 403, 404):
            raise TelegramError(ErrorKind.AUTH, description, status=status)
        raise TelegramError(ErrorKind.BAD_REQUEST, description, status=status)

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        if not self._chat_id:
            raise ValueError("chat_id is required to send messages")
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if silent:
            payload["disable_notification"] = True
        result = await self._call("sendMessage", payload)
        # Telegram accepted the message: an odd result must not turn that into a
        # failure (which would resend it). 0 means "sent, id unknown".
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int | str):
            return 0
        try:
            return int(message_id)
        except ValueError:
            return 0

    async def _call_dict(self, method: str) -> dict[str, Any]:
        result = await self._call(method, {})
        if not isinstance(result, dict):
            raise TelegramError(ErrorKind.BAD_REQUEST, f"{method}: unexpected result type")
        return dict(result)

    async def get_me(self) -> dict[str, Any]:
        return await self._call_dict("getMe")

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._call_dict("getWebhookInfo")

    async def get_updates(self, *, offset: int | None = None) -> list[dict[str, Any]]:
        """Pending updates (Telegram returns at most the oldest 100). ``offset``
        confirms every update below it: Telegram never returns those again."""
        payload: dict[str, Any] = {"timeout": 0}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [dict(u) for u in result if isinstance(u, dict)]


def _as_float(value: Any) -> float | None:
    """A usable ``retry_after``: a finite number of seconds, never negative; else None
    (the caller then waits its normal backoff, so ``next_try_at`` is never NaN)."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        seconds = float(value)
    except (ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
