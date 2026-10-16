"""The forms under node with a fake DOM: the project editor (editor.js), its file
tree (tree.js) and Settings (settings.js), UI refine spec §4.4-§4.6."""

from __future__ import annotations

import json
from typing import Any

import pytest

from diffbell.config import check_pattern
from diffbell.patterns import matches
from tests.web.node import needs_node, run_js

pytestmark = needs_node

SETUP = """
const dom = await import(js("dom.js"));
const { state } = await import(js("state.js"));
const api = await import(js("api.js"));
const editor = await import(js("editor.js"));
const tree = await import(js("tree.js"));
const settings = await import(js("settings.js"));
const doc = new FakeDocument();
dom.useDocument(doc);
const net = fakeFetch();
api.configureApi({ fetch: net.fetch });
const $ = (id) => doc.getElementById(id);
const texts = (el, cls) => el.byClass(cls).map((n) => n.textContent);
state.settings = {};  // loadProjects would fetch it otherwise
"""


def run(script: str) -> Any:
    return run_js(SETUP + script)


# -- per-line validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ('"*.lock"', "remove the quotes around it"),
        ("‘Makefile’", "remove the quotes around it"),
        ("/etc/x", "remove the leading / (patterns start at the folder root)"),
        ("a/../b", "'..' is not allowed"),
        ("a//b", "remove the double /"),
        ("src/", "remove the trailing / (use folder/** for a folder's files)"),
        ("src/**/x.py", None),
        ("it's.txt", None),
    ],
)
def test_precheck(line: str, reason: str | None) -> None:
    assert run(f"done(editor.precheck({json.dumps(line)}));") == reason


@pytest.mark.parametrize(
    "text",
    [
        "*.lock",
        "it's",
        'say "hi"',
        "both ' and \"",
        "back\\slash",
        "tab\there",
        # Not printable for Python: escaped as \xNN, \uNNNN, \UNNNNNNNN.
        "nb\u00a0sp",
        "zw\u200bsp",
        "rtl\u202eo",
        "nel\x85x",
        "ls\u2028x",
        "tag\U000e0041x",
    ],
)
def test_py_repr_matches_python(text: str) -> None:
    assert run(f"done(editor.pyRepr({json.dumps(text)}));") == repr(text)


def test_server_messages_map_back_to_their_lines() -> None:
    bad = ["a[z-a]", "it's[", "x\\y"]
    message = "\n".join(m for p in bad if (m := check_pattern(p))) + "\nnothing to watch"
    text = "ok.txt\n\na[z-a]\nit's[\n  x\\y  \n'quoted'"
    script = f"""
const result = editor.lineIssues({json.dumps(text)}, {json.dumps(message)});
done([result.issues.map((i) => [i.line, i.reason.split(" (")[0]]), result.other]);
"""
    issues, other = run(script)
    assert [line for line, _ in issues] == [3, 4, 5, 6]
    assert issues[0][1] == "unsupported character class"
    assert issues[1][1] == "unclosed '['"
    assert issues[2][1] == "backslash escapes are not supported"
    assert issues[3][1] == "remove the quotes around it"
    assert other == ["nothing to watch"]


def test_the_gutter_marks_each_line() -> None:
    script = """
$("e-patterns").value = "package.json\\n\\n'x'\\n";
const result = editor.checkTextarea("e-patterns");
const gutter = $("e-patterns-gutter");
done([
  [...gutter.children].map((li) => li.className),
  [...$("e-patterns-issues").children].map((li) => li.textContent),
  editor.hasIssues(["e-patterns"]),
]);
"""
    assert run(script) == [
        ["mark ok", "mark", "mark bad", "mark"],
        ["line 3: remove the quotes around it"],
        True,
    ]


def test_save_shows_the_count_and_waits_for_valid_lines() -> None:
    script = """
state.preview = { total: 12, exact: true };
editor.renderCounts();
const ok = [$("e-save").textContent, $("e-save").disabled, $("tree-title").textContent];
state.preview = { total: 500, exact: false };
$("e-patterns").value = "/abs";
editor.checkTextarea("e-patterns");
editor.renderCounts();
const capped = [$("e-save").textContent, $("e-save").disabled];
state.preview = null;
editor.renderCounts();
done([ok, capped, $("e-save").textContent, $("tree-title").textContent]);
"""
    assert run(script) == [
        ["Save · watching 12 files", False, "Will be watched: 12 files"],
        ["Save · watching 500+ files", True],
        "Save",
        "Will be watched: …",
    ]


def test_a_preview_422_marks_lines_and_names_the_folder_problem() -> None:
    message = "\n".join(
        [str(check_pattern("'x'")), "not an existing directory under your home folder"]
    )
    script = f"""
$("e-path").value = "~/nope";
$("e-patterns").value = "'x'";
$("e-mode-defaults").checked = true;
const running = editor.runPreview();
net.reply(422, {{ error: {json.dumps(message)} }});  // the preview
net.reply(422, {{ error: {json.dumps(message)} }});  // the tree's browse
await running;
done([$("e-path-error").textContent, state.lineIssues["e-patterns"].map((i) => i.reason), state.preview]);
"""
    assert run(script) == [
        "not an existing directory under your home folder",
        ["remove the quotes around it"],
        None,
    ]


# -- tree.js -----------------------------------------------------------------------


def entry(name: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "type": "file",
        "redacted": False,
        "excluded": False,
        "matched_by": None,
        "ignored_by": None,
    }
    row.update(fields)
    return row


@pytest.mark.parametrize(
    ("row", "rel", "lines", "expected"),
    [
        (entry("x.txt"), "x.txt", [], [False, False, ""]),
        (entry("x.txt", matched_by="x.txt"), "x.txt", ["x.txt"], [True, False, "watching"]),
        (
            entry("a.lock", matched_by="built-in: *.lock"),
            "a.lock",
            [],
            [True, True, "matched by built-in: *.lock"],
        ),
        (
            entry("package.json", matched_by="built-in: package.json"),
            "package.json",
            [],
            [True, True, "watching"],
        ),
        (
            entry("y.lock", matched_by="*.lock", ignored_by="y.lock"),
            "y.lock",
            [],
            [False, True, "ignored by y.lock"],
        ),
        (
            entry("y.lock", matched_by="y.lock", ignored_by="*.lock"),
            "y.lock",
            ["y.lock"],
            [True, False, "ignored by *.lock"],
        ),
    ],
)
def test_tick_state(row: dict[str, Any], rel: str, lines: list[str], expected: list[Any]) -> None:
    script = f"""
const t = tree.tickState({json.dumps(row)}, {json.dumps(rel)}, {json.dumps(lines)});
done([t.checked, t.disabled, t.badge]);
"""
    assert run(script) == expected


def test_a_wildcard_row_explains_itself() -> None:
    script = f"""
done(tree.tickState({json.dumps(entry("a.lock", matched_by="*.lock"))}, "a.lock", []).title);
"""
    assert run(script) == "matched by *.lock; edit the pattern to change this"


def test_ticking_adds_and_unticking_removes_the_exact_line() -> None:
    script = """
done([
  tree.toggledText("", "a.txt", true),
  tree.toggledText("*.lock", "sub/a.txt", true),
  tree.toggledText("*.lock\\n", "sub/a.txt", true),
  tree.toggledText("a.txt\\n*.lock", "a.txt", true),
  tree.toggledText("a.txt\\n*.lock\\n  a.txt  ", "a.txt", false),
]);
"""
    assert run(script) == [
        "a.txt",
        "*.lock\nsub/a.txt",
        "*.lock\nsub/a.txt",
        "a.txt\n*.lock",
        "*.lock",
    ]


# Names whose characters are glob syntax: a tick must write a line that matches
# the file itself (checked against the server's matcher below).
GLOB_NAMES = {
    "app/[slug]/page.tsx": "app/?slug?/page.tsx",
    "a*b.txt": "a[*]b.txt",
    "what?.md": "what[?].md",
    "x[1]y": "x?1?y",
    "notes/plain.json": "notes/plain.json",
    "Makefile": "Makefile",
    # Characters a line can't hold or check_pattern rejects become "?".
    "a\nb.txt": "a?b.txt",
    "tab\there": "tab?here",
    "del\x7fx": "del?x",
    "nel\x85x": "nel?x",
    "back\\slash": "back?slash",
    " lead.txt": "?lead.txt",
    "trail.txt ": "trail.txt?",
    "  two": "??two",
    "\u00a0nbsp": "?nbsp",
    "dir/ inner .txt": "dir/ inner .txt",
    '"quoted"': '?quoted"',
    "'single'": "?single'",
    "\u2018smart\u2019": "?smart\u2019",
    "it's.txt": "it's.txt",
    "sub/'x'": "sub/'x'",
}


def test_pattern_for_path_escapes_glob_characters() -> None:
    script = f"done({json.dumps(list(GLOB_NAMES))}.map((rel) => tree.patternForPath(rel)));"
    assert run(script) == list(GLOB_NAMES.values())


@pytest.mark.parametrize(("rel", "pattern"), sorted(GLOB_NAMES.items()))
def test_escaped_patterns_are_valid_and_match_the_file(rel: str, pattern: str) -> None:
    """The parity check in pytest: the server accepts the escaped line and its
    matcher (the one Watchman's expression mirrors) matches the file with it."""
    assert check_pattern(pattern) is None
    assert matches(rel, pattern)


def test_the_raw_names_would_not_do() -> None:
    """Why the escaping: a bracketed name read as a glob misses the file itself,
    and a raw "*" or "?" also watches its neighbours."""
    assert not matches("app/[slug]/page.tsx", "app/[slug]/page.tsx")
    assert not matches("x[1]y", "x[1]y")
    assert matches("aXb.txt", "a*b.txt") and not matches("aXb.txt", "a[*]b.txt")
    assert matches("whatX.md", "what?.md") and not matches("whatX.md", "what[?].md")
    for raw in ("back\\slash", " lead.txt", "trail.txt ", '"quoted"', "\u2018smart\u2019"):
        assert check_pattern(raw) is not None


def test_ticking_a_glob_name_writes_the_escaped_line_and_unticking_removes_it() -> None:
    script = """
const rel = "app/[slug]/page.tsx";
const added = tree.toggledText("*.lock", rel, true);
done([
  added,
  tree.toggledText(added, rel, true),
  tree.toggledText(added, rel, false),
  tree.toggledText("app/[slug]/page.tsx\\n*.lock", rel, false),
]);
"""
    assert run(script) == [
        "*.lock\napp/?slug?/page.tsx",
        "*.lock\napp/?slug?/page.tsx",
        "*.lock",
        "*.lock",
    ]


def test_a_line_that_matches_nothing_is_flagged() -> None:
    raw = entry("page.tsx")  # matched_by is null: the typed line doesn't match it
    matched = entry("page.tsx", matched_by="app/?slug?/page.tsx")
    script = f"""
const rel = "app/[slug]/page.tsx";
const typed = tree.tickState({json.dumps(raw)}, rel, [rel]);
const escaped = tree.tickState({json.dumps(matched)}, rel, ["app/?slug?/page.tsx"]);
done([[typed.checked, typed.disabled, typed.badge, typed.warn], [escaped.badge, escaped.warn]]);
"""
    assert run(script) == [
        [True, False, "line doesn't match this name", True],
        ["watching", False],
    ]


ROOT = {
    "dir": "",
    "entries": [
        entry("node_modules", type="dir", excluded=True),
        entry("src", type="dir"),
        entry(".env.local", redacted=True, matched_by="built-in: .env*"),
        entry("notes.txt"),
        entry("x.lock", matched_by="built-in: *.lock", ignored_by="x.lock"),
    ],
    "truncated": False,
    "error": None,
}
SRC = {"dir": "src", "entries": [entry("app.py")], "truncated": True, "error": None}


def tree_script(body: str) -> str:
    return f"""
$("e-path").value = "/home/p";
const lists = {{ path: "/home/p", patterns: [], ignore: [], use_defaults: true }};
net.reply(200, {json.dumps(ROOT)});
await tree.syncTree(lists);
const root = $("tree");
const row = (name) => root.all().find((n) => n.tagName === "LI" && n.byClass("name").length && n.byClass("name")[0].textContent === name);
{body}
"""


def test_the_tree_shows_badges_ticks_and_excluded_folders() -> None:
    script = tree_script(
        """
const rows = root.elements().map((li) => [li.byClass("name")[0].textContent, li.byClass("badge")[0].textContent]);
const env = row(".env.local").byClass("check")[0];
const notes = row("notes.txt").byClass("check")[0];
done([rows, [env.checked, env.disabled], [notes.checked, notes.disabled], row(".env.local").byClass("lock")[0].className, row("notes.txt").byClass("lock")[0].className,
      row("node_modules").byClass("disclose")[0].tagName, net.calls[0].body]);
"""
    )
    assert run(script) == [
        [
            ["node_modules", "excluded"],
            ["src", ""],
            [".env.local", "matched by built-in: .env*"],
            ["notes.txt", ""],
            ["x.lock", "ignored by x.lock"],
        ],
        [True, True],
        [False, False],
        "icon lock",
        "icon lock off",
        "SPAN",
        {"path": "/home/p", "dir": "", "patterns": [], "ignore": [], "use_defaults": True},
    ]


def test_a_folder_opens_one_level_and_notes_a_cap() -> None:
    script = tree_script(
        f"""
net.reply(200, {json.dumps(SRC)});
row("src").byClass("disclose")[0].click();
await settle();
const src = row("src");
const disclose = src.byClass("disclose")[0];
done([disclose.getAttribute("aria-expanded"), src.getAttribute("role"), src.getAttribute("aria-expanded"),
      src.byClass("name").map((n) => n.textContent), texts(src, "tree-note"), net.calls[1].body.dir,
      row("app.py").getAttribute("role"), root.all().filter((n) => n.getAttribute("role")).length]);
"""
    )
    assert run(script) == [
        "true",
        None,
        None,
        ["src", "app.py"],
        ["Showing the first 2,000"],
        "src",
        None,
        0,
    ]


def test_ticking_a_file_writes_the_patterns_line() -> None:
    script = tree_script(
        """
const box = row("notes.txt").byClass("check")[0];
box.checked = true;
box.dispatch("change");
const added = $("e-patterns").value;
done([added, state.editorDirty]);
"""
    )
    assert run(script) == ["notes.txt", True]


def test_the_filter_hides_rows_by_name() -> None:
    script = tree_script(
        """
tree.setTreeFilter("LOCK");
done(root.elements().map((li) => li.byClass("name")[0].textContent));
"""
    )
    assert run(script) == ["x.lock"]


def test_an_unreadable_folder_says_so() -> None:
    unreadable = {"dir": "", "entries": [], "truncated": False, "error": "unreadable"}
    script = f"""
$("e-path").value = "/home/p";
net.reply(200, {json.dumps(unreadable)});
await tree.syncTree({{ path: "/home/p", patterns: [], ignore: [], use_defaults: true }});
done(texts($("tree"), "tree-note"));
"""
    assert run(script) == ["Can't read this folder"]


SRC_PLAIN = {"dir": "src", "entries": [entry("app.py")], "truncated": False, "error": None}
SRC_MATCHED = {
    "dir": "src",
    "entries": [entry("app.py", matched_by="src/app.py")],
    "truncated": False,
    "error": None,
}
NEW_LISTS = '{ path: "/home/p", patterns: ["src/app.py"], ignore: [], use_defaults: true }'


def test_a_closed_folder_is_listed_afresh_when_reopened() -> None:
    script = tree_script(
        f"""
net.reply(200, {json.dumps(SRC_PLAIN)});
row("src").byClass("disclose")[0].click();
await settle();
row("src").byClass("disclose")[0].click();  // closed
net.reply(200, {json.dumps(ROOT)});
await tree.syncTree({NEW_LISTS});  // the patterns changed while it was closed
net.reply(200, {json.dumps(SRC_MATCHED)});
row("src").byClass("disclose")[0].click();
await settle();
done([net.calls.map((c) => c.body.dir), row("app.py").byClass("badge")[0].textContent]);
"""
    )
    assert run(script) == [["", "src", "", "src"], "watching"]


def test_a_folder_listing_older_than_a_sync_is_dropped() -> None:
    script = tree_script(
        f"""
const pending = [];
api.configureApi({{ fetch: (path, options) => new Promise((resolve) => pending.push({{ body: JSON.parse(options.body), resolve }})) }});
const answer = (i, body) => pending[i].resolve({{ ok: true, status: 200, json: async () => body }});
row("src").byClass("disclose")[0].click();  // listed with the old lists
const syncing = tree.syncTree({NEW_LISTS});
await settle();
answer(1, {json.dumps(ROOT)});
await settle(); await settle();
answer(2, {json.dumps(SRC_MATCHED)});
await syncing;
answer(0, {json.dumps(SRC_PLAIN)});  // the folder's own, older reply comes last
await settle(); await settle();
done([pending.map((p) => [p.body.dir, p.body.patterns]), row("app.py").byClass("badge")[0].textContent]);
"""
    )
    assert run(script) == [[["src", []], ["", ["src/app.py"]], ["src", ["src/app.py"]]], "watching"]


def test_a_spoofing_file_name_is_shown_as_text() -> None:
    name = '<img src=x onerror=alert(1)>"\u202egpj.exe'
    listing = {"dir": "", "entries": [entry(name)], "truncated": False, "error": None}
    script = f"""
net.reply(200, {json.dumps(listing)});
await tree.syncTree({{ path: "/home/p", patterns: [], ignore: [], use_defaults: true }});
const span = $("tree").byClass("name")[0];
done([span.textContent, span.children.length, $("tree").byClass("check")[0].getAttribute("aria-label")]);
"""
    assert run(script) == [
        '<img src=x onerror=alert(1)>"\\u202Egpj.exe',
        0,
        'Watch <img src=x onerror=alert(1)>"\\u202Egpj.exe',
    ]


# -- loading and saving (moved from the app.js slicing tests) -----------------------

LOAD = """
state.editing = "a";
state.projects = [{ name: "a", path: "/h/a", patterns: [], ignore: [], use_defaults: true }];
state.projectsVersion = "v1";
state.projectsLoaded = true;
const data = {
  config_version: "v1",
  projects: [
    { name: "a", path: "/h/a", patterns: ["x"], ignore: [], use_defaults: true },
    { name: "b", path: "/h/b", patterns: [], ignore: [], use_defaults: false },
  ],
};
const filled = () => [$("e-path").value, $("e-patterns").value];
$("e-path").value = "typed";
"""


def test_an_automatic_refill_follows_only_the_strip_once_another_project_is_shown() -> None:
    script = (
        LOAD
        + """
const loading = editor.loadProjects("a", { auto: true });
state.editing = "b";  // the person picked another project meanwhile
net.reply(200, data);
await loading;
done([filled(), state.editing, state.projects.length, $("strip").children.length]);
"""
    )
    assert run(script) == [["typed", ""], "b", 2, 3]


def test_an_automatic_refill_skips_a_dirty_editor_but_reload_refills() -> None:
    script = (
        LOAD
        + """
state.editorDirty = true;
let loading = editor.loadProjects("a", { auto: true });
net.reply(200, data);
await loading;
const auto = filled();
loading = editor.loadProjects();  // the explicit Reload button
net.reply(200, data);
await loading;
done([auto, filled()]);
"""
    )
    assert run(script) == [["typed", ""], ["/h/a", "x"]]


def test_an_automatic_refill_of_a_clean_editor_refills_it() -> None:
    script = (
        LOAD
        + """
const loading = editor.loadProjects("a", { auto: true });
net.reply(200, data);
await loading;
done(filled());
"""
    )
    assert run(script) == ["/h/a", "x"]


def test_a_dirty_editor_of_a_just_added_project_is_titled_as_an_edit() -> None:
    script = (
        LOAD
        + """
state.editing = "b";
state.editorDirty = true;
$("editor-title").textContent = "Add a project";
$("e-remove").hidden = true;
const loading = editor.loadProjects("b", { auto: true });
net.reply(200, data);
await loading;
done([$("editor-title").textContent, $("e-remove").hidden, $("e-path").value]);
"""
    )
    assert run(script) == ["Edit b", False, "typed"]


def test_a_save_report_says_nothing_once_another_project_was_picked() -> None:
    script = """
state.editing = "a";
state.projectsVersion = "v1";
state.dash = { daemon: { config_version: "v2" }, projects: [{ name: "a", status: "subscribed", path: "/h/a" }], missing: [] };
state.saveNotice = { name: "a", version: "v2", polls: 0, appliedPolls: 0, before: null };
const reporting = editor.reportSave();
state.editing = "other";  // picked while the GET was pending
net.reply(200, { config_version: "v1", projects: [] });
await reporting;
done([net.calls.length, $("e-message").textContent, state.saveNotice]);
"""
    assert run(script) == [1, "", None]


@pytest.mark.parametrize(
    ("daemon", "outcome"),
    [
        ({"config_version": "v2", "config_rejected_version": "v2"}, [True, False]),
        ({"config_version": "v1", "config_rejected_version": "v2"}, [False, True]),
        ({"config_version": "v2", "config_rejected_version": None}, [True, False]),
        ({"config_version": "v1", "config_rejected_version": "v0"}, [False, False]),
        ({}, [False, False]),  # an older daemon reports neither
    ],
)
def test_save_outcome_checks_applied_before_rejected(
    daemon: dict[str, Any], outcome: list[bool]
) -> None:
    script = f'const o = editor.saveOutcome({json.dumps(daemon)}, {{ version: "v2" }});\ndone([o.applied, o.rejected]);'
    assert run(script) == outcome


def test_a_save_with_bad_lines_maps_the_422() -> None:
    message = str(check_pattern("a[z-a]"))
    script = f"""
state.editing = "a";
state.projectsVersion = "v1";
$("e-path").value = "/h/a";
$("e-patterns").value = "ok\\na[z-a]";
$("e-mode-defaults").checked = true;
const saving = editor.saveProject();
net.reply(422, {{ error: {json.dumps(message)} }});
await saving;
done([state.lineIssues["e-patterns"].map((i) => i.line), $("e-message").textContent, $("e-save").disabled, net.calls[0].method]);
"""
    assert run(script) == [[2], "Not saved: fix the marked lines.", True, "PUT"]


def test_only_the_files_i_list_is_saved_without_the_defaults() -> None:
    script = """
state.editing = "a";
state.projectsVersion = "v1";
$("e-path").value = "/h/a";
$("e-patterns").value = "x.txt";
$("e-mode-defaults").checked = false;
$("e-mode-only").checked = true;
const saving = editor.saveProject();
net.reply(200, { config_version: "v2" });
await saving;
done([net.calls[0].method, net.calls[0].body.use_defaults, net.calls[0].body.patterns]);
"""
    assert run(script) == ["PUT", False, ["x.txt"]]


def test_a_successful_save_moves_the_versions_and_waits_for_the_daemon() -> None:
    script = """
state.editing = "a";
state.projectsVersion = "v1";
state.settingsVersion = "v0";  // a settings form filled from an older version keeps it
state.settings = { config_version: "v1" };
state.dash = { daemon: {}, projects: [{ name: "a", status: "subscribed", path: "/h/a" }], missing: [] };
$("e-path").value = "/h/a";
$("e-mode-defaults").checked = true;
const saving = editor.saveProject();
net.reply(200, { config_version: "v2" });
await saving;
done([state.projectsVersion, state.settingsVersion, state.settings.config_version, state.saveNotice,
      $("e-message").textContent, state.editorDirty, state.saving]);
"""
    assert run(script) == [
        "v2",
        "v0",
        "v2",
        {
            "name": "a",
            "version": "v2",
            "polls": 0,
            "appliedPolls": 0,
            "before": '["subscribed",null,null,"/h/a"]',
        },
        "Saved: reloading…",
        False,
        False,
    ]


SAVED_A = {"name": "a", "path": "/h/a", "patterns": [], "ignore": [], "use_defaults": True}


@pytest.mark.parametrize(
    ("daemon", "status", "before", "reports", "expected"),
    [
        (
            {"config_version": "v2"},
            "subscribed",
            None,
            1,
            ["Saved: a · Watching.", "message ok"],
        ),
        (
            {"config_version": "v1", "config_rejected_version": "v2"},
            "subscribed",
            None,
            1,
            ["Saved, but the daemon rejected the config (see the alert).", "message error"],
        ),
        # Applied, still subscribing: three more polls, then its status as it is.
        (
            {"config_version": "v2"},
            "pending",
            None,
            4,
            ["Saved: a · Starting….", "message ok"],
        ),
        # An older daemon reports no version and nothing changed: three polls.
        (
            {},
            "subscribed",
            '["subscribed",null,null,"/h/a"]',
            3,
            ["Saved. a is still Watching (no new status reported yet).", "message ok"],
        ),
    ],
)
def test_the_save_report_messages(
    daemon: dict[str, Any], status: str, before: str | None, reports: int, expected: list[str]
) -> None:
    script = f"""
state.editing = "a";
state.projectsVersion = "v2";
state.projects = [{json.dumps(SAVED_A)}];
state.dash = {{ daemon: {json.dumps(daemon)}, projects: [{{ name: "a", status: {json.dumps(status)}, path: "/h/a" }}], missing: [] }};
state.saveNotice = {{ name: "a", version: "v2", polls: 0, appliedPolls: 0, before: {json.dumps(before)} }};
for (let i = 1; i < {reports}; i += 1) await editor.reportSave();
const waited = [net.calls.length, state.saveNotice !== null];
net.reply(200, {{ config_version: "v2", projects: [{json.dumps(SAVED_A)}] }});
await editor.reportSave();
done([waited, $("e-message").textContent, $("e-message").className, state.saveNotice]);
"""
    assert run(script) == [[0, True], *expected, None]


def test_the_strip_ignores_a_list_of_another_version() -> None:
    script = f"""
state.editing = "other";
state.projectsVersion = "v2";
state.projects = [{json.dumps(SAVED_A)}];
state.dash = {{ daemon: {{ config_version: "v2" }}, projects: [{{ name: "a", status: "subscribed", path: "/h/a" }}], missing: [] }};
state.saveNotice = {{ name: "a", version: "v2", polls: 0, appliedPolls: 0, before: null }};
const reporting = editor.reportSave();
net.reply(200, {{ config_version: "v9", projects: [] }});  // changed on disk since
await reporting;
done([state.projects.map((p) => p.name), $("strip").children.length]);
"""
    assert run(script) == [["a"], 2]


@pytest.mark.parametrize(
    ("text", "bad", "lines"),
    [
        ("ok\na[z-a]\nb\na[z-a]", "a[z-a]", [2, 4]),  # the same pattern on two lines
        ("A[\na[", "a[", [2]),  # case matters: only the line it names
        ("A[\na[", "A[", [1]),
    ],
)
def test_a_422_marks_exactly_the_lines_it_names(text: str, bad: str, lines: list[int]) -> None:
    script = f"""
state.editing = "a";
state.projectsVersion = "v1";
$("e-path").value = "/h/a";
$("e-patterns").value = {json.dumps(text)};
const saving = editor.saveProject();
net.reply(422, {{ error: {json.dumps(str(check_pattern(bad)))} }});
await saving;
done(state.lineIssues["e-patterns"].map((i) => i.line));
"""
    assert run(script) == lines


# -- Remove and Undo ---------------------------------------------------------------

REMOVE = """
const entry = { name: "shop", path: "/h/shop", patterns: ["*.csv"], ignore: ["tmp/**"], use_defaults: false };
state.projects = [entry];
state.projectsVersion = "v1";
state.editing = "shop";
net.reply(200, { config_version: "v2" });                   // DELETE
net.reply(200, { config_version: "v2", projects: [] });     // the reload
await editor.removeProject();
const toast = $("toasts").textContent;
"""


def test_remove_then_undo_re_adds_the_saved_entry() -> None:
    script = (
        REMOVE
        + """
net.reply(200, { config_version: "v3" });                                    // POST
net.reply(200, { config_version: "v3", projects: [entry] });                 // the reload
$("toasts").byClass("link")[0].click();
await settle(); await settle();
done([toast, net.calls.map((c) => [c.method, c.path.split("?")[0]]), net.calls[2].body, state.editing, $("toasts").textContent]);
"""
    )
    assert run(script) == [
        "Removed shopUndo",
        [
            ["DELETE", "/api/projects/shop"],
            ["GET", "/api/projects"],
            ["POST", "/api/projects"],
            ["GET", "/api/projects"],
        ],
        {
            "config_version": "v2",
            "path": "/h/shop",
            "name": None,
            "patterns": ["*.csv"],
            "ignore": ["tmp/**"],
            "use_defaults": False,
        },
        "shop",
        "Restored shop",
    ]


def test_a_double_click_on_remove_sends_one_delete() -> None:
    script = """
const entry = { name: "shop", path: "/h/shop", patterns: ["*.csv"], ignore: [], use_defaults: false };
state.projects = [entry];
state.projectsVersion = "v1";
state.editing = "shop";
const first = editor.removeProject();
const second = editor.removeProject();
const deletes = net.calls.filter((c) => c.method === "DELETE").length;
net.reply(200, { config_version: "v2" });
net.reply(200, { config_version: "v2", projects: [] });
await first;
await second;
done([deletes, net.calls.map((c) => c.method), state.saving]);
"""
    assert run(script) == [1, ["DELETE", "GET"], False]


def test_undo_after_the_config_changed_shows_the_conflict() -> None:
    script = (
        REMOVE
        + """
$("e-conflict").hidden = true;
net.reply(409, { error: "config changed on disk" });
await editor.undoRemove();
done([$("e-conflict").hidden, net.calls.length, $("toasts").textContent, state.removed]);
"""
    )
    assert run(script) == [False, 3, "Not restored: config.yaml changed on disk", None]


# -- settings.js -------------------------------------------------------------------

SETTINGS = {
    "config_version": "v1",
    "debounce": {"quiet_seconds": 2, "max_wait_seconds": 30},
    "diff": {"max_lines": 20},
    "defaults": ["Procfile"],
    "ignore": [],
    "redact": [],
    "builtin_defaults": ["package.json", "*.lock"],
    "builtin_redact": [".env*"],
    "always_excluded": [".git"],
    "web": {"port": 8765, "editor": "vscode"},
}


def test_the_editor_setting_loads_and_saves() -> None:
    """Plan 7 spec §3.5: "Open files in" is saved through config_edit with the rest."""
    script = f"""
net.reply(200, {json.dumps(SETTINGS)});
await settings.loadSettings();
const loaded = $("s-editor").value;
$("s-editor").value = "none";
net.reply(200, {{ config_version: "v2" }});
await settings.saveSettings();
done([loaded, net.calls[1].method, net.calls[1].body.web]);
"""
    assert run(script) == ["vscode", "PUT", {"editor": "none"}]


def test_a_server_without_the_editor_shows_cursor() -> None:
    old = {**SETTINGS, "web": {"port": 8765}}
    script = f"""
net.reply(200, {json.dumps(old)});
await settings.loadSettings();
done($("s-editor").value);
"""
    assert run(script) == "cursor"


def test_load_settings_fills_the_rows_and_the_built_ins() -> None:
    script = f"""
net.reply(200, {json.dumps(SETTINGS)});
await settings.loadSettings();
done([$("s-quiet").value, $("s-maxwait").value, $("s-lines").value, $("s-defaults").value,
      $("e-mode-defaults-label").textContent, [...$("b-defaults").children].map((n) => n.textContent),
      [...$("e-builtin-list").children].map((n) => n.textContent)]);
"""
    assert run(script) == [
        "2",
        "30",
        "20",
        "Procfile",
        "Key files (2 built-in) + my additions",
        ["package.json", "*.lock"],
        ["package.json", "*.lock"],
    ]


def test_log_out_all_asks_inline_first() -> None:
    script = """
let ended = 0;
settings.initSettings({ loggedOut: () => { ended += 1; } });
$("s-logout-confirm").hidden = true;
settings.askLogout();
const asking = [$("s-logout").hidden, $("s-logout-confirm").hidden, doc.activeElement === $("s-logout-yes")];
settings.cancelLogout();
const cancelled = [$("s-logout").hidden, $("s-logout-confirm").hidden, net.calls.length];
settings.askLogout();
net.reply(200, { ok: true });
await settings.logoutAll();
done([asking, cancelled, net.calls[0].path, ended]);
"""
    assert run(script) == [[True, False, True], [False, True, 0], "/api/logout?all=true", 1]
