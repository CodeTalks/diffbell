"""The page and its assets: served with the CSP, one ES module entry, nothing the
CSP would block, and modules that are import-safe (UI refine spec §4.1, §6, §7)."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

import httpx
import pytest

from diffbell.config import (
    ALWAYS_EXCLUDED_DIRS,
    BUILTIN_DEFAULTS,
    BUILTIN_REDACT,
    check_pattern,
)
from diffbell.web import auth
from diffbell.web.app import MAX_PAGE, STATIC_DIR
from tests.web.node import MIN_NODE, REQUIRE_NODE, needs_node, run_js, skip_js_tests
from tests.web.support import SECRET

JS = STATIC_DIR / "js"
MODULES = [
    "api.js",
    "dashboard.js",
    "detail.js",
    "diffview.js",
    "dom.js",
    "editor.js",
    "feed.js",
    "guide.js",
    "keys.js",
    "main.js",
    "settings-model.js",
    "settings.js",
    "shell.js",
    "state.js",
    "status.js",
    "telegram-card.js",
    "telegram.js",
    "theme.js",
    "time.js",
    "toast.js",
    "tree.js",
]
LIBRARY = [m for m in MODULES if m != "main.js"]
BROWSER_GLOBALS = ("document", "window", "location", "history", "localStorage", "sessionStorage")
# Other names of the window object: a library module could reach the DOM through them.
WINDOW_ALIASES = ("self", "top", "parent", "frames")


def html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def source(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def code(name: str) -> str:
    """The module's code: string literals emptied first, then comments removed."""
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', source(name))
    text = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", text)
    text = re.sub(r"`(?:\\.|[^`\\])*`", "``", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*$", "", text, flags=re.M)


def _function_body(js: str, name: str) -> str:
    """The body of `[export] [async] function name(` (brace-matched)."""
    match = re.search(rf"(?:async\s+)?function\s+{name}\s*\(", js)
    assert match, f"function {name} not found"
    # Past the parameter list, which may hold braces of its own (`{ auto } = {}`).
    depth, i = 1, match.end()
    while depth:
        depth += {"(": 1, ")": -1}.get(js[i], 0)
        i += 1
    start = js.index("{", i)
    depth = 0
    for i in range(start, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start : i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# -- serving -----------------------------------------------------------------------


async def test_index_is_served_to_a_session_with_the_csp(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == auth.CSP
    assert SECRET not in response.text


def test_index_loads_exactly_one_module_script() -> None:
    scripts = re.findall(r"<script\b[^>]*>.*?</script>", html(), re.S | re.I)
    assert scripts == ['<script type="module" src="/static/js/main.js"></script>']


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("js/main.js", "javascript"),
        ("js/dom.js", "javascript"),
        ("app.css", "text/css"),
        ("favicon.svg", "image/svg+xml"),
        ("icons.svg", "image/svg+xml"),
        ("apple-touch-icon.png", "image/png"),
    ],
)
async def test_assets_are_served(anon: httpx.AsyncClient, name: str, kind: str) -> None:
    response = await anon.get(f"/static/{name}")
    assert response.status_code == 200
    assert kind in response.headers["content-type"]
    assert response.headers["content-security-policy"] == auth.CSP
    # Revalidated on every load, so an upgraded page never mixes old and new files.
    assert response.headers["cache-control"] == "no-cache"


def test_assets_exist() -> None:
    assert sorted(p.name for p in STATIC_DIR.iterdir()) == [
        "app.css",
        "apple-touch-icon.png",
        "favicon.svg",
        "guide.json",
        "icons.svg",
        "index.html",
        "js",
    ]
    assert sorted(p.name for p in JS.iterdir()) == MODULES


# -- the CSP and untrusted text ----------------------------------------------------


def test_no_inline_script_or_style() -> None:
    page = html()
    # Every <script> has a src and an empty body; no <style> blocks, no style="…",
    # no on…= handler attributes, no javascript: URLs.
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", page, re.S | re.I):
        assert "src=" in match.group(1) and match.group(2).strip() == "", match.group(0)
    assert not re.search(r"<style\b", page, re.I)
    assert not re.search(r"\sstyle\s*=", page, re.I)
    assert not re.search(r"\son[a-z]+\s*=", page, re.I)
    assert "javascript:" not in page.lower()


@pytest.mark.parametrize("path", sorted(JS.glob("*.js")), ids=lambda p: p.name)
def test_scripts_never_build_markup_or_compile_strings(path: Path) -> None:
    """File names and diff lines come from the user's disk: only textContent.
    Every file under js/ is scanned, listed in MODULES or not."""
    js = path.read_text(encoding="utf-8")
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        'setAttribute("on',
        "setAttribute('on",
        "setAttribute(`on",
    ):
        assert sink not in js, sink


def test_diff_lines_are_set_as_text() -> None:
    body = _function_body(source("detail.js"), "diffLines")
    # As text, its control characters shown (diffview.js `visible`, test_js_visible.py).
    assert "text: visible(line)" in body


# -- modules -------------------------------------------------------------------------


@pytest.mark.parametrize("name", MODULES)
def test_every_module_is_an_es_module(name: str) -> None:
    js = code(name)
    assert re.search(r"^(import|export) ", js, re.M), name
    for commonjs in ("require(", "module.exports", "exports."):
        assert commonjs not in js, (name, commonjs)
    if name != "main.js":
        assert re.search(r"^export ", js, re.M), name


@pytest.mark.parametrize("name", LIBRARY)
def test_only_main_names_the_browser_globals(name: str) -> None:
    js = code(name)
    for word in (*BROWSER_GLOBALS, *WINDOW_ALIASES):
        assert not re.search(rf"\b{word}\b", js), (name, word)
    assert not re.search(r"\bglobalThis\s*\[", js), name
    # Only hooks.fetch(…) in api.js (and the `fetch:` key of its hooks object).
    assert not re.search(r"\bfetch\b(?!\s*:)", js.replace("hooks.fetch", "")), name


def test_main_boots_on_the_window() -> None:
    assert re.search(r"^boot\(window\);$", source("main.js"), re.M)


@needs_node
@pytest.mark.parametrize("name", LIBRARY)
def test_modules_are_import_safe(name: str) -> None:
    """Imported on its own in node, with traps on the browser globals: nothing is
    touched while the module (and everything it imports) loads."""
    script = f"""
const touched = [];
for (const name of {json.dumps([*BROWSER_GLOBALS, *WINDOW_ALIASES, "fetch"])}) {{
  Object.defineProperty(globalThis, name, {{
    configurable: true,
    get() {{ touched.push(name); return undefined; }},
  }});
}}
await import(js({json.dumps(name)}));
// Work a module starts at load time (a promise, a 0 ms timer) runs before the verdict.
await settle();
await settle();
done(touched);
"""
    assert run_js(script) == []


def test_node_can_be_required_instead_of_skipped() -> None:
    old = (22, 6)
    assert skip_js_tests(old, {}) is True
    assert skip_js_tests((0, 0), {REQUIRE_NODE: "0"}) is True
    assert skip_js_tests(old, {REQUIRE_NODE: "1"}) is False  # run_js then fails loudly
    assert skip_js_tests(MIN_NODE, {}) is False


# -- icons ---------------------------------------------------------------------------


def sprite_ids() -> set[str]:
    return set(re.findall(r'<symbol id="([a-z-]+)"', (STATIC_DIR / "icons.svg").read_text()))


def test_the_sprite_has_the_spec_icons() -> None:
    expected = {
        "watch", "file", "folder", "created", "modified", "deleted", "lock", "sent",
        "pending", "retry", "failed", "chevron", "search", "undo", "keyboard", "warning",
        "diffbell", "diffbell-wordmark", "theme-system", "theme-light", "theme-dark",
    }  # fmt: skip
    assert expected <= sprite_ids()


def test_icons_come_only_from_the_sprite() -> None:
    ids = sprite_ids()
    page = html()
    assert "<img" not in page
    hrefs = re.findall(r'<use href="([^"]+)"', page)
    assert hrefs
    for href in hrefs:
        path, _, name = href.partition("#")
        assert path == "/static/icons.svg" and name in ids, href
    assert 'export const ICONS = "/static/icons.svg";' in source("dom.js")
    for name in MODULES:
        for icon in re.findall(r'\bicon\("([a-z-]+)"', source(name)):
            assert icon in ids, (name, icon)
        for icon in re.findall(r'\bsetIcon\([^,]+, "([a-z-]+)"\)', source(name)):
            assert icon in ids, (name, icon)
        for icon in re.findall(r'icon: "([a-z-]+)"', source(name)):
            assert icon in ids, (name, icon)
        for icon in re.findall(r'"(created|modified|deleted)"', source(name)):
            assert icon in ids, (name, icon)


# -- behaviour that the polled page relies on (structure) ----------------------------


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("dashboard.js", "renderCards"),
        ("feed.js", "renderProjectFilter"),
        ("feed.js", "renderFeed"),
        ("editor.js", "renderStrip"),
    ],
)
def test_polled_views_update_in_place(module: str, name: str) -> None:
    """A poll every 5 s must not rebuild these nodes (that drops keyboard focus and
    closes an open <select>): they are reconciled by key instead."""
    body = _function_body(source(module), name)
    assert "replaceChildren" not in body, name
    assert "reconcile(" in body, name


def test_poll_merges_the_newest_page_into_the_loaded_rows() -> None:
    refresh = _function_body(source("main.js"), "refresh")
    assert "state.events = page.events" not in refresh  # would undo "Load more"
    assert "mergeNewest(page.events, limit)" in refresh
    assert "Math.min(Math.max(PAGE, state.events.length), MAX_PAGE)" in refresh
    assert f"export const MAX_PAGE = {MAX_PAGE};" in source("feed.js")
    feed = source("feed.js")
    assert "filterGen" in _function_body(feed, "loadMore")
    # setFilter and restoreUrl (Back) both start the list over through filtersApplied.
    assert "filterGen" in _function_body(feed, "filtersApplied")
    assert "filtersApplied()" in _function_body(feed, "setFilter")
    assert "filtersApplied()" in _function_body(feed, "restoreUrl")


def test_load_more_uses_the_before_id_cursor() -> None:
    feed = source("feed.js")
    load_more = _function_body(feed, "loadMore")
    assert "state.events.length, PAGE" not in load_more  # an offset shifts as rows arrive
    assert "state.events[state.events.length - 1].id" in load_more
    assert 'params.set("before_id"' in _function_body(feed, "eventQuery")


def test_tabs_follow_the_wai_aria_keyboard_pattern() -> None:
    main = source("main.js")
    body = _function_body(main, "onTabKey")
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert f'"{key}"' in body, key
    assert 'addEventListener("keydown", onTabKey)' in main


def test_url_filters_are_clamped() -> None:
    feed = source("feed.js")
    assert "MAX_FILTER" in _function_body(feed, "readUrl")
    assert "export const MAX_FILTER = 1024;" in feed


def test_date_bounds_are_dst_safe() -> None:
    assert "86400" not in _function_body(source("feed.js"), "eventQuery")
    assert "86400" not in _function_body(source("time.js"), "localMidnight")
    assert "86400" not in _function_body(source("time.js"), "dayLabel")


def test_session_is_retried_at_startup() -> None:
    assert "setTimeout(start" in _function_body(source("main.js"), "start")


def test_switching_tabs_keeps_unsaved_edits() -> None:
    body = _function_body(source("main.js"), "showView")
    assert "state.projectsLoaded" in body and "renderStrip()" in body
    assert "if (!state.settings) loadSettings()" in body


def test_save_reports_use_the_applied_config_version() -> None:
    editor = source("editor.js")
    assert "version: result.config_version" in _function_body(editor, "saveProject")
    report = _function_body(editor, "reportSave")
    assert "saveOutcome(" in report
    assert re.search(r"state\.editing [!=]== notice\.name", report)
    assert "saveOutcome(" in _function_body(source("settings.js"), "reportSettingsSave")
    assert "reportSettingsSave()" in _function_body(source("main.js"), "refresh")


# -- the markup --------------------------------------------------------------------


@pytest.mark.parametrize("name", MODULES)
def test_every_looked_up_id_is_in_the_page(name: str) -> None:
    """The fake DOM invents an element for an unknown id, so a typo in `$("…")`
    would pass the node tests: the page itself must have each one."""
    page = html()
    for id_ in re.findall(r'\$\("([\w-]+)"\)', source(name)):
        assert f'id="{id_}"' in page, (name, id_)


def test_ids_built_by_main_are_in_the_page() -> None:
    """The ids main.js looks up through a template: the views and the gutters."""
    page = html()
    lists = re.search(r"const LINE_LISTS = \[(.*?)\];", source("main.js"))
    assert lists
    for id_ in re.findall(r'"([\w-]+)"', lists.group(1)):
        for suffix in ("", "-gutter", "-issues"):
            assert f'id="{id_}{suffix}"' in page, id_ + suffix
    for view in re.findall(r'data-view="([a-z]+)"', page):
        assert f'id="view-{view}"' in page, view


def test_every_id_is_unique() -> None:
    ids = re.findall(r'\sid="([^"]+)"', html())
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_number_fields_send_the_raw_text_with_the_unit_inside() -> None:
    """Spec §3, §4: the server parses what was typed (a type=number field would hand
    over "" for "abc"), and each unit sits inside its field."""
    page = html()
    for field, mode, unit in (
        ("s-quiet", "decimal", "seconds"),
        ("s-maxwait", "decimal", "seconds"),
        ("s-lines", "numeric", "lines"),
    ):
        tag = re.search(rf'<input [^>]*id="{field}"[^>]*>', page)
        assert tag, field
        assert 'type="text"' in tag.group(0) and f'inputmode="{mode}"' in tag.group(0), field
        assert "min=" not in tag.group(0) and "step=" not in tag.group(0), field
        assert f'<span class="s-unit" id="{field}-unit">{unit}</span>' in page, field


NOTIFICATION_ROWS = [
    ("s-quiet", "Wait for quiet", "Send once files have stopped changing for this long."),
    ("s-maxwait", "Never wait longer than", "Even if files keep changing, send after this long."),
    ("s-lines", "Diff lines per file", "Shown in Telegram. The full diff is always on this page."),
]


def test_each_notification_setting_is_one_row_with_its_help_below() -> None:
    page = html()
    for field, label, help_ in NOTIFICATION_ROWS:
        assert f'<label class="s-label" for="{field}">{label}</label>' in page, field
        assert f'<p class="s-help" id="{field}-help">{help_}</p>' in page, field
        assert f'<p class="s-error" id="{field}-error"></p>' in page, field
        assert f'aria-describedby="{field}-unit {field}-help {field}-error"' in page, field


def test_schedule_switches_head_their_own_rows() -> None:
    page = html()
    for prefix, title, help_ in (
        (
            "s-qh",
            "Quiet hours",
            "Hold notifications in a daily window, then send one catch-up per project.",
        ),
        (
            "s-hb",
            "Daily heartbeat",
            'A silent "all OK" message, so a quiet day isn\'t mistaken for a stopped Diffbell.',
        ),
    ):
        assert f'<h3 class="s-switch-title" id="{prefix}-title">{title}</h3>' in page
        assert (
            f'<input type="checkbox" role="switch" class="s-switch" id="{prefix}-on" '
            f'aria-labelledby="{prefix}-title" aria-describedby="{prefix}-help">'
        ) in page
        assert f'<p class="s-help" id="{prefix}-help">{help_}</p>' in page
        assert f'<p class="s-preview" id="{prefix}-preview" aria-live="polite"></p>' in page
    for field, label in (("s-qh-from", "From"), ("s-qh-to", "To"), ("s-hb-at", "Every day at")):
        assert f'<label class="s-label" for="{field}">{label}</label>' in page
        assert f'<input type="time" id="{field}" aria-describedby="{field}-error">' in page


def test_the_conflict_copy_says_a_reload_discards_unsaved_changes() -> None:
    """The project editor's banner, and the settings save bar's conflict state (spec
    §2: its sentence reused verbatim, like "Saving…" and the two outcomes)."""
    text = "Config changed on disk. Reload to load it (your unsaved changes here are discarded)."
    banner = re.search(r'<div class="conflict" id="e-conflict" hidden>(.*?)</div>', html(), re.S)
    assert banner
    assert text in " ".join(banner.group(1).split())
    js = source("settings-model.js")
    for copy in (
        f'conflict: "{text}"',
        'saving: "Saving…"',
        'applied: "Saved and applied by the daemon."',
        'rejected: "Saved, but the daemon rejected the config (see the alert)."',
    ):
        assert copy in js, copy


def test_log_out_all_has_an_inline_confirm() -> None:
    confirm = re.search(
        r'<span class="confirm" id="s-logout-confirm" hidden>(.*?)</span>', html(), re.S
    )
    assert confirm
    text = " ".join(re.sub(r"<[^>]+>", "", confirm.group(1)).split())
    assert text == "Log out everywhere, including this browser? Log out · Cancel"


def test_the_shortcut_overlay_lists_every_key() -> None:
    overlay = re.search(
        r'<div class="overlay" id="keys-overlay" hidden>(.*?)</div>\s*</div>', html(), re.S
    )
    assert overlay
    for key in ("⌘S", "Ctrl+S", "/", "↑", "↓", "j", "k", "Esc", "?"):
        assert f"<kbd>{key}</kbd>" in overlay.group(1), key


def test_live_regions_stay_short() -> None:
    """The diff pane is not a live region (a screen reader would read a whole
    diff); a one-line note is. Neither is the status line, whose "last change"
    and "Updated N s ago" tick every minute: a hidden twin without them is."""
    page = html()
    assert '<section class="detail" id="detail" aria-label="Event detail"></section>' in page
    assert '<p class="sr-only" id="detail-live" aria-live="polite"></p>' in page
    line = re.search(r'<p class="status-line" id="status-line">(.*?)</p>', page, re.S)
    assert line and 'id="refreshed"' not in line.group(1)
    assert "aria-live" not in line.group(1)
    assert '<span class="sr-only" id="status-live" aria-live="polite"></span>' in page
    assert 'id="refreshed"' in page


def test_the_tree_and_the_feed_have_honest_roles() -> None:
    """The file tree is a plain nested list (role=tree would promise the arrow
    keys); the feed is a group, so its aria-label is read."""
    page = html()
    assert 'role="tree"' not in page
    assert '<ul class="tree" id="tree" aria-labelledby="tree-title"></ul>' in page
    assert '<div class="feed" id="feed" role="group" aria-label="Events"></div>' in page
    assert "role" not in code("tree.js")
    assert "treeitem" not in source("tree.js")


# -- the stylesheet ------------------------------------------------------------------


def css() -> str:
    return (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def rules() -> dict[str, str]:
    """Selector -> declarations of every top-level rule (media blocks skipped)."""
    top = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css())
    top = re.sub(r"/\*.*?\*/", "", top, flags=re.S)
    return {
        " ".join(sel.split()): " ".join(body.split())
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", top)
    }


SYSTEM_DARK = re.compile(
    r'@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}'
)
FORCED_DARK = re.compile(r':root\[data-theme="dark"\]\s*\{([^}]*)\}')


def dark_tokens() -> str:
    match = SYSTEM_DARK.search(css())
    assert match
    return match.group(1)


def media_rules(query: str) -> dict[str, str]:
    """Selector -> declarations inside every `@media <query>` block."""
    found: dict[str, str] = {}
    pattern = r"@media\s*" + re.escape(query) + r"\s*\{((?:[^{}]*\{[^{}]*\})*)[^{}]*\}"
    for match in re.finditer(pattern, css()):
        body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
        for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            found[" ".join(sel.split())] = " ".join(decl.split())
    return found


def test_focus_rings_are_not_clipped() -> None:
    """A ring drawn outside an overflow:hidden/auto box is cut off: the editor's
    line box rings itself, and rows inside scrollers ring inward."""
    css_rules = rules()
    assert (
        css_rules[".lines:focus-within"] == "outline: 2px solid var(--focus); outline-offset: 2px;"
    )
    assert css_rules[".lines textarea:focus-visible"] == "outline: none;"
    inward = ".row:focus-visible, .file-head:focus-visible, .trow :focus-visible"
    assert css_rules[inward] == "outline-offset: -2px;"
    # The file actions sit in the same clipped box (.file is overflow: hidden).
    assert css_rules[".file-actions :focus-visible"] == "outline-offset: -2px;"


def test_text_on_coloured_buttons_has_its_own_tokens() -> None:
    """White on the dark theme's accent and red is under 4.5:1."""
    light = rules()[":root"]
    for token, dark_value in (("--on-accent", "#0d1117"), ("--on-bad", "#0d1117")):
        assert f"{token}: #fff;" in light
        assert f"{token}: {dark_value};" in dark_tokens()
    assert "color: var(--on-accent);" in rules()["button.primary"]
    assert "color: var(--on-bad);" in rules()[".toast.error"]


def test_status_colours_tint_icons_and_messages_only() -> None:
    """A bare .ok/.warn/.bad rule would tint a whole card or chip."""
    for selector in rules():
        for part in selector.split(","):
            assert part.strip() not in (".ok", ".warn", ".bad"), selector


def test_rows_clear_the_sticky_day_heading_and_widths_line_up() -> None:
    css_rules = rules()
    assert "scroll-margin-top: 32px;" in css_rules[".row"]
    # The header's content is 1280 - 2 * 24 px wide; the views match it.
    assert (
        "max-width: 1280px;" in css_rules[".top-inner"]
        and "padding: 0 24px;" in css_rules[".top-inner"]
    )
    assert css_rules[".view.wide"] == "max-width: 1232px;"
    # Every view has the same width: switching tabs never moves the left edge.
    views = re.findall(r'<section id="view-[a-z]+" class="([^"]+)"', html())
    assert views == ["view wide"] * 4  # the three tabs and the guide
    assert ".view.narrow" not in css_rules


def test_the_toast_region_is_polite() -> None:
    assert '<div class="toasts" id="toasts" aria-live="polite"></div>' in html()


def test_an_aria_disabled_button_looks_disabled() -> None:
    """A button kept focusable but marked aria-disabled reads like a disabled one."""
    assert (
        rules()['button:disabled, button[aria-disabled="true"]']
        == "opacity: 0.55; cursor: default; border-color: var(--border);"
    )


# -- the settings redesign (settings redesign spec §8) --------------------------------


def test_the_knob_token_is_in_all_three_token_blocks() -> None:
    forced = FORCED_DARK.search(css())
    assert forced
    for block in (rules()[":root"], dark_tokens(), forced.group(1)):
        assert "--knob: #fff;" in " ".join(block.split())
    # The switch is a native checkbox: its track is ::before, its knob ::after.
    assert "background: var(--knob);" in rules()[".s-switch::after"]


def test_the_show_failed_link_is_failed_href() -> None:
    """index.html's static href and telegram-card.js's FAILED_HREF never drift."""
    const = re.search(r'^export const FAILED_HREF = "([^"]*)";$', source("telegram-card.js"), re.M)
    link = re.search(r'<a [^>]*\bid="tg-show-failed"[^>]*>', html())
    assert const
    assert link
    href = re.search(r'\bhref="([^"]*)"', link.group(0))
    assert href
    assert unescape(href.group(1)) == const.group(1)


def test_no_style_attribute_anywhere() -> None:
    """The CSP forbids inline styles: none in the page, none set from a module."""
    assert not re.search(r"\sstyle\s*=", html(), re.I)
    for path in sorted(JS.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""\.style\b|["'`]style["'`]|\bstyle\s*:""", text), path.name


def test_settings_rows_stack_below_600_px() -> None:
    assert "grid-template-columns: minmax(0, 200px) minmax(0, 1fr);" in rules()[".s-row"]
    narrow = media_rules("(max-width: 599px)")
    assert narrow[".s-row"] == "grid-template-columns: 1fr;"
    assert narrow[".s-row > .s-label"] == "padding-top: 0;"


def test_the_index_sits_beside_the_column_from_900_px_and_sticks_there() -> None:
    wide = media_rules("(min-width: 900px)")
    assert "grid-template-columns: 160px minmax(0, 760px);" in wide[".s-layout"]
    assert "max-width: 760px;" in rules()[".s-column"]
    # The index is a <nav> holding a <ul>: the list is what wraps.
    assert "flex-wrap: wrap;" in rules()[".s-index ul"]
    assert "flex-direction: column;" in wide[".s-index ul"]
    # Above the column (narrow) it scrolls away; beside it, it stays under the
    # sticky header, and sections scroll to just below that header.
    assert "sticky" not in rules()[".s-index"] + rules()[".s-index ul"]
    assert "position: sticky; top: 65px;" in wide[".s-index"]
    assert "position: sticky; top: 0;" in wide[".top"]
    assert "scroll-margin-top: 65px;" in wide[".s-section, .s-column > .panel, .guide-step"]


def test_the_index_marks_the_section_in_view() -> None:
    css_rules = rules()
    wide = media_rules("(min-width: 900px)")
    assert "border-bottom-color: var(--accent);" in css_rules['.s-index a[aria-current="true"]']
    assert "border-left-color: var(--accent);" in wide['.s-index a[aria-current="true"]']
    assert 'win.addEventListener("scroll", onScroll, { passive: true });' in source("main.js")


def test_the_save_bar_reads_the_same_in_both_themes() -> None:
    css_rules = rules()
    bar = css_rules[".s-bar"]
    for decl in (
        "position: sticky;",
        "bottom: 16px;",
        "background: var(--panel);",
        "border: 1px solid var(--border);",
        "box-shadow: 0 8px 24px rgb(0 0 0 / 20%);",
    ):
        assert decl in bar, decl
    # The state is a class from BAR_CLASS (settings-model.js), one literal per state.
    model = source("settings-model.js")
    for state in ("applied", "rejected", "conflict"):
        assert f'"s-bar s-bar-{state}"' in model, state
    assert css_rules[".s-bar.s-bar-applied"] == "background: var(--add-bg);"
    assert css_rules[".s-bar.s-bar-rejected"] == "background: var(--del-bg);"
    assert (
        css_rules[".s-bar.s-bar-conflict"]
        == "background: var(--banner); border-color: var(--warn);"
    )
    assert "data-state" not in css()


def test_touch_targets_in_the_bar_rows_and_switches() -> None:
    css_rules = rules()
    assert "min-height: 36px;" in css_rules[".s-bar button"]
    assert "min-height: 36px;" in css_rules[".s-section button:not(.link)"]
    assert "min-height: 36px;" in css_rules[".s-input"]
    assert "min-height: 36px;" in css_rules[".s-choice"]
    # The switch's own box is its 44 px hit area; the track drawn inside is 36 x 20.
    switch = css_rules[".s-switch"]
    for decl in ("appearance: none;", "width: 44px;", "height: 44px;"):
        assert decl in switch, decl
    track = css_rules[".s-switch::before"]
    assert "width: 36px;" in track and "height: 20px;" in track


def test_switches_use_muted_off_and_accent_on() -> None:
    css_rules = rules()
    assert "background: var(--muted);" in css_rules[".s-switch::before"]
    assert css_rules[".s-switch:checked::before"] == "background: var(--accent);"
    assert css_rules[".s-switch:checked::after"] == "transform: translateX(16px);"
    # The ring goes round the visible track, not the invisible hit area.
    assert css_rules[".s-switch:focus-visible"] == "outline: none;"
    assert (
        css_rules[".s-switch:focus-visible::before"]
        == "outline: 2px solid var(--focus); outline-offset: 2px;"
    )
    assert media_rules("(prefers-reduced-motion: reduce)")[
        ".s-switch::before, .s-switch::after"
    ] == ("transition: none;")


def test_the_chosen_theme_is_the_selected_chip() -> None:
    chosen = rules()[".s-choice:has(input:checked)"]
    assert "background: var(--selected);" in chosen and "color: var(--text);" in chosen
    assert (
        rules()[".s-choice:has(input:focus-visible)"]
        == "outline: 2px solid var(--focus); outline-offset: 2px;"
    )


def test_the_old_settings_layout_rules_are_gone() -> None:
    css_rules = rules()
    for selector in (
        "#view-settings",
        "#view-settings > .settings",
        "#view-settings > .telegram",
        ".sentence",
        ".sentence input",
        ".quiet-hours, .heartbeat",
        ".quiet-hours input, .heartbeat input",
        ".tools input[readonly]",
    ):
        assert selector not in css_rules, selector
    assert "#view-settings" not in css()


CLASS_PREFIXES = ("s-", "tg-")
QUOTED = r'"([^"]*)"|`([^`]*)`'


def defined_classes() -> set[str]:
    """The s-/tg- classes app.css styles."""
    text = re.sub(r"/\*.*?\*/", "", css(), flags=re.S)
    return set(re.findall(r"\.((?:s|tg)-[a-z0-9-]+)", text))


def class_literals(js: str) -> list[str]:
    """Every class string a module puts on an element: `class:` in h(), every literal
    in a setClass(el, …) argument (a ternary holds several), `tone:` (the Telegram
    card's state class) and the values of a `*_CLASS` map such as BAR_CLASS."""
    found = re.findall(r'\bclass: ["`]([^"`]+)["`]', js)
    found += re.findall(r'\btone: "([^"]+)"', js)
    for arg in re.findall(r"\bsetClass\([^,]+,([^\n]*)", js):
        found += [a or b for a, b in re.findall(QUOTED, arg)]
    for body in re.findall(r"\bconst [A-Z_]*CLASS[A-Z_]* = \{(.*?)\};", js, flags=re.S):
        found += re.findall(r':\s*"([^"]+)"', body)
    return found


def used_classes() -> set[str]:
    """The s-/tg- classes the page and the modules put on elements."""
    literals = re.findall(r'class="([^"]+)"', html())
    for name in MODULES:
        literals += class_literals(source(name))
    return {c for literal in literals for c in literal.split() if c.startswith(CLASS_PREFIXES)}


def test_the_class_harvest_reads_maps_ternaries_and_tones() -> None:
    js = (
        'const BAR_CLASS = {\n  a: "s-bar s-bar-x",\n  "b-c": "s-bar s-bar-y", // why\n};\n'
        'setClass(el, on ? "tg-link" : "primary");\n'
        'setClass($("tg-status"), `tg-status ${card.tone}`.trim());\n'
        'const row = { tone: "tg-ok" };\nh("p", { class: "s-help" });\n'
    )
    classes = {c for literal in class_literals(js) for c in literal.split()}
    for name in ("s-bar", "s-bar-x", "s-bar-y", "tg-link", "tg-status", "tg-ok", "s-help"):
        assert name in classes, name
    assert "s-bar s-bar-applied" in class_literals(source("settings-model.js"))


def test_every_settings_and_card_class_has_a_rule_and_every_rule_is_used() -> None:
    """One vocabulary between the markup (T4, T5, T6) and the stylesheet (T7): an
    unstyled class or a dead rule means two names for one component."""
    assert used_classes() - defined_classes() == set()
    everything = html() + "".join(source(name) for name in MODULES)
    unused = {
        c
        for c in defined_classes()
        if not re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", everything)
    }
    assert unused == set()


def test_the_telegram_card_states_use_the_spec_fills() -> None:
    css_rules = rules()
    assert css_rules[".tg-status.tg-ok .tg-badge"] == "background: var(--add-bg); color: var(--ok);"
    assert (
        css_rules[".tg-status.tg-bad .tg-badge"] == "background: var(--del-bg); color: var(--bad);"
    )
    assert (
        css_rules[".tg-status.tg-warn .tg-badge"]
        == "background: var(--banner); color: var(--warn);"
    )
    notice = css_rules[".tg-status.tg-notice"]
    assert "background: var(--banner);" in notice and "border: 1px solid var(--warn);" in notice
    assert "min-height: 36px;" in css_rules["#tg-card button"]
    assert "min-height: 36px;" in css_rules[".tg-link"]
    assert "overflow-wrap: anywhere;" in css_rules[".tg-error-text"]
    for gone in ("#view-settings > .telegram", ".telegram h3", ".telegram .icon.ok"):
        assert gone not in css_rules, gone


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_telegram_badges_read_in_both_themes(theme: str) -> None:
    """The badge icon is non-text (3:1 on its fill); the error and the notice's text
    are text (4.5:1 on the panel and the banner)."""
    tokens = _hex_tokens(rules()[":root"] if theme == "light" else dark_tokens())
    for fg, fill in (("--ok", "--add-bg"), ("--bad", "--del-bg"), ("--warn", "--banner")):
        assert contrast(tokens[fg], tokens[fill]) >= 3.0, (fg, fill)
    assert contrast(tokens["--bad"], tokens["--panel"]) >= 4.5
    assert contrast(tokens["--text"], tokens["--banner"]) >= 4.5
    assert contrast(tokens["--muted"], tokens["--banner"]) >= 4.5


def test_a_problems_details_float_below_its_badge() -> None:
    """Hovering a badge must not make its card's grid row grow."""
    detail = rules()[".problem .problem-detail"]
    for decl in ("position: absolute;", "top: 100%;", "z-index: 4;", "background: var(--panel);"):
        assert decl in detail


def test_search_marks_in_diff_lines_use_the_mark_token() -> None:
    """The browser's default yellow with black text is unreadable on dark diff rows:
    a <mark> in a diff line takes --mark, defined for both themes."""
    assert "background: var(--mark);" in rules()[".ln mark"]
    assert "color: inherit;" in rules()[".ln mark"]
    assert "--mark:" in rules()[":root"]
    assert "--mark:" in dark_tokens()


def test_the_search_wrapper_is_not_an_editor_field() -> None:
    """The editor's .field has a bottom margin and a gap: in the filter row (aligned
    to the end) it would lift the search box above the other controls."""
    form = re.search(r'<form class="filters".*?</form>', html(), re.S)
    assert form is not None
    classes = re.findall(r'class="([^"]*)"', form.group(0))
    assert not [c for c in classes if "field" in c.split()]
    assert '<div class="grow search-field">' in form.group(0)
    assert (
        rules()[".filters .search-field"]
        == "display: flex; flex-direction: column; margin: 0; gap: 0;"
    )


def test_the_card_corner_cannot_cover_the_name() -> None:
    """The title reserves more than the corner can take (badges wrap inside it), and
    a long name ends in an ellipsis (its full text is the title attribute)."""
    css_rules = rules()

    def px(decls: str, prop: str) -> int:
        found = re.search(rf"(?:^|; ){prop}: (\d+)px;", decls)
        assert found, (prop, decls)
        return int(found.group(1))

    tools = css_rules[".card-tools"]
    assert "flex-wrap: wrap;" in tools and "justify-content: flex-end;" in tools
    assert "flex-wrap: wrap;" in css_rules[".card-problems"]
    reserve = px(css_rules[".card-title"], "padding-right")
    assert reserve >= px(tools, "max-width") + px(tools, "right") + 4
    assert px(tools, "max-width") >= 104  # two badges and Resume, or three and the bell
    name = css_rules[".card-title .name"]
    for decl in (
        "min-width: 0;",
        "overflow: hidden;",
        "text-overflow: ellipsis;",
        "white-space: nowrap;",
    ):
        assert decl in name


def test_the_title_reserves_only_what_the_corner_holds() -> None:
    """Each corner-* class reserves its contents plus the corner's 4 px offset and a
    4 px gap: the bell is 24 px (16 px icon, 4 px padding), a badge about as much,
    Resume about 50 px (12 px text, 4 px padding). Three or more keep the maximum."""
    css_rules = rules()
    assert css_rules[".corner-0 .card-title"] == "padding-right: 0;"
    assert css_rules[".corner-1 .card-title"] == "padding-right: 32px;"
    assert css_rules[".corner-2 .card-title, .corner-resume .card-title"] == "padding-right: 60px;"
    assert css_rules[".corner-3plus .card-title"] == "padding-right: 112px;"
    assert "padding: 4px;" in css_rules[".icon-button"]
    assert "padding: 4px;" in css_rules[".card-snooze .resume"]


def test_a_hovered_or_focused_bar_is_highlighted_whatever_its_height() -> None:
    """`.bar:not(.h0) .fill` has the same specificity as the hover rule: the hover
    (and focus) rule must come after it, and after every height class."""
    order = list(rules())
    highlight = order.index(".bar:hover .fill, .bar:focus-visible .fill")
    assert "background: var(--accent);" in rules()[order[highlight]]
    others = [i for i, sel in enumerate(order) if sel.startswith(".bar") and " .fill" in sel]
    assert max(i for i in others if i != highlight) < highlight


def test_the_lowest_bar_is_taller_than_the_empty_stub() -> None:
    assert rules()[".bar.h0 .fill"] == "height: 2px;"
    assert rules()[".bar.h1 .fill"] == "height: max(10%, 4px);"


# -- the Diffbell brand (branding spec §4) ------------------------------------------


def test_the_header_shows_the_diffbell_lockup() -> None:
    page = html()
    assert "<title>Diffbell</title>" in page
    assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in page
    assert '<use href="/static/icons.svg#diffbell"></use>' in page
    assert '<use href="/static/icons.svg#diffbell-wordmark"></use>' in page
    # The wordmark is a picture: screen readers get the name as text.
    assert '<span class="sr-only">Diffbell</span>' in page
    css_rules = rules()
    # The mark has its own fixed colours (bell-mark spec): the page doesn't colour it.
    assert "color:" not in css_rules[".brand-mark"]
    assert "color: var(--text);" in css_rules[".brand-word"]


def test_the_footer_shows_the_version_copyright_and_licence() -> None:
    page = html()
    assert '<span id="foot-version">Diffbell</span> · © 2026 CodeTalks ·' in page
    # A plain link out: a new tab that can't reach back into the page.
    assert (
        '<a href="https://github.com/CodeTalks/diffbell/blob/main/LICENSE" '
        'target="_blank" rel="noopener noreferrer">MIT License</a>'
    ) in page


def _hex_tokens(block: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for name, value in re.findall(r"(--[a-z-]+):\s*#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", block):
        tokens[name] = value if len(value) == 6 else "".join(c * 2 for c in value)
    return tokens


def _luminance(hex6: str) -> float:
    channels = [int(hex6[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_teal_accent_reads_on_every_surface(theme: str) -> None:
    """WCAG AA (4.5:1) for accent text on every surface and for text on an accent fill."""
    tokens = _hex_tokens(rules()[":root"] if theme == "light" else dark_tokens())
    assert contrast(tokens["--brand"], tokens["--panel"]) >= 3.0  # the mark: WCAG non-text contrast
    for surface in ("--bg", "--panel", "--selected"):
        assert contrast(tokens["--accent"], tokens[surface]) >= 4.5, surface
    assert contrast(tokens["--on-accent"], tokens["--accent"]) >= 4.5
    assert tokens["--focus"] == tokens["--accent"]


# -- the theme switch (theme switch spec §1) ----------------------------------------


def test_forced_dark_is_the_system_dark_theme() -> None:
    """One set of dark tokens: forced dark and system dark can't drift apart."""
    forced = FORCED_DARK.search(css())
    assert forced

    def normalise(body: str) -> str:
        return " ".join(re.sub(r"/\*.*?\*/", "", body, flags=re.S).split())

    assert normalise(forced.group(1)) == normalise(dark_tokens())


def test_native_controls_follow_the_forced_theme() -> None:
    assert "color-scheme: dark;" in " ".join(dark_tokens().split())
    assert rules()[':root[data-theme="light"]'] == "color-scheme: light;"
    assert "color-scheme: light dark;" in rules()[":root"]


def test_the_header_has_the_theme_button() -> None:
    """A quick toggle on every view; Settings → This page holds the same choice."""
    page = html()
    assert '<button type="button" class="icon-button" id="theme-toggle"' in page
    # Before the shortcuts button, and labelled for System (main.js keeps it current).
    assert page.index('id="theme-toggle"') < page.index('id="keys-open"')
    assert 'aria-label="Theme: System (click for Light)"' in page


def test_this_page_has_the_theme_radio_group() -> None:
    page = html()
    group = re.search(
        r'<div class="s-choices" id="s-theme" role="radiogroup"[^>]*>(.*?)</div>', page, re.S
    )
    assert group
    for theme, name in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
        radio = f'<input type="radio" name="s-theme" id="s-theme-{theme}" value="{theme}">'
        icon = (
            '<svg class="icon" aria-hidden="true">'
            f'<use href="/static/icons.svg#theme-{theme}"></use></svg>'
        )
        assert f'<label class="s-choice">{radio}{icon} {name}</label>' in group.group(1), theme
    assert 'aria-describedby="s-theme-help"' in group.group(0)
    help_ = "Saved in this browser only; it changes nothing for the daemon."
    assert f'<p class="s-help" id="s-theme-help">{help_}</p>' in page


def test_filter_controls_share_one_height_so_labels_line_up() -> None:
    """The filter row aligns at the end: equal control heights put every label on one
    line. Safari ignores a native select's height, so the selects drop their native look
    and sit in a .select wrapper that draws the chevron."""
    css_rules = rules()
    key = '.filters select, .filters input[type="date"], .filters .search, .filters button'
    assert css_rules[key] == "height: 32px;"
    assert "appearance: none;" in css_rules[".filters select"]
    assert "pointer-events: none;" in css_rules[".select::after"]
    page = html()
    for select_id in ("f-project", "f-status"):
        assert f'<span class="select"><select id="{select_id}">' in page


def test_scrolling_panes_keep_their_width_and_clear_an_overlay_bar() -> None:
    """The page and the panes get a thin themed scrollbar with a stable gutter (the
    content never shifts when it appears), and feed rows whose counts stay clear of Safari's overlay bar, which takes
    no space and is drawn over the rows' right edge."""
    css_rules = rules()
    panes = css_rules["html, .feed, #tree"]
    for declaration in (
        "scrollbar-gutter: stable;",
        "scrollbar-width: thin;",
        "scrollbar-color: var(--scrollbar) transparent;",
    ):
        assert declaration in panes
    assert (
        "width: 8px;"
        in css_rules["html::-webkit-scrollbar, .feed::-webkit-scrollbar, #tree::-webkit-scrollbar"]
    )
    thumb = css_rules[
        "html::-webkit-scrollbar-thumb, .feed::-webkit-scrollbar-thumb, #tree::-webkit-scrollbar-thumb"
    ]
    assert "background: var(--scrollbar);" in thumb
    assert css_rules[".feed .row"] == "padding-right: 20px;"
    assert "--scrollbar:" in css_rules[":root"]
    assert "--scrollbar:" in dark_tokens()


def test_the_telegram_wait_line_is_live_before_step_3_shows() -> None:
    """The aria-live line isn't inside #tg-find, which is hidden until step 3: a
    region that appears with its text isn't announced."""
    find = re.search(r'<div id="tg-find" hidden>(.*?)</div>', html(), re.S)
    assert find
    assert 'id="tg-waiting"' not in find.group(1)
    assert 'id="tg-waiting" aria-live="polite"' in html()


def test_the_test_message_lives_only_in_the_telegram_card() -> None:
    """Settings redesign spec §6: both buttons in the card; the Tools button is gone."""
    page = html()
    assert 'id="s-test"' not in page and 'id="s-test-hint"' not in page
    assert "sendTestFromSettings" not in source("settings.js") + source("main.js")
    actions = re.search(r'<div class="actions" id="tg-actions">(.*?)</div>', page, re.S)
    assert actions
    assert '<button type="button" id="tg-send-test" hidden>Send test message</button>' in (
        actions.group(1)
    )
    assert (
        '<a class="tg-link" href="?status=failed" id="tg-show-failed" '
        "hidden>Show failed changes</a>"
    ) in actions.group(1)
    assert "Kept in this Mac's Keychain; the page never shows it again." in page
    assert "Saved separately, through its own setup steps." in page
    for gone in ('id="tg-bot"', 'id="tg-chat"', 'id="tg-last"', "Sending to"):
        assert gone not in page, gone


# -- encoding guard ----------------------------------------------------------------

_MOJIBAKE = ("â", "Ã", "Â")  # UTF-8 bytes read as Latin-1: "â", "Ã", "Â"


def _text_files() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    static = root / "src" / "diffbell" / "web" / "static"
    files = [p for p in static.rglob("*") if p.suffix in {".html", ".css", ".js", ".svg", ".json"}]
    files += (root / "docs").rglob("*.md")
    return sorted(files)


def test_text_files_are_utf8_without_mojibake() -> None:
    bad = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8")  # raises on invalid UTF-8
        if any(marker in text for marker in _MOJIBAKE):
            bad.append(path.name)
    assert bad == []


def test_setup_banner_characters_are_real() -> None:
    static = Path(__file__).resolve().parents[2] / "src" / "diffbell" / "web" / "static"
    assert ">Set up ▸<" in (static / "index.html").read_text(encoding="utf-8")
    assert "isn't set up — " in (static / "js" / "dashboard.js").read_text(encoding="utf-8")


def test_settings_and_card_copy_characters_are_real() -> None:
    """The guard above covers every static file; these are the redesign's own
    non-ASCII characters, written as the real ones."""
    assert '.join(" · ")' in source("telegram-card.js")  # "Since 14:02 · 3 changes …"
    assert "Saving…" in source("settings-model.js")
    assert "Loading…" in html()
    assert "How? ▸" in html()


# -- Settings: sections and This page (settings redesign spec §1, §5) ----------------

SECTION_NAMES = {
    "telegram": "Telegram",
    "notifications": "Notifications",
    "schedule": "Schedule",
    "patterns": "Patterns",
    "page": "This page",
}


def section_targets() -> dict[str, tuple[str, str]]:
    block = re.search(r"SECTION_TARGETS = \{(.*?)\};", source("settings.js"), re.S)
    assert block
    pairs = re.findall(r'(\w+): \["([\w-]+)", "([\w-]+)"\]', block.group(1))
    return {name: (box, heading) for name, box, heading in pairs}


def test_settings_has_five_sections_in_order_and_an_index() -> None:
    page = html()
    allow = re.search(r"const SECTIONS = \[(.*?)\];", source("feed.js"))
    assert allow
    assert re.findall(r'"([a-z]+)"', allow.group(1)) == list(SECTION_NAMES)
    targets = section_targets()
    assert list(targets) == list(SECTION_NAMES)
    assert '<nav class="s-index" id="s-index" aria-label="Settings sections">' in page
    for section, name in SECTION_NAMES.items():
        box, heading = targets[section]
        assert f'id="{box}"' in page, box
        assert f'<h2 id="{heading}">{name}</h2>' in page, heading
        link = (
            f'<li><a href="?view=settings&amp;section={section}" id="s-index-{section}"'
            f' data-section="{section}" aria-current="false">'
        )
        assert f"{link}{name}</a></li>" in page, section
    starts = [page.index(f'id="{targets[s][0]}"') for s in SECTION_NAMES]
    assert starts == sorted(starts)


def page_section() -> str:
    box = re.search(r'<section class="s-section" id="s-page"[^>]*>(.*?)</section>', html(), re.S)
    assert box
    return box.group(1)


def test_this_page_holds_the_editor_the_port_and_the_sessions() -> None:
    box = page_section()
    for id_ in (
        "s-editor",
        "s-theme",
        "s-port",
        "s-logout",
        "s-logout-confirm",
        "s-page-message",
    ):
        assert f'id="{id_}"' in box, id_
    assert "Telegram messages link to this dashboard (works on this Mac only)." in box
    page = html()
    assert '<div class="tools panel">' not in page
    assert '<div class="settings-col">' not in page


HINT = (
    '<p class="hint"><code>*.lock</code> matches at any depth · <code>src/**/x.py</code> · '
    "a pattern containing <code>/</code> is matched from the folder root</p>"
)
PATTERN_LISTS = [
    # textarea, label, placeholder lines, help, disclosure id, disclosure title (no count)
    (
        "s-defaults",
        "Extra key files",
        ["infra/*.tf", "deploy/**"],
        "Changes to these send a notification.",
        "b-defaults",
        "Always watched (built in)",
    ),
    (
        "s-ignore",
        "Ignore",
        ["fixtures/**", "*.snap"],
        "Matching changes never notify, even if they match a key file.",
        "b-excluded",
        "Always excluded folders (built in)",
    ),
    (
        "s-redact",
        "Extra redactions",
        ["config/prod.*", "*.sqlite"],
        "Reported by name only; their contents are never opened or sent.",
        "b-redact",
        "Always redacted (built in)",
    ),
]


def patterns_section() -> str:
    box = re.search(
        r'<section class="s-section" id="s-patterns"[^>]*>(.*?)</section>', html(), re.S
    )
    assert box
    return box.group(1)


def test_patterns_open_with_the_intro_and_the_editors_hint() -> None:
    box = patterns_section()
    intro = "Added to the built-in lists for every project. One pattern per line."
    assert f'<p class="s-intro">{intro}</p>' in box
    assert HINT in box
    assert html().count(HINT) == 2  # the project editor's, reused verbatim
    assert '<div class="locked panel">' not in html()
    for area, label, _, help_, list_id, title in PATTERN_LISTS:
        assert f'<label class="s-label" for="{area}">{label}</label>' in box, area
        assert f'<ol class="gutter" id="{area}-gutter" aria-hidden="true"></ol>' in box, area
        assert f'<ul class="line-issues" id="{area}-issues" aria-live="polite"></ul>' in box, area
        assert f'<p class="s-help" id="{area}-help">{help_}</p>' in box, area
        assert f'aria-describedby="{area}-help {area}-issues"' in box, area
        summary = f'<summary id="{list_id}-title">{title}</summary>'
        disclosure = (
            f'<details class="s-builtins">{summary}<ul class="chips" id="{list_id}"></ul></details>'
        )
        assert disclosure in box, list_id


def test_pattern_placeholders_are_valid_and_never_built_in_entries() -> None:
    built_in = {*BUILTIN_DEFAULTS, *BUILTIN_REDACT, *ALWAYS_EXCLUDED_DIRS}
    for area, _, expected, *_ in PATTERN_LISTS:
        tag = re.search(rf'<textarea id="{area}"[^>]*placeholder="([^"]*)"', html())
        assert tag, area
        entries = unescape(tag.group(1)).split("\n")
        assert entries == expected, area
        assert not built_in & set(entries), area
        assert all(check_pattern(entry) is None for entry in entries), area


def test_every_checked_field_has_its_message_line() -> None:
    """settings.js looks these up through a template (`${id}-error`)."""
    page = html()
    block = re.search(r"const FIELD_INPUTS = \{(.*?)\};", source("settings.js"), re.S)
    assert block
    pairs = re.findall(r'(\w+): "([\w-]+)"', block.group(1))
    assert [field for field, _ in pairs] == [
        "quiet_seconds",
        "max_wait_seconds",
        "max_lines",
        "quiet_from",
        "quiet_to",
        "heartbeat",
    ]
    for _, id_ in pairs:
        assert f'<p class="s-error" id="{id_}-error"></p>' in page, id_
        tag = re.search(rf'<input [^>]*id="{id_}"[^>]*>', page)
        assert tag and f"{id_}-error" in tag.group(0), id_  # aria-describedby names it
    lists = re.search(r"const LIST_AREAS = \{(.*?)\};", source("settings.js"))
    assert lists
    for _, id_ in re.findall(r'(\w+): "([\w-]+)"', lists.group(1)):
        assert f'id="{id_}-issues"' in page and f'id="{id_}-gutter"' in page, id_
    for reveal in ("s-qh-times", "s-hb-times"):
        assert f'<div class="s-reveal" id="{reveal}" hidden>' in page


def test_the_page_has_no_settings_rules_of_its_own() -> None:
    """Spec §3: the server owns every rule and message; the page only shows them."""
    js = source("settings.js") + source("settings-model.js")
    for gone in (
        "numberField",
        "must be a number",
        "greater than 0",
        "needs a time",
        "need a start and an end time",
        "must differ",
        "const HHMM",
        "ApiError",
        "checkTextarea",
        "precheck",
    ):
        assert gone not in js, gone


def test_the_saved_controls_are_the_ones_read_values_reads() -> None:
    """settingsEdited ignores any other control's input (the theme radios, the port)."""
    js = source("settings.js")
    controls = re.search(r"const SAVED_CONTROLS = \[(.*?)\];", js, re.S)
    reader = re.search(r"export function readValues\(\) \{(.*?)\n\}", js, re.S)
    assert controls and reader
    read = set(re.findall(r'(?:\$|time)\("(s-[\w-]+)"\)', reader.group(1)))
    assert set(re.findall(r'"(s-[\w-]+)"', controls.group(1))) == read
    assert len(read) == 12


def test_the_save_bar_is_one_polite_region_with_its_three_buttons() -> None:
    page = html()
    bar = re.search(
        r'<div class="s-bar" id="s-bar" aria-live="polite" hidden>(.*?)</div>\s*</div>', page, re.S
    )
    assert bar
    assert '<p class="s-bar-text" id="s-bar-text"></p>' in bar.group(1)
    for button in (
        '<button type="button" id="s-reload" hidden>Reload</button>',
        '<button type="button" id="s-discard" hidden>Discard</button>',
        '<button type="submit" class="primary" id="s-save" hidden>Save changes</button>',
    ):
        assert button in bar.group(1), button
    assert 'id="s-conflict"' not in page
    assert "Save settings" not in page
    assert (
        page.index('id="s-page"')
        < page.index('id="s-bar"')
        < page.index("</form>", page.index('id="s-bar"'))
    )


def test_a_message_about_no_field_is_read_out() -> None:
    """#s-other-error holds the check's messages about no field on the page: like
    the field errors, a screen reader hears them as they arrive."""
    assert '<p class="s-error" id="s-other-error" aria-live="polite"></p>' in html()


def test_main_wires_the_save_bar_and_the_unload_prompt() -> None:
    main = source("main.js")
    for line in (
        '$("settings").addEventListener("submit", saveSettings);',
        '$("s-discard").addEventListener("click", discardSettings);',
        '$("s-reload").addEventListener("click", loadSettings);',
        'w.addEventListener("beforeunload", (event) => {',
    ):
        assert line in main, line
    assert 'if (!$("s-save").disabled) saveSettings();' not in main
