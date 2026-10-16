"""SenderSlot: the daemon's one sender, empty until Telegram is set up (bot
onboarding spec §1)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from diffbell.delivery import classify
from diffbell.sender_slot import SenderSlot
from diffbell.telegram import ErrorKind, NotSetUp, TelegramError
from tests.fakes import FakeSender


class Factory:
    """make_client: records each (token, chat_id) and hands out a new FakeSender."""

    def __init__(self) -> None:
        self.made: list[tuple[str, str | None]] = []
        self.clients: list[FakeSender] = []

    def __call__(self, token: str, chat_id: str | None) -> FakeSender:
        self.made.append((token, chat_id))
        self.clients.append(FakeSender())
        return self.clients[-1]


def test_not_set_up_is_a_telegram_error_of_its_own_kind() -> None:
    exc = NotSetUp()
    assert isinstance(exc, TelegramError)
    assert exc.kind is ErrorKind.NOT_SET_UP and exc.kind.value == "not_set_up"
    assert str(exc) == "Telegram isn't set up yet"
    assert (exc.status, exc.retry_after) == (None, None)


def test_classify_passes_not_set_up_through() -> None:
    assert classify(NotSetUp(), 0).kind is ErrorKind.NOT_SET_UP


async def test_an_empty_slot_is_not_configured_and_raises_not_set_up() -> None:
    slot = SenderSlot(None, make_client=Factory())
    assert not slot.configured and slot.bot_username is None
    with pytest.raises(NotSetUp):
        await slot.send("hi")


async def test_a_configured_slot_forwards_every_argument() -> None:
    inner = FakeSender([7])
    slot = SenderSlot(inner, make_client=Factory())
    assert slot.configured
    assert await slot.send("hi", parse_mode=None, silent=True) == 7
    assert inner.sent == [("hi", None)] and inner.silent == [True]


async def test_swap_installs_the_new_client_and_closes_the_old_one() -> None:
    old, factory = FakeSender(), Factory()
    slot = SenderSlot(old, make_client=factory)
    await slot.swap("123:new", "42", bot_username="new_bot")
    assert factory.made == [("123:new", "42")]
    assert old.closed and not factory.clients[0].closed
    assert slot.configured and slot.bot_username == "new_bot"
    await slot.send("after")
    assert old.sent == [] and factory.clients[0].sent == [("after", "HTML")]


async def test_swap_from_empty_configures_the_slot() -> None:
    factory = Factory()
    slot = SenderSlot(None, make_client=factory)
    await slot.swap("123:tok", "42", bot_username=None)
    assert slot.configured and slot.bot_username is None
    assert await slot.send("first") > 0
    assert factory.clients[0].sent == [("first", "HTML")]


async def test_a_failing_close_of_the_old_client_still_swaps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BadClose(FakeSender):
        async def aclose(self) -> None:
            raise RuntimeError("boom")

    factory = Factory()
    slot = SenderSlot(BadClose(), make_client=factory)
    await slot.swap("123:tok", "42", bot_username="b")
    assert slot.configured and len(factory.clients) == 1
    assert "RuntimeError" in caplog.text
    await slot.send("after")
    assert factory.clients[0].sent == [("after", "HTML")]


async def test_fetch_username_reads_get_me() -> None:
    inner = FakeSender()
    slot = SenderSlot(inner, make_client=Factory())
    await slot.fetch_username()
    assert slot.bot_username == "my_bot" and inner.me_calls == 1


@pytest.mark.parametrize(
    "error",
    [
        TelegramError(ErrorKind.RETRYABLE, "getMe: network error for bot123:SECRET"),
        ValueError("odd reply bot123:SECRET"),
    ],
)
async def test_a_failing_fetch_leaves_none_and_logs_only_the_type(
    caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    inner = FakeSender()
    inner.me_error = error
    slot = SenderSlot(inner, make_client=Factory())
    with caplog.at_level(logging.DEBUG):
        await slot.fetch_username()
    assert slot.bot_username is None
    assert "SECRET" not in caplog.text
    assert type(error).__name__ in caplog.text


async def test_fetch_on_an_empty_slot_does_nothing() -> None:
    slot = SenderSlot(None, make_client=Factory())
    await slot.fetch_username()
    assert slot.bot_username is None


async def test_a_fetch_answered_after_a_swap_does_not_overwrite_the_new_username() -> None:
    inner = FakeSender()
    inner.username = "old_bot"
    slot = SenderSlot(inner, make_client=Factory())

    async def get_me_then_swap() -> dict[str, object]:
        await slot.swap("123:new", "42", bot_username="new_bot")
        return {"username": "old_bot"}

    inner.get_me = get_me_then_swap  # type: ignore[method-assign]
    await slot.fetch_username()
    assert slot.bot_username == "new_bot"


async def test_aclose_closes_the_client() -> None:
    inner = FakeSender()
    slot = SenderSlot(inner, make_client=Factory())
    await slot.aclose()
    assert inner.closed and not slot.configured


class BlockingSender(FakeSender):
    """send() blocks until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        self.started.set()
        await self.release.wait()
        return await super().send(text, parse_mode=parse_mode, silent=silent)


async def test_swap_does_not_close_a_client_under_an_in_flight_send() -> None:
    old, factory = BlockingSender(), Factory()
    slot = SenderSlot(old, make_client=factory)
    task = asyncio.create_task(slot.send("slow"))
    await old.started.wait()
    await slot.swap("123:new", "42", bot_username="b")
    assert not old.closed and slot.configured
    await slot.send("fresh")
    assert factory.clients[0].sent == [("fresh", "HTML")]
    old.release.set()
    assert await task == 101
    assert old.closed


async def test_aclose_waits_for_an_in_flight_send() -> None:
    old = BlockingSender()
    slot = SenderSlot(old, make_client=Factory())
    task = asyncio.create_task(slot.send("slow"))
    await old.started.wait()
    await slot.aclose()
    assert not old.closed and not slot.configured
    old.release.set()
    assert await task == 101
    assert old.closed


async def test_a_failing_deferred_close_is_logged_and_does_not_break_the_send(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BadClose(BlockingSender):
        async def aclose(self) -> None:
            raise RuntimeError("boom")

    old = BadClose()
    slot = SenderSlot(old, make_client=Factory())
    task = asyncio.create_task(slot.send("slow"))
    await old.started.wait()
    await slot.swap("123:new", "42", bot_username=None)
    old.release.set()
    assert await task == 101
    assert "RuntimeError" in caplog.text
