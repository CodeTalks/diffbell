"""Store.activity (Plan 7 spec §2.2): events per project and local day, the last
14 days, in one query; day boundaries are local midnights, DST included."""

from __future__ import annotations

import json
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import ACTIVITY_SQL, ClockState, DayCount, Store, day_bounds
from tests.helpers import sync_names

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")
BERLIN = ZoneInfo("Europe/Berlin")


def at(text: str) -> float:
    """Epoch seconds of a Berlin wall-clock time, "2026-10-25 23:30"."""
    return datetime.fromisoformat(text).replace(tzinfo=BERLIN).timestamp()


# Tuesday 27 Oct 2026, noon in Berlin; Sunday 25 Oct is the 25-hour day (DST ends).
NOW = at("2026-10-27 12:00")


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "web", "api", "removed", "p0", "p1", "p2")
    return store


def add(store: Store, project: str, when: float, status: str = "pending") -> int:
    obs = Observation()
    path = f"f-{when}"
    obs.upserts[path] = "fp"
    obs.changes.append((path, ChangeKind.MODIFIED))
    changes = store.record_observation(project, STATE, obs, now=when)
    event_id = store.create_event(project, changes, [], now=when)
    assert event_id is not None
    if status == "failed":
        store.mark_failed(event_id, "boom", kind="bad_request", at=0.0)
    elif status == "sent":
        store.mark_sent(event_id, 1)
    elif status == "held":
        store.mark_held(event_id, when + 3600, "snooze")
    elif status == "skipped":
        store.mark_skipped([event_id])
    return event_id


def by_day(counts: list[DayCount]) -> dict[str, tuple[int, int]]:
    return {c.day.isoformat(): (c.total, c.failed) for c in counts if c.total}


def test_day_bounds_are_local_midnights_oldest_first_across_dst() -> None:
    bounds = day_bounds(NOW, 14, BERLIN)
    assert len(bounds) == 14
    assert bounds[0][0] == date(2026, 10, 14) and bounds[-1][0] == date(2026, 10, 27)
    for (day, since, until), nxt in pairwise(bounds):
        assert until == nxt[1]  # no gap, no overlap
        assert since == at(f"{day.isoformat()} 00:00")
    lengths = {day.isoformat(): until - since for day, since, until in bounds}
    assert lengths["2026-10-25"] == 25 * 3600  # the clocks went back
    assert lengths["2026-10-24"] == lengths["2026-10-26"] == 24 * 3600


def test_every_project_gets_exactly_14_days_with_the_gaps_filled(store: Store) -> None:
    add(store, "web", at("2026-10-27 09:00"))
    add(store, "web", at("2026-10-20 09:00"), "failed")
    counts = store.activity(NOW, ["web", "api"], tz=BERLIN)
    assert set(counts) == {"web", "api"}
    for name in ("web", "api"):
        assert [c.day for c in counts[name]] == [d for d, _, _ in day_bounds(NOW, 14, BERLIN)]
    assert by_day(counts["web"]) == {"2026-10-20": (1, 1), "2026-10-27": (1, 0)}
    assert by_day(counts["api"]) == {}


def test_no_events_is_all_zero(store: Store) -> None:
    [only] = store.activity(NOW, ["web"], tz=BERLIN).values()
    assert [(c.total, c.failed) for c in only] == [(0, 0)] * 14


def test_every_status_counts_and_failed_is_counted_apart(store: Store) -> None:
    for status in ("pending", "sent", "failed", "failed", "held", "skipped"):
        add(store, "web", at("2026-10-26 10:00"), status)
    counts = store.activity(NOW, ["web"], tz=BERLIN)["web"]
    assert by_day(counts) == {"2026-10-26": (6, 2)}


def test_local_midnight_is_the_boundary(store: Store) -> None:
    add(store, "web", at("2026-10-22 23:59:59"))
    add(store, "web", at("2026-10-23 00:00"))
    add(store, "web", at("2026-10-13 23:59:59"))  # before the window
    add(store, "web", at("2026-10-14 00:00"))  # its first second
    counts = store.activity(NOW, ["web"], tz=BERLIN)["web"]
    assert by_day(counts) == {
        "2026-10-14": (1, 0),
        "2026-10-22": (1, 0),
        "2026-10-23": (1, 0),
    }


def test_the_25_hour_day_keeps_its_last_hour(store: Store) -> None:
    add(store, "web", at("2026-10-25 23:30"))  # the 25th's 25th hour starts at 23:00
    add(store, "web", at("2026-10-25 02:30") + 3600)  # 02:30 again, after the change
    counts = store.activity(NOW, ["web"], tz=BERLIN)["web"]
    assert by_day(counts) == {"2026-10-25": (2, 0)}


def test_unconfigured_projects_are_not_returned(store: Store) -> None:
    add(store, "removed", at("2026-10-27 08:00"))
    counts = store.activity(NOW, ["web"], tz=BERLIN)
    assert set(counts) == {"web"} and by_day(counts["web"]) == {}


def test_activity_is_one_query(store: Store) -> None:
    for i in range(30):
        add(store, f"p{i % 3}", at("2026-10-20 10:00") + i)
    statements: list[str] = []
    store._db.set_trace_callback(statements.append)
    try:
        store.activity(NOW, ["p0", "p1", "p2"], tz=BERLIN)
    finally:
        store._db.set_trace_callback(None)
    assert len(statements) == 1 and statements[0].lstrip().startswith("SELECT")


def test_a_shorter_window(store: Store) -> None:
    add(store, "web", at("2026-10-26 10:00"))
    counts = store.activity(NOW, ["web"], days=2, tz=BERLIN)["web"]
    assert [(c.day.isoformat(), c.total) for c in counts] == [
        ("2026-10-26", 1),
        ("2026-10-27", 0),
    ]


def test_the_query_uses_the_project_index(store: Store) -> None:
    """The configured names reach the SQL, so the planner can seek
    events_project_created per project instead of scanning every event."""
    for i in range(30):
        add(store, f"p{i % 3}", at("2026-10-20 10:00") + i)
    bounds = day_bounds(NOW, 14, BERLIN)
    pairs = json.dumps([[since, until] for _, since, until in bounds])
    ids = [store._ids["p0"], store._ids["p1"]]
    params = (pairs, bounds[0][1], bounds[-1][2], json.dumps(ids))
    plan = " | ".join(
        str(row[-1]) for row in store._db.execute("EXPLAIN QUERY PLAN " + ACTIVITY_SQL, params)
    )
    assert "SEARCH e USING INDEX events_project_created" in plan, plan


def test_unconfigured_projects_never_reach_the_counts(store: Store) -> None:
    add(store, "removed", at("2026-10-27 08:00"))
    add(store, "web", at("2026-10-27 09:00"))
    statements: list[str] = []
    store._db.set_trace_callback(statements.append)
    try:
        counts = store.activity(NOW, ["web"], tz=BERLIN)
    finally:
        store._db.set_trace_callback(None)
    assert by_day(counts["web"]) == {"2026-10-27": (1, 0)}
    # The configured ids are bound into the one statement (the trace shows it
    # expanded): web's, never removed's.
    assert f"json_each('[{store._ids['web']}]')" in statements[0]


@pytest.mark.parametrize("days", [0, -1])
def test_a_window_of_no_days_is_refused(store: Store, days: int) -> None:
    with pytest.raises(ValueError, match="days"):
        store.activity(NOW, ["web"], days=days, tz=BERLIN)
