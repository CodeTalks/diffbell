"""Control and line-separator characters in a diff line are shown, never obeyed
(ADR 0017, "Display of control characters").

The parser splits diff output on "\\n" only, so a changed line may hold "\\r", other
C0 controls, DEL, C1 (U+0085 among them), U+2028/U+2029 and the bidi controls. A
Telegram client draws U+2028 as a line break, so "+ok<U+2028>-secret" would read
as a fake removed line; a bidi override reorders what is shown (Trojan Source).
Every such character is replaced by ONE visible character, so lengths and offsets
stay valid; a file whose every line ends in "\\r" (CRLF) shows them without it."""

from __future__ import annotations

import html as html_lib
from datetime import datetime

import pytest

from diffbell.diffing import FileReport
from diffbell.models import ChangeKind
from diffbell.render import INDENT, MessageContext, compose, render, tg_len, visible

CTX = MessageContext("myapp", "main", datetime(2026, 10, 8, 11, 42, 7), web_hint=False)
M = ChangeKind.MODIFIED
OTHER = "\u2370"  # U+2370: C1, the Unicode line separators and the bidi controls

# The mapping, written out independently of render.py: C0 (but "\t") to the Control
# Pictures, DEL to its picture, the rest to one marker.
EXPECTED: dict[int, str] = {c: chr(0x2400 + c) for c in range(0x20) if c != 0x09}
EXPECTED[0x7F] = "\u2421"
for _c in (
    *range(0x80, 0xA0),
    0x061C,
    0x200E,
    0x200F,
    0x2028,
    0x2029,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
):
    EXPECTED[_c] = OTHER
del _c

ALL = "".join(chr(c) for c in EXPECTED)


def test_visible_maps_every_listed_character_and_nothing_else() -> None:
    assert visible(ALL) == "".join(EXPECTED.values())
    assert visible("\r") == "\u240d" and visible("\x0b") == "\u240b"
    assert visible("\x1c") == "\u241c" and visible("\x7f") == "\u2421"
    others = "".join(chr(c) for c in range(0x10000) if c not in EXPECTED)
    assert visible(others) == others  # "\t", text, emoji halves, U+2370 itself


def test_visible_keeps_the_utf16_length() -> None:
    for text in (ALL, "a\r", "\u00e9\u2028x\U0001f600", ""):
        assert tg_len(visible(text)) == tg_len(text)
        assert len(visible(text)) == len(text)


def diff_lines(text: str) -> list[str]:
    """The message's indented diff lines, as Telegram would break them: on "\\n" and
    on the characters a client draws as a line break."""
    out = text
    for sep in ("\u2028", "\u2029", "\r", "\x0b", "\x0c", "\x85"):
        out = out.replace(sep, "\n")
    body = out.split("<code>")[-1].split("</code>")[0] if "<code>" in out else out
    return [line for line in body.split("\n") if line.startswith(INDENT)]


@pytest.mark.parametrize("as_html", [True, False])
def test_a_line_separator_in_a_changed_line_cannot_fake_a_removed_line(as_html: bool) -> None:
    reports = [FileReport("a.txt", M, lines=("-x", "+ok\u2028-secret = 1", "+y\u2029-z"))]
    text = render(CTX, reports, max_lines=5, html=as_html)
    assert "\u2028" not in text and "\u2029" not in text
    shown = diff_lines(text)
    assert shown[:3] == [f"{INDENT}-x", f"{INDENT}+ok{OTHER}-secret = 1", f"{INDENT}+y{OTHER}-z"]
    assert [line for line in shown if line.startswith(f"{INDENT}-")] == [f"{INDENT}-x"]


@pytest.mark.parametrize("as_html", [True, False])
def test_every_control_character_in_a_line_is_shown(as_html: bool) -> None:
    line = "+" + ALL.replace("\n", "") + "end"
    reports = [FileReport("a.txt", M, lines=("-a", line))]
    text = render(CTX, reports, max_lines=5, html=as_html)
    escape = (lambda s: html_lib.escape(s, quote=True)) if as_html else str
    assert escape(INDENT + visible(line)) in text
    assert not any(ch in text for ch in ALL.replace("\n", ""))


@pytest.mark.parametrize("as_html", [True, False])
def test_bidi_controls_in_a_line_are_visible(as_html: bool) -> None:
    line = "+access = 'user\u202e \u2066// admin\u2069 \u2066'"
    text = render(CTX, [FileReport("a.py", M, lines=(line,))], max_lines=5, html=as_html)
    assert not any(ch in text for ch in "\u202e\u2066\u2069")
    assert text.count(OTHER) == 4


@pytest.mark.parametrize("as_html", [True, False])
def test_a_crlf_to_lf_change_shows_the_carriage_return(as_html: bool) -> None:
    """Mixed endings in one report: "\u240d" shows where they differ."""
    text = render(CTX, [FileReport("a.txt", M, lines=("-a\r", "+a"))], max_lines=5, html=as_html)
    assert diff_lines(text) == [f"{INDENT}-a\u240d", f"{INDENT}+a"]


@pytest.mark.parametrize("as_html", [True, False])
def test_a_uniform_crlf_file_shows_no_carriage_returns(as_html: bool) -> None:
    """Every line ends in "\\r" (an edit in a Windows file): none is shown."""
    lines = ("-a = 1\r", "+a = 2\r", "+b\u2028c\r")
    text = render(CTX, [FileReport("a.ini", M, lines=lines)], max_lines=1, html=as_html)
    assert "\u240d" not in text and "\r" not in text
    assert diff_lines(text) == [f"{INDENT}-a = 1", f"{INDENT}\u2026 (2 more lines)"]
    full = render(CTX, [FileReport("a.ini", M, lines=lines)], max_lines=5, html=as_html)
    assert diff_lines(full) == [f"{INDENT}-a = 1", f"{INDENT}+a = 2", f"{INDENT}+b{OTHER}c"]


def test_only_the_trailing_carriage_return_of_a_uniform_file_goes() -> None:
    lines = ("-a\rb\r", "+a\r\r")
    text = render(CTX, [FileReport("a.txt", M, lines=lines)], max_lines=5, html=False)
    assert diff_lines(text) == [f"{INDENT}-a\u240db", f"{INDENT}+a\u240d"]


@pytest.mark.parametrize("as_html", [True, False])
def test_names_with_line_breaks_cannot_fake_a_message_line(as_html: bool) -> None:
    """A path or note holding "\\n" or U+2028 must not add a line to the message."""
    reports = [
        FileReport("a\n\U0001f5d1 b\u2028c\x85d\x7f.txt", M, lines=("+1",)),
        FileReport("e.bin", M, note="binary\r\u2029x"),
    ]
    text = render(CTX, reports, max_lines=5, html=as_html)
    assert "a\\u000A\U0001f5d1 b\\u2028c\\u0085d\\u007F.txt" in text
    assert "binary\\u000D\\u2029x" in text


def test_compose_fits_after_the_mapping() -> None:
    line = "+" + "\u2028\U0001f600\r" * 2000
    composed = compose(CTX, [FileReport("a.txt", M, lines=(line,))], max_lines=5, html=True)
    assert tg_len(composed.text) <= 4096
    assert "\u2028" not in composed.text
