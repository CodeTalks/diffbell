"""When a project's notifications are paused (notify controls spec §4.1). Pure.

A snooze (one project, from the web page) and the daily quiet hours (every
project, from the config) both hold notifications. The quiet hours' end steps
real time and checks the local wall clock (DST-safe), so a 23 h or 25 h day and a
clock that goes back out of the window are right.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from diffbell.store import Snooze

# 9999-12-31 23:59:59 UTC: when an indefinite snooze ends.
HELD_FOREVER = 253402300799.0

PauseReason = Literal["snooze", "quiet", "setup"]


@dataclass(frozen=True, slots=True)
class QuietHours:
    """A daily window [start, end) in minutes after local midnight; it crosses
    midnight when end < start."""

    start: int
    end: int

    @classmethod
    def parse(cls, text: str) -> QuietHours:
        """``text`` as config.NotifyConfig validated it ("23:00-08:00")."""
        start, end = (int(part[:2]) * 60 + int(part[3:]) for part in text.split("-"))
        return cls(start, end)


@dataclass(frozen=True, slots=True)
class Pause:
    until: float  # epoch seconds
    reason: PauseReason


def _wall(ts: float, tz: tzinfo | None) -> datetime:
    """The naive local wall-clock time of ``ts`` (``tz`` None: the Mac's zone)."""
    if tz is None:
        return datetime.fromtimestamp(ts)
    return datetime.fromtimestamp(ts, tz).replace(tzinfo=None)


def _inside(quiet: QuietHours, minute: int) -> bool:
    """Whether ``minute`` (after local midnight) is in the window."""
    if quiet.start < quiet.end:
        return quiet.start <= minute < quiet.end
    return minute >= quiet.start or minute < quiet.end  # crosses midnight


def quiet_end(quiet: QuietHours, now: float, tz: tzinfo | None = None) -> float | None:
    """When the quiet window that ``now`` falls in ends, or None outside it.

    Steps real time minute by minute and checks the local wall clock (DST-safe): a
    23 h or 25 h day, an end time a spring-forward skips, and a fall-back that sets
    the clock back out of the window all end where the wall clock leaves it. Zone
    offsets are whole minutes, so minute boundaries are enough."""
    local = _wall(now, tz)
    if not _inside(quiet, local.hour * 60 + local.minute):
        return None
    ts = math.floor(now / 60) * 60 + 60
    for _ in range(27 * 60):
        w = _wall(ts, tz)
        if not _inside(quiet, w.hour * 60 + w.minute):
            return float(ts)
        ts += 60
    return float(ts)  # unreachable: no window is longer than a day plus a DST hour


def pause_until(
    snooze: Snooze | None,
    quiet: QuietHours | None,
    now: float,
    tz: tzinfo | None = None,
    *,
    configured: bool = True,
) -> Pause | None:
    """Whether a project is paused at ``now`` and until when. Telegram not set up
    (``configured`` False, bot onboarding spec §1) holds forever, reason "setup",
    before anything else: saving the bot releases it. Otherwise, with both a snooze
    and the quiet hours, the later end wins; the reason is "snooze" whenever a snooze
    applies. None when nothing does."""
    if not configured:
        return Pause(HELD_FOREVER, "setup")
    snooze_end: float | None = None
    if snooze is not None and snooze.active(now):
        snooze_end = HELD_FOREVER if snooze.until is None else snooze.until
    quiet_until = quiet_end(quiet, now, tz) if quiet is not None else None
    if snooze_end is None and quiet_until is None:
        return None
    until = max(end for end in (snooze_end, quiet_until) if end is not None)
    return Pause(until, "snooze" if snooze_end is not None else "quiet")
