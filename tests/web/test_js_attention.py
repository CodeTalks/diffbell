"""Needs attention (Plan 7 spec §3.3) and the empty states (§3.4) under node: the
Status choice, the open problems above the results, the header link, and one clean
message when nothing matches."""

from __future__ import annotations

import json
import re
from typing import Any

from diffbell.web.app import STATIC_DIR
from tests.web import test_js_dashboard as dash_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_dashboard import NOW, ev
from tests.web.test_js_health import problem

pytestmark = needs_node

FIX = "watchman watch-del '/w/a b' ; watchman watch-project '/w/a b'"
LOST = problem("project:web", "serious", "project web: watch lost", project="web", fix=FIX)
CONFIG = problem("config", "warning", "config: an unknown key")
SETUP = """
const copied = [];
dashboard.initDashboard({ copy: async (text) => { copied.push(text); } });
const rows = () => $("attention").byClass("attention-row");
const layout = () => [$("feed").hidden, $("detail").hidden, $("feed-split").className];
"""


def run(script: str) -> Any:
    return dash_js.run(SETUP + script)


def dash(problems: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "projects": [{"name": "web", "status": "subscribed"}],
            "missing": [],
            "problems": problems,
        }
    )


def test_the_status_filter_offers_needs_attention() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    select = re.search(r'<select id="f-status">(.*?)</select>', page, re.S)
    assert select and '<option value="attention">Needs attention</option>' in select.group(1)
    script = """
feed.readUrl("?status=attention");
const kept = state.filters.status;
done([kept, feed.eventQuery(0, 50)]);
"""
    assert run(script) == ["attention", "limit=50&offset=0&status=attention"]


def test_the_open_problems_are_listed_above_the_results_while_it_is_selected() -> None:
    script = f"""
state.dash = {dash([LOST, CONFIG])};
state.filters.status = "attention";
state.events = [{json.dumps(ev(3, status="failed", last_error="boom"))}];
state.total = 1;
feed.renderFeed();
const shown = [$("attention").hidden,
  rows().map((r) => [r.className, texts(r, "badge"), texts(r, "attention-message"), texts(r, "attention-fix")]),
  rows().map((r) => r.byClass("copy-fix").map((b) => [b.textContent, b.getAttribute("aria-label")]))];
rows()[0].byClass("copy-fix")[0].click();
await settle();
state.filters.status = "failed";
feed.renderFeed();
done([shown, copied, texts($("toasts"), "toast-text"), $("attention").hidden, rows().length]);
"""
    assert run(script) == [
        [
            False,
            [
                ["attention-row serious", ["⚠️"], ["project web: watch lost"], [FIX]],
                ["attention-row warning", ["ⓘ"], ["config: an unknown key"], []],
            ],
            [[["Copy", "Copy the fix: project web: watch lost"]], []],
        ],
        [FIX],
        ["Fix copied"],
        True,
        0,
    ]


def test_the_problems_box_is_labelled_in_the_page() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert (
        '<div class="attention" id="attention" role="group" aria-label="Open problems" hidden>'
        in page
    )


def test_rows_go_when_the_problems_resolve_while_it_stays_selected() -> None:
    script = f"""
state.dash = {dash([LOST, CONFIG])};
state.filters.status = "attention";
state.total = 0;
feed.renderFeed();
const before = [rows().length, $("feed-status").textContent];
state.dash = {dash([])};
feed.renderFeed();
done([before, rows().length, $("attention").hidden, $("feed-empty").elements()[0].textContent,
      $("feed-status").textContent]);
"""
    assert run(script) == [
        [2, "2 open problems"],  # the live region says what the view holds
        0,
        True,
        "Nothing needs attention.",
        "",
    ]


def test_a_missing_folder_is_listed_even_without_a_problem() -> None:
    """The header counts a missing project; the view must list it, not say
    "Nothing needs attention." A project with its own missing: problem is not
    listed twice."""
    gone = problem("missing:old", "warning", "project old: path /x does not exist", project="old")
    script = f"""
state.dash = {{ ...{dash([gone])}, missing: [{{ name: "gone", path: "/w/gone" }}, {{ name: "old", path: "/x" }}] }};
state.filters.status = "attention";
state.total = 0;
feed.renderFeed();
done([rows().map((r) => [r.className, texts(r, "attention-message"), r.byClass("copy-fix").length]),
      $("feed-empty").hidden, $("feed-status").textContent]);
"""
    assert run(script) == [
        [
            ["attention-row warning", ["project old: path /x does not exist"], 0],
            ["attention-row warning", ["project gone: path does not exist"], 0],
        ],
        True,
        "2 open problems",
    ]


def test_back_to_needs_attention_restores_the_view() -> None:
    script = f"""
state.dash = {dash([LOST])};
state.view = "settings";
feed.restoreUrl("?status=attention");
state.total = 0;
feed.renderFeed();
done([state.view, state.filters.status, rows().length, $("attention").hidden]);
"""
    assert run(script) == ["dashboard", "attention", 1, False]


def test_the_first_run_message_is_alone_too() -> None:
    script = f"""
state.dash = {dash([])};
state.total = 0;
feed.renderFeed();
done([$("feed-empty").elements()[0].textContent.startsWith("No changes yet"), layout()]);
"""
    assert run(script) == [True, [True, True, "split-feed single"]]


def test_a_poll_with_the_same_problems_keeps_the_rows() -> None:
    script = f"""
state.dash = {dash([LOST])};
state.filters.status = "attention";
state.total = 0;
feed.renderFeed();
const first = rows()[0];
state.dash = {dash([LOST])};
feed.renderFeed();
done([rows().length, rows()[0] === first]);
"""
    assert run(script) == [1, True]


def test_nothing_to_show_says_nothing_needs_attention() -> None:
    script = f"""
state.dash = {dash([])};
state.filters.status = "attention";
state.total = 0;
feed.renderFeed();
const box = $("feed-empty");
const empty = [box.hidden, box.elements().map((n) => n.textContent), $("attention").hidden, layout()];
state.dash = {dash([LOST])};
feed.renderFeed();
done([empty, box.hidden, rows().length, layout()]);
"""
    assert run(script) == [
        [
            False,
            ["Nothing needs attention.", "Clear filters"],
            True,
            [True, True, "split-feed single"],
        ],
        True,  # the problems are what needs attention: no "nothing" message
        1,
        [True, True, "split-feed single"],
    ]


def test_a_filter_without_matches_shows_one_message_and_no_empty_boxes() -> None:
    script = f"""
state.dash = {dash([])};
state.filters.q = "nope";
state.total = 0;
state.events = [];
feed.renderFeed();
detail.renderDetail();
const box = $("feed-empty");
const empty = [box.hidden, box.elements().map((n) => n.textContent), layout()];
state.events = [{json.dumps(ev(3))}];
state.total = 1;
feed.renderFeed();
done([empty, box.hidden, layout()]);
"""
    assert run(script) == [
        [
            False,
            ["No events match these filters.", "Clear filters"],
            [True, True, "split-feed single"],
        ],
        True,
        [False, False, "split-feed"],
    ]


def test_before_the_first_poll_nothing_is_hidden() -> None:
    script = """
feed.renderFeed();
done(layout());
"""
    assert run(script) == [False, False, "split-feed"]


def test_a_selected_event_outside_the_list_keeps_its_detail() -> None:
    """?event= of an event the filter doesn't list: its detail is not an empty box."""
    script = f"""
state.dash = {dash([])};
state.filters.status = "held";
state.total = 0;
state.detail = {json.dumps({**ev(3), "reports": [], "telegram_lines": 20, "telegram_files": 0})};
detail.renderDetail();
done(layout());
"""
    assert run(script) == [True, False, "split-feed"]


# -- the header link ----------------------------------------------------------------------

HEADER = f"""
let views = [];
dashboard.initDashboard({{ showView: (v) => views.push(v), now: () => {NOW} }});
dom.icon("pending").setAttribute("id", "status-icon");
dashboard.wireDashboard();
const pushed = [];
feed.initFeed({{ pushUrl: (q) => pushed.push(q) }});
const link = $("status-attention");
"""


def test_the_header_sentence_links_to_needs_attention() -> None:
    script = (
        HEADER
        + """
state.dash = { daemon: { connected: true }, telegram: {}, counts: {}, projects: [], missing: [{ name: "gone" }], problems: [] };
dashboard.renderHeader();
const shown = [link.hidden, link.textContent, $("status-text").hidden];
const replaced = urls.length;
link.click();
done([shown, views, state.filters.status, pushed, urls.length - replaced, refreshes]);
"""
    )
    assert dash_js.run(script) == [
        [False, "1 project needs attention", True],
        ["dashboard"],
        "attention",
        ["status=attention"],  # a new history entry: Back restores the cleared filters
        0,
        1,
    ]


def test_the_header_link_then_the_view_lists_the_missing_project() -> None:
    script = (
        HEADER
        + """
state.dash = { daemon: { connected: true }, telegram: {}, counts: {}, projects: [], missing: [{ name: "gone" }], problems: [] };
dashboard.renderHeader();
link.click();
state.total = 0;
feed.renderFeed();
done([texts($("attention"), "attention-message"), $("feed-empty").hidden]);
"""
    )
    assert dash_js.run(script) == [["project gone: path does not exist"], True]


def test_warnings_alone_do_not_link() -> None:
    script = (
        HEADER
        + f"""
state.dash = {{ daemon: {{ connected: true }}, telegram: {{}}, counts: {{}}, projects: [], missing: [],
  problems: [{json.dumps(CONFIG)}] }};
dashboard.renderHeader();
done([link.hidden, $("status-text").hidden]);
"""
    )
    assert dash_js.run(script) == [True, False]


def test_the_header_link_clears_every_other_filter() -> None:
    script = (
        HEADER
        + """
state.dash = { daemon: { connected: true }, telegram: {}, counts: {}, projects: [], missing: [{ name: "gone" }], problems: [] };
dashboard.renderHeader();
Object.assign(state.filters, { project: "web", status: "sent", from: "2026-10-01", to: "2026-10-02",
  since: "1790726400", until: "1790812800", q: "lock" });
link.click();
done([state.filters, pushed.at(-1)]);
"""
    )
    assert dash_js.run(script) == [
        {
            "project": "",
            "status": "attention",
            "from": "",
            "to": "",
            "since": "",
            "until": "",
            "q": "",
        },
        "status=attention",
    ]


def test_a_serious_problem_outside_any_project_links_there_too() -> None:
    config = problem("config", "serious", "config rejected, keeping the last good config: x")
    script = (
        HEADER
        + f"""
state.dash = {{ daemon: {{ connected: true }}, telegram: {{}}, counts: {{}}, projects: [], missing: [],
  problems: [{json.dumps(config)}] }};
dashboard.renderHeader();
const linked = [link.hidden, link.textContent];
state.dash.problems = [];
dashboard.renderHeader();
done([linked, link.hidden, $("status-text").textContent.startsWith("All good")]);
"""
    )
    assert dash_js.run(script) == [
        [False, "config rejected, keeping the last good config: x"],
        True,
        True,
    ]


def test_the_sentence_says_which_states_link() -> None:
    script = """
const m = await import(js("status.js"));
const base = { daemon: { connected: true }, telegram: {}, counts: {}, projects: [], missing: [], problems: [] };
const s = (dash) => m.statusSentence({ pollFailed: false, dash: { ...base, ...dash }, now: 0 }).link;
done([
  s({ missing: [{ name: "gone" }] }),
  s({ problems: [{ key: "config", severity: "serious", project: null, message: "m" }] }),
  s({}),
  s({ telegram: { last_error: "x" } }),
  m.statusSentence({ pollFailed: true, dash: null, now: 0 }).link,
]);
"""
    assert run_js(script) == [True, True, False, False, False]
