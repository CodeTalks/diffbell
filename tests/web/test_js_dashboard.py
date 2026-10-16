"""The dashboard modules under node with a fake DOM: the list (feed.js), the
detail pane (detail.js) and the cards and header (dashboard.js), UI refine spec §4.2-§4.3."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.web.node import needs_node, run_js

pytestmark = needs_node

NOW = 1_791_547_200_000  # 2026-10-09 12:00:00 UTC, ms
T = NOW // 1000

SETUP = f"""
const dom = await import(js("dom.js"));
const {{ state }} = await import(js("state.js"));
const api = await import(js("api.js"));
const feed = await import(js("feed.js"));
const detail = await import(js("detail.js"));
const dashboard = await import(js("dashboard.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const net = fakeFetch();
api.configureApi({{ fetch: net.fetch }});
const urls = [];
let refreshes = 0;
feed.initFeed({{
  replaceUrl: (q) => urls.push(q),
  refresh: () => {{ refreshes += 1; }},
  now: () => {NOW},
  selectionChanged: () => detail.syncDetail(),
}});
dashboard.initDashboard({{ now: () => {NOW} }});
const $ = (id) => doc.getElementById(id);
const texts = (el, cls) => el.byClass(cls).map((n) => n.textContent);
"""


def ev(id_: int, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": id_,
        "project": "proj",
        "created_at": T - 60 * id_,
        "status": "sent",
        "attempts": 1,
        "last_error": None,
        "next_attempt_at": T,
        "branch": "main",
        "added": 2,
        "removed": 1,
        "files": [{"path": "package.json", "kind": "modified", "redacted": False}],
    }
    row.update(fields)
    return row


def run(script: str) -> Any:
    return run_js(SETUP + script)


# -- feed.js: merging and paging (moved from the app.js slicing tests) ---------------


def ids(n: int, top: int) -> str:
    return json.dumps([{"id": top - i} for i in range(n)])


def merge(page: str, loaded: str, limit: int) -> list[int]:
    script = f"state.events = {loaded};\ndone(feed.mergeNewest({page}, {limit}).map((e) => e.id));"
    return list(run(script))


def test_merge_newest_joins_a_page_that_reaches_the_loaded_rows() -> None:
    assert merge(ids(5, 12), ids(10, 10), 5) == list(range(12, 0, -1))


def test_merge_newest_drops_the_older_rows_when_a_gap_would_open() -> None:
    assert merge(ids(5, 30), ids(10, 10), 5) == [30, 29, 28, 27, 26]


def test_merge_newest_takes_a_short_page_as_the_whole_result() -> None:
    assert merge(ids(3, 12), ids(10, 10), 5) == [12, 11, 10]


def test_load_more_appends_the_rows_older_than_the_tail() -> None:
    script = f"""
state.events = [{json.dumps(ev(10))}, {json.dumps(ev(9))}];
state.total = 30;
const loading = feed.loadMore();
net.reply(200, {{ events: [{json.dumps(ev(8))}, {json.dumps(ev(7))}], total: 30 }});
await loading;
done([state.events.map((e) => e.id), net.calls[0].path.includes("before_id=9"), $("more").disabled]);
"""
    assert run(script) == [[10, 9, 8, 7], True, False]


def test_load_more_drops_a_reply_once_a_poll_moved_the_tail() -> None:
    script = f"""
state.events = [{json.dumps(ev(10))}, {json.dumps(ev(9))}];
const loading = feed.loadMore();
state.events = [{json.dumps(ev(40))}, {json.dumps(ev(39))}];  // a poll replaced the rows
net.reply(200, {{ events: [{json.dumps(ev(8))}], total: 30 }});
await loading;
done([state.events.map((e) => e.id), $("more").disabled]);
"""
    assert run(script) == [[40, 39], False]


def test_a_load_more_error_survives_the_next_render() -> None:
    script = f"""
state.events = [{json.dumps(ev(10))}];
state.total = 30;
const loading = feed.loadMore();
net.reply(503, {{ error: "database is locked" }});
await loading;
feed.renderFeed();
done($("feed-status").textContent);
"""
    assert run(script) == "1 of 30 events · Load more failed: Unavailable: database is locked"


# -- feed.js: URL, rows, day groups and selection ------------------------------------


def test_the_feed_count_never_exceeds_the_total() -> None:
    """A row can stay loaded for a moment after it left the filter (Plan 2)."""
    script = f"""
state.events = [{json.dumps(ev(3))}, {json.dumps(ev(2))}, {json.dumps(ev(1))}];
state.total = 2;
feed.renderFeed();
done($("feed-status").textContent);
"""
    assert run(script) == "2 of 2 events"


def test_the_url_carries_the_filters_and_the_selected_event() -> None:
    script = """
feed.readUrl("?project=" + "p".repeat(2000) + "&status=bogus&event=42&view=projects");
const first = [state.filters.project.length, state.filters.status, state.selected, state.view];
feed.readUrl("?event=0");
const zero = state.selected;
feed.readUrl("?event=12abc");
const junk = state.selected;
state.view = "dashboard";
state.filters.q = "lock";
state.selected = 7;
feed.writeUrl();
done([first, zero, junk, urls.at(-1)]);
"""
    assert run(script) == [[1024, "", 42, "projects"], None, None, "q=lock&event=7"]


def test_rows_are_grouped_by_day_with_plain_words() -> None:
    events = [
        ev(
            1, created_at=T - 120, status="pending", attempts=0, branch="feat/x", added=5, removed=0
        ),
        ev(
            2,
            created_at=T - 13 * 3600,
            status="failed",
            last_error="sendMessage: Forbidden: bot was blocked by the user",
            added=None,
            removed=None,
            branch=None,
        ),
        ev(3, created_at=T - 40 * 3600),
    ]
    script = f"""
state.events = {json.dumps(events)};
state.total = 3;
feed.renderFeed();
const feedEl = $("feed");
done([
  texts(feedEl, "day"),
  texts(feedEl, "when"),
  texts(feedEl, "branch"),
  texts(feedEl, "add"),
  texts(feedEl, "del"),
  texts(feedEl, "word"),
  feedEl.byClass("row").map((r) => r.className),
  $("more").hidden,
]);
"""
    assert run(script) == [
        ["Today", "Yesterday", "Wed 7 Oct"],
        ["2 min ago", "23:00", "20:00"],  # relative in Today only
        ["feat/x", "", "main"],
        ["+5", "", "+2"],
        ["−0", "", "−1"],
        ["Sending…", "Failed: bot was blocked", "Sent"],
        ["row selected", "row", "row"],
        True,
    ]


def test_selection_moves_and_clamps() -> None:
    events = [ev(3), ev(2), ev(1)]
    script = f"""
state.events = {json.dumps(events)};
state.total = 3;
net.reply(200, {json.dumps(ev(2, reports=None))});
feed.renderFeed();
const moved = feed.moveSelection(1);
const second = state.selected;
feed.moveSelection(5);
const last = state.selected;
feed.moveSelection(-10);
const first = state.selected;
const selectedRows = $("feed").byClass("selected").length;
state.events = [];
done([moved, second, last, first, selectedRows, feed.moveSelection(1), urls.at(-1)]);
"""
    assert run(script) == [True, 2, 1, 3, 1, False, "event=3"]


def test_set_filter_resets_the_list_and_the_selection() -> None:
    script = f"""
state.events = [{json.dumps(ev(3))}];
state.selected = 3;
state.detailNote = "That event is no longer in history";
feed.setFilter({{ status: "failed" }});
done([state.events, state.selected, state.filterGen, refreshes, urls.at(-1), $("f-status").value, state.detailNote]);
"""
    assert run(script) == [[], None, 1, 1, "status=failed", "failed", ""]


def test_set_filter_tells_the_cards() -> None:
    """The cards show which project is filtered: they re-render on every filter change."""
    script = """
let changed = 0;
feed.initFeed({ filtersChanged: () => { changed += 1; } });
feed.setFilter({ project: "web" });
feed.clearFilters();
done(changed);
"""
    assert run(script) == 2


def test_a_day_that_comes_back_gets_its_own_heading() -> None:
    """Day headings are keyed by their first event too, so a day that appears twice
    (out-of-order rows) doesn't share one heading node."""
    events = [ev(3, created_at=T), ev(2, created_at=T - 2 * 86400), ev(1, created_at=T - 60)]
    script = f"""
state.events = {json.dumps(events)};
state.total = 3;
feed.renderFeed();
done(texts($("feed"), "day"));
"""
    assert run(script) == ["Today", "Wed 7 Oct", "Today"]


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (
            {},
            [
                "No changes yet. When a watched file changes, it shows up here and in Telegram.",
                "Send test notification",
            ],
        ),
        ({"q": "lock"}, ["No events match these filters.", "Clear filters"]),
    ],
)
def test_empty_feed_states(filters: dict[str, str], expected: list[str]) -> None:
    script = f"""
state.dash = {{ projects: [{{ name: "proj", status: "subscribed" }}], missing: [] }};
Object.assign(state.filters, {json.dumps(filters)});
state.total = 0;
feed.renderFeed();
const box = $("feed-empty");
done([box.hidden, box.elements().slice(0, 2).map((n) => n.textContent)]);
"""
    assert run(script) == [False, expected]


def test_clear_filters_from_the_empty_state() -> None:
    script = """
state.dash = { projects: [{ name: "proj", status: "subscribed" }], missing: [] };
state.filters.q = "lock";
state.total = 0;
feed.renderFeed();
[...$("feed-empty").children].find((n) => n.textContent === "Clear filters").click();
done([state.filters.q, refreshes]);
"""
    assert run(script) == ["", 1]


def test_retry_queues_and_toasts() -> None:
    script = f"""
const row = {json.dumps(ev(5, status="failed", attempts=1, last_error="boom"))};
state.events = [row];
net.reply(200, {{ ok: true }});
const ok = await feed.retryEvent(row);
const toast = $("toasts").textContent;
net.reply(409, {{ error: "event 5 is not failed" }});
const again = await feed.retryEvent(row);
done([ok, row.status, toast, refreshes, again, state.feedNote]);
"""
    assert run(script) == [
        True,
        "pending",
        "Retry queued",
        2,
        False,
        "Retry not needed: Conflict: event 5 is not failed",
    ]


def test_a_refused_retry_puts_the_row_back() -> None:
    script = f"""
const row = {json.dumps(ev(5, status="failed", attempts=2, last_error="boom"))};
state.events = [row];
net.reply(503, {{ error: "database is locked" }});
await feed.retryEvent(row);
done([row.status, row.attempts, row.last_error, state.feedNote]);
"""
    assert run(script) == ["failed", 2, "boom", "Retry failed: Unavailable: database is locked"]


# -- detail.js ---------------------------------------------------------------------


def report(n_lines: int, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": "package.json",
        "kind": "modified",
        "redacted": False,
        "note": "vs HEAD",
        "lines": [f"+line {i}" for i in range(n_lines)],
        "truncated": False,
    }
    entry.update(fields)
    return entry


@pytest.mark.parametrize(
    ("status", "lines", "index", "budget", "files", "expected"),
    [
        ("sent", 30, 0, 20, 1, [20, 10, "Telegram showed the lines above", False]),
        ("pending", 30, 0, 20, 1, [20, 10, "The Telegram message has the lines above", False]),
        ("failed", 30, 0, 20, 1, [20, 10, "The Telegram message has the lines above", False]),
        ("sent", 20, 0, 20, 1, [20, 0, "", False]),  # no more lines than the budget
        ("sent", 30, 0, 0, 1, [30, 0, "", False]),  # budget 0: no divider
        ("sent", 30, 1, 20, 1, [30, 0, "", True]),  # past telegram_files
    ],
)
def test_diff_plan(
    status: str, lines: int, index: int, budget: int, files: int, expected: list[Any]
) -> None:
    script = f"""
const plan = detail.diffPlan({json.dumps(report(lines))}, {index}, {{ status: "{status}", telegram_lines: {budget}, telegram_files: {files} }});
done([plan.head.length, plan.rest.length, plan.divider, plan.notListed]);
"""
    assert run(script) == expected


def detail_of(reports: list[dict[str, Any]] | None, **fields: Any) -> dict[str, Any]:
    row = ev(7)
    row.update(reports=reports, telegram_lines=2, telegram_files=1, **fields)
    return row


def test_diff_lines_are_text_with_a_class_per_line() -> None:
    lines = ["@@ -1 +1 @@", "-old", "+<script>alert(1)</script>", " same", "+four", "+five"]
    body = detail_of([report(0, lines=lines), report(0, path="b.txt", lines=["+x"])])
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
const pane = $("detail");
const before = pane.byClass("ln").map((n) => [n.className, n.textContent]);
const show = pane.byClass("link").find((b) => b.textContent.startsWith("Show all"));
const label = show.textContent;
show.click();
done([before, texts(pane, "divider"), label, pane.byClass("ln").length, texts(pane, "muted")]);
"""
    before, dividers, label, count, notes = run(script)
    assert before == [
        ["ln hunk", "@@ -1 +1 @@"],
        ["ln del", "-old"],
        ["ln add", "+x"],
    ]
    assert dividers == ["Telegram showed the lines above"]
    assert label == "Show all 6 lines"
    assert count == 7  # the rest, one element per line, once shown
    assert notes == ["Not listed in the Telegram message (too long)"]


def test_script_tags_stay_text() -> None:
    body = detail_of([report(0, lines=["+<script>alert(1)</script>"])])
    body["telegram_lines"] = 20
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
const lines = $("detail").byClass("ln");
done([lines[0].textContent, lines[0].elements().map((e) => [e.tagName, e.elements().length])]);
"""
    # package.json is coloured as JSON: the "1" is a number span, and no element
    # but that span was made from the line.
    assert run(script) == ["+<script>alert(1)</script>", [["SPAN", 0]]]


def test_redacted_truncated_and_unrecorded_files() -> None:
    body = detail_of(
        [
            report(0, path=".env.local", redacted=True, note=None),
            report(0, path="big.json", truncated=True, note="diff omitted (size limit)"),
        ],
        status="failed",
        last_error="boom",
    )
    old = detail_of(None)
    old["files"] = [{"path": ".env", "kind": "created", "redacted": True}]
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
const pane = $("detail");
const first = [texts(pane, "muted"), pane.byClass("path").map((n) => n.textContent), pane.textContent.includes("Retry")];
state.detail = {json.dumps(old)};
detail.renderDetail();
done([first, texts(pane, "muted"), texts(pane, "files")]);
"""
    assert run(script) == [
        [
            [
                " contents redacted",
                "Not listed in the Telegram message (too long)",  # big.json is past telegram_files
                "diff omitted (size limit)",
            ],
            [".env.local", "big.json"],
            True,
        ],
        ["Diff not recorded (event predates diff history)."],
        [".env"],
    ]


def test_a_file_past_telegram_files_is_marked_even_when_redacted_or_truncated() -> None:
    body = detail_of(
        [
            report(1),
            report(0, path=".env.local", redacted=True, note=None),
            report(0, path="big.json", truncated=True, note="diff omitted (size limit)"),
        ]
    )
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
done(texts($("detail"), "muted"));
"""
    assert run(script) == [
        "Not listed in the Telegram message (too long)",
        " contents redacted",
        "Not listed in the Telegram message (too long)",
        "diff omitted (size limit)",
    ]


FOCUS = f"""
state.events = [{json.dumps(ev(7, status="pending", attempts=0))}];
net.reply(200, {json.dumps(detail_of([report(5)]))});
await detail.syncDetail();
const pane = $("detail");
const head = () => pane.byClass("file-head")[0];
"""


def test_the_detail_keeps_focus_when_a_status_change_refetches_it() -> None:
    script = (
        FOCUS
        + f"""
const before = head();
before.focus();
state.events[0].status = "sent";
net.reply(200, {json.dumps(detail_of([report(5)]))});
await detail.syncDetail();
done([net.calls.length, head() !== before, doc.activeElement === head()]);
"""
    )
    assert run(script) == [2, True, True]


def test_collapsing_and_show_all_keep_focus_in_the_pane() -> None:
    script = (
        FOCUS
        + """
const show = pane.byClass("link").find((b) => b.textContent.startsWith("Show all"));
show.focus();
show.click();  // the button goes: focus falls back to its file's header
const afterShow = doc.activeElement === head();
head().click();  // Enter on the focused header collapses the file
done([afterShow, doc.activeElement === head(), head().getAttribute("aria-expanded")]);
"""
    )
    assert run(script) == [True, True, "false"]


def test_retry_from_the_detail_is_sent_once() -> None:
    failed = ev(7, status="failed", attempts=1, last_error="boom")
    script = f"""
state.events = [{json.dumps(failed)}];
net.reply(200, {json.dumps(detail_of([], status="failed", last_error="boom"))});
await detail.syncDetail();
const retry = () => $("detail").all().find((n) => n.tagName === "BUTTON" && n.textContent === "Retry");
retry().focus();
retry().click();
// Busy is aria-disabled, not disabled: a disabled button can't hold focus.
const busy = [retry().getAttribute("aria-disabled"), retry().disabled, doc.activeElement === retry()];
retry().click();
await settle();
const posts = net.calls.filter((c) => c.method === "POST").length;
net.reply(200, {{ ok: true }});
net.reply(200, {json.dumps(detail_of([], status="pending", attempts=0))});
await settle(); await settle(); await settle();
// Retry is gone (the event is pending) and there is no file: the pane takes focus.
done([busy, posts, net.calls.map((c) => c.method), Boolean(retry()),
      doc.activeElement === $("detail"), $("detail").getAttribute("tabindex")]);
"""
    assert run(script) == [["true", False, True], 1, ["GET", "POST", "GET"], False, True, "-1"]


def test_focus_after_a_retry_moves_to_the_first_file_header() -> None:
    failed = ev(7, status="failed", attempts=1, last_error="boom")
    script = f"""
state.events = [{json.dumps(failed)}];
net.reply(200, {json.dumps(detail_of([report(1)], status="failed", last_error="boom"))});
await detail.syncDetail();
const retry = () => $("detail").all().find((n) => n.tagName === "BUTTON" && n.textContent === "Retry");
retry().focus();
retry().click();
net.reply(200, {{ ok: true }});
net.reply(200, {json.dumps(detail_of([report(1)], status="pending", attempts=0))});
await settle(); await settle(); await settle();
done([Boolean(retry()), doc.activeElement === $("detail").byClass("file-head")[0]]);
"""
    assert run(script) == [False, True]


def test_a_section_collapses() -> None:
    body = detail_of([report(1)])
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
$("detail").byClass("file-head")[0].click();
done([$("detail").byClass("ln").length, $("detail").byClass("file-head")[0].getAttribute("aria-expanded")]);
"""
    assert run(script) == [0, "false"]


def test_the_detail_refetches_only_when_status_or_attempts_change() -> None:
    script = f"""
state.events = [{json.dumps(ev(7, status="pending", attempts=0))}];
net.reply(200, {json.dumps(detail_of([]))});
await detail.syncDetail();
const live = $("detail-live").textContent;
await detail.syncDetail();
state.events[0].attempts = 1;
net.reply(200, {json.dumps(detail_of([]))});
await detail.syncDetail();
done([net.calls.map((c) => c.path), live.startsWith("Loaded event: proj, ")]);
"""
    assert run(script) == [["/api/events/7", "/api/events/7"], True]


def test_an_unknown_event_falls_back_to_the_newest_with_a_note() -> None:
    script = f"""
state.events = [{json.dumps(ev(9))}];
state.selected = 4;
net.reply(404, {{ error: "unknown event 4" }});
net.reply(200, {json.dumps(detail_of([]) | {"id": 9})});
await detail.syncDetail();
done([state.selected, net.calls.map((c) => c.path), texts($("detail"), "note")[0], urls.at(-1)]);
"""
    assert run(script) == [
        None,
        ["/api/events/4", "/api/events/9"],
        "That event is no longer in history",
        "",
    ]


def test_a_failed_fetch_offers_retry() -> None:
    script = f"""
state.events = [{json.dumps(ev(9))}];
net.reply(500, {{ error: "internal error" }});
await detail.syncDetail();
const shown = $("detail").textContent;
net.reply(200, {json.dumps(detail_of([]) | {"id": 9})});
$("detail").byClass("link")[0].click();
await settle();
done([shown, state.detailError, net.calls.length]);
"""
    assert run(script) == ["Couldn't load this event · Retry", "", 2]


# -- dashboard.js ------------------------------------------------------------------


def test_cards_show_status_count_and_last_change() -> None:
    dash = {
        "projects": [
            {
                "name": "web",
                "status": "subscribed",
                "watched_count": 12,
                "watched_exact": True,
                "last_change_at": T - 180,
            },
            {
                "name": "api",
                "status": "pending",
                "watched_count": None,
                "watched_exact": None,
                "last_change_at": None,
            },
            {
                "name": "big",
                "status": "error",
                "error": "watch failed",
                "watched_count": 500,
                "watched_exact": False,
                "last_change_at": None,
            },
        ],
        "missing": [{"name": "gone", "path": "/x"}],
    }
    script = f"""
state.dash = {json.dumps(dash)};
dashboard.renderCards();
const cards = $("cards");
done([texts(cards, "name"), texts(cards, "state"), texts(cards, "meta")]);
"""
    assert run(script) == [
        ["api", "big", "gone", "web"],
        ["Starting…", "Error: watch failed", "Folder missing", "Watching"],
        [
            "counting… · no changes yet",
            "500+ files · no changes yet",
            "path does not exist",
            "12 files · 3 min ago",
        ],
    ]


@pytest.mark.parametrize(
    ("count", "exact", "text"),
    [(1, True, "1 file"), (1, False, "1+ files"), (0, True, "0 files"), (500, False, "500+ files")],
)
def test_watched_text_never_says_one_plus_file(count: int, exact: bool, text: str) -> None:
    script = f"""
done(dashboard.watchedText({{ status: "subscribed", watched_count: {count}, watched_exact: {json.dumps(exact)} }}));
"""
    assert run(script) == text


def test_a_card_toggles_the_project_filter() -> None:
    script = """
state.dash = { projects: [{ name: "web", status: "subscribed" }], missing: [] };
dashboard.renderCards();
const card = $("cards").children[0].children[0];  // the card's button, above its snooze controls
card.click();
const on = [state.filters.project, urls.at(-1)];
dashboard.renderCards();
const pressed = card.getAttribute("aria-pressed");
card.click();
done([on, pressed, state.filters.project]);
"""
    assert run(script) == [["web", "project=web"], "true", ""]


def test_the_header_sentence_and_the_attention_link() -> None:
    script = f"""
dom.icon("pending").setAttribute("id", "status-icon");  // index.html's <svg id="status-icon">
state.dash = {{ daemon: {{ connected: true }}, telegram: {{}}, counts: {{}}, projects: [], missing: [{{ name: "gone" }}] }};
state.lastPollOk = {NOW} - 20000;
dashboard.renderHeader();
const link = $("status-attention");
const first = [link.hidden, link.textContent, $("status-text").hidden, $("refreshed").textContent];
const live = [$("status-live").textContent];
state.dash.missing = [];
state.lastPollOk = {NOW};
dashboard.renderHeader();
live.push($("status-live").textContent);
let writes = 0;
const node = $("status-live");
const write = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(Object.getPrototypeOf(node)), "textContent");
Object.defineProperty(node, "textContent", {{ get: () => write.get.call(node), set: (v) => {{ writes += 1; write.set.call(node, v); }} }});
state.dash.projects = [{{ name: "a", status: "subscribed", last_change_at: {T} - 120 }}];
dashboard.renderHeader();
state.dash.projects[0].last_change_at = {T} - 180;  // only the "last change" clause moves
dashboard.renderHeader();
done([first, $("status-text").textContent, link.hidden, $("refreshed").textContent, live, writes,
      $("status-line").getAttribute("aria-live")]);
"""
    assert run(script) == [
        [False, "1 project needs attention", True, "Updated 20 s ago"],
        "All good · watching 1 project · last change 3 min ago",
        True,
        "",
        ["1 project needs attention", "All good · watching 0 projects"],
        1,
        None,
    ]


def test_no_projects_offers_the_add_form() -> None:
    script = """
let added = 0;
dashboard.initDashboard({ addProject: () => { added += 1; } });
state.dash = { projects: [], missing: [] };
dashboard.renderDashEmpty();
const box = $("dash-empty");
[...box.children].find((n) => n.textContent === "Add your first project").click();
done([box.hidden, box.children[0].textContent, added]);
"""
    assert run(script) == [False, "No projects yet", 1]


def test_send_test_notification_reports_the_outcome() -> None:
    script = """
const button = doc.createElement("button");
const message = doc.createElement("p");
net.reply(422, { error: "Telegram rejected the bot token: it was probably revoked in @BotFather. Use Replace bot." });
await dashboard.sendTest(button, message);
const failed = [message.textContent, message.className, button.disabled];
net.reply(200, { ok: true });
await dashboard.sendTest(button, message);
done([failed, message.textContent]);
"""
    assert run(script) == [
        # The server's sentence alone, without the status label.
        [
            "Telegram rejected the bot token: it was probably revoked in @BotFather."
            " Use Replace bot.",
            "message error",
            False,
        ],
        "Sample notification sent.",
    ]


def test_send_test_notification_waits_longer_than_other_requests() -> None:
    """Sending goes through Telegram (with its own retries): 60 s, not 15 s."""
    script = """
const timeouts = [];
const timeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = (ms) => { timeouts.push(ms); return timeout(ms); };
net.reply(200, { ok: true });
await dashboard.sendTest(doc.createElement("button"), doc.createElement("p"));
done([timeouts, net.calls[0].path]);
"""
    assert run(script) == [[60000], "/api/test-notification"]


# -- the setup banner (bot onboarding spec §3) ----------------------------------------


@pytest.mark.parametrize(
    ("telegram", "counts", "text"),
    [
        ({"configured": False}, {"held": 3}, "Telegram isn't set up — 3 changes are waiting."),
        ({"configured": False}, {"held": 1}, "Telegram isn't set up — 1 change is waiting."),
        ({"configured": False}, {}, "Telegram isn't set up — nothing is sent until it is."),
        ({"configured": True}, {"held": 3}, ""),
        ({}, {"held": 3}, ""),  # a daemon that doesn't report it: no banner
    ],
)
def test_the_setup_banner_counts_the_held_changes(
    telegram: dict[str, Any], counts: dict[str, int], text: str
) -> None:
    script = f"""
state.dash = {{ telegram: {json.dumps(telegram)}, counts: {json.dumps(counts)}, projects: [], missing: [] }};
dashboard.renderSetupBanner();
done([dashboard.setupBannerText(state.dash), $("setup-text").textContent, $("setup-banner").hidden]);
"""
    assert run(script) == [text, text, not text]


def test_set_up_opens_the_telegram_card() -> None:
    script = """
const views = [];
dashboard.initDashboard({ showView: (view, options) => views.push([view, options]) });
dashboard.wireDashboard();
$("setup-open").click();
done(views);
"""
    assert run(script) == [["settings", {"section": "telegram"}]]
