"""Settings → Telegram: the wizard's endpoints and the status (bot onboarding spec
§2). A FakeBot stands in for Telegram and a dict for the Keychain: no test touches
either for real."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import Any, NoReturn

import httpx
import pytest

from diffbell import secrets as secrets_mod
from diffbell.render import sample_message
from diffbell.secrets import (
    ACCOUNT_BOT_TOKEN,
    ACCOUNT_CHAT_ID,
    ACCOUNT_SAVE_PENDING,
    SecretStoreError,
)
from diffbell.store import TELEGRAM_SAVED_AT
from diffbell.telegram import ErrorKind, NotSetUp, TelegramError
from diffbell.web import auth
from diffbell.web.app import create_app
from diffbell.web.telegram_setup import CHAT_NAME_KEY, DRAFT_TTL, UNSAVABLE, Drafts
from tests.web import support
from tests.web.support import BASE, Env, log_in, make_context
from tests.web.test_app import add_event

TOKEN = "4242424242:AAH-test_token-value-for-the-wizard-01"
SECRET_PART = TOKEN.split(":")[1]
CHAT = 4815162342
BOT = "diffbell_test_bot"
GONE = "This setup expired or was restarted in another tab; check the token again."
UNAUTHORIZED = TelegramError(ErrorKind.AUTH, "getMe: Unauthorized", status=401)
OFFLINE = TelegramError(ErrorKind.RETRYABLE, "getMe: network error (ConnectError)")


def message(
    chat_id: int,
    date: float,
    *,
    kind: str = "private",
    name: str = "Alex",
    update_id: int = 1,
) -> dict[str, Any]:
    """One getUpdates entry: a message in a chat at `date` (whole seconds)."""
    chat = {"id": chat_id, "type": kind, "first_name": name}
    return {
        "update_id": update_id,
        "message": {"message_id": 1, "date": int(date), "chat": chat},
    }


async def new_draft(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/telegram/draft", json={"token": TOKEN})
    assert response.status_code == 200, response.text
    return str(response.json()["draft"])


async def detected(env: Env, client: httpx.AsyncClient, draft: str) -> str:
    """The choice handle of CHAT, found after the draft was created."""
    env.bot.updates = [message(CHAT, env.now + 1)]
    response = await client.get(f"/api/telegram/draft/{draft}/chats")
    assert response.status_code == 200, response.text
    return str(response.json()["chats"][0]["choice"])


# -- check the token ----------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        "hello",
        "123:short",
        "abc:" + "A" * 35,
        "123:" + "A" * 29,
        "123:" + "A" * 34 + "!",
        "123:" + "A" * 34 + " x",
        "١٢٣:" + "A" * 35,
    ],
)
async def test_a_malformed_token_is_refused_before_any_network_call(
    env: Env, client: httpx.AsyncClient, token: str
) -> None:
    response = await client.post("/api/telegram/draft", json={"token": token})
    assert response.status_code == 422
    assert response.json() == {"error": "That doesn't look like a bot token."}
    assert env.bot.calls == []


async def test_check_returns_only_the_draft_handle_and_the_bot(
    env: Env, client: httpx.AsyncClient
) -> None:
    response = await client.post("/api/telegram/draft", json={"token": f"  {TOKEN}\n"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"draft", "bot"}
    assert body["bot"] == BOT
    assert SECRET_PART not in response.text
    assert env.bot.calls == [("get_me", TOKEN, None)]  # stripped before use
    assert env.bot.closed == 1


@pytest.mark.parametrize(
    ("error", "status", "text"),
    [
        (UNAUTHORIZED, 422, "Telegram rejected this token."),
        (OFFLINE, 503, "Couldn't reach Telegram; try again."),
    ],
)
async def test_a_failed_check_says_why(
    env: Env, client: httpx.AsyncClient, error: TelegramError, status: int, text: str
) -> None:
    env.bot.errors["get_me"] = error
    response = await client.post("/api/telegram/draft", json={"token": TOKEN})
    assert response.status_code == status
    assert response.json() == {"error": text}


# -- find the chat --------------------------------------------------------------------


async def test_chats_are_private_chats_messaged_after_the_draft(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    env.bot.updates = [
        message(111, env.now - 1, name="Before"),  # before Check: maybe someone else
        message(222, env.now + 5, kind="group", name="Team"),
        message(CHAT, env.now + 5),
        message(CHAT, env.now + 9),  # the same chat again
    ]
    response = await client.get(f"/api/telegram/draft/{draft}/chats")
    assert response.status_code == 200
    chats = response.json()["chats"]
    assert [c["name"] for c in chats] == ["Alex"]
    assert str(CHAT) not in response.text and "111" not in response.text
    again = (await client.get(f"/api/telegram/draft/{draft}/chats")).json()["chats"]
    assert again == chats  # the same handle on every poll: the page's pick stays


async def test_a_message_in_the_second_check_was_clicked_counts(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.now = 1_000_000.75
    draft = await new_draft(client)
    env.bot.updates = [message(CHAT, 1_000_000)]
    chats = (await client.get(f"/api/telegram/draft/{draft}/chats")).json()["chats"]
    assert len(chats) == 1


async def test_no_chat_yet_is_an_empty_list(env: Env, client: httpx.AsyncClient) -> None:
    draft = await new_draft(client)
    response = await client.get(f"/api/telegram/draft/{draft}/chats")
    assert response.status_code == 200
    assert response.json() == {"chats": []}


async def test_stale_updates_are_confirmed_so_a_new_start_shows_up(
    env: Env, client: httpx.AsyncClient
) -> None:
    """Telegram returns only the oldest 100 unconfirmed updates: a reused bot's
    backlog must be confirmed (offset) or the new /start never shows."""
    env.bot.updates = [
        message(1000 + i, env.now - 60, name=f"Old{i}", update_id=i) for i in range(1, 121)
    ]
    draft = await new_draft(client)
    path = f"/api/telegram/draft/{draft}/chats"
    assert (await client.get(path)).json() == {"chats": []}  # updates 1-100, all stale
    env.bot.updates.append(message(CHAT, env.now + 1, update_id=121))
    chats = (await client.get(path)).json()["chats"]  # 101-121: the new /start is in
    assert [c["name"] for c in chats] == ["Alex"]
    assert (await client.get(path)).json()["chats"] == chats
    assert env.bot.offsets == [None, 101, 122]


async def test_a_found_chat_stays_after_its_update_is_confirmed(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    path = f"/api/telegram/draft/{draft}/chats"
    env.bot.updates = [message(CHAT, env.now + 1, update_id=7)]
    first = (await client.get(path)).json()["chats"]
    env.bot.updates.append(message(555, env.now + 2, name="Second", update_id=8))
    second = (await client.get(path)).json()["chats"]
    assert env.bot.offsets == [None, 8]
    assert [c["name"] for c in second] == ["Alex", "Second"]
    assert second[0] == first[0]
    assert (await client.get(path)).json()["chats"] == second  # nothing new: still both
    assert env.bot.offsets == [None, 8, 9]


async def test_a_webhook_is_a_409_and_the_chat_id_can_be_entered(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    env.bot.errors["get_updates"] = TelegramError(
        ErrorKind.BAD_REQUEST,
        "getUpdates: Conflict: can't use getUpdates method while webhook is active",
        status=409,
    )
    response = await client.get(f"/api/telegram/draft/{draft}/chats")
    assert response.status_code == 409
    assert response.json() == {
        "error": "This bot has a webhook, or another program reads its updates; "
        "enter the chat id instead."
    }
    manual = await client.post(f"/api/telegram/draft/{draft}/chats", json={"chat_id": str(CHAT)})
    assert manual.status_code == 200
    assert manual.json()["name"] is None


@pytest.mark.parametrize("raw", [" 4815162342 ", "-1001234567890", "-100 123\t456"])
async def test_a_manual_chat_id_may_be_negative(
    env: Env, client: httpx.AsyncClient, raw: str
) -> None:
    draft = await new_draft(client)
    response = await client.post(f"/api/telegram/draft/{draft}/chats", json={"chat_id": raw})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"choice", "name"}
    assert body["name"] is None
    assert "".join(raw.split()).lstrip("-") not in response.text


@pytest.mark.parametrize("raw", ["", "abc", "12a", "--5", "+5", "1.5", "١٢٣", "1" * 21])
async def test_a_manual_chat_id_is_a_number(env: Env, client: httpx.AsyncClient, raw: str) -> None:
    draft = await new_draft(client)
    response = await client.post(f"/api/telegram/draft/{draft}/chats", json={"chat_id": raw})
    assert response.status_code == 422
    assert response.json() == {"error": "A chat id is a number, like 123456789 or -1001234567890."}


# -- the draft's lifetime -------------------------------------------------------------


async def test_the_draft_lives_30_minutes_after_its_last_use(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    path = f"/api/telegram/draft/{draft}/chats"
    env.now += DRAFT_TTL - 1
    assert (await client.get(path)).status_code == 200
    env.now += DRAFT_TTL - 1  # sliding: counted from the last call
    assert (await client.get(path)).status_code == 200
    env.now += DRAFT_TTL + 1
    gone = await client.get(path)
    assert gone.status_code == 404
    assert gone.json() == {"error": GONE}


async def test_a_second_draft_replaces_the_first(env: Env, client: httpx.AsyncClient) -> None:
    first = await new_draft(client)
    second = await new_draft(client)
    assert first != second
    assert (await client.get(f"/api/telegram/draft/{first}/chats")).status_code == 404
    assert (await client.get(f"/api/telegram/draft/{second}/chats")).status_code == 200


async def test_an_unknown_draft_is_404_everywhere(env: Env, client: httpx.AsyncClient) -> None:
    await new_draft(client)
    base = "/api/telegram/draft/not-a-draft"
    responses = [
        await client.get(f"{base}/chats"),
        await client.post(f"{base}/chats", json={"chat_id": "1"}),
        await client.post(f"{base}/test", json={"choice": "x"}),
        await client.post(f"{base}/save", json={"choice": "x"}),
    ]
    assert [r.status_code for r in responses] == [404] * 4
    assert all(r.json() == {"error": GONE} for r in responses)
    assert [c[0] for c in env.bot.calls] == ["get_me"]  # only the Check


async def test_the_wizard_needs_a_session_and_the_csrf_token(
    env: Env, anon: httpx.AsyncClient
) -> None:
    assert (await anon.get("/api/telegram")).status_code == 401
    assert (await anon.post("/api/telegram/draft", json={"token": TOKEN})).status_code == 401
    await log_in(env, anon)
    del anon.headers[auth.CSRF_HEADER]
    response = await anon.post("/api/telegram/draft", json={"token": TOKEN})
    assert response.status_code == 403
    assert env.bot.calls == []


# -- test and save --------------------------------------------------------------------


async def test_test_sends_the_sample_message_to_the_chosen_chat(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    response = await client.post(f"/api/telegram/draft/{draft}/test", json={"choice": choice})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert env.bot.sent == [(str(CHAT), sample_message(datetime.fromtimestamp(env.now)))]
    assert env.setups == []  # a test saves nothing


async def test_test_needs_a_chat_of_this_draft(env: Env, client: httpx.AsyncClient) -> None:
    draft = await new_draft(client)
    response = await client.post(f"/api/telegram/draft/{draft}/test", json={"choice": "nope"})
    assert response.status_code == 422
    assert response.json() == {"error": "That chat isn't part of this setup; find the chat again."}
    assert env.bot.sent == []


async def test_a_refused_test_message_says_so(env: Env, client: httpx.AsyncClient) -> None:
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    env.bot.errors["send"] = TelegramError(
        ErrorKind.AUTH, "sendMessage: Forbidden: bot was blocked by the user", status=403
    )
    response = await client.post(f"/api/telegram/draft/{draft}/test", json={"choice": choice})
    assert response.status_code == 422
    assert response.json() == {
        "error": "Telegram refused to send to this chat; send /start to the bot and try again."
    }


async def test_save_writes_the_keychain_then_switches_the_daemon(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.configured, env.bot_username = False, None
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    before = len(env.bot.calls)
    response = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert response.status_code == 200
    assert response.json() == {"bot": BOT}
    assert env.bot.calls[before] == ("get_me", TOKEN, None)  # the token is checked again
    assert env.keychain_log == [
        f"put {ACCOUNT_SAVE_PENDING}",
        f"put {ACCOUNT_CHAT_ID}",
        f"put {ACCOUNT_BOT_TOKEN}",
        f"delete {ACCOUNT_SAVE_PENDING}",
    ]
    assert env.keychain == {ACCOUNT_CHAT_ID: str(CHAT), ACCOUNT_BOT_TOKEN: TOKEN}
    assert env.setups == [(TOKEN, str(CHAT), BOT)]
    assert env.store.kv_get(CHAT_NAME_KEY) == "Alex"
    assert (await client.get(f"/api/telegram/draft/{draft}/chats")).status_code == 404
    status = (await client.get("/api/telegram")).json()
    assert (status["configured"], status["bot"], status["chat_name"]) == (True, BOT, "Alex")


@pytest.mark.parametrize("fail", [ACCOUNT_CHAT_ID, ACCOUNT_BOT_TOKEN])
async def test_a_failed_keychain_write_keeps_the_draft_and_the_marker(
    env: Env, client: httpx.AsyncClient, fail: str
) -> None:
    env.configured, env.bot_username = False, None
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    env.keychain_fail = fail
    response = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert response.status_code == 500
    assert response.json() == {"error": "Saving to the Keychain failed; try Save again."}
    assert ACCOUNT_SAVE_PENDING in env.keychain  # the half-written pair is not trusted
    assert env.setups == [] and env.configured is False  # the slot is unchanged
    env.keychain_fail = None
    retry = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert retry.status_code == 200
    assert ACCOUNT_SAVE_PENDING not in env.keychain
    assert env.setups == [(TOKEN, str(CHAT), BOT)]


async def test_a_network_error_during_save_keeps_the_draft(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    env.bot.errors["get_me"] = OFFLINE
    response = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert response.status_code == 503
    assert env.keychain_log == [] and env.setups == []
    del env.bot.errors["get_me"]
    retry = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert retry.status_code == 200


async def test_a_chat_entered_by_hand_saves_no_name(env: Env, client: httpx.AsyncClient) -> None:
    env.store.kv_set(CHAT_NAME_KEY, "The old bot's chat")
    draft = await new_draft(client)
    manual = await client.post(
        f"/api/telegram/draft/{draft}/chats", json={"chat_id": "-1001234567890"}
    )
    choice = manual.json()["choice"]
    response = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert response.status_code == 200
    assert env.store.kv_get(CHAT_NAME_KEY) is None
    assert env.setups == [(TOKEN, "-1001234567890", BOT)]


async def test_a_value_the_keychain_writer_refuses_is_a_fixed_422(
    env: Env, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """save_credentials validates both values before writing anything (ValueError):
    a fixed text, never the value, and nothing written."""

    def refuse(token: str, chat_id: str, **_: Any) -> None:
        raise ValueError(f"bad value {token!r} {chat_id!r}")

    monkeypatch.setattr(support, "save_credentials", refuse)
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    response = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert response.status_code == 422
    assert response.json() == {"error": UNSAVABLE}
    assert env.keychain_log == [] and env.setups == []
    assert (await client.get(f"/api/telegram/draft/{draft}/chats")).status_code == 200


async def test_a_second_save_while_one_runs_is_409(env: Env, client: httpx.AsyncClient) -> None:
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    gate = asyncio.Event()
    env.bot.gates["get_me"] = gate
    path = f"/api/telegram/draft/{draft}/save"
    first = asyncio.create_task(client.post(path, json={"choice": choice}))
    for _ in range(200):
        if env.bot.waiting:
            break
        await asyncio.sleep(0.005)
    assert env.bot.waiting
    try:
        second = await asyncio.wait_for(client.post(path, json={"choice": choice}), 5)
    finally:
        gate.set()
    assert second.status_code == 409
    assert second.json() == {"error": "Saving is already in progress."}
    gate.set()
    assert (await first).status_code == 200
    assert env.setups == [(TOKEN, str(CHAT), BOT)]


async def test_a_save_of_another_tabs_draft_while_one_runs_is_409(
    env: Env, client: httpx.AsyncClient
) -> None:
    """One save at a time across all drafts: two interleaved Keychain sequences
    could leave a trusted, mismatched token and chat id."""
    draft_a = await new_draft(client)
    choice_a = await detected(env, client, draft_a)
    gate = asyncio.Event()
    env.bot.gates["get_me"] = gate
    first = asyncio.create_task(
        client.post(f"/api/telegram/draft/{draft_a}/save", json={"choice": choice_a})
    )
    for _ in range(200):
        if env.bot.waiting:
            break
        await asyncio.sleep(0.005)
    assert env.bot.waiting
    del env.bot.gates["get_me"]  # the second tab's Check isn't held
    try:
        draft_b = await new_draft(client)  # replaces draft A
        choice_b = await detected(env, client, draft_b)
        path_b = f"/api/telegram/draft/{draft_b}/save"
        second = await asyncio.wait_for(client.post(path_b, json={"choice": choice_b}), 5)
    finally:
        gate.set()
    assert second.status_code == 409
    assert second.json() == {"error": "Saving is already in progress."}
    assert (await first).status_code == 200
    assert len(env.setups) == 1
    retry = await client.post(path_b, json={"choice": choice_b})
    assert retry.status_code == 200
    assert len(env.setups) == 2


async def test_a_cancelled_save_stays_in_progress_until_the_keychain_write_ends(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that disconnects mid-save cancels the request, not the Keychain
    thread: the in-progress mark stays until that write ends, so no second save can
    interleave with it."""
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def blocking(token: str, chat_id: str, **_: Any) -> None:
        calls.append(chat_id)
        if len(calls) == 1:
            started.set()
            release.wait(10)

    monkeypatch.setattr(support, "save_credentials", blocking)
    app = create_app(make_context(env))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await log_in(env, client)
        drafts = app.state.telegram_drafts
        draft = await new_draft(client)
        choice = await detected(env, client, draft)
        path = f"/api/telegram/draft/{draft}/save"
        try:
            request = asyncio.create_task(client.post(path, json={"choice": choice}))
            assert await asyncio.to_thread(started.wait, 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert drafts.saving is True
            second = await client.post(path, json={"choice": choice})
            assert second.status_code == 409
            assert second.json() == {"error": "Saving is already in progress."}
        finally:
            release.set()
        for _ in range(200):
            if not drafts.saving:
                break
            await asyncio.sleep(0.005)
        assert drafts.saving is False
        retry = await client.post(path, json={"choice": choice})
        assert retry.status_code == 200
        assert len(calls) == 2


async def test_a_failed_save_can_be_retried_after_the_in_progress_mark(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    env.bot.errors["get_me"] = OFFLINE
    path = f"/api/telegram/draft/{draft}/save"
    assert (await client.post(path, json={"choice": choice})).status_code == 503
    del env.bot.errors["get_me"]
    assert (await client.post(path, json={"choice": choice})).status_code == 200


def test_drafts_create_and_sweep_drop_an_expired_draft() -> None:
    now = [100.0]
    drafts = Drafts(clock=lambda: now[0])
    drafts.create(TOKEN, BOT)
    now[0] += DRAFT_TTL - 1
    drafts.sweep()
    assert drafts.current is not None
    now[0] += DRAFT_TTL + 1
    drafts.sweep()
    assert drafts.current is None


def test_drafts_drop_needs_the_exact_id() -> None:
    drafts = Drafts(clock=lambda: 0.0)
    draft = drafts.create(TOKEN, BOT)
    drafts.drop(draft.id + "x")
    assert drafts.current is draft
    drafts.drop(draft.id)
    assert drafts.current is None


async def test_the_status_call_sweeps_an_abandoned_draft(env: Env) -> None:
    app = create_app(make_context(env))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await log_in(env, client)
        await new_draft(client)
        drafts = app.state.telegram_drafts
        assert isinstance(drafts, Drafts) and drafts.current is not None
        env.now += DRAFT_TTL + 1
        assert (await client.get("/api/telegram")).status_code == 200
        assert drafts.current is None


async def test_a_new_check_sweeps_an_expired_draft_even_if_it_fails(
    env: Env, client: httpx.AsyncClient
) -> None:
    draft = await new_draft(client)
    env.now += DRAFT_TTL + 1
    bad = await client.post("/api/telegram/draft", json={"token": "nope"})
    assert bad.status_code == 422
    env.now -= DRAFT_TTL + 1  # had the draft survived, it would be usable again
    assert (await client.get(f"/api/telegram/draft/{draft}/chats")).status_code == 404


# -- status and the test notification ---------------------------------------------------


async def test_status_reads_the_slot_and_the_outbox(env: Env, client: httpx.AsyncClient) -> None:
    env.configured, env.bot_username = False, None
    env.store.kv_set(CHAT_NAME_KEY, "Alex")
    assert (await client.get("/api/telegram")).json() == {
        "configured": False,
        "bot": None,
        "chat_name": None,
        "last_ok": None,
        "last_error": None,
        "reason": "both",
        "failing": None,
    }
    env.configured, env.bot_username = True, BOT
    env.store.mark_sent(add_event(env, ["package.json"]), 1)
    env.now += 10
    env.store.mark_failed(
        add_event(env, ["Makefile"]), "sendMessage: Unauthorized", kind="auth", at=env.now
    )
    assert (await client.get("/api/telegram")).json() == {
        "configured": True,
        "bot": BOT,
        "chat_name": "Alex",
        "last_ok": 1_000_000.0,
        "last_error": "sendMessage: Unauthorized",
        "reason": None,
        "failing": {
            "kind": "auth",
            "since": 1_000_010.0,
            "failed": 1,
            "waiting": 0,
            "error": "sendMessage: Unauthorized",
        },
    }
    assert env.bot.calls == []  # no Bot API call


async def test_the_test_notification_is_409_while_not_set_up(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.configured = False
    response = await client.post("/api/test-notification")
    assert response.status_code == 409
    assert response.json() == {"error": "Telegram isn't set up yet."}
    assert env.sent == []
    env.configured = True
    env.send_error = NotSetUp()  # the slot was emptied between the check and the send
    response = await client.post("/api/test-notification")
    assert response.status_code == 409
    assert response.json() == {"error": "Telegram isn't set up yet."}


# -- no leaks -------------------------------------------------------------------------


async def test_no_response_or_log_line_carries_the_token_or_the_chat_id(
    env: Env, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env.configured, env.bot_username = False, None
    responses: list[httpx.Response] = []

    async def call(method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        response = await client.request(method, path, json=body)
        responses.append(response)
        return response

    await call("POST", "/api/telegram/draft", {"token": TOKEN + "!"})
    env.bot.errors["get_me"] = UNAUTHORIZED
    await call("POST", "/api/telegram/draft", {"token": TOKEN})
    del env.bot.errors["get_me"]
    draft = (await call("POST", "/api/telegram/draft", {"token": TOKEN})).json()["draft"]
    base = f"/api/telegram/draft/{draft}"
    await call("GET", f"{base}/chats")
    env.bot.updates = [message(CHAT, env.now + 1)]
    choice = (await call("GET", f"{base}/chats")).json()["chats"][0]["choice"]
    await call("POST", f"{base}/chats", {"chat_id": f"-{CHAT}"})
    await call("POST", f"{base}/chats", {"chat_id": f"{CHAT}x"})
    env.bot.errors["send"] = TelegramError(
        ErrorKind.BAD_REQUEST, f"sendMessage: Bad Request: chat {CHAT} not found", status=400
    )
    await call("POST", f"{base}/test", {"choice": choice})
    del env.bot.errors["send"]
    await call("POST", f"{base}/test", {"choice": choice})
    env.keychain_fail = ACCOUNT_BOT_TOKEN
    await call("POST", f"{base}/save", {"choice": choice})
    env.keychain_fail = None
    await call("POST", f"{base}/save", {"choice": choice})
    await call("GET", "/api/telegram")
    await call("GET", "/api/dashboard")
    assert [r.status_code for r in responses] == [
        422, 422, 200, 200, 200, 200, 422, 422, 200, 500, 200, 200, 200,
    ]  # fmt: skip
    for response in responses:
        assert SECRET_PART not in response.text, response.text
        assert str(CHAT) not in response.text, response.text
    assert SECRET_PART not in caplog.text
    assert str(CHAT) not in caplog.text


async def test_the_dashboard_says_whether_telegram_is_set_up(
    env: Env, client: httpx.AsyncClient
) -> None:
    """The page's setup banner rides on the 5 s dashboard poll (no extra request)."""
    assert (await client.get("/api/dashboard")).json()["telegram"]["configured"] is True
    env.configured = False
    body = (await client.get("/api/dashboard")).json()
    assert body["telegram"] == {"configured": False, "last_ok": None, "last_error": None}


# -- reason and failing (settings redesign spec §7) ----------------------------------


async def test_the_reason_is_the_daemons_cached_one_only_while_not_set_up(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.configured, env.bot_username = False, None
    env.setup_reason = "chat id"
    assert (await client.get("/api/telegram")).json()["reason"] == "chat id"
    env.configured = True
    assert (await client.get("/api/telegram")).json()["reason"] is None


async def test_a_failed_keychain_write_makes_the_reason_an_unfinished_save(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.configured, env.bot_username = False, None
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    env.keychain_fail = ACCOUNT_BOT_TOKEN
    path = f"/api/telegram/draft/{draft}/save"
    assert (await client.post(path, json={"choice": choice})).status_code == 500
    assert (await client.get("/api/telegram")).json()["reason"] == "unfinished save"
    env.keychain_fail = None
    assert (await client.post(path, json={"choice": choice})).status_code == 200
    status = (await client.get("/api/telegram")).json()
    assert (status["configured"], status["reason"]) == (True, None)
    assert env.setup_reason is None  # complete_setup cleared the cache


async def test_a_refused_value_leaves_the_reason_as_it_was(
    env: Env, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(token: str, chat_id: str, **_: Any) -> None:
        raise ValueError("bad value")

    monkeypatch.setattr(support, "save_credentials", refuse)
    env.configured, env.bot_username = False, None
    draft = await new_draft(client)
    choice = await detected(env, client, draft)
    save = await client.post(f"/api/telegram/draft/{draft}/save", json={"choice": choice})
    assert save.status_code == 422
    assert (await client.get("/api/telegram")).json()["reason"] == "both"


async def test_a_cancelled_saves_failed_write_also_marks_an_unfinished_save(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()

    def failing(token: str, chat_id: str, **_: Any) -> None:
        started.set()
        release.wait(10)
        raise SecretStoreError("keychain write failed for 'bot-token' (exit 1)")

    monkeypatch.setattr(support, "save_credentials", failing)
    env.configured, env.bot_username = False, None
    app = create_app(make_context(env))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await log_in(env, client)
        drafts = app.state.telegram_drafts
        draft = await new_draft(client)
        choice = await detected(env, client, draft)
        path = f"/api/telegram/draft/{draft}/save"
        try:
            request = asyncio.create_task(client.post(path, json={"choice": choice}))
            assert await asyncio.to_thread(started.wait, 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        finally:
            release.set()
        for _ in range(200):
            if not drafts.saving:
                break
            await asyncio.sleep(0.005)
        assert drafts.saving is False
        assert (await client.get("/api/telegram")).json()["reason"] == "unfinished save"


async def test_the_status_never_reads_the_keychain(
    env: Env, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_keychain(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("the status endpoint ran a Keychain command")

    monkeypatch.setattr(secrets_mod, "_run", no_keychain)
    env.configured, env.bot_username = False, None
    response = await client.get("/api/telegram")
    assert response.status_code == 200 and response.json()["reason"] == "both"


async def test_failing_names_the_kind_the_backlog_and_its_counts(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.store.mark_sent(add_event(env, ["package.json"]), 1)
    env.now += 10
    revoked = add_event(env, ["Makefile"])
    env.store.mark_failed(revoked, "sendMessage: Unauthorized", kind="auth", at=env.now)
    env.now += 10
    retrying = add_event(env, ["go.mod"])
    env.store.mark_retry(retrying, "sendMessage: 502", env.now + 30, kind="retryable", at=env.now)
    assert (await client.get("/api/telegram")).json()["failing"] == {
        "kind": "auth",
        "since": 1_000_010.0,
        "failed": 1,
        "waiting": 1,
        "error": "sendMessage: Unauthorized",
    }
    env.now += 10
    env.store.mark_sent(add_event(env, ["README.md"]), 2)
    assert (await client.get("/api/telegram")).json()["failing"] is None


async def test_a_legacy_failure_with_a_newer_retry_never_says_keeps_retrying(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.store.mark_failed(
        add_event(env, ["Makefile"]), "sendMessage: Bad Request", kind=None, at=env.now
    )
    env.now += 10
    retrying = add_event(env, ["go.mod"])
    env.store.mark_retry(retrying, "sendMessage: 502", env.now + 30, kind="retryable", at=env.now)
    failing = (await client.get("/api/telegram")).json()["failing"]
    assert (failing["kind"], failing["error"], failing["failed"], failing["waiting"]) == (
        "legacy_failed",
        "sendMessage: Bad Request",
        1,
        1,
    )


async def test_failing_after_a_bot_save_counts_only_newer_errors(
    env: Env, client: httpx.AsyncClient
) -> None:
    revoked = add_event(env, ["Makefile"])
    env.store.mark_failed(revoked, "sendMessage: Unauthorized", kind="auth", at=env.now)
    env.store.kv_set(TELEGRAM_SAVED_AT, repr(env.now + 5))  # Replace bot
    assert (await client.get("/api/telegram")).json()["failing"] is None
    env.now += 10
    retrying = add_event(env, ["go.mod"])
    env.store.mark_retry(retrying, "sendMessage: 502", env.now + 30, kind="retryable", at=env.now)
    failing = (await client.get("/api/telegram")).json()["failing"]
    assert (failing["kind"], failing["failed"], failing["waiting"]) == ("retryable", 0, 1)
