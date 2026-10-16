"""Store support for notification controls (notify controls spec §3.2): snoozes,
held and skipped events, catch-up batches and their stored reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from diffbell.diffing import FileReport
from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import ClockState, Snooze, Store, encode_reports
from tests.helpers import sync_names

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")
M, C, D = ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED
FOREVER = 253402300799.0


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "p", "q", "old-name", "kept")
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


# -- snoozes -----------------------------------------------------------------------


def test_snooze_set_get_replace_and_expiry(store: Store) -> None:
    assert store.get_snooze("p") is None
    assert store.set_snooze("p", 100.0, 200.0) == Snooze("p", 100.0, 200.0)
    assert store.get_snooze("p") == Snooze("p", 100.0, 200.0)
    store.set_snooze("p", 150.0, None)  # replaced: now indefinite
    assert store.get_snooze("p") == Snooze("p", 150.0, None)
    store.set_snooze("q", 100.0, 120.0)
    assert set(store.active_snoozes(110.0)) == {"p", "q"}
    assert set(store.active_snoozes(120.0)) == {"p"}  # until is exclusive
    assert Snooze("q", 100.0, 120.0).active(119.9) is True
    assert Snooze("q", 100.0, None).active(1e12) is True


def test_unsnoozing_makes_the_snooze_held_events_due(store: Store) -> None:
    snoozed = add_event(store, ["a"])
    quiet = add_event(store, ["b"])
    other = add_event(store, ["c"], project="q")
    store.set_snooze("p", 10.0, None)
    store.mark_held(snoozed, FOREVER, "snooze")
    store.mark_held(quiet, 500.0, "quiet")
    store.mark_held(other, FOREVER, "snooze")
    store.delete_snooze("p", now=50.0)
    assert store.get_snooze("p") is None
    assert [e.id for e in store.due_held(50.0)] == [snoozed]
    assert store.get_event(quiet).next_attempt_at == 500.0
    assert store.get_event(other).next_attempt_at == FOREVER
    store.delete_snooze("p", now=60.0)  # idempotent


def test_a_new_snooze_re_evaluates_the_events_it_holds(store: Store) -> None:
    """Shortening an indefinite snooze: its held events are due at once, and the
    catch-up check holds them again until the new end."""
    held = add_event(store, ["a"])
    store.set_snooze("p", 10.0, None)
    store.mark_held(held, FOREVER, "snooze")
    store.set_snooze("p", 20.0, 3600.0)
    assert [e.id for e in store.due_held(20.0)] == [held]


def test_snoozes_of_projects_no_longer_configured_are_dropped(store: Store) -> None:
    held = add_event(store, ["a"], project="old-name")
    store.set_snooze("old-name", 10.0, None)
    store.set_snooze("kept", 10.0, None)
    store.mark_held(held, FOREVER, "snooze")
    assert store.drop_snoozes_except(["kept", "other"], now=30.0) == ["old-name"]
    assert store.get_snooze("old-name") is None and store.get_snooze("kept") is not None
    assert [e.id for e in store.due_held(30.0)] == [held]
    assert store.drop_snoozes_except(["kept"], now=40.0) == []


# -- held and skipped ------------------------------------------------------------------


def test_held_events_carry_their_end_and_reason(store: Store) -> None:
    event_id = add_event(store, ["a"])
    store.mark_held(event_id, 500.0, "quiet")
    ev = store.get_event(event_id)
    assert (ev.status, ev.next_attempt_at, ev.held_reason, ev.batch_id) == (
        "held",
        500.0,
        "quiet",
        None,
    )
    assert store.due_events(now=1e12) == []  # never picked up by the retry loop
    assert store.due_held(499.0) == [] and [e.id for e in store.due_held(500.0)] == [event_id]
    store.mark_batch_retry(
        [event_id], "sendMessage: 502", 600.0, kind="retryable", at=0.0
    )  # a failed catch-up
    store.reschedule_held([event_id], 900.0, "snooze")
    ev = store.get_event(event_id)
    # Held anew: the earlier attempts and error no longer apply.
    assert (ev.next_attempt_at, ev.held_reason, ev.attempts, ev.last_error) == (
        900.0,
        "snooze",
        0,
        None,
    )
    assert store.status_counts()["held"] == 1


def test_release_held_by_reason_and_project(store: Store) -> None:
    a = add_event(store, ["a"])
    b = add_event(store, ["b"], project="q")
    c = add_event(store, ["c"])
    store.mark_held(a, 500.0, "quiet")
    store.mark_held(b, 500.0, "quiet")
    store.mark_held(c, FOREVER, "snooze")
    assert store.release_held(20.0, reason="quiet", project="q") == 1
    assert [e.id for e in store.due_held(20.0)] == [b]
    assert store.release_held(30.0, reason="quiet") == 2
    assert [e.id for e in store.due_held(30.0)] == [a, b]
    assert store.release_held(40.0) == 3  # no reason: every held event
    assert [e.id for e in store.due_held(40.0)] == [a, b, c]


def test_skipped_events_are_recorded(store: Store) -> None:
    a = add_event(store, ["a"])
    b = add_event(store, ["b"])
    store.mark_skipped([a, b])
    assert {store.get_event(a).status, store.get_event(b).status} == {"skipped"}
    assert store.status_counts()["skipped"] == 2
    rows, total = store.query_events(status="skipped")
    assert total == 2 and [r.event.id for r in rows] == [b, a]


# -- catch-up batches ------------------------------------------------------------------


def test_a_batch_is_sent_with_one_message_id(store: Store) -> None:
    ids = [add_event(store, [name]) for name in ("a", "b", "c")]
    for event_id in ids:
        store.mark_held(event_id, 100.0, "snooze")
    store.mark_batch_sent(ids, 77, ids[0])
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.telegram_message_id, e.batch_id) for e in rows} == {("sent", 77, ids[0])}
    assert store.batch_size(ids[0]) == 3
    assert store.batch_size(ids[1]) == 0


def test_a_retryable_batch_stays_held_and_a_failed_one_fails_together(store: Store) -> None:
    ids = [add_event(store, [name]) for name in ("a", "b")]
    for event_id in ids:
        store.mark_held(event_id, 100.0, "quiet")
    store.mark_batch_retry(ids, "sendMessage: 502", 400.0, kind="retryable", at=0.0)
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.attempts, e.last_error, e.next_attempt_at) for e in rows} == {
        ("held", 1, "sendMessage: 502", 400.0)
    }
    store.mark_batch_failed(ids, "sendMessage: Forbidden", ids[0], kind="auth", at=0.0)
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.attempts, e.batch_id, e.held_reason) for e in rows} == {
        ("failed", 2, ids[0], "quiet")
    }


def test_batch_report_round_trip(store: Store) -> None:
    batch = add_event(store, ["a"])
    reports = [
        FileReport("package.json", M, lines=("-1", "+2", "+3"), note="vs HEAD"),
        FileReport(".env", M, lines=("+SECRET",), redacted=True),
    ]
    store.put_batch_report(batch, reports, telegram_lines=20, telegram_files=2)
    stored = store.get_batch_report(batch)
    assert stored is not None
    assert (stored.branch, stored.added, stored.removed) == (None, 2, 1)
    assert (stored.telegram_lines, stored.telegram_files) == (20, 2)
    assert stored.reports[1]["lines"] == []  # redacted: never stored
    store.set_batch_report_telegram(batch, lines=10, files=1)
    again = store.get_batch_report(batch)
    assert again is not None and (again.telegram_lines, again.telegram_files) == (10, 1)
    assert store.get_batch_report(9999) is None


def test_query_rows_carry_batch_and_hold_fields(store: Store) -> None:
    event_id = add_event(store, ["a"])
    store.mark_held(event_id, 300.0, "snooze")
    (row,), _ = store.query_events(status="held")
    assert (row.event.held_reason, row.event.next_attempt_at, row.event.batch_id) == (
        "snooze",
        300.0,
        None,
    )


# -- the rule_skipped flag ----------------------------------------------------------


def test_rule_skipped_is_flagged_only_where_set() -> None:
    reports = [FileReport("a", C, lines=("+1",)), FileReport("b", D)]
    plain = json.loads(encode_reports(reports).text)
    assert all("rule_skipped" not in entry for entry in plain)  # v3 format unchanged
    flagged = json.loads(encode_reports(reports, rule_skipped={"b"}).text)
    assert "rule_skipped" not in flagged[0]
    assert flagged[1]["rule_skipped"] is True
    stub = json.loads(encode_reports(reports, max_bytes=10, rule_skipped={"b"}).text)
    assert stub[1]["truncated"] is True and stub[1]["rule_skipped"] is True


def test_put_event_report_stores_the_flag(store: Store) -> None:
    event_id = add_event(store, ["a", "b"])
    reports = [FileReport("a", C, lines=("+1",)), FileReport("b", D)]
    store.put_event_report(
        event_id, "main", reports, telegram_lines=20, telegram_files=1, rule_skipped=["b"]
    )
    stored = store.get_event_report(event_id)
    assert stored is not None and [e.get("rule_skipped") for e in stored.reports] == [None, True]


# -- transitions only act on the status they expect --------------------------------------


def test_held_and_skipped_only_apply_to_pending_events(store: Store) -> None:
    sent = add_event(store, ["a"])
    store.mark_sent(sent, 5)
    store.mark_held(sent, 100.0, "snooze")
    store.mark_skipped([sent])
    ev = store.get_event(sent)
    assert (ev.status, ev.held_reason, ev.telegram_message_id) == ("sent", None, 5)


def test_batch_transitions_only_apply_to_held_events(store: Store) -> None:
    pending = add_event(store, ["a"])
    sent = add_event(store, ["b"])
    store.mark_sent(sent, 5)
    store.mark_batch_retry([pending, sent], "sendMessage: 502", 400.0, kind="retryable", at=0.0)
    store.mark_batch_sent([pending, sent], 9, pending)
    store.mark_batch_failed([pending, sent], "sendMessage: Forbidden", pending, kind="auth", at=0.0)
    p, s = store.get_event(pending), store.get_event(sent)
    assert (p.status, p.attempts, p.last_error, p.batch_id) == ("pending", 0, None, None)
    assert (s.status, s.attempts, s.telegram_message_id, s.batch_id) == ("sent", 0, 5, None)


def test_a_held_event_can_be_skipped(store: Store) -> None:
    """A catch-up the current notify_on rules empty skips its held events."""
    held = add_event(store, ["a"])
    store.mark_held(held, 100.0, "snooze")
    store.mark_skipped([held])
    assert store.get_event(held).status == "skipped"


def test_a_batch_is_marked_sent_only_from_held(store: Store) -> None:
    """The plain-text fallback runs while the group is still held (review fix 10),
    so mark_batch_sent only ever acts on held events: a failed one is untouched."""
    a, b, other = add_event(store, ["a"]), add_event(store, ["b"]), add_event(store, ["c"])
    for event_id in (a, b, other):
        store.mark_held(event_id, 100.0, "snooze")
    store.mark_batch_failed([other], "can't parse entities", other, kind="bad_request", at=0.0)
    store.mark_batch_sent([a, b, other], 9, a, note="plain-text fallback after: x")
    rows = [store.get_event(i) for i in (a, b)]
    assert {(e.status, e.telegram_message_id, e.batch_id, e.last_error) for e in rows} == {
        ("sent", 9, a, "plain-text fallback after: x")
    }
    assert store.get_event(other).status == "failed"  # not held: untouched


def test_skipping_clears_the_error_state(store: Store) -> None:
    """A pending event that failed retryably, then a rule skipped (review fix 3):
    it was never going to be sent, so it carries no error and no attempts."""
    event_id = add_event(store, ["a"])
    store.mark_retry(event_id, "sendMessage: 502", 50.0, kind="retryable", at=0.0)
    assert store.get_event(event_id).attempts == 1
    store.mark_skipped([event_id])
    ev = store.get_event(event_id)
    assert (ev.status, ev.last_error, ev.attempts) == ("skipped", None, 0)
