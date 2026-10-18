"""The page's foundation modules under node: time, status words, keys, DOM
helpers, the API wrapper and toasts (UI refine spec §3.3, §4.2, §4.6, §4.7)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from diffbell.web.app import STATIC_DIR
from tests.web.node import needs_node, run_js

pytestmark = needs_node

# 2026-10-09 12:00:00 UTC, in ms.
NOON = 1_791_547_200_000


def js_value(module: str, expression: str, *, tz: str = "UTC") -> Any:
    return run_js(f'const m = await import(js("{module}"));\ndone({expression});', tz=tz)


# -- time.js -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds_ago", "expected"),
    [(5, "just now"), (59, "just now"), (60, "1 min ago"), (3599, "59 min ago"), (7200, "2 h ago")],
)
def test_relative_time(seconds_ago: int, expected: str) -> None:
    ts = NOON / 1000 - seconds_ago
    assert js_value("time.js", f"m.relativeTime({ts}, {NOON})") == expected


def test_relative_time_past_a_day_is_the_clock_time() -> None:
    ts = NOON / 1000 - 86_400 - 3_600  # 11:00 UTC the day before
    assert js_value("time.js", f"m.relativeTime({ts}, {NOON})") == "11:00"
    assert js_value("time.js", f"m.ago({ts}, {NOON})") == "1 d ago"
    assert js_value("time.js", f"m.ago(null, {NOON})") == "no changes yet"


def test_day_labels_across_midnight() -> None:
    script = f"""
const m = await import(js("time.js"));
const now = {NOON};
done([
  m.dayLabel(now / 1000, now),
  m.dayLabel(now / 1000 - 12 * 3600, now),       // 00:00 today
  m.dayLabel(now / 1000 - 12 * 3600 - 1, now),   // 23:59:59 yesterday
  m.dayLabel(now / 1000 - 36 * 3600 - 1, now),   // 23:59:59 two days ago
  m.dayLabel(now / 1000 - 400 * 86400, now),     // another year
]);
"""
    assert run_js(script) == ["Today", "Today", "Yesterday", "Wed 7 Oct", "Thu 4 Sep 2025"]


def test_since_text_names_the_day_like_until_text() -> None:
    """The Telegram card's "Since {time}" (settings redesign spec §6)."""
    script = f"""
const m = await import(js("time.js"));
const now = {NOON};
done([
  m.sinceText(now / 1000 - 3600, now),
  m.sinceText(now / 1000 - 12 * 3600, now),       // 00:00 today
  m.sinceText(now / 1000 - 12 * 3600 - 60, now),  // 23:59 yesterday
  m.sinceText(now / 1000 - 3 * 86400, now),
  m.sinceText(now / 1000 - 400 * 86400, now),     // another year
]);
"""
    assert run_js(script) == [
        "11:00",
        "00:00",
        "yesterday 23:59",
        "Tue 6 Oct 12:00",
        "Thu 4 Sep 2025 12:00",
    ]


def test_day_grouping_across_a_dst_change() -> None:
    """Europe/Berlin leaves summer time on Sun 25 Oct 2026 (a 25 h day): an event
    at 00:30 on the 25th is still "Yesterday" at 00:30 on the 26th, 24 h + 1 h later."""
    script = """
const m = await import(js("time.js"));
const at = (y, mo, d, h, mi) => new Date(y, mo - 1, d, h, mi).getTime();
const now = at(2026, 10, 26, 0, 30);
done([
  m.dayLabel(at(2026, 10, 25, 0, 30) / 1000, now),
  m.dayLabel(at(2026, 10, 24, 23, 59) / 1000, now),
  m.dayKey(at(2026, 10, 25, 23, 59) / 1000),
  (m.localMidnight("2026-10-25", 1) - m.localMidnight("2026-10-25")) / 3600,
  (m.localMidnight("2026-03-29", 1) - m.localMidnight("2026-03-29")) / 3600,
]);
"""
    assert run_js(script, tz="Europe/Berlin") == ["Yesterday", "Sat 24 Oct", "2026-10-25", 25, 23]


def test_local_midnight_rejects_bad_dates() -> None:
    assert js_value("time.js", '[m.localMidnight("2026-1-1"), m.localMidnight("")]') == [None, None]


@pytest.mark.parametrize(
    "value", ["2026-02-31", "2026-02-29", "2026-13-01", "2026-00-10", "2026-04-31"]
)
def test_local_midnight_rejects_days_that_do_not_exist(value: str) -> None:
    """new Date() would roll 31 Feb over into March; the date must round-trip."""
    assert js_value("time.js", f"m.localMidnight({json.dumps(value)})") is None


def test_local_midnight_accepts_a_leap_day() -> None:
    assert js_value("time.js", 'm.localMidnight("2028-02-29")') == 1835395200


# -- status.js ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("project", "word", "icon"),
    [
        ({"status": "subscribed"}, "Watching", "watch"),
        ({"status": "pending"}, "Starting…", "pending"),
        ({"status": "missing"}, "Folder missing", "warning"),
        ({"status": "error", "error": "watch failed"}, "Error: watch failed", "failed"),
    ],
)
def test_project_status_words(project: dict[str, str], word: str, icon: str) -> None:
    result = js_value("status.js", f"m.projectStatus({json.dumps(project)})")
    assert (result["word"], result["icon"]) == (word, icon)


@pytest.mark.parametrize(
    ("event", "word", "icon"),
    [
        ({"status": "pending", "attempts": 0}, "Sending…", "pending"),
        (
            {"status": "pending", "attempts": 2, "next_attempt_at": NOON / 1000 + 90},
            "Retrying at 12:01",
            "retry",
        ),
        ({"status": "sent", "attempts": 1}, "Sent", "sent"),
        (
            {
                "status": "failed",
                "last_error": "sendMessage: Forbidden: bot was blocked by the user",
            },
            "Failed: bot was blocked",
            "failed",
        ),
        (
            {"status": "failed", "last_error": "sendMessage: network error (ConnectError)"},
            "Failed: Telegram unreachable",
            "failed",
        ),
        (
            {"status": "failed", "last_error": "sendMessage: Too Many Requests: retry after 9"},
            "Failed: rate limited",
            "failed",
        ),
    ],
)
def test_event_status_words(event: dict[str, Any], word: str, icon: str) -> None:
    script = f"""
const m = await import(js("status.js"));
done(m.eventStatus({json.dumps(event)}));
"""
    result = run_js(script)
    assert (result["word"], result["icon"]) == (word, icon)


def test_other_failures_are_clipped_with_the_full_text_in_the_title() -> None:
    raw = "sendMessage: Bad Request: " + "x" * 200
    script = f"""
const m = await import(js("status.js"));
done(m.eventStatus({{ status: "failed", last_error: "{raw}" }}));
"""
    result = run_js(script)
    assert result["word"] == "Failed: " + raw[:79] + "…"
    assert result["title"] == raw


DASH = {
    "daemon": {"connected": True},
    "telegram": {"last_error": None},
    "counts": {"pending": 3},
    "projects": [
        {"name": "a", "status": "subscribed", "last_change_at": NOON / 1000 - 180},
        {"name": "b", "status": "subscribed", "last_change_at": None},
    ],
    "missing": [],
}


def sentence(changes: dict[str, Any], *, poll_failed: bool = False) -> dict[str, Any]:
    dash = json.loads(json.dumps(DASH))
    for key, value in changes.items():
        dash[key] = value
    script = f"""
const m = await import(js("status.js"));
done(m.statusSentence({{ pollFailed: {json.dumps(poll_failed)}, dash: {json.dumps(dash)}, now: {NOON} }}));
"""
    return dict(run_js(script))


def test_the_status_sentence_states_in_order() -> None:
    every_problem = {
        "daemon": {"connected": False},
        "telegram": {"last_error": "sendMessage: network error (ConnectError)"},
        "missing": [{"name": "gone", "path": "/x"}],
    }
    unreachable = sentence(every_problem, poll_failed=True)
    assert (unreachable["text"], unreachable["tone"]) == (
        "Can't reach the daemon · retrying",
        "bad",
    )
    watchman = sentence(every_problem)
    assert (watchman["text"], watchman["tone"]) == ("Watchman not running", "warn")
    del every_problem["daemon"]
    telegram = sentence(every_problem)
    assert (telegram["text"], telegram["tone"]) == (
        "Telegram unreachable · 3 notifications queued",
        "bad",
    )
    del every_problem["telegram"]
    attention = sentence(every_problem)
    assert (attention["text"], attention["link"]) == ("1 project needs attention", True)
    assert sentence({})["text"] == "All good · watching 2 projects · last change 3 min ago"
    # The announced twin leaves out the clause that ticks every minute.
    assert sentence({})["live"] == "All good · watching 2 projects"
    assert unreachable["live"] == unreachable["text"]
    assert attention["live"] == attention["text"]


@pytest.mark.parametrize(
    ("last_error", "text"),
    [
        (
            "sendMessage: Forbidden: bot was blocked by the user",
            "Telegram failing: bot was blocked",
        ),
        ("sendMessage: Too Many Requests: retry after 9", "Telegram failing: rate limited"),
        ("sendMessage: Unauthorized", "Telegram failing: sendMessage: Unauthorized"),
    ],
)
def test_the_telegram_sentence_names_the_reason(last_error: str, text: str) -> None:
    result = sentence({"telegram": {"last_error": last_error}, "counts": {"pending": 1}})
    assert result["text"] == f"{text} · 1 notification queued"


def test_two_projects_in_trouble_and_no_changes_yet() -> None:
    projects = [
        {"name": "a", "status": "error", "error": "x"},
        {"name": "b", "status": "subscribed"},
    ]
    assert sentence({"projects": projects, "missing": [{"name": "c"}]})["text"] == (
        "2 projects need attention"
    )
    quiet = [{"name": "a", "status": "subscribed", "last_change_at": None}]
    assert sentence({"projects": quiet})["text"] == "All good · watching 1 project · no changes yet"
    assert sentence({"projects": quiet})["live"] == "All good · watching 1 project"


def test_stale_text_only_after_15_seconds() -> None:
    assert js_value(
        "status.js",
        f"[m.staleText({NOON - 15000}, {NOON}), m.staleText({NOON - 16000}, {NOON}), m.staleText({NOON - 300000}, {NOON}), m.staleText(0, {NOON})]",
    ) == [
        "",
        "Updated 16 s ago",
        "Updated 5 min ago",
        "",
    ]


def test_the_footer_names_the_running_version() -> None:
    assert js_value(
        "status.js",
        '[m.footerVersion({daemon: {version: "0.1.3"}}), m.footerVersion(null), '
        'm.footerVersion({daemon: {}}), m.footerVersion({daemon: {version: ""}})]',
    ) == ["Diffbell 0.1.3", "Diffbell", "Diffbell", "Diffbell"]


def test_every_status_icon_is_in_the_sprite() -> None:
    sprite = (STATIC_DIR / "icons.svg").read_text(encoding="utf-8")
    script = """
const m = await import(js("status.js"));
const names = new Set();
for (const s of ["subscribed", "pending", "missing", "error", "other"]) names.add(m.projectStatus({ status: s }).icon);
for (const ev of [{ status: "sent" }, { status: "pending", attempts: 0 }, { status: "pending", attempts: 1, next_attempt_at: 1 }, { status: "failed" }]) names.add(m.eventStatus(ev).icon);
done([...names]);
"""
    for name in run_js(script):
        assert f'<symbol id="{name}"' in sprite, name


# -- keys.js -----------------------------------------------------------------------

KEYS = """
const m = await import(js("keys.js"));
const input = { tagName: "INPUT" };
const body = { tagName: "BODY" };
const k = (key, view, target = body, extra = {}) => m.shortcut({ key, target, ...extra }, view);
"""


def test_shortcuts_per_view() -> None:
    script = (
        KEYS
        + """
done([
  k("/", "dashboard"), k("/", "projects"), k("/", "settings"),
  k("j", "dashboard"), k("ArrowDown", "dashboard"), k("k", "dashboard"), k("ArrowUp", "dashboard"),
  k("j", "projects"), k("?", "settings"), k("Escape", "projects"), k("x", "dashboard"),
]);
"""
    )
    assert run_js(script) == [
        "focus-search",
        "focus-tree-filter",
        None,
        "next",
        "next",
        "previous",
        "previous",
        None,
        "help",
        "escape",
        None,
    ]


def test_keys_are_ignored_while_typing_except_save_and_escape() -> None:
    script = (
        KEYS
        + """
const textarea = { tagName: "textarea" };
done([
  k("/", "dashboard", input), k("j", "dashboard", textarea), k("?", "dashboard", { tagName: "SELECT" }),
  k("s", "projects", textarea, { metaKey: true }), k("S", "settings", input, { ctrlKey: true }),
  k("Escape", "dashboard", input),
]);
"""
    )
    assert run_js(script) == [None, None, None, "save-project", "save-settings", "escape"]


def test_the_browser_save_is_kept_on_the_dashboard() -> None:
    script = (
        KEYS
        + 'done([k("s", "dashboard", body, { metaKey: true }), k("j", "dashboard", body, { metaKey: true })]);'
    )
    assert run_js(script) == [None, None]


def test_keys_during_ime_composition_are_left_to_the_ime() -> None:
    """An Esc (or any key) that ends an IME composition must not close or move anything."""
    script = (
        KEYS
        + """
done([
  k("Escape", "dashboard", body, { isComposing: true }),
  k("Escape", "projects", input, { keyCode: 229 }),
  k("s", "projects", input, { metaKey: true, isComposing: true }),
  k("j", "dashboard", body, { keyCode: 229 }),
]);
"""
    )
    assert run_js(script) == [None, None, None, None]


def test_install_keys_prevents_the_default_only_for_handled_actions() -> None:
    script = """
const m = await import(js("keys.js"));
const doc = new FakeDocument();
const target = doc.createElement("div");
const runs = [];
m.installKeys(target, () => "dashboard", (action) => { runs.push(action); return action !== "escape"; });
const handled = target.dispatch("keydown", { key: "j" });
const unhandled = target.dispatch("keydown", { key: "Escape" });
const ignored = target.dispatch("keydown", { key: "x" });
done([runs, handled.defaultPrevented, unhandled.defaultPrevented, ignored.defaultPrevented]);
"""
    assert run_js(script) == [["next", "escape"], True, False, False]


# -- dom.js ------------------------------------------------------------------------


def test_reconcile_reuses_nodes_by_key_and_keeps_order() -> None:
    script = """
const dom = await import(js("dom.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const list = doc.getElementById("list");
const render = (items) => dom.reconcile(list, items, (x) => x, (x) => dom.h("li"), (li, x) => dom.setText(li, x));
render(["a", "b", "c"]);
const [a, b] = list.children;
render(["c", "a", "d"]);
done([[...list.children].map((n) => n.textContent), list.children[1] === a, [...list.children].includes(b)]);
"""
    assert run_js(script) == [["c", "a", "d"], True, False]


def test_reconcile_reuses_nodes_for_numeric_keys() -> None:
    """data-key holds a string in a browser: a numeric key must still find its node."""
    script = """
const dom = await import(js("dom.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const list = doc.getElementById("list");
const render = (items) => dom.reconcile(list, items, (x) => x.id, () => dom.h("li"), (li, x) => dom.setText(li, x.t));
render([{ id: 1, t: "a" }, { id: 2, t: "b" }]);
const [a, b] = list.children;
render([{ id: 2, t: "B" }, { id: 1, t: "A" }]);
done([[...list.children].map((n) => n.textContent), list.children[0] === b, list.children[1] === a, a.dataset.key]);
"""
    assert run_js(script) == [["B", "A"], True, True, "1"]


@pytest.mark.parametrize("key", ["onclick", "ONCLICK", "onClick"])
def test_h_refuses_a_string_event_handler_in_any_case(key: str) -> None:
    script = f"""
const dom = await import(js("dom.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
let thrown = null;
let el = null;
try {{ el = dom.h("button", {{ {json.dumps(key)}: "alert(1)" }}); }} catch (err) {{ thrown = [err.name, err.message]; }}
done([thrown, el && el.getAttribute({json.dumps(key)})]);
"""
    assert run_js(script) == [["TypeError", key], None]


def test_h_attaches_an_event_handler_in_any_case() -> None:
    script = """
const dom = await import(js("dom.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
let clicks = 0;
const el = dom.h("button", { ONCLICK: () => { clicks += 1; } });
el.click();
done([clicks, el.getAttribute("ONCLICK")]);
"""
    assert run_js(script) == [1, None]


def test_h_sets_text_never_markup_and_icon_uses_the_sprite() -> None:
    script = """
const dom = await import(js("dom.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const el = dom.h("div", { class: "x", text: "<b>hi</b>", "data-n": 1 });
const svg = dom.icon("lock", "big");
done([el.textContent, el.childNodes.length, el.getAttribute("data-n"), svg.getAttribute("class"), svg.firstChild.getAttribute("href")]);
"""
    assert run_js(script) == ["<b>hi</b>", 1, "1", "icon big", "/static/icons.svg#lock"]


def test_shown_escapes_bidi_controls() -> None:
    """A right-to-left override can make "gpj.exe" read as "exe.jpg"."""
    names = "a\u200eb\u200fc\u202ad\u202ee\u2066f\u2069g"
    script = f"""
const dom = await import(js("dom.js"));
done(dom.shown({json.dumps(names)}));
"""
    assert run_js(script) == "a\\u200Eb\\u200Fc\\u202Ad\\u202Ee\\u2066f\\u2069g"


def test_shown_escapes_the_arabic_letter_mark_and_unicode_line_breaks() -> None:
    """U+061C is a bidi control too; U+2028/U+2029 break a line like "\\n" does."""
    names = "a\u061cb\u2028c\u2029d"
    script = f"""
const dom = await import(js("dom.js"));
done(dom.shown({json.dumps(names)}));
"""
    assert run_js(script) == "a\\u061Cb\\u2028c\\u2029d"


def test_shown_escapes_control_characters_and_lines_trims() -> None:
    script = """
const dom = await import(js("dom.js"));
done([dom.shown("a\\nb\\tc\\u0001"), dom.lines(" a \\n\\n b\\n"), dom.baseName("/x/y/"), dom.plural(1, "file", "files")]);
"""
    assert run_js(script) == ["a\\u000Ab\tc\\u0001", ["a", "b"], "y", "1 file"]


# -- api.js ------------------------------------------------------------------------

API = """
const { state } = await import(js("state.js"));
const api = await import(js("api.js"));
const net = fakeFetch();
const seen = [];
api.configureApi({ fetch: net.fetch, loggedOut: () => seen.push("401"), outagesChanged: () => seen.push("503") });
state.csrf = "tok";
"""


def test_api_sends_json_and_the_csrf_token_on_mutations() -> None:
    script = (
        API
        + """
net.reply(200, { ok: true });
net.reply(200, { a: 1 });
await api.api("POST", "/api/x", { b: 2 });
await api.api("GET", "/api/y");
done(net.calls.map((c) => [c.method, c.path, c.headers["X-Diffbell-Token"] || null, c.body || null]));
"""
    )
    assert run_js(script) == [["POST", "/api/x", "tok", {"b": 2}], ["GET", "/api/y", None, None]]


def test_api_errors_carry_the_status_and_the_server_message() -> None:
    script = (
        API
        + """
const errors = [];
for (const [status, body] of [[422, { error: "invalid pattern 'x': bad" }], [409, { error: "config changed on disk" }], [401, { error: "no" }], [503, { error: "down" }]]) {
  net.reply(status, body);
  try { await api.api("GET", "/api/z"); } catch (err) { errors.push([err.status, err.message]); }
}
net.reply(200, {});
await api.api("GET", "/api/z");  // a later success clears the outage
done([errors, seen, [...state.outages.keys()]]);
"""
    )
    assert run_js(script) == [
        [
            [422, "invalid pattern 'x': bad"],
            [409, "Conflict: config changed on disk"],
            [401, "Not logged in: no"],
            [503, "Unavailable: down"],
        ],
        ["401", "503", "503"],
        [],
    ]


def test_an_unreachable_daemon_is_status_zero() -> None:
    script = (
        API
        + """
api.configureApi({ fetch: () => Promise.reject(new TypeError("Failed to fetch")) });
try { await api.api("GET", "/api/x"); } catch (err) { done([err.status, err.message]); }
"""
    )
    assert run_js(script) == [0, "Couldn't reach the daemon"]


def test_a_hung_request_times_out_as_unreachable() -> None:
    """Every request carries a timeout signal; its abort reads as unreachable, so
    a hung daemon can't stop the polling for good."""
    script = (
        API
        + """
net.reply(200, {});
await api.api("GET", "/api/x");
const signal = net.calls[0].signal;
api.configureApi({ fetch: () => Promise.reject(new DOMException("", "TimeoutError")) });
try { await api.api("GET", "/api/x"); } catch (err) {
  done([err.status, err.message, signal instanceof AbortSignal, signal.aborted, api.TIMEOUT_MS]);
}
"""
    )
    assert run_js(script) == [0, "Couldn't reach the daemon", True, False, 15000]


def test_a_call_can_set_its_own_timeout() -> None:
    script = (
        API
        + """
const timeouts = [];
const timeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = (ms) => { timeouts.push(ms); return timeout(ms); };
net.reply(200, {});
net.reply(200, {});
await api.api("GET", "/api/x");
await api.api("POST", "/api/y", undefined, { timeout: 60000 });
done([timeouts, net.calls[1].body || null]);
"""
    )
    assert run_js(script) == [[15000, 60000], None]


# -- toast.js ----------------------------------------------------------------------


def test_one_toast_at_a_time_with_an_action() -> None:
    script = """
const dom = await import(js("dom.js"));
const toast = await import(js("toast.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const region = doc.getElementById("toasts");
let undone = 0;
toast.showToast("Saved");
toast.showToast("Removed shop", { action: "Undo", onAction: () => { undone += 1; }, ms: 10000 });
const count = region.children.length;
const text = region.textContent;
region.children[0].byClass("link")[0].click();
done([count, text, undone, region.children.length, toast.closeToast()]);
"""
    assert run_js(script) == [1, "Removed shopUndo", 1, 0, False]


def test_a_toast_closes_itself() -> None:
    script = """
const dom = await import(js("dom.js"));
const toast = await import(js("toast.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
toast.showToast("Retry queued", { ms: 5 });
await new Promise((r) => setTimeout(r, 30));
done(doc.getElementById("toasts").children.length);
"""
    assert run_js(script) == 0


def test_an_outage_banner_key_folds_numeric_path_segments() -> None:
    """A 503 on /api/events/7 and on /api/events/8 is one outage, not two banner
    lines, and a later success on any event clears it."""
    script = (
        API
        + """
for (const path of ["/api/events/7", "/api/events/8?x=1", "/api/events/12/diff"]) {
  net.reply(503, { error: "down" });
  try { await api.api("GET", path); } catch {}
}
const keys = [...state.outages.keys()];
net.reply(200, {});
await api.api("GET", "/api/events/9");
done([keys, [...state.outages.keys()]]);
"""
    )
    assert run_js(script) == [
        ["GET /api/events/:id", "GET /api/events/:id/diff"],
        ["GET /api/events/:id/diff"],
    ]


def test_wizard_draft_calls_never_reach_the_page_banner() -> None:
    """The wizard shows its own errors; a draft endpoint may never be called again
    (the chat poll stops once a chat is found), so its outage would never clear."""
    script = (
        API
        + """
const errors = [];
const calls = [
  () => api.checkBotToken("t"),
  () => api.findChats("d1"),
  () => api.enterChatId("d1", "5"),
  () => api.sendDraftTest("d1", "c"),
  () => api.saveDraft("d1", "c"),
];
for (const call of calls) {
  net.reply(503, { error: "Couldn't reach Telegram; try again." });
  try { await call(); } catch (err) { errors.push([err.status, err.message]); }
}
done([errors.length, errors[1], seen, [...state.outages.keys()]]);
"""
    )
    assert run_js(script) == [5, [503, "Unavailable: Couldn't reach Telegram; try again."], [], []]
