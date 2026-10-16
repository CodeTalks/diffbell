"""main.js boots against a fake window: the session, the first poll and the
wiring of every control (UI refine spec §4.1-§4.7)."""

from __future__ import annotations

import json
from typing import Any

from tests.web import test_js_forms as forms_js
from tests.web.node import needs_node, run_js

pytestmark = needs_node

NOW = 1_791_547_200_000
T = NOW // 1000

DASH = {
    "daemon": {
        "pid": 1,
        "connected": True,
        "config_version": "v1",
        "config_rejected_version": None,
    },
    "watchman": {"version": "2026.10.05.00"},
    "web": {"running": True, "port": 8765, "last_error": None},
    "telegram": {"last_ok": T - 60, "last_error": None},
    "projects": [
        {
            "name": "proj",
            "path": "/h/proj",
            "status": "subscribed",
            "error": None,
            "note": None,
            "last_change_at": T - 60,
            "watched_count": 3,
            "watched_exact": True,
        },
    ],
    "missing": [],
    "counts": {"pending": 0, "sent": 1, "failed": 0},
}
EVENT = {
    "id": 7,
    "project": "proj",
    "created_at": T - 60,
    "status": "sent",
    "attempts": 1,
    "last_error": None,
    "next_attempt_at": T - 60,
    "branch": "main",
    "added": 1,
    "removed": 1,
    "files": [{"path": "package.json", "kind": "modified", "redacted": False}],
}
DETAIL = {
    **EVENT,
    "reports": [
        {
            "path": "package.json",
            "kind": "modified",
            "redacted": False,
            "note": "vs HEAD",
            "lines": ['-"v": 1', '+"v": 2'],
            "truncated": False,
        },
    ],
    "telegram_lines": 20,
    "telegram_files": 1,
}

# Timers of a second or more (the poll, the start retry, toasts) are held in
# `timers` for the test to fire; shorter ones (debounces, settle()) run.
TIMERS = """
const timers = [];
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = (fn, ms, ...args) => {
  if (!(ms >= 1000)) return realSetTimeout(fn, ms, ...args);
  const timer = { fn, ms, cleared: false };
  timers.push(timer);
  return timer;
};
globalThis.clearTimeout = (id) => {
  if (timers.includes(id)) id.cleared = true;
  else realClearTimeout(id);
};
const live = () => timers.filter((t) => !t.cleared);
const fire = async (timer) => {
  timer.cleared = true;
  timer.fn();
  for (let i = 0; i < 5; i += 1) await settle();
};
"""

BOOT = (
    TIMERS
    + """
const doc = new FakeDocument();
const tabs = ["dashboard", "projects", "settings"].map((view) => {
  const tab = doc.createElement("button");
  tab.setAttribute("role", "tab");
  tab.dataset.view = view;
  return tab;
});
const tablist = doc.createElement("nav");
doc.querySelectorAll = () => tabs;
doc.querySelector = () => tablist;
for (const id of ["status-icon", "e-save", "s-save"]) doc.getElementById(id);
const { icon, useDocument } = await import(js("dom.js"));
useDocument(doc);
icon("pending").setAttribute("id", "status-icon");
doc.getElementById("keys-overlay").hidden = true;
const net = fakeFetch();
const replaced = [];
const pushedUrls = [];
const winListeners = {};
globalThis.window = {
  document: doc,
  fetch: net.fetch,
  location: { search: "?event=7", pathname: "/" },
  history: {
    replaceState: (a, b, url) => replaced.push(url),
    pushState: (a, b, url) => pushedUrls.push(url),
  },
  addEventListener: (type, fn) => {
    (winListeners[type] ||= []).push(fn);
  },
};
__REPLIES__
await import(js("main.js"));
for (let i = 0; i < 5; i += 1) await settle();
const $ = (id) => doc.getElementById(id);
"""
)


POLL = f"""net.reply(200, {json.dumps(DASH)});
net.reply(200, {{ events: [{json.dumps(EVENT)}], total: 1 }});
"""
SESSION = 'net.reply(200, { csrf: "tok" });\n'
FIRST = SESSION + POLL + f"net.reply(200, {json.dumps(DETAIL)});\n"


def boot(script: str, replies: str = FIRST) -> Any:
    return run_js(BOOT.replace("__REPLIES__", replies) + script)


def poll_again() -> str:
    """Fire the page's visibilitychange (it refreshes) and let it finish."""
    return """
doc.listeners.visibilitychange[0]();
for (let i = 0; i < 5; i += 1) await settle();
"""


def test_boot_polls_and_renders_every_part() -> None:
    script = """
done([
  net.calls.map((c) => c.path),
  $("status-text").textContent.startsWith("All good · watching 1 project · last change"),
  $("cards").byClass("meta")[0].textContent.startsWith("3 files"),
  $("feed").byClass("row").length,
  $("detail").byClass("ln").map((n) => n.className),
  replaced.at(-1),
]);
"""
    assert boot(script) == [
        ["/api/session", "/api/dashboard", "/api/events?limit=50&offset=0", "/api/events/7"],
        True,
        True,
        1,
        ["ln del", "ln add"],
        "?event=7",
    ]


def test_tabs_and_shortcuts_are_wired() -> None:
    script = """
net.reply(200, { config_version: "v1", projects: [] });  // the projects tab loads them
tabs[1].click();
const projects = [tabs[1].getAttribute("aria-selected"), $("view-projects").hidden, $("view-dashboard").hidden, replaced.at(-1)];
const help = doc.listeners.keydown[0];
const event = { key: "?", target: doc.createElement("div"), preventDefault() { this.prevented = true; } };
help(event);
const overlay = [$("keys-overlay").hidden, event.prevented === true];
help({ key: "Escape", target: doc.createElement("div"), preventDefault() {} });
done([projects, overlay, $("keys-overlay").hidden]);
"""
    assert boot(script) == [
        ["true", False, True, "?view=projects&event=7"],
        [False, True],
        True,
    ]


def test_a_failed_poll_says_the_daemon_is_unreachable() -> None:
    script = """
globalThis.window.fetch = () => Promise.reject(new TypeError("Failed to fetch"));
const { configureApi } = await import(js("api.js"));
configureApi({ fetch: () => Promise.reject(new TypeError("Failed to fetch")) });
doc.listeners.visibilitychange[0]();
for (let i = 0; i < 5; i += 1) await settle();
done([$("status-text").textContent, $("feed").byClass("row").length]);
"""
    assert boot(script) == ["Can't reach the daemon · retrying", 1]


def test_a_filter_change_re_renders_the_cards() -> None:
    """feed.js's filtersChanged hook is wired to renderCards: a card shows whether
    it is the active project filter, at once, before the next poll answers."""
    script = """
const card = () => $("cards").byClass("card")[0];
const before = card().className.split(" ").includes("active");
const { setFilter } = await import(js("feed.js"));
setFilter({ project: "proj" });
done([before, card().className.split(" ").includes("active")]);
"""
    assert boot(script) == [False, True]


def test_a_refresh_error_with_a_status_is_not_unreachable() -> None:
    """Spec §5 keeps "Can't reach the daemon" for status 0; a 500 says what it is."""
    script = (
        """
net.reply(500, { error: "boom" });
net.reply(500, { error: "boom" });
"""
        + poll_again()
        + """
done([$("status-text").textContent.startsWith("All good"), $("feed-status").textContent]);
"""
    )
    assert boot(script) == [True, "1 of 1 event · Internal error in the daemon: boom"]


def test_a_bug_in_a_refresh_is_logged_not_shown_as_unreachable() -> None:
    script = (
        """
const logged = [];
console.error = (err) => logged.push(err instanceof TypeError);
net.reply(200, { daemon: {} });  // no projects list: rendering throws a TypeError
net.reply(200, { events: [], total: 0 });
"""
        + poll_again()
        + """
done([logged, $("status-text").textContent === "Can't reach the daemon · retrying"]);
"""
    )
    assert boot(script) == [[True], False]


def test_the_status_line_recovers_after_a_refresh_error() -> None:
    script = (
        """
net.reply(500, { error: "boom" });
net.reply(500, { error: "boom" });
"""
        + poll_again()
        + POLL
        + poll_again()
        + """
done($("feed-status").textContent);
"""
    )
    assert boot(script) == "1 of 1 event"


def test_the_shortcut_overlay_is_modal() -> None:
    script = """
const key = (k, extra = {}) => {
  const event = { key: k, target: doc.createElement("div"), preventDefault() { this.prevented = true; }, ...extra };
  doc.listeners.keydown[0](event);
  return event;
};
const overlay = $("keys-overlay");
$("f-q").focus();
$("keys-open").click();
const opened = [overlay.hidden, doc.activeElement === $("keys-close")];
key("/");  // not "focus the search" while the overlay is open
key("j");
const ignored = [doc.activeElement === $("keys-close"), $("feed").byClass("selected").length];
// Tab is trapped at the document, so it holds wherever focus sits.
const docKey = (init) => {
  const event = { target: overlay, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...init };
  for (const fn of doc.listeners.keydown) fn(event);
  return event;
};
const tab = docKey({ key: "Tab" });
const shiftTab = docKey({ key: "Tab", shiftKey: true });
const trapped = [tab.defaultPrevented, shiftTab.defaultPrevented, doc.activeElement === $("keys-close")];
key("Escape");
const closed = [overlay.hidden, doc.activeElement === $("f-q")];
key("?");
const inside = doc.createElement("p");
overlay.append(inside);
inside.dispatch("click");  // a click in the dialog keeps it open
const stays = overlay.hidden;
overlay.dispatch("click");  // a click on the backdrop closes it
done([opened, ignored, trapped, closed, stays, overlay.hidden, doc.activeElement === $("f-q")]);
"""
    assert boot(script) == [
        [False, True],
        [True, 1],
        [True, True, True],
        [True, True],
        False,
        True,
        True,
    ]


def test_the_overlay_traps_tab_at_the_document_only_while_open() -> None:
    """Focus may have left the dialog (a click on its text): Tab still can't
    reach the page behind it. With the overlay closed, Tab is left alone."""
    script = """
const tabKey = () => {
  const event = { key: "Tab", target: doc.createElement("div"), defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (const fn of doc.listeners.keydown) fn(event);
  return event.defaultPrevented;
};
const closed = tabKey();
$("keys-open").click();
doc.activeElement = null;  // focus fell to the body
const open = [tabKey(), doc.activeElement === $("keys-close")];
done([closed, open]);
"""
    assert boot(script) == [False, [True, True]]


def test_closing_the_overlay_falls_back_to_its_button_when_focus_was_removed() -> None:
    script = """
const row = doc.createElement("button");
$("cards").append(row);
row.focus();
$("keys-open").click();
row.remove();  // a refresh rebuilt what had focus
$("keys-close").click();
done([$("keys-overlay").hidden, doc.activeElement === $("keys-open")]);
"""
    assert boot(script) == [True, True]


def test_an_unreachable_poll_clears_a_stale_error_note() -> None:
    """The status line says "Can't reach the daemon"; the feed no longer repeats
    the error of an earlier poll."""
    script = (
        """
net.reply(500, { error: "boom" });
net.reply(500, { error: "boom" });
"""
        + poll_again()
        + """
const before = $("feed-status").textContent;
const { configureApi } = await import(js("api.js"));
configureApi({ fetch: () => Promise.reject(new TypeError("Failed to fetch")) });
"""
        + poll_again()
        + """
done([before, $("feed-status").textContent, $("status-text").textContent]);
"""
    )
    assert boot(script) == [
        "1 of 1 event · Internal error in the daemon: boom",
        "1 of 1 event",
        "Can't reach the daemon · retrying",
    ]


def test_a_503_poll_is_told_by_the_banner_not_the_feed() -> None:
    script = (
        """
net.reply(503, { error: "starting" });
net.reply(503, { error: "starting" });
"""
        + poll_again()
        + """
done([$("feed-status").textContent, $("banner").textContent.includes("starting")]);
"""
    )
    assert boot(script) == ["1 of 1 event", True]


SESSION_DOWN = 'net.reply(503, { error: "starting" });\n'


def test_the_session_is_retried_with_a_growing_delay() -> None:
    script = (
        """
const first = [live().map((t) => t.ms), $("refreshed").textContent, $("status-text").textContent];
net.reply(503, { error: "starting" });
await fire(live()[0]);
const second = [live().map((t) => t.ms), $("refreshed").textContent];
"""
        + FIRST
        + """
await fire(live()[0]);
done([first, second, net.calls.map((c) => c.path), $("status-text").textContent.startsWith("All good"), live().map((t) => t.ms)]);
"""
    )
    assert boot(script, SESSION_DOWN) == [
        [[1000], "retrying in 1 s", "Can't reach the daemon · retrying"],
        [[2000], "retrying in 2 s"],
        [
            "/api/session",
            "/api/session",
            "/api/session",
            "/api/dashboard",
            "/api/events?limit=50&offset=0",
            "/api/events/7",
        ],
        True,
        [5000],
    ]


def test_coming_back_into_view_before_the_session_does_not_poll() -> None:
    script = (
        poll_again()
        + """
done(net.calls.map((c) => c.path));
"""
    )
    assert boot(script, SESSION_DOWN) == ["/api/session"]


def test_no_poll_timer_while_the_page_is_hidden() -> None:
    script = (
        """
const hidden = live().map((t) => t.ms);
doc.visibilityState = "visible";
"""
        + POLL
        + poll_again()
        + """
done([hidden, live().map((t) => t.ms)]);
"""
    )
    replies = 'doc.visibilityState = "hidden";\n' + FIRST
    assert boot(script, replies) == [[], [5000]]


def test_the_banner_is_not_rebuilt_when_nothing_changed() -> None:
    failed = json.dumps({**DASH, "counts": {"pending": 0, "sent": 0, "failed": 1}})
    poll = f"""net.reply(200, {failed});
net.reply(200, {{ events: [{json.dumps(EVENT)}], total: 1 }});
"""
    script = (
        """
const span = $("banner").children[0];
"""
        + poll
        + poll_again()
        + """
done([span.textContent, $("banner").children[0] === span, $("banner").hidden]);
"""
    )
    replies = SESSION + poll + f"net.reply(200, {json.dumps(DETAIL)});\n"
    assert boot(script, replies) == ["1 failed delivery.", True, False]


def test_save_shortcut_is_left_to_the_browser_on_the_dashboard() -> None:
    script = """
const before = net.calls.length;
const event = { key: "s", metaKey: true, target: doc.createElement("div"), preventDefault() { this.prevented = true; } };
doc.listeners.keydown[0](event);
for (let i = 0; i < 3; i += 1) await settle();
done([event.prevented === true, net.calls.length - before]);
"""
    assert boot(script) == [False, 0]


def test_a_click_or_focus_outside_closes_the_snooze_menu() -> None:
    """Without moving focus; a click or focus inside the open card's tools keeps it."""
    script = """
const dashboard = await import(js("dashboard.js"));
const { state } = await import(js("state.js"));
const menus = () => $("cards").byClass("menu").length;
dashboard.toggleSnoozeMenu("proj");
const item = $("cards").byClass("menu-item")[1];
item.focus();
$("cards").byClass("menu")[0].click();  // the gap between its items
const inside = [menus(), state.snoozeMenu];
$("feed").click();
const clicked = [menus(), state.snoozeMenu];
dashboard.toggleSnoozeMenu("proj");
$("f-q").focus();
done([inside, clicked, menus(), state.snoozeMenu, doc.activeElement === $("f-q")]);
"""
    assert boot(script) == [[1, "proj"], [0, None], 0, None, True]


def test_a_click_on_a_control_that_removes_itself_still_closes_the_snooze_menu() -> None:
    """The target of a toast's close button is detached by its own handler: the
    dispatch path still says it was outside the menu. The snooze button that opens
    the menu is detached too (the tools re-render), but its path is inside."""
    script = """
const dashboard = await import(js("dashboard.js"));
const { state } = await import(js("state.js"));
const { showToast } = await import(js("toast.js"));
const menus = () => $("cards").byClass("menu").length;
$("cards").byClass("snooze")[0].click();
const opened = [menus(), state.snoozeMenu];
showToast("Saved");
$("toasts").byClass("close")[0].click();
done([opened, $("toasts").children.length, menus(), state.snoozeMenu]);
"""
    assert boot(script) == [[1, "proj"], 0, 0, None]


THEME_RADIOS = '["system", "light", "dark"].map((t) => $(`s-theme-${t}`).checked)'


def test_the_theme_radios_apply_at_once_and_remember() -> None:
    """Settings → This page (spec §5): a choice applies at once, writes the diffbell_theme
    cookie the server reads for the first paint, and never touches the save bar."""
    script = f"""
const root = doc.documentElement;
const seen = [[root.dataset.theme ?? null, {THEME_RADIOS}, doc.cookie]];
for (const theme of ["light", "dark", "system"]) {{
  $(`s-theme-${{theme}}`).checked = true;
  $(`s-theme-${{theme}}`).dispatch("change");
  seen.push([root.dataset.theme ?? null, {THEME_RADIOS}, doc.cookie]);
}}
done(seen);
"""
    assert boot(script) == [
        [None, [True, False, False], ""],
        [
            "light",
            [False, True, False],
            "diffbell_theme=light; Path=/; Max-Age=31536000; SameSite=Strict",
        ],
        [
            "dark",
            [False, False, True],
            "diffbell_theme=dark; Path=/; Max-Age=31536000; SameSite=Strict",
        ],
        [None, [True, False, False], "diffbell_theme=; Path=/; Max-Age=0; SameSite=Strict"],
    ]


def test_the_header_button_cycles_and_keeps_the_radios_in_step() -> None:
    """The header's quick toggle and Settings → This page are one choice: a click
    cycles System → Light → Dark, writes the cookie and checks the matching radio;
    a radio choice relabels the button."""
    script = f"""
const root = doc.documentElement;
const button = $("theme-toggle");
const seen = [];
for (let i = 0; i < 3; i += 1) {{
  button.click();
  seen.push([root.dataset.theme ?? null, button.getAttribute("aria-label"), {THEME_RADIOS}, doc.cookie]);
}}
$("s-theme-dark").checked = true;
$("s-theme-dark").dispatch("change");
seen.push(button.getAttribute("aria-label"));
done(seen);
"""
    assert boot(script) == [
        [
            "light",
            "Theme: Light (click for Dark)",
            [False, True, False],
            "diffbell_theme=light; Path=/; Max-Age=31536000; SameSite=Strict",
        ],
        [
            "dark",
            "Theme: Dark (click for System)",
            [False, False, True],
            "diffbell_theme=dark; Path=/; Max-Age=31536000; SameSite=Strict",
        ],
        [
            None,
            "Theme: System (click for Light)",
            [True, False, False],
            "diffbell_theme=; Path=/; Max-Age=0; SameSite=Strict",
        ],
        "Theme: Dark (click for System)",
    ]


def test_the_theme_radios_start_from_the_servers_attribute() -> None:
    """The server wrote data-theme from the cookie (the first paint): the radio
    group shows that choice."""
    script = f"""
done([doc.documentElement.dataset.theme, {THEME_RADIOS}]);
"""
    replies = 'doc.documentElement.dataset.theme = "dark";\n' + FIRST
    assert boot(script, replies) == ["dark", [False, False, True]]


GUIDE = {
    "title": "Guide",
    "intro": ["Intro."],
    "sections": [
        {"id": "start", "title": "3. Start", "paragraphs": ["Send /start."]},
        {"id": "token", "title": "2. Token", "paragraphs": ["Copy it."]},
    ],
}


def test_the_guide_view_loads_the_guide_once_and_scrolls_to_its_step() -> None:
    """?view=guide&step=start: showView renders the guide; opening it again (here
    through Back/Forward) reuses the loaded guide.json."""
    script = """
const first = [net.calls.map((c) => c.path), $("view-guide").hidden, $("guide-title").textContent, $("guide-body").children.length, $("guide-start").scrolled];
window.location.search = "?view=guide&step=token";
winListeners.popstate[0]();
for (let i = 0; i < 5; i += 1) await settle();
done([first, net.calls.filter((c) => c.path === "/static/guide.json").length, $("guide-token").scrolled]);
"""
    replies = (
        SESSION
        + f"net.reply(200, {json.dumps(GUIDE)});\n"
        + POLL
        + f"net.reply(200, {json.dumps(DETAIL)});\n"
    )
    run = BOOT.replace('search: "?event=7"', 'search: "?view=guide&step=start"')
    assert run_js(run.replace("__REPLIES__", replies) + script) == [
        [
            [
                "/api/session",
                "/static/guide.json",
                "/api/dashboard",
                "/api/events?limit=50&offset=0",
                "/api/events/7",  # the feed selects its first event
            ],
            False,
            "Guide",
            3,
            1,
        ],
        1,
        1,
    ]


def test_show_failed_changes_opens_the_dashboard_filtered_to_failed() -> None:
    script = """
const { state } = await import(js("state.js"));
state.view = "settings";
$("tg-show-failed").dispatch("click");
done([state.view, state.filters.status, replaced.at(-1)]);
"""
    assert boot(script) == ["dashboard", "failed", "?status=failed"]


# -- Settings' save bar in the page (settings redesign spec §2) ------------------------

SETTINGS_REPLY = f"net.reply(200, {json.dumps(forms_js.SETTINGS)});\n"


def test_unloading_with_unsaved_settings_asks_first() -> None:
    script = (
        """
const settings = await import(js("settings.js"));
const unload = () => {
  const event = { returnValue: undefined, prevented: false, preventDefault() { this.prevented = true; } };
  for (const fn of winListeners.beforeunload) fn(event);
  return [event.prevented, event.returnValue ?? null];
};
const before = unload();  // Settings never loaded
"""
        + SETTINGS_REPLY
        + """
await settings.loadSettings();
const clean = unload();
$("s-quiet").value = "3";
done([before, clean, unload()]);
"""
    )
    assert boot(script) == [[False, None], [False, None], [True, ""]]


def test_leaving_settings_keeps_the_edits_and_the_bar() -> None:
    script = (
        SETTINGS_REPLY
        + """
const settings = await import(js("settings.js"));
tabs[2].click();
for (let i = 0; i < 5; i += 1) await settle();
$("s-quiet").value = "3";
settings.settingsEdited({ target: $("s-quiet") });
tabs[0].click();
tabs[2].click();
for (let i = 0; i < 5; i += 1) await settle();
done([$("s-quiet").value, $("s-bar").hidden, $("s-bar-text").textContent,
      net.calls.filter((c) => c.path === "/api/settings").length]);
"""
    )
    assert boot(script) == ["3", False, "1 unsaved change", 1]
