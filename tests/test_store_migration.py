"""Schema v4: the events table rebuild (notify controls spec §3.2).

A version 3 database is built from a frozen copy of the v3 schema, then opened
with the current Store, which rebuilds `events` with foreign keys off."""

from __future__ import annotations

import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from diffbell import store as store_mod
from diffbell.pausing import HELD_FOREVER
from diffbell.store import (
    SCHEMA_VERSION,
    V4_REBUILD,
    ClockState,
    MigrationError,
    Store,
    _migrate_v4,
    _migrate_v6,
    _rebuild_events_v4,
    project_folder,
)

# The tables of schema v3 (main at da2a70b) that the rebuild touches or relies on.
V3_SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    telegram_message_id INTEGER
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
"""
COUNTED = ("events", "changes", "event_reports")
V6_COUNTED = (*COUNTED, "batch_reports")  # a v3 database has no batch_reports


def make_v3(path: Path, events: int = 3) -> None:
    """A v3 database: `events` rows (ids 1..n, the last one pruned so the sequence
    is ahead of the highest id), two changes and one report per event, and one
    unflushed change."""
    raw = sqlite3.connect(path)
    raw.executescript(V3_SCHEMA)
    for i in range(1, events + 2):
        status = ("sent", "failed", "pending")[(i - 1) % 3]
        raw.execute(
            "INSERT INTO events (project, created_at, status, attempts, next_attempt_at, "
            "last_error, telegram_message_id) VALUES ('p', ?, ?, 1, ?, ?, ?)",
            (float(i), status, float(i), "boom" if status == "failed" else None, 100 + i),
        )
        for name in ("a", "b"):
            raw.execute(
                "INSERT INTO changes (project, path, kind, observed_at, event_id) "
                "VALUES ('p', ?, 'modified', ?, ?)",
                (f"{name}{i}", float(i), i),
            )
        raw.execute(
            "INSERT INTO event_reports VALUES (?, 'main', '[]', 1, 0, 2, 20, 1)",
            (i,),
        )
    raw.execute("DELETE FROM changes WHERE event_id = ?", (events + 1,))
    raw.execute("DELETE FROM event_reports WHERE event_id = ?", (events + 1,))
    raw.execute("DELETE FROM events WHERE id = ?", (events + 1,))  # the newest is gone
    raw.execute(
        "INSERT INTO changes (project, path, kind, observed_at) VALUES ('p', 'u', 'created', 9.0)"
    )
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()


def counts(db: sqlite3.Connection, tables: tuple[str, ...] = COUNTED) -> dict[str, int]:
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def raw_counts(path: Path) -> dict[str, int]:
    raw = sqlite3.connect(path)
    try:
        return counts(raw)
    finally:
        raw.close()


def events_sql(db: sqlite3.Connection) -> str:
    return str(
        db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()[0]
    )


@pytest.fixture
def v3(tmp_dir: Path) -> Path:
    path = tmp_dir / "state.db"
    make_v3(path)
    return path


def test_a_new_database_is_created_at_the_current_version(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    db = store._db
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    assert "'held', 'skipped'" in events_sql(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"snoozes", "batch_reports", "event_reports"} <= tables
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    store.close()


def test_the_rebuild_keeps_every_row_id_link_and_index(v3: Path) -> None:
    before = raw_counts(v3)
    store = Store(v3)
    db = store._db
    assert counts(db) == before == {"events": 3, "changes": 7, "event_reports": 3}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"snoozes", "batch_reports"} <= tables
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert "'held', 'skipped'" in events_sql(db)
    assert {r[1] for r in db.execute("PRAGMA index_list(events)")} >= {"events_project_created"}
    assert {r[1] for r in db.execute("PRAGMA index_list(changes)")} >= {
        "changes_event",
        "changes_unflushed",
    }
    # A known event still resolves: its row, its changes and its report.
    ev = store.get_event(2)
    assert (ev.project, ev.status, ev.last_error, ev.telegram_message_id) == (
        "p",
        "failed",
        "boom",
        102,
    )
    assert [c.path for c in store.event_changes(2)] == ["a2", "b2"]
    report = store.get_event_report(2)
    assert report is not None and report.branch == "main"
    assert [c.path for c in store.unflushed_changes()] == ["u"]
    row = db.execute("SELECT batch_id, held_reason FROM events WHERE id = 2").fetchone()
    assert row == (None, None)
    # Cascades still work after the rename: they point at the new table.
    store.mark_sent(2, 1)
    store.prune(older_than=100.0)
    assert store.event_changes(2) == [] and store.get_event_report(2) is None
    store.close()


def test_ids_are_never_reused_after_the_rebuild(v3: Path) -> None:
    """The sequence was ahead of the highest id (event 4 was deleted): the next
    event is 5, never a second 4."""
    store = Store(v3)
    (change,) = store.unflushed_changes()
    assert store.create_event("p", [change], [], now=10.0) == 5
    store.close()


def test_reopening_a_migrated_database_changes_nothing(v3: Path) -> None:
    Store(v3).close()
    raw = sqlite3.connect(v3)
    sql = events_sql(raw)
    raw.close()
    store = Store(v3)
    assert events_sql(store._db) == sql
    assert counts(store._db) == {"events": 3, "changes": 7, "event_reports": 3}
    store.close()


def test_foreign_keys_pragma_is_a_no_op_inside_a_transaction(v3: Path) -> None:
    """The hazard the migration order guards against."""
    raw = sqlite3.connect(v3, isolation_level=None)
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("BEGIN")
    raw.execute("PRAGMA foreign_keys=OFF")
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    raw.execute("ROLLBACK")
    raw.close()


def test_a_rebuild_with_foreign_keys_left_on_is_detected(v3: Path) -> None:
    """With enforcement on, DROP TABLE events cascades into changes and
    event_reports: the count check catches it and the transaction rolls back."""
    raw = sqlite3.connect(v3, isolation_level=None)
    raw.execute("PRAGMA foreign_keys=ON")
    before = counts(raw)
    raw.execute("BEGIN IMMEDIATE")
    with pytest.raises(MigrationError, match="row counts changed") as caught:
        _rebuild_events_v4(raw)
    assert "'changes': 7" in str(caught.value) and "'changes': 1" in str(caught.value)
    raw.execute("ROLLBACK")
    assert counts(raw) == before
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    raw.close()


def test_a_failure_mid_migration_rolls_back_to_v3(
    v3: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = raw_counts(v3)
    # Fail right after DROP TABLE events, the most destructive point.
    broken = (*V4_REBUILD[:3], "SELECT * FROM no_such_table", *V4_REBUILD[3:])
    monkeypatch.setattr(store_mod, "V4_REBUILD", broken)
    with pytest.raises(MigrationError, match="database migration failed: no such table"):
        Store(v3)
    raw = sqlite3.connect(v3)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    assert "'held'" not in events_sql(raw)
    assert counts(raw) == before
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not tables & {"events_new", "snoozes", "batch_reports"}  # nothing left behind
    raw.close()
    monkeypatch.setattr(store_mod, "V4_REBUILD", V4_REBUILD)
    store = Store(v3)  # retrying later is safe
    assert counts(store._db) == before
    store.close()


def test_a_failed_migration_turns_foreign_keys_back_on(
    v3: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "V4_REBUILD", ("SELECT * FROM no_such_table",))
    raw = sqlite3.connect(v3, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError):
        _migrate_v4(raw)
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not raw.in_transaction
    raw.close()


def test_migrate_v4_with_foreign_keys_on_keeps_every_row(v3: Path) -> None:
    """The connection arrives with foreign keys on (as Store._open has it): the
    migration must turn them off before BEGIN, or DROP TABLE events cascades."""
    raw = sqlite3.connect(v3, isolation_level=None)
    raw.execute("PRAGMA foreign_keys=ON")
    before = counts(raw)
    _migrate_v4(raw)
    assert counts(raw) == before
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 4
    raw.close()


def test_a_concurrent_migration_is_not_redone_under_the_lock(v3: Path) -> None:
    """Another opener migrates between the first column check and BEGIN IMMEDIATE:
    the re-check under the lock must see it and not rebuild `events` again (a second
    rebuild would reset batch_id and held_reason)."""

    class Racing(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "BEGIN IMMEDIATE":
                Store(v3).close()
                other = sqlite3.connect(v3)
                other.execute("UPDATE events SET batch_id = 1 WHERE id = 1")
                other.commit()
                other.close()
            return super().execute(sql, parameters)

    raw = sqlite3.connect(v3, isolation_level=None, factory=Racing)
    raw.execute("PRAGMA foreign_keys=ON")
    _migrate_v4(raw)
    assert raw.execute("SELECT batch_id FROM events WHERE id = 1").fetchone()[0] == 1
    assert counts(raw) == {"events": 3, "changes": 7, "event_reports": 3}
    raw.close()


def test_a_locked_database_reads_as_busy_not_as_a_failed_migration(
    v3: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Apply the parts of _open before the migration (WAL, SCHEMA, the v2 column)
    # by letting a first open fail inside the migration: they stay, v3 stays.
    monkeypatch.setattr(store_mod, "V4_REBUILD", ("SELECT * FROM no_such_table",))
    with pytest.raises(MigrationError):
        Store(v3)
    monkeypatch.setattr(store_mod, "V4_REBUILD", V4_REBUILD)
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT", 0.05)
    holder = sqlite3.connect(v3, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another process is writing
    try:
        with pytest.raises(MigrationError) as caught:
            Store(v3)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert str(caught.value) == "database busy (another diffbell process is using it); retry"
    raw = sqlite3.connect(v3)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    raw.close()
    store = Store(v3)  # retrying once the lock is gone migrates
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.close()


def test_a_lock_before_the_migration_also_reads_as_busy(
    v3: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is held while `journal_mode` / `SCHEMA` run, before the migration
    itself (review fix 8): the same "database busy" message, not a raw error."""
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT", 0.05)
    holder = sqlite3.connect(v3, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(MigrationError) as caught:
            Store(v3)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert str(caught.value) == "database busy (another diffbell process is using it); retry"
    raw = sqlite3.connect(v3)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    raw.close()


def test_the_store_waits_for_a_busy_database() -> None:
    assert store_mod.BUSY_TIMEOUT == 30.0


def index_names(db: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in db.execute("PRAGMA index_list(events)")}


def test_the_events_indexes_exist_on_a_new_database(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    assert {"events_status_next", "events_batch"} <= index_names(store._db)
    store.close()


def test_the_events_indexes_exist_after_the_migration(v3: Path) -> None:
    store = Store(v3)
    assert {"events_project_created", "events_status_next", "events_batch"} <= index_names(
        store._db
    )
    store.close()


def test_schema_v5_runs_on_a_v3_database(v3: Path) -> None:
    """executescript(SCHEMA_V5) runs before the migrations: on v3, every index it
    creates must use columns v3 has (events_batch is v4-only)."""
    raw = sqlite3.connect(v3)
    raw.executescript(store_mod.SCHEMA_V5)
    assert "events_batch" not in index_names(raw)
    raw.close()


def test_a_dangling_change_refuses_the_migration(v3: Path) -> None:
    raw = sqlite3.connect(v3)
    raw.execute(
        "INSERT INTO changes (project, path, kind, observed_at, event_id) "
        "VALUES ('p', 'x', 'created', 1.0, 999)"
    )
    raw.commit()
    raw.close()
    with pytest.raises(MigrationError, match="foreign_key_check"):
        Store(v3)
    raw = sqlite3.connect(v3)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    raw.close()


def test_a_v1_database_is_rebuilt_too(tmp_dir: Path) -> None:
    path = tmp_dir / "state.db"
    make_v3(path)
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE event_reports")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    store = Store(path)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert [c.path for c in store.event_changes(1)] == ["a1", "b1"]
    store.close()


# -- schema v6: stable project ids (stable project ids spec §3) --------------------

DW, SANDBOX, SHOP, NOTES = (
    "/Users/me/projects/diffbell",
    "/Users/me/diffbell-sandbox",
    "/Users/me/projects/shop",
    "/Users/me/notes",
)
# name -> (watch_root, relative_root, clock, updated_at). Each renamed project has a
# clock row under both names; the new name's is the latest. shop and Shop reach
# the same folder through different watch roots.
V5_CLOCKS = {
    "diffbell": (DW, "", "c:old-dw", 100.0),
    "Diffbell": (DW, "", "c:new-dw", 200.0),
    "diffbell-sandbox": (SANDBOX, "", "c:old-sb", 110.0),
    "Diffbell Sandbox": (SANDBOX, "", "c:new-sb", 210.0),
    "shop": ("/Users/me/projects", "shop", "c:old-shop", 120.0),
    "Shop": (SHOP, "", "c:new-shop", 220.0),
    "notes": (NOTES, "", "c:notes", 130.0),
}
QUIET_END = 4102444800.0  # 2100-01-01: a quiet-hours hold still in force

# name -> events; "ghost" has events but no clock row.
V5_EVENTS = {"diffbell": 13, "diffbell-sandbox": 2, "shop": 4, "notes": 1, "ghost": 2}


def make_v5(path: Path) -> None:
    """A v5 database with renamed projects: old names hold the events
    and changes, new names the clock, the hashes and a snooze; an unrelated project;
    a name with events but no clock row; an unflushed change under a new name; and
    the newest event pruned, so the sequence is ahead of the highest id."""
    raw = sqlite3.connect(path)
    raw.executescript(store_mod.SCHEMA_V5)
    for sql in (*store_mod.V4_TABLES, *store_mod.V4_INDEXES):
        raw.execute(sql)
    for name, (root, rel, clock, updated) in V5_CLOCKS.items():
        raw.execute(
            "INSERT INTO clocks VALUES (?, ?, ?, ?, 'h', ?)", (name, root, rel, clock, updated)
        )
        raw.execute("INSERT INTO hashes VALUES (?, 'package.json', ?)", (name, f"sha-{name}"))
    raw.execute("INSERT INTO hashes VALUES ('Diffbell', 'Makefile', 'sha-make')")
    raw.execute("INSERT INTO snoozes VALUES ('Diffbell', 50.0, NULL)")
    raw.execute("INSERT INTO snoozes VALUES ('shop', 60.0, NULL)")  # the old name's
    when = 0.0
    for name, count in V5_EVENTS.items():
        for _ in range(count):
            when += 1.0
            cur = raw.execute(
                "INSERT INTO events (project, created_at, status, next_attempt_at) "
                "VALUES (?, ?, 'sent', ?)",
                (name, when, when),
            )
            raw.execute(
                "INSERT INTO changes (project, path, kind, observed_at, event_id) "
                "VALUES (?, 'package.json', 'modified', ?, ?)",
                (name, when, cur.lastrowid),
            )
            raw.execute(
                "INSERT INTO event_reports VALUES (?, 'main', '[]', 1, 0, 2, 20, 1)",
                (cur.lastrowid,),
            )
    # A catch-up batch (events 1-2, its report keyed on the first), a pending event
    # (15) and two held ones of shop, an old name whose snooze the merge drops: 19
    # held by that snooze (indefinitely), 18 by the quiet hours.
    raw.execute("UPDATE events SET batch_id = 1, held_reason = 'snooze' WHERE id IN (1, 2)")
    raw.execute("INSERT INTO batch_reports VALUES (1, '[]', 2, 0, 2, 20, 2)")
    raw.execute("UPDATE events SET status = 'pending', attempts = 1 WHERE id = 15")
    raw.execute(
        "UPDATE events SET status = 'held', held_reason = 'snooze', next_attempt_at = ? "
        "WHERE id = 19",
        (HELD_FOREVER,),
    )
    raw.execute(
        "UPDATE events SET status = 'held', held_reason = 'quiet', next_attempt_at = ? "
        "WHERE id = 18",
        (QUIET_END,),
    )
    raw.execute(
        "INSERT INTO changes (project, path, kind, observed_at) "
        "VALUES ('Diffbell', 'Makefile', 'created', 99.0)"
    )
    last = raw.execute("SELECT MAX(id) FROM events").fetchone()[0]
    raw.execute("DELETE FROM changes WHERE event_id = ?", (last,))
    raw.execute("DELETE FROM event_reports WHERE event_id = ?", (last,))
    raw.execute("DELETE FROM events WHERE id = ?", (last,))  # ghost keeps one event
    raw.execute("PRAGMA user_version = 5")
    raw.commit()
    raw.close()


@pytest.fixture
def v5(tmp_dir: Path) -> Path:
    path = tmp_dir / "state.db"
    make_v5(path)
    return path


def snapshot(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    raw = sqlite3.connect(path)
    try:
        return {
            "events": raw.execute("SELECT id, project, status FROM events ORDER BY id").fetchall(),
            "changes": raw.execute(
                "SELECT id, project, event_id FROM changes ORDER BY id"
            ).fetchall(),
            "clocks": raw.execute("SELECT * FROM clocks ORDER BY project").fetchall(),
            "hashes": raw.execute("SELECT * FROM hashes ORDER BY project, path").fetchall(),
            "snoozes": raw.execute("SELECT * FROM snoozes ORDER BY project").fetchall(),
            "batch_reports": raw.execute("SELECT * FROM batch_reports").fetchall(),
            "event_state": raw.execute(
                "SELECT id, next_attempt_at, batch_id, held_reason FROM events ORDER BY id"
            ).fetchall(),
        }
    finally:
        raw.close()


def test_v6_gives_each_folder_one_id_named_after_its_latest_clock(v5: Path) -> None:
    store = Store(v5)
    db = store._db
    projects = {
        path: (name, active)
        for path, name, active in db.execute("SELECT path, name, active FROM projects")
    }
    assert projects == {
        DW: ("Diffbell", 0),
        SANDBOX: ("Diffbell Sandbox", 0),
        SHOP: ("Shop", 0),
        NOTES: ("notes", 0),
        None: ("ghost", 0),  # no clock row: its folder is unknown
    }
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()


def test_v6_keeps_every_event_and_change_with_its_id(v5: Path) -> None:
    before = snapshot(v5)
    store = Store(v5)
    db = store._db
    events = db.execute(
        "SELECT e.id, p.path, e.status FROM events e JOIN projects p ON p.id = e.project_id "
        "ORDER BY e.id"
    ).fetchall()
    folder: dict[str, str | None] = {"diffbell": DW, "diffbell-sandbox": SANDBOX}
    folder |= {"shop": SHOP, "notes": NOTES, "ghost": None}
    assert events == [(i, folder[name], status) for i, name, status in before["events"]]
    changes = db.execute(
        "SELECT c.id, p.name, c.event_id FROM changes c JOIN projects p ON p.id = c.project_id "
        "ORDER BY c.id"
    ).fetchall()
    assert [(i, e) for i, _, e in changes] == [(i, e) for i, _, e in before["changes"]]
    assert changes[-1] == (changes[-1][0], "Diffbell", None)  # the unflushed one
    assert counts(db, V6_COUNTED) == {
        "events": 21,
        "changes": 22,
        "event_reports": 21,
        "batch_reports": 1,
    }
    store.close()


def test_v6_keeps_the_latest_names_clock_hashes_and_snooze(v5: Path) -> None:
    store = Store(v5)
    db = store._db
    clocks = db.execute(
        "SELECT p.path, c.watch_root, c.relative_root, c.clock FROM clocks c "
        "JOIN projects p ON p.id = c.project_id ORDER BY p.path"
    ).fetchall()
    assert clocks == [
        (SANDBOX, SANDBOX, "", "c:new-sb"),
        (NOTES, NOTES, "", "c:notes"),
        (DW, DW, "", "c:new-dw"),
        (SHOP, SHOP, "", "c:new-shop"),
    ]
    hashes = db.execute(
        "SELECT p.path, h.path, h.fingerprint FROM hashes h "
        "JOIN projects p ON p.id = h.project_id WHERE p.path = ? ORDER BY h.path",
        (DW,),
    ).fetchall()
    assert hashes == [(DW, "Makefile", "sha-make"), (DW, "package.json", "sha-Diffbell")]
    snoozes = db.execute(
        "SELECT p.name, s.since FROM snoozes s JOIN projects p ON p.id = s.project_id"
    ).fetchall()
    assert snoozes == [("Diffbell", 50.0)]  # shop' snooze went with its clock
    store.close()


def test_after_the_first_sync_the_history_shows_under_the_configured_names(v5: Path) -> None:
    store = Store(v5)
    store.sync_projects(
        [("Diffbell", DW), ("Diffbell Sandbox", SANDBOX), ("Shop", SHOP)], now=1000.0
    )
    totals = {name: store.query_events(project=name)[1] for name in store._ids}
    assert totals == {"Diffbell": 13, "Diffbell Sandbox": 2, "Shop": 4}
    assert store.get_clock("Diffbell") == ClockState(DW, "", "c:new-dw", "h")
    assert store.get_snooze("Diffbell") is not None
    assert [c.project for c in store.unflushed_changes()] == ["Diffbell"]
    feed = {row.event.project for row in store.query_events(limit=200)[0]}
    assert feed == {"Diffbell", "Diffbell Sandbox", "Shop", "notes", "ghost"}
    (change,) = store.unflushed_changes()
    assert store.create_event("Diffbell", [change], [], now=1000.0) == 23  # 22 was pruned
    store.close()


def test_a_name_without_a_clock_takes_its_folder_at_the_first_sync(v5: Path) -> None:
    """A project snoozed before the daemon ever subscribed it has a snooze but no
    clock: its id has no folder until a sync names one, and keeps its snooze."""
    raw = sqlite3.connect(v5)
    raw.execute("INSERT INTO snoozes VALUES ('fresh', 70.0, NULL)")
    raw.commit()
    raw.close()
    fresh = "/Users/me/projects/fresh"
    store = Store(v5)
    (old_id,) = store._db.execute("SELECT id FROM projects WHERE name = 'fresh'").fetchone()
    store.sync_projects([("Diffbell", DW), ("fresh", fresh), ("ghost", NOTES)], now=1000.0)
    assert store.drop_snoozes_except(["Diffbell", "fresh", "ghost"], 1000.0) == []
    assert store._ids["fresh"] == old_id
    snooze = store.get_snooze("fresh")
    assert snooze is not None and snooze.since == 70.0
    # A folder that already has an id keeps it: "ghost"'s folderless id is not taken.
    assert (
        store._ids["ghost"]
        != store._db.execute(
            "SELECT id FROM projects WHERE path IS NULL AND name = 'ghost'"
        ).fetchone()[0]
    )
    store.close()


def test_v6_backs_the_database_up_first(v5: Path) -> None:
    Store(v5).close()
    [backup] = v5.parent.glob("state-pre-v6-*.db")
    assert re.fullmatch(r"state-pre-v6-\d{8}T\d{6}Z\.db", backup.name)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    raw = sqlite3.connect(backup)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
    assert raw.execute("SELECT COUNT(*) FROM events WHERE project = 'diffbell'").fetchone()[0] == 13
    raw.close()


def test_a_new_database_is_not_backed_up(tmp_dir: Path) -> None:
    Store(tmp_dir / "state.db").close()
    Store(tmp_dir / "state.db").close()
    assert list(tmp_dir.glob("*-pre-v6-*")) == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("DELETE FROM changes WHERE event_id IS NULL", "row counts changed"),
        ("UPDATE events SET project_id = 999 WHERE id = 1", "foreign_key_check"),
    ],
)
def test_a_failed_v6_guard_rolls_everything_back(
    v5: Path, monkeypatch: pytest.MonkeyPatch, extra: str, message: str
) -> None:
    before = snapshot(v5)
    monkeypatch.setattr(store_mod, "V6_REBUILD", (*store_mod.V6_REBUILD, extra))
    with pytest.raises(MigrationError, match=message):
        Store(v5)
    raw = sqlite3.connect(v5)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "projects" not in tables and not {t for t in tables if t.endswith("_new")}
    raw.close()
    assert snapshot(v5) == before
    monkeypatch.undo()
    store = Store(v5)  # retrying later is safe
    assert counts(store._db, V6_COUNTED) == {
        "events": 21,
        "changes": 22,
        "event_reports": 21,
        "batch_reports": 1,
    }
    store.close()
    # The first attempt's backup is kept, complete: the retry adds no copy.
    assert len(list(v5.parent.glob("state-pre-v6-*.db"))) == 1
    assert list(v5.parent.glob("*.part")) == []


def test_a_concurrent_v6_migration_is_not_redone_under_the_lock(v5: Path) -> None:
    class Racing(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "BEGIN IMMEDIATE":
                Store(v5).close()  # another opener migrates first
            return super().execute(sql, parameters)

    raw = sqlite3.connect(v5, isolation_level=None, factory=Racing)
    raw.execute("PRAGMA foreign_keys=ON")
    _migrate_v6(raw, now=1.0)
    assert raw.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 5
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 7  # the racing Store went on to v7
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    raw.close()


def test_project_folder_joins_the_relative_root() -> None:
    assert project_folder("/Users/me/projects", "shop") == SHOP
    assert project_folder(SHOP, "") == SHOP


def test_v6_releases_the_events_held_by_a_merged_away_names_snooze(v5: Path) -> None:
    """shop' snooze goes with its clock (Shop is kept): what it held is due now,
    as drop_snoozes_except does for a removed project, never held forever (Task 2b
    D). A quiet-hours hold is not the snooze's and keeps its end."""
    start = time.time()
    store = Store(v5)
    held = {
        int(i): (float(at), str(reason))
        for i, at, reason in store._db.execute(
            "SELECT id, next_attempt_at, held_reason FROM events WHERE status = 'held'"
        )
    }
    assert set(held) == {18, 19}
    assert held[18] == (QUIET_END, "quiet")
    assert start <= held[19][0] <= time.time() and held[19][1] == "snooze"
    store.sync_projects([("Shop", SHOP)], now=time.time())
    assert [e.id for e in store.due_held(time.time())] == [19]
    assert store.get_snooze("Shop") is None
    store.close()


def test_v6_keeps_batches_and_pending_and_held_events(v5: Path) -> None:
    """Task 2b G: the catch-up batch, its report and the open events survive intact."""
    before = snapshot(v5)
    store = Store(v5)
    db = store._db
    assert db.execute("SELECT * FROM batch_reports").fetchall() == before["batch_reports"]
    after = db.execute(
        "SELECT id, next_attempt_at, batch_id, held_reason FROM events ORDER BY id"
    ).fetchall()
    released = {19}  # test_v6_releases_the_events_held_by_a_merged_away_names_snooze
    assert [r for r in after if r[0] not in released] == [
        r for r in before["event_state"] if r[0] not in released
    ]
    assert [(e.id, e.batch_id) for e in reversed(store.recent_events(30)) if e.batch_id] == [
        (1, 1),
        (2, 1),
    ]
    assert store.batch_size(1) == 2
    assert store.get_batch_report(1) is not None
    statuses = dict(db.execute("SELECT id, status FROM events WHERE status != 'sent'"))
    assert statuses == {15: "pending", 18: "held", 19: "held"}
    store.sync_projects([("Diffbell Sandbox", SANDBOX)], now=time.time())
    assert [e.id for e in store.due_events(time.time())] == [15]
    store.close()


@pytest.mark.parametrize("failure", ["connect", "backup"])
def test_a_failed_backup_is_a_migration_error_and_leaves_nothing(
    v5: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Task 2b E: an sqlite3.Error from the backup is a MigrationError like an OSError,
    with no .part file left and the database still at v5."""
    real = sqlite3.connect

    def connect(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if not str(target).endswith(".part"):
            conn: sqlite3.Connection = real(target, *args, **kwargs)
            return conn
        if failure == "connect":
            raise sqlite3.OperationalError("unable to open database file")
        # A read-only target: the backup's first write fails.
        readonly: sqlite3.Connection = real(f"file:{target}?mode=ro", uri=True)
        return readonly

    monkeypatch.setattr(sqlite3, "connect", connect)
    before = snapshot(v5)
    with pytest.raises(MigrationError, match="database backup failed"):
        Store(v5)
    monkeypatch.undo()
    assert [p.name for p in v5.parent.iterdir() if "pre-v6" in p.name] == []
    raw = sqlite3.connect(v5)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
    raw.close()
    assert snapshot(v5) == before
