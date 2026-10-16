"""The Telegram message, layout A2 (health spec §3, §8 "Render").

The SHA-256 pins of the old layout are gone on purpose: the message changed. Each case
is now pinned as a readable golden text (GOLDEN below), HTML and plain."""

from __future__ import annotations

import dataclasses
import html as html_lib
import re
from datetime import datetime

import pytest

from diffbell.diffing import FileReport
from diffbell.models import ChangeKind
from diffbell.render import (
    ENTITY_LIMIT,
    SAMPLE_REPORTS,
    CatchUp,
    Composed,
    MessageContext,
    capped,
    compose,
    entity_count,
    line_counts,
    link_line,
    message_order,
    render,
    sample_message,
    tg_len,
)

CTX = MessageContext("myapp", "main", datetime(2026, 10, 8, 11, 42, 7), web_hint=False)
LINKED = dataclasses.replace(CTX, event_id=42, web_port=8765)
M, C, D = ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED
HELD = CatchUp(datetime(2026, 10, 9, 14, 5), datetime(2026, 10, 9, 15, 40), "snooze", 3)

LAYOUT = [
    FileReport("package.json", M, lines=('-  "a": 1', '+  "a": 2'), note="vs HEAD"),
    FileReport("migrations/x.sql", C, lines=("+CREATE INDEX i;",), note="new file"),
    FileReport(".env.local", M, redacted=True),
    FileReport("Dockerfile.old", D),
]
SINGLE = [
    FileReport(
        "package.json",
        M,
        lines=('-  "version": "1.0.0"', '+  "version": "1.1.0"', '+  "private": true'),
        note="vs HEAD",
    )
]
NO_LINES = [
    FileReport("logo.png", M, note="binary file changed"),
    FileReport("old.lock", D),
    FileReport("same.json", M, note="matches HEAD"),
]

# (context, reports) per golden case.
CASES: dict[str, tuple[MessageContext, list[FileReport]]] = {
    "single": (LINKED, SINGLE),
    "multiple": (LINKED, LAYOUT),
    "redacted": (LINKED, [FileReport(".env.production", C, redacted=True)]),
    "catch_up": (
        dataclasses.replace(LINKED, branch=None, catch_up=HELD, more=2),
        [FileReport("a.json", M, lines=("+1",)), FileReport("b.json", D)],
    ),
    "no_branch": (
        dataclasses.replace(LINKED, branch=None),
        [FileReport("Makefile", C, lines=("+all:",), note="new file")],
    ),
    "no_link": (CTX, LAYOUT),
    "no_lines": (LINKED, NO_LINES),
}


def lines(*parts: str) -> str:
    return "\n".join(parts)


LINK_HTML = '🔗 <a href="http://127.0.0.1:8765/?event=42">Open #42</a>'
LINK_PLAIN = "🔗 Open #42: http://127.0.0.1:8765/?event=42"
LAYOUT_HTML = (
    "<b>🔑 myapp</b> · <code>main</code> · 11:42",
    "4 files · +2 −1",
    "✏️ <code>package.json</code> +1 −1",
    "➕ <code>migrations/x.sql</code> +1 −0",
    "🗑 <code>Dockerfile.old</code>",
    "🔒 <code>.env.local</code> · contents hidden",
    "<blockquote expandable>package.json",
    "<code>   -  &quot;a&quot;: 1",
    "   +  &quot;a&quot;: 2</code>",
    "migrations/x.sql",
    "<code>   +CREATE INDEX i;</code></blockquote>",
)
LAYOUT_PLAIN = (
    "🔑 myapp · main · 11:42",
    "4 files · +2 −1",
    "✏️ package.json +1 −1",
    '   -  "a": 1',
    '   +  "a": 2',
    "➕ migrations/x.sql +1 −0",
    "   +CREATE INDEX i;",
    "🗑 Dockerfile.old",
    "🔒 .env.local · contents hidden",
)

# The whole message per case, HTML (True) and plain text (False).
GOLDEN: dict[tuple[str, bool], str] = {
    ("single", True): lines(
        "<b>🔑 myapp</b> · <code>main</code> · 11:42",
        "1 file · +2 −1",
        "✏️ <code>package.json</code> +2 −1",
        "<blockquote expandable>package.json",
        "<code>   -  &quot;version&quot;: &quot;1.0.0&quot;",
        "   +  &quot;version&quot;: &quot;1.1.0&quot;",
        "   +  &quot;private&quot;: true</code></blockquote>",
        LINK_HTML,
    ),
    ("single", False): lines(
        "🔑 myapp · main · 11:42",
        "1 file · +2 −1",
        "✏️ package.json +2 −1",
        '   -  "version": "1.0.0"',
        '   +  "version": "1.1.0"',
        '   +  "private": true',
        LINK_PLAIN,
    ),
    ("multiple", True): lines(*LAYOUT_HTML, LINK_HTML),
    ("multiple", False): lines(*LAYOUT_PLAIN, LINK_PLAIN),
    ("redacted", True): lines(
        "<b>🔑 myapp</b> · <code>main</code> · 11:42",
        "1 file",
        "🔒 <code>.env.production</code> · contents hidden",
        LINK_HTML,
    ),
    ("redacted", False): lines(
        "🔑 myapp · main · 11:42",
        "1 file",
        "🔒 .env.production · contents hidden",
        LINK_PLAIN,
    ),
    ("catch_up", True): lines(
        "<b>🔕 Catch-up · myapp</b> · held 14:05–15:40 (snoozed)",
        "3 changes in 2 files · +1 −0",
        "✏️ <code>a.json</code> +1 −0",
        "🗑 <code>b.json</code>",
        "<blockquote expandable>a.json",
        "<code>   +1</code></blockquote>",
        '🔗 <a href="http://127.0.0.1:8765/?event=42">Open #42 (+2 more)</a>',
    ),
    ("catch_up", False): lines(
        "🔕 Catch-up · myapp · held 14:05–15:40 (snoozed)",
        "3 changes in 2 files · +1 −0",
        "✏️ a.json +1 −0",
        "   +1",
        "🗑 b.json",
        "🔗 Open #42 (+2 more): http://127.0.0.1:8765/?event=42",
    ),
    ("no_branch", True): lines(
        "<b>🔑 myapp</b> · 11:42",
        "1 file · +1 −0",
        "➕ <code>Makefile</code> +1 −0",
        "<blockquote expandable>Makefile",
        "<code>   +all:</code></blockquote>",
        LINK_HTML,
    ),
    ("no_branch", False): lines(
        "🔑 myapp · 11:42",
        "1 file · +1 −0",
        "➕ Makefile +1 −0",
        "   +all:",
        LINK_PLAIN,
    ),
    ("no_link", True): lines(*LAYOUT_HTML),
    ("no_link", False): lines(*LAYOUT_PLAIN),
    ("no_lines", True): lines(
        "<b>🔑 myapp</b> · <code>main</code> · 11:42",
        "3 files",
        "✏️ <code>logo.png</code> · binary file changed",
        "✏️ <code>same.json</code> · matches HEAD",
        "🗑 <code>old.lock</code>",
        LINK_HTML,
    ),
    ("no_lines", False): lines(
        "🔑 myapp · main · 11:42",
        "3 files",
        "✏️ logo.png · binary file changed",
        "✏️ same.json · matches HEAD",
        "🗑 old.lock",
        LINK_PLAIN,
    ),
}


@pytest.mark.parametrize(("case", "as_html"), sorted(GOLDEN))
def test_golden_text(case: str, as_html: bool) -> None:
    ctx, reports = CASES[case]
    assert compose(ctx, reports, max_lines=20, html=as_html) == Composed(
        GOLDEN[(case, as_html)], 20, len(reports)
    )


def test_the_sample_message() -> None:
    assert sample_message(datetime(2026, 10, 8, 12, 0, 0)) == lines(
        "<b>🔑 Diffbell (sample)</b> · <code>main</code> · 12:00",
        "3 files · +2 −1",
        "✏️ <code>package.json</code> +1 −1",
        "➕ <code>migrations/0002_index.sql</code> +1 −0",
        "🔒 <code>.env.local</code> · contents hidden",
        "<blockquote expandable>package.json",
        "<code>   -  &quot;version&quot;: &quot;0.1.0&quot;",
        "   +  &quot;version&quot;: &quot;0.1.1&quot;</code>",
        "migrations/0002_index.sql",
        "<code>   +CREATE INDEX events_project ON events(project);</code></blockquote>",
    )


def test_the_sample_message_never_has_a_link() -> None:
    assert "🔗" not in sample_message(datetime(2026, 10, 8, 12, 0, 0))


def test_the_sample_uses_the_content_snapshot_notes() -> None:
    assert [r.note for r in SAMPLE_REPORTS] == ["uncommitted", "untracked", None]


# -- line rules ----------------------------------------------------------------------


def test_files_are_ordered_modified_created_deleted_then_redacted() -> None:
    reports = [
        FileReport("d1", D),
        FileReport("r1", C, redacted=True),
        FileReport("c1", C),
        FileReport("m1", M),
        FileReport("d2", D),
        FileReport("r2", M, redacted=True),
        FileReport("m2", M),
    ]
    assert [r.path for r in message_order(reports)] == ["m1", "m2", "c1", "d1", "d2", "r1", "r2"]


def test_per_file_counts() -> None:
    assert line_counts(FileReport("a", M, lines=("+x", "+y", "-z"))) == (2, 1)
    assert line_counts(FileReport("a", C, lines=("+x",))) == (1, 0)
    assert line_counts(FileReport("a", M)) is None  # no lines
    assert line_counts(FileReport("a", D, lines=("-x",))) is None  # deleted
    assert line_counts(FileReport("a", M, redacted=True, lines=("+x",))) is None


def test_a_file_at_the_collection_cap_counts_at_least() -> None:
    """diffing keeps at most MAX_COLLECT_LINES (200) lines: such a file may have more,
    so its counts and the totals carry a "+" (they are lower bounds)."""
    big = FileReport("data.json", M, lines=("-old",) * 50 + ("+new",) * 150, note="vs HEAD")
    small = FileReport("a.json", C, lines=("+x",), note="new file")
    assert capped(big) and not capped(small)
    ctx = dataclasses.replace(LINKED, web_port=None)
    assert render(ctx, [big, small], max_lines=1, html=True) == lines(
        "<b>🔑 myapp</b> · <code>main</code> · 11:42",
        "2 files · +151+ −50+",
        "✏️ <code>data.json</code> +150+ −50+",
        "➕ <code>a.json</code> +1 −0",
        "<blockquote expandable>data.json",
        "<code>   -old",
        "   … (199 more lines)</code>",
        "a.json",
        "<code>   +x</code></blockquote>",
    )
    assert render(ctx, [big, small], max_lines=1, html=False) == lines(
        "🔑 myapp · main · 11:42",
        "2 files · +151+ −50+",
        "✏️ data.json +150+ −50+",
        "   -old",
        "   … (199 more lines)",
        "➕ a.json +1 −0",
        "   +x",
    )


def test_a_redacted_files_lines_never_count_or_show() -> None:
    reports = [FileReport(".env", M, redacted=True, lines=("+SECRET=1",))]
    for as_html in (True, False):
        text = render(CTX, reports, max_lines=20, html=as_html)
        assert "SECRET" not in text and "+1" not in text
        assert text.splitlines()[1] == "1 file"


def test_render_escapes_html_everywhere() -> None:
    reports = [
        FileReport("<script>.json", M, lines=("+<b>&",), note="vs HEAD"),
        FileReport("a&b", M, note="symlink → <x>"),
    ]
    ctx = MessageContext("<p>", "a&b", CTX.when, False)
    text = render(ctx, reports, max_lines=5, html=True)
    assert "<script>" not in text and "&lt;script&gt;.json" in text
    assert "<b>&</b>" not in text and "+&lt;b&gt;&amp;" in text
    assert "<b>🔑 &lt;p&gt;</b>" in text and "<code>a&amp;b</code>" in text
    assert "symlink → &lt;x&gt;" in text
    assert "<p>" not in text


def test_render_truncates_lines_per_file() -> None:
    reports = [FileReport("a", M, lines=tuple(f"+{i}" for i in range(30)), note="vs HEAD")]
    text = render(CTX, reports, max_lines=3, html=False)
    assert "   +0\n" in text and "   +2\n" in text and "+3\n" not in text
    assert "   … (27 more lines)" in text
    assert "a +30 −0" in text  # the counts are the report's, not the excerpt's


def test_plain_text_has_no_tags() -> None:
    for case in CASES:
        ctx, reports = CASES[case]
        text = compose(ctx, reports, max_lines=20, html=False).text
        assert re.search(r"</?[a-z]", text) is None, case


FILE_LINE = re.compile(r"^(?:✏️|➕|🗑|🔒) (?:<code>)?(.+?)(?:</code>)?(?: [+·].*)?$")


def listed(text: str) -> list[str]:
    return [m.group(1) for line in text.splitlines() if (m := FILE_LINE.match(line))]


@pytest.mark.parametrize("case", sorted(CASES))
def test_plain_and_html_list_the_same_files_in_the_same_order(case: str) -> None:
    ctx, reports = CASES[case]
    html_text = compose(ctx, reports, max_lines=20, html=True).text
    plain = compose(ctx, reports, max_lines=20, html=False).text
    assert listed(html_text) == listed(plain) == [r.path for r in message_order(reports)]


def balanced(text: str) -> bool:
    for tag in ("b", "code", "a", "blockquote"):
        opens = len(re.findall(rf"<{tag}[ >]", text))
        if opens != text.count(f"</{tag}>"):
            return False
    return "<pre" not in text and text.count("<blockquote") <= 1


# -- shrinking to 4096 (spec §3.4) -----------------------------------------------------

EMOJI_LINES = tuple("+" + "😀" * 100 for _ in range(20))
WIDE = [FileReport(f"dir/{'x' * 80}-{i}.json", M) for i in range(80)]
LONG = [FileReport("a", M, lines=tuple("+" + "x" * 500 for _ in range(50)))]
HEAVY = [
    FileReport(f"f{i}", M, lines=tuple("+" + "y" * 1500 for _ in range(30)), note="vs HEAD")
    for i in range(3)
] + [FileReport("empty", M, note="matches HEAD")]
HINTED = dataclasses.replace(LINKED, web_hint=True)
HUGE = MessageContext("<p>" * 3_400, "b" * 10_000, CTX.when, web_hint=True)
TINY = MessageContext("<&>" * 50, "main", CTX.when, web_hint=False)
TWO = [FileReport("a", M, lines=("+x",)), FileReport(".env", M, redacted=True)]

SHRINK: dict[str, tuple[MessageContext, list[FileReport], int, int]] = {
    "layout": (LINKED, LAYOUT, 20, 4096),
    "emoji": (LINKED, [FileReport("a", M, lines=EMOJI_LINES, note="vs HEAD")], 20, 4096),
    "collapse": (HINTED, WIDE, 20, 4096),
    "negative": (LINKED, LONG, -1, 4096),
    "huge_header": (HUGE, TWO, 20, 4096),
    "tiny": (TINY, [FileReport("a", M, lines=("+x",))], 5, 40),
    "heavy": (LINKED, HEAVY, 20, 4096),
}


@pytest.mark.parametrize("case", sorted(SHRINK))
@pytest.mark.parametrize("as_html", [True, False])
def test_every_composition_fits_and_is_balanced(case: str, as_html: bool) -> None:
    ctx, reports, max_lines, limit = SHRINK[case]
    text = compose(ctx, reports, max_lines=max_lines, html=as_html, limit=limit).text
    assert tg_len(text) <= limit
    if as_html:
        assert balanced(text), text[:300]


def test_step_1_halves_the_line_budget_and_keeps_the_quote() -> None:
    reports = [FileReport("a", M, lines=EMOJI_LINES, note="vs HEAD")]
    full = render(LINKED, reports, max_lines=20, html=True)
    assert len(full) <= 4096 < tg_len(full)  # a code-point count would wrongly accept it
    composed = compose(LINKED, reports, max_lines=20, html=True)
    assert (composed.lines, composed.files) == (10, 1)
    assert "<blockquote expandable>" in composed.text and "… (10 more lines)" in composed.text


def test_step_2_drops_the_quote_and_no_line_reached_telegram() -> None:
    """HEAVY's quote doesn't fit even at budget 0 (its "lines hidden" markers alone
    are too long): the quote goes, every file stays listed, and lines is 0."""
    composed = compose(LINKED, HEAVY, max_lines=20, html=True, limit=300)
    assert composed.lines == 0 and composed.files == len(HEAVY)
    assert "blockquote" not in composed.text and "y" * 10 not in composed.text
    assert listed(composed.text) == [r.path for r in HEAVY]
    plain = compose(LINKED, HEAVY, max_lines=20, html=False, limit=200)
    assert plain.files == len(HEAVY)
    assert plain.lines == 0 and "   " not in plain.text  # no inline diff either


def test_step_3_collapses_the_trailing_files() -> None:
    composed = compose(HINTED, WIDE, max_lines=20, html=True)
    assert composed.lines == 0 and 0 < composed.files < len(WIDE)
    assert f"…and {len(WIDE) - composed.files} more files (see web UI)" in composed.text
    assert composed.text.splitlines()[-1] == link_line(HINTED, html=True)


def test_step_4_clips_the_header_and_step_5_hard_truncates() -> None:
    clipped = compose(HUGE, TWO, max_lines=20, html=True)
    assert (clipped.lines, clipped.files) == (0, 0)
    assert "b" * 200 + "…" in clipped.text and "b" * 201 not in clipped.text
    tiny = compose(TINY, [FileReport("a", M, lines=("+x",))], max_lines=5, html=True, limit=40)
    assert tg_len(tiny.text) <= 40 and (tiny.lines, tiny.files) == (0, 0)
    assert "<" not in tiny.text.replace("&lt;", "")
    assert not tiny.text.rstrip().endswith(("&", "&l", "&lt", "&a", "&amp", "&g", "&gt"))


def stage(composed: Composed, reports: list[FileReport], max_lines: int) -> int:
    """Which step of §3.4 produced this composition."""
    if composed.files < len(reports):
        return 3
    if "blockquote" not in composed.text:
        return 2
    return 0 if composed.lines == max_lines else 1


def test_the_steps_come_in_order_as_the_limit_shrinks() -> None:
    reports = [
        FileReport("package.json", M, lines=tuple(f"+line {i} " + "z" * 30 for i in range(16))),
        FileReport("app.json", C, lines=tuple(f"+{i}" for i in range(8))),
        *[FileReport(f"conf/{i:02}.yaml", M) for i in range(12)],
    ]
    seen: list[int] = []
    for limit in range(1700, 100, -20):
        composed = compose(LINKED, reports, max_lines=16, html=True, limit=limit)
        assert tg_len(composed.text) <= limit
        seen.append(stage(composed, reports, 16))
    assert seen == sorted(seen)  # never back to an earlier step
    assert set(seen) == {0, 1, 2, 3}  # and every step happened


@pytest.mark.parametrize("case", sorted(SHRINK))
def test_composed_reports_what_it_rendered(case: str) -> None:
    """Re-rendering with the returned budget and file count gives the same text, with
    the quote when it is there, except in the clipped-header cases, which list no
    file and no line."""
    ctx, reports, max_lines, limit = SHRINK[case]
    composed = compose(ctx, reports, max_lines=max_lines, html=True, limit=limit)
    if case in ("huge_header", "tiny"):
        assert (composed.lines, composed.files) == (0, 0)
        return
    shown = None if composed.files == len(reports) else composed.files
    quoted = "<blockquote expandable>" in composed.text
    assert quoted or composed.lines == 0  # no quote: no diff line reached Telegram
    again = render(ctx, reports, max_lines=composed.lines, html=True, shown=shown, diffs=quoted)
    assert again == composed.text


def test_the_link_survives_every_step_but_the_last_two() -> None:
    for case in ("layout", "emoji", "collapse", "negative", "heavy"):
        ctx, reports, max_lines, limit = SHRINK[case]
        for as_html in (True, False):
            text = compose(ctx, reports, max_lines=max_lines, html=as_html, limit=limit).text
            assert text.splitlines()[-1] == link_line(ctx, html=as_html), case
    huge = dataclasses.replace(HUGE, event_id=42, web_port=8765)
    assert compose(huge, TWO, max_lines=20, html=True).text.endswith("Open #42</a>")
    tiny = dataclasses.replace(TINY, event_id=42, web_port=8765)
    clipped = compose(tiny, [FileReport("a", M, lines=("+x",))], max_lines=5, html=True, limit=40)
    assert tg_len(clipped.text) <= 40 and "🔗" not in clipped.text


def test_the_header_and_summary_survive_shrinking() -> None:
    composed = compose(HINTED, WIDE, max_lines=20, html=True)
    first, second = composed.text.splitlines()[:2]
    assert first == "<b>🔑 myapp</b> · <code>main</code> · 11:42"
    assert second == "80 files"


def test_tg_len_counts_utf16_units() -> None:
    assert tg_len("abc") == 3
    assert tg_len("😀") == 2
    assert tg_len("✏️") == 2  # pencil + variation selector
    assert tg_len("a\ud800b") == 3  # a lone surrogate is tolerated


def test_a_negative_budget_is_zero() -> None:
    assert "+xxx" not in render(CTX, LONG, max_lines=-5, html=False)
    assert compose(CTX, LONG, max_lines=-1, html=True).lines == 0


# -- the deep link (notify controls spec §4.4) ----------------------------------------


def test_a_catch_up_link_names_the_other_events() -> None:
    ctx = dataclasses.replace(LINKED, more=6)
    assert link_line(ctx, html=True) == (
        '🔗 <a href="http://127.0.0.1:8765/?event=42">Open #42 (+6 more)</a>'
    )
    assert link_line(ctx, html=False) == "🔗 Open #42 (+6 more): http://127.0.0.1:8765/?event=42"


@pytest.mark.parametrize(("event_id", "web_port"), [(None, None), (42, None), (None, 8765)])
def test_no_link_without_both_an_event_and_the_web_port(
    event_id: int | None, web_port: int | None
) -> None:
    ctx = dataclasses.replace(CTX, event_id=event_id, web_port=web_port)
    assert link_line(ctx, html=True) is None
    assert render(ctx, LAYOUT, max_lines=20, html=True) == GOLDEN[("no_link", True)]


# -- catch-up (notify controls spec §4.3, health spec §3.2) ----------------------------


def test_a_quiet_hours_catch_up_of_a_removed_project() -> None:
    held = dataclasses.replace(HELD, reason="quiet", changes=1, removed=True)
    ctx = dataclasses.replace(CTX, project="o<l>d", branch=None, catch_up=held)
    first, second = render(ctx, [FileReport("a", M)], max_lines=20, html=True).splitlines()[:2]
    assert first == "<b>🔕 Catch-up · o&lt;l&gt;d</b> (removed) · held 14:05–15:40 (quiet hours)"
    assert second == "1 change in 1 file"


def test_a_collapsed_catch_up_still_counts_every_file_in_its_header() -> None:
    """compose may collapse trailing files to fit: the summary keeps counting all of
    them ("in N files", N = len(reports)), like "…and K more files" below it."""
    ctx = dataclasses.replace(CTX, catch_up=dataclasses.replace(HELD, changes=6), event_id=9)
    reports = [FileReport(f"{name}.json", M, lines=("+x" * 40,)) for name in "abcdef"]
    composed = compose(ctx, reports, max_lines=20, html=True, limit=200)
    assert 0 < composed.files < len(reports)  # some files were collapsed
    assert composed.text.splitlines()[1] == "6 changes in 6 files · +6 −0"
    assert f"…and {len(reports) - composed.files} more files" in composed.text


# -- review fixes, batch 4 ------------------------------------------------------------


def test_max_lines_0_in_html_keeps_the_quote_with_the_hidden_markers() -> None:
    assert render(CTX, SINGLE, max_lines=0, html=True) == (
        "<b>🔑 myapp</b> · <code>main</code> · 11:42\n"
        "1 file · +2 −1\n"
        "✏️ <code>package.json</code> +2 −1\n"
        "<blockquote expandable>package.json\n"
        "<code>   … (3 lines hidden)</code></blockquote>"
    )


BIDI = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
SHOWN = "\\u200E\\u200F\\u202A\\u202B\\u202C\\u202D\\u202E\\u2066\\u2067\\u2068\\u2069"


@pytest.mark.parametrize("as_html", [True, False])
def test_bidi_controls_are_shown_not_obeyed(as_html: bool) -> None:
    """A file named "x\\u202Egnp.exe" must not read as "xexe.png": every bidi control
    in a project, branch, path or note is replaced by its visible \\uXXXX form."""
    reports = [
        FileReport(f"a{BIDI}.json", M, lines=("+1",), note="vs HEAD"),
        FileReport(f"b{BIDI}.png", M, note=f"binary{BIDI}"),
        FileReport(f"c{BIDI}.env", M, redacted=True),
    ]
    ctx = MessageContext(f"p{BIDI}", f"br{BIDI}", CTX.when, False)
    text = render(ctx, reports, max_lines=5, html=as_html)
    assert not any(c in text for c in BIDI)
    for shown in (f"p{SHOWN}", f"br{SHOWN}", f"a{SHOWN}.json", f"b{SHOWN}.png"):
        assert shown in text
    assert f"binary{SHOWN}" in text and f"c{SHOWN}.env" in text
    held = dataclasses.replace(ctx, catch_up=HELD)
    assert not any(c in render(held, reports, max_lines=5, html=as_html) for c in BIDI)


# -- monospace lines in the quote (diff readability spec §5.2) -------------------------


def quote_of(text: str) -> str | None:
    """What is inside the blockquote (None when there is none)."""
    match = re.search(r"<blockquote expandable>(.*?)</blockquote>", text, re.S)
    return None if match is None else match.group(1)


QUOTED = sorted(CASES) + [f"shrink:{c}" for c in sorted(SHRINK)]


def composed_case(case: str) -> tuple[list[FileReport], str]:
    if case.startswith("shrink:"):
        ctx, reports, max_lines, limit = SHRINK[case.removeprefix("shrink:")]
    else:
        (ctx, reports), max_lines, limit = CASES[case], 20, 4096
    return reports, compose(ctx, reports, max_lines=max_lines, html=True, limit=limit).text


# The quote: per file, its plain path line, then ONE <code> spanning its indented lines.
FILE_BLOCK = r"[^<\n]+\n<code>   [^<]*</code>"
QUOTE = re.compile(rf"{FILE_BLOCK}(?:\n{FILE_BLOCK})*")


@pytest.mark.parametrize("case", QUOTED)
def test_each_files_lines_are_one_code_block_and_never_pre(case: str) -> None:
    reports, text = composed_case(case)
    assert "<pre" not in text
    quote = quote_of(text)
    if quote is None:
        return
    assert QUOTE.fullmatch(quote), quote
    paths = {html_lib.escape(r.path, quote=True) for r in reports}
    for path, block in re.findall(r"([^<\n]+)\n<code>([^<]*)</code>", quote):
        assert path in paths
        assert all(line.startswith("   ") for line in block.split("\n")), block


def test_a_files_lines_share_one_code_tag_not_one_per_line() -> None:
    text = render(LINKED, SINGLE, max_lines=20, html=True)
    quote = quote_of(text)
    assert quote is not None and quote.count("<code>") == 1
    assert (
        "<code>   -  &quot;version&quot;: &quot;1.0.0&quot;\n"
        "   +  &quot;version&quot;: &quot;1.1.0&quot;\n"
        "   +  &quot;private&quot;: true</code>"
    ) in text


def test_entity_count_counts_every_formatting_tag() -> None:
    assert entity_count("plain &lt;b&gt; text") == 0
    assert entity_count('<b>x</b> <code>y</code> <a href="u">z</a>') == 3
    assert entity_count("<blockquote expandable>p\n<code>   +1\n   +2</code></blockquote>") == 2
    assert entity_count(GOLDEN[("multiple", True)]) == 1 + 1 + 4 + 1 + 2 + 1
    assert entity_count(GOLDEN[("single", True)]) == 6
    assert entity_count(GOLDEN[("no_lines", True)]) == 6
    for case in CASES:
        assert entity_count(GOLDEN[(case, True)]) <= ENTITY_LIMIT


def test_too_many_entities_drop_the_code_blocks_from_the_quote() -> None:
    """Telegram apps ignore formatting entities past ~100 per message (probe message
    28): above ENTITY_LIMIT the quote's lines go plain, all of them, so the
    formatting degrades uniformly instead of half-way."""
    reports = [FileReport(f"f{i:02}.json", M, lines=(f"+{i}",)) for i in range(50)]
    text = compose(LINKED, reports, max_lines=20, html=True).text
    quote = quote_of(text)
    assert quote is not None and "<code>" not in quote
    assert quote.startswith("f00.json\n   +0\nf01.json\n   +1\n")
    assert balanced(text)
    # A normal message keeps the blocks.
    few = compose(LINKED, reports[:5], max_lines=20, html=True).text
    assert entity_count(few) <= ENTITY_LIMIT and "<code>   +0</code>" in few


@pytest.mark.parametrize("limit", [4096, 2000, 900, 400, 120])
def test_many_files_stay_under_the_limit_or_lose_the_quote_code(limit: int) -> None:
    reports = [FileReport(f"f{i:02}.json", M, lines=(f"+{i}", f"-{i}")) for i in range(50)]
    text = compose(LINKED, reports, max_lines=20, html=True, limit=limit).text
    assert tg_len(text) <= limit and balanced(text)
    quote = quote_of(text)
    assert entity_count(text) <= ENTITY_LIMIT or quote is None or "<code>" not in quote


def test_the_code_tags_count_toward_the_limit() -> None:
    """The tags are inside the tg_len budget: a limit that would fit the lines
    without their tags (13 characters per file block) makes compose shrink the line
    budget."""
    reports = [FileReport("a", M, lines=tuple(f"+{i:03}" for i in range(20)))]
    full = compose(LINKED, reports, max_lines=20, html=True)
    assert full.lines == 20
    untagged = full.text.replace("<code>", "").replace("</code>", "")
    composed = compose(LINKED, reports, max_lines=20, html=True, limit=tg_len(untagged))
    assert composed.lines < 20 and tg_len(composed.text) <= tg_len(untagged)


def test_the_plain_text_fallback_has_no_code_lines() -> None:
    for case in CASES:
        ctx, reports = CASES[case]
        plain = compose(ctx, reports, max_lines=20, html=False).text
        assert "<code>" not in plain and plain == GOLDEN[(case, False)]


def test_a_setup_catch_up_says_it_waited_for_telegram() -> None:
    held = dataclasses.replace(HELD, reason="setup")
    ctx = dataclasses.replace(CTX, branch=None, catch_up=held)
    reports = [FileReport("a", M)]
    html = render(ctx, reports, max_lines=20, html=True).splitlines()[0]
    plain = render(ctx, reports, max_lines=20, html=False).splitlines()[0]
    assert html == "<b>🔕 Catch-up · myapp</b> · held until Telegram was set up (14:05–15:40)"
    assert plain == "🔕 Catch-up · myapp · held until Telegram was set up (14:05–15:40)"


def test_a_catch_up_header_shows_the_branch() -> None:
    ctx = MessageContext(
        "proj",
        "main",
        datetime(2026, 10, 15, 8, 0),
        False,
        catch_up=CatchUp(datetime(2026, 10, 15, 1, 0), datetime(2026, 10, 15, 7, 0), "quiet", 2),
    )
    reports = [FileReport("a.json", M, lines=("+x",), note="uncommitted")]
    html = render(ctx, reports, max_lines=20, html=True)
    plain = render(ctx, reports, max_lines=20, html=False)
    assert html.splitlines()[0] == (
        "<b>🔕 Catch-up · proj</b> · <code>main</code> · held 01:00–07:00 (quiet hours)"
    )
    assert plain.splitlines()[0] == "🔕 Catch-up · proj · main · held 01:00–07:00 (quiet hours)"
