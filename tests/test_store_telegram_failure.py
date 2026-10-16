"""Store.telegram_failure: the Settings card's failing state, from the outbox
(settings redesign spec §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import TELEGRAM_SAVED_AT, ClockState, Store, TelegramFailure
from tests.helpers import sync_names

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


def test_nothing_failing_is_none(store: Store) -> None:
    assert store.telegram_failure() is None
    store.mark_sent(add_event(store, "a", 1.0), 1)
    assert store.telegram_failure() is None


def test_retrying_sends_are_retryable_since_the_oldest(store: Store) -> None:
    first, second = add_event(store, "a", 10.0), add_event(store, "b", 20.0)
    store.mark_retry(
        first, "sendMessage: network error (ConnectError)", 50.0, kind="retryable", at=40.0
    )
    store.mark_retry(second, "sendMessage: 502", 60.0, kind="retryable", at=45.0)
    assert store.telegram_failure() == TelegramFailure("retryable", 10.0, 0, 2, "sendMessage: 502")


def test_a_stuck_catch_up_counts_as_waiting_even_after_a_send(store: Store) -> None:
    """The send (event created at 25) is older than the catch-up's error (30)."""
    a, b = held(store, "a", 10.0), held(store, "b", 20.0)
    store.mark_batch_retry([a, b], "sendMessage: 502", 400.0, kind="retryable", at=30.0)
    store.mark_sent(add_event(store, "c", 25.0), 1)
    assert store.telegram_failure() == TelegramFailure("retryable", 10.0, 0, 2, "sendMessage: 502")


def test_none_once_a_send_is_newer_than_the_failure(store: Store) -> None:
    store.mark_failed(
        add_event(store, "a", 10.0), "sendMessage: Unauthorized", kind="auth", at=11.0
    )
    assert store.telegram_failure() is not None
    store.mark_sent(add_event(store, "b", 20.0), 1)
    assert store.telegram_failure() is None


def test_auth_outranks_a_newer_retryable_error(store: Store) -> None:
    store.mark_failed(
        add_event(store, "a", 10.0), "sendMessage: Unauthorized", kind="auth", at=11.0
    )
    store.mark_retry(
        add_event(store, "b", 20.0), "sendMessage: 502", 50.0, kind="retryable", at=21.0
    )
    assert store.telegram_failure() == TelegramFailure(
        "auth", 10.0, 1, 1, "sendMessage: Unauthorized"
    )


def test_the_error_is_the_newest_text_of_the_winning_kind(store: Store) -> None:
    for now, text in ((10.0, "sendMessage: Unauthorized"), (20.0, "sendMessage: Forbidden")):
        store.mark_failed(add_event(store, f"f{now}", now), text, kind="auth", at=now + 1)
    store.mark_retry(
        add_event(store, "r", 30.0), "sendMessage: 502", 50.0, kind="retryable", at=31.0
    )
    failure = store.telegram_failure()
    assert failure is not None
    assert (failure.kind, failure.error, failure.failed, failure.waiting) == (
        "auth",
        "sendMessage: Forbidden",
        2,
        1,
    )


def test_bad_request_outranks_retryable(store: Store) -> None:
    text = "sendMessage: Bad Request: can't parse entities"
    store.mark_failed(add_event(store, "a", 10.0), text, kind="bad_request", at=11.0)
    store.mark_retry(
        add_event(store, "b", 20.0), "sendMessage: 502", 50.0, kind="retryable", at=21.0
    )
    assert store.telegram_failure() == TelegramFailure("bad_request", 10.0, 1, 1, text)


def test_a_legacy_failed_row_is_terminal_and_never_keeps_retrying(store: Store) -> None:
    """Spec §9: a NULL-kind failed row (written before v7) plus a newer retryable one
    is the legacy 4b variant, never "keeps retrying"."""
    store.mark_failed(add_event(store, "a", 10.0), "boom", kind=None, at=11.0)
    store.mark_retry(
        add_event(store, "b", 20.0), "sendMessage: 502", 50.0, kind="retryable", at=21.0
    )
    assert store.telegram_failure() == TelegramFailure("legacy_failed", 10.0, 1, 1, "boom")


def test_a_legacy_retrying_row_is_retryable(store: Store) -> None:
    store.mark_retry(add_event(store, "a", 10.0), "sendMessage: 502", 50.0, kind=None, at=11.0)
    assert store.telegram_failure() == TelegramFailure("retryable", 10.0, 0, 1, "sendMessage: 502")


def test_an_unknown_stored_kind_reads_like_no_kind(store: Store) -> None:
    store.mark_failed(add_event(store, "a", 10.0), "boom", kind="not_set_up", at=11.0)
    failure = store.telegram_failure()
    assert failure is not None and failure.kind == "legacy_failed"


@pytest.mark.parametrize(
    ("first", "second", "winner"),
    [("legacy", "bad_request", "bad_request"), ("bad_request", "legacy", "legacy_failed")],
)
def test_bad_request_and_legacy_rank_equal_and_the_newest_wins(
    store: Store, first: str, second: str, winner: str
) -> None:
    for now, which in ((10.0, first), (20.0, second)):
        kind = None if which == "legacy" else "bad_request"
        store.mark_failed(add_event(store, which, now), which, kind=kind, at=now + 1)
    failure = store.telegram_failure()
    assert failure is not None
    assert (failure.kind, failure.error, failure.failed) == (winner, second, 2)


def test_requeued_and_never_tried_events_are_not_in_the_backlog(store: Store) -> None:
    requeued = add_event(store, "a", 10.0)
    store.mark_failed(requeued, "sendMessage: Unauthorized", kind="auth", at=11.0)
    assert store.retry_event(requeued, now=15.0)
    held(store, "b", 12.0)
    current = add_event(store, "c", 20.0)
    store.mark_retry(current, "sendMessage: 502", 50.0, kind="retryable", at=21.0)
    assert store.telegram_failure() == TelegramFailure("retryable", 20.0, 0, 1, "sendMessage: 502")


def test_a_save_after_the_failure_hides_it(store: Store) -> None:
    """Replace bot: an error recorded before the save is not the new bot's."""
    store.mark_failed(
        add_event(store, "a", 10.0), "sendMessage: Unauthorized", kind="auth", at=11.0
    )
    store.kv_set(TELEGRAM_SAVED_AT, repr(12.0))
    assert store.telegram_failure() is None


def test_a_failure_after_the_save_counts_alone(store: Store) -> None:
    store.mark_failed(
        add_event(store, "a", 10.0), "sendMessage: Unauthorized", kind="auth", at=11.0
    )
    store.kv_set(TELEGRAM_SAVED_AT, repr(12.0))
    store.mark_retry(
        add_event(store, "b", 20.0), "sendMessage: 502", 50.0, kind="retryable", at=21.0
    )
    assert store.telegram_failure() == TelegramFailure("retryable", 20.0, 0, 1, "sendMessage: 502")


def test_with_a_save_a_pre_v7_error_counts_as_older(store: Store) -> None:
    event_id = add_event(store, "a", 10.0)
    store.mark_failed(event_id, "boom", kind="auth", at=11.0)
    store._db.execute("UPDATE events SET last_error_at = NULL WHERE id = ?", (event_id,))
    assert store.telegram_failure() is not None  # no save: nothing filtered
    store.kv_set(TELEGRAM_SAVED_AT, repr(5.0))
    assert store.telegram_failure() is None


def test_an_unreadable_saved_at_filters_nothing(store: Store) -> None:
    store.mark_failed(add_event(store, "a", 10.0), "boom", kind="auth", at=11.0)
    store.kv_set(TELEGRAM_SAVED_AT, "not a number")
    assert store.telegram_failure() is not None


def test_a_sent_rows_fallback_note_is_not_a_failure(store: Store) -> None:
    store.mark_sent(add_event(store, "a", 10.0), 7, note="sent as plain text")
    assert store.telegram_failure() is None


def test_the_newest_error_is_by_error_time_not_event_age(store: Store) -> None:
    """An older event retried later carries the newer error text."""
    old, new = add_event(store, "a", 10.0), add_event(store, "b", 20.0)
    store.mark_retry(new, "sendMessage: 502", 60.0, kind="retryable", at=30.0)
    store.mark_retry(
        old, "sendMessage: network error (ConnectError)", 70.0, kind="retryable", at=40.0
    )
    assert store.telegram_failure() == TelegramFailure(
        "retryable", 10.0, 0, 2, "sendMessage: network error (ConnectError)"
    )


def test_a_later_successful_send_makes_older_errors_history(store: Store) -> None:
    """A CLI-set bot writes no save time: the last delivery is the cutoff instead."""
    store.mark_failed(
        add_event(store, "a", 10.0), "sendMessage: Unauthorized", kind="auth", at=11.0
    )
    store.mark_sent(add_event(store, "b", 20.0), 1)
    store.mark_retry(
        add_event(store, "c", 30.0), "sendMessage: 502", 50.0, kind="retryable", at=31.0
    )
    assert store.telegram_failure() == TelegramFailure("retryable", 30.0, 0, 1, "sendMessage: 502")


def test_the_later_of_save_and_send_is_the_cutoff(store: Store) -> None:
    store.mark_failed(
        add_event(store, "a", 10.0), "sendMessage: Unauthorized", kind="auth", at=11.0
    )
    store.mark_sent(add_event(store, "b", 5.0), 1)
    store.kv_set(TELEGRAM_SAVED_AT, repr(12.0))
    store.mark_retry(
        add_event(store, "c", 30.0), "sendMessage: 502", 50.0, kind="retryable", at=31.0
    )
    assert store.telegram_failure() == TelegramFailure("retryable", 30.0, 0, 1, "sendMessage: 502")


def test_telegram_health_drops_the_replaced_bots_errors(store: Store) -> None:
    """The header and the daemon's `telegram` warning follow the card: an error
    recorded before the last bot save is history; a later one counts again."""
    failed = add_event(store, "a", 10.0)
    store.mark_failed(failed, "sendMessage: Unauthorized", kind="auth", at=11.0)
    assert store.telegram_health() == (None, "sendMessage: Unauthorized")
    store.kv_set(TELEGRAM_SAVED_AT, repr(12.0))
    assert store.telegram_health() == (None, None)
    stuck = held(store, "b", 13.0)
    store.mark_batch_retry([stuck], "sendMessage: 502", 400.0, kind="retryable", at=14.0)
    assert store.telegram_health() == (None, "sendMessage: 502")
