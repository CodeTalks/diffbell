"""The page's addresses for Settings → Telegram and the guide (bot onboarding spec
§3): ?view=settings&section=telegram and ?view=guide&step=<id> survive a reload,
unknown values are dropped, and a tab switch drops the step."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.web.node import needs_node, run_js
from tests.web.test_js_main import BOOT, SESSION

pytestmark = needs_node


def round_trip(search: str) -> Any:
    script = f"""
const {{ state }} = await import(js("state.js"));
const feed = await import(js("feed.js"));
feed.readUrl({json.dumps(search)});
const first = [state.view, state.section, state.step, feed.urlQuery()];
state.view = "dashboard";
feed.readUrl(`?${{feed.urlQuery()}}`);
state.view = "dashboard";
feed.readUrl(`?${{first[3]}}`);
done([...first, feed.urlQuery()]);
"""
    return run_js(script)


@pytest.mark.parametrize(
    ("search", "view", "section", "step", "query"),
    [
        (
            "?view=settings&section=telegram",
            "settings",
            "telegram",
            "",
            "view=settings&section=telegram",
        ),
        ("?view=guide", "guide", "", "", "view=guide"),
        ("?view=guide&step=start", "guide", "", "start", "view=guide&step=start"),
        *[
            (f"?view=settings&section={s}", "settings", s, "", f"view=settings&section={s}")
            for s in ("notifications", "schedule", "patterns", "page")
        ],
        # Unknown values are dropped.
        ("?view=settings&section=other", "settings", "", "", "view=settings"),
        ("?view=settings&section=tools", "settings", "", "", "view=settings"),
        ("?view=guide&step=No_Such!", "guide", "", "", "view=guide"),
        ("?view=guide&step=" + "a" * 33, "guide", "", "", "view=guide"),
        ("?view=nowhere&step=start", "dashboard", "", "", ""),
        # Each belongs to its own view only.
        ("?view=settings&step=start", "settings", "", "", "view=settings"),
        ("?view=guide&section=telegram", "guide", "", "", "view=guide"),
        ("?section=telegram&step=start", "dashboard", "", "", ""),
    ],
)
def test_read_and_write_keep_view_section_and_step(
    search: str, view: str, section: str, step: str, query: str
) -> None:
    assert round_trip(search) == [view, section, step, query, query]


def boot(search: str, script: str) -> Any:
    page = BOOT.replace('search: "?event=7"', f"search: {json.dumps(search)}")
    page = page.replace("__REPLIES__", SESSION)  # later requests stay unanswered
    return run_js(page + script)


def test_the_guide_is_a_fourth_view_without_a_tab() -> None:
    script = """
const { state } = await import(js("state.js"));
const shown = [
  $("view-guide").hidden,
  tabs.map((t) => t.getAttribute("aria-selected")),
  tabs.map((t) => t.tabIndex),
  ["dashboard", "projects", "settings"].map((v) => $(`view-${v}`).hidden),
  replaced.at(-1),
];
tabs[2].dispatch("click");
const afterTab = [$("view-guide").hidden, $("view-settings").hidden, state.step, replaced.at(-1)];
done([shown, afterTab]);
"""
    assert boot("?view=guide&step=start", script) == [
        [
            False,
            ["false", "false", "false"],
            [0, -1, -1],
            [True, True, True],
            "?view=guide&step=start",
        ],
        [True, False, "", "?view=settings"],
    ]


def test_back_to_settings_opens_the_telegram_section() -> None:
    script = """
const { state } = await import(js("state.js"));
const event = $("guide-back").dispatch("click");
done([event.defaultPrevented, state.view, state.section, $("view-guide").hidden, replaced.at(-1)]);
"""
    assert boot("?view=guide", script) == [
        True,
        "settings",
        "telegram",
        True,
        "?view=settings&section=telegram",
    ]


def test_back_to_settings_focuses_the_telegram_heading() -> None:
    script = """
$("guide-back").dispatch("click", { button: 0 });
done(doc.activeElement === $("tg-title"));
"""
    assert boot("?view=guide", script) is True


@pytest.mark.parametrize(
    "init",
    [{"metaKey": True}, {"ctrlKey": True}, {"shiftKey": True}, {"altKey": True}, {"button": 1}],
)
def test_a_modified_back_click_is_left_to_the_browser(init: dict[str, Any]) -> None:
    script = f"""
const {{ state }} = await import(js("state.js"));
const event = $("guide-back").dispatch("click", {json.dumps(init)});
done([event.defaultPrevented, state.view, $("view-guide").hidden]);
"""
    assert boot("?view=guide", script) == [False, "guide", False]


def test_a_tab_switch_drops_the_settings_section() -> None:
    script = """
tabs[0].dispatch("click");
const first = replaced.at(-1);
tabs[2].dispatch("click");
done([first, replaced.at(-1)]);
"""
    assert boot("?view=settings&section=telegram", script) == ["/", "?view=settings"]


def test_the_index_opens_a_section_in_place() -> None:
    script = """
const { state } = await import(js("state.js"));
const link = doc.createElement("a");
link.dataset.section = "patterns";
$("s-index").append(link);
const event = link.dispatch("click", { button: 0 });
done([event.defaultPrevented, state.section, replaced.at(-1),
      doc.activeElement === $("s-patterns-title"), $("s-patterns").scrolled]);
"""
    assert boot("?view=settings", script) == [
        True,
        "patterns",
        "?view=settings&section=patterns",
        True,
        1,
    ]


def test_a_click_on_a_child_of_the_link_opens_the_section() -> None:
    script = """
const { state } = await import(js("state.js"));
const link = doc.createElement("a");
link.dataset.section = "patterns";
const inner = doc.createElement("span");
link.append(inner);
$("s-index").append(link);
const event = inner.dispatch("click", { button: 0 });
done([event.defaultPrevented, state.section, replaced.at(-1)]);
"""
    assert boot("?view=settings", script) == [
        True,
        "patterns",
        "?view=settings&section=patterns",
    ]


@pytest.mark.parametrize("init", [{"metaKey": True}, {"ctrlKey": True}, {"button": 1}])
def test_a_modified_index_click_is_left_to_the_browser(init: dict[str, Any]) -> None:
    script = f"""
const {{ state }} = await import(js("state.js"));
const link = doc.createElement("a");
link.dataset.section = "page";
$("s-index").append(link);
const event = link.dispatch("click", {json.dumps(init)});
done([event.defaultPrevented, state.section]);
"""
    assert boot("?view=settings", script) == [False, ""]


def test_a_click_beside_the_links_does_nothing() -> None:
    script = """
const { state } = await import(js("state.js"));
const event = $("s-index").dispatch("click", { button: 0 });
done([event.defaultPrevented, state.section]);
"""
    assert boot("?view=settings", script) == [False, ""]
