"""The activity strip on each project card (Plan 7 spec §3.1) under node: bar
heights as h0..h10 classes, titles and aria-labels, the red tick, the click that
filters the feed with the bucket's own since/until, and the keyboard."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

from tests.web import test_js_dashboard as dash_js
from tests.web import test_js_forms as forms_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_main import BOOT, DASH, FIRST, POLL

pytestmark = needs_node

FIRST_DAY = date(2026, 9, 26)  # 14 days ending Fri 9 Oct 2026 (NOW's day)
MIDNIGHT = 1_790_380_800  # 2026-09-26 00:00 UTC; the node tests run in UTC


def days(
    totals: list[int], failed: dict[int, int] | None = None, *, shift: int = 0
) -> list[dict[str, Any]]:
    """14 buckets from FIRST_DAY (``shift`` days later: the window after midnight)."""
    failed = failed or {}
    return [
        {
            "day": (FIRST_DAY + timedelta(days=i + shift)).isoformat(),
            "since": MIDNIGHT + (i + shift) * 86400,
            "until": MIDNIGHT + (i + shift + 1) * 86400,
            "total": total,
            "failed": failed.get(i, 0),
        }
        for i, total in enumerate(totals)
    ]


QUIET = [0] * 14
BUSY = [0, 3, 0, 0, 1000, 1, 0, 0, 500, 0, 0, 0, 4, 0]

CARDS = """
const pushed = [];
feed.initFeed({ pushUrl: (q) => pushed.push(q) });
const strip = (name) => {
  const wrap = [...$("cards").children].find((n) => n.dataset.key === `p:${name}`);
  return wrap.byClass("activity")[0];
};
const bars = (name) => strip(name).byClass("bar");
"""


def run(dash_activity: dict[str, list[dict[str, Any]]], script: str) -> Any:
    dash = {
        "projects": [
            {"name": "web", "status": "subscribed"},
            {"name": "api", "status": "subscribed"},
        ],
        "missing": [],
        "activity": dash_activity,
    }
    setup = f"state.dash = {json.dumps(dash)};\ndashboard.renderCards();\n"
    return dash_js.run(CARDS + setup + script)


def test_bars_scale_to_the_busiest_day_and_any_change_shows() -> None:
    script = """done([bars("web").map((b) => b.className), bars("api").map((b) => b.className)]);"""
    web, api = run({"web": days(BUSY, {4: 2}), "api": days(QUIET)}, script)
    assert web == [
        "bar h0",
        "bar h1",  # 3 of 1000 would round to 0: any change is at least h1
        "bar h0",
        "bar h0",
        "bar h10 failed",
        "bar h1",  # 1 of 1000
        "bar h0",
        "bar h0",
        "bar h5",
        "bar h0",
        "bar h0",
        "bar h0",
        "bar h1",
        "bar h0",
    ]
    assert api == ["bar h0"] * 14  # a quiet project: every day a stub, no division by zero


def test_titles_and_aria_labels_say_the_day_and_the_count() -> None:
    script = """done(bars("web").map((b) => [b.title, b.getAttribute("aria-label")]));"""
    totals = [0] * 11 + [1, 4, 0]
    labels = run({"web": days(totals, {12: 1}), "api": days(QUIET)}, script)
    assert labels[0] == ["Sat 26 Sep · no changes", "Sat 26 Sep · no changes"]
    assert labels[11] == ["Wed 7 Oct · 1 change", "Wed 7 Oct · 1 change"]
    assert labels[12] == ["Thu 8 Oct · 4 changes (1 failed)", "Thu 8 Oct · 4 changes (1 failed)"]
    assert labels[13][0] == "Fri 9 Oct · no changes"


def test_the_strip_is_one_labelled_group_with_one_tab_stop() -> None:
    script = """
const s = strip("web");
done([s.getAttribute("role"), s.getAttribute("aria-label"), bars("web").map((b) => b.getAttribute("tabindex")),
      bars("web").every((b) => b.tagName === "BUTTON" && b.getAttribute("type") === "button")]);
"""
    role, label, tabindex, buttons = run({"web": days(BUSY), "api": days(QUIET)}, script)
    assert (role, label, buttons) == ("group", "Activity, last 14 days", True)
    assert tabindex == ["-1"] * 13 + ["0"]  # today's bar is the group's tab stop


def test_a_click_filters_the_feed_to_that_project_and_bucket() -> None:
    script = """
bars("web")[4].click();
const f = state.filters;
done([f.project, f.from, f.to, f.since, f.until, pushed.at(-1), urls.length, refreshes,
      feed.eventQuery(0, 50)]);
"""
    bucket = days(BUSY)[4]
    result = run({"web": days(BUSY), "api": days(QUIET)}, script)
    since, until = str(bucket["since"]), str(bucket["until"])
    assert result == [
        "web",
        "2026-09-30",
        "2026-09-30",
        since,
        until,
        f"project=web&from=2026-09-30&to=2026-09-30&since={since}&until={until}",
        0,  # pushed, not replaced: Back returns to the previous filters
        1,
        f"limit=50&offset=0&project=web&since={since}&until={until}",
    ]


def test_a_click_clears_the_status_and_the_search_so_the_list_is_what_the_bar_counted() -> None:
    script = """
Object.assign(state.filters, { status: "failed", q: "lock" });
bars("web")[4].click();
done([state.filters.status, state.filters.q, pushed.at(-1)]);
"""
    status, q, url = run({"web": days(BUSY), "api": days(QUIET)}, script)
    assert (status, q) == ("", "")
    assert url.startswith("project=web&from=") and "status" not in url and "q=" not in url


def test_the_click_uses_the_servers_bounds_not_the_label() -> None:
    """A 25-hour DST day: the bucket's own since/until go to the feed unchanged."""
    odd = days(QUIET)
    odd[3]["until"] = odd[3]["since"] + 25 * 3600
    odd[4]["since"] = odd[3]["until"]
    script = """
bars("web")[3].click();
done([state.filters.since, state.filters.until]);
"""
    assert run({"web": odd, "api": days(QUIET)}, script) == [
        str(odd[3]["since"]),
        str(odd[3]["since"] + 25 * 3600),
    ]


@pytest.mark.parametrize(
    ("keys", "focused", "applied"),
    [
        (["ArrowLeft"], 12, None),
        (["ArrowLeft", "ArrowLeft", "ArrowRight"], 12, None),
        (["Home"], 0, None),
        (["Home", "ArrowLeft"], 0, None),  # stops at the ends
        (["End", "ArrowRight"], 13, None),
        (["ArrowLeft", "Enter"], 12, 12),
    ],
)
def test_the_keyboard_moves_between_days_and_enter_applies(
    keys: list[str], focused: int, applied: int | None
) -> None:
    script = f"""
bars("web")[13].focus();
const prevented = [];
for (const key of {json.dumps(keys)}) {{
  const event = doc.activeElement.dispatch("keydown", {{ key }});
  prevented.push(event.defaultPrevented);
}}
const all = bars("web");
done([all.indexOf(doc.activeElement), all.map((b) => b.getAttribute("tabindex")).indexOf("0"),
      state.filters.since, prevented]);
"""
    index, stop, since, prevented = run({"web": days(BUSY), "api": days(QUIET)}, script)
    assert (index, stop) == (focused, focused)
    expected = "" if applied is None else str(days(BUSY)[applied]["since"])
    assert since == expected
    assert prevented == [True] * len(keys)


def test_a_poll_with_the_same_data_keeps_the_bars_and_focus() -> None:
    script = """
const before = bars("web")[5];
before.focus();
dashboard.renderCards();
done([bars("web")[5] === before, doc.activeElement === before]);
"""
    assert run({"web": days(BUSY), "api": days(QUIET)}, script) == [True, True]


def test_a_new_day_rebuilds_the_strip_and_keeps_focus_on_the_same_place() -> None:
    script = f"""
bars("web")[13].focus();
state.dash.activity.web = {json.dumps(days([*BUSY[1:], 7]))};
dashboard.renderCards();
done([bars("web")[13].className, doc.activeElement === bars("web")[13]]);
"""
    assert run({"web": days(BUSY), "api": days(QUIET)}, script) == ["bar h1", True]


def test_after_midnight_focus_follows_the_day_not_the_place() -> None:
    """The window moves by one day: the focused day (and the tab stop) is now one
    bar to the left. A day that left the window falls back to the same place."""
    script = f"""
bars("web")[10].focus();
state.dash.activity.web = {json.dumps(days(BUSY, shift=1))};
dashboard.renderCards();
const stop = bars("web").map((b) => b.getAttribute("tabindex")).indexOf("0");
const moved = [bars("web").indexOf(doc.activeElement), stop];
bars("api")[0].focus();
state.dash.activity.api = {json.dumps(days([1, *QUIET[1:]], shift=1))};
dashboard.renderCards();
done([moved, bars("api").indexOf(doc.activeElement)]);
"""
    assert run({"web": days(BUSY), "api": days(QUIET)}, script) == [[9, 12], 0]


def test_a_click_moves_the_tab_stop_to_that_bar() -> None:
    script = """
bars("web")[3].click();
done(bars("web").map((b) => b.getAttribute("tabindex")).indexOf("0"));
"""
    assert run({"web": days(BUSY), "api": days(QUIET)}, script) == 3


def test_enter_applies_the_bars_own_bucket() -> None:
    """Enter acts on what the bar shows, even if the state moved on before a rebuild."""
    script = f"""
bars("web")[12].focus();
state.dash.activity.web = {json.dumps(days(BUSY, shift=1))};
doc.activeElement.dispatch("keydown", {{ key: "Enter" }});
done(state.filters.since);
"""
    expected = str(days(BUSY)[12]["since"])
    assert run({"web": days(BUSY), "api": days(QUIET)}, script) == expected


def test_a_project_without_activity_has_an_empty_strip() -> None:
    script = """done([strip("web").children.length, strip("api").children.length]);"""
    assert run({"web": days(BUSY)}, script) == [14, 0]


def test_the_strip_sits_outside_the_cards_button() -> None:
    script = """
const wrap = $("cards").children[0];
done([wrap.children[0].tagName, wrap.children[0].byClass("activity").length,
      wrap.children.length, wrap.children[2].className]);
"""
    assert run({"web": days(BUSY), "api": days(QUIET)}, script) == ["BUTTON", 0, 3, "activity"]


@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Pacific/Auckland"])
def test_day_titles_come_from_the_date_not_a_timestamp(tz: str) -> None:
    script = """
const time = await import(js("time.js"));
done([time.dayTitle("2026-10-08"), time.dayTitle("2026-03-29"), time.dayTitle("bogus")]);
"""
    assert run_js(script, tz=tz) == ["Thu 8 Oct", "Sun 29 Mar", ""]


# -- the feed's since/until filter ----------------------------------------------------


def test_the_url_keeps_since_and_until_only_when_they_are_numbers() -> None:
    script = """
feed.readUrl("?project=web&since=1790726400&until=1790812800.5&from=2026-09-30");
const good = [state.filters.since, state.filters.until, feed.eventQuery(0, 50)];
feed.readUrl("?since=yesterday&until=1e9&from=2026-09-30");
done([good, state.filters.since, state.filters.until, feed.eventQuery(0, 50)]);
"""
    assert dash_js.run(script) == [
        [
            "1790726400",
            "1790812800.5",
            "limit=50&offset=0&project=web&since=1790726400&until=1790812800.5",
        ],
        "",
        "",
        "limit=50&offset=0&since=1790726400",  # the From date (UTC midnight) again
    ]


def test_clear_filters_clears_the_bucket_too() -> None:
    script = """
feed.setFilter({ project: "web", since: "1", until: "2", from: "2026-09-30", to: "2026-09-30" });
feed.clearFilters();
done([Object.values(state.filters).every((v) => v === ""), feed.filtersActive()]);
"""
    assert dash_js.run(script) == [True, False]


# -- main.js: Back restores the filters, a date input drops the bucket -----------------

MAIN = BOOT  # its fake window records pushState and the popstate listener


def test_a_strip_click_pushes_and_back_restores_the_previous_filters() -> None:
    dash = {**DASH, "activity": {"proj": days(BUSY)}}
    replies = FIRST.replace(json.dumps(DASH), json.dumps(dash))
    script = f"""
const bar = $("cards").byClass("bar")[4];
{POLL}
bar.click();
for (let i = 0; i < 5; i += 1) await settle();
const after = [pushedUrls.at(-1), state.filters.project];
globalThis.window.location.search = "?event=7";
const pushes = pushedUrls.length;
{POLL}
for (const fn of winListeners.popstate) fn();
for (let i = 0; i < 5; i += 1) await settle();
done([after, state.filters.project, state.filters.since, net.calls.at(-1).path, pushedUrls.length - pushes]);
"""
    script = 'const { state } = await import(js("state.js"));\n' + script
    since = days(BUSY)[4]["since"]
    result = run_js(MAIN.replace("__REPLIES__", replies) + script)
    assert result[0][0].startswith("?project=proj&from=2026-09-30&to=2026-09-30&since=")
    assert result[0][1] == "proj"
    assert result[1:3] == ["", ""]
    assert result[3] == "/api/events?limit=50&offset=0"
    assert result[4] == 0  # Back restores; it never pushes an entry of its own
    assert str(since) in result[0][0]


def test_back_from_settings_after_a_strip_click_returns_to_the_dashboard() -> None:
    """The click pushes an entry; Settings then replaces it (?view=settings…); Back
    lands on the entry before the click, which has no ?view=: the dashboard."""
    dash = {**DASH, "activity": {"proj": days(BUSY)}}
    replies = FIRST.replace(json.dumps(DASH), json.dumps(dash))
    script = f"""
const {{ state }} = await import(js("state.js"));
{POLL}
$("cards").byClass("bar")[4].click();
for (let i = 0; i < 5; i += 1) await settle();
net.reply(200, {json.dumps(forms_js.SETTINGS)});  // the settings tab loads them
tabs[2].click();
for (let i = 0; i < 5; i += 1) await settle();
const before = [state.view, replaced.at(-1).startsWith("?view=settings&project=proj")];
globalThis.window.location.search = "?event=7";
{POLL}
for (const fn of winListeners.popstate) fn();
for (let i = 0; i < 5; i += 1) await settle();
done([before, state.view, $("view-dashboard").hidden, $("view-settings").hidden, tabs[0].getAttribute("aria-selected")]);
"""
    assert run_js(MAIN.replace("__REPLIES__", replies) + script) == [
        ["settings", True],
        "dashboard",
        False,
        True,
        "true",
    ]


def test_a_date_input_drops_the_buckets_exact_bounds() -> None:
    script = """
const { state } = await import(js("state.js"));
const { setFilter } = await import(js("feed.js"));
setFilter({ project: "proj", from: "2026-09-30", to: "2026-09-30", since: "1790726400", until: "1790812800" });
$("f-to").value = "2026-10-02";
$("f-to").dispatch("change");
done([state.filters.from, state.filters.to, state.filters.since, state.filters.until]);
"""
    assert run_js(MAIN.replace("__REPLIES__", FIRST) + script) == [
        "2026-09-30",
        "2026-10-02",
        "",
        "",
    ]


def test_back_after_the_header_link_returns_to_the_tab_it_was_clicked_on() -> None:
    """The link pushes its entry before the view switch writes the URL, so the
    Settings entry is left as it was and Back lands there."""
    dash = {**DASH, "missing": [{"name": "gone", "path": "/g"}]}
    replies = FIRST.replace(json.dumps(DASH), json.dumps(dash))
    script = f"""
const {{ state }} = await import(js("state.js"));
net.reply(200, {json.dumps(forms_js.SETTINGS)});
tabs[2].click();
for (let i = 0; i < 5; i += 1) await settle();
// A browser's history from here on: one entry per push, replace edits the current one.
const entries = [replaced.at(-1)];
let at = 0;
globalThis.window.history.replaceState = (a, b, url) => {{ entries[at] = url; }};
globalThis.window.history.pushState = (a, b, url) => {{ entries.splice(at + 1); entries.push(url); at += 1; }};
net.reply(200, {{ events: [], total: 0 }});
$("status-attention").click();
for (let i = 0; i < 5; i += 1) await settle();
const after = [state.view, state.filters.status, entries.length];
at -= 1;
globalThis.window.location.search = entries[at];
for (const fn of winListeners.popstate) fn();
for (let i = 0; i < 5; i += 1) await settle();
done([after, entries[0].startsWith("?view=settings"), state.view, $("view-settings").hidden, state.filters.status]);
"""
    assert run_js(MAIN.replace("__REPLIES__", replies) + script) == [
        ["dashboard", "attention", 2],
        True,
        "settings",
        False,
        "",
    ]
