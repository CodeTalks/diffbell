from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from diffbell.telegram import ErrorKind, TelegramClient, TelegramError
from tests.fakes import FakeSender

TOKEN = "123:SECRET-TOKEN"


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> TelegramClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramClient(TOKEN, "42", http=http)


async def test_send_posts_html_and_returns_message_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    tg = client_with(handler)
    assert await tg.send("<b>hi</b>") == 7
    assert seen["path"] == f"/bot{TOKEN}/sendMessage"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["chat_id"] == "42"
    assert body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}


async def test_send_plain_text_omits_parse_mode() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    await client_with(handler).send("x", parse_mode=None)
    assert "parse_mode" not in bodies[0]


@pytest.mark.parametrize(
    ("status", "body", "kind", "retry_after"),
    [
        (
            429,
            {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 12}},
            ErrorKind.RETRYABLE,
            12.0,
        ),
        (502, {"ok": False, "description": "Bad Gateway"}, ErrorKind.RETRYABLE, None),
        (401, {"ok": False, "description": "Unauthorized"}, ErrorKind.AUTH, None),
        (403, {"ok": False, "description": "Forbidden: bot was blocked"}, ErrorKind.AUTH, None),
        (
            400,
            {"ok": False, "description": "Bad Request: can't parse entities"},
            ErrorKind.BAD_REQUEST,
            None,
        ),
    ],
)
async def test_error_taxonomy(
    status: int, body: dict[str, object], kind: ErrorKind, retry_after: float | None
) -> None:
    tg = client_with(lambda _req: httpx.Response(status, json=body))
    with pytest.raises(TelegramError) as exc:
        await tg.send("x")
    assert exc.value.kind is kind
    assert exc.value.retry_after == retry_after
    assert TOKEN not in str(exc.value)


async def test_network_error_is_retryable_and_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with pytest.raises(TelegramError) as exc:
        await client_with(handler).send("x")
    assert exc.value.kind is ErrorKind.RETRYABLE
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def ok(result: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(200, json={"ok": True, "result": result})


async def test_debug_logging_on_root_does_not_leak_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    assert await client_with(ok({"message_id": 3})).send("x") == 3
    assert TOKEN not in caplog.text


async def test_debug_logging_on_httpx_redacts_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    assert await client_with(ok({"message_id": 3})).send("x") == 3
    assert "HTTP Request" in caplog.text  # the record is kept, only the token is gone
    assert TOKEN not in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>captive portal</html>"),
        httpx.Response(200, json=["not", "a", "dict"]),
        httpx.Response(302, headers={"location": "http://portal.example/"}),
        httpx.Response(301, text=""),
    ],
)
async def test_malformed_or_redirect_replies_are_retryable(response: httpx.Response) -> None:
    tg = client_with(lambda _req: response)
    with pytest.raises(TelegramError) as exc:
        await tg.send("x")
    assert exc.value.kind is ErrorKind.RETRYABLE
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("value", ["soon", None, [1], {"a": 1}])
async def test_non_numeric_retry_after_is_none(value: object) -> None:
    body = {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": value}}
    tg = client_with(lambda _req: httpx.Response(429, json=body))
    with pytest.raises(TelegramError) as exc:
        await tg.send("x")
    assert exc.value.kind is ErrorKind.RETRYABLE
    assert exc.value.retry_after is None


@pytest.mark.parametrize(
    "result", [None, True, "7", [7], {}, {"message_id": None}, {"message_id": "abc"}]
)
async def test_send_with_odd_result_returns_zero(result: object) -> None:
    assert await client_with(ok(result)).send("x") == 0


async def test_send_accepts_numeric_string_message_id() -> None:
    assert await client_with(ok({"message_id": "9"})).send("x") == 9


@pytest.mark.parametrize("result", [None, True, [1], "x"])
async def test_get_me_and_webhook_non_dict_is_bad_request(result: object) -> None:
    tg = client_with(ok(result))
    for call in (tg.get_me, tg.get_webhook_info):
        with pytest.raises(TelegramError) as exc:
            await call()
        assert exc.value.kind is ErrorKind.BAD_REQUEST


@pytest.mark.parametrize("result", [None, {"a": 1}, "x", 3])
async def test_get_updates_non_list_is_empty(result: object) -> None:
    assert await client_with(ok(result)).get_updates() == []


async def test_get_updates_skips_non_dict_items() -> None:
    assert await client_with(ok([{"update_id": 1}, 5, "x"])).get_updates() == [{"update_id": 1}]


async def test_get_updates_sends_the_offset_only_when_given() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": []})

    tg = client_with(handler)
    await tg.get_updates()
    await tg.get_updates(offset=101)
    assert bodies == [{"timeout": 0}, {"timeout": 0, "offset": 101}]


async def test_get_me_and_updates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        results: dict[str, object] = {
            "getMe": {"id": 1, "username": "my_bot"},
            "getUpdates": [{"update_id": 5, "message": {"chat": {"id": 99, "type": "private"}}}],
            "getWebhookInfo": {"url": ""},
        }
        return httpx.Response(200, json={"ok": True, "result": results[method]})

    tg = client_with(handler)
    assert (await tg.get_me())["username"] == "my_bot"
    assert (await tg.get_updates())[0]["message"]["chat"]["id"] == 99
    assert (await tg.get_webhook_info())["url"] == ""


async def test_send_without_chat_id_is_a_programming_error() -> None:
    tg = TelegramClient(TOKEN, None, http=httpx.AsyncClient())
    with pytest.raises(ValueError):
        await tg.send("x")


@pytest.mark.parametrize(
    ("token", "text", "exc_name"),
    [
        ("123:SECRET\nTOKEN", "hi", "InvalidURL"),
        ("123:SECRET\x00TOKEN", "hi", "InvalidURL"),
        (TOKEN, "lone surrogate \ud800", "UnicodeEncodeError"),
    ],
)
async def test_unbuildable_request_is_bad_request_without_token(
    token: str, text: str, exc_name: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be sent")

    tg = TelegramClient(token, "42", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(TelegramError) as info:
        await tg.send(text)
    assert info.value.kind is ErrorKind.BAD_REQUEST
    assert str(info.value) == f"sendMessage: invalid request ({exc_name})"
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert "SECRET" not in str(info.value)


@pytest.mark.parametrize(("silent", "expected"), [(True, True), (False, None)])
async def test_silent_sets_disable_notification(silent: bool, expected: bool | None) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    await client_with(handler).send("x", parse_mode=None, silent=silent)
    assert bodies[0].get("disable_notification") == expected


async def test_the_fake_sender_records_silent() -> None:
    sender = FakeSender()
    await sender.send("a")
    await sender.send("b", parse_mode=None, silent=True)
    assert sender.silent == [False, True]
    assert sender.sent == [("a", "HTML"), ("b", None)]


@pytest.mark.parametrize("value", ['"nan"', '"inf"', '"-Infinity"', "NaN", "Infinity", "-1"])
async def test_a_non_finite_or_negative_retry_after_is_none(value: str) -> None:
    """Python's JSON parser also reads the bare literals NaN and Infinity."""
    body = '{"ok": false, "description": "429", "parameters": {"retry_after": ' + value + "}}"
    tg = client_with(lambda _req: httpx.Response(429, content=body.encode()))
    with pytest.raises(TelegramError) as exc:
        await tg.send("x")
    assert exc.value.retry_after is None
