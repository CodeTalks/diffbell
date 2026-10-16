from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.store import ClockState, Store
from tests.helpers import sync_names

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")


def open_store(db: Path) -> Store:
    """A Store with the project "p" configured, as the daemon has it after its sync."""
    store = Store(db)
    sync_names(store, "p")
    return store


@pytest.fixture
def db(tmp_dir: Path) -> Path:
    return tmp_dir / "state.db"


def obs_created(*names: str) -> Observation:
    o = Observation()
    for n in names:
        o.upserts[n] = f"sha-{n}"
        o.changes.append((n, ChangeKind.CREATED))
    return o


def test_record_observation_writes_journal_hashes_and_clock(db: Path) -> None:
    store = open_store(db)
    changes = store.record_observation("p", STATE, obs_created("a", "b"), now=10.0)
    assert [c.path for c in changes] == ["a", "b"]
    assert all(c.id > 0 for c in changes)
    assert store.get_hashes("p") == {"a": "sha-a", "b": "sha-b"}
    assert store.get_clock("p") == STATE
    store.close()


def test_unflushed_changes_survive_restart(db: Path) -> None:
    store = open_store(db)
    store.record_observation("p", STATE, obs_created("a"), now=1.0)
    store.close()
    reopened = open_store(db)
    assert [(c.project, c.path, c.kind) for c in reopened.unflushed_changes()] == [
        ("p", "a", ChangeKind.CREATED)
    ]
    reopened.close()


def test_failed_transaction_rolls_back_everything(db: Path) -> None:
    store = open_store(db)
    bad = obs_created("a")
    bad.deletes.append("x")
    bad.changes.append(("b", "not-a-kind"))  # type: ignore[arg-type]  # violates CHECK
    with pytest.raises(sqlite3.IntegrityError):
        store.record_observation("p", STATE, bad, now=1.0)
    assert store.get_hashes("p") == {}
    assert store.get_clock("p") is None
    assert store.unflushed_changes() == []
    store.close()


def test_deletes_remove_hash_rows(db: Path) -> None:
    store = open_store(db)
    store.record_observation("p", STATE, obs_created("a"), now=1.0)
    o = Observation(deletes=["a"], changes=[("a", ChangeKind.DELETED)])
    store.record_observation("p", STATE, o, now=2.0)
    assert store.get_hashes("p") == {}
    store.close()


def test_replace_baseline_resets_hashes_without_journal(db: Path) -> None:
    store = open_store(db)
    store.record_observation("p", STATE, obs_created("old"), now=1.0)
    new_state = ClockState("/w", "", "c:2", "h2")
    store.replace_baseline("p", new_state, {"x": "1"}, now=2.0)
    assert store.get_hashes("p") == {"x": "1"}
    assert store.get_clock("p") == new_state
    assert [c.path for c in store.unflushed_changes()] == ["old"]  # journal untouched
    store.close()


def test_create_event_links_kept_and_deletes_superseded(db: Path) -> None:
    store = open_store(db)
    a1, a2 = store.record_observation("p", STATE, obs_created("a", "a2"), now=1.0)
    kept = [Change(a2.id, "p", "a2", ChangeKind.CREATED)]
    event_id = store.create_event("p", kept, [a1.id], now=5.0)
    assert event_id is not None
    assert store.unflushed_changes() == []
    assert store.event_changes(event_id) == kept
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.next_attempt_at) == ("pending", 0, 5.0)
    store.close()


def test_batch_collapsing_to_nothing_deletes_rows_and_creates_no_event(db: Path) -> None:
    store = open_store(db)
    (a,) = store.record_observation("p", STATE, obs_created("a"), now=1.0)
    assert store.create_event("p", [], [a.id], now=2.0) is None
    store.close()
    reopened = open_store(db)
    assert reopened.unflushed_changes() == []  # not resurrected on restart
    reopened.close()


def test_event_status_transitions_and_due(db: Path) -> None:
    store = open_store(db)
    (a,) = store.record_observation("p", STATE, obs_created("a"), now=1.0)
    eid = store.create_event("p", [a], [], now=1.0)
    assert eid is not None
    assert [e.id for e in store.due_events(now=1.0)] == [eid]
    store.mark_retry(eid, "HTTP 500", next_attempt_at=100.0, kind="retryable", at=0.0)
    assert store.due_events(now=50.0) == []
    assert store.get_event(eid).attempts == 1
    store.mark_sent(eid, 42, note="plain-text fallback")
    ev = store.get_event(eid)
    assert (ev.status, ev.telegram_message_id, ev.last_error) == ("sent", 42, "plain-text fallback")
    assert store.status_counts() == {"pending": 0, "sent": 1, "failed": 0}
    store.close()


def test_prune_removes_old_finished_events_and_their_changes(db: Path) -> None:
    store = open_store(db)
    (a,) = store.record_observation("p", STATE, obs_created("a"), now=1.0)
    eid = store.create_event("p", [a], [], now=1.0)
    assert eid is not None
    store.mark_sent(eid, 1)
    assert store.prune(older_than=2.0) == 1
    assert store.status_counts()["sent"] == 0
    assert store.event_changes(eid) == []
    assert store._db.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0
    store.close()


def test_prune_keeps_pending_events_and_ids_are_not_reused(db: Path) -> None:
    store = open_store(db)
    (a, b) = store.record_observation("p", STATE, obs_created("a", "b"), now=1.0)
    e1 = store.create_event("p", [a], [], now=1.0)
    e2 = store.create_event("p", [b], [], now=1.0)
    assert e1 is not None and e2 is not None
    store.mark_sent(e2, 1)
    assert store.prune(older_than=100.0) == 1
    assert store.get_event(e1).status == "pending"
    assert [c.path for c in store.event_changes(e1)] == ["a"]
    (c,) = store.record_observation("p", STATE, obs_created("c"), now=2.0)
    e3 = store.create_event("p", [c], [], now=2.0)
    assert e3 is not None and e3 > e2
    store.close()


def test_create_event_with_missing_kept_id_rolls_back(db: Path) -> None:
    store = open_store(db)
    (a, b) = store.record_observation("p", STATE, obs_created("a", "b"), now=1.0)
    ghost = Change(9999, "p", "ghost", ChangeKind.CREATED)
    with pytest.raises(ValueError, match="journal row 9999 missing"):
        store.create_event("p", [a, ghost], [b.id], now=1.0)
    assert store.status_counts() == {"pending": 0, "sent": 0, "failed": 0}
    assert [c.path for c in store.unflushed_changes()] == ["a", "b"]
    store.close()


def test_create_event_id_in_both_kept_and_superseded_stays_linked(db: Path) -> None:
    store = open_store(db)
    (a,) = store.record_observation("p", STATE, obs_created("a"), now=1.0)
    eid = store.create_event("p", [a], [a.id], now=1.0)
    assert eid is not None
    assert [c.path for c in store.event_changes(eid)] == ["a"]
    store.close()


def test_schema_version_and_event_link_index(db: Path) -> None:
    store = open_store(db)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
    names = {r[1] for r in store._db.execute("PRAGMA index_list(changes)")}
    assert "changes_event" in names
    store.close()


def test_a_v4_database_gains_the_last_change_index(db: Path) -> None:
    """The dashboard's per-project MAX(created_at) index is additive: an existing
    database gets it on the next open, without a version bump."""
    Store(db).close()
    raw = sqlite3.connect(db)
    raw.execute("DROP INDEX events_project_created")
    raw.close()
    store = open_store(db)
    names = {r[1] for r in store._db.execute("PRAGMA index_list(events)")}
    assert "events_project_created" in names
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
    store.close()


def test_user_version_is_set_once_and_never_downgraded(db: Path) -> None:
    Store(db).close()
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 7
    raw.execute("PRAGMA user_version = 8")
    raw.close()
    Store(db).close()
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 8
    raw.close()


class _FailingRollback:
    """Connection proxy whose ROLLBACK raises, as on a dead or broken connection."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.in_transaction = True

    def execute(self, sql: str, *args: object) -> object:
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("cannot rollback")
        return self._inner.execute(sql, *args)  # type: ignore[arg-type]


def test_failing_rollback_does_not_mask_the_original_error(db: Path) -> None:
    store = open_store(db)
    store._db = _FailingRollback(store._db)  # type: ignore[assignment]
    with pytest.raises(ValueError, match="original"), store._tx():
        raise ValueError("original")


def test_a_new_database_is_owner_only(db: Path) -> None:
    store = open_store(db)
    try:
        files = [db, db.with_name("state.db-wal"), db.with_name("state.db-shm")]
        assert {f.name: f.stat().st_mode & 0o777 for f in files} == {f.name: 0o600 for f in files}
    finally:
        store.close()


def test_an_existing_database_is_tightened_to_owner_only(db: Path) -> None:
    """The docs promise 0600: a database (and its -wal/-shm) made world-readable
    by an earlier version is tightened when it is opened."""
    Store(db).close()
    files = [db, db.with_name("state.db-wal"), db.with_name("state.db-shm")]
    for f in files:
        f.touch()
        f.chmod(0o644)
    store = open_store(db)
    try:
        assert {f.name: f.stat().st_mode & 0o777 for f in files} == {f.name: 0o600 for f in files}
    finally:
        store.close()
