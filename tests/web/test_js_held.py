"""Held, skipped and catch-up events on the page under node (notify controls spec
§5.2): status words, the status sentence, list rows and the detail pane."""

from __future__ import annotations

import json
from typing import Any

import pytest

from diffbell.pausing import HELD_FOREVER
from diffbell.web.app import STATIC_DIR
from tests.web.node import needs_node, run_js
from tests.web.test_js_dashboard import NOW, T, ev, report, run

pytestmark = needs_node

HOUR = 3600


def status_of(event: dict[str, Any]) -> dict[str, Any]:
    script = f"""
const m = await import(js("status.js"));
done(m.eventStatus({json.dumps(event)}, {NOW}));
"""
    return dict(run_js(script))


# -- time.js and status.js ---------------------------------------------------------------


def test_until_text_today_tomorrow_and_later() -> None:
    script = f"""
const m = await import(js("time.js"));
done([m.untilText({T + 4 * HOUR}, {NOW}), m.untilText({T + 21 * HOUR}, {NOW}), m.untilText({T + 72 * HOUR}, {NOW})]);
"""
    assert run_js(script) == ["16:00", "tomorrow 09:00", "Mon 12 Oct 12:00"]


@pytest.mark.parametrize(
    ("event", "word", "icon", "tone", "title"),
    [
        (
            {"status": "held", "held_until": T + 4 * HOUR, "held_reason": "snooze", "attempts": 0},
            "Held until 16:00",
            "snooze",
            "muted",
            "Held until 16:00 (snoozed)",
        ),
        (
            {"status": "held", "held_until": None, "held_reason": "snooze", "attempts": 0},
            "Held",
            "snooze",
            "muted",
            "Held (snoozed)",
        ),
        (
            {"status": "held", "held_until": T + 20 * HOUR, "held_reason": "quiet", "attempts": 0},
            "Held until tomorrow 08:00",
            "snooze",
            "muted",
            "Held until tomorrow 08:00 (quiet hours)",
        ),
        (  # its catch-up hit a retryable error: due, so the API sends no held_until
            {
                "status": "held",
                "held_until": None,
                "held_reason": "snooze",
                "attempts": 2,
                "last_error": "sendMessage: 502",
            },
            "Catch-up retrying: sendMessage: 502",
            "retry",
            "warn",
            "sendMessage: 502",
        ),
        (  # the plain reason, as for a failed event; the raw error in the title
            {
                "status": "held",
                "held_until": T + 60,
                "held_reason": "quiet",
                "attempts": 1,
                "last_error": "network error: ConnectError: [Errno 61] Connection refused",
            },
            "Catch-up retrying: Telegram unreachable",
            "retry",
            "warn",
            "network error: ConnectError: [Errno 61] Connection refused",
        ),
        (
            {"status": "skipped"},
            "Not notified",
            "skip",
            "muted",
            "Nothing left to report (rules, or files created then deleted)",
        ),
    ],
)
def test_held_and_skipped_words(
    event: dict[str, Any], word: str, icon: str, tone: str, title: str
) -> None:
    result = status_of(event)
    assert (result["word"], result["icon"], result["tone"], result["title"]) == (
        word,
        icon,
        tone,
        title,
    )


def test_the_new_status_icons_are_in_the_sprite() -> None:
    sprite = (STATIC_DIR / "icons.svg").read_text(encoding="utf-8")
    for name in ("snooze", "skip"):
        assert f'<symbol id="{name}"' in sprite, name


DASH = {
    "daemon": {"connected": True},
    "telegram": {"last_error": None},
    "counts": {"held": 4},
    "projects": [
        {"name": "a", "status": "subscribed", "last_change_at": T - 180, "snooze": None},
        {"name": "b", "status": "subscribed", "last_change_at": None, "snooze": {"until": None}},
    ],
    "missing": [],
    "quiet": {"window": None, "active": False, "ends_at": None},
}


def sentence(**changes: Any) -> dict[str, Any]:
    dash = {**json.loads(json.dumps(DASH)), **changes}
    script = f"""
const m = await import(js("status.js"));
done(m.statusSentence({{ pollFailed: false, dash: {json.dumps(dash)}, now: {NOW} }}));
"""
    return dict(run_js(script))


def test_all_good_counts_the_snoozed_projects() -> None:
    result = sentence()
    assert result["text"] == "All good · watching 2 projects · 1 snoozed · last change 3 min ago"
    assert result["live"] == "All good · watching 2 projects · 1 snoozed"


def test_quiet_hours_say_until_when_and_how_many_are_held() -> None:
    quiet = {"window": "23:00-08:00", "active": True, "ends_at": T + 20 * HOUR}
    result = sentence(quiet=quiet)
    assert (result["text"], result["icon"], result["tone"]) == (
        "Quiet hours until 08:00 · 4 notifications held",
        "snooze",
        "ok",
    )
    assert (
        sentence(quiet=quiet, counts={})["text"] == "Quiet hours until 08:00 · 0 notifications held"
    )
    # Something failing still comes first.
    failing = sentence(quiet=quiet, telegram={"last_error": "sendMessage: Unauthorized"})
    assert failing["text"].startswith("Telegram failing")


# -- feed.js ----------------------------------------------------------------------------


def test_the_status_filter_accepts_held_and_skipped() -> None:
    script = """
const out = [];
for (const s of ["held", "skipped", "bogus"]) { feed.readUrl(`?status=${s}`); out.push(state.filters.status); }
done(out);
"""
    assert run(script) == ["held", "skipped", ""]


def test_rows_say_held_and_not_notified() -> None:
    rows = [
        ev(3, status="held", held_until=T + 4 * HOUR, held_reason="snooze", attempts=0),
        ev(2, status="skipped"),
    ]
    script = f"""
state.events = {json.dumps(rows)};
feed.renderFeed();
done(texts($("feed"), "word"));
"""
    assert run(script) == ["Held until 16:00", "Not notified"]


# -- detail.js ------------------------------------------------------------------------


def detail(**fields: Any) -> dict[str, Any]:
    row = ev(7)
    row.update(
        reports=[report(30)],
        telegram_lines=20,
        telegram_files=1,
        reports_state="stored",
        batch_id=None,
        batch_size=None,
        batch_report=None,
        held_until=None,
        held_reason=None,
    )
    row.update(fields)
    return row


def render(body: dict[str, Any]) -> Any:
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
const pane = $("detail");
done({{ notes: texts(pane, "delivery"), parts: texts(pane, "part"), dividers: texts(pane, "divider"),
        muted: pane.byClass("muted").filter((n) => n.tagName === "P").map((n) => n.textContent),
        lines: pane.byClass("ln").length }});
"""
    return run(script)


def test_a_held_event_says_until_when_and_why_without_a_divider() -> None:
    held = detail(
        status="held", held_until=T + 4 * HOUR, held_reason="quiet", telegram_lines=0,
        telegram_files=0,
    )  # fmt: skip
    result = render(held)
    assert result["notes"] == ["Held until 16:00 (quiet hours)"]
    assert result["dividers"] == [] and result["muted"] == []  # no "not listed" either
    assert result["lines"] == 30
    forever = render(detail(status="held", held_reason="snooze", next_attempt_at=HELD_FOREVER))
    assert forever["notes"] == ["Held until you turn notifications back on (snoozed)"]


def test_an_event_held_for_setup_says_so_never_snoozed() -> None:
    """Bot onboarding's held_reason "setup": not a snooze to turn back on."""
    setup = detail(status="held", held_reason="setup", next_attempt_at=HELD_FOREVER)
    assert render(setup)["notes"] == ["Held until Telegram is set up (Settings → Telegram)"]
    assert status_of({"status": "held", "held_reason": "setup", "attempts": 0})["title"] == (
        "Held (Telegram not set up)"
    )


def test_a_due_held_event_does_not_say_until_turned_back_on() -> None:
    """Once due the API sends no held_until (review fix 3): the note says the catch-up
    is retrying, and why, or about to be sent."""
    retrying = detail(
        status="held", held_reason="snooze", next_attempt_at=T + 60, attempts=2,
        last_error="sendMessage: Too Many Requests: retry after 5 (429)",
    )  # fmt: skip
    assert render(retrying)["notes"] == ["Catch-up retrying: rate limited (snoozed)"]
    due = detail(status="held", held_reason="quiet", next_attempt_at=T - 1, attempts=0)
    assert render(due)["notes"] == ["Catch-up about to be sent (quiet hours)"]


def test_a_catch_up_event_shows_the_message_then_its_own_change() -> None:
    batch = {
        "reports": [report(30, path="package.json"), report(2, path="Makefile")],
        "added": 32,
        "removed": 0,
        "telegram_lines": 20,
        "telegram_files": 2,
    }
    body = detail(batch_id=5, batch_size=3, batch_report=batch, telegram_lines=0, telegram_files=0)
    result = render(body)
    assert result["notes"] == ["Sent in catch-up #5 with 2 other changes"]
    assert result["parts"] == ["Catch-up message", "This change (when it was held)"]
    assert result["dividers"] == ["Telegram showed the lines above"]  # the message's only
    assert result["lines"] == 20 + 2 + 30


def test_a_failed_catch_up_offers_retry() -> None:
    body = detail(
        status="failed", last_error="sendMessage: Forbidden: bot was blocked by the user",
        batch_id=7, batch_size=1,
    )  # fmt: skip
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
const pane = $("detail");
done([texts(pane, "delivery"), pane.all().some((n) => n.dataset.focus === "retry")]);
"""
    assert run(script) == [["Catch-up failed: bot was blocked"], True]


def test_a_rule_skipped_file_is_marked() -> None:
    body = detail(
        reports=[report(3), report(0, path="old.json", kind="deleted", rule_skipped=True)]
    )
    result = render(body)
    assert result["muted"] == ["Not in the Telegram message (rule)"]


@pytest.mark.parametrize(
    ("state", "text"),
    [
        ("pruned", "Diff no longer kept (older than 30 days)."),
        ("not_recorded", "Diff not recorded (event predates diff history)."),
    ],
)
def test_a_missing_report_says_why(state: str, text: str) -> None:
    result = render(detail(reports=None, reports_state=state))
    assert result["muted"] == [text]
