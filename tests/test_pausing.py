"""pause_until and the quiet window (notify controls spec §4.1, §8)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from diffbell.pausing import HELD_FOREVER, Pause, QuietHours, pause_until, quiet_end
from diffbell.store import Snooze

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
NIGHT = QuietHours.parse("23:00-08:00")
LUNCH = QuietHours.parse("12:00-14:00")


def at(tz: ZoneInfo, year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


def utc(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return at(ZoneInfo("UTC"), year, month, day, hour, minute)


def test_parse() -> None:
    assert QuietHours.parse("23:00-08:00") == QuietHours(23 * 60, 8 * 60)
    assert QuietHours.parse("00:05-23:59") == QuietHours(5, 23 * 60 + 59)


@pytest.mark.parametrize(
    ("hour", "minute", "end"),
    [
        (22, 59, None),  # before the window
        (23, 0, (2026, 1, 16, 8, 0)),  # the start is inside: [start, end)
        (23, 30, (2026, 1, 16, 8, 0)),  # past midnight's eve: ends tomorrow
        (3, 0, (2026, 1, 15, 8, 0)),  # after midnight: ends today
        (7, 59, (2026, 1, 15, 8, 0)),
        (8, 0, None),  # the end is outside
        (12, 0, None),
    ],
)
def test_a_window_crossing_midnight(
    hour: int, minute: int, end: tuple[int, int, int, int, int] | None
) -> None:
    now = at(BERLIN, 2026, 1, 15, hour, minute)
    assert quiet_end(NIGHT, now, BERLIN) == (None if end is None else at(BERLIN, *end))


@pytest.mark.parametrize(
    ("hour", "minute", "inside"),
    [(11, 59, False), (12, 0, True), (13, 59, True), (14, 0, False)],
)
def test_a_daytime_window(hour: int, minute: int, inside: bool) -> None:
    now = at(NEW_YORK, 2026, 6, 1, hour, minute)
    expected = at(NEW_YORK, 2026, 6, 1, 14, 0) if inside else None
    assert quiet_end(LUNCH, now, NEW_YORK) == expected


def test_seconds_inside_the_last_minute_still_count() -> None:
    now = at(BERLIN, 2026, 1, 15, 7, 59) + 59.5
    assert quiet_end(NIGHT, now, BERLIN) == at(BERLIN, 2026, 1, 15, 8, 0)


# -- DST, in two zones ------------------------------------------------------------------


def test_an_end_skipped_by_spring_forward_moves_to_the_next_valid_minute_berlin() -> None:
    """2026-03-29: Berlin jumps from 02:00 to 03:00, so 02:30 does not exist."""
    window = QuietHours.parse("22:00-02:30")
    now = at(BERLIN, 2026, 3, 28, 23, 0)
    assert quiet_end(window, now, BERLIN) == utc(2026, 3, 29, 1, 0)  # 03:00 CEST


def test_an_end_skipped_by_spring_forward_moves_to_the_next_valid_minute_new_york() -> None:
    """2026-03-08: New York jumps from 02:00 to 03:00."""
    window = QuietHours.parse("01:00-02:15")
    now = at(NEW_YORK, 2026, 3, 8, 1, 30)
    assert quiet_end(window, now, NEW_YORK) == utc(2026, 3, 8, 7, 0)  # 03:00 EDT


def test_a_window_across_fall_back_is_ten_hours_long_berlin() -> None:
    """2026-10-25: Berlin repeats 02:00-03:00, so 22:00 to 07:00 lasts 10 hours."""
    now = at(BERLIN, 2026, 10, 24, 22, 0)
    end = quiet_end(QuietHours.parse("22:00-07:00"), now, BERLIN)
    assert end == utc(2026, 10, 25, 6, 0)  # 07:00 CET
    assert end is not None and end - now == 10 * 3600


def test_inside_the_repeated_hour_the_window_ends_at_the_second_end_berlin() -> None:
    """2026-10-25: Berlin repeats 02:00-03:00. At the second 02:15 (CET, fold=1) the
    first 02:30 (CEST) has passed: the window 23:00-02:30 ends at the second one."""
    now = datetime(2026, 10, 25, 2, 15, tzinfo=BERLIN, fold=1).timestamp()
    assert now == utc(2026, 10, 25, 1, 15)
    end = quiet_end(QuietHours.parse("23:00-02:30"), now, BERLIN)
    assert end == utc(2026, 10, 25, 1, 30)  # 02:30 CET
    first = datetime(2026, 10, 25, 2, 15, tzinfo=BERLIN).timestamp()  # fold=0, CEST
    assert quiet_end(QuietHours.parse("23:00-02:30"), first, BERLIN) == utc(2026, 10, 25, 0, 30)


def test_a_window_the_fall_back_leaves_ends_when_the_clock_goes_back_berlin() -> None:
    """2026-10-25: at 03:00 CEST Berlin goes back to 02:00 CET, which is before the
    window 02:30-08:00 starts: the window ends then, not at 08:00 (review fix 6)."""
    now = at(BERLIN, 2026, 10, 25, 2, 45)  # fold=0: the first 02:45, CEST
    assert now == utc(2026, 10, 25, 0, 45)
    assert quiet_end(QuietHours.parse("02:30-08:00"), now, BERLIN) == utc(2026, 10, 25, 1, 0)


def test_a_window_the_fall_back_leaves_at_midnight_santiago() -> None:
    """2026-04-05: at 00:00 -03 Santiago goes back to 23:00 -04 (Apr 4), before the
    window 23:30-00:30 starts: the window ends then, not at 00:30 (review fix 6)."""
    santiago = ZoneInfo("America/Santiago")
    now = at(santiago, 2026, 4, 4, 23, 45)  # fold=0: the first 23:45, -03
    assert now == utc(2026, 4, 5, 2, 45)
    assert quiet_end(QuietHours.parse("23:30-00:30"), now, santiago) == utc(2026, 4, 5, 3, 0)


def test_a_window_across_fall_back_new_york() -> None:
    """2026-11-01: New York repeats 01:00-02:00."""
    now = at(NEW_YORK, 2026, 11, 1, 0, 30)
    end = quiet_end(QuietHours.parse("00:00-06:00"), now, NEW_YORK)
    assert end == utc(2026, 11, 1, 11, 0)  # 06:00 EST


def test_spring_forward_day_is_23_hours_new_york() -> None:
    now = at(NEW_YORK, 2026, 3, 7, 23, 30)
    end = quiet_end(NIGHT, now, NEW_YORK)
    assert end == at(NEW_YORK, 2026, 3, 8, 8, 0)
    assert end is not None and end - now == 7.5 * 3600


# -- pause_until --------------------------------------------------------------------------


def test_nothing_applies() -> None:
    assert pause_until(None, None, 1000.0) is None
    assert pause_until(Snooze("p", 0.0, 500.0), None, 1000.0) is None  # expired
    assert pause_until(None, NIGHT, at(BERLIN, 2026, 1, 15, 12, 0), BERLIN) is None


def test_a_snooze_alone() -> None:
    assert pause_until(Snooze("p", 0.0, 2000.0), None, 1000.0) == Pause(2000.0, "snooze")
    assert pause_until(Snooze("p", 0.0, 2000.0), None, 2000.0) is None


def test_an_indefinite_snooze_is_held_forever() -> None:
    assert pause_until(Snooze("p", 0.0, None), None, 1000.0) == Pause(HELD_FOREVER, "snooze")
    assert datetime.fromtimestamp(HELD_FOREVER, UTC) == datetime(
        9999, 12, 31, 23, 59, 59, tzinfo=UTC
    )


def test_quiet_hours_alone() -> None:
    now = at(BERLIN, 2026, 1, 15, 23, 30)
    assert pause_until(None, NIGHT, now, BERLIN) == Pause(at(BERLIN, 2026, 1, 16, 8, 0), "quiet")


@pytest.mark.parametrize("snooze_hours", [1, 20])
def test_snooze_and_quiet_hours_the_later_end_wins(snooze_hours: int) -> None:
    now = at(BERLIN, 2026, 1, 15, 23, 30)
    snooze = Snooze("p", now, now + snooze_hours * 3600)
    quiet = at(BERLIN, 2026, 1, 16, 8, 0)
    pause = pause_until(snooze, NIGHT, now, BERLIN)
    assert pause == Pause(max(quiet, now + snooze_hours * 3600), "snooze")


def test_the_local_zone_is_used_by_default() -> None:
    now = datetime(2026, 1, 15, 23, 30).timestamp()  # naive: the Mac's own zone
    assert quiet_end(NIGHT, now) == datetime(2026, 1, 16, 8, 0).timestamp()


# -- not set up (bot onboarding spec §1) ----------------------------------------------


def test_not_set_up_holds_forever_before_a_snooze_or_quiet_hours() -> None:
    now = at(BERLIN, 2026, 1, 15, 23, 30)
    snooze = Snooze("p", now, now + 3600)
    expected = Pause(HELD_FOREVER, "setup")
    assert pause_until(None, None, now, BERLIN, configured=False) == expected
    assert pause_until(snooze, NIGHT, now, BERLIN, configured=False) == expected


def test_configured_is_the_default() -> None:
    assert pause_until(None, None, 1000.0) is None
    assert pause_until(None, None, 1000.0, configured=True) is None
