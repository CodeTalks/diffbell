"""last_error_kind and last_error_at are written with every last_error and cleared
with it (settings redesign spec §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import ClockState, Store
from tests.helpers import error_at, error_kind, sync_names

STATE = ClockState("/w", "", "c:1", "h")
FOREVER = 4102444800.0


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "p")
    return store


def add_event(store: Store, path: str, now: float) -> int:
    obs = Observation()
    obs.upserts[path] = f"sha-{path}"
    obs.changes.append((path, ChangeKind.MODIFIED))
    changes = store.record_observation("p", STATE, obs, now=now)
    event_id = store.create_event("p", changes, [], now=now)
    assert event_id is not None
    return event_id


def held(store: Store, path: str, now: float) -> int:
    event_id = add_event(store, path, now)
    store.mark_held(event_id, FOREVER, "quiet")
    return event_id


def meta(store: Store, event_id: int) -> tuple[str | None, str | None, float | None]:
    return (
        store.get_event(event_id).last_error,
        error_kind(store, event_id),
        error_at(store, event_id),
    )


def test_a_new_event_has_no_kind_or_time(store: Store) -> None:
    assert meta(store, add_event(store, "a", 1.0)) == (None, None, None)


def test_retry_and_failure_store_their_kind_and_time(store: Store) -> None:
    retried = add_event(store, "a", 1.0)
    store.mark_retry(retried, "sendMessage: 502", 50.0, kind="retryable", at=20.0)
    failed = add_event(store, "b", 2.0)
    store.mark_failed(failed, "sendMessage: Unauthorized", kind="auth", at=21.0)
    assert meta(store, retried) == ("sendMessage: 502", "retryable", 20.0)
    assert meta(store, failed) == ("sendMessage: Unauthorized", "auth", 21.0)


def test_batch_retry_and_failure_store_their_kind_and_time(store: Store) -> None:
    a, b = held(store, "a", 1.0), held(store, "b", 2.0)
    store.mark_batch_retry([a, b], "sendMessage: 502", 400.0, kind="retryable", at=30.0)
    assert [meta(store, i) for i in (a, b)] == [("sendMessage: 502", "retryable", 30.0)] * 2
    store.mark_batch_failed([a, b], "sendMessage: Forbidden", a, kind="auth", at=31.0)
    assert [meta(store, i) for i in (a, b)] == [("sendMessage: Forbidden", "auth", 31.0)] * 2


def test_a_send_clears_kind_and_time_even_with_a_fallback_note(store: Store) -> None:
    event_id = add_event(store, "a", 1.0)
    store.mark_retry(event_id, "sendMessage: 502", 50.0, kind="retryable", at=20.0)
    note = "plain-text fallback after: can't parse entities"
    store.mark_sent(event_id, 7, note=note)
    assert meta(store, event_id) == (note, None, None)


def test_a_batch_send_clears_kind_and_time(store: Store) -> None:
    a = held(store, "a", 1.0)
    store.mark_batch_retry([a], "sendMessage: 502", 400.0, kind="retryable", at=20.0)
    store.mark_batch_sent([a], 9, a, note="plain-text fallback after: bad")
    assert meta(store, a) == ("plain-text fallback after: bad", None, None)


def test_holding_skipping_and_rescheduling_clear_kind_and_time(store: Store) -> None:
    pending = add_event(store, "a", 1.0)
    store.mark_retry(pending, "sendMessage: 502", 50.0, kind="retryable", at=20.0)
    store.mark_held(pending, FOREVER, "snooze")
    skipped = add_event(store, "b", 2.0)
    store.mark_retry(skipped, "sendMessage: 502", 50.0, kind="retryable", at=20.0)
    store.mark_skipped([skipped])
    again = held(store, "c", 3.0)
    store.mark_batch_retry([again], "sendMessage: 502", 400.0, kind="retryable", at=20.0)
    store.reschedule_held([again], FOREVER, "quiet")
    for event_id in (pending, skipped, again):
        assert meta(store, event_id) == (None, None, None), event_id


def test_a_retried_failure_keeps_its_error_kind_and_time(store: Store) -> None:
    """retry_event leaves last_error as it was (the feed shows why it failed), so
    its kind and time stay with it; attempts 0 keeps it out of the failing state."""
    event_id = add_event(store, "a", 1.0)
    store.mark_failed(event_id, "sendMessage: Unauthorized", kind="auth", at=20.0)
    assert store.retry_event(event_id, now=25.0)
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts) == ("pending", 0)
    assert meta(store, event_id) == ("sendMessage: Unauthorized", "auth", 20.0)


def test_no_row_ever_has_a_kind_without_an_error(store: Store) -> None:
    a, b = add_event(store, "a", 1.0), held(store, "b", 2.0)
    store.mark_retry(a, "sendMessage: 502", 50.0, kind="retryable", at=20.0)
    store.mark_batch_retry([b], "sendMessage: 502", 50.0, kind="retryable", at=20.0)
    store.mark_sent(a, 1)
    store.reschedule_held([b], FOREVER, "quiet")
    stray = store._db.execute(
        "SELECT COUNT(*) FROM events WHERE last_error IS NULL "
        "AND (last_error_kind IS NOT NULL OR last_error_at IS NOT NULL)"
    ).fetchone()[0]
    assert stray == 0
