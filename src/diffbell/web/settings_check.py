"""Settings validation: one source for every rule and message (settings redesign
spec §3).

``POST /api/settings/check`` and ``PUT /api/settings`` run :func:`check_settings` on
the body as the page sends it (numbers as the text typed), and
``config_edit.describe_validation_error`` words the config model's own errors with
:func:`message_for`, so the page, a save and a config-load rejection say the same
sentence. The rules are exactly the config model's (``DebounceConfig``,
``DiffConfig``, ``NotifyConfig`` with ``machine_name_ok``, and ``check_pattern``);
the page never decides validity itself. Pure: nothing here reads or writes a file.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from diffbell.config import (
    HEARTBEAT_TIME,
    MaxBelowQuiet,
    QuietHoursSame,
    check_pattern,
    machine_name_ok,
)

# The one table. The page shows these texts as they are.
MESSAGES: dict[str, str] = {
    "seconds_number": "Enter a number of seconds.",
    "seconds_positive": "Must be more than 0 seconds.",
    "seconds_at_least_quiet": "Must be at least the quiet time ({quiet} seconds).",
    "lines_range": "Enter 0 to 200 lines.",
    "time_format": "Use a time like 23:00.",
    "time_differs": "Must differ from the start time.",
    "machine_name": "Use at most 40 characters, without control characters.",
}
MAX_LINES = 200  # config.DiffConfig's le (tests/web/test_settings_check.py pins it)

# A number as typed into a number field: ASCII digits with an optional sign, decimal
# point and exponent. float() alone would also take "inf", "nan", "1_000" and other
# scripts' digits.
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
_WHOLE = re.compile(r"[+-]?\d+", re.ASCII)
_LISTS = ("defaults", "ignore", "redact")
_SECONDS_LOCS = (("debounce", "quiet_seconds"), ("debounce", "max_wait_seconds"))


@dataclass(frozen=True, slots=True)
class Issue:
    """A bad pattern. ``line`` is its 1-based position in the list as sent (the page
    sends its non-empty lines, trimmed, in order)."""

    line: int
    reason: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Field name -> message, and list name -> its issues. Both empty: valid."""

    errors: dict[str, str] = field(default_factory=dict)
    issues: dict[str, list[Issue]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.issues

    def body(self) -> dict[str, Any]:
        """What POST /api/settings/check returns, and the 422's errors/issues."""
        return {
            "errors": dict(self.errors),
            "issues": {
                name: [{"line": issue.line, "reason": issue.reason} for issue in issues]
                for name, issues in self.issues.items()
            },
        }

    def summary(self) -> str:
        """The 422's one-line ``error``: at most five problems, joined like
        describe_validation_error's."""
        parts = [f"{name}: {message}" for name, message in self.errors.items()]
        parts += [
            f"{name} line {issue.line}: {issue.reason}"
            for name, issues in self.issues.items()
            for issue in issues
        ]
        text = "; ".join(parts[:5])
        if len(parts) > 5:
            text += f" …and {len(parts) - 5} more"
        return text


@dataclass(frozen=True, slots=True)
class SettingsRaw:
    """PUT /api/settings's body without config_version: each number as sent (the text
    typed, or a JSON number), the pattern lists as sent."""

    quiet_seconds: str | float
    max_wait_seconds: str | float
    max_lines: str | float
    defaults: Sequence[str]
    ignore: Sequence[str]
    redact: Sequence[str]
    quiet_hours: str | None = None  # "23:00-08:00"; None: off, or the block not sent
    heartbeat: str | None = None  # "09:00"; None: off, or not sent
    machine: str | None = None  # this Mac's name; "": none; None: Computer Name, or not sent


def check_settings(raw: SettingsRaw) -> CheckResult:
    """Every rule the config model applies to these fields, worded by MESSAGES. An
    empty or non-numeric number is an error, never 0."""
    errors: dict[str, str] = {}
    quiet = _number(raw.quiet_seconds)
    max_wait = _number(raw.max_wait_seconds)
    if (message := _seconds_error(quiet)) is not None:
        errors["quiet_seconds"] = message
    if (message := _seconds_error(max_wait)) is not None:
        errors["max_wait_seconds"] = message
    elif quiet is not None and quiet > 0 and max_wait is not None and max_wait < quiet:
        errors["max_wait_seconds"] = MESSAGES["seconds_at_least_quiet"].format(
            quiet=_as_typed(raw.quiet_seconds)
        )
    if _lines(raw.max_lines) is None:
        errors["max_lines"] = MESSAGES["lines_range"]
    if raw.quiet_hours is not None:
        errors |= _window(raw.quiet_hours)
    if raw.heartbeat is not None and not HEARTBEAT_TIME.fullmatch(raw.heartbeat):
        errors["heartbeat"] = MESSAGES["time_format"]
    if raw.machine is not None and not machine_name_ok(raw.machine):
        errors["machine"] = MESSAGES["machine_name"]
    lists = {"defaults": raw.defaults, "ignore": raw.ignore, "redact": raw.redact}
    issues = {name: found for name in _LISTS if (found := _pattern_issues(lists[name]))}
    return CheckResult(errors, issues)


def settings_values(raw: SettingsRaw) -> tuple[float, float, int]:
    """(quiet_seconds, max_wait_seconds, max_lines) of a body check_settings passed.
    Raises ValueError for one that doesn't parse."""
    quiet, max_wait, lines = (
        _number(raw.quiet_seconds),
        _number(raw.max_wait_seconds),
        _lines(raw.max_lines),
    )
    if quiet is None or max_wait is None or lines is None:
        raise ValueError("settings_values needs a body that passed check_settings")
    return quiet, max_wait, lines


def message_for(error: Mapping[str, object]) -> tuple[str, str] | None:
    """(dotted config key, message) for one of the config model's errors (an item of
    ``ValidationError.errors()``) on a setting the Settings view edits, worded by
    MESSAGES; None for any other error. The quiet time in the max-wait message comes
    from the validated model (MaxBelowQuiet), never from the input."""
    raw_loc = error.get("loc", ())
    loc = tuple(str(part) for part in raw_loc) if isinstance(raw_loc, tuple) else ()
    ctx = error.get("ctx")
    cause = ctx.get("error") if isinstance(ctx, Mapping) else None
    if loc == ("debounce",) and isinstance(cause, MaxBelowQuiet):
        message = MESSAGES["seconds_at_least_quiet"].format(quiet=_plain(cause.quiet))
        return "debounce.max_wait_seconds", message
    if loc in _SECONDS_LOCS:
        key = "seconds_positive" if error.get("type") == "greater_than" else "seconds_number"
        return ".".join(loc), MESSAGES[key]
    if loc == ("diff", "max_lines"):
        return "diff.max_lines", MESSAGES["lines_range"]
    if loc == ("notify", "heartbeat"):
        return "notify.heartbeat", MESSAGES["time_format"]
    if loc == ("notify", "machine"):
        return "notify.machine", MESSAGES["machine_name"]
    if loc == ("notify", "quiet_hours"):
        key = "time_differs" if isinstance(cause, QuietHoursSame) else "time_format"
        return "notify.quiet_hours", MESSAGES[key]
    return None


def _number(raw: str | float) -> float | None:
    """A number field's value, or None when it isn't a finite number (a bool, null,
    a list or any other non-number type included: never a TypeError)."""
    if isinstance(raw, str):
        text = raw.strip()
        if not _NUMBER.fullmatch(text):
            return None
        value = float(text)
    elif isinstance(raw, int | float) and not isinstance(raw, bool):
        value = float(raw)
    else:
        return None
    return value if math.isfinite(value) else None


def _seconds_error(value: float | None) -> str | None:
    if value is None:
        return MESSAGES["seconds_number"]
    if value <= 0:
        return MESSAGES["seconds_positive"]
    return None


def _lines(raw: str | float) -> int | None:
    """The diff-lines value, or None unless it is a whole number from 0 to 200 (a
    bool, null, a list or any other non-number type is None, never a TypeError)."""
    if isinstance(raw, str):
        text = raw.strip()
        if not _WHOLE.fullmatch(text):
            return None
        value = int(text)
    elif isinstance(raw, bool):
        return None
    elif isinstance(raw, float):
        if not raw.is_integer():
            return None
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    else:
        return None
    return value if 0 <= value <= MAX_LINES else None


def _as_typed(raw: str | float) -> str:
    """A number as the person typed it (the N of "at least the quiet time")."""
    return raw.strip() if isinstance(raw, str) else _plain(float(raw))


def _plain(value: float) -> str:
    """10.0 -> "10", 2.5 -> "2.5"."""
    return str(int(value)) if value.is_integer() else repr(value)


def _window(value: str) -> dict[str, str]:
    """quiet_from / quiet_to problems of "HH:MM-HH:MM": config.QUIET_HOURS is two
    HEARTBEAT_TIME halves around one "-", plus QuietHoursSame."""
    start, _, end = value.partition("-")
    errors: dict[str, str] = {}
    if not HEARTBEAT_TIME.fullmatch(start):
        errors["quiet_from"] = MESSAGES["time_format"]
    if not HEARTBEAT_TIME.fullmatch(end):
        errors["quiet_to"] = MESSAGES["time_format"]
    if not errors and start == end:
        errors["quiet_to"] = MESSAGES["time_differs"]
    return errors


def _pattern_issues(patterns: Sequence[str]) -> list[Issue]:
    """config.check_pattern's message for each bad pattern, without its
    "invalid pattern '<p>': " prefix (the page shows the reason beside the line)."""
    issues: list[Issue] = []
    for line, pattern in enumerate(patterns, start=1):
        message = check_pattern(pattern)
        if message is not None:
            issues.append(Issue(line, message.removeprefix(f"invalid pattern {pattern!r}: ")))
    return issues
