"""Control and line-separator characters on the page (ADR 0017, "Display of control
characters"): diffview.js shows them as one visible character each, AFTER the word
changes and search marks are worked out on the raw line, so their offsets hold; a
file whose every line ends in "\\r" shows no carriage returns. The mapping is
render.py's, code point for code point."""

from __future__ import annotations

import json
from typing import Any

from diffbell import render
from tests.web.node import needs_node, run_js

pytestmark = needs_node

SHOW = """
const dv = await import(js("diffview.js"));
const show = (segs) => segs.map((s) => (s.classes.length ? `[${s.classes.join(",")}:${s.text}]` : s.text)).join("");
const render = (lines, path, q = "") => dv.diffSegments(lines, path, q).map(show);
"""
OTHER = "\u2370"
CR = "\u240d"


def run(script: str) -> Any:
    return run_js(SHOW + script)


def shown(lines: list[str], path: str = "notes.txt", q: str = "") -> list[str]:
    return list(run(f"done(render({json.dumps(lines)}, {json.dumps(path)}, {json.dumps(q)}));"))


def test_the_js_mapping_equals_the_python_one_on_every_bmp_code_point() -> None:
    script = """
const out = [];
for (let c = 0; c <= 0xffff; c += 1) {
  const v = dv.visible(String.fromCharCode(c));
  if (v !== String.fromCharCode(c)) out.push([c, v.length, v.charCodeAt(0)]);
}
done(out);
"""
    js_map = {c: (n, v) for c, n, v in run(script)}
    py_map = {
        c: (1, ord(render.visible(chr(c))))
        for c in range(0x10000)
        if render.visible(chr(c)) != chr(c)
    }
    assert js_map == py_map
    assert len(py_map) == 31 + 1 + 32 + 1 + 2 + 2 + 5 + 4


def test_a_line_separator_stays_inside_its_line_and_is_visible() -> None:
    assert shown(["+ok\u2028-secret = 1"]) == [f"+ok{OTHER}-secret = 1"]


def test_search_marks_keep_their_offsets() -> None:
    # The mark is found on the raw line; the marker before it is one unit wide.
    assert shown(["+a\u2028bc\u202edx"], q="bc") == [f"+a{OTHER}[mark:bc]{OTHER}dx"]
    assert shown(["+\x00\x1fab"], q="ab") == ["+\u2400\u241f[mark:ab]"]


def test_word_changes_keep_their_offsets() -> None:
    assert shown(["-k = \x0b1", "+k = \x0b2"]) == ["-k = \u240b[wdel:1]", "+k = \u240b[wadd:2]"]


def test_a_crlf_to_lf_change_shows_the_carriage_return() -> None:
    assert shown(["-a\r", "+a"]) == [f"-a[wdel:{CR}]", "+a"]


def test_a_uniform_crlf_file_shows_no_carriage_returns() -> None:
    assert shown(['-  "a": 1\r', '+  "a": 2\r'], "x.json") == [
        '-  [sk:"a"]: [sn,wdel:1]',
        '+  [sk:"a"]: [sn,wadd:2]',
    ]
    # Only the trailing one goes.
    assert shown(["-ab\rbc\r", "+ab\r\r"], q="bc") == [f"-ab{CR}[mark,wdel:bc]", f"+ab{CR}"]
    assert shown(["-\r", "+\r"]) == ["-", "+"]


def test_bidi_controls_and_c1_are_visible() -> None:
    line = "+x\u202e\u2066\u2069\u200e\u200f\u061c\x85\x9f\x7fy"
    assert shown([line]) == [f"+x{OTHER * 8}\u2421y"]


def test_every_segment_is_mapped_and_the_text_joins_to_the_mapped_line() -> None:
    lines = ["-a\u2028b\r", "+a\u2029c", "+\x1b[31m"]
    script = f"""
const segs = dv.diffSegments({json.dumps(lines)}, "a.txt", "a");
done(segs.map((s) => s.map((p) => p.text).join("")));
"""
    assert run(script) == [render.visible(line) for line in lines]


def test_uniform_crlf_is_decided_over_every_line_of_the_report() -> None:
    script = """
done([dv.uniformCrlf(["-a\\r", "+b\\r"]), dv.uniformCrlf(["-a\\r", "+b"]), dv.uniformCrlf([])]);
"""
    assert run(script) == [True, False, False]
    # Uniform: no "\u240d" anywhere, segments, word diff and marks intact.
    out = shown(["-k = 1 ab\r", "+k = 2 ab\r", " ctx\r"], q="ab")
    assert out == ["-k = [wdel:1] [mark:ab]", "+k = [wadd:2] [mark:ab]", " ctx"]
    assert CR not in "".join(out)
    # Mixed: every "\r" stays visible.
    mixed = shown(["-k = 1\r", "+k = 2\r", " ctx"])
    assert mixed == [f"-k = [wdel:1]{CR}", f"+k = [wadd:2]{CR}", " ctx"]


def test_the_name_mapping_equals_the_python_one_on_every_bmp_code_point() -> None:
    script = """
const out = [];
for (let c = 0; c <= 0xffff; c += 1) {
  const s = String.fromCharCode(c);
  const v = dv.visibleName(s);
  if (v !== s) out.push([c, v]);
}
done(out);
"""
    js_map = {c: v for c, v in run(script)}
    py_map = {
        c: render._visible(chr(c)) for c in range(0x10000) if render._visible(chr(c)) != chr(c)
    }
    assert js_map == py_map
    assert len(py_map) == 31 + 1 + 32 + 1 + 2 + 2 + 5 + 4
    assert js_map[0x0A] == "\\u000A" and 0x09 not in js_map


def test_the_page_shows_names_with_the_same_escapes_as_telegram() -> None:
    name = "a\n\u202eb\x85\tc"
    script = f"""
const dom = await import(js("dom.js"));
done(dom.shown({json.dumps(name)}));
"""
    assert run_js(script) == render._visible(name)
