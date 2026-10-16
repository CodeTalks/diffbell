"""Snooze on the cards and the editor's "Notify me about" ticks, under node (notify
controls spec §5.2). The quiet-hours setting is in test_js_settings.py."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from diffbell.web.app import STATIC_DIR
from tests.web import test_js_dashboard as dash_js
from tests.web import test_js_forms as forms_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_dashboard import NOW, T

pytestmark = needs_node

CARDS = """
dashboard.initDashboard({ refresh: () => { refreshes += 1; } });
state.dash = { projects: [
  { name: "web", status: "subscribed", snooze: null },
  { name: "api", status: "subscribed", snooze: { since: 1, until: null } },
], missing: [{ name: "gone", path: "/x" }] };
dashboard.renderCards();
const wrap = (name) => [...$("cards").children].find((n) => n.dataset.key === `p:${name}`);
const tools = (name) => wrap(name).children[1];  // the corner: badges, then snooze
const status = (name) => wrap(name).byClass("state")[0].textContent;
const snoozeBox = (name) => tools(name).byClass("card-snooze")[0];
"""


def run_cards(script: str) -> Any:
    return dash_js.run(CARDS + script)


# -- the menu's times -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tz", "now", "tomorrow_nine"),
    [
        ("UTC", NOW, T + 21 * 3600),
        ("Europe/Berlin", NOW, T + 19 * 3600),  # 09:00 CEST is 07:00 UTC
        # 23:30 in Berlin (21:30 UTC): still the same local day, so 09:00 is 9.5 h away.
        ("Europe/Berlin", NOW + int(9.5 * 3600 * 1000), T + 19 * 3600),
        # 2026-10-24 20:00 CEST: the night has 25 hours, 09:00 CET is 08:00 UTC.
        ("Europe/Berlin", 1_792_864_800_000, 1_792_915_200),
    ],
)
def test_snooze_options_and_tomorrow_nine_in_local_time(
    tz: str, now: int, tomorrow_nine: int
) -> None:
    script = f"""
const m = await import(js("dashboard.js"));
done(m.snoozeOptions({now}));
"""
    options = run_js(script, tz=tz)
    assert [o["label"] for o in options] == [
        "1 hour",
        "4 hours",
        "Until tomorrow 9:00",
        "Until I turn it back on",
    ]
    assert [o["until"] for o in options] == [
        now / 1000 + 3600,
        now / 1000 + 4 * 3600,
        tomorrow_nine,
        None,
    ]


# -- cards --------------------------------------------------------------------------------


def test_cards_offer_snooze_or_show_the_snooze_with_resume() -> None:
    script = """
done([
  tools("web").byClass("snooze").map((b) => [b.getAttribute("aria-label"), b.getAttribute("aria-expanded")]),
  status("api"), texts(tools("api"), "resume"),
  tools("api").byClass("resume")[0].getAttribute("aria-label"),
  snoozeBox("gone").children.length,
  status("web"),
]);
"""
    assert run_cards(script) == [
        [["Snooze web", "false"]],
        "Watching · snoozed",  # the status line says it; the corner offers Resume
        ["Resume"],
        "Resume api",
        0,  # a missing project has nothing to snooze
        "Watching",
    ]


def test_snoozing_from_the_menu_posts_toasts_and_undo_resumes() -> None:
    script = f"""
dashboard.toggleSnoozeMenu("web");
const items = tools("web").byClass("menu-item");
const opened = [items.map((b) => b.textContent), doc.activeElement === items[0],
                tools("web").byClass("snooze")[0].getAttribute("aria-expanded")];
net.reply(200, {{ snooze: {{ since: 1, until: {NOW / 1000 + 3600} }} }});
items[0].click();
await settle();
const after = [net.calls[0].method, net.calls[0].path, net.calls[0].body, status("web"),
               texts($("toasts"), "toast-text"), state.snoozeMenu, refreshes,
               doc.activeElement === tools("web").byClass("resume")[0]];
const undo = $("toasts").byClass("link")[0];
net.reply(204, null);
undo.click();
await settle();
done([opened, after, net.calls[1].method, net.calls[1].path, tools("web").byClass("snooze").length,
      texts($("toasts"), "toast-text"), doc.activeElement === tools("web").byClass("snooze")[0]]);
"""
    opened, after, method, path, buttons, toast, focused = run_cards(script)
    assert opened == [
        ["1 hour", "4 hours", "Until tomorrow 9:00", "Until I turn it back on"],
        True,
        "true",
    ]
    assert after == [
        "POST",
        "/api/projects/web/snooze",
        {"until": NOW / 1000 + 3600},
        "Watching · snoozed until 13:00",
        ["Snoozed web until 13:00"],
        None,
        1,
        True,  # focus moved to Resume
    ]
    assert (method, path, buttons, toast, focused) == (
        "DELETE",
        "/api/projects/web/snooze",
        1,
        ["Notifications on for web"],
        True,  # and back to the snooze button after Undo
    )


def test_a_snoozed_project_whose_folder_is_missing_can_still_be_resumed() -> None:
    script = """
state.dash.missing = [{ name: "gone", path: "/x", snooze: { since: 1, until: null } }];
dashboard.renderCards();
net.reply(204, null);
const shown = [status("gone"), texts(tools("gone"), "resume"),
               tools("gone").byClass("snooze").length];
tools("gone").byClass("resume")[0].click();
await settle();
done([shown, net.calls[0].method, net.calls[0].path, snoozeBox("gone").children.length]);
"""
    assert run_cards(script) == [
        ["Folder missing · snoozed", ["Resume"], 0],
        "DELETE",
        "/api/projects/gone/snooze",
        0,  # resumed: a missing project has nothing to snooze
    ]


def test_the_snooze_time_is_computed_at_click_not_at_render() -> None:
    script = f"""
dashboard.toggleSnoozeMenu("web");
dashboard.initDashboard({{ now: () => {NOW + 60_000} }});  // a minute later
net.reply(200, {{ snooze: {{ since: 1, until: 1 }} }});
tools("web").byClass("menu-item")[0].click();
await settle();
done(net.calls[0].body);
"""
    assert run_cards(script) == {"until": (NOW + 60_000) / 1000 + 3600}


def test_snooze_and_resume_ignore_a_second_press_while_one_is_in_flight() -> None:
    script = """
const first = dashboard.snooze("web", null);
const again = await dashboard.snooze("web", null);
const resumed = await dashboard.resume("web");  // the same project: also ignored
net.reply(200, { snooze: { since: 1, until: null } });
const ok = await first;
const calls = net.calls.length;
const later = dashboard.resume("web");
const twice = await dashboard.resume("web");
const other = dashboard.resume("api");  // another project is not blocked
net.reply(204, null);
net.reply(204, null);
done([again, resumed, ok, calls, twice, await later, await other,
      net.calls.map((c) => `${c.method} ${c.path}`)]);
"""
    assert run_cards(script) == [
        False,
        False,
        True,
        1,
        False,
        True,
        True,
        [
            "POST /api/projects/web/snooze",
            "DELETE /api/projects/web/snooze",
            "DELETE /api/projects/api/snooze",
        ],
    ]


def test_the_menu_keyboard_moves_between_items_and_tab_leaves() -> None:
    """Arrows wrap, Home/End jump; none reaches the page's shortcuts (↓ would move
    the feed's selection). Tab closes the menu, focus back on its button."""
    script = """
const keys = await import(js("keys.js"));
const moved = [];
keys.installKeys(doc, () => "dashboard", (action) => { moved.push(action); return true; });
const root = $("cards");  // connected, so its events reach the document
state.selected = 7;
dashboard.toggleSnoozeMenu("web");
const items = () => tools("web").byClass("menu-item");
const at = () => items().indexOf(doc.activeElement);
const steps = [];
const press = (key) => {
  const event = doc.activeElement.dispatch("keydown", { key });
  steps.push([key, at(), event.defaultPrevented, event.stopped]);
};
for (const key of ["ArrowDown", "ArrowUp", "ArrowUp", "ArrowDown", "End", "Home"]) press(key);
const tab = doc.activeElement.dispatch("keydown", { key: "Tab" });
done([steps, moved, state.selected, state.snoozeMenu, tab.defaultPrevented,
      doc.activeElement === tools("web").byClass("snooze")[0]]);
"""
    steps, moved, selected, menu, tab_prevented, on_button = run_cards(script)
    assert steps == [
        ["ArrowDown", 1, True, True],
        ["ArrowUp", 0, True, True],
        ["ArrowUp", 3, True, True],  # wraps round
        ["ArrowDown", 0, True, True],
        ["End", 3, True, True],
        ["Home", 0, True, True],
    ]
    assert (moved, selected) == ([], 7)  # installKeys never saw the arrows
    assert (menu, tab_prevented, on_button) == (None, False, True)


def test_resume_turns_notifications_back_on() -> None:
    script = """
net.reply(204, null);
tools("api").byClass("resume")[0].click();
await settle();
done([net.calls[0].method, net.calls[0].path, tools("api").byClass("snooze").length,
      doc.activeElement === tools("api").byClass("snooze")[0]]);
"""
    assert run_cards(script) == ["DELETE", "/api/projects/api/snooze", 1, True]


def test_an_indefinite_snooze_toast_has_no_time() -> None:
    script = """
net.reply(200, { snooze: { since: 1, until: null } });
await dashboard.snooze("web", null);
done(texts($("toasts"), "toast-text"));
"""
    assert run_cards(script) == ["Snoozed web"]


def test_a_failed_snooze_says_so_and_changes_nothing() -> None:
    script = """
net.reply(422, { error: "the snooze must end in the future" });
const ok = await dashboard.snooze("web", 1);
done([ok, texts($("toasts"), "toast-text"), tools("web").byClass("snooze").length,
      doc.activeElement === tools("web").byClass("snooze")[0]]);
"""
    assert run_cards(script) == [
        False,
        ["Snooze failed: the snooze must end in the future"],
        1,
        True,
    ]


def test_escape_closes_the_menu_and_returns_focus() -> None:
    script = """
dashboard.toggleSnoozeMenu("web");
const closed = dashboard.closeSnoozeMenu();
done([closed, tools("web").byClass("menu").length, doc.activeElement === tools("web").byClass("snooze")[0],
      dashboard.closeSnoozeMenu()]);
"""
    assert run_cards(script) == [True, 0, True, False]


def test_a_poll_keeps_the_open_menu_and_its_focus() -> None:
    script = """
dashboard.toggleSnoozeMenu("web");
const item = tools("web").byClass("menu-item")[1];
item.focus();
dashboard.renderCards();
done([tools("web").byClass("menu-item")[1] === item, doc.activeElement === item]);
"""
    assert run_cards(script) == [True, True]


def test_snoozed_until_tomorrow_says_tomorrow() -> None:
    script = f"""
done(dashboard.snoozedText({{ until: {T + 21 * 3600} }}, {NOW}));
"""
    assert dash_js.run(script) == "Snoozed until tomorrow 09:00"


# -- the editor's ticks ------------------------------------------------------------------

PROJECT = {
    "name": "proj",
    "path": "/p/proj",
    "patterns": [],
    "ignore": [],
    "use_defaults": True,
    "status": "subscribed",
}


def test_ticks_follow_the_project_and_the_last_one_is_locked() -> None:
    script = f"""
const read = () => editor.NOTIFY_TICKS.map(([, id]) => [$(id).checked, $(id).disabled]);
editor.fillEditor({json.dumps({**PROJECT, "notify_on": ["deleted"]})});
const one = [read(), $("e-notify-hint").hidden, $("e-notify-deleted").title, editor.editorBody().notify_on];
editor.fillEditor({json.dumps(PROJECT)});  // an older daemon: every kind
const all = [read(), $("e-notify-hint").hidden, editor.editorBody().notify_on];
$("e-notify-created").checked = false;
$("e-notify-modified").checked = false;
editor.syncNotifyTicks();
done([one, all, read(), editor.editorBody().notify_on]);
"""
    one, every, after, body = forms_js.run(script)
    assert one == [
        [[False, False], [False, False], [True, True]],
        False,
        "To silence a project for a while, use snooze on the dashboard.",
        ["deleted"],
    ]
    assert every == [
        [[True, False], [True, False], [True, False]],
        True,
        ["created", "modified", "deleted"],
    ]
    assert after == [[False, False], [False, False], [True, True]]
    assert body == ["deleted"]


# -- the markup ------------------------------------------------------------------------


def page() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_the_notify_ticks_and_the_deep_link_note() -> None:
    box = re.search(r'<fieldset class="field notify">(.*?)</fieldset>', page(), re.S)
    assert box
    text = " ".join(re.sub(r"<[^>]+>", " ", box.group(1)).split())
    assert text == (
        "Notify me about New files Changes Deletions "
        "To silence a project for a while, use snooze on the dashboard."
    )
    assert "Telegram messages link to this dashboard (works on this Mac only)." in page()


def test_the_status_filter_offers_held_and_not_notified() -> None:
    assert '<option value="held">Held</option>' in page()
    assert '<option value="skipped">Not notified</option>' in page()
