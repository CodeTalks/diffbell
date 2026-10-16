"""Render a batch as a Telegram message: layout A2 (health spec §3).

    <b>🔑 myapp</b> · <code>main</code> · 11:42
    2 files · +2 −1
    ✏️ <code>package.json</code> +1 −1
    🔒 <code>.env.local</code> · contents hidden
    <blockquote expandable>package.json
    <code>   -  "a": 1
       +  "a": 2</code></blockquote>
    🔗 <a href="http://127.0.0.1:8765/?event=42">Open #42</a>

The header and summary make the lock-screen preview; the diffs are folded into one
expandable quote: a path line per file (plain), then its lines indented by three
spaces, all inside ONE ``<code>`` per file so they show in monospace. Checked with
real sends (Plans 5a and 5b): ``<code>`` inside the quote is accepted and renders
monospace on the Mac and on Android; ``<pre>`` would split the quote on Android, so
it is never used. One ``<code>`` per line was dropped: Telegram apps ignore entities
past about 100 per message, so a long message showed its last files plain (probe
B in ADR 0013; one block per file, probe C, rendered every file). Above
ENTITY_LIMIT entities, ``compose`` quotes the lines without ``<code>``. The
plain-text fallback has no tags.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from diffbell import BRAND
from diffbell.diffing import MAX_COLLECT_LINES, FileReport
from diffbell.models import ChangeKind

TELEGRAM_LIMIT = 4096
HEADER_FIELD_MAX = 200  # project/branch are clipped to this when the header alone is too long
EMOJI = {ChangeKind.CREATED: "➕", ChangeKind.MODIFIED: "✏️", ChangeKind.DELETED: "🗑"}
# Files are listed modified, created, deleted, then redacted (report order within).
KIND_ORDER = (ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED)
MINUS = "−"  # U+2212: "+3 −1"
INDENT = "   "  # a diff line under its file (the quote, and the plain-text form)
# Telegram apps stop applying formatting entities past about 100 per message (probe
# B in ADR 0013): above this the quote's lines go plain, so the formatting degrades
# uniformly rather than half-way through the message.
ENTITY_LIMIT = 90
_ENTITY = re.compile(r"<(?:b|code|blockquote|a)[ >]")
# Bidi controls (ALM, LRM, RLM, the embeddings/overrides and the isolates): in a name
# they could make "x\u202egnp.exe" read as "xexe.png"; in a diff line they reorder
# what is shown (Trojan Source). They are shown, never obeyed.
BIDI_CONTROLS = (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
# What a Telegram client may draw as a line break or not at all: C0 (but "\t"), DEL,
# C1 (U+0085 among them), U+2028/U+2029 and the bidi controls. In a diff line each
# becomes ONE visible character (one UTF-16 unit for one: offsets and tg_len hold):
# C0 its Control Picture (U+2400 + code point, "\r" -> U+240D), DEL U+2421, the
# rest OTHER_MARK. static/js/diffview.js `visible` is the same mapping
# (tests/web/test_js_visible.py). ADR 0017, "Display of control characters".
OTHER_MARK = "\u2370"
SHOWN_CONTROLS = (
    *(c for c in range(0x20) if c != 0x09),
    0x7F,
    *range(0x80, 0xA0),
    0x2028,
    0x2029,
    *BIDI_CONTROLS,
)
_LINE_MAP = {
    c: chr(0x2400 + c) if c < 0x20 else "\u2421" if c == 0x7F else OTHER_MARK
    for c in SHOWN_CONTROLS
}
# In a name or note (no offsets to keep) the same characters read as "\uXXXX".
_NAME_MAP = {c: f"\\u{c:04X}" for c in SHOWN_CONTROLS}


def _visible(name: str) -> str:
    """``name`` with every SHOWN_CONTROLS character replaced by its ``\\uXXXX`` form:
    a path holding "\\n" or U+2028 cannot add a line to the message."""
    return name.translate(_NAME_MAP)


def visible(line: str) -> str:
    """A diff line as displayed: every SHOWN_CONTROLS character replaced by one
    visible character (same length in code points and in UTF-16 units)."""
    return line.translate(_LINE_MAP)


def display_lines(lines: Sequence[str]) -> list[str]:
    """A file's stored diff lines as displayed. When EVERY line ends in "\\r" (a CRLF
    file) that "\\r" is dropped, so an edit in a Windows file shows no noise; with
    mixed endings each "\\r" stays and shows as U+240D. Display only: storage,
    search and ``event_reports`` keep the raw lines."""
    if lines and all(line.endswith("\r") for line in lines):
        lines = [line[:-1] for line in lines]
    return [visible(line) for line in lines]


@dataclass(frozen=True, slots=True)
class CatchUp:
    """What a catch-up message's header says (notify controls spec §4.3)."""

    start: datetime  # created_at of the group's first event
    end: datetime  # created_at of its last event
    reason: Literal["snooze", "quiet", "setup"]
    changes: int  # journal rows behind the files the message lists
    removed: bool = False  # the project is no longer in the config


@dataclass(frozen=True, slots=True)
class MessageContext:
    project: str
    branch: str | None
    when: datetime
    web_hint: bool
    # The deep link (notify controls spec §4.4): only with both an event and the web
    # UI's port. The sample message never sets event_id, so it never has a link.
    event_id: int | None = None
    more: int = 0  # the other events of a catch-up message: "#ID (+K more)"
    web_port: int | None = None  # None while the web UI is disabled
    catch_up: CatchUp | None = None


def message_order(reports: Sequence[FileReport]) -> list[FileReport]:
    """The order a message lists files in; the notifier stores them in it too, so
    ``Composed.files`` counts the stored report's first files."""
    return sorted(reports, key=lambda r: 3 if r.redacted else KIND_ORDER.index(r.kind))


def line_counts(report: FileReport) -> tuple[int, int] | None:
    """(+, −) lines of a file's report; None for a deleted or redacted file, or one
    without lines (no counts are shown for those)."""
    if report.redacted or report.kind is ChangeKind.DELETED or not report.lines:
        return None
    added = sum(1 for line in report.lines if line.startswith("+"))
    return added, sum(1 for line in report.lines if line.startswith("-"))


def capped(report: FileReport) -> bool:
    """The report holds as many lines as diffing collects (MAX_COLLECT_LINES): the
    file may have more, so its counts are lower bounds."""
    return len(report.lines) >= MAX_COLLECT_LINES


def _counts(added: int, removed: int, at_least: bool) -> str:
    """ "+3 −1", or "+150+ −50+" when the counts are lower bounds."""
    more = "+" if at_least else ""
    return f"+{added}{more} {MINUS}{removed}{more}"


def link_line(ctx: MessageContext, *, html: bool) -> str | None:
    """ "🔗 Open #42", linking to the local dashboard. It names only the event id,
    never a token."""
    if ctx.event_id is None or ctx.web_port is None:
        return None
    url = f"http://127.0.0.1:{ctx.web_port}/?event={ctx.event_id}"
    more = f" (+{ctx.more} more)" if ctx.more else ""
    label = f"Open #{ctx.event_id}{more}"
    return f'🔗 <a href="{url}">{label}</a>' if html else f"🔗 {label}: {url}"


def entity_count(text: str) -> int:
    """The formatting entities of a rendered HTML message: its opening ``<b>``,
    ``<code>``, ``<blockquote>`` and ``<a>`` tags (escaped text has no ``<``)."""
    return len(_ENTITY.findall(text))


def tg_len(text: str) -> int:
    """Telegram counts message length in UTF-16 code units, not code points."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _clip_utf16(text: str, limit: int) -> str:
    """Longest prefix of ``text`` whose UTF-16 length is <= ``limit``."""
    used = 0
    for i, char in enumerate(text):
        used += tg_len(char)
        if used > limit:
            return text[:i]
    return text


def _clip_field(value: str) -> str:
    return value if len(value) <= HEADER_FIELD_MAX else value[:HEADER_FIELD_MAX] + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _body(report: FileReport, max_lines: int) -> list[str]:
    """A file's diff lines as displayed (``display_lines``), cut to ``max_lines``,
    with the "more lines" marker."""
    shown = display_lines(report.lines)[:max_lines]
    if not shown:
        return [f"… ({len(report.lines)} lines hidden)"] if report.lines else []
    hidden = len(report.lines) - len(shown)
    return shown + ([f"… ({hidden} more lines)"] if hidden else [])


def render(
    ctx: MessageContext,
    reports: Sequence[FileReport],
    *,
    max_lines: int,
    html: bool,
    shown: int | None = None,
    diffs: bool = True,
    mono: bool = True,
) -> str:
    """The message. ``shown``: list only the first files (the rest collapse into
    "…and K more files"); ``diffs``: include the diff lines (the quote, or the
    plain-text form's indented lines); ``mono``: wrap each file's quoted lines in
    one ``<code>`` (HTML only; ``compose`` turns it off above ENTITY_LIMIT)."""
    max_lines = max(0, max_lines)
    escape: Callable[[str], str] = (lambda s: html_lib.escape(s, quote=True)) if html else str

    def esc(s: str) -> str:
        """A name or note: escaped, its controls and bidi controls made visible."""
        return escape(_visible(s))

    def bold(s: str) -> str:
        return f"<b>{s}</b>" if html else s

    def code(s: str) -> str:
        return f"<code>{esc(s)}</code>" if html else esc(s)

    ordered = message_order(reports)
    visible = ordered if shown is None else ordered[:shown]
    counted = [n for r in ordered if (n := line_counts(r)) is not None]
    at_least = any(capped(r) for r in ordered if line_counts(r) is not None)
    totals = (
        " · " + _counts(sum(a for a, _ in counted), sum(r for _, r in counted), at_least)
        if counted
        else ""
    )
    if ctx.catch_up is None:
        head = [bold(f"🔑 {esc(ctx.project)}")]
        if ctx.branch:
            head.append(code(ctx.branch))
        head.append(f"{ctx.when:%H:%M}")
        out = [" · ".join(head), _plural(len(ordered), "file") + totals]
    else:
        c = ctx.catch_up
        removed = " (removed)" if c.removed else ""
        # The last held event's branch, frozen at its creation (content snapshots §3.3).
        branch = f" · {code(ctx.branch)}" if ctx.branch else ""
        span = f"{c.start:%H:%M}–{c.end:%H:%M}"
        if c.reason == "setup":
            held = f"held until Telegram was set up ({span})"
        else:
            held = f"held {span} ({'snoozed' if c.reason == 'snooze' else 'quiet hours'})"
        out = [
            f"{bold(f'🔕 Catch-up · {esc(ctx.project)}')}{removed}{branch} · {held}",
            f"{_plural(c.changes, 'change')} in {_plural(len(ordered), 'file')}{totals}",
        ]

    quote: list[str] = []
    for report in visible:
        if report.redacted:
            out.append(f"🔒 {code(report.path)} · contents hidden")
            continue
        label = f"{EMOJI[report.kind]} {code(report.path)}"
        counts = line_counts(report)
        if counts is not None:
            label += " " + _counts(*counts, capped(report))
        elif report.note:
            label += f" · {esc(report.note)}"
        out.append(label)
        body = _body(report, max_lines) if diffs else []
        if not body:
            continue
        indented = [escape(INDENT + line) for line in body]
        if html:
            # The path line stays plain; the file's lines share ONE <code> (monospace).
            block = "\n".join(indented)
            quote += [esc(report.path), f"<code>{block}</code>" if mono else block]
        else:
            out += indented

    hidden_files = len(ordered) - len(visible)
    if hidden_files:
        tail = f"…and {_plural(hidden_files, 'more file')}"
        out.append(tail + (" (see web UI)" if ctx.web_hint else ""))
    if quote:
        out.append("<blockquote expandable>" + "\n".join(quote) + "</blockquote>")
    link = link_line(ctx, html=html)
    if link is not None:
        out.append(link)
    return "\n".join(out)


@dataclass(frozen=True, slots=True)
class Composed:
    """A rendered message and what it holds: ``lines`` is the per-file diff budget
    whose lines reached Telegram (``compose`` may have halved ``max_lines`` to fit;
    exactly 0 once the quote is dropped), and ``files`` how many files, in message
    order, it lists. Stored with the event's report, so the web UI can mark where
    the Telegram excerpt ended."""

    text: str
    lines: int
    files: int


def compose(
    ctx: MessageContext,
    reports: Sequence[FileReport],
    *,
    max_lines: int,
    html: bool,
    limit: int = TELEGRAM_LIMIT,
) -> Composed:
    """The largest rendering that fits (spec §3.4), trying in order: halve the
    per-file line budget down to 0; drop the quote (the plain form's inline lines);
    collapse the trailing file lines; clip the header fields; hard-truncate.
    A rendering with more than ENTITY_LIMIT formatting entities quotes its lines
    without ``<code>`` (tried before its length is checked).

    The text always satisfies ``tg_len(text) <= limit`` and, in HTML mode, contains
    only balanced tags and whole entities. The header, summary and link survive every
    step but the last two (a header too long even when clipped, a tiny limit).
    """
    files = len(reports)
    budget = max(0, max_lines)
    while True:
        text = render(ctx, reports, max_lines=budget, html=html)
        if html and entity_count(text) > ENTITY_LIMIT:
            text = render(ctx, reports, max_lines=budget, html=html, mono=False)
        if tg_len(text) <= limit:
            return Composed(text, budget, files)
        if budget == 0:
            break
        budget //= 2
    text = render(ctx, reports, max_lines=0, html=html, diffs=False)
    if tg_len(text) <= limit:
        return Composed(text, 0, files)
    for shown in range(files - 1, -1, -1):
        text = render(ctx, reports, max_lines=0, html=html, shown=shown, diffs=False)
        if tg_len(text) <= limit:
            return Composed(text, 0, shown)
    # The header alone is too long (absurd project/branch name): clip those.
    short = replace(
        ctx,
        project=_clip_field(ctx.project),
        branch=None if ctx.branch is None else _clip_field(ctx.branch),
    )
    text = render(short, reports, max_lines=0, html=html, shown=0, diffs=False)
    if tg_len(text) <= limit:
        return Composed(text, 0, 0)
    # Tiny limit: hard-truncate a tag-free rendering. Escape after clipping so
    # no entity is ever cut, shrinking until the escaped form fits.
    plain = render(short, reports, max_lines=0, html=False, shown=0, diffs=False)
    if not html:
        return Composed(_clip_utf16(plain, limit), 0, 0)
    clipped = _clip_utf16(plain, limit)
    while clipped:
        escaped = html_lib.escape(clipped, quote=True)
        if tg_len(escaped) <= limit:
            return Composed(escaped, 0, 0)
        clipped = clipped[:-1]
    return Composed("", 0, 0)


# The sample's files: a changed file and a new one (both in the quote) and a redacted
# file, with the notes content snapshots give them (content snapshots spec §3.3).
SAMPLE_REPORTS: tuple[FileReport, ...] = (
    FileReport(
        "package.json",
        ChangeKind.MODIFIED,
        lines=('-  "version": "0.1.0"', '+  "version": "0.1.1"'),
        note="uncommitted",
    ),
    FileReport(
        "migrations/0002_index.sql",
        ChangeKind.CREATED,
        lines=("+CREATE INDEX events_project ON events(project);",),
        note="untracked",
    ),
    FileReport(".env.local", ChangeKind.MODIFIED, redacted=True),
)


def sample_message(when: datetime) -> str:
    """The sample notification sent by `diffbell test` and the web UI's
    "Send test notification" (SAMPLE_REPORTS). It never has a link."""
    ctx = MessageContext(f"{BRAND} (sample)", "main", when, web_hint=False)
    return compose(ctx, list(SAMPLE_REPORTS), max_lines=20, html=True).text
