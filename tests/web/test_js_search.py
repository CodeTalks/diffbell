"""Search inside diffs on the page (diff readability spec §2.3, §7 "Page"): the
placeholder and the one-character hint, the feed's "in diff" tag, <mark> in the
detail, auto-opened files and "Show all N lines (k matches)"."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from diffbell.web.app import STATIC_DIR
from tests.web import test_js_main as main_js
from tests.web.node import needs_node
from tests.web.test_js_dashboard import detail_of, ev, report, run

pytestmark = needs_node


def test_the_search_box_says_it_searches_diffs() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    box = re.search(r'<input type="search" id="f-q"[^>]*>', page)
    assert box is not None
    assert 'placeholder="Search files and diffs…  (/)"' in box.group(0)
    assert 'aria-describedby="f-q-hint"' in box.group(0)
    assert '<span class="hint" id="f-q-hint" hidden></span>' in page
    # The hint is outside the <label>, so it is not part of the input's accessible
    # name; aria-describedby links it instead.
    assert '<div class="grow search-field">' in page
    label = re.search(r"<label[^>]*>Search.*?</label>", page, re.S)
    assert label is not None
    assert "f-q-hint" not in label.group(0).replace('aria-describedby="f-q-hint"', "")


@pytest.mark.parametrize(
    ("value", "hint"),
    [
        ("", ""),
        ("a", "2+ characters search inside diffs"),
        (" a ", "2+ characters search inside diffs"),
        ("😀", "2+ characters search inside diffs"),  # one code point
        ("ab", ""),
        ("  ", ""),
    ],
)
def test_the_hint_shows_for_exactly_one_character(value: str, hint: str) -> None:
    script = f"""
$("f-q").value = {json.dumps(value)};
feed.syncSearchHint();
done([feed.searchHint({json.dumps(value)}), $("f-q-hint").textContent, $("f-q-hint").hidden]);
"""
    assert run(script) == [hint, hint, not hint]


def test_typing_updates_the_hint_at_once() -> None:
    script = """
$("f-q").value = "p";
$("f-q").dispatch("input");
const one = [$("f-q-hint").textContent, $("f-q-hint").hidden];
$("f-q").value = "pg";
$("f-q").dispatch("input");
done([one, $("f-q-hint").hidden]);
"""
    assert main_js.boot(script) == [["2+ characters search inside diffs", False], True]


def test_a_row_matched_in_its_diff_has_an_in_diff_tag_after_its_file_count() -> None:
    events = [ev(2, q_in_diff=True), ev(1, q_in_diff=False)]
    script = f"""
state.events = {json.dumps(events)};
state.total = 2;
feed.renderFeed();
done($("feed").byClass("size").map((s) => [...s.children].map((c) => [c.className, c.hidden])));
"""
    tagged, plain = run(script)
    assert [c for c, _ in tagged] == ["files", "in-diff", "add", "del"]
    assert tagged[1] == ["in-diff", False] and plain[1] == ["in-diff", True]


SEARCHED = detail_of(
    [
        report(0, path="compose.yml", lines=["-  image: postgres:15", "+  image: Postgres:16"]),
        report(0, path="notes.txt", lines=["+nothing here"]),
    ]
)


def test_the_detail_marks_every_match_with_mark_elements() -> None:
    body = {**SEARCHED, "telegram_lines": 20, "telegram_files": 2}
    script = f"""
state.filters.q = "postgres";
state.detail = {json.dumps(body)};
detail.renderDetail();
done($("detail").all().filter((n) => n.tagName === "MARK").map((m) => [m.textContent, m.className]));
"""
    # ASCII case-insensitive; a mark keeps the other classes of its piece (the case
    # change is a word change too).
    assert run(script) == [["postgres", "wdel"], ["Postgres", "wadd"]]


def test_a_one_character_search_marks_nothing() -> None:
    body = {**SEARCHED, "telegram_lines": 20, "telegram_files": 2}
    script = f"""
state.filters.q = "p";
state.detail = {json.dumps(body)};
detail.renderDetail();
done($("detail").all().filter((n) => n.tagName === "MARK").length);
"""
    assert run(script) == 0


def test_a_collapsed_file_with_a_match_opens_until_toggled() -> None:
    body = {**SEARCHED, "telegram_lines": 20, "telegram_files": 2}
    script = f"""
state.detail = {json.dumps(body)};
state.collapsed.add("7:0");
state.collapsed.add("7:1");
detail.renderDetail();
const heads = () => $("detail").byClass("file-head").map((b) => b.getAttribute("aria-expanded"));
const before = heads();
state.filters.q = "postgres";
detail.renderDetail();
const searching = heads();  // the matching file opens, the other stays collapsed
$("detail").byClass("file-head")[0].click();  // the person closes it again: that wins
const toggled = heads();
detail.renderDetail();
const again = heads();
state.filters.q = "image";  // a new search: earlier toggles are forgotten
detail.renderDetail();
const next = heads();
state.filters.q = "";
detail.renderDetail();
done([before, searching, toggled, again, next, heads()]);
"""
    assert run(script) == [
        ["false", "false"],
        ["true", "false"],
        ["false", "false"],
        ["false", "false"],
        ["true", "false"],
        ["false", "false"],
    ]


def test_show_all_counts_the_matches_behind_the_divider() -> None:
    lines = ["+a", "+needle", "+needle needle", "+b"]
    body = detail_of([report(0, path="x.txt", lines=lines)])  # telegram_lines 2
    script = f"""
state.filters.q = "needle";
state.detail = {json.dumps(body)};
detail.renderDetail();
const label = () => $("detail").byClass("link").find((b) => b.textContent.startsWith("Show all")).textContent;
const many = label();
state.filters.q = "b";
detail.renderDetail();
const one = label();
state.filters.q = "zz";
detail.renderDetail();
done([many, one, label()]);
"""
    # "+needle" sits above the divider; the rest holds two matches. "b" is one
    # character: no marks, no count.
    assert run(script) == ["Show all 4 lines (2 matches)", "Show all 4 lines", "Show all 4 lines"]
    body["reports"][0]["lines"] = ["+a", "+b", "+xneedle", "+c"]
    script = f"""
state.filters.q = "needle";
state.detail = {json.dumps(body)};
detail.renderDetail();
done($("detail").byClass("link").find((b) => b.textContent.startsWith("Show all")).textContent);
"""
    assert run(script) == "Show all 4 lines (1 match)"


def test_a_new_search_re_renders_the_detail_without_refetching() -> None:
    body: dict[str, Any] = {**SEARCHED, "telegram_lines": 20, "telegram_files": 2}
    script = f"""
state.events = [{json.dumps(ev(7, status="sent"))}];
net.reply(200, {json.dumps(body)});
await detail.syncDetail();
const marks = () => $("detail").all().filter((n) => n.tagName === "MARK").length;
const before = marks();
state.filters.q = "image";
await detail.syncDetail();
done([before, marks(), net.calls.length]);
"""
    assert run(script) == [0, 2, 1]


def test_a_redacted_or_truncated_file_with_the_term_stays_collapsed_and_unmarked() -> None:
    files = [
        report(0, path=".env.local", redacted=True, note=None, lines=["+needle"]),
        report(0, path="big.txt", truncated=True, lines=["+needle"]),
    ]
    body = {**detail_of(files), "telegram_lines": 20, "telegram_files": 2}
    script = f"""
state.detail = {json.dumps(body)};
state.collapsed.add("7:0");
state.collapsed.add("7:1");
state.filters.q = "needle";
detail.renderDetail();
const heads = () => $("detail").byClass("file-head").map((b) => b.getAttribute("aria-expanded"));
const marks = () => $("detail").all().filter((n) => n.tagName === "MARK").length;
const closed = [heads(), marks()];
state.collapsed.clear();  // opened by hand: still no lines, no marks
detail.renderDetail();
done([closed, heads(), marks(), $("detail").byClass("ln").length]);
"""
    assert run(script) == [[["false", "false"], 0], ["true", "true"], 0, 0]


def test_a_same_event_poll_keeps_a_file_the_person_closed_during_a_search() -> None:
    body: dict[str, Any] = {**SEARCHED, "telegram_lines": 20, "telegram_files": 2}
    script = f"""
state.events = [{json.dumps(ev(7, status="pending", attempts=0))}];
state.filters.q = "postgres";
net.reply(200, {json.dumps(body)});
await detail.syncDetail();
const heads = () => $("detail").byClass("file-head").map((b) => b.getAttribute("aria-expanded"));
const opened = heads();
$("detail").byClass("file-head")[0].click();  // closed during the search
const closed = heads();
await detail.syncDetail();  // a poll, nothing changed: no refetch
const polled = heads();
state.events[0].attempts = 1;  // a poll that refetches the same event
net.reply(200, {json.dumps(body)});
await detail.syncDetail();
done([opened, closed, polled, heads(), net.calls.length]);
"""
    assert run(script) == [
        ["true", "true"],
        ["false", "true"],
        ["false", "true"],
        ["false", "true"],
        2,
    ]


def test_a_match_only_behind_the_divider_opens_the_file_and_counts_it() -> None:
    body = detail_of([report(0, path="x.txt", lines=["+a", "+b", "+needle", "+c"])])
    script = f"""
state.detail = {json.dumps(body)};
state.collapsed.add("7:0");
state.filters.q = "needle";
detail.renderDetail();
const pane = $("detail");
done([
  pane.byClass("file-head")[0].getAttribute("aria-expanded"),
  pane.all().filter((n) => n.tagName === "MARK").length,  // the match is not shown yet
  pane.byClass("link").find((b) => b.textContent.startsWith("Show all")).textContent,
]);
"""
    assert run(script) == ["true", 0, "Show all 4 lines (1 match)"]
