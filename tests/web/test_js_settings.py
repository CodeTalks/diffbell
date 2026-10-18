"""Settings under node (settings redesign spec §1-§5): the sections and their
focus, the pure model behind the save bar, the rows and switches, the server's
check, and the save bar itself."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.web import test_js_forms as forms_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_main import TIMERS

pytestmark = needs_node

NOW = 1_791_547_200_000  # 12:00 UTC; run_js runs node with TZ=UTC
T = NOW // 1000
SECTIONS = ["telegram", "notifications", "schedule", "patterns", "page"]
SETTINGS = {**forms_js.SETTINGS, "notify": {"quiet_hours": "22:30-07:00", "heartbeat": None}}

SETUP = (
    TIMERS
    + f"""
const dom = await import(js("dom.js"));
const {{ state }} = await import(js("state.js"));
const api = await import(js("api.js"));
const feed = await import(js("feed.js"));
const settings = await import(js("settings.js"));
const model = await import(js("settings-model.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const net = fakeFetch();
api.configureApi({{ fetch: net.fetch }});
const $ = (id) => doc.getElementById(id);
settings.initSettings({{ now: () => {NOW} }});
const flush = async () => {{ for (let i = 0; i < 5; i += 1) await settle(); }};
// Past the check's 300 ms pause (a real timer: TIMERS holds only those of 1 s or more).
const pause = () => new Promise((resolve) => setTimeout(resolve, 350));
const load = async (payload) => {{ net.reply(200, payload); await settings.loadSettings(); }};
const edit = (id, value) => {{ $(id).value = value; settings.settingsEdited({{ target: $(id) }}); }};
const flip = (id, on) => {{ $(id).checked = on; settings.settingsEdited({{ target: $(id) }}); }};
const checks = () => net.calls.filter((c) => c.path === "/api/settings/check");
const puts = () => net.calls.filter((c) => c.method === "PUT");
const barView = () => [$("s-bar").hidden, $("s-bar").className, $("s-bar-text").textContent,
  ["s-reload", "s-discard", "s-save"].filter((id) => !$(id).hidden)];
"""
)


def run(script: str) -> Any:
    return run_js(SETUP + script)


# -- sections (spec §1) ------------------------------------------------------------


def test_the_sections_are_the_urls_allowlist() -> None:
    script = "done([feed.SECTIONS, Object.keys(settings.SECTION_TARGETS)]);"
    assert run(script) == [SECTIONS, SECTIONS]


def test_each_section_scrolls_to_its_box_and_focuses_its_heading() -> None:
    script = """
const seen = [];
for (const [section, [box, heading]] of Object.entries(settings.SECTION_TARGETS)) {
  settings.focusSection(section);
  seen.push([section, $(box).scrolled, doc.activeElement === $(heading), $(heading).getAttribute("tabindex")]);
}
done(seen);
"""
    assert run(script) == [[s, 1, True, "-1"] for s in SECTIONS]


def test_no_section_or_an_unknown_one_moves_nothing() -> None:
    script = """
const before = doc.activeElement;
settings.focusSection("");
settings.focusSection("tools");
done([doc.activeElement === before, $("tg-card").scrolled]);
"""
    assert run(script) == [True, 0]


def test_the_spy_picks_the_last_section_past_the_line_or_the_last_at_the_end() -> None:
    tops = [["telegram", -900], ["notifications", -200], ["schedule", 90], ["patterns", 600]]
    script = f"""
const tops = {json.dumps(tops)};
done([
  model.spySection(tops, 96, false),
  model.spySection(tops, 50, false),
  model.spySection(tops.map(([s]) => [s, 300]), 96, false),
  model.spySection(tops, 96, true),
  model.spySection([], 96, false),
]);
"""
    assert run(script) == ["schedule", "notifications", "telegram", "patterns", ""]


MARKS = 'Object.keys(settings.SECTION_TARGETS).map((s) => $(`s-index-${s}`).getAttribute("aria-current"))'


def test_scrolling_marks_the_section_in_view() -> None:
    script = f"""
const place = (tops) => Object.values(settings.SECTION_TARGETS).forEach(([box], i) => {{ $(box).top = tops[i]; }});
place([-900, -400, 40, 700, 1400]);
settings.spySections({{ y: 1200, atBottom: false }});
const middle = {MARKS};
settings.spySections({{ y: 2400, atBottom: true }});
done([middle, {MARKS}]);
"""
    assert run(script) == [
        ["false", "false", "true", "false", "false"],
        ["false", "false", "false", "false", "true"],
    ]


def test_a_clicked_section_stays_marked_until_the_page_scrolls_on() -> None:
    """Patterns can't reach the spy's line at the end of the page: the click wins
    until the reader scrolls away from where it landed."""
    script = f"""
let y = 2000;
settings.initSettings({{ scrollY: () => y }});
Object.values(settings.SECTION_TARGETS).forEach(([box], i) => {{ $(box).top = [-1500, -1000, -500, 200, 500][i]; }});
settings.focusSection("patterns");
const clicked = {MARKS};
settings.spySections({{ y: 2001, atBottom: true }});  // the click's own scroll event
const held = {MARKS};
y = 1500;
settings.spySections({{ y: 1500, atBottom: false }});  // the reader scrolls up
done([clicked, held, {MARKS}]);
"""
    assert run(script) == [
        ["false", "false", "false", "true", "false"],
        ["false", "false", "false", "true", "false"],
        ["false", "false", "true", "false", "false"],
    ]


# -- This page (spec §5) -----------------------------------------------------------


def test_a_failed_log_out_all_says_why_on_this_page() -> None:
    script = """
settings.askLogout();
net.reply(503, { error: "starting" });
await settings.logoutAll();
done([$("s-page-message").textContent, $("s-logout").hidden, $("s-logout-confirm").hidden]);
"""
    assert run(script) == ["Unavailable: starting", False, True]


# -- Patterns (spec §5) ------------------------------------------------------------


def test_the_built_in_disclosures_count_from_the_payload() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
done([
  ["b-defaults-title", "b-excluded-title", "b-redact-title"].map((id) => $(id).textContent),
  ["b-defaults", "b-excluded", "b-redact"].map((id) => [...$(id).children].map((n) => n.textContent)),
]);
"""
    assert run(script) == [
        [
            "Always watched (built in, 2)",
            "Always excluded folders (built in, 1)",
            "Always redacted (built in, 1)",
        ],
        [["package.json", "*.lock"], [".git"], [".env*"]],
    ]


# -- the model (spec §2-§4): pure ----------------------------------------------------

CONFLICT = "Config changed on disk. Reload to load it (your unsaved changes here are discarded)."
SAVED = {
    "quiet_seconds": "2",
    "max_wait_seconds": "30",
    "max_lines": "20",
    "quiet_hours": "22:30-07:00",
    "heartbeat": None,
    "machine": None,
    "defaults": ["Procfile"],
    "ignore": [],
    "redact": [],
    "editor": "vscode",
}


def value(expression: str) -> Any:
    return run(f"done({expression});")


def test_saved_values_are_text_as_the_fields_show_them() -> None:
    older = {**forms_js.SETTINGS, "web": {"port": 8765}}  # no notify, no editor
    assert value(f"model.savedValues({json.dumps(SETTINGS)})") == SAVED
    assert value(f"model.savedValues({json.dumps(older)})") == {
        **SAVED,
        "quiet_hours": None,
        "heartbeat": "09:00",
        "editor": "cursor",
    }
    halves = {**SETTINGS, "debounce": {"quiet_seconds": 2.5, "max_wait_seconds": 30}}
    assert value(f"model.savedValues({json.dumps(halves)}).quiet_seconds") == "2.5"


def test_a_list_is_its_lines_as_typed_without_trailing_empty_ones() -> None:
    assert value('["a\\n  b \\n\\n  \\n", "", "\\na"].map(model.listLines)') == [
        ["a", "  b "],
        [],
        ["", "a"],
    ]


def test_dirty_fields_compare_text_as_typed() -> None:
    script = f"""
const saved = model.savedValues({json.dumps(SETTINGS)});
const changed = (patch) => model.dirtyFields(saved, {{ ...saved, ...patch }});
done([
  changed({{}}),
  changed({{ defaults: model.listLines("Procfile\\n\\n") }}),
  changed({{ quiet_seconds: "2.0" }}),
  changed({{ defaults: model.listLines("\\nProcfile") }}),
  changed({{ quiet_hours: null, heartbeat: "09:00", editor: "none" }}),
  model.dirtyFields(null, saved),
  model.FIELDS,
]);
"""
    assert run(script) == [
        [],
        [],
        ["quiet_seconds"],
        ["defaults"],
        ["quiet_hours", "heartbeat", "editor"],
        [],
        [
            "quiet_seconds",
            "max_wait_seconds",
            "max_lines",
            "quiet_hours",
            "heartbeat",
            "machine",
            "defaults",
            "ignore",
            "redact",
            "editor",
        ],
    ]


def test_the_body_sends_raw_text_and_trimmed_non_empty_lines() -> None:
    values = {**SAVED, "quiet_seconds": " 2.5 ", "max_lines": "", "defaults": ["  a ", "", "b"]}
    assert value(f"model.settingsBody({json.dumps(values)})") == {
        "debounce": {"quiet_seconds": " 2.5 ", "max_wait_seconds": "30"},
        "diff": {"max_lines": ""},
        "defaults": ["a", "b"],
        "ignore": [],
        "redact": [],
        "notify": {"quiet_hours": "22:30-07:00", "heartbeat": None, "machine": None},
        "web": {"editor": "vscode"},
    }


def test_list_issues_map_back_to_the_textareas_lines() -> None:
    issues = [
        {"line": 2, "reason": "x"},
        {"line": 4, "reason": "stale"},
        {"line": 0, "reason": "no"},
    ]
    expression = f'[model.lineIssuesFor("a\\n\\n  b\\nc", {json.dumps(issues)}), model.lineIssuesFor("a", undefined)]'
    assert value(expression) == [[{"index": 2, "line": 3, "reason": "x"}], []]


def test_has_errors() -> None:
    checks: list[dict[str, Any] | None] = [
        None,
        {"errors": {}, "issues": {}},
        {"errors": {"max_lines": "Enter 0 to 200 lines."}, "issues": {}},
        {"errors": {}, "issues": {"ignore": []}},
        {"errors": {}, "issues": {"ignore": [{"line": 1, "reason": "x"}]}},
    ]
    assert value(f"{json.dumps(checks)}.map(model.hasErrors)") == [
        False,
        False,
        True,
        False,
        True,
    ]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({"count": 0, "invalid": False, "outcome": None}, ["hidden", "", []]),
        (
            {"count": 1, "invalid": False, "outcome": None},
            ["dirty", "1 unsaved change", ["discard", "save"]],
        ),
        (
            {"count": 3, "invalid": False, "outcome": None},
            ["dirty", "3 unsaved changes", ["discard", "save"]],
        ),
        (
            {"count": 2, "invalid": True, "outcome": None},
            [
                "dirty-invalid",
                "2 unsaved changes · fix the highlighted fields to save",
                ["discard", "save"],
            ],
        ),
        ({"count": 2, "invalid": False, "outcome": "saving"}, ["saving", "Saving…", []]),
        ({"count": 0, "invalid": False, "outcome": "waiting"}, ["saving", "Saving…", []]),
        (
            {"count": 1, "invalid": False, "outcome": "waiting"},
            ["dirty", "1 unsaved change", ["discard", "save"]],
        ),
        (
            {"count": 0, "invalid": False, "outcome": "applied"},
            ["applied", "Saved and applied by the daemon.", []],
        ),
        (
            {"count": 1, "invalid": False, "outcome": "applied"},
            ["dirty", "1 unsaved change", ["discard", "save"]],
        ),
        (
            {"count": 0, "invalid": False, "outcome": "saved"},
            ["saved", "Saved: the daemon reloads the config.", []],
        ),
        (
            {"count": 1, "invalid": False, "outcome": "saved"},
            ["dirty", "1 unsaved change", ["discard", "save"]],
        ),
        (
            {"count": 0, "invalid": False, "outcome": "rejected"},
            ["rejected", "Saved, but the daemon rejected the config (see the alert).", []],
        ),
        (
            {"count": 2, "invalid": True, "outcome": "conflict"},
            ["conflict", CONFLICT, ["reload", "discard"]],
        ),
    ],
)
def test_the_save_bar_states(given: dict[str, Any], expected: list[Any]) -> None:
    result = value(f"model.barState({json.dumps(given)})")
    assert [result["state"], result["text"], result["actions"]] == expected
    # A literal per state, so the class-coverage test finds each one in the source.
    assert result["class"] == BAR_CLASSES[expected[0]]


BAR_CLASSES = {
    "hidden": "s-bar s-bar-hidden",
    "dirty": "s-bar s-bar-dirty",
    "dirty-invalid": "s-bar s-bar-dirty-invalid",
    "saving": "s-bar s-bar-saving",
    "applied": "s-bar s-bar-applied",
    "saved": "s-bar s-bar-applied",  # the daemon silent: the same look as applied
    "rejected": "s-bar s-bar-rejected",
    "conflict": "s-bar s-bar-conflict",
}


def test_every_bar_state_has_its_literal_class_in_the_source() -> None:
    source = (
        Path(__file__).parents[2] / "src/diffbell/web/static/js/settings-model.js"
    ).read_text()
    assert [name for name in BAR_CLASSES.values() if f'"{name}"' not in source] == []


def test_the_previews() -> None:
    quiet = {"window": "22:30-07:00", "active": False, "ends_at": None}
    active = {**quiet, "active": True, "ends_at": T + 19 * 3600}  # 07:00 UTC tomorrow
    script = f"""
done([
  model.heartbeatPreview("08:59", "09:00"),
  model.heartbeatPreview("09:00", "09:00"),
  model.heartbeatPreview("23:30", "09:00"),
  model.heartbeatPreview("08:00", null),
  model.quietPreview({json.dumps(quiet)}, false),
  model.quietPreview({json.dumps(active)}, false),
  model.quietPreview({json.dumps(active)}, true),
  model.quietPreview({{ window: null, active: false, ends_at: null }}, false),
  model.quietPreview(null, true),
  model.quietPreview(undefined, false),
  model.quietPreview(null, true, "23:00-08:00", "07:05"),
  model.quietPreview(null, true, "23:00-08:00", "08:00"),
  model.quietPreview(null, true, "09:00-17:00", "12:00"),
  model.quietPreview(null, true, "09:00-17:00", "17:30"),
  model.quietPreview(null, true, null, "12:00"),
  model.quietPreview(null, true, "-17:00", "12:00"),
]);
"""
    assert run(script) == [
        "Next: today 09:00",
        "Next: tomorrow 09:00",
        "Next: tomorrow 09:00",
        "",
        "Not active now · starts 22:30",
        "Active now · ends 07:00",
        "Takes effect when you save.",
        "",
        "Takes effect when you save.",
        "",
        "Once saved: active now · ends 08:00",
        "Once saved: not active now · starts 23:00",
        "Once saved: active now · ends 17:00",
        "Once saved: not active now · starts 09:00",
        "Takes effect when you save.",
        "Takes effect when you save.",
    ]


def test_a_refusal_carries_its_body_and_the_check_is_a_post() -> None:
    script = """
net.reply(422, { error: "Not saved", errors: { max_lines: "Enter 0 to 200 lines." }, issues: {} });
let refused = null;
try { await api.api("PUT", "/api/settings", {}); } catch (err) { refused = [err.status, err.message, err.data.errors]; }
net.reply(200, { errors: {}, issues: { ignore: [{ line: 1, reason: "x" }] } });
const answer = await settings.checkSettings({ diff: { max_lines: "7" } });
const call = net.calls[1];
net.reply(200, {});
const empty = await settings.checkSettings({});
done([refused, [call.method, call.path, call.body, "X-Diffbell-Token" in call.headers], answer, empty]);
"""
    assert run(script) == [
        [422, "Not saved", {"max_lines": "Enter 0 to 200 lines."}],
        ["POST", "/api/settings/check", {"diff": {"max_lines": "7"}}, True],
        {"errors": {}, "issues": {"ignore": [{"line": 1, "reason": "x"}]}},
        {"errors": {}, "issues": {}},
    ]


# -- the rows, the switches and the server's check (spec §3, §4) ---------------------


def notify(quiet: str | None, heartbeat: str | None) -> dict[str, Any]:
    return {**forms_js.SETTINGS, "notify": {"quiet_hours": quiet, "heartbeat": heartbeat}}


def test_load_fills_the_rows_the_switches_and_the_port() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
done([
  ["s-quiet", "s-maxwait", "s-lines", "s-port", "s-editor", "s-defaults", "s-qh-from", "s-qh-to", "s-hb-at"]
    .map((id) => $(id).value),
  [$("s-qh-on").checked, $("s-qh-times").hidden, $("s-hb-on").checked, $("s-hb-times").hidden],
  state.settingsSaved,
]);
"""
    assert run(script) == [
        ["2", "30", "20", "8765", "vscode", "Procfile", "22:30", "07:00", "09:00"],
        [True, False, False, True],
        SAVED,
    ]


def test_a_server_without_notify_shows_quiet_hours_off_and_the_heartbeat_on() -> None:
    script = f"""
await load({json.dumps(forms_js.SETTINGS)});
done([$("s-qh-on").checked, $("s-qh-from").value, $("s-qh-to").value, $("s-qh-times").hidden,
      $("s-hb-on").checked, $("s-hb-at").value, $("s-hb-times").hidden]);
"""
    assert run(script) == [False, "23:00", "08:00", True, True, "09:00", False]


def test_a_failed_load_says_why() -> None:
    """The fields would be empty: the form is hidden and the notice says why."""
    script = """
net.reply(503, { error: "starting" });
await settings.loadSettings();
const index = Object.keys(settings.SECTION_TARGETS).map((s) => Boolean($(`s-index-${s}`).hidden));
done([$("s-load-error").hidden, $("s-load-error-text").textContent, $("settings").hidden, state.settingsSaved, index]);
"""
    assert run(script) == [
        False,
        "Settings can't be shown. Unavailable: starting",
        True,
        None,
        [False, True, True, True, True],
    ]


def test_the_poll_retries_a_failed_load_until_the_config_is_valid_again() -> None:
    script = f"""
net.reply(422, {{ error: "config.yaml is invalid: x: unknown setting" }});
await settings.loadSettings();
const failed = [$("s-load-error-text").textContent, $("settings").hidden];
state.view = "dashboard";
settings.retrySettingsLoad();  // not on Settings: nothing is fetched
const calls = net.calls.length;
state.view = "settings";
net.reply(200, {json.dumps(SETTINGS)});
settings.retrySettingsLoad();
await settings.loadSettings();  // the same load, still in flight
const index = Object.keys(settings.SECTION_TARGETS).map((s) => Boolean($(`s-index-${{s}}`).hidden));
done([failed, calls, net.calls.length, $("s-load-error").hidden, $("settings").hidden, state.settingsLoadError, index]);
"""
    assert run(script) == [
        ["Settings can't be shown. config.yaml is invalid: x: unknown setting", True],
        1,
        2,
        True,
        False,
        "",
        [False, False, False, False, False],
    ]


def test_a_switch_turned_off_sends_null_and_keeps_its_times() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
flip("s-qh-on", false);
const off = [$("s-qh-times").hidden, settings.readValues().quiet_hours,
             model.dirtyFields(state.settingsSaved, settings.readValues())];
flip("s-qh-on", true);
const back = [$("s-qh-from").value, settings.readValues().quiet_hours,
              model.dirtyFields(state.settingsSaved, settings.readValues())];
$("s-qh-from").value = "22:30:15";  // a time input can report seconds
done([off, back, settings.readValues().quiet_hours]);
"""
    assert run(script) == [
        [True, None, ["quiet_hours"]],
        ["22:30", "22:30-07:00", []],
        "22:30-07:00",
    ]


def test_edits_are_checked_by_the_server_after_a_pause() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "ab");
edit("s-quiet", "abc");
const early = checks().length;
await pause();
const sent = checks().map((c) => c.body);
net.reply(200, {{ errors: {{ quiet_seconds: "Enter a number of seconds." }}, issues: {{}} }});
await flush();
done([early, sent.length, sent[0].debounce, "config_version" in sent[0],
      $("s-quiet-error").textContent, $("s-quiet").getAttribute("aria-invalid"),
      $("s-maxwait").getAttribute("aria-invalid")]);
"""
    assert run(script) == [
        0,
        1,
        {"quiet_seconds": "abc", "max_wait_seconds": "30"},
        False,
        "Enter a number of seconds.",
        "true",
        "false",
    ]


def test_one_check_in_flight_and_an_older_answer_is_dropped() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "x");
await pause();
const first = checks().length;
edit("s-quiet", "y");
await pause();  // its check came due while the first is in flight: it waits
const waiting = checks().length;
net.reply(200, {{ errors: {{ quiet_seconds: "about x" }}, issues: {{}} }});
await flush();
const dropped = [$("s-quiet-error").textContent, checks().length];
net.reply(200, {{ errors: {{}}, issues: {{}} }});
await flush();
done([first, waiting, dropped, checks().map((c) => c.body.debounce.quiet_seconds),
      $("s-quiet").getAttribute("aria-invalid")]);
"""
    assert run(script) == [1, 1, ["", 2], ["x", "y"], "false"]


def test_list_issues_mark_the_textareas_own_lines() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-ignore", "ok\\n\\nx[z-a]");
await pause();
const sent = checks()[0].body.ignore;
net.reply(200, {{ errors: {{}}, issues: {{ ignore: [{{ line: 2, reason: "bad range" }}] }} }});
await flush();
done([sent, state.lineIssues["s-ignore"], $("s-ignore-issues").textContent,
      [...$("s-ignore-gutter").children].map((li) => li.className),
      $("s-ignore").getAttribute("aria-invalid")]);
"""
    assert run(script) == [
        ["ok", "x[z-a]"],
        [{"index": 2, "line": 3, "reason": "bad range"}],
        "line 3: bad range",
        ["mark ok", "mark", "mark bad"],
        "true",
    ]


def test_a_message_about_no_field_on_the_page_is_still_shown() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-qh-to", "22:30");
await pause();
net.reply(200, {{ errors: {{ quiet_hours: "Start and end must differ." }}, issues: {{}} }});
await flush();
done([$("s-other-error").textContent, $("s-qh-to-error").textContent]);
"""
    assert run(script) == ["Start and end must differ.", ""]


def test_an_empty_heartbeat_time_is_the_servers_call() -> None:
    script = f"""
await load({json.dumps(notify(None, "09:00"))});
edit("s-hb-at", "");
await pause();
const sent = checks()[0].body.notify;
net.reply(200, {{ errors: {{ heartbeat: "Use a time like 23:00." }}, issues: {{}} }});
await flush();
done([sent, $("s-hb-at-error").textContent, $("s-hb-at").getAttribute("aria-invalid"),
      $("s-hb-preview").textContent]);
"""
    assert run(script) == [
        {"quiet_hours": None, "heartbeat": "", "machine": None},
        "Use a time like 23:00.",
        "true",
        "",
    ]


def test_the_schedule_previews_follow_the_dashboard_and_the_fields() -> None:
    quiet = {"window": "22:30-07:00", "active": False, "ends_at": None}
    script = f"""
state.dash = {{ quiet: {json.dumps(quiet)} }};
await load({json.dumps(SETTINGS)});
const idle = $("s-qh-preview").textContent;
state.dash = {{ quiet: {{ ...state.dash.quiet, active: true, ends_at: {T + 19 * 3600} }} }};
settings.renderSettings();  // the dashboard poll
const active = $("s-qh-preview").textContent;
edit("s-qh-from", "21:00");
const changed = $("s-qh-preview").textContent;
flip("s-hb-on", true);
const next = [$("s-hb-times").hidden, $("s-hb-preview").textContent];
edit("s-hb-at", "13:00");
done([idle, active, changed, next, $("s-hb-preview").textContent]);
"""
    assert run(script) == [
        "Not active now · starts 22:30",
        "Active now · ends 07:00",
        "Once saved: not active now · starts 21:00",  # the edited window, at 12:00
        [False, "Next: tomorrow 09:00"],
        "Next: today 13:00",
    ]


def test_the_theme_radios_are_never_checked_or_saved_by_the_form() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
settings.settingsEdited({{ target: $("s-theme-dark") }});
await pause();
done([checks().length, settings.settingsDirty()]);
"""
    assert run(script) == [0, False]


def test_only_a_saved_field_schedules_a_check() -> None:
    """An input event bubbles to #settings from anything in the form: only the fields
    PUT /api/settings writes count (4b review: the theme radios, the port, no target)."""
    script = f"""
await load({json.dumps(SETTINGS)});
for (const target of [$("s-theme"), $("s-theme-light"), $("s-port"), null]) {{
  settings.settingsEdited({{ target }});
}}
settings.settingsEdited(undefined);
await pause();
const ignored = checks().length;
edit("s-editor", "cursor");
await pause();
done([ignored, checks().length]);
"""
    assert run(script) == [0, 1]


def test_the_heartbeat_saves_its_time_and_null_when_off() -> None:
    script = f"""
await load({json.dumps(notify(None, "07:30"))});
const loaded = [$("s-hb-on").checked, $("s-hb-at").value, $("s-hb-times").hidden];
$("s-hb-at").value = "06:15:00";  // a time input can report seconds
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
$("s-hb-on").checked = false;
settings.settingsEdited({{ target: $("s-hb-on") }});
const off = $("s-hb-times").hidden;
net.reply(200, {{ config_version: "v3" }});
await settings.saveSettings();
done([loaded, puts()[0].body.notify, off, puts()[1].body.notify, puts()[1].body.config_version]);
"""
    assert run(script) == [
        [True, "07:30", False],
        {"quiet_hours": None, "heartbeat": "06:15", "machine": None},
        True,
        {"quiet_hours": None, "heartbeat": None, "machine": None},
        "v2",
    ]


def test_quiet_hours_save_their_window_and_null_when_off() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
$("s-qh-from").value = "21:00:15";
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
$("s-qh-on").checked = false;
settings.settingsEdited({{ target: $("s-qh-on") }});
net.reply(200, {{ config_version: "v3" }});
await settings.saveSettings();
done(puts().map((c) => c.body.notify));
"""
    assert run(script) == [
        {"quiet_hours": "21:00-07:00", "heartbeat": None, "machine": None},
        {"quiet_hours": None, "heartbeat": None, "machine": None},
    ]


def test_a_refused_save_shows_the_servers_messages_and_focuses_the_first() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
$("s-lines").value = "500";
$("s-ignore").value = "x[z-a]";
net.reply(422, {{ error: "Enter 0 to 200 lines.", errors: {{ max_lines: "Enter 0 to 200 lines." }},
                  issues: {{ ignore: [{{ line: 1, reason: "bad range" }}] }} }});
await settings.saveSettings();
done([puts()[0].body.diff, puts()[0].body.ignore, $("s-lines-error").textContent,
      $("s-ignore-issues").textContent, doc.activeElement === $("s-lines")]);
"""
    assert run(script) == [
        {"max_lines": "500"},
        ["x[z-a]"],
        "Enter 0 to 200 lines.",
        "line 1: bad range",
        True,
    ]


def test_a_known_problem_blocks_the_save_and_focuses_its_field() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-redact", "secrets/");
await pause();
net.reply(200, {{ errors: {{}}, issues: {{ redact: [{{ line: 1, reason: "remove the trailing /" }}] }} }});
await flush();
await settings.saveSettings();
done([puts().length, doc.activeElement === $("s-redact")]);
"""
    assert run(script) == [0, True]


def test_a_failed_check_forgets_the_last_answer_and_the_put_decides() -> None:
    """A check that fails (unreachable, 503) leaves no stale answer behind: problems
    already fixed must not block Save. The PUT's own check (a 422) decides."""
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-maxwait", "1");
await pause();
net.reply(200, {{ errors: {{ max_wait_seconds: "Must be at least the quiet time (2 seconds)." }}, issues: {{}} }});
await flush();
const invalid = barView()[1];
edit("s-maxwait", "60");
await pause();
net.reply(503, {{ error: "starting" }});
await flush();
const after = [state.settingsCheck, barView()[1], $("s-maxwait-error").textContent,
               $("s-maxwait").getAttribute("aria-invalid")];
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
done([invalid, after, puts().length, puts()[0].body.debounce.max_wait_seconds]);
"""
    assert run(script) == [
        "s-bar s-bar-dirty-invalid",
        [None, "s-bar s-bar-dirty", "", "false"],
        1,
        "60",
    ]


# -- the save bar (spec §2) --------------------------------------------------------


def test_the_bar_counts_changed_fields() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
const clean = barView();
edit("s-quiet", "3");
const one = barView();
edit("s-lines", "10");
const two = barView();
edit("s-quiet", "2");
done([clean, one, two, barView()]);
"""
    one = [False, "s-bar s-bar-dirty", "1 unsaved change", ["s-discard", "s-save"]]
    assert run(script) == [
        [True, "s-bar s-bar-hidden", "", []],
        one,
        [False, "s-bar s-bar-dirty", "2 unsaved changes", ["s-discard", "s-save"]],
        one,
    ]


def test_an_invalid_field_marks_the_bar_and_save_focuses_it() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-maxwait", "1");
await pause();
net.reply(200, {{ errors: {{ max_wait_seconds: "Must be at least the quiet time (2 seconds)." }}, issues: {{}} }});
await flush();
const invalid = [...barView(), $("s-save").getAttribute("aria-disabled")];
await settings.saveSettings();
done([invalid, puts().length, doc.activeElement === $("s-maxwait")]);
"""
    assert run(script) == [
        [
            False,
            "s-bar s-bar-dirty-invalid",
            "1 unsaved change · fix the highlighted fields to save",
            ["s-discard", "s-save"],
            "true",
        ],
        0,
        True,
    ]


def test_nothing_changed_saves_nothing() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
await settings.saveSettings();
done([puts().length, barView()[0]]);
"""
    assert run(script) == [0, True]


def test_a_save_says_saving_until_the_daemon_applies_it() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
const saving = settings.saveSettings();
const during = barView();
net.reply(200, {{ config_version: "v2" }});
await saving;
const waiting = [barView(), state.settingsVersion, settings.settingsDirty()];
state.dash = {{ daemon: {{ config_version: "v1", config_rejected_version: null }} }};
settings.reportSettingsSave();
const still = barView()[2];
state.dash = {{ daemon: {{ config_version: "v2", config_rejected_version: null }} }};
settings.reportSettingsSave();
const applied = barView();
await fire(live().find((t) => t.ms === 4000));
done([during, waiting, still, applied, barView()[0], puts()[0].body.debounce]);
"""
    saving = [False, "s-bar s-bar-saving", "Saving…", []]
    assert run(script) == [
        saving,
        [saving, "v2", False],
        "Saving…",
        [False, "s-bar s-bar-applied", "Saved and applied by the daemon.", []],
        True,
        {"quiet_seconds": "3", "max_wait_seconds": "30"},
    ]


def test_applied_hides_on_the_next_edit() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
state.dash = {{ daemon: {{ config_version: "v2", config_rejected_version: null }} }};
settings.reportSettingsSave();
const applied = barView()[2];
edit("s-quiet", "3");  // an edit that changes nothing: the bar has nothing left to say
done([applied, barView()[0], state.settingsOutcome]);
"""
    assert run(script) == ["Saved and applied by the daemon.", True, None]


def test_a_silent_daemon_says_saved_then_hides() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
state.dash = {{ daemon: {{ config_version: "v1", config_rejected_version: null }} }};
const polls = [];
for (let i = 0; i < 3; i += 1) {{
  settings.reportSettingsSave();
  polls.push(barView()[2]);
}}
const saved = barView();
await fire(live().find((t) => t.ms === 4000));
done([polls, saved, barView()[0], state.settingsOutcome]);
"""
    assert run(script) == [
        ["Saving…", "Saving…", "Saved: the daemon reloads the config."],
        [False, "s-bar s-bar-applied", "Saved: the daemon reloads the config.", []],
        True,
        None,
    ]


def test_a_rejected_save_stays_until_the_next_edit() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
state.dash = {{ daemon: {{ config_version: "v1", config_rejected_version: "v2" }} }};
settings.reportSettingsSave();
const rejected = [barView(), live().length];
edit("s-lines", "5");
done([rejected, barView()]);
"""
    assert run(script) == [
        [
            [
                False,
                "s-bar s-bar-rejected",
                "Saved, but the daemon rejected the config (see the alert).",
                [],
            ],
            0,
        ],
        [False, "s-bar s-bar-dirty", "1 unsaved change", ["s-discard", "s-save"]],
    ]


def test_an_edit_while_waiting_drops_the_outcome() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
edit("s-lines", "5");
state.dash = {{ daemon: {{ config_version: "v2", config_rejected_version: null }} }};
settings.reportSettingsSave();
edit("s-lines", "20");
done([barView()[0], state.settingsOutcome]);
"""
    assert run(script) == [True, None]


def test_a_conflict_offers_reload_and_discard() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
net.reply(409, {{ error: "config.yaml changed on disk" }});
await settings.saveSettings();
const conflict = barView();
edit("s-lines", "5");
const still = barView()[1];
const fresh = {{ ...{json.dumps(SETTINGS)}, config_version: "v9", debounce: {{ quiet_seconds: 5, max_wait_seconds: 30 }} }};
await load(fresh);  // what Reload runs
done([conflict, still, [barView()[0], $("s-quiet").value, state.settingsVersion]]);
"""
    assert run(script) == [
        [False, "s-bar s-bar-conflict", CONFLICT, ["s-reload", "s-discard"]],
        "s-bar s-bar-conflict",
        [True, "5", "v9"],
    ]


def test_discard_in_a_conflict_reloads_the_file_as_it_is_now() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
net.reply(409, {{ error: "config.yaml changed on disk" }});
await settings.saveSettings();
const gets = () => net.calls.filter((c) => c.method === "GET" && c.path === "/api/settings").length;
const before = gets();
const fresh = {{ ...{json.dumps(SETTINGS)}, config_version: "v9", debounce: {{ quiet_seconds: 5, max_wait_seconds: 30 }} }};
net.reply(200, fresh);
await settings.discardSettings();
const after = [gets() - before, barView()[0], $("s-quiet").value, $("s-maxwait").value, state.settingsVersion];
edit("s-lines", "7");
net.reply(200, {{ config_version: "v10" }});
await settings.saveSettings();
done([after, puts().at(-1).body.config_version]);
"""
    assert run(script) == [[1, True, "5", "30", "v9"], "v9"]


def test_a_save_that_fails_otherwise_says_why_and_keeps_the_edits() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "3");
net.reply(503, {{ error: "starting" }});
await settings.saveSettings();
const unavailable = [$("s-message").textContent, barView()[1], settings.settingsDirty()];
net.reply(401, {{ error: "no session" }});
await settings.saveSettings();
done([unavailable, $("s-message").textContent]);
"""
    assert run(script) == [
        ["Unavailable: starting", "s-bar s-bar-dirty", True],
        "Not saved: your session has ended. Run diffbell web to log in again.",
    ]


def test_discard_restores_the_saved_fields_and_clears_the_messages() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
edit("s-quiet", "x");
flip("s-qh-on", false);
await pause();
net.reply(200, {{ errors: {{ quiet_seconds: "Enter a number of seconds." }}, issues: {{}} }});
await flush();
const before = net.calls.length;
settings.discardSettings();
done([$("s-quiet").value, $("s-qh-on").checked, $("s-qh-times").hidden, $("s-quiet-error").textContent,
      barView()[0], settings.settingsDirty(), net.calls.length - before]);
"""
    assert run(script) == ["2", True, False, "", True, False, 0]


def test_the_theme_never_shows_the_bar() -> None:
    script = f"""
await load({json.dumps(SETTINGS)});
settings.settingsEdited({{ target: $("s-theme-light") }});
done(barView()[0]);
"""
    assert run(script) is True


# -- this Mac's name (machine name spec §4, ADR 0023) ---------------------------------------


def machine_settings(name: object, detected: object = "Test Mac") -> dict[str, Any]:
    return {
        **forms_js.SETTINGS,
        "notify": {
            "quiet_hours": None,
            "heartbeat": "09:00",
            "machine": name,
            "machine_default": detected,
        },
    }


def test_the_model_reads_the_name_in_and_out() -> None:
    script = """
done([
  [null, "", "Work MBP"].map((m) => model.savedValues({ ...SAMPLE, notify: { machine: m } }).machine),
  model.savedValues({ ...SAMPLE, notify: {} }).machine,
  [null, "", "Work MBP"].map(model.machineFields),
  [[true, ""], [true, "   "], [true, " Work MBP "], [false, "Work MBP"], [false, ""]]
    .map(([on, text]) => model.machineValue(on, text)),
  model.settingsBody({ ...model.savedValues({ ...SAMPLE, notify: { machine: "M" } }) }).notify.machine,
]);
""".replace("SAMPLE", json.dumps(forms_js.SETTINGS))
    assert run(script) == [
        [None, "", "Work MBP"],
        None,
        [{"on": True, "text": ""}, {"on": False, "text": ""}, {"on": True, "text": "Work MBP"}],
        [None, None, "Work MBP", "", ""],
        "M",
    ]


def test_a_named_mac_loads_the_switch_the_field_and_the_placeholder() -> None:
    script = f"""
const seen = [];
for (const payload of {
        json.dumps(
            [
                machine_settings("Work MBP"),
                machine_settings(""),
                machine_settings(None),
                machine_settings(None, None),
            ]
        )
    }) {{
  await load(payload);
  seen.push([$("s-machine-on").checked, $("s-machine-name").value,
             $("s-machine-name").getAttribute("placeholder"), $("s-machine-field").hidden]);
}}
done(seen);
"""
    assert run(script) == [
        [True, "Work MBP", "Test Mac", False],
        [False, "", "Test Mac", True],
        [True, "", "Test Mac", False],
        [True, "", "", False],
    ]


def test_the_name_saves_its_text_empty_for_off_and_null_for_the_computer_name() -> None:
    script = f"""
await load({json.dumps(machine_settings(None))});
edit("s-machine-name", "  Work MBP ");
const dirty = model.dirtyFields(state.settingsSaved, settings.readValues());
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
flip("s-machine-on", false);
const hidden = $("s-machine-field").hidden;
net.reply(200, {{ config_version: "v3" }});
await settings.saveSettings();
flip("s-machine-on", true);
edit("s-machine-name", "");
net.reply(200, {{ config_version: "v4" }});
await settings.saveSettings();
done([dirty, puts().map((p) => p.body.notify.machine), hidden]);
"""
    assert run(script) == [["machine"], ["Work MBP", "", None], True]


def test_the_servers_message_shows_under_the_name() -> None:
    message = "Use at most 40 characters, without control characters."
    script = f"""
await load({json.dumps(machine_settings(None))});
edit("s-machine-name", "x".repeat(41));
await pause();
net.reply(200, {{ errors: {{ machine: {json.dumps(message)} }}, issues: {{}} }});
await flush();
done([$("s-machine-name-error").textContent, $("s-machine-name").getAttribute("aria-invalid"),
      checks()[0].body.notify.machine]);
"""
    assert run(script) == [message, "true", "x" * 41]
