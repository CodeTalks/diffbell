"""Cards and polish (diff readability spec §4, §7 "Page"): the card corner, the
snoozed status line, feed times per day group, the catch-up wording and the "6+"
title, under node."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.web.node import needs_node, run_js
from tests.web.test_js_dashboard import NOW, SETUP, T, ev, run
from tests.web.test_js_held import detail as held_detail
from tests.web.test_js_held import render as render_detail

pytestmark = needs_node

LOST = {
    "key": "project:web",
    "severity": "serious",
    "project": "web",
    "message": "project web: watch lost",
    "fix": None,
    "since": T - 60,
    "notified": True,
    "attempts": 0,
    "last_error": None,
}
WARN = {**LOST, "key": "recrawl:web", "severity": "warning", "message": "recrawl"}


def cards(dash: dict[str, Any], script: str) -> Any:
    return run(
        f"""
state.dash = {json.dumps(dash)};
dashboard.renderCards();
const wrap = (name) => [...$("cards").children].find((n) => n.dataset.key === `p:${{name}}`);
"""
        + script
    )


# -- the corner (§4.1) -------------------------------------------------------------------


def test_the_corner_holds_the_badges_then_the_bell_and_nothing_is_below_the_card() -> None:
    dash = {
        "projects": [{"name": "web", "status": "subscribed", "snooze": None}],
        "missing": [],
        "problems": [LOST, WARN],
    }
    script = """
const w = wrap("web");
const [card, corner] = w.children;
done([
  [...w.children].map((n) => [n.tagName, n.className]),
  [...corner.children].map((n) => n.className),
  corner.children[0].byClass("badge").map((b) => b.textContent),
  corner.children[1].byClass("snooze").map((b) => b.tagName),
  card.byClass("card-tools").length,  // never inside the card's own button
]);
"""
    assert cards(dash, script) == [
        # The activity strip (Plan 7) is drawn at the card's foot, over its padding.
        [["BUTTON", "card ok"], ["DIV", "card-tools"], ["DIV", "activity"]],
        ["card-problems", "card-snooze"],
        ["⚠️", "ⓘ"],
        ["BUTTON"],
        0,
    ]


def test_the_title_reserves_only_what_the_corner_holds() -> None:
    """The wrap's corner-* class follows the corner's contents on every poll: the
    bell or one badge is one slot, Resume (wider text) has its own class, and a
    badge that disappears gives the room back."""
    dash = {
        "projects": [
            {"name": "bell", "status": "subscribed", "snooze": None},
            {"name": "web", "status": "subscribed", "snooze": None},
            {"name": "zz", "status": "subscribed", "snooze": {"since": 1, "until": None}},
            {"name": "three", "status": "subscribed", "snooze": None},
            {"name": "gone", "status": "missing", "snooze": None},
        ],
        "missing": [],
        "problems": [
            LOST,
            {**LOST, "key": "a:three", "project": "three"},
            {**WARN, "key": "b:three", "project": "three"},
            {**LOST, "key": "c:zz", "project": "zz"},
        ],
    }
    script = """
const cls = () => ["bell", "web", "zz", "three", "gone"].map((n) => wrap(n).getAttribute("class"));
const before = cls();
state.dash.problems = state.dash.problems.filter((q) => q.key !== "project:web" && q.key !== "c:zz");
state.dash.projects[2].snooze = null;
dashboard.renderCards();
done([before, cls()]);
"""
    assert cards(dash, script) == [
        [
            "card-wrap corner-1",  # the bell
            "card-wrap corner-2",  # a badge and the bell
            "card-wrap corner-3plus",  # a badge and Resume
            "card-wrap corner-3plus",  # two badges and the bell
            "card-wrap corner-0",  # missing, not snoozed: nothing in the corner
        ],
        [
            "card-wrap corner-1",
            "card-wrap corner-1",  # the badge went away
            "card-wrap corner-1",  # resumed, badge gone
            "card-wrap corner-3plus",
            "card-wrap corner-0",
        ],
    ]


def test_a_snoozed_card_with_only_resume_has_its_own_class() -> None:
    dash = {
        "projects": [{"name": "zz", "status": "subscribed", "snooze": {"since": 1, "until": None}}],
        "missing": [],
    }
    assert cards(dash, 'done(wrap("zz").getAttribute("class"));') == "card-wrap corner-resume"


def test_a_snoozed_cards_status_line_says_until_when() -> None:
    dash = {
        "projects": [
            {"name": "a", "status": "subscribed", "snooze": {"since": 1, "until": T + 2 * 3600}},
            {"name": "b", "status": "subscribed", "snooze": {"since": 1, "until": None}},
            {"name": "c", "status": "subscribed", "snooze": None},
        ],
        "missing": [],
    }
    script = """
done([texts($("cards"), "state"), $("cards").byClass("resume").map((b) => b.getAttribute("title"))]);
"""
    assert cards(dash, script) == [
        ["Watching · snoozed until 14:00", "Watching · snoozed", "Watching"],
        [
            "Snoozed until 14:00: turn notifications back on",
            "Snoozed: turn notifications back on",
        ],
    ]


def test_a_cards_name_carries_its_full_text_as_a_title() -> None:
    long = "a-very-long-project-name-that-the-card-cuts-off-with-an-ellipsis"
    dash = {"projects": [{"name": long, "status": "subscribed"}], "missing": []}
    script = f"""
const name = wrap({json.dumps(long)}).byClass("name")[0];
done([name.textContent, name.getAttribute("title")]);
"""
    assert cards(dash, script) == [long, long]


def test_a_cards_name_title_adds_its_folder() -> None:
    """The title reads "name — folder" when the dashboard has the folder (a missing
    project has only its configured path)."""
    dash = {
        "projects": [
            {"name": "web", "status": "subscribed", "path": "~/web", "folder": "/h/web"},
            {"name": "api", "status": "subscribed", "folder": None},
        ],
        "missing": [{"name": "gone", "path": "/h/gone"}],
    }
    script = """
const title = (n) => wrap(n).byClass("name")[0].getAttribute("title");
done([title("web"), title("api"), title("gone")]);
"""
    assert cards(dash, script) == ["web — /h/web", "api", "gone — /h/gone"]


# -- "6+ files" (§4.4) ---------------------------------------------------------------------


def test_a_count_that_stopped_early_says_so_in_its_title() -> None:
    dash = {
        "projects": [
            {"name": "big", "status": "subscribed", "watched_count": 6, "watched_exact": False},
            {"name": "small", "status": "subscribed", "watched_count": 6, "watched_exact": True},
        ],
        "missing": [],
    }
    script = """
const meta = (name) => wrap(name).byClass("meta")[0];
const first = [meta("big").textContent, meta("big").getAttribute("title"), meta("small").getAttribute("title")];
state.dash.projects[0].watched_exact = true;  // the next count was exact
dashboard.renderCards();
done([first, meta("big").getAttribute("title")]);
"""
    assert cards(dash, script) == [
        [
            "6+ files · no changes yet",
            "At least 6 key files; counting stopped early in this large folder.",
            None,
        ],
        None,
    ]


# -- feed times (§4.2) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tz", "age_h", "expected"),
    [
        ("UTC", 2, "2 h ago"),  # today
        ("UTC", 11.9, "11 h ago"),  # still today (00:06)
        ("UTC", 12.5, "23:30"),  # yesterday, 12.5 h ago: the clock time
        ("UTC", 40, "20:00"),  # older
        ("Pacific/Auckland", 0.5, "30 min ago"),  # 00:30 on 10 Oct there: today
        ("Pacific/Auckland", 1.5, "23:30"),  # 23:30 on 9 Oct there: yesterday
    ],
)
def test_feed_times_are_relative_today_and_clock_times_before(
    tz: str, age_h: float, expected: str
) -> None:
    script = f"""
const time = await import(js("time.js"));
const ts = {T} - {age_h} * 3600;
done([time.feedTime(ts, {NOW}), time.fullTime(ts) !== ""]);
"""
    assert run_js(script, tz=tz) == [expected, True]


def test_a_rows_title_keeps_the_full_date_and_time() -> None:
    script = f"""
state.events = [{json.dumps(ev(1, created_at=T - 40 * 3600))}];
state.total = 1;
feed.renderFeed();
const when = $("feed").byClass("when")[0];
const time = await import(js("time.js"));
done([when.textContent, when.getAttribute("title") === time.fullTime({T - 40 * 3600})]);
"""
    assert run_js(SETUP + script) == ["20:00", True]


# -- catch-up wording (§4.3) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "note"),
    [
        (1, "Sent in catch-up #12"),
        (2, "Sent in catch-up #12 with 1 other change"),
        (4, "Sent in catch-up #12 with 3 other changes"),
    ],
)
def test_the_catch_up_line(size: int, note: str) -> None:
    body = held_detail(status="sent", batch_id=12, batch_size=size)
    assert render_detail(body)["notes"] == [note]


def test_a_failed_catch_up_keeps_its_wording() -> None:
    body = held_detail(status="failed", batch_id=12, batch_size=3, last_error="boom")
    assert render_detail(body)["notes"] == ["Catch-up failed: boom"]


# -- no stale count after a lost watch (§4.5) ------------------------------------------------


def test_a_lost_watch_shows_a_dash_then_counting_then_the_number() -> None:
    dash = {
        "projects": [
            {"name": "web", "status": "subscribed", "watched_count": None, "watched_exact": None},
            {"name": "api", "status": "subscribed", "watched_count": 4, "watched_exact": True},
        ],
        "missing": [],
        "problems": [LOST],
    }
    script = """
const meta = (name) => wrap(name).byClass("meta")[0].textContent;
const lost = [meta("web"), meta("api")];
state.dash.problems = [];  // resolved: the server starts a fresh count
dashboard.renderCards();
const counting = meta("web");
state.dash.projects[0].watched_count = 3;
state.dash.projects[0].watched_exact = true;
dashboard.renderCards();
done([lost, counting, meta("web")]);
"""
    assert cards(dash, script) == [
        ["— · no changes yet", "4 files · no changes yet"],
        "counting… · no changes yet",
        "3 files · no changes yet",
    ]
