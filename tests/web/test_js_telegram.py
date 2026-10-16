"""Settings → Telegram under node (bot onboarding spec §3, settings redesign spec §6):
the pure wizard state, the card's rows, the token round trip, the wait for /start and
the card's Send test message."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.web.node import needs_node, run_js
from tests.web.test_js_main import TIMERS

pytestmark = needs_node

NOW = 1_791_547_200_000
T = NOW // 1000

SETUP = (
    TIMERS
    + f"""
const dom = await import(js("dom.js"));
const {{ state }} = await import(js("state.js"));
const api = await import(js("api.js"));
const tg = await import(js("telegram.js"));
const card = await import(js("telegram-card.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const net = fakeFetch();
api.configureApi({{ fetch: net.fetch }});
let visible = true;
const views = [];
let refreshes = 0;
tg.initTelegram({{
  visible: () => visible,
  showView: (view, options) => views.push([view, options]),
  refresh: () => {{ refreshes += 1; }},
  now: () => {NOW},
}});
const $ = (id) => doc.getElementById(id);
const submit = () => ({{ preventDefault() {{}} }});
const flush = async () => {{ for (let i = 0; i < 5; i += 1) await settle(); }};
const shown = () => ["tg-loading", "tg-state", "tg-wizard"].filter((id) => !$(id).hidden);
"""
)

NOT_SET_UP = {
    "configured": False,
    "bot": None,
    "chat_name": None,
    "last_ok": None,
    "last_error": None,
    "reason": "both",
    "failing": None,
}
CONNECTED = {
    "configured": True,
    "bot": "my_bot",
    "chat_name": "Alex",
    "last_ok": T - 120,
    "last_error": None,
    "reason": None,
    "failing": None,
}
# T is Fri 9 Oct 2026 12:00 UTC (the tests run with TZ=UTC).
AUTH = {
    "kind": "auth",
    "since": T - 3600,
    "failed": 3,
    "waiting": 0,
    "error": "Telegram: unauthorized",
}
RETRYABLE = {
    "kind": "retryable",
    "since": T - 86400,
    "failed": 0,
    "waiting": 2,
    "error": "Telegram: timed out",
}
BAD_REQUEST = {
    "kind": "bad_request",
    "since": T - 3600,
    "failed": 1,
    "waiting": 0,
    "error": "Telegram: Bad Request: message is too long",
}
LEGACY = {
    "kind": "legacy_failed",
    "since": T - 3600,
    "failed": 2,
    "waiting": 0,
    "error": "HTTP 500",
}


def run(script: str) -> Any:
    return run_js(SETUP + script)


def step(status: dict[str, Any] | None, draft: dict[str, Any]) -> Any:
    wizard: dict[str, Any] = {
        "open": False,
        "draft": None,
        "bot": "",
        "chats": [],
        "choice": None,
        "tested": None,
    }
    wizard.update(draft)
    return run(f"done(tg.wizardStep({json.dumps(status)}, {json.dumps(wizard)}));")


# -- wizardStep (pure) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "draft", "expected"),
    [
        (None, {}, {"card": "loading", "step": 0, "canTest": False, "canSave": False}),
        (CONNECTED, {}, {"card": "connected", "step": 0, "canTest": False, "canSave": False}),
        (
            {**CONNECTED, "failing": AUTH},
            {},
            {"card": "failing", "step": 0, "canTest": False, "canSave": False},
        ),
        (
            {**NOT_SET_UP, "reason": "unfinished save"},
            {},
            {"card": "unfinished", "step": 0, "canTest": False, "canSave": False},
        ),
        # A failed save keeps its draft: the wizard stays, the token isn't re-entered.
        (
            {**NOT_SET_UP, "reason": "unfinished save"},
            {"draft": "d1", "choice": "c1", "tested": "c1"},
            {"card": "wizard", "step": 4, "canTest": True, "canSave": True},
        ),
        (
            {**NOT_SET_UP, "reason": "unfinished save"},
            {"draft": "d1"},
            {"card": "wizard", "step": 3, "canTest": False, "canSave": False},
        ),
        # Set up again: the wizard over the notice.
        (
            {**NOT_SET_UP, "reason": "unfinished save"},
            {"open": True},
            {"card": "wizard", "step": 2, "canTest": False, "canSave": False},
        ),
        (NOT_SET_UP, {}, {"card": "wizard", "step": 2, "canTest": False, "canSave": False}),
        # Replace bot: the wizard over a Connected card.
        (
            CONNECTED,
            {"open": True},
            {"card": "wizard", "step": 2, "canTest": False, "canSave": False},
        ),
        (
            NOT_SET_UP,
            {"draft": "d1"},
            {"card": "wizard", "step": 3, "canTest": False, "canSave": False},
        ),
        (
            NOT_SET_UP,
            {"draft": "d1", "choice": "c1"},
            {"card": "wizard", "step": 4, "canTest": True, "canSave": False},
        ),
        (
            NOT_SET_UP,
            {"draft": "d1", "choice": "c1", "tested": "c1"},
            {"card": "wizard", "step": 4, "canTest": True, "canSave": True},
        ),
        # A test to another chat doesn't unlock Save.
        (
            NOT_SET_UP,
            {"draft": "d1", "choice": "c2", "tested": "c1"},
            {"card": "wizard", "step": 4, "canTest": True, "canSave": False},
        ),
    ],
)
def test_wizard_step(
    status: dict[str, Any] | None, draft: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert step(status, draft) == expected


# -- cardState (pure, settings redesign spec §6) ---------------------------------------

CARD: dict[str, Any] = {
    "row": "",
    "tone": "",
    "icon": "",
    "headline": [],
    "detail": "",
    "error": "",
    "actions": [],
    "primary": None,
    "reason": "",
    "held": "",
}


def card(status: dict[str, Any], counts: dict[str, Any] | None = None) -> Any:
    return run(f"done(card.cardState({json.dumps(status)}, {json.dumps(counts)}, {NOW}));")


@pytest.mark.parametrize(
    ("status", "counts", "expected"),
    [
        # 1: connected
        (
            CONNECTED,
            {"held": 0},
            {
                "row": "connected",
                "tone": "tg-ok",
                "icon": "sent",
                "headline": [
                    ["Notifications go to ", False],
                    ["Alex", True],
                    [" via ", False],
                    ["@my_bot", True],
                ],
                "detail": "Last sent 2 min ago",
                "actions": ["test", "replace"],
            },
        ),
        # 2: connected, the chat's name unknown: never a chat id in its place
        (
            {**CONNECTED, "chat_name": None, "last_ok": None},
            None,
            {
                "row": "connected-noname",
                "tone": "tg-ok",
                "icon": "sent",
                "headline": [["Notifications go to ", False], ["@my_bot", True]],
                "detail": "Nothing sent yet",
                "actions": ["test", "replace"],
            },
        ),
        # 3: token rejected
        (
            {**CONNECTED, "failing": AUTH},
            None,
            {
                "row": "auth",
                "tone": "tg-bad",
                "icon": "failed",
                "headline": [["Telegram rejected the bot token", True]],
                "detail": "Since 11:00 · 3 changes couldn't be sent. "
                "The token was probably revoked in @BotFather.",
                "actions": ["replace", "failed"],
                "primary": "replace",
            },
        ),
        # 4: can't reach Telegram
        (
            {**CONNECTED, "failing": RETRYABLE},
            None,
            {
                "row": "retryable",
                "tone": "tg-warn",
                "icon": "retry",
                "headline": [["Can't reach Telegram", True]],
                "detail": "Since yesterday 12:00 · 2 changes waiting. "
                "Diffbell keeps retrying on its own; nothing is lost.",
                "actions": ["test", "replace"],
            },
        ),
        # 4b: a message refused, with the stored error text
        (
            {**CONNECTED, "failing": BAD_REQUEST},
            None,
            {
                "row": "bad_request",
                "tone": "tg-bad",
                "icon": "failed",
                "headline": [["Telegram refused a message", True]],
                "detail": "Since 11:00 · 1 change couldn't be sent.",
                "error": "Telegram: Bad Request: message is too long",
                "actions": ["failed", "test", "replace"],
            },
        ),
        # 4b, legacy: a failed row from before v7 (no kind)
        (
            {**CONNECTED, "failing": LEGACY},
            None,
            {
                "row": "legacy_failed",
                "tone": "tg-bad",
                "icon": "failed",
                "headline": [["Some changes couldn't be sent", True]],
                "detail": "Since 11:00 · 2 changes couldn't be sent.",
                "error": "HTTP 500",
                "actions": ["failed", "test", "replace"],
            },
        ),
        # 5: not set up, both missing: the held line only
        (
            NOT_SET_UP,
            {"held": 2},
            {
                "row": "not_set_up",
                "held": "2 changes are waiting and will arrive as one message per project.",
            },
        ),
        # 5: one item missing names it; one held change is singular
        (
            {**NOT_SET_UP, "reason": "bot token"},
            {"held": 1},
            {
                "row": "not_set_up",
                "reason": "Telegram not set up: the bot token is missing",
                "held": "1 change is waiting and will arrive as one message per project.",
            },
        ),
        # 5: nothing held hides the line
        (
            {**NOT_SET_UP, "reason": "chat id"},
            {"held": 0},
            {"row": "not_set_up", "reason": "Telegram not set up: the chat id is missing"},
        ),
        # 6: an earlier save didn't finish
        (
            {**NOT_SET_UP, "reason": "unfinished save"},
            {"held": 3},
            {
                "row": "unfinished",
                "tone": "tg-notice",
                "icon": "warning",
                "headline": [["An earlier save didn't finish", True]],
                "detail": "Diffbell won't trust a half-saved bot. "
                "Changes are held until you set Telegram up again.",
                "actions": ["setup"],
                "primary": "setup",
                "held": "3 changes are waiting and will arrive as one message per project.",
            },
        ),
    ],
)
def test_card_state(
    status: dict[str, Any], counts: dict[str, Any] | None, expected: dict[str, Any]
) -> None:
    assert card(status, counts) == {**CARD, **expected}


@pytest.mark.parametrize(
    ("failing", "detail", "actions"),
    [
        # One change is singular; 0 drops the count clause (and the link to an empty feed).
        (
            {**AUTH, "failed": 1},
            "Since 11:00 · 1 change couldn't be sent. "
            "The token was probably revoked in @BotFather.",
            ["replace", "failed"],
        ),
        (
            {**AUTH, "failed": 0},
            "Since 11:00. The token was probably revoked in @BotFather.",
            ["replace"],
        ),
        (
            {**RETRYABLE, "waiting": 1},
            "Since yesterday 12:00 · 1 change waiting. "
            "Diffbell keeps retrying on its own; nothing is lost.",
            ["test", "replace"],
        ),
        (
            {**RETRYABLE, "waiting": 0, "since": T - 3 * 86400},
            "Since Tue 6 Oct 12:00. Diffbell keeps retrying on its own; nothing is lost.",
            ["test", "replace"],
        ),
        ({**BAD_REQUEST, "failed": 0}, "Since 11:00.", ["test", "replace"]),
        # No backlog start known: no "Since" either.
        ({**BAD_REQUEST, "since": None, "failed": 0}, "", ["test", "replace"]),
    ],
)
def test_card_state_counts(failing: dict[str, Any], detail: str, actions: list[str]) -> None:
    result = card({**CONNECTED, "failing": failing})
    assert [result["detail"], result["actions"]] == [detail, actions]


def test_an_unknown_kind_reads_as_the_generic_failure() -> None:
    result = card({**CONNECTED, "failing": {**LEGACY, "kind": "something_new"}})
    assert result["row"] == "legacy_failed"
    assert result["headline"] == [["Some changes couldn't be sent", True]]


def test_the_name_fallback_never_shows_an_id() -> None:
    """The page never receives the chat id; without a name the card names only the bot."""
    result = card({**CONNECTED, "chat_name": None, "bot": None})
    assert result["headline"] == [["Notifications go to ", False], ["your bot", True]]


def test_every_row_renders_without_a_bot_username() -> None:
    """With Telegram unreachable the background getMe fails too, so `bot` can be null
    in any state: "via @…" is left out, and nothing else stands in for it."""
    no_bot = {**CONNECTED, "bot": None}
    assert card(no_bot)["headline"] == [["Notifications go to ", False], ["Alex", True]]
    for failing in (AUTH, RETRYABLE, BAD_REQUEST, LEGACY):
        result = card({**no_bot, "failing": failing})
        assert result["row"] == failing["kind"]
        assert "@" not in json.dumps(result["headline"]) and "None" not in result["detail"]
    assert card({**no_bot, "failing": RETRYABLE})["detail"] == (
        "Since yesterday 12:00 · 2 changes waiting. "
        "Diffbell keeps retrying on its own; nothing is lost."
    )


# -- the card ------------------------------------------------------------------------


def test_settings_loads_the_status() -> None:
    """Opening Settings refreshes the card. Scrolling to ?section= and focusing its
    heading is settings.focusSection's job (test_js_settings.py)."""
    script = f"""
state.section = "telegram";
tg.showTelegram();
net.reply(200, {json.dumps(NOT_SET_UP)});
await flush();
done([net.calls.map((c) => c.path), $("tg-card").scrolled, shown(), $("tg-step-2").className]);
"""
    assert run(script) == [["/api/telegram"], 0, ["tg-wizard"], "wizard-step current"]


def test_check_sends_the_token_once_and_clears_the_field() -> None:
    script = f"""
state.telegram = {json.dumps(NOT_SET_UP)};
$("tg-token").value = "  123:secret-token  ";
const checking = tg.checkToken(submit());
net.reply(200, {{ draft: "d1", bot: "my_bot" }});
await checking;
await flush();
done([
  net.calls.map((c) => [c.method, c.path, c.body]),
  $("tg-token").value,
  JSON.stringify(state.wizard).includes("secret-token"),
  state.wizard.draft,
  $("tg-bot-name").textContent,
  $("tg-step-3").className,
  $("tg-find").hidden,
  $("tg-waiting").textContent,
]);
"""
    assert run(script) == [
        [
            ["POST", "/api/telegram/draft", {"token": "123:secret-token"}],
            ["GET", "/api/telegram/draft/d1/chats", None],
        ],
        "",
        False,
        "d1",
        "@my_bot",
        "wizard-step current",
        False,
        "Waiting for your /start…",
    ]


def test_a_rejected_token_stays_in_the_field_with_the_reason() -> None:
    script = f"""
state.telegram = {json.dumps(NOT_SET_UP)};
$("tg-token").value = "123:bad";
const checking = tg.checkToken(submit());
net.reply(422, {{ error: "Telegram rejected this token." }});
await checking;
done([$("tg-token").value, $("tg-message").textContent, state.wizard.draft, net.calls.length]);
"""
    assert run(script) == ["123:bad", "Telegram rejected this token.", None, 1]


CHECKED = f"""
state.telegram = {json.dumps(NOT_SET_UP)};
$("tg-token").value = "123:secret-token";
const checking = tg.checkToken(submit());
net.reply(200, {{ draft: "d1", bot: "my_bot" }});
await checking;
await flush();
"""


def test_the_chat_list_is_polled_every_3_s_and_the_first_chat_preselected() -> None:
    script = (
        CHECKED
        + """
net.reply(200, { chats: [] });
await flush();
const first = live().map((t) => t.ms);
await fire(live()[0]);
net.reply(200, { chats: [{ choice: "c1", name: "Alex" }, { choice: "c2", name: "Ada" }] });
await flush();
const radios = $("tg-chat-list").children;
done([
  first,
  net.calls.length,
  state.wizard.choice,
  [...radios].map((r) => [r.textContent, r.children[0].checked]),
  live().length,
  $("tg-step-4").className,
  $("tg-test").disabled,
  $("tg-save").disabled,
  $("tg-waiting").textContent,
]);
"""
    )
    assert run(script) == [
        [3000],
        3,
        "c1",
        [["Alex", True], ["Ada", False]],
        0,  # found: the polling stops
        "wizard-step current",
        False,
        True,  # Save waits for a test
        "More than one person messaged the bot: pick yourself.",
    ]


def test_after_2_minutes_it_asks_to_send_start_again_and_offers_the_chat_id() -> None:
    script = (
        CHECKED
        + """
net.reply(200, { chats: [] });
await flush();
for (let i = 1; i < tg.MAX_POLLS; i += 1) {
  const timer = live()[0];
  net.reply(200, { chats: [] });
  await fire(timer);
}
done([tg.MAX_POLLS * tg.POLL_MS, net.calls.length, live().length, $("tg-manual").hidden, state.wizard.polling]);
"""
    )
    assert run(script) == [120000, 41, 0, False, False]


def test_check_again_restarts_the_wait() -> None:
    script = (
        CHECKED
        + """
net.reply(200, { chats: [] });
await flush();
state.wizard.polling = false;  // as after MAX_POLLS
state.wizard.manual = true;
tg.startPolling();
net.reply(200, { chats: [{ choice: "c1", name: "Alex" }] });
await flush();
done([state.wizard.choice, net.calls.length, state.wizard.manual]);
"""
    )
    assert run(script) == ["c1", 3, True]


def test_a_hidden_page_pauses_the_wait_until_it_is_visible() -> None:
    script = (
        CHECKED
        + """
net.reply(200, { chats: [] });
await flush();
visible = false;
await fire(live()[0]);
const paused = [net.calls.length, live().length];
visible = true;
tg.resumeTelegramPoll();
net.reply(200, { chats: [] });
await flush();
done([paused, net.calls.length, live().length]);
"""
    )
    assert run(script) == [[2, 0], 3, 1]


def test_a_webhook_switches_to_the_chat_id_field() -> None:
    script = (
        CHECKED
        + """
net.reply(409, { error: "This bot has a webhook; enter the chat id instead." });
await flush();
$("tg-chat-id").value = " -100 123 ";
const using = tg.useManualChat(submit());
net.reply(200, { choice: "m1", name: null });
await using;
done([
  $("tg-manual").hidden,
  live().length,
  net.calls.at(-1).body,
  $("tg-chat-id").value,
  state.wizard.choice,
  $("tg-chat-list").children[0].textContent,
  $("tg-test").disabled,
]);
"""
    )
    assert run(script) == [
        False,
        0,
        {"chat_id": " -100 123 "},
        "",
        "m1",
        "The chat id you entered",
        False,
    ]


def test_an_expired_draft_goes_back_to_the_token() -> None:
    script = (
        CHECKED
        + """
net.reply(404, { error: "This setup expired or was restarted in another tab; check the token again." });
await flush();
done([state.wizard.draft, $("tg-step-2").className, $("tg-message").textContent, live().length]);
"""
    )
    assert run(script) == [
        None,
        "wizard-step current",
        "This setup expired or was restarted in another tab; check the token again.",
        0,
    ]


FOUND = (
    CHECKED
    + """
net.reply(200, { chats: [{ choice: "c1", name: "Alex" }] });
await flush();
"""
)


def test_save_needs_a_test_to_the_picked_chat() -> None:
    script = (
        FOUND
        + """
const testing = tg.testDraft();
net.reply(200, { ok: true });
await testing;
const tested = [net.calls.at(-1).path, net.calls.at(-1).body, $("tg-save").disabled];
tg.chooseChat("c2");
done([tested, $("tg-save").disabled]);
"""
    )
    assert run(script) == [["/api/telegram/draft/d1/test", {"choice": "c1"}, False], True]


def test_save_switches_the_card_to_connected() -> None:
    script = (
        FOUND
        + f"""
const testing = tg.testDraft();
net.reply(200, {{ ok: true }});
await testing;
const saving = tg.saveWizard();
net.reply(200, {{ bot: "my_bot" }});
net.reply(200, {json.dumps(CONNECTED)});
await saving;
done([
  net.calls.slice(-2).map((c) => [c.method, c.path, c.body]),
  shown(),
  $("tg-message").textContent,
  state.wizard.draft,
  refreshes,
]);
"""
    )
    assert run(script) == [
        [["POST", "/api/telegram/draft/d1/save", {"choice": "c1"}], ["GET", "/api/telegram", None]],
        ["tg-state"],
        "Saved: Diffbell now sends to @my_bot.",
        None,
        1,
    ]


def test_a_failed_save_keeps_the_draft_for_another_try() -> None:
    script = (
        FOUND
        + """
state.wizard.tested = "c1";
const saving = tg.saveWizard();
net.reply(500, { error: "Saving to the Keychain failed; try Save again." });
await saving;
done([state.wizard.draft, $("tg-save").disabled, $("tg-message").textContent]);
"""
    )
    assert run(script) == [
        "d1",
        False,
        "Saving to the Keychain failed; try Save again.",
    ]


def test_replace_bot_opens_the_wizard_and_cancel_closes_it() -> None:
    script = f"""
state.telegram = {json.dumps(CONNECTED)};
tg.renderTelegram();
tg.replaceBot();
const open = [shown(), $("tg-cancel").hidden, doc.activeElement === $("tg-token")];
tg.cancelWizard();
done([open, shown()]);
"""
    assert run(script) == [[["tg-wizard"], False, True], ["tg-state"]]


def test_how_links_open_the_guide_at_their_step() -> None:
    script = """
tg.wireTelegram();
for (const [id, step] of [["tg-how-create", "create"], ["tg-how-token", "token"], ["tg-how-start", "start"]]) {
  $(id).dataset.step = step;
}
const event = $("tg-how-token").dispatch("click");
done([event.defaultPrevented, views]);
"""
    assert run(script) == [True, [["guide", {"step": "token"}]]]


# -- the card's Send test message ---------------------------------------------------------


def test_the_card_test_button_is_disabled_while_not_set_up() -> None:
    script = """
const states = [];
const look = () => states.push($("tg-send-test").disabled);
tg.syncTestButton();
look();  // nothing known yet
state.dash = { telegram: { configured: false } };
tg.syncTestButton();
look();
state.dash = { telegram: { configured: true } };
tg.syncTestButton();
look();
state.dash = { telegram: {} };  // an older daemon: the card's status decides
state.telegram = { configured: false };
tg.syncTestButton();
look();
done(states);
"""
    assert run(script) == [False, True, False, True]


def test_send_test_message_posts_the_sample_and_reports_in_the_card() -> None:
    """The defined "try now" for state 4 (spec §6): POST /api/test-notification."""
    script = f"""
tg.wireTelegram();
state.telegram = {json.dumps({**CONNECTED, "failing": RETRYABLE})};
tg.renderTelegram();
$("tg-send-test").dispatch("click");
const busy = $("tg-send-test").disabled;
net.reply(200, {{ ok: true }});
await flush();
done([net.calls.map((c) => [c.method, c.path]), busy, $("tg-send-test").disabled, $("tg-message").textContent]);
"""
    assert run(script) == [
        [["POST", "/api/test-notification"]],
        True,
        False,
        "Sample notification sent.",
    ]


# -- review fixes: stale replies, focus, wiring ----------------------------------------


def test_the_initial_wizard_is_a_fresh_one() -> None:
    script = "done([state.wizard, tg.freshWizard(false)]);"
    first, fresh = run(script)
    assert first == fresh


def test_a_stale_poll_in_flight_does_not_stall_the_next_draft() -> None:
    # The first draft's …/chats request is held; Check makes a second draft meanwhile.
    script = (
        """
const held = [];
api.configureApi({
  fetch: (path, options) =>
    path.endsWith("/d1/chats") ? new Promise((resolve) => held.push(resolve)) : net.fetch(path, options),
});
"""
        + CHECKED
        + """
$("tg-token").value = "123:other-token";
const again = tg.checkToken(submit());
net.reply(200, { draft: "d2", bot: "other_bot" });
await again;
await flush();
held[0]({ ok: true, status: 200, json: async () => ({ chats: [] }) });
await flush();
net.reply(200, { chats: [] });
await flush();
done([held.length, state.wizard.draft, net.calls.map((c) => c.path), live().length, state.wizard.polling]);
"""
    )
    assert run(script) == [
        1,
        "d2",
        ["/api/telegram/draft", "/api/telegram/draft", "/api/telegram/draft/d2/chats"],
        1,
        True,
    ]


def test_check_again_while_a_poll_is_in_flight_keeps_one_poll() -> None:
    script = (
        CHECKED
        + """
tg.startPolling();  // Check again while the first request still waits
net.reply(200, { chats: [] });
await flush();
done([net.calls.length, live().length]);
"""
    )
    assert run(script) == [2, 1]


STALE_ACTIONS = {
    "test": ("tg.testDraft()", ""),
    "save": ('(state.wizard.tested = "c1", tg.saveWizard())', ""),
    "manual": ('($("tg-chat-id").value = "42", tg.useManualChat(submit()))', ""),
}


@pytest.mark.parametrize("action", ["test", "save", "manual"])
@pytest.mark.parametrize("leave", ["replaceBot", "cancelWizard"])
def test_a_late_404_leaves_the_new_wizard_alone(action: str, leave: str) -> None:
    script = (
        FOUND
        + f"""
const pending = {STALE_ACTIONS[action][0]};
tg.{leave}();
const fresh = state.wizard;
net.reply(404, {{ error: "This setup expired or was restarted in another tab; check the token again." }});
await pending;
await flush();
done([state.wizard === fresh, state.wizard.draft, $("tg-message").textContent]);
"""
    )
    assert run(script) == [True, None, ""]


@pytest.mark.parametrize("action", ["test", "manual"])
def test_a_late_success_leaves_the_new_wizard_alone(action: str) -> None:
    script = (
        FOUND
        + f"""
const pending = {STALE_ACTIONS[action][0]};
tg.replaceBot();
const fresh = state.wizard;
net.reply(200, {{ ok: true, choice: "m1", name: null }});
await pending;
await flush();
done([state.wizard === fresh, state.wizard.draft, state.wizard.tested, state.wizard.chats, $("tg-message").textContent]);
"""
    )
    assert run(script) == [True, None, None, [], ""]


def test_a_late_save_reloads_the_status_but_keeps_the_new_wizard() -> None:
    script = (
        FOUND
        + f"""
state.wizard.tested = "c1";
const pending = tg.saveWizard();
tg.replaceBot();
const fresh = state.wizard;
net.reply(200, {{ bot: "my_bot" }});
net.reply(200, {json.dumps(CONNECTED)});
await pending;
await flush();
done([state.wizard === fresh, state.wizard.open, shown(), net.calls.at(-1).path, refreshes]);
"""
    )
    assert run(script) == [True, True, ["tg-wizard"], "/api/telegram", 1]


def test_save_and_cancel_move_focus_to_the_card_heading() -> None:
    script = (
        FOUND
        + f"""
const testing = tg.testDraft();
net.reply(200, {{ ok: true }});
await testing;
const saving = tg.saveWizard();
net.reply(200, {{ bot: "my_bot" }});
net.reply(200, {json.dumps(CONNECTED)});
await saving;
const afterSave = doc.activeElement === $("tg-title");
tg.replaceBot();
const inWizard = doc.activeElement === $("tg-token");
tg.cancelWizard();
done([afterSave, inWizard, doc.activeElement === $("tg-title")]);
"""
    )
    assert run(script) == [True, True, True]


def test_use_this_chat_is_disabled_while_its_request_runs() -> None:
    script = (
        FOUND
        + """
$("tg-chat-id").value = "42";
const using = tg.useManualChat(submit());
const during = $("tg-use").disabled;
net.reply(200, { choice: "m1", name: null });
await using;
done([during, $("tg-use").disabled]);
"""
    )
    assert run(script) == [True, False]


def test_the_current_step_is_marked_for_assistive_technology() -> None:
    script = (
        CHECKED
        + """
done(["tg-step-1", "tg-step-2", "tg-step-3", "tg-step-4"].map((id) => $(id).getAttribute("aria-current")));
"""
    )
    assert run(script) == [None, None, "step", None]


def test_a_failed_reload_after_save_still_shows_the_saved_bot() -> None:
    script = (
        FOUND
        + """
state.wizard.tested = "c1";
const saving = tg.saveWizard();
net.reply(200, { bot: "my_bot" });
net.reply(500, { error: "boom" });
await saving;
done([shown(), $("tg-headline").textContent, $("tg-message").textContent]);
"""
    )
    assert run(script) == [
        ["tg-state"],
        "Notifications go to Alex via @my_bot",
        "Saved: Diffbell now sends to @my_bot.",
    ]


def test_the_wired_controls_drive_the_wizard() -> None:
    script = f"""
tg.wireTelegram();
state.telegram = {json.dumps(CONNECTED)};
tg.renderTelegram();
$("tg-replace").dispatch("click");
const opened = shown();
$("tg-token").value = "123:secret-token";
$("tg-token-form").dispatch("submit");
net.reply(200, {{ draft: "d1", bot: "my_bot" }});
await flush();
net.reply(200, {{ chats: [] }});
await flush();
state.wizard.polling = false;
state.wizard.manual = true;
$("tg-again").dispatch("click");
net.reply(200, {{ chats: [{{ choice: "c1", name: "Alex" }}] }});
await flush();
$("tg-test").dispatch("click");
net.reply(200, {{ ok: true }});
await flush();
const tested = $("tg-save").disabled;
$("tg-save").dispatch("click");
net.reply(200, {{ bot: "my_bot" }});
net.reply(200, {json.dumps(CONNECTED)});
await flush();
const saved = shown();
$("tg-replace").dispatch("click");
$("tg-cancel").dispatch("click");
done([
  opened,
  net.calls.map((c) => [c.method, c.path]),
  tested,
  saved,
  shown(),
]);
"""
    assert run(script) == [
        ["tg-wizard"],
        [
            ["POST", "/api/telegram/draft"],
            ["GET", "/api/telegram/draft/d1/chats"],
            ["GET", "/api/telegram/draft/d1/chats"],
            ["POST", "/api/telegram/draft/d1/test"],
            ["POST", "/api/telegram/draft/d1/save"],
            ["GET", "/api/telegram"],
        ],
        False,
        ["tg-state"],
        ["tg-state"],
    ]


def test_returning_to_settings_resumes_a_paused_wait() -> None:
    # main.js's `visible` is false while another view is shown.
    script = (
        CHECKED
        + f"""
net.reply(200, {{ chats: [] }});
await flush();
visible = false;
await fire(live()[0]);
const paused = net.calls.length;
visible = true;
tg.showTelegram();
net.reply(200, {json.dumps(NOT_SET_UP)});
net.reply(200, {{ chats: [] }});
await flush();
done([paused, net.calls.slice(paused).map((c) => c.path), live().length]);
"""
    )
    assert run(script) == [2, ["/api/telegram", "/api/telegram/draft/d1/chats"], 1]


# -- the card's rows, rendered (settings redesign spec §6) -----------------------------

LOOK = """
const look = () => ({
  shown: shown(),
  status: $("tg-status").className,
  icon: $("tg-badge").children.length ? $("tg-badge").children[0].children[0].getAttribute("href") : null,
  headline: $("tg-headline").textContent,
  strong: [...$("tg-headline").children].map((c) => c.textContent),
  detail: $("tg-detail").hidden ? null : $("tg-detail").textContent,
  error: $("tg-error").hidden ? null : $("tg-error").textContent,
  actions: [...$("tg-actions").children].filter((c) => !c.hidden).map((c) => [c.id, c.className]),
  reason: $("tg-reason").hidden ? null : $("tg-reason").textContent,
  held: $("tg-held").hidden ? null : $("tg-held").textContent,
});
"""


def render(status: dict[str, Any], held: int = 0) -> Any:
    return run(
        LOOK
        + f"""
state.dash = {{ counts: {{ held: {held} }} }};
state.telegram = {json.dumps(status)};
tg.renderTelegram();
done(look());
"""
    )


ICONS = "/static/icons.svg#"


def test_row_connected() -> None:
    assert render(CONNECTED) == {
        "shown": ["tg-state"],
        "status": "tg-status tg-ok",
        "icon": ICONS + "sent",
        "headline": "Notifications go to Alex via @my_bot",
        "strong": ["Alex", "@my_bot"],
        "detail": "Last sent 2 min ago",
        "error": None,
        "actions": [["tg-send-test", ""], ["tg-replace", ""]],
        "reason": None,
        "held": None,
    }


def test_row_connected_without_a_chat_name() -> None:
    result = render({**CONNECTED, "chat_name": None})
    assert [result["headline"], result["strong"], result["actions"]] == [
        "Notifications go to @my_bot",
        ["@my_bot"],
        [["tg-send-test", ""], ["tg-replace", ""]],
    ]


def test_rows_render_without_a_bot_username() -> None:
    connected = render({**CONNECTED, "bot": None})
    assert [connected["headline"], connected["strong"]] == ["Notifications go to Alex", ["Alex"]]
    retrying = render({**CONNECTED, "bot": None, "failing": RETRYABLE})
    assert [retrying["headline"], retrying["actions"]] == [
        "Can't reach Telegram",
        [["tg-send-test", ""], ["tg-replace", ""]],
    ]


def test_row_token_rejected() -> None:
    assert render({**CONNECTED, "failing": AUTH}) == {
        "shown": ["tg-state"],
        "status": "tg-status tg-bad",
        "icon": ICONS + "failed",
        "headline": "Telegram rejected the bot token",
        "strong": ["Telegram rejected the bot token"],
        "detail": "Since 11:00 · 3 changes couldn't be sent. "
        "The token was probably revoked in @BotFather.",
        "error": None,
        "actions": [["tg-replace", "primary"], ["tg-show-failed", "tg-link"]],
        "reason": None,
        "held": None,
    }


def test_row_cant_reach_telegram() -> None:
    result = render({**CONNECTED, "failing": RETRYABLE})
    assert [result["status"], result["icon"], result["headline"], result["error"]] == [
        "tg-status tg-warn",
        ICONS + "retry",
        "Can't reach Telegram",
        None,
    ]
    assert result["actions"] == [["tg-send-test", ""], ["tg-replace", ""]]


def test_row_message_refused_shows_the_stored_error() -> None:
    result = render({**CONNECTED, "failing": BAD_REQUEST})
    assert [result["headline"], result["detail"], result["error"], result["actions"]] == [
        "Telegram refused a message",
        "Since 11:00 · 1 change couldn't be sent.",
        "Telegram: Bad Request: message is too long",
        [["tg-show-failed", "tg-link"], ["tg-send-test", ""], ["tg-replace", ""]],
    ]


def test_row_legacy_failed() -> None:
    result = render({**CONNECTED, "failing": LEGACY})
    assert [result["headline"], result["error"], result["actions"]] == [
        "Some changes couldn't be sent",
        "HTTP 500",
        [["tg-show-failed", "tg-link"], ["tg-send-test", ""], ["tg-replace", ""]],
    ]


def test_row_not_set_up_shows_the_wizard_with_the_reason_and_held_lines() -> None:
    result = render({**NOT_SET_UP, "reason": "chat id"}, held=2)
    assert [result["shown"], result["reason"], result["held"]] == [
        ["tg-wizard"],
        "Telegram not set up: the chat id is missing",
        "2 changes are waiting and will arrive as one message per project.",
    ]
    nothing = render(NOT_SET_UP, held=0)
    assert [nothing["shown"], nothing["reason"], nothing["held"]] == [["tg-wizard"], None, None]


def test_row_unfinished_save_and_set_up_again() -> None:
    script = (
        LOOK
        + f"""
tg.wireTelegram();
state.dash = {{ counts: {{ held: 3 }} }};
state.telegram = {json.dumps({**NOT_SET_UP, "reason": "unfinished save"})};
tg.renderTelegram();
const notice = look();
$("tg-setup-again").dispatch("click");
const wizard = [shown(), $("tg-held").textContent, $("tg-cancel").hidden, doc.activeElement === $("tg-token")];
$("tg-cancel").dispatch("click");
done([notice, wizard, shown()]);
"""
    )
    notice, wizard, after = run(script)
    assert notice == {
        "shown": ["tg-state"],
        "status": "tg-status tg-notice",
        "icon": ICONS + "warning",
        "headline": "An earlier save didn't finish",
        "strong": ["An earlier save didn't finish"],
        "detail": "Diffbell won't trust a half-saved bot. "
        "Changes are held until you set Telegram up again.",
        "error": None,
        "actions": [["tg-setup-again", "primary"]],
        "reason": None,
        "held": None,
    }
    assert wizard == [
        ["tg-wizard"],
        "3 changes are waiting and will arrive as one message per project.",
        False,
        True,
    ]
    assert after == ["tg-state"]


def test_a_new_state_reorders_the_actions_and_the_held_count_follows_the_poll() -> None:
    script = (
        LOOK
        + f"""
state.telegram = {json.dumps(CONNECTED)};
tg.renderTelegram();
const first = look().actions;
state.telegram = {json.dumps({**CONNECTED, "failing": BAD_REQUEST})};
tg.renderTelegram();
const second = look().actions;
state.telegram = {json.dumps(NOT_SET_UP)};
state.dash = {{ counts: {{ held: 1 }} }};
tg.renderTelegram();
const one = look().held;
state.dash = {{ counts: {{ held: 4 }} }};
tg.renderTelegram();
done([first, second, one, look().held]);
"""
    )
    assert run(script) == [
        [["tg-send-test", ""], ["tg-replace", ""]],
        [["tg-show-failed", "tg-link"], ["tg-send-test", ""], ["tg-replace", ""]],
        "1 change is waiting and will arrive as one message per project.",
        "4 changes are waiting and will arrive as one message per project.",
    ]


def test_show_failed_changes_opens_the_failed_feed() -> None:
    script = """
let opened = 0;
tg.initTelegram({ showFailed: () => { opened += 1; } });
tg.wireTelegram();
const plain = $("tg-show-failed").dispatch("click");
// A modified click opens the link in a new tab, as the browser does it.
const modified = $("tg-show-failed").dispatch("click", { metaKey: true });
const feed = await import(js("feed.js"));
feed.readUrl(card.FAILED_HREF);  // what that new tab reads (a fresh page: the dashboard)
done([plain.defaultPrevented, modified.defaultPrevented, opened, card.FAILED_HREF, state.view, state.filters.status]);
"""
    assert run(script) == [True, False, 1, "?status=failed", "dashboard", "failed"]


def test_the_setup_guide_link_opens_the_guide() -> None:
    script = """
tg.wireTelegram();
const event = $("tg-guide").dispatch("click");
done([event.defaultPrevented, views]);
"""
    assert run(script) == [True, [["guide", {}]]]


def test_a_failed_test_message_stays_in_the_card_not_the_page_banner() -> None:
    script = f"""
tg.wireTelegram();
state.telegram = {json.dumps(CONNECTED)};
tg.renderTelegram();
$("tg-send-test").dispatch("click");
net.reply(503, {{ error: "Couldn't reach Telegram; try again." }});
await flush();
done([$("tg-message").textContent, [...state.outages.keys()]]);
"""
    assert run(script) == ["Couldn't reach Telegram; try again.", []]


def test_the_poll_refreshes_the_card_while_settings_shows() -> None:
    """A send that fails after Settings opened turns the card without a reload; one
    request at a time, none off Settings."""
    script = f"""
state.telegram = {json.dumps(CONNECTED)};
tg.renderTelegram();
state.view = "dashboard";
tg.refreshTelegram();
const off = net.calls.length;
state.view = "settings";
tg.refreshTelegram();
tg.refreshTelegram();  // the first is still in flight
const calls = net.calls.map((c) => c.path);
net.reply(200, {json.dumps({**CONNECTED, "failing": RETRYABLE})});
await flush();
done([off, calls, $("tg-headline").textContent]);
"""
    assert run(script) == [0, ["/api/telegram"], "Can't reach Telegram"]


def test_a_poll_that_moves_the_card_on_clears_its_old_message() -> None:
    """Connected with "Saved: …" shown, then the credentials go: the wizard must not
    say "Saved"."""
    script = f"""
state.view = "settings";
state.telegram = {json.dumps(CONNECTED)};
tg.renderTelegram();
$("tg-message").textContent = "Saved: Diffbell now sends to @my_bot.";
tg.refreshTelegram();
net.reply(200, {json.dumps({**CONNECTED, "failing": RETRYABLE})});
await flush();
const moved = $("tg-message").textContent;
$("tg-message").textContent = "Sample notification sent.";
tg.refreshTelegram();
net.reply(200, {json.dumps({**CONNECTED, "failing": RETRYABLE})});
await flush();
done([moved, $("tg-message").textContent]);
"""
    assert run(script) == ["", "Sample notification sent."]
