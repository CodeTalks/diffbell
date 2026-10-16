"""Store additions for the web UI: activity queries, retry, logins, sessions, requests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import SCHEMA_VERSION, ClockState, Store
from tests.helpers import sync_names

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "a", "b")
    return store


def add_event(store: Store, project: str, paths: list[str], now: float) -> int:
    obs = Observation()
    for path in paths:
        obs.upserts[path] = f"sha-{path}"
        obs.changes.append((path, ChangeKind.MODIFIED))
    changes = store.record_observation(project, STATE, obs, now=now)
    event_id = store.create_event(project, changes, [], now=now)
    assert event_id is not None
    return event_id


def test_query_events_newest_first_with_changes_and_total(store: Store) -> None:
    e1 = add_event(store, "a", ["package.json"], 10.0)
    e2 = add_event(store, "b", ["Makefile", ".env.local"], 20.0)
    rows, total = store.query_events()
    assert total == 2
    assert [r.event.id for r in rows] == [e2, e1]
    assert [(c.path, c.kind) for c in rows[0].changes] == [
        ("Makefile", ChangeKind.MODIFIED),
        (".env.local", ChangeKind.MODIFIED),
    ]


def test_query_events_filters_by_project_status_and_time(store: Store) -> None:
    e1 = add_event(store, "a", ["x"], 10.0)
    e2 = add_event(store, "a", ["y"], 20.0)
    e3 = add_event(store, "b", ["z"], 30.0)
    store.mark_failed(e2, "boom", kind="bad_request", at=0.0)
    store.mark_sent(e3, 1)
    assert [r.event.id for r in store.query_events(project="a")[0]] == [e2, e1]
    assert [r.event.id for r in store.query_events(status="failed")[0]] == [e2]
    assert [r.event.id for r in store.query_events(since=20.0)[0]] == [e3, e2]
    assert [r.event.id for r in store.query_events(until=20.0)[0]] == [e1]
    rows, total = store.query_events(project="a", status="pending")
    assert ([r.event.id for r in rows], total) == ([e1], 1)


def test_query_events_pages(store: Store) -> None:
    ids = [add_event(store, "a", [f"f{i}"], float(i)) for i in range(5)]
    rows, total = store.query_events(limit=2, offset=2)
    assert total == 5
    assert [r.event.id for r in rows] == [ids[2], ids[1]]


def test_query_events_before_id_returns_older_rows_newest_first(store: Store) -> None:
    ids = [add_event(store, "a", [f"f{i}"], float(i)) for i in range(5)]
    rows, total = store.query_events(before_id=ids[3], limit=2)
    assert [r.event.id for r in rows] == [ids[2], ids[1]]
    assert total == 5  # the total ignores the cursor
    rows, _ = store.query_events(before_id=ids[0])
    assert rows == []


def test_query_events_before_id_combines_with_filters(store: Store) -> None:
    a1 = add_event(store, "a", ["src/x.json"], 1.0)
    add_event(store, "b", ["src/y.json"], 2.0)
    a2 = add_event(store, "a", ["src/z.json"], 3.0)
    store.mark_failed(a2, "boom", kind="bad_request", at=0.0)
    a3 = add_event(store, "a", ["src/w.json"], 4.0)
    store.mark_failed(a3, "boom", kind="bad_request", at=0.0)
    a4 = add_event(store, "a", ["README"], 5.0)
    rows, total = store.query_events(project="a", q="JSON", before_id=a4)
    assert ([r.event.id for r in rows], total) == ([a3, a2, a1], 3)
    rows, total = store.query_events(project="a", status="failed", before_id=a3)
    assert ([r.event.id for r in rows], total) == ([a2], 2)


def test_query_events_search_is_case_insensitive_substring(store: Store) -> None:
    e1 = add_event(store, "a", ["src/Package.JSON"], 1.0)
    add_event(store, "a", ["Makefile"], 2.0)
    rows, total = store.query_events(q="package.json")
    assert ([r.event.id for r in rows], total) == ([e1], 1)
    # The event's other files are still listed in full.
    assert [c.path for c in rows[0].changes] == ["src/Package.JSON"]


@pytest.mark.parametrize(
    ("q", "expected"), [("%", "100%.txt"), ("_", "a_b"), ("\\", "back\\slash")]
)
def test_query_events_search_matches_wildcards_literally(
    store: Store, q: str, expected: str
) -> None:
    for i, name in enumerate(["100%.txt", "a_b", "back\\slash", "plain"]):
        add_event(store, "a", [name], float(i))
    rows, _ = store.query_events(q=q)
    assert [c.path for r in rows for c in r.changes] == [expected]


def test_retry_event_only_changes_failed_events(store: Store) -> None:
    failed = add_event(store, "a", ["x"], 1.0)
    store.mark_failed(failed, "sendMessage: Unauthorized", kind="auth", at=0.0)
    sent = add_event(store, "a", ["y"], 2.0)
    store.mark_sent(sent, 7)
    pending = add_event(store, "a", ["z"], 3.0)
    assert store.retry_event(failed, now=50.0) is True
    ev = store.get_event(failed)
    assert (ev.status, ev.attempts, ev.next_attempt_at) == ("pending", 0, 50.0)
    assert [e.id for e in store.due_events(now=50.0)] == [failed, pending]
    assert store.retry_event(sent, now=50.0) is False
    assert store.get_event(sent).status == "sent"
    assert store.retry_event(pending, now=50.0) is False
    assert store.retry_event(9999, now=50.0) is False


def test_telegram_health_from_the_outbox(store: Store) -> None:
    assert store.telegram_health() == (None, None)
    ok = add_event(store, "a", ["x"], 10.0)
    store.mark_sent(ok, 1)
    assert store.telegram_health() == (10.0, None)
    old = add_event(store, "a", ["y"], 5.0)
    store.mark_failed(old, "old failure", kind="bad_request", at=0.0)
    assert store.telegram_health() == (10.0, None)  # older than the last success
    retrying = add_event(store, "a", ["z"], 20.0)
    store.mark_retry(retrying, "network error", 99.0, kind="retryable", at=0.0)
    assert store.telegram_health() == (10.0, "network error")
    fresh = add_event(store, "a", ["w"], 30.0)
    store.mark_sent(fresh, 2)
    assert store.telegram_health() == (30.0, None)


def test_login_token_is_single_use_and_expires(store: Store) -> None:
    store.add_login("h1", expires_at=60.0, secret_hash="s")
    assert store.consume_login("h1", now=10.0, secret_hash="s") is True
    assert store.consume_login("h1", now=10.0, secret_hash="s") is False  # deleted on first use
    store.add_login("h2", expires_at=60.0, secret_hash="s")
    assert store.consume_login("h2", now=60.0, secret_hash="s") is False  # expired
    assert store.consume_login("h2", now=0.0, secret_hash="s") is False  # and gone
    assert store.consume_login("unknown", now=0.0, secret_hash="s") is False


def test_login_token_needs_the_matching_secret_hash(store: Store) -> None:
    store.add_login("h1", expires_at=60.0, secret_hash="old")
    assert store.consume_login("h1", now=10.0, secret_hash="new") is False
    assert store.consume_login("h1", now=10.0, secret_hash="old") is False  # consumed


def test_delete_logins_removes_every_pending_login(store: Store) -> None:
    store.add_login("h1", expires_at=60.0, secret_hash="s")
    store.add_login("h2", expires_at=60.0, secret_hash="s")
    assert store.delete_logins() == 2
    assert store.consume_login("h1", now=0.0, secret_hash="s") is False


def test_delete_logins_by_hash_removes_only_that_login(store: Store) -> None:
    store.add_login("h1", expires_at=60.0, secret_hash="s")
    store.add_login("h2", expires_at=60.0, secret_hash="s")
    assert store.delete_logins("h1") == 1
    assert store.consume_login("h1", now=0.0, secret_hash="s") is False
    assert store.consume_login("h2", now=0.0, secret_hash="s") is True


def test_web_logins_without_secret_hash_gain_the_column(tmp_dir: Path) -> None:
    """A v2 database from before tokens were bound to the server secret."""
    db = tmp_dir / "state.db"
    Store(db).close()
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE web_logins")
    raw.execute("CREATE TABLE web_logins (token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL)")
    raw.execute("INSERT INTO web_logins VALUES ('legacy', 1e12)")
    raw.commit()
    raw.close()
    store = Store(db)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # A legacy row is bound to no server: it never redeems.
    assert store.consume_login("legacy", now=0.0, secret_hash="s") is False
    store.add_login("h", expires_at=60.0, secret_hash="s")
    assert store.consume_login("h", now=0.0, secret_hash="s") is True
    store.close()
    Store(db).close()  # reopening is idempotent


def test_sessions_expire_touch_and_delete(store: Store) -> None:
    store.add_session("s1", "csrf1", now=100.0, expires_at=200.0)
    store.add_session("s2", "csrf2", now=100.0, expires_at=200.0)
    session = store.get_session("s1", now=150.0)
    assert session is not None
    assert (session.csrf, session.created_at, session.last_used_at) == ("csrf1", 100.0, 100.0)
    store.touch_session("s1", now=160.0)
    touched = store.get_session("s1", now=161.0)
    assert touched is not None and touched.last_used_at == 160.0
    assert store.get_session("s1", now=200.0) is None  # absolute expiry
    assert store.delete_sessions("s1") == 1
    assert store.get_session("s1", now=150.0) is None
    assert store.get_session("s2", now=150.0) is not None
    store.add_session("s3", "csrf3", now=100.0, expires_at=200.0)
    assert store.delete_sessions() == 2


def test_take_web_requests_returns_young_rows_and_deletes_all(store: Store) -> None:
    store.add_web_request("restart", now=100.0)
    store.add_web_request("restart", now=10.0)  # older than 60 s at consume time
    assert store.take_web_requests(now=120.0) == ["restart"]
    assert store.take_web_requests(now=120.0) == []
    store.add_web_request("restart", now=0.0)
    # Without any prune_web in between, an old row is still ignored.
    assert store.take_web_requests(now=1000.0) == []


def test_web_request_kind_is_checked(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.add_web_request("shutdown", now=0.0)


def test_prune_web_removes_expired_rows_only(store: Store) -> None:
    store.add_login("old", expires_at=10.0, secret_hash="s")
    store.add_login("new", expires_at=500.0, secret_hash="s")
    store.add_session("old", "c", now=0.0, expires_at=10.0)
    store.add_session("new", "c", now=0.0, expires_at=500.0)
    store.add_web_request("restart", now=0.0)
    store.add_web_request("restart", now=90.0)
    store.prune_web(now=100.0)
    db = store._db
    assert [r[0] for r in db.execute("SELECT token_hash FROM web_logins")] == ["new"]
    assert [r[0] for r in db.execute("SELECT token_hash FROM web_sessions")] == ["new"]
    assert [r[0] for r in db.execute("SELECT created_at FROM web_requests")] == [90.0]


def test_v1_database_is_migrated_to_the_current_version(tmp_dir: Path) -> None:
    db = tmp_dir / "state.db"
    v1 = Store(db)
    sync_names(v1, "a")
    event_id = add_event(v1, "a", ["package.json"], 10.0)
    counts = v1.status_counts()
    v1.close()
    raw = sqlite3.connect(db)
    for table in ("web_logins", "web_sessions", "web_requests"):
        raw.execute(f"DROP TABLE {table}")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    store = Store(db)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # The migration keeps the v1 events and their changes.
    rows, total = store.query_events()
    assert total == 1 and [r.event.id for r in rows] == [event_id]
    assert [(c.path, c.kind) for c in rows[0].changes] == [("package.json", ChangeKind.MODIFIED)]
    assert store.status_counts() == counts
    store.add_login("h", expires_at=10.0, secret_hash="s")
    store.add_web_request("restart", now=0.0)
    store.add_session("s", "c", now=0.0, expires_at=10.0)
    store.close()
