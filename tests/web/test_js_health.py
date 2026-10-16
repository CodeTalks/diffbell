"""The health registry on the page (health spec §6), under node: problem badges with
the Copy button and its toast, "alert not delivered yet", and the status sentence with
problems and warnings. The daily-heartbeat setting is in test_js_settings.py."""

from __future__ import annotations

import json
from typing import Any

import pytest

from diffbell.web.app import STATIC_DIR
from tests.web import test_js_dashboard as dash_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_dashboard import NOW, T

pytestmark = needs_node

FIX = "watchman watch-del '/w/a b;$x' ; watchman watch-project '/w/a b;$x'"


def problem(key: str, severity: str, message: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "severity": severity,
        "project": None,
        "message": message,
        "fix": None,
        "since": T - 60,
        "notified": False,
        "attempts": 0,
        "last_error": None,
    }
    row.update(fields)
    return row


LOST = problem("project:web", "serious", "project web: watch lost", project="web", notified=True)
RECRAWL = problem(
    "recrawl:web", "warning", "project web: Watchman had to recrawl", project="web", fix=FIX
)
CARDS = f"""
const copied = [];
let copyError = null;
dashboard.initDashboard({{ copy: async (text) => {{ if (copyError) throw copyError; copied.push(text); }} }});
state.dash = {{ projects: [
  {{ name: "web", status: "subscribed", snooze: null }},
  {{ name: "api", status: "subscribed", snooze: null }},
], missing: [], problems: {json.dumps([LOST, RECRAWL])} }};
dashboard.renderCards();
const wrap = (name) => [...$("cards").children].find((n) => n.dataset.key === `p:${{name}}`);
// The corner (.card-tools) holds the badges first, then the snooze control.
const box = (name) => wrap(name).children[1].children[0];
"""


def run_cards(script: str) -> Any:
    return dash_js.run(CARDS + script)


def test_a_projects_problems_are_badges_on_its_card() -> None:
    script = """
const badges = box("web").byClass("problem");
done([
  badges.map((b) => [b.className, b.getAttribute("tabindex"), b.title, b.getAttribute("aria-label")]),
  texts(box("web"), "badge"),
  texts(box("web"), "problem-message"),
  texts(box("web"), "problem-fix"),
  box("web").byClass("copy-fix").map((b) => [b.textContent, b.getAttribute("aria-label")]),
  box("api").children.length,
]);
"""
    assert run_cards(script) == [
        [
            ["problem serious", "0", "project web: watch lost", "Problem: project web: watch lost"],
            [
                "problem warning",
                "0",
                "project web: Watchman had to recrawl",
                "Warning: project web: Watchman had to recrawl",
            ],
        ],
        ["⚠️", "ⓘ"],
        ["project web: watch lost", "project web: Watchman had to recrawl"],
        [FIX],
        [["Copy", "Copy the fix for web"]],
        0,
    ]


def test_copy_puts_the_fix_on_the_clipboard_and_a_toast_confirms() -> None:
    script = """
box("web").byClass("copy-fix")[0].click();
await settle();
done([copied, texts($("toasts"), "toast-text"), $("toasts").children[0].className]);
"""
    assert run_cards(script) == [
        [FIX],
        ["Fix copied"],
        "toast",
    ]


def test_a_failed_copy_says_so() -> None:
    script = """
copyError = new Error("Write permission denied.");
box("web").byClass("copy-fix")[0].click();
await settle();
done([copied, texts($("toasts"), "toast-text"), $("toasts").children[0].className]);
"""
    assert run_cards(script) == [
        [],
        ["Copy failed: Write permission denied."],
        "toast error",
    ]


@pytest.mark.parametrize(
    ("fields", "late"),
    [
        ({"notified": False, "attempts": 2, "last_error": "sendMessage: network error"}, True),
        ({"notified": False, "attempts": 0}, False),  # not attempted yet (grace)
        ({"notified": True, "attempts": 0}, False),
    ],
)
def test_an_undelivered_alert_says_so_on_its_badge(fields: dict[str, Any], late: bool) -> None:
    stuck = {**LOST, **fields}
    script = f"""
state.dash.problems = [{json.dumps(stuck)}];
dashboard.renderCards();
done(texts(box("web"), "problem-late"));
"""
    assert run_cards(script) == (["alert not delivered yet: Telegram unreachable"] if late else [])


def test_a_poll_keeps_the_badges_and_their_focus() -> None:
    script = """
const before = box("web").byClass("problem")[1];
before.focus();
dashboard.renderCards();
const kept = box("web").byClass("problem")[1] === before && doc.activeElement === before;
state.dash.problems = [{ ...state.dash.problems[1], message: "project web: recrawled again" }];
dashboard.renderCards();
done([kept, texts(box("web"), "problem-message")]);
"""
    assert run_cards(script) == [True, ["project web: recrawled again"]]


def test_a_missing_projects_warning_is_on_its_card() -> None:
    missing = problem(
        "missing:gone",
        "warning",
        "project gone: path /x does not exist; skipped until it reappears. "
        "Reconnect the drive or fix the path",
        project="gone",
    )
    script = f"""
state.dash.missing = [{{ name: "gone", path: "/x" }}];
state.dash.problems = [{json.dumps(missing)}];
dashboard.renderCards();
done([texts(box("gone"), "badge"), texts(box("gone"), "problem-fix"), box("gone").byClass("copy-fix").length]);
"""
    assert run_cards(script) == [["ⓘ"], [], 0]  # advice, not a command: nothing to copy


# -- the status sentence ---------------------------------------------------------------

DASH = {
    "daemon": {"connected": True},
    "telegram": {"last_error": None},
    "counts": {"pending": 0},
    "projects": [
        {"name": "web", "status": "subscribed", "last_change_at": T - 180},
        {"name": "api", "status": "subscribed", "last_change_at": None},
    ],
    "missing": [],
    "problems": [],
}


def sentence(problems: list[dict[str, Any]]) -> dict[str, Any]:
    dash = {**DASH, "problems": problems}
    script = f"""
const m = await import(js("status.js"));
done(m.statusSentence({{ pollFailed: false, dash: {json.dumps(dash)}, now: {NOW} }}));
"""
    return dict(run_js(script))


def test_warnings_follow_all_good() -> None:
    other = problem("telegram", "warning", "Telegram failing: x")
    result = sentence([RECRAWL, other])
    assert result["text"] == "All good · 2 warnings · watching 2 projects · last change 3 min ago"
    assert result["live"] == "All good · 2 warnings · watching 2 projects"
    assert sentence([RECRAWL])["text"].startswith("All good · 1 warning · watching")


def test_a_serious_project_problem_needs_attention() -> None:
    result = sentence([RECRAWL, LOST])
    assert (result["text"], result["link"], result["tone"]) == (
        "1 project needs attention",
        True,
        "warn",
    )
    assert "attention" not in result  # the link opens the view; no card is named


def test_a_serious_problem_outside_any_project_is_named() -> None:
    config = problem(
        "config",
        "serious",
        "config rejected, keeping the last good config: bad",
        attempts=1,
        last_error="sendMessage: Too Many Requests: retry after 9",
    )
    web = problem("web", "serious", "web UI stopped: boom", notified=True)
    result = sentence([config, web])
    assert result["text"] == (
        "config rejected, keeping the last good config: bad · 1 more · "
        "alert not delivered yet: rate limited"
    )
    assert (result["link"], result["tone"]) == (True, "warn")
    # Watchman's own problem is the connection state's: not repeated here.
    down = problem("watchman", "serious", "Watchman is unreachable")
    assert sentence([down])["text"].startswith("All good")


def test_main_wires_the_settings_edits_and_the_clipboard() -> None:
    main = (STATIC_DIR / "js" / "main.js").read_text(encoding="utf-8")
    assert '$("settings").addEventListener("change", settingsEdited);' in main
    assert "copy: clipboardCopy(w.navigator)," in main


def test_without_a_clipboard_the_copy_says_so() -> None:
    script = """
dashboard.initDashboard({ copy: dashboard.clipboardCopy({}) });
await dashboard.copyFix("x");
const failed = texts($("toasts"), "toast-text");
const written = [];
dashboard.initDashboard({ copy: dashboard.clipboardCopy({ clipboard: { writeText: async (t) => written.push(t) } }) });
await dashboard.copyFix("y");
done([failed, texts($("toasts"), "toast-text"), written]);
"""
    assert run_cards(script) == [
        ["Copy failed: the clipboard is not available here"],
        ["Fix copied"],
        ["y"],
    ]


def test_a_problem_outside_any_project_stays_visible_while_one_needs_attention() -> None:
    config = problem("config", "serious", "config rejected, keeping the last good config: bad")
    web = problem("web", "serious", "web UI stopped: boom")
    dash = {**DASH, "missing": [{"name": "gone", "path": "/x"}], "problems": [config]}
    script = f"""
const m = await import(js("status.js"));
const one = m.statusSentence({{ pollFailed: false, dash: {json.dumps(dash)}, now: {NOW} }});
const two = m.statusSentence({{
  pollFailed: false, dash: {json.dumps({**dash, "problems": [config, web]})}, now: {NOW},
}});
done([one.text, one.link, two.text]);
"""
    assert run_js(script) == [
        "1 project needs attention · config rejected, keeping the last good config: bad",
        True,
        "1 project needs attention · config rejected, keeping the last good config: bad · 1 more",
    ]
