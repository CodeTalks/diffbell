"""Schema v7: events.last_error_kind and events.last_error_at (settings redesign spec
§7). Added nullable columns: ALTER TABLE, never a rebuild, and the older migrations'
templates stay as they were at their version."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from diffbell import store as store_mod
from diffbell import upgrade
from diffbell.store import SCHEMA_VERSION, Store, _migrate_v7
from tests.test_store_migration import make_v5

V7_COLUMNS = ["last_error_kind", "last_error_at"]
_V6_INDEXES = ";\n".join(store_mod.V6_INDEXES)
# Schema v6 as it was (main at 73dff8f), from the frozen templates.
V6_SCHEMA = f"""
{store_mod.PROJECTS_TABLE};
CREATE TABLE IF NOT EXISTS clocks {store_mod.CLOCKS_COLUMNS};
CREATE TABLE IF NOT EXISTS hashes {store_mod.HASHES_COLUMNS};
CREATE TABLE IF NOT EXISTS events {store_mod.EVENTS_V6_COLUMNS};
CREATE TABLE IF NOT EXISTS changes {store_mod.CHANGES_COLUMNS};
CREATE TABLE IF NOT EXISTS snoozes {store_mod.SNOOZES_COLUMNS};
{store_mod.V4_TABLES[0]};
{_V6_INDEXES};
{store_mod.COMMON_SCHEMA}"""
COUNTED = ("events", "changes", "event_reports")


def make_v6(path: Path) -> None:
    """A v6 database: one project, an event of each status (the failed and the
    retrying ones with an error), a change and a report per event."""
    raw = sqlite3.connect(path)
    raw.executescript(V6_SCHEMA)
    raw.execute("INSERT INTO projects (path, name, active, created_at) VALUES ('/p', 'p', 1, 0.0)")
    rows = [
        ("sent", 0, None),
        ("failed", 1, "sendMessage: Unauthorized"),
        ("pending", 2, "sendMessage: 502"),
        ("held", 0, None),
    ]
    for i, (status, attempts, error) in enumerate(rows, start=1):
        raw.execute(
            "INSERT INTO events (project_id, created_at, status, attempts, next_attempt_at, "
            "last_error) VALUES (1, ?, ?, ?, ?, ?)",
            (float(i), status, attempts, float(i), error),
        )
        raw.execute(
            "INSERT INTO changes (project_id, path, kind, observed_at, event_id) "
            "VALUES (1, ?, 'modified', ?, ?)",
            (f"f{i}", float(i), i),
        )
        raw.execute("INSERT INTO event_reports VALUES (?, 'main', '[]', 1, 0, 2, 20, 1)", (i,))
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()


@pytest.fixture
def v6(tmp_dir: Path) -> Path:
    path = tmp_dir / "state.db"
    make_v6(path)
    return path


def columns(db: sqlite3.Connection) -> list[str]:
    return [str(row[1]) for row in db.execute("PRAGMA table_info(events)")]


def counts(db: sqlite3.Connection) -> dict[str, int]:
    return {t: int(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in COUNTED}


EVENT_SQL = (
    "SELECT id, project_id, created_at, status, attempts, last_error FROM events ORDER BY id"
)


def test_a_new_database_is_v7_with_the_new_columns_last(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    db = store._db
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    assert columns(db)[-2:] == V7_COLUMNS
    store.close()


def test_v6_gains_the_columns_and_keeps_every_row(v6: Path, tmp_dir: Path) -> None:
    raw = sqlite3.connect(v6)
    before, counted = raw.execute(EVENT_SQL).fetchall(), counts(raw)
    raw.close()
    store = Store(v6)
    db = store._db
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    fresh = Store(tmp_dir / "fresh.db")
    assert columns(db) == columns(fresh._db)  # the same order as a new database
    fresh.close()
    assert db.execute(EVENT_SQL).fetchall() == before
    assert counts(db) == counted == {"events": 4, "changes": 4, "event_reports": 4}
    kinds = db.execute("SELECT last_error_kind, last_error_at FROM events").fetchall()
    assert set(kinds) == {(None, None)}  # nothing is known about a pre-v7 error
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.close()
    assert list(v6.parent.glob("*-pre-v6-*.db")) == []  # no rebuild, no backup


def test_reopening_a_v7_database_changes_nothing(v6: Path) -> None:
    Store(v6).close()
    raw = sqlite3.connect(v6)
    shape = columns(raw)
    raw.close()
    store = Store(v6)
    assert columns(store._db) == shape
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
    store.close()


def test_a_concurrent_v7_migration_is_not_redone_under_the_lock(v6: Path) -> None:
    class Racing(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql == "BEGIN IMMEDIATE":
                Store(v6).close()  # another opener adds the columns first
            return super().execute(sql, parameters)

    raw = sqlite3.connect(v6, isolation_level=None, factory=Racing)
    _migrate_v7(raw)  # no "duplicate column name"
    assert columns(raw)[-2:] == V7_COLUMNS
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 7
    raw.close()


def test_a_newer_version_is_never_lowered_by_the_v7_step(v6: Path) -> None:
    raw = sqlite3.connect(v6, isolation_level=None)
    raw.execute("PRAGMA user_version = 9")
    _migrate_v7(raw)
    assert columns(raw)[-2:] == V7_COLUMNS
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 9
    raw.close()


def test_a_v5_database_migrates_through_v6_to_v7(tmp_dir: Path) -> None:
    path = tmp_dir / "state.db"
    make_v5(path)
    store = Store(path)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert columns(store._db)[-2:] == V7_COLUMNS
    assert "project_id" in columns(store._db)
    store.close()


def test_the_older_migrations_templates_are_frozen() -> None:
    old = (*store_mod.V4_REBUILD, *store_mod.V6_REBUILD, store_mod.SCHEMA_V5)
    for sql in old:
        assert "last_error_kind" not in sql and "last_error_at" not in sql
    assert f"CREATE TABLE events_new {store_mod.EVENTS_V6_COLUMNS}" in store_mod.V6_REBUILD
    for column in V7_COLUMNS:
        assert column not in store_mod.EVENTS_V6_COLUMNS
        assert column in store_mod.EVENTS_COLUMNS and column in store_mod.SCHEMA


def test_rolling_back_from_v7_to_v6_keeps_the_restore_optional(tmp_dir: Path) -> None:
    """v7 only adds nullable columns and v6 code never lowers user_version: the
    previous code still runs on the database, so a restore stays optional."""
    state = tmp_dir / "state"
    state.mkdir()
    Store(state / "state.db").close()
    backup = state / "state-upgrade-20270115T080000Z.db"
    db = state / "state.db"
    assert upgrade.rollback_text(state, None, backup, version_before=6) == [
        "To roll back:",
        "  previous source unknown: reinstall from the checkout you used before "
        "(uv tool install --force --reinstall PATH), then diffbell install",
        "The upgrade moved the database from schema v6 to v7, which only adds columns: "
        "the previous code still runs on it (its doctor reports the newer schema).",
        f"Database backup: {backup}",
        "To restore it (loses the events since the backup):",
        "  diffbell uninstall",
        f"  cp {backup} {db}",
        f"  rm -f {db}-wal {db}-shm",
        "  then reinstall as above",
    ]
    assert "REQUIRED" not in "\n".join(upgrade.rollback_text(state, None, None, 6))


def test_a_bump_through_a_breaking_version_still_requires_a_restore() -> None:
    assert upgrade.breaking_bump(5, 7) is True
    assert upgrade.breaking_bump(6, 7) is False
    assert upgrade.breaking_bump(7, 7) is False
