"""The file header's quick actions (Plan 7 spec §2.5, §3.2) under node: shellQuote
equals Python's shlex.quote, the git log command, the editor link and its percent-
encoding, and the three actions on each file of the detail pane."""

from __future__ import annotations

import json
import shlex
from typing import Any

import pytest

from tests.web import test_js_dashboard as dash_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_dashboard import detail_of, report

pytestmark = needs_node

AWKWARD = [
    "",
    "plain",
    "a b",
    "it's",
    'say "hi"',
    "$HOME",
    "`id`",
    "$(id)",
    "a;b|c&d",
    "naïve.txt",
    "日本語/ファイル.md",
    "emoji 🎉",
    "line\nbreak",
    "tab\there",
    "back\\slash",
    "*.lock",
    "[.]env",
    "~/x",
    "!bang",
    "#hash",
    "-rf",
    "'",
    "''",
    "a'b'c",
]
SAFE_ONLY = [
    "abcXYZ019",
    "safe-chars_%+,./:=@",
    "src/app/main.py",
    "/Users/me/projects/x",
    "-",
    "@",
    "a=b",
]


def quoted(words: list[str]) -> list[str]:
    script = f"""
const {{ shellQuote }} = await import(js("shell.js"));
done({json.dumps(words)}.map(shellQuote));
"""
    return list(run_js(script))


def test_shell_quote_equals_shlex_quote_on_awkward_names() -> None:
    assert quoted(AWKWARD) == [shlex.quote(w) for w in AWKWARD]


def test_shell_quote_leaves_safe_only_strings_unquoted() -> None:
    """The part reimplementations get wrong: shlex.quote returns these as they are."""
    assert quoted(SAFE_ONLY) == SAFE_ONLY == [shlex.quote(w) for w in SAFE_ONLY]


def test_the_git_log_command_quotes_both_arguments_and_reads_paths_literally() -> None:
    folder, path = "/Users/me/my proj", "config/[prod]*.yaml"
    script = f"""
const {{ gitLogCommand }} = await import(js("shell.js"));
done(gitLogCommand({json.dumps(folder)}, {json.dumps(path)}));
"""
    command = run_js(script)
    assert command == (
        f"git -C {shlex.quote(folder)} --literal-pathspecs log -p --follow -- {shlex.quote(path)}"
    )
    assert "--literal-pathspecs" in command
    assert shlex.split(command)[-1] == path  # one word, exactly the file


@pytest.mark.parametrize(
    ("editor", "folder", "path", "line", "url"),
    [
        ("cursor", "/Users/me/app", "src/main.py", 12, "cursor://file/Users/me/app/src/main.py:12"),
        ("vscode", "/Users/me/app", "src/main.py", 12, "vscode://file/Users/me/app/src/main.py:12"),
        (
            "cursor",
            "/Users/me/app",
            "src/main.py",
            None,
            "cursor://file/Users/me/app/src/main.py:1",
        ),
        (
            "cursor",
            "/Users/me/my proj",
            "a b/c#1?.py",
            3,
            "cursor://file/Users/me/my%20proj/a%20b/c%231%3F.py:3",
        ),
        ("cursor", "/Users/me/app", "x:2.txt", 5, "cursor://file/Users/me/app/x%3A2.txt:5"),
        ("cursor", "/Users/me/app", "100%.md", 1, "cursor://file/Users/me/app/100%25.md:1"),
        (
            "vscode",
            "/Users/me/app",
            "日本.md",
            1,
            "vscode://file/Users/me/app/%E6%97%A5%E6%9C%AC.md:1",
        ),
        ("cursor", "/Users/me/app/", "a.txt", 0, "cursor://file/Users/me/app/a.txt:1"),
        ("cursor", "/Users/me/app", "a.txt", "3", "cursor://file/Users/me/app/a.txt:1"),
        # A name ending like a line (or line:column): the editors read it from the
        # decoded path whatever follows, so there is no safe link (no Open at all).
        ("cursor", "/Users/me/app", "build:12", None, None),
        ("cursor", "/Users/me/app", "src/build:12", 4, None),
        ("vscode", "/Users/me/app", "out/a:3:7", 2, None),
        # Only the last segment counts: a folder named like that is fine.
        ("cursor", "/Users/me/app", "v:2/a.txt", 2, "cursor://file/Users/me/app/v%3A2/a.txt:2"),
        ("cursor", "/Users/me/a#b%c", "x.txt", 2, "cursor://file/Users/me/a%23b%25c/x.txt:2"),
        (
            "cursor",
            "/Users/me/app",
            "line\nbreak.txt",
            1,
            "cursor://file/Users/me/app/line%0Abreak.txt:1",
        ),
        # A lone surrogate can't be percent-encoded (URIError): no link, no crash.
        ("cursor", "/Users/me/app", "bad\ud800.txt", 1, None),
        ("none", "/Users/me/app", "a.txt", 3, None),
        ("cursor", None, "a.txt", 3, None),
        ("emacs", "/Users/me/app", "a.txt", 3, None),
    ],
)
def test_the_open_link(
    editor: str, folder: str | None, path: str, line: Any, url: str | None
) -> None:
    script = f"""
const detail = await import(js("detail.js"));
done(detail.editorUrl({json.dumps(editor)}, {json.dumps(folder)}, {json.dumps(path)}, {json.dumps(line)}));
"""
    assert run_js(script) == url


# -- the actions in the detail pane ---------------------------------------------------------

DASH = {
    "projects": [{"name": "proj", "status": "subscribed", "folder": "/Users/me/my proj"}],
    "missing": [],
    "problems": [],
    "web": {"running": True, "port": 8765, "last_error": None, "editor": "cursor"},
}
LINES = ["-old", "+new", "+ <b>x</b>"]
ACTIONS = f"""
const copied = [];
dashboard.initDashboard({{ copy: async (text) => {{ copied.push(text); }} }});
state.dash = {json.dumps(DASH)};
const pane = $("detail");
const show = (body) => {{ state.detail = body; detail.renderDetail(); }};
const bars = () => pane.byClass("file-bar");
const actions = (bar) => bar.byClass("file-action").map((a) => [
  a.tagName, a.textContent, a.getAttribute("href"), a.disabled, a.getAttribute("title"), a.getAttribute("aria-label"),
]);
"""


def run(script: str) -> Any:
    return dash_js.run(ACTIONS + script)


def body(*reports: dict[str, Any]) -> str:
    return json.dumps(detail_of(list(reports)))


def test_each_file_has_open_copy_diff_and_git_log() -> None:
    script = f"""
show({body(report(0, path="src/a b.py", lines=LINES, first_line=40))});
done(bars().map(actions));
"""
    command = "git -C '/Users/me/my proj' --literal-pathspecs log -p --follow -- 'src/a b.py'"
    assert run(script) == [
        [
            [
                "A",
                "Open ↗",
                "cursor://file/Users/me/my%20proj/src/a%20b.py:40",
                False,
                "Open in Cursor at line 40",
                "Open src/a b.py in Cursor",
            ],
            ["BUTTON", "Copy diff", None, False, None, "Copy the diff of src/a b.py"],
            ["BUTTON", "git log", None, False, command, "Copy a git log command for src/a b.py"],
        ]
    ]


def test_copy_diff_copies_the_stored_lines_exactly() -> None:
    script = f"""
show({body(report(0, lines=LINES))});
bars()[0].byClass("copy-diff")[0].click();
await settle();
done([copied, texts($("toasts"), "toast-text")]);
"""
    assert run(script) == [["-old\n+new\n+ <b>x</b>"], ["Diff copied"]]


def test_the_git_log_button_copies_the_command() -> None:
    script = f"""
show({body(report(1, path="it's.json"))});
bars()[0].byClass("git-log")[0].click();
await settle();
done([copied, texts($("toasts"), "toast-text")]);
"""
    assert run(script) == [
        ["git -C '/Users/me/my proj' --literal-pathspecs log -p --follow -- 'it'\"'\"'s.json'"],
        ["git log command copied"],
    ]


def test_a_failed_copy_says_so() -> None:
    script = f"""
dashboard.initDashboard({{ copy: async () => {{ throw new Error("denied"); }} }});
show({body(report(2))});
bars()[0].byClass("copy-diff")[0].click();
await settle();
done(texts($("toasts"), "toast-text"));
"""
    assert run(script) == ["Copy failed: denied"]


def test_pressing_an_action_never_toggles_the_section() -> None:
    script = f"""
show({body(report(2))});
const head = () => pane.byClass("file-head")[0].getAttribute("aria-expanded");
const before = head();
bars()[0].byClass("copy-diff")[0].click();
bars()[0].byClass("git-log")[0].click();
bars()[0].byClass("open-file")[0].click();
await settle();
done([before, head(), bars()[0].children[0].className]);
"""
    assert run(script) == ["true", "true", "file-head"]


def test_redacted_and_truncated_files_cannot_copy_their_diff() -> None:
    script = f"""
show({
        body(
            report(0, path=".env.local", redacted=True, note=None),
            report(0, path="big.json", truncated=True, note="diff omitted (size limit)"),
            report(0, path="logo.png", note="binary file changed"),
        )
    });
done(bars().map((b) => [
  b.byClass("open-file").map((a) => a.getAttribute("href")),
  b.byClass("copy-diff")[0].disabled,
  b.byClass("git-log").length,
]));
"""
    assert run(script) == [
        [["cursor://file/Users/me/my%20proj/.env.local:1"], True, 1],  # Open: the user's own file
        [["cursor://file/Users/me/my%20proj/big.json:1"], True, 1],
        [["cursor://file/Users/me/my%20proj/logo.png:1"], True, 1],
    ]


def test_a_disabled_copy_diff_says_why() -> None:
    script = f"""
show({
        body(
            report(0, path=".env.local", redacted=True, note=None),
            report(0, path="big.json", truncated=True, note="diff omitted (size limit)"),
            report(0, path="logo.png", note="binary file changed"),
            report(0, path="ok.txt", lines=LINES),
        )
    });
done(bars().map((b) => b.byClass("copy-diff")[0].getAttribute("title")));
"""
    assert run(script) == [
        "Nothing to copy: contents redacted",
        "Nothing to copy: diff omitted (size limit)",
        "Nothing to copy: no lines stored",
        None,
    ]


def test_a_name_that_cannot_be_encoded_keeps_the_pane() -> None:
    script = f"""
show({body(report(1, path="bad\ud800.txt"), report(1, path="build:12"))});
done(bars().map((b) => b.byClass("file-action").map((a) => a.textContent)));
"""
    assert run(script) == [["Copy diff", "git log"], ["Copy diff", "git log"]]


def test_a_deleted_file_has_no_open_link() -> None:
    """Nothing to open: the file is gone from the folder. Its diff and history stay."""
    script = f"""
show({body(report(0, path="old.txt", kind="deleted", lines=["-gone"], first_line=1))});
done(bars().map((b) => b.byClass("file-action").map((a) => a.textContent)));
"""
    assert run(script) == [["Copy diff", "git log"]]


def test_editor_none_hides_open_and_vscode_links_there() -> None:
    script = f"""
show({body(report(1, first_line=3))});
const cursor = bars()[0].byClass("open-file").map((a) => a.getAttribute("href"));
state.dash.web.editor = "none";
detail.renderDetail();
const none = bars()[0].byClass("open-file").length;
state.dash.web.editor = "vscode";
detail.renderDetail();
const vscode = bars()[0].byClass("open-file").map((a) => [a.getAttribute("href"), a.getAttribute("title")]);
done([cursor, none, vscode, bars()[0].byClass("copy-diff").length]);
"""
    assert run(script) == [
        ["cursor://file/Users/me/my%20proj/package.json:3"],
        0,
        [["vscode://file/Users/me/my%20proj/package.json:3", "Open in VS Code at line 3"]],
        1,
    ]


def test_an_unknown_folder_has_no_open_and_no_git_log() -> None:
    """An event of a project that is no longer configured (or not resolved)."""
    script = f"""
state.dash.projects = [];
show({body(report(1))});
done(bars().map((b) => b.byClass("file-action").map((a) => a.textContent)));
"""
    assert run(script) == [["Copy diff"]]


def test_an_inactive_projects_event_has_no_folder_actions_for_a_same_named_one() -> None:
    """The event's project is no longer configured (project_active false) while a
    configured project, another folder, has its name: no Open and no git log."""
    inactive = json.dumps({**detail_of([report(1)]), "project_active": False})
    script = f"""
show({inactive});
done(bars().map((b) => b.byClass("file-action").map((a) => a.textContent)));
"""
    assert run(script) == [["Copy diff"]]


def test_without_first_line_open_goes_to_line_1() -> None:
    script = f"""
show({body(report(1), report(1, path="b.txt", first_line=0), report(1, path="c.txt", first_line="7"))});
done(bars().map((b) => [b.byClass("open-file")[0].getAttribute("href"), b.byClass("open-file")[0].getAttribute("title")]));
"""
    assert run(script) == [
        ["cursor://file/Users/me/my%20proj/package.json:1", "Open in Cursor"],
        ["cursor://file/Users/me/my%20proj/b.txt:1", "Open in Cursor"],
        ["cursor://file/Users/me/my%20proj/c.txt:1", "Open in Cursor"],
    ]


def test_a_settings_change_reaches_the_open_detail_on_the_next_poll() -> None:
    event = dash_js.ev(7, status="sent")
    script = f"""
state.events = [{json.dumps(event)}];
net.reply(200, {body(report(1, first_line=2))});
await detail.syncDetail();
const before = pane.byClass("open-file").length;
state.dash = {{ ...state.dash, web: {{ ...state.dash.web, editor: "none" }} }};
await detail.syncDetail();  // the poll: same event, no refetch
done([before, pane.byClass("open-file").length, net.calls.length]);
"""
    assert run(script) == [1, 0, 1]
