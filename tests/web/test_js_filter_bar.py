"""The filter bar's polish under node: empty date inputs read "Any date" (Safari
draws today's date in grey in an empty one), and the Clear button counts the
active filters, in its badge and its accessible name."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from diffbell.web.app import STATIC_DIR
from tests.web import test_js_dashboard as dash_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_main import BOOT, FIRST

EMPTY = {"project": "", "status": "", "from": "", "to": "", "since": "", "until": "", "q": ""}

# The date inputs' classes, the badge (text, hidden) and the button's aria-label.
SNAPSHOT = """
const snap = () => [
  $("f-from").getAttribute("class") || "",
  $("f-to").getAttribute("class") || "",
  $("f-clear-count").textContent,
  $("f-clear-count").hidden,
  $("f-clear").getAttribute("aria-label"),
];
"""


def html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def css() -> str:
    return (STATIC_DIR / "app.css").read_text(encoding="utf-8")


# -- counting ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filters", "count"),
    [
        ({}, 0),
        ({"project": "web"}, 1),
        ({"status": "failed"}, 1),
        ({"q": "token"}, 1),
        ({"from": "2026-10-01"}, 1),
        ({"from": "2026-10-01", "to": "2026-10-09"}, 1),  # the dates count once
        ({"from": "2026-10-09", "to": "2026-10-09", "since": "1", "until": "2"}, 1),
        ({"since": "1", "until": "2"}, 1),
        ({"project": "web", "status": "attention", "to": "2026-10-09", "q": "x"}, 4),
    ],
)
@needs_node
def test_active_filters_are_counted_with_the_dates_as_one(
    filters: dict[str, str], count: int
) -> None:
    script = f"""
Object.assign(state.filters, {json.dumps({**EMPTY, **filters})});
done([feed.activeFilterCount(), feed.filtersActive()]);
"""
    assert dash_js.run(script) == [count, count > 0]


# -- the inputs after a programmatic change (syncFilterInputs) -----------------------


@needs_node
def test_sync_marks_empty_dates_and_counts_the_filters() -> None:
    script = (
        SNAPSHOT
        + """
const out = [];
feed.syncFilterInputs();
out.push(snap());
feed.setFilter({ project: "web", from: "2026-10-01" });
out.push(snap());
feed.setFilter({ to: "2026-10-09", status: "failed" });
out.push(snap());
feed.clearFilters();
out.push(snap());
done(out);
"""
    )
    assert dash_js.run(script) == [
        ["date-empty", "date-empty", "", True, None],
        ["", "date-empty", "2 filters", False, "Clear 2 filters"],
        ["", "", "3 filters", False, "Clear 3 filters"],
        ["date-empty", "date-empty", "", True, None],
    ]


@needs_node
def test_one_filter_reads_in_the_singular() -> None:
    script = SNAPSHOT + 'feed.setFilter({ q: "token" });\ndone(snap().slice(2));'
    assert dash_js.run(script) == ["1 filter", False, "Clear 1 filter"]


@needs_node
def test_a_strip_day_fills_both_dates_and_counts_them_once() -> None:
    script = (
        SNAPSHOT
        + """
feed.initFeed({ pushUrl: () => {} });
dashboard.showDay("web", { day: "2026-10-09", since: 1791504000, until: 1791590400 });
done(snap());
"""
    )
    assert dash_js.run(script) == ["", "", "2 filters", False, "Clear 2 filters"]


@needs_node
def test_restore_url_resyncs_the_dates_and_the_count() -> None:
    script = (
        SNAPSHOT
        + """
feed.setFilter({ from: "2026-10-01", q: "x" });
const before = snap();
feed.restoreUrl("?status=failed");
done([before, snap()]);
"""
    )
    assert dash_js.run(script) == [
        ["", "date-empty", "2 filters", False, "Clear 2 filters"],
        ["date-empty", "date-empty", "1 filter", False, "Clear 1 filter"],
    ]


# -- main.js wiring ----------------------------------------------------------------


def boot(script: str, search: str = "?event=7") -> Any:
    program = BOOT.replace("__REPLIES__", FIRST).replace(
        'search: "?event=7"', f"search: {json.dumps(search)}"
    )
    return run_js(program + SNAPSHOT + script)


@needs_node
def test_boot_marks_the_dates_from_the_url() -> None:
    assert boot("done(snap());", "?from=2026-10-01&project=proj") == [
        "",
        "date-empty",
        "2 filters",
        False,
        "Clear 2 filters",
    ]


@needs_node
def test_typing_a_date_shows_it_at_once() -> None:
    """On input (before change commits the filter) the class follows the value."""
    script = """
const to = $("f-to");
to.value = "2026-10-09";
to.dispatch("input");
const typed = snap()[1];
to.value = "";
to.dispatch("input");
done([typed, snap()[1]]);
"""
    assert boot(script) == ["", "date-empty"]


@needs_node
def test_back_resyncs_the_dates_and_the_count() -> None:
    script = """
const { setFilter } = await import(js("feed.js"));
setFilter({ from: "2026-10-01", to: "2026-10-02" });
const before = snap();
window.location.search = "";
for (const fn of winListeners.popstate) fn();
done([before, snap()]);
"""
    assert boot(script) == [
        ["", "", "1 filter", False, "Clear 1 filter"],
        ["date-empty", "date-empty", "", True, None],
    ]


# -- editing state (Safari: focus sits on an inner segment or the picker) ------------

EDIT = """
const field = $("f-to-field");
field.append($("f-to")); // the page's markup: the input inside its wrapper
const editing = () => field.className.split(" ").includes("date-editing");
"""


@needs_node
def test_focusin_and_pointerdown_mark_the_field_as_edited() -> None:
    script = (
        EDIT
        + """
const out = [editing()];
$("f-to").focus(); // focusin bubbles to the wrapper
out.push(editing());
$("f-to").blur();
$("f-to").dispatch("focusout", { relatedTarget: null });
out.push(editing());
$("f-to").dispatch("pointerdown");
out.push(editing());
done(out);
"""
    )
    assert boot(script) == [False, True, False, True]


@needs_node
def test_focus_moving_inside_the_field_keeps_it_edited() -> None:
    script = (
        EDIT
        + """
const inner = doc.createElement("span");
field.append(inner);
$("f-to").focus();
$("f-to").dispatch("focusout", { relatedTarget: inner });
done(editing());
"""
    )
    assert boot(script) is True


@needs_node
def test_focus_leaving_the_field_ends_editing_and_resyncs() -> None:
    script = (
        EDIT
        + """
$("f-to").focus();
$("f-to").value = "2026-10-09"; // a picked date not yet committed by change
$("f-to").blur();
$("f-to").dispatch("focusout", { relatedTarget: $("f-clear") });
done([editing(), snap()[1]]);
"""
    )
    assert boot(script) == [False, ""]


@needs_node
def test_change_ends_editing_only_once_the_input_lost_focus() -> None:
    script = (
        EDIT
        + """
$("f-to").focus();
$("f-to").value = "2026-10-09";
$("f-to").dispatch("change");
const focused = editing();
$("f-to").dispatch("pointerdown");
$("f-to").blur();
$("f-to").dispatch("change");
done([focused, editing()]);
"""
    )
    assert boot(script) == [True, False]


@needs_node
def test_clear_while_not_focused_shows_any_date_again() -> None:
    script = (
        EDIT
        + """
$("f-to").focus();
$("f-to").value = "2026-10-09";
$("f-to").dispatch("change");
$("f-to").blur();
$("f-to").dispatch("focusout", { relatedTarget: $("f-clear") });
const set = snap()[1];
$("f-clear").click();
done([set, snap()[1], editing(), $("f-to").value]);
"""
    )
    assert boot(script) == ["", "date-empty", False, ""]


# -- the markup and the stylesheet ---------------------------------------------------


@pytest.mark.parametrize("field", ["f-from", "f-to"])
def test_each_date_input_keeps_its_label_and_has_a_hidden_overlay(field: str) -> None:
    label = "From" if field == "f-from" else "To"
    pattern = (
        rf'<label>{label} <span class="date-field" id="{field}-field">'
        rf'<input type="date" id="{field}">'
        r'<span class="date-any" aria-hidden="true">Any date</span></span></label>'
    )
    assert re.search(pattern, html())


def test_the_clear_button_holds_a_hidden_count_badge() -> None:
    assert (
        '<button type="button" id="f-clear">Clear'
        '<span class="filter-count" id="f-clear-count" aria-hidden="true" hidden></span></button>'
    ) in html()


def rules() -> dict[str, str]:
    from tests.web.test_static import rules as static_rules

    return static_rules()


HIDDEN_TEXT = ".date-field:not(:focus-within):not(.date-editing) input.date-empty"
SAFARI_PARTS = [
    "::-webkit-datetime-edit",
    "::-webkit-datetime-edit-fields-wrapper",
    "::-webkit-datetime-edit-text",
    "::-webkit-datetime-edit-month-field",
    "::-webkit-datetime-edit-day-field",
    "::-webkit-datetime-edit-year-field",
]


def test_an_empty_idle_date_hides_its_text_and_shows_the_overlay() -> None:
    r = rules()
    assert "position: relative;" in r[".date-field"]
    overlay = r[".date-any"]
    assert "position: absolute;" in overlay
    assert "pointer-events: none;" in overlay
    assert "color: var(--muted);" in overlay  # a theme token: right in dark mode too
    assert r[HIDDEN_TEXT] == "color: transparent;"


@pytest.mark.parametrize("part", SAFARI_PARTS)
def test_every_safari_date_part_is_transparent_in_a_rule_of_its_own(part: str) -> None:
    """Safari draws its placeholder date in these parts; one rule each, so an
    unsupported pseudo-element can't drop the others."""
    assert rules()[HIDDEN_TEXT + part] == "color: transparent;"


def test_the_overlay_hides_while_the_field_is_edited_not_on_input_focus_alone() -> None:
    """Safari keeps focus on an inner segment or its picker: the wrapper's
    :focus-within and the page's .date-editing hide the overlay, not input:focus."""
    hide = (
        ".date-field:focus-within .date-any, .date-field.date-editing .date-any, "
        ".date-field input:not(.date-empty) + .date-any"
    )
    assert rules()[hide] == "display: none;"
    assert not re.search(r"input:focus\s*\+\s*\.date-any", css())
    assert "date-empty:not(:focus)" not in css()


def test_the_count_badge_uses_theme_tokens() -> None:
    badge = rules()[".filter-count"]
    assert "var(--" in badge
    assert "#" not in badge  # no fixed colour: the tokens switch in dark mode
