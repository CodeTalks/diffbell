"""Schema v5: the `problems` and `kv` tables (health spec §4.1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from diffbell import store as store_mod
from diffbell.store import BUSY_MESSAGE, SCHEMA_VERSION, MigrationError, Problem, Store

PROBLEM_COLUMNS = [
    "key",
    "severity",
    "project",
    "message",
    "fix",
    "opened_at",
    "notified_open",
    "attempts",
    "next_try_at",
    "last_error",
    "resolved_at",
]


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    return Store(tmp_dir / "state.db")


def columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")]


# The whole of schema v4 (main at b13c863), frozen: the database a 5a daemon upgrades.
V4_SCHEMA = """
CREATE TABLE clocks (
    project TEXT PRIMARY KEY,
    watch_root TEXT NOT NULL,
    relative_root TEXT NOT NULL,
    clock TEXT NOT NULL,
    expr_hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE hashes (
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (project, path)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    telegram_message_id INTEGER,
    batch_id INTEGER,
    held_reason TEXT
);
CREATE TABLE changes (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('created', 'modified', 'deleted')),
    observed_at REAL NOT NULL,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX changes_unflushed ON changes(id) WHERE event_id IS NULL;
CREATE INDEX changes_event ON changes(event_id);
CREATE INDEX events_project_created ON events(project, created_at);
CREATE INDEX events_status_next ON events(status, next_attempt_at);
CREATE TABLE alerts (
    key TEXT PRIMARY KEY,
    last_sent_at REAL NOT NULL
);
CREATE TABLE web_logins (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    secret_hash TEXT
);
CREATE TABLE web_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_used_at REAL NOT NULL
);
CREATE TABLE web_requests (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('restart')),
    created_at REAL NOT NULL
);
CREATE TABLE event_reports (
    event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    branch TEXT,
    reports TEXT NOT NULL,
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    size INTEGER NOT NULL,
    telegram_lines INTEGER NOT NULL,
    telegram_files INTEGER NOT NULL
);
CREATE TABLE batch_reports (
    batch_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    reports TEXT NOT NULL,
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    size INTEGER NOT NULL,
    telegram_lines INTEGER NOT NULL,
    telegram_files INTEGER NOT NULL
);
CREATE TABLE snoozes (
    project TEXT PRIMARY KEY,
    since REAL NOT NULL,
    until REAL
);
CREATE INDEX events_batch ON events(batch_id) WHERE batch_id IS NOT NULL;
"""


def make_v4(path: Path) -> None:
    """A schema v4 database, built from the frozen v4 schema: one event, and one row
    in the old `alerts` table."""
    raw = sqlite3.connect(path)
    raw.executescript(
        V4_SCHEMA
        + """INSERT INTO events (project, created_at, status, next_attempt_at)
             VALUES ('p', 1.0, 'sent', 1.0);
             INSERT INTO alerts VALUES ('watchman-down', 1.0);
             PRAGMA user_version = 4;"""
    )
    raw.close()


def test_a_new_database_has_problems_and_kv(store: Store) -> None:
    db = store._db
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    assert columns(db, "problems") == PROBLEM_COLUMNS
    assert columns(db, "kv") == ["key", "value"]


def test_a_v4_database_gains_the_tables_without_a_rebuild(tmp_dir: Path) -> None:
    path = tmp_dir / "state.db"
    make_v4(path)
    store = Store(path)
    db = store._db
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert columns(db, "problems") == PROBLEM_COLUMNS
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    store.close()


def test_a_problem_round_trips(store: Store) -> None:
    problem = Problem(
        "recrawl:p",
        "warning",
        "p",
        "project p: Watchman recrawled the folder",
        "watchman watch-del '/w/a b' ; watchman watch-project '/w/a b'",
        100.0,
    )
    store.put_problem(problem)
    assert store.get_problem("recrawl:p") == problem
    assert store.get_problem("other") is None


def test_put_replaces_every_column(store: Store) -> None:
    store.put_problem(Problem("watchman", "serious", None, "down", None, 1.0))
    later = Problem(
        "watchman",
        "serious",
        None,
        "still down",
        "watchman version",
        1.0,
        notified_open=True,
        attempts=2,
        next_try_at=99.0,
        last_error="sendMessage: 502",
        resolved_at=50.0,
    )
    store.put_problem(later)
    assert store.get_problem("watchman") == later
    assert store.all_problems() == [later]


def test_problems_are_listed_oldest_first_and_deleted(store: Store) -> None:
    for key, at in (("b", 2.0), ("a", 2.0), ("c", 1.0)):
        store.put_problem(Problem(key, "warning", None, key, None, at))
    assert [p.key for p in store.all_problems()] == ["c", "a", "b"]
    store.delete_problem("a")
    store.delete_problem("absent")  # no error
    assert [p.key for p in store.all_problems()] == ["c", "b"]


def test_an_unknown_severity_is_refused(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.put_problem(Problem("k", "fatal", None, "m", None, 1.0))  # type: ignore[arg-type]


def table_names(path: Path) -> set[str]:
    raw = sqlite3.connect(path)
    try:
        return {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()


def test_every_open_drops_the_old_alerts_table(tmp_dir: Path) -> None:
    path = tmp_dir / "state.db"
    make_v4(path)
    assert "alerts" in table_names(path)
    Store(path).close()
    assert "alerts" not in table_names(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # Rolled back to the old code, which recreates its table (and keeps v5), then
    # upgraded again: the drop is not gated on the version, so it goes again.
    raw.execute("CREATE TABLE alerts (key TEXT PRIMARY KEY, last_sent_at REAL NOT NULL)")
    raw.commit()
    raw.close()
    Store(path).close()
    assert "alerts" not in table_names(path)


def test_a_lock_at_the_alerts_drop_reads_as_busy(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every open drops `alerts`, a write: another process holding the write lock
    then gets the same "database busy" as in the migration, not a raw error."""
    path = tmp_dir / "state.db"
    Store(path).close()
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE alerts (key TEXT PRIMARY KEY, last_sent_at REAL NOT NULL)")
    raw.commit()
    raw.close()
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT", 0.05)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(MigrationError) as caught:
            Store(path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert str(caught.value) == BUSY_MESSAGE
    Store(path).close()
    assert "alerts" not in table_names(path)


def test_a_new_database_has_no_alerts_table(store: Store) -> None:
    tables = {
        r[0] for r in store._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "alerts" not in tables


def test_kv_get_set_and_replace(store: Store) -> None:
    assert store.kv_get("last_heartbeat") is None
    store.kv_set("last_heartbeat", "2026-10-09 09:00")
    store.kv_set("last_heartbeat", "2026-10-10 09:00")
    assert store.kv_get("last_heartbeat") == "2026-10-10 09:00"


def test_problems_survive_a_reopen(tmp_dir: Path) -> None:
    path = tmp_dir / "state.db"
    first = Store(path)
    first.put_problem(Problem("watchman", "serious", None, "down", None, 1.0, notified_open=True))
    first.close()
    again = Store(path)
    found = again.get_problem("watchman")
    assert found is not None and found.notified_open is True
    again.close()
