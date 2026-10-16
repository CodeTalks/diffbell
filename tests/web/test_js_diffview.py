"""diffview.js under node: run pairing, word changes, light syntax colour, search marks
and their merge into segments (diff readability spec §3, §7 "diffview.js")."""

from __future__ import annotations

import json
from typing import Any

from tests.web import test_js_dashboard as dash_js
from tests.web.node import needs_node, run_js
from tests.web.test_js_dashboard import detail_of, report

pytestmark = needs_node

SHOW = """
const dv = await import(js("diffview.js"));
// A line's segments as one string: plain text as is, a classed piece as [classes:text].
const show = (segs) => segs.map((s) => (s.classes.length ? `[${s.classes.join(",")}:${s.text}]` : s.text)).join("");
const render = (lines, path, q = "") => dv.diffSegments(lines, path, q).map(show);
"""


def run(script: str) -> Any:
    return run_js(SHOW + script)


def shown(lines: list[str], path: str, q: str = "") -> list[str]:
    return list(run(f"done(render({json.dumps(lines)}, {json.dumps(path)}, {json.dumps(q)}));"))


# -- pairing -----------------------------------------------------------------------------


def test_runs_pair_by_position_and_unequal_runs_leave_lines_unpaired() -> None:
    lines = ["-a", "-b", "+c", "-d", "+e", "+f", "+g", "@@ x", "-h", "@@ y", "+i"]
    # -a/+c and -d/+e pair; -b, +f, +g are left over; "@@" ends a run (-h, +i).
    assert run(f"done(dv.pairRuns({json.dumps(lines)}));") == [
        2,
        -1,
        0,
        4,
        3,
        -1,
        -1,
        -1,
        -1,
        -1,
        -1,
    ]


def test_an_added_line_before_any_removed_one_is_unpaired() -> None:
    assert run('done(dv.pairRuns(["+a", "-b", "+c"]));') == [-1, 2, 1]


# -- word changes ---------------------------------------------------------------------


def test_a_version_bump_marks_only_the_changed_digit() -> None:
    lines = ['-  "version": "1.0.3",', '+  "version": "1.0.4",']
    assert shown(lines, "package.json") == [
        '-  [sk:"version"]: [ss:"1.0.][ss,wdel:3][ss:"],',
        '+  [sk:"version"]: [ss:"1.0.][ss,wadd:4][ss:"],',
    ]


def test_an_image_tag_change_marks_only_the_tag() -> None:
    lines = ["-    image: postgres:15", "+    image: postgres:16"]
    assert shown(lines, "notes.txt") == [
        "-    image: postgres:[wdel:15]",
        "+    image: postgres:[wadd:16]",
    ]


def test_tokens_are_words_single_spaces_and_single_other_characters() -> None:
    assert run('done(dv.tokens("ab_1  x.y(é)"));') == [
        "ab_1",
        " ",
        " ",
        "x",
        ".",
        "y",
        "(",
        "é",
        ")",
    ]


def test_a_line_over_400_characters_keeps_whole_line_colour_only() -> None:
    long_old = "-" + "a " * 199 + "b"  # 400 characters: still compared
    long_new = "+" + "a " * 199 + "c"
    assert shown([long_old, long_new], "x.txt")[0].endswith("[wdel:b]")
    longer_old, longer_new = long_old + "z", long_new + "z"  # 401: skipped
    assert shown([longer_old, longer_new], "x.txt") == [longer_old, longer_new]


def test_under_40_percent_shared_tokens_keeps_whole_line_colour_only() -> None:
    # 3 and 5 tokens, 1 shared (the space): under 40% of 5.
    assert shown(["-alpha beta", "+gamma delta epsilon"], "x.txt") == [
        "-alpha beta",
        "+gamma delta epsilon",
    ]
    # 2 of 5 shared is exactly 40%: compared.
    assert shown(["-aa bb cc", "+xx bb yy"], "x.txt") == [
        "-[wdel:aa] bb [wdel:cc]",
        "+[wadd:xx] bb [wadd:yy]",
    ]


# -- light syntax colour ---------------------------------------------------------------


def test_json_keys_strings_numbers_and_literals() -> None:
    lines = ['+  "name": "a: b", "n": -1.5e3, "ok": true, "x": null, "id": "v2"']
    assert shown(lines, "Config.JSON") == [
        '+  [sk:"name"]: [ss:"a: b"], [sk:"n"]: [sn:-1.5e3], [sk:"ok"]: [sn:true], '
        '[sk:"x"]: [sn:null], [sk:"id"]: [ss:"v2"]'
    ]


def test_a_json_string_cut_across_lines_is_left_plain() -> None:
    assert shown(['+  "a": "cut across', '+  the line"'], "x.json") == [
        '+  [sk:"a"]: "cut across',
        '+  the line"',
    ]


def test_yaml_keys_list_markers_and_comments_not_inside_quotes() -> None:
    lines = [
        '+  - name: "a # b" # real comment',
        "+key: value",
        "+# only a comment",
        "+url: http://x/#frag",
        "+'quoted key': 1",
    ]
    assert shown(lines, "ci.YML") == [
        '+  [sp:-] [sk:name]: "a # b" [sc:# real comment]',
        "+[sk:key]: value",
        "+[sc:# only a comment]",
        "+[sk:url]: http://x/#frag",
        "+[sk:'quoted key']: 1",
    ]


def test_toml_sections_keys_strings_numbers_and_comments() -> None:
    lines = [
        "+[tool.ruff]",
        "+[[bin]]",
        '+line-length = 100 # "max"',
        '+name = "a # b"',
        "+enabled = false",
        '+text = """',
    ]
    assert shown(lines, "pyproject.toml") == [
        "+[sk:[tool.ruff]]",
        "+[sk:[[bin]]]",
        '+[sk:line-length] = [sn:100] [sc:# "max"]',
        '+[sk:name] = [ss:"a # b"]',
        "+[sk:enabled] = [sn:false]",
        '+[sk:text] = [ss:""]"',  # a multi-line string: only the closed pair is coloured
    ]


def test_other_extensions_are_plain() -> None:
    assert shown(['+  "a": 1'], "x.js") == ['+  "a": 1']
    assert run(
        'done(["a.json", "b.YAML", "c.yml", "d.Toml", "e.txt", "json"].map(dv.syntaxOf));'
    ) == [
        "json",
        "yaml",
        "yaml",
        "toml",
        "",
        "",
    ]


# -- marks and merging ---------------------------------------------------------------


def test_search_marks_are_ascii_case_insensitive_on_the_whole_line() -> None:
    lines = ["-    image: postgres:15", "+    image: postgres:16"]
    assert shown(lines, "x.txt", "POSTGRES") == [
        "-    image: [mark:postgres]:[wdel:15]",
        "+    image: [mark:postgres]:[wadd:16]",
    ]
    # The marker is part of the searched line, as on the server.
    assert shown(["+a+b"], "x.txt", "+a") == ["[mark:+a]+b"]
    # Non-ASCII letters match their own case only.
    assert shown(["+Ärger ärger"], "x.txt", "ärger") == ["+Ärger [mark:ärger]"]


def test_one_character_marks_nothing() -> None:
    assert shown(["+aaa"], "x.txt", " a ") == ["+aaa"]
    assert run('done([dv.searchTerm("ab"), dv.searchTerm(" a "), dv.searchTerm("😀")]);') == [
        "ab",
        "",
        "",  # one code point, two UTF-16 units: still one character
    ]


def test_marks_word_changes_and_syntax_merge_into_one_segment_list() -> None:
    lines = ['-  "image": "postgres:15"', '+  "image": "postgres:16"']
    assert shown(lines, "x.json", "res:1") == [
        '-  [sk:"image"]: [ss:"postg][mark,ss:res:][mark,ss,wdel:1][ss,wdel:5][ss:"]',
        '+  [sk:"image"]: [ss:"postg][mark,ss:res:][mark,ss,wadd:1][ss,wadd:6][ss:"]',
    ]


def test_match_counts() -> None:
    # ASCII case-insensitive, non-overlapping: 1 + 2 + 0.
    assert run('done(dv.countMatches(["+aXa", "-xx", "+none"], "x"));') == 3


def test_match_counts_survive_lines_that_are_not_strings() -> None:
    # Like diffSegments: null/undefined count as "", anything else as its text.
    script = 'done([dv.countMatches([null, 42, "+x42", undefined, {}], "42"), dv.markRanges(null, "ab")]);'
    assert run(script) == [2, []]


def test_segments_always_rebuild_the_displayed_line_exactly() -> None:
    lines = [
        "+<script>alert(1)</script>",
        "-",
        "+",
        "",
        "+tab\there\r",
        "@@ -1 +1 @@",
        "-" + "é" * 500,
        "+" + "😀" * 300,
    ]
    script = f"""
const lines = {json.dumps(lines)};
const segs = dv.diffSegments(lines, "x.json", "sc");
done(segs.map((s, i) => s.map((p) => p.text).join("") === dv.visible(lines[i])));
"""
    # The displayed line: "\r" is shown as U+240D (test_js_visible.py), the rest exact.
    assert run(script) == [True] * len(lines)


def test_odd_input_never_throws() -> None:
    script = """
const out = [
  dv.diffSegments([null, undefined, 7, "+ok"], "x.json", "ok").map((s) => s.map((p) => p.text).join("")),
  dv.diffSegments([], "x.toml", ""),
  dv.diffSegments(["+a"], null, null).map(show),
];
done(out);
"""
    assert run(script) == [["", "", "7", "+ok"], [], ["+a"]]


def test_lines_that_are_not_an_array_give_no_segments() -> None:
    """A truthy non-array (a string, an object) must not throw: no lines, no segments."""
    script = """
done([dv.diffSegments("+a\\n-b", "x.json", "ab"), dv.diffSegments({ 0: "+a", length: 1 }, "x.yml", "")]);
"""
    assert run(script) == [[], []]


def test_the_detail_builds_each_line_from_its_segments() -> None:
    body = detail_of([report(0, lines=['-  "v": "1.0.3"', '+  "v": "1.0.4"'])])
    body["telegram_lines"] = 20
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
done($("detail").byClass("ln").map((ln) => [ln.className, ln.textContent,
  ln.elements().map((e) => [e.tagName, e.className, e.textContent])]));
"""
    assert dash_js.run(script) == [
        [
            "ln del",
            '-  "v": "1.0.3"',
            [
                ["SPAN", "sk", '"v"'],
                ["SPAN", "ss", '"1.0.'],
                ["SPAN", "ss wdel", "3"],
                ["SPAN", "ss", '"'],
            ],
        ],
        [
            "ln add",
            '+  "v": "1.0.4"',
            [
                ["SPAN", "sk", '"v"'],
                ["SPAN", "ss", '"1.0.'],
                ["SPAN", "ss wadd", "4"],
                ["SPAN", "ss", '"'],
            ],
        ],
    ]


def test_pairs_span_the_telegram_divider() -> None:
    """Pairing runs over all stored lines: a pair cut by the divider still shows its
    word change once "Show all" opens the rest."""
    body = detail_of([report(0, path="notes.txt", lines=["-a 1", "+a 2"])])
    body["telegram_lines"] = 1
    script = f"""
state.detail = {json.dumps(body)};
detail.renderDetail();
$("detail").byClass("link").find((b) => b.textContent.startsWith("Show all")).click();
done($("detail").byClass("ln").map((ln) => ln.elements().map((e) => [e.className, e.textContent])));
"""
    assert dash_js.run(script) == [[["wdel", "1"]], [["wadd", "2"]]]
