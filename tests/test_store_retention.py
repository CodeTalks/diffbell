"""Retention, batch retry, Telegram health and reports_state with held events
(notify controls spec §3.2, §5.1, §5.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from diffbell.diffing import FileReport
from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import RETENTION, ClockState, Store, reports_state
from tests.helpers import sync_names

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")
M = ChangeKind.MODIFIED
DAY = 24 * 3600.0


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "p")
    return store


def add_event(store: Store, paths: list[str], now: float = 10.0, project: str = "p") -> int:
    obs = Observation()
    for path in paths:
        obs.upserts[path] = f"sha-{path}"
        obs.changes.append((path, M))
    changes = store.record_observation(project, STATE, obs, now=now)
    event_id = store.create_event(project, changes, [], now=now)
    assert event_id is not None
    return event_id


def with_report(store: Store, event_id: int) -> int:
    store.put_event_report(
        event_id, "main", [FileReport("a", M, lines=("+1",))], telegram_lines=0, telegram_files=0
    )
    return event_id


def test_retention_is_thirty_days() -> None:
    assert RETENTION == 30 * DAY


def test_prune_never_removes_pending_or_held_events(store: Store) -> None:
    pending = with_report(store, add_event(store, ["a"]))
    held = with_report(store, add_event(store, ["b"]))
    sent = with_report(store, add_event(store, ["c"]))
    skipped = with_report(store, add_event(store, ["d"]))
    store.mark_held(held, 253402300799.0, "snooze")
    store.mark_sent(sent, 1)
    store.mark_skipped([skipped])
    assert store.prune(older_than=100.0) == 2  # the sent and the skipped event
    assert store.get_event(pending).status == "pending"
    assert store.get_event(held).status == "held"
    assert [c.path for c in store.event_changes(held)] == ["b"]  # its changes stay
    for gone in (sent, skipped):
        with pytest.raises(KeyError):
            store.get_event(gone)


def test_prune_deletes_old_reports_whatever_the_status(store: Store) -> None:
    """Diffs at rest stay bounded: a still-held event outlives its report."""
    old_held = with_report(store, add_event(store, ["a"], now=10.0))
    young = with_report(store, add_event(store, ["b"], now=500.0))
    store.mark_held(old_held, 253402300799.0, "snooze")
    store.put_batch_report(
        old_held, [FileReport("a", M, lines=("+1",))], telegram_lines=20, telegram_files=1
    )
    store.put_batch_report(
        young, [FileReport("b", M, lines=("+1",))], telegram_lines=20, telegram_files=1
    )
    store.prune(older_than=100.0)
    assert store.get_event(old_held).status == "held"
    assert store.get_event_report(old_held) is None
    assert store.get_batch_report(old_held) is None
    assert store.get_event_report(young) is not None
    assert store.get_batch_report(young) is not None


def test_retrying_a_failed_batch_holds_the_whole_batch_again(store: Store) -> None:
    ids = [add_event(store, [name]) for name in ("a", "b", "c")]
    other = add_event(store, ["d"])
    for event_id in ids:
        store.mark_held(event_id, 100.0, "quiet")
    store.mark_batch_failed(ids, "sendMessage: Forbidden", ids[0], kind="auth", at=0.0)
    store.mark_failed(other, "sendMessage: Forbidden", kind="auth", at=0.0)
    assert store.retry_event(ids[1], now=500.0) is True
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.attempts, e.next_attempt_at, e.held_reason) for e in rows} == {
        ("held", 0, 500.0, "quiet")
    }
    assert [e.id for e in store.due_held(500.0)] == ids  # one catch-up, not three sends
    assert store.due_events(500.0) == []
    assert store.get_event(other).status == "failed"  # not part of the batch
    assert store.retry_event(ids[0], now=600.0) is False  # no longer failed


def test_retrying_a_single_failed_event_is_unchanged(store: Store) -> None:
    event_id = add_event(store, ["a"])
    store.mark_failed(event_id, "boom", kind="bad_request", at=0.0)
    assert store.retry_event(event_id, now=50.0) is True
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.next_attempt_at) == ("pending", 0, 50.0)


def test_telegram_health_counts_a_stuck_catch_up(store: Store) -> None:
    ok = add_event(store, ["a"], now=10.0)
    store.mark_sent(ok, 1)
    held = add_event(store, ["b"], now=20.0)
    store.mark_held(held, 100.0, "snooze")
    assert store.telegram_health() == (10.0, None)  # held, never attempted: fine
    store.mark_batch_retry(
        [held], "sendMessage: network error (ConnectError)", 400.0, kind="retryable", at=0.0
    )
    assert store.telegram_health() == (10.0, "sendMessage: network error (ConnectError)")


@pytest.mark.parametrize(
    ("has_report", "age_days", "expected"),
    [
        (True, 40, "stored"),
        (True, 1, "stored"),
        (False, 31, "pruned"),
        (False, 29, "not_recorded"),
    ],
)
def test_reports_state(has_report: bool, age_days: int, expected: str) -> None:
    now = 100 * DAY
    assert reports_state(has_report, now - age_days * DAY, now) == expected


def test_at_exactly_thirty_days_a_missing_report_was_never_recorded() -> None:
    """prune deletes strictly older reports (created_at < cutoff), so one exactly
    RETENTION old is still kept: missing, it was never recorded."""
    now = 100 * DAY
    assert reports_state(False, now - RETENTION, now) == "not_recorded"


def test_telegram_health_shows_a_stuck_catch_up_older_than_the_last_send(store: Store) -> None:
    """A held event is older than any send made after it: its error still shows."""
    held = add_event(store, ["b"], now=20.0)
    store.mark_held(held, 100.0, "snooze")
    store.mark_batch_retry(
        [held], "sendMessage: network error (ConnectError)", 400.0, kind="retryable", at=0.0
    )
    ok = add_event(store, ["a"], now=30.0)
    store.mark_sent(ok, 1)
    assert store.telegram_health() == (30.0, "sendMessage: network error (ConnectError)")


def test_a_fresh_hold_forgets_an_old_send_error(store: Store) -> None:
    event_id = add_event(store, ["a"], now=20.0)
    store.mark_retry(event_id, "sendMessage: 502", 30.0, kind="retryable", at=0.0)
    store.mark_held(event_id, 100.0, "quiet")
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.last_error) == ("held", 0, None)
    assert store.telegram_health() == (None, None)


def test_prune_keeps_a_batch_with_a_young_member_whole(store: Store) -> None:
    old = with_report(store, add_event(store, ["a"], now=10.0))
    young = add_event(store, ["b"], now=500.0)
    for event_id in (old, young):
        store.mark_held(event_id, 600.0, "quiet")
    store.mark_batch_failed([old, young], "sendMessage: Forbidden", old, kind="auth", at=0.0)
    store.put_batch_report(
        old, [FileReport("a", M, lines=("+1",))], telegram_lines=20, telegram_files=2
    )
    assert store.prune(older_than=100.0) == 0
    assert store.get_event(old).batch_id == old
    assert store.get_batch_report(old) is not None  # a member is kept: so is its report
    assert store.get_event_report(old) is None  # the old event's own report still goes
    assert store.retry_event(young, now=700.0) is True
    assert [e.id for e in store.due_held(700.0)] == [old, young]


def test_prune_deletes_a_batch_whose_members_are_all_old(store: Store) -> None:
    ids = [add_event(store, [name], now=10.0) for name in ("a", "b")]
    for event_id in ids:
        store.mark_held(event_id, 50.0, "quiet")
    store.mark_batch_sent(ids, 7, ids[0])
    store.put_batch_report(
        ids[0], [FileReport("a", M, lines=("+1",))], telegram_lines=20, telegram_files=2
    )
    assert store.prune(older_than=100.0) == 2
    assert store.get_batch_report(ids[0]) is None


def test_retrying_a_failed_batch_clears_its_batch_id(store: Store) -> None:
    ids = [add_event(store, [name]) for name in ("a", "b")]
    for event_id in ids:
        store.mark_held(event_id, 100.0, "quiet")
    store.mark_batch_failed(ids, "sendMessage: Forbidden", ids[0], kind="auth", at=0.0)
    assert store.retry_event(ids[0], now=500.0) is True
    assert {store.get_event(i).batch_id for i in ids} == {None}
