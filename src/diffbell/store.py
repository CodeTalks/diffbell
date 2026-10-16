"""SQLite state: clocks, hashes, the change journal and the outbox (spec §5).

Invariant: ``changes.event_id IS NULL`` ⇔ the change is still waiting to be flushed.
Collapsed or superseded rows are deleted, never left unlinked.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Literal, cast

from diffbell.diffing import FileReport
from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.patterns import is_redacted
from diffbell.snapshots import (
    EVENT_CONTENT_BUDGET,
    PROJECT_EVENT_CONTENT_BUDGET,
    PROJECT_SNAPSHOT_BUDGET,
    AfterState,
    Captured,
    StoredContent,
)

# The events table's columns as schema v5 (and every older version) had them, keyed
# by the project's name. Frozen: used only by SCHEMA_V5 and the v3 -> v4 rebuild.
EVENTS_V5_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,  -- for a held event: when its pause ends
    last_error TEXT,
    telegram_message_id INTEGER,
    batch_id INTEGER,               -- events sent in one catch-up share its first event id
    held_reason TEXT                -- 'snooze' | 'quiet' | NULL
)"""

# The tables no schema version since v5 has changed: every open runs them (inside
# SCHEMA, or SCHEMA_V5 for a database still to migrate).
COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_logins (
    token_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    secret_hash TEXT  -- sha256 of the web server secret the token was minted for
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_used_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS web_requests (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('restart')),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS event_reports (
    event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    branch TEXT,
    reports TEXT NOT NULL,           -- JSON, see encode_reports
    added INTEGER NOT NULL,          -- "+" lines stored
    removed INTEGER NOT NULL,        -- "-" lines stored
    size INTEGER NOT NULL,           -- bytes of `reports`
    telegram_lines INTEGER NOT NULL, -- diff lines per file the message contained
    telegram_files INTEGER NOT NULL  -- files (in report order) the message listed
);
-- The health registry (health spec §4.1): one row per open problem, plus a resolved
-- one whose resolution message is still to be sent, plus one-shot notices.
CREATE TABLE IF NOT EXISTS problems (
    key TEXT PRIMARY KEY,
    severity TEXT NOT NULL CHECK (severity IN ('serious', 'warning', 'notice')),
    project TEXT,
    message TEXT NOT NULL,
    fix TEXT,
    opened_at REAL NOT NULL,
    notified_open INTEGER NOT NULL DEFAULT 0,  -- the "opened" message was delivered
    attempts INTEGER NOT NULL DEFAULT 0,       -- failed sends since the last success
    next_try_at REAL NOT NULL DEFAULT 0,       -- no send before this (backoff)
    last_error TEXT,
    resolved_at REAL                           -- set while the resolution is unsent
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- The stored reports' epoch (diff readability spec §2.1): bumped in the writing
-- statement's own transaction by every insert (a retry's INSERT OR REPLACE among
-- them), update and delete (the prune, and the cascade from `events`), so the diff
-- search's memo never serves a stale id set. No user_version bump: every open
-- creates them, and code without them ignores them.
CREATE TRIGGER IF NOT EXISTS event_reports_epoch_insert AFTER INSERT ON event_reports
BEGIN
    INSERT INTO kv (key, value) VALUES ('reports_epoch', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
END;
CREATE TRIGGER IF NOT EXISTS event_reports_epoch_update AFTER UPDATE ON event_reports
BEGIN
    INSERT INTO kv (key, value) VALUES ('reports_epoch', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
END;
CREATE TRIGGER IF NOT EXISTS event_reports_epoch_delete AFTER DELETE ON event_reports
BEGIN
    INSERT INTO kv (key, value) VALUES ('reports_epoch', '1')
    ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;
END;
"""

# Schema v5 as it was, frozen: a database from before v6 (its `events` has a
# `project` column) runs this, then _migrate_v4 and _migrate_v6. Only columns every
# old version has: it runs before the v4 migration.
SCHEMA_V5 = f"""
CREATE TABLE IF NOT EXISTS clocks (
    project TEXT PRIMARY KEY,
    watch_root TEXT NOT NULL,
    relative_root TEXT NOT NULL,
    clock TEXT NOT NULL,
    expr_hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS hashes (
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (project, path)
);
CREATE TABLE IF NOT EXISTS events {EVENTS_V5_COLUMNS};
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('created', 'modified', 'deleted')),
    observed_at REAL NOT NULL,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS changes_unflushed ON changes(id) WHERE event_id IS NULL;
CREATE INDEX IF NOT EXISTS changes_event ON changes(event_id);
CREATE INDEX IF NOT EXISTS events_project_created ON events(project, created_at);
CREATE INDEX IF NOT EXISTS events_status_next ON events(status, next_attempt_at);
{COMMON_SCHEMA}"""

# The tables schema v4 adds. Created by _migrate_v4, inside the rebuild's transaction
# for an older database, so a failed migration leaves nothing behind. Frozen with
# v5's `snoozes` (keyed by name): _migrate_v6 rebuilds it.
V4_TABLES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS batch_reports (
    batch_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    reports TEXT NOT NULL,           -- the merged report a catch-up message was built from
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    size INTEGER NOT NULL,
    telegram_lines INTEGER NOT NULL,
    telegram_files INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS snoozes (
    project TEXT PRIMARY KEY,
    since REAL NOT NULL,
    until REAL                       -- NULL: until turned back on
)""",
)
# Indexes on v4-only columns: never in SCHEMA_V5, which runs on a v3 `events` before
# the migration. Created by the rebuild and ensured after it (_migrate_v4).
V4_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS events_batch ON events(batch_id) WHERE batch_id IS NOT NULL",
)

# Schema v6 (stable project ids spec §1): a project is its folder. `projects` gives
# each canonical path a stable id, and every per-project row keys on that id; the
# name is a label that sync_projects keeps current. A row with a NULL path is a
# migrated name whose folder was unknown (it had no clock row).
PROJECTS_TABLE = """CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE,              -- canonical folder; NULL only for a migrated name
                                   -- whose folder is unknown (no clock row)
    name TEXT NOT NULL,            -- the current label: the config name while configured,
                                   -- the last one it had afterwards
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),  -- configured now
    created_at REAL NOT NULL
)"""
PROJECTS_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS projects_active_name ON projects(name) WHERE active = 1"
)
CLOCKS_COLUMNS = """(
    project_id INTEGER NOT NULL PRIMARY KEY REFERENCES projects(id),
    watch_root TEXT NOT NULL,
    relative_root TEXT NOT NULL,
    clock TEXT NOT NULL,
    expr_hash TEXT NOT NULL,
    updated_at REAL NOT NULL
)"""
HASHES_COLUMNS = """(
    project_id INTEGER NOT NULL REFERENCES projects(id),
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (project_id, path)
)"""
# The events table's columns as schema v6 had them. Frozen: used only by V6_REBUILD
# (a v5 database is rebuilt to these, then gains v7's columns in _migrate_v7).
EVENTS_V6_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,  -- for a held event: when its pause ends
    last_error TEXT,
    telegram_message_id INTEGER,
    batch_id INTEGER,               -- events sent in one catch-up share its first event id
    held_reason TEXT                -- 'snooze' | 'quiet' | NULL
)"""
# Schema v7 (settings redesign spec §7): last_error_kind is the ErrorKind value of
# last_error ("retryable", "auth", "bad_request") and last_error_at when it was
# recorded; both are written with last_error and cleared with it. NULL: no error, or
# one written before v7. Last, where _migrate_v7's ALTER TABLE adds them.
EVENTS_COLUMNS = """(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'held', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,  -- for a held event: when its pause ends
    last_error TEXT,
    telegram_message_id INTEGER,
    batch_id INTEGER,               -- events sent in one catch-up share its first event id
    held_reason TEXT,               -- 'snooze' | 'quiet' | NULL
    last_error_kind TEXT,           -- ErrorKind value of last_error (v7)
    last_error_at REAL              -- when last_error was recorded (v7)
)"""
CHANGES_COLUMNS = """(
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('created', 'modified', 'deleted')),
    observed_at REAL NOT NULL,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE
)"""
SNOOZES_COLUMNS = """(
    project_id INTEGER NOT NULL PRIMARY KEY REFERENCES projects(id),
    since REAL NOT NULL,
    until REAL                       -- NULL: until turned back on
)"""
V6_INDEXES: tuple[str, ...] = (
    PROJECTS_INDEX,
    "CREATE INDEX IF NOT EXISTS changes_unflushed ON changes(id) WHERE event_id IS NULL",
    "CREATE INDEX IF NOT EXISTS changes_event ON changes(event_id)",
    "CREATE INDEX IF NOT EXISTS events_project_created ON events(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS events_status_next ON events(status, next_attempt_at)",
    *V4_INDEXES,
)

_V6_INDEX_SQL = ";\n".join(V6_INDEXES)

# A new database, and every open of a v6 or v7 one: the whole schema.
SCHEMA = f"""
{PROJECTS_TABLE};
CREATE TABLE IF NOT EXISTS clocks {CLOCKS_COLUMNS};
CREATE TABLE IF NOT EXISTS hashes {HASHES_COLUMNS};
CREATE TABLE IF NOT EXISTS events {EVENTS_COLUMNS};
CREATE TABLE IF NOT EXISTS changes {CHANGES_COLUMNS};
CREATE TABLE IF NOT EXISTS snoozes {SNOOZES_COLUMNS};
{V4_TABLES[0]};
{_V6_INDEX_SQL};
{COMMON_SCHEMA}"""

# Content snapshots (content snapshots spec §1). Additive, with no user_version
# bump: every open creates them, after any migration (so `projects` exists), and
# code without them ignores them. ON DELETE CASCADE lets an older prune clean
# event_contents and event_meta; nothing ever deletes a `projects` row.
SNAPSHOT_SCHEMA = """
-- The current "before" of each watched file: its content as of the last event or baseline.
CREATE TABLE IF NOT EXISTS snapshots (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    path TEXT NOT NULL,
    content BLOB NOT NULL,
    captured_at REAL NOT NULL,
    PRIMARY KEY (project_id, path)
);
-- What each event's diff is built from (kept as long as its event_reports).
CREATE TABLE IF NOT EXISTS event_contents (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    before BLOB,                     -- NULL: no usable snapshot (fallback) or created
    after BLOB,                      -- set only when after_state = 'content'
    after_state TEXT NOT NULL CHECK (after_state IN ('content', 'deleted', 'uncapturable')),
    note TEXT,                       -- the git note frozen at creation, or NULL
    PRIMARY KEY (event_id, path)
);
-- Per-event context frozen at creation.
CREATE TABLE IF NOT EXISTS event_meta (
    event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    branch TEXT                      -- the git branch at creation, NULL outside git
);
"""
# kv key: when this code last wrote snapshots (every create_event with contents and
# replace_baseline with snapshots, and each maintenance tick). A clock advanced more
# than SNAPSHOT_GAP after it means older code ran in between (the downgrade guard).
SNAPSHOTS_SEEN = "snapshots_seen_at"
SNAPSHOT_GAP = 600.0
# Statuses whose message no future path rebuilds (spec §5): their contents are
# evicted first. Every other status (pending, held, failed) is waiting.
SETTLED_STATUSES = ("sent", "skipped")

# Bumped whenever the schema gains tables or columns. v4 rebuilds `events`
# (_migrate_v4); v5 adds `problems` and `kv`; v6 adds `projects` and keys every
# per-project row on its id (_migrate_v6); v7 adds events.last_error_kind and
# events.last_error_at (_migrate_v7: ALTER TABLE, since no CHECK changes). An added
# index (CREATE INDEX IF NOT EXISTS) needs no bump: every open creates it. The
# SNAPSHOT_SCHEMA tables are a deliberate exception: additive tables with no bump, so
# older code keeps running on the database (the downgrade guard, SNAPSHOTS_SEEN,
# covers what it misses).
SCHEMA_VERSION = 7
# A CLI -> daemon web request older than this is ignored (and pruned), so a request
# written while the daemon was down never fires at its next startup.
WEB_REQUEST_MAX_AGE = 60.0
# Stored diffs (event_reports): a longer line is clipped, and once the JSON would
# pass the cap, the remaining files are stored without lines ("truncated").
# diffview.js relies on this clip; don't raise it without checking the page's cost.
REPORT_LINE_MAX = 500
# The result is within max_bytes whenever the stubs alone fit. If it is over, every
# file is a stub, and the overshoot is bounded by the stubs (each file's path plus
# fixed fields), which `changes` already stores.
REPORT_MAX_BYTES = 64 * 1024
SIZE_LIMIT_NOTE = "diff omitted (size limit)"
# Finished events, and every stored report whatever its event's status, are pruned
# once they are this old (ADR 0007, "Diffs at rest").
RETENTION = 30 * 24 * 3600.0
# The kv row the event_reports triggers bump (text: read it with int()).
REPORTS_EPOCH = "reports_epoch"
# kv key: when a bot was last saved through the wizard (Daemon.complete_setup), epoch
# seconds as repr(float). telegram_failure ignores errors recorded before it.
TELEGRAM_SAVED_AT = "telegram.saved_at"
# What the Settings card says is failing (settings redesign spec §7).
FailureKind = Literal["auth", "bad_request", "retryable", "legacy_failed"]
# The undelivered events (settings redesign spec §7): failed for good, or still being
# retried. A pending or held event never attempted is not part of the backlog.
_UNDELIVERED = "status = 'failed' OR (status IN ('pending', 'held') AND attempts > 0)"
_FAILURE_RANK: dict[FailureKind, int] = {
    "auth": 3,
    "bad_request": 2,
    "legacy_failed": 2,
    "retryable": 1,
}
_STORED_KINDS: dict[str, FailureKind] = {
    "auth": "auth",
    "bad_request": "bad_request",
    "retryable": "retryable",
}
# A search shorter than this (after trimming, in code points) matches paths only.
DIFF_SEARCH_MIN = 2
# Distinct searches whose diff-line matches are remembered (least recently used out).
DIFF_SEARCH_MEMO = 16
# Step 1 of the diff search (diff readability spec §2.1): the (event, path) pairs with
# a stored line containing the pattern. Each CASE turns what a JSON function would
# raise on (an invalid report; an entry that is not an object, whose value is then not
# JSON) into an empty one unconditionally, whatever order the planner evaluates the
# filters in. Only text items of a `lines` array count; a redacted entry or one
# without a text path never matches.
DIFF_SEARCH_SQL = """
WITH entries AS (
    SELECT r.event_id AS event_id,
           CASE WHEN f.type = 'object' THEN f.value ELSE '{}' END AS entry
    FROM event_reports r,
         json_each(CASE WHEN json_valid(r.reports) THEN r.reports ELSE '[]' END) f
)
SELECT DISTINCT e.event_id, json_extract(e.entry, '$.path')
FROM entries e, json_each(e.entry, '$.lines') l
WHERE json_type(e.entry, '$.path') = 'text'
  AND json_type(e.entry, '$.lines') = 'array'
  AND json_type(e.entry, '$.redacted') IS NOT 'true'
  AND l.type = 'text'
  AND l.value LIKE ? ESCAPE '\\'
"""

# The activity strip (Plan 7 spec §2.2): events per project and bucket in one query.
# Each bucket is a JSON pair [since, until) built by day_bounds, so the strip and the
# feed's since/until filter count exactly the same events. The configured projects are
# the last parameter (a JSON array of their ids), so the planner seeks
# events_project_created per project instead of scanning every event.
ACTIVITY_SQL = """
SELECT e.project_id, CAST(b.key AS INTEGER), COUNT(*), SUM(e.status = 'failed')
FROM json_each(?) AS b
JOIN events AS e
  ON e.created_at >= json_extract(b.value, '$[0]')
 AND e.created_at < json_extract(b.value, '$[1]')
WHERE e.created_at >= ? AND e.created_at < ?
  AND e.project_id IN (SELECT value FROM json_each(?))
GROUP BY e.project_id, b.key
"""
ACTIVITY_DAYS = 14
# "Needs attention" (Plan 7 spec §2.3): the statuses the status=attention filter selects.
ATTENTION = "attention"
ATTENTION_STATUSES = ("failed", "held")

ReportsState = Literal["stored", "pruned", "not_recorded"]


def reports_state(has_report: bool, created_at: float, now: float) -> ReportsState:
    """Why an event has (no) stored report. "pruned": older than the retention, so
    its report was deleted (it may be held or pending and still be listed).
    "not_recorded": younger, so it never had one (an event from before schema v3)."""
    if has_report:
        return "stored"
    return "pruned" if created_at < now - RETENTION else "not_recorded"


# How long an opener waits for another process's write lock (sqlite3 `timeout`).
BUSY_TIMEOUT = 30.0
# What a Store open says when that wait runs out (the daemon exits; launchd retries).
BUSY_MESSAGE = "database busy (another diffbell process is using it); retry"


class MigrationError(Exception):
    """A schema migration failed and was rolled back: the database is unchanged."""


class UnknownProject(LookupError):
    """A write named a project that is not configured (not active since the last
    Store.sync_projects). The daemon syncs before it watches, so this is a bug."""


class ProjectPathTaken(Exception):
    """Another project already owns the folder a project is being moved to."""


# The v3 -> v4 rebuild of `events` (SQLite can't alter a CHECK constraint), SQLite's
# documented procedure. A tuple of statements, so a test can inject a failure.
# The rename runs while `changes`, `event_reports` and `batch_reports` still
# reference events(id); with the default legacy_alter_table=OFF SQLite handles that.
# Should a SQLite build refuse it, the documented escape hatch is
# PRAGMA legacy_alter_table=ON around that one statement; the row-count check in
# _rebuild_events_v4 catches either outcome.
V4_REBUILD: tuple[str, ...] = (
    f"CREATE TABLE events_new {EVENTS_V5_COLUMNS}",
    """INSERT INTO events_new (id, project, created_at, status, attempts, next_attempt_at,
                              last_error, telegram_message_id, batch_id, held_reason)
       SELECT id, project, created_at, status, attempts, next_attempt_at,
              last_error, telegram_message_id, NULL, NULL FROM events""",
    "DROP TABLE events",
    "ALTER TABLE events_new RENAME TO events",
    "CREATE INDEX IF NOT EXISTS events_project_created ON events(project, created_at)",
    "CREATE INDEX IF NOT EXISTS events_status_next ON events(status, next_attempt_at)",
    *V4_INDEXES,
)
# Rows that must survive the rebuild one for one.
_COUNTED = ("events", "changes", "event_reports")
# The v6 rebuild counts batch_reports too (a v3 database gets it only in _migrate_v4).
_V6_COUNTED = (*_COUNTED, "batch_reports")


def _row_counts(db: sqlite3.Connection, tables: tuple[str, ...] = _COUNTED) -> dict[str, int]:
    return {t: int(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables}


def _has_table(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
    return row.fetchone() is not None


def _has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))


def _keep_sequence(db: sqlite3.Connection, seq: int) -> None:
    """After a rebuild of `events`: AUTOINCREMENT never reuses an id, even a pruned
    one, so an old ?event= link or batch id never names a new event. The copy only
    knows the ids it holds."""
    cur = db.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'events'", (seq,))
    if cur.rowcount == 0:
        db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('events', ?)", (seq,))


def _rebuild_events_v4(db: sqlite3.Connection) -> None:
    """The rebuild, inside the caller's transaction. Raises MigrationError when the
    row counts change: with foreign keys still on, DROP TABLE events deletes every
    row first and cascades into `changes` and `event_reports`. Also when
    foreign_key_check finds a row whose event is gone."""
    before = _row_counts(db)
    seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
    for sql in V4_REBUILD:
        db.execute(sql)
    if seq is not None:
        _keep_sequence(db, int(seq[0]))
    after = _row_counts(db)
    if after != before:
        raise MigrationError(f"row counts changed during the events rebuild: {before} -> {after}")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("foreign_key_check found rows whose event is missing")


def _migrate_v4(db: sqlite3.Connection) -> None:
    """Rebuild `events` and add the v4 tables in one transaction. Once `events` has
    the v4 columns (a new database, or a concurrent opener that migrated first) it
    only makes sure the v4 tables exist."""
    if _has_column(db, "events", "batch_id"):
        for sql in (*V4_TABLES, *V4_INDEXES):
            db.execute(sql)
        return
    # PRAGMA foreign_keys is a silent no-op inside a transaction: it must be turned
    # off before BEGIN, or DROP TABLE events would cascade (see _rebuild_events_v4).
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise MigrationError("could not turn foreign keys off")
        db.execute("BEGIN IMMEDIATE")
        try:
            if not _has_column(db, "events", "batch_id"):  # re-checked under the lock
                _rebuild_events_v4(db)
                db.execute("PRAGMA user_version = 4")
            for sql in (*V4_TABLES, *V4_INDEXES):
                db.execute(sql)
            db.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                if db.in_transaction:
                    db.execute("ROLLBACK")
            raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


# The v5 -> v6 rebuild (stable project ids spec §3): every per-project table is
# rebuilt keyed on projects.id, through temp.v6_ids (name -> id, and `keep`: 1 for
# the name whose clock, hashes and snooze survive). The same documented procedure
# as V4_REBUILD; a tuple of statements, so a test can inject a failure. Each runs
# with the named parameter :now (the migration's time).
V6_REBUILD: tuple[str, ...] = (
    # A merged-away name's snooze is not kept (below): what it held is due now, as
    # drop_snoozes_except does for a removed project's, so the next catch-up check
    # re-evaluates it (the kept name's own snooze still holds it) instead of it
    # waiting for a pause that no longer exists, possibly forever.
    """UPDATE events SET next_attempt_at = :now
       WHERE status = 'held' AND held_reason = 'snooze' AND project IN (
         SELECT s.project FROM snoozes s JOIN temp.v6_ids m ON m.name = s.project
         WHERE m.keep = 0)""",
    f"CREATE TABLE clocks_new {CLOCKS_COLUMNS}",
    """INSERT INTO clocks_new (project_id, watch_root, relative_root, clock, expr_hash,
                              updated_at)
       SELECT m.id, c.watch_root, c.relative_root, c.clock, c.expr_hash, c.updated_at
       FROM clocks c JOIN temp.v6_ids m ON m.name = c.project WHERE m.keep = 1""",
    "DROP TABLE clocks",
    "ALTER TABLE clocks_new RENAME TO clocks",
    f"CREATE TABLE hashes_new {HASHES_COLUMNS}",
    """INSERT INTO hashes_new (project_id, path, fingerprint)
       SELECT m.id, h.path, h.fingerprint
       FROM hashes h JOIN temp.v6_ids m ON m.name = h.project WHERE m.keep = 1""",
    "DROP TABLE hashes",
    "ALTER TABLE hashes_new RENAME TO hashes",
    f"CREATE TABLE snoozes_new {SNOOZES_COLUMNS}",
    """INSERT INTO snoozes_new (project_id, since, until)
       SELECT m.id, s.since, s.until
       FROM snoozes s JOIN temp.v6_ids m ON m.name = s.project WHERE m.keep = 1""",
    "DROP TABLE snoozes",
    "ALTER TABLE snoozes_new RENAME TO snoozes",
    f"CREATE TABLE events_new {EVENTS_V6_COLUMNS}",
    """INSERT INTO events_new (id, project_id, created_at, status, attempts, next_attempt_at,
                              last_error, telegram_message_id, batch_id, held_reason)
       SELECT e.id, m.id, e.created_at, e.status, e.attempts, e.next_attempt_at,
              e.last_error, e.telegram_message_id, e.batch_id, e.held_reason
       FROM events e JOIN temp.v6_ids m ON m.name = e.project""",
    "DROP TABLE events",
    "ALTER TABLE events_new RENAME TO events",
    f"CREATE TABLE changes_new {CHANGES_COLUMNS}",
    """INSERT INTO changes_new (id, project_id, path, kind, observed_at, event_id)
       SELECT c.id, m.id, c.path, c.kind, c.observed_at, c.event_id
       FROM changes c JOIN temp.v6_ids m ON m.name = c.project""",
    "DROP TABLE changes",
    "ALTER TABLE changes_new RENAME TO changes",
    *V6_INDEXES,
)
# Every name a v5 database stores, in any per-project table.
V5_NAMES_SQL = """SELECT project FROM clocks UNION SELECT project FROM hashes
                  UNION SELECT project FROM events UNION SELECT project FROM changes
                  UNION SELECT project FROM snoozes"""


def project_folder(watch_root: str, relative_root: str) -> str:
    """A clock row's canonical folder: the watch root, plus the relative root when
    there is one (as the watcher compares them). Watchman's roots are real paths, so
    this meets the config side's realpath."""
    return os.path.normpath(os.path.join(watch_root, relative_root))


def _assign_v6_ids(db: sqlite3.Connection, now: float) -> None:
    """Steps 1-3 of the migration: one inactive `projects` row per distinct folder
    (named after the name with the latest clock `updated_at`), one per name without
    a clock row (path NULL), and temp.v6_ids mapping every stored name to its id."""
    names = sorted(str(row[0]) for row in db.execute(V5_NAMES_SQL))
    clocks = {
        str(name): (project_folder(str(root), str(rel)), float(updated))
        for name, root, rel, updated in db.execute(
            "SELECT project, watch_root, relative_root, updated_at FROM clocks"
        )
    }
    by_folder: dict[str, list[str]] = {}
    for name in names:
        if name in clocks:
            by_folder.setdefault(clocks[name][0], []).append(name)
    db.execute("DROP TABLE IF EXISTS temp.v6_ids")
    db.execute(
        "CREATE TEMP TABLE v6_ids (name TEXT PRIMARY KEY, id INTEGER NOT NULL, "
        "keep INTEGER NOT NULL)"
    )
    for folder, group in sorted(by_folder.items()):
        latest = max(group, key=lambda name: (clocks[name][1], name))
        cur = db.execute(
            "INSERT INTO projects (path, name, active, created_at) VALUES (?, ?, 0, ?)",
            (folder, latest, now),
        )
        db.executemany(
            "INSERT INTO temp.v6_ids (name, id, keep) VALUES (?, ?, ?)",
            [(name, cur.lastrowid, int(name == latest)) for name in group],
        )
    for name in names:
        if name not in clocks:
            cur = db.execute(
                "INSERT INTO projects (path, name, active, created_at) VALUES (NULL, ?, 0, ?)",
                (name, now),
            )
            db.execute(
                "INSERT INTO temp.v6_ids (name, id, keep) VALUES (?, ?, 1)", (name, cur.lastrowid)
            )


def _rebuild_v6(db: sqlite3.Connection, now: float) -> None:
    """The rebuild, inside the caller's transaction. Raises MigrationError when the
    `events`, `changes`, `event_reports` or `batch_reports` counts change, or when
    foreign_key_check finds a row whose event or project is missing (it checks every
    project_id)."""
    before = _row_counts(db, _V6_COUNTED)
    seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
    db.execute(PROJECTS_TABLE)
    _assign_v6_ids(db, now)
    for sql in V6_REBUILD:
        db.execute(sql, {"now": now})
    db.execute("DROP TABLE temp.v6_ids")
    if seq is not None:
        _keep_sequence(db, int(seq[0]))
    after = _row_counts(db, _V6_COUNTED)
    if after != before:
        raise MigrationError(f"row counts changed during the v6 rebuild: {before} -> {after}")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("foreign_key_check found rows whose event or project is missing")


def _migrate_v6(db: sqlite3.Connection, now: float) -> None:
    """Key every per-project row on a `projects` id, in one transaction with foreign
    keys off (the procedure of _migrate_v4). Once `events` has `project_id` (a
    concurrent opener migrated first) there is nothing to do."""
    if _has_column(db, "events", "project_id"):
        return
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise MigrationError("could not turn foreign keys off")
        db.execute("BEGIN IMMEDIATE")
        try:
            # Re-checked under the lock: the shape, and the version it goes with.
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version < 6 and not _has_column(db, "events", "project_id"):
                _rebuild_v6(db, now)
                db.execute("PRAGMA user_version = 6")
            db.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                if db.in_transaction:
                    db.execute("ROLLBACK")
            raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


# The v7 columns (settings redesign spec §7), in the order a new database has them.
V7_COLUMNS: tuple[tuple[str, str], ...] = (
    ("last_error_kind", "TEXT"),
    ("last_error_at", "REAL"),
)


def _migrate_v7(db: sqlite3.Connection) -> None:
    """Add events.last_error_kind and events.last_error_at: nullable columns with no
    CHECK, so ALTER TABLE, never a rebuild (foreign keys stay on). Under BEGIN
    IMMEDIATE and re-checked there, so a concurrent opener adds each column once.
    Nothing to do once both exist (a new database, or another opener was first).
    user_version is raised to 7, never lowered."""
    if all(_has_column(db, "events", name) for name, _ in V7_COLUMNS):
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        for name, sql_type in V7_COLUMNS:
            if not _has_column(db, "events", name):  # re-checked under the lock
                db.execute(f"ALTER TABLE events ADD COLUMN {name} {sql_type}")
        if int(db.execute("PRAGMA user_version").fetchone()[0]) < 7:
            db.execute("PRAGMA user_version = 7")
        db.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            if db.in_transaction:
                db.execute("ROLLBACK")
        raise


@dataclass(frozen=True, slots=True)
class ClockState:
    """A Watchman clock is only meaningful for the watch root that issued it."""

    watch_root: str
    relative_root: str
    clock: str
    expr_hash: str


@dataclass(frozen=True, slots=True)
class EventRow:
    id: int
    project: str
    created_at: float
    status: str
    attempts: int
    next_attempt_at: float
    last_error: str | None
    telegram_message_id: int | None
    batch_id: int | None = None  # set once the event went out in a catch-up message
    held_reason: str | None = None  # "snooze", "quiet" or "setup" while (or since) held
    # False once the event's project is no longer configured: its name may then be a
    # configured project's (another folder), so the notifier never looks it up.
    project_active: bool = True


@dataclass(frozen=True, slots=True)
class TelegramFailure:
    """Store.telegram_failure: the winning kind, when the backlog began (the oldest
    undelivered event's creation), how many failed for good and how many are still
    being retried, and the newest stored error text of that kind (token-free)."""

    kind: FailureKind
    since: float
    failed: int
    waiting: int
    error: str


@dataclass(frozen=True, slots=True)
class EventMeta:
    """An event_meta row: the context frozen when the event was created."""

    branch: str | None


@dataclass(frozen=True, slots=True)
class Snooze:
    """A paused project (one row of `snoozes`)."""

    project: str
    since: float
    until: float | None  # None: until turned back on

    def active(self, now: float) -> bool:
        return self.until is None or now < self.until


@dataclass(frozen=True, slots=True)
class ReportSummary:
    """The list-row part of a stored report."""

    branch: str | None
    added: int
    removed: int


@dataclass(frozen=True, slots=True)
class EventDetail:
    """An event plus its file changes, for the web activity feed. ``summary`` is
    None for an event without a stored report (created before schema v3)."""

    event: EventRow
    changes: list[Change]
    summary: ReportSummary | None = None
    q_in_diff: bool = False  # the search matched one of its stored diff lines


@dataclass(frozen=True, slots=True)
class DayCount:
    """One local day of a project's activity strip (Plan 7 spec §2.2). ``since`` and
    ``until`` are that day's local midnight and the next one (epoch seconds): the
    page filters the feed with exactly these."""

    day: date
    since: float
    until: float
    total: int
    failed: int


def day_bounds(now: float, days: int, tz: tzinfo | None = None) -> list[tuple[date, float, float]]:
    """The ``days`` local days ending with the one that holds ``now``, oldest first,
    as (day, its midnight, the next midnight) in epoch seconds. Built from dates,
    never by adding 86400 s, so a 23 h or 25 h DST day is right. ``tz`` None is the
    Mac's local time (the feed's Today/Yesterday use the same machine's)."""
    today = datetime.fromtimestamp(now, tz).date()

    def midnight(day: date) -> float:
        return datetime.combine(day, clock_time(), tzinfo=tz).timestamp()

    out: list[tuple[date, float, float]] = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        out.append((day, midnight(day), midnight(day + timedelta(days=1))))
    return out


@dataclass(frozen=True, slots=True)
class EncodedReports:
    text: str  # the JSON stored in event_reports.reports
    added: int
    removed: int


@dataclass(frozen=True, slots=True)
class StoredReport:
    """One event_reports row (or batch_reports row, whose branch is None), decoded."""

    branch: str | None
    reports: list[dict[str, Any]]
    added: int
    removed: int
    telegram_lines: int
    telegram_files: int


# A problem's severity; "notice" is a one-shot message, never listed (health.py).
Severity = Literal["serious", "warning", "notice"]


@dataclass(frozen=True, slots=True)
class Problem:
    """One `problems` row (health spec §4.1). ``severity`` is "serious", "warning"
    or "notice" (a one-shot message, never listed). ``resolved_at`` is set while the
    problem is resolved but its resolution message is still to be sent."""

    key: str
    severity: Severity
    project: str | None
    message: str
    fix: str | None
    opened_at: float
    notified_open: bool = False
    attempts: int = 0
    next_try_at: float = 0.0
    last_error: str | None = None
    resolved_at: float | None = None


_PROBLEM_COLUMNS = """key, severity, project, message, fix, opened_at, notified_open,
                      attempts, next_try_at, last_error, resolved_at"""


def _problem(row: Sequence[Any]) -> Problem:
    key, severity, project, message, fix, opened_at, notified, attempts, next_try, error, done = row
    return Problem(
        str(key),
        cast(Severity, str(severity)),  # guarded by the table's CHECK
        None if project is None else str(project),
        str(message),
        None if fix is None else str(fix),
        float(opened_at),
        bool(notified),
        int(attempts),
        float(next_try),
        None if error is None else str(error),
        None if done is None else float(done),
    )


@dataclass(frozen=True, slots=True)
class WebSession:
    csrf: str
    created_at: float
    expires_at: float
    last_used_at: float


def _like_escape(text: str) -> str:
    """Escape LIKE wildcards so ``%``, ``_`` and ``\\`` in the input match literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clip_line(line: str) -> str:
    if len(line) <= REPORT_LINE_MAX:
        return line
    return line[: REPORT_LINE_MAX - 1] + "…"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def stored_first_line(entry: dict[str, Any]) -> int | None:
    """A stored entry's ``first_line`` (Plan 7 spec §2.1), or None: a report stored
    before Plan 7 has no key, and a non-integer (a bool included) or a value below 1
    is never trusted."""
    value = entry.get("first_line")
    return value if type(value) is int and value >= 1 else None


def _flagged(entry: dict[str, Any], rule_skipped: bool) -> dict[str, Any]:
    """The flag is only written when set, so other entries keep the v3 format."""
    return {**entry, "rule_skipped": True} if rule_skipped else entry


def encode_reports(
    reports: Sequence[FileReport],
    max_bytes: int = REPORT_MAX_BYTES,
    *,
    rule_skipped: Collection[str] = (),
) -> EncodedReports:
    """The JSON stored for an event: one entry per FileReport, in report order.

    Lines are clipped to REPORT_LINE_MAX characters. A file is kept whole only if
    the JSON, with the stubs of every later file, stays within ``max_bytes``; from
    the first file that would not, every remaining file keeps only its path, kind
    and redaction flag (``truncated``). The result is within ``max_bytes``
    whenever the stubs alone fit. If it is over, every file is a stub, and the
    overshoot is bounded by the stubs (each file's path plus fixed fields), which
    ``changes`` already stores.
    A redacted file never carries lines, whatever the report holds. A file whose
    path is in ``rule_skipped`` (a notify_on rule left it out of the Telegram
    message) gains ``"rule_skipped": true``. A kept, unredacted file with a
    ``first_line`` of 1 or more gains ``"first_line"``; a stub never has it, and a
    kept entry's cost is measured from its own JSON, so the key counts in the cap."""
    stubs: list[dict[str, Any]] = [
        _flagged(
            {
                "path": report.path,
                "kind": report.kind.value,
                "redacted": report.redacted,
                "note": SIZE_LIMIT_NOTE,
                "lines": [],
                "truncated": True,
            },
            report.path in rule_skipped,
        )
        for report in reports
    ]
    # stub_after[i]: the bytes the stubs of the files after i need, each with its comma.
    stub_after = [0] * len(reports)
    later = 0
    for i in range(len(reports) - 1, -1, -1):
        stub_after[i] = later
        later += len(_json(stubs[i]).encode()) + 1
    entries: list[dict[str, Any]] = []
    used = 2  # "[" and "]"
    full = True
    for i, report in enumerate(reports):
        if full:
            lines = [] if report.redacted else [_clip_line(line) for line in report.lines]
            entry: dict[str, Any] = _flagged(
                {
                    "path": report.path,
                    "kind": report.kind.value,
                    "redacted": report.redacted,
                    "note": report.note,
                    "lines": lines,
                    "truncated": False,
                },
                report.path in rule_skipped,
            )
            first = report.first_line
            if not report.redacted and first is not None and first >= 1:
                entry["first_line"] = first
            cost = len(_json(entry).encode()) + (1 if entries else 0)  # the comma
            if used + cost + stub_after[i] > max_bytes:
                full = False
        if not full:
            entry = stubs[i]
            cost = len(_json(entry).encode()) + (1 if entries else 0)
        entries.append(entry)
        used += cost
    stored = [line for entry in entries for line in entry["lines"]]
    return EncodedReports(
        _json(entries),
        sum(1 for line in stored if line.startswith("+")),
        sum(1 for line in stored if line.startswith("-")),
    )


# An event row with its project's current name (stable project ids spec §2): read
# FROM _EVENTS_FROM, whose alias `e` every filter on events uses.
_EVENT_COLUMNS = """e.id, p.name, e.created_at, e.status, e.attempts, e.next_attempt_at,
                    e.last_error, e.telegram_message_id, e.batch_id, e.held_reason,
                    p.active = 1"""
_EVENTS_FROM = "events e JOIN projects p ON p.id = e.project_id"


def _event(row: Sequence[Any]) -> EventRow:
    """An EventRow from a row of _EVENT_COLUMNS (SQLite returns the flag as 0/1)."""
    return replace(EventRow(*row[:-1]), project_active=bool(row[-1]))


def _failure_kind(status: str, kind: object) -> FailureKind:
    """A row's kind for telegram_failure: the stored ErrorKind value, else (NULL, or
    a value it doesn't rank) legacy_failed for a failed row, retryable for one still
    being retried."""
    stored = _STORED_KINDS.get(kind) if isinstance(kind, str) else None
    if stored is not None:
        return stored
    return "legacy_failed" if status == "failed" else "retryable"


def _float_or_none(text: str | None) -> float | None:
    """A kv number, or None when it is missing or unreadable (nothing is filtered)."""
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value else None  # NaN reads as missing


# Journal rows with their project's current name, as Change(id, project, path, kind).
_CHANGES_FROM = "changes c JOIN projects p ON p.id = c.project_id"


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Owner-only (0600), also for a database an earlier version left 0644.
        # SQLite gives a new -wal/-shm the database's own mode.
        os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            with contextlib.suppress(FileNotFoundError):
                os.chmod(p, 0o600)
        self._path = path
        # Explicit transactions; wait for another process's write lock.
        self._db = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT)
        # Configured name -> projects.id, rebuilt by sync_projects (empty until then).
        self._ids: dict[str, int] = {}
        # projects.id -> how many times its snapshots were written or deleted by any
        # writer but replace_baseline, in this process (content snapshots spec
        # §2.1): a baseline read off the loop stores nothing if it moved meanwhile.
        self._snap_epoch: dict[int, int] = {}
        # q -> (redact, reports epoch, ids): the diff search's memo (spec §2.1).
        self._diff_memo: OrderedDict[str, tuple[tuple[str, ...], int, frozenset[int]]] = (
            OrderedDict()
        )
        try:
            self._open()
        except BaseException:
            self._db.close()
            raise

    def _open(self) -> None:
        # On from the start, so the migrations really go through their OFF step.
        self._db.execute("PRAGMA foreign_keys=ON")
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            # A database from before v6 keys its rows by name (`events.project`).
            legacy = _has_table(self._db, "events") and not _has_column(
                self._db, "events", "project_id"
            )
            if legacy:
                self._backup_before_v6()
            self._db.executescript(SCHEMA_V5 if legacy else SCHEMA)
            # Added within schema version 2: a login token is bound to one server
            # start. A row from before (NULL) is bound to no server and never redeems.
            if not self._has_login_secret_column():
                try:
                    self._db.execute("ALTER TABLE web_logins ADD COLUMN secret_hash TEXT")
                except sqlite3.OperationalError:
                    if not self._has_login_secret_column():  # not a concurrent add
                        raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):  # the same busy case as in the migration below
                raise MigrationError(BUSY_MESSAGE) from exc
            raise
        try:
            if legacy:
                _migrate_v4(self._db)
                _migrate_v6(self._db, time.time())
            _migrate_v7(self._db)  # every database: a v6 one, or one v6 just built
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                # Another process holds the write lock past BUSY_TIMEOUT: rolled back,
                # nothing changed; the next start (or launchd's retry) tries again.
                raise MigrationError(BUSY_MESSAGE) from exc
            raise MigrationError(f"database migration failed: {exc}") from exc
        except (sqlite3.Error, MigrationError) as exc:
            # Rolled back: user_version and every row are as before. The daemon exits
            # with this message (and launchd retries, which is safe).
            raise MigrationError(f"database migration failed: {exc}") from exc
        self._db.execute("PRAGMA foreign_keys=ON")
        if self._db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise MigrationError("database migration failed: foreign keys are not enforced")
        # The hourly alert dedupe is gone (health spec §4.3). Dropped on every open,
        # not only by the v4 -> v5 step: code from before v5 recreates the table (and
        # keeps user_version 5), so a rollback and re-upgrade would otherwise leave it
        # behind. Idempotent, and no foreign key points to it. A write: another
        # process holding the lock reads as busy, as in the migration.
        try:
            self._db.execute("DROP TABLE IF EXISTS alerts")
            self._db.executescript(SNAPSHOT_SCHEMA)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                raise MigrationError(BUSY_MESSAGE) from exc
            raise
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        # Future schema migrations key off this version; never overwrite a newer one.
        if version < SCHEMA_VERSION:
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _backup_before_v6(self) -> None:
        """Copy the database with SQLite's backup API to ``<name>-pre-v6-<UTC>.db``
        next to it (0600), before the v6 migration rewrites every per-project row: a
        reinstall runs no `upgrade`, so nothing else keeps a copy. Written under a
        ``.part`` name, then renamed, so a backup that exists is complete. One left
        by an earlier attempt is kept: a failed migration changed nothing, so
        launchd's retries add no copies. Raises MigrationError (nothing migrated, no
        ``.part`` left) if the copy can't be written."""
        if any(self._path.parent.glob(f"{glob.escape(self._path.stem)}-pre-v6-*.db")):
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self._path.with_name(f"{self._path.stem}-pre-v6-{stamp}.db")
        part = target.with_name(target.name + ".part")
        try:
            os.close(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600))
            os.chmod(part, 0o600)  # also a part file an interrupted attempt left
            copy = sqlite3.connect(part)
            try:
                self._db.backup(copy)
            finally:
                copy.close()
            os.replace(part, target)
        except (OSError, sqlite3.Error) as exc:
            # Never a half-written copy: the next attempt starts over.
            with contextlib.suppress(OSError):
                os.unlink(part)
            raise MigrationError(f"database backup failed: {exc}") from exc

    def _has_login_secret_column(self) -> bool:
        return any(
            row[1] == "secret_hash" for row in self._db.execute("PRAGMA table_info(web_logins)")
        )

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._rollback()
            raise
        else:
            try:
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def _rollback(self) -> None:
        """Best-effort ROLLBACK: a failure here must never mask the original exception."""
        try:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -- projects (stable project ids spec §2) ----------------------------------

    def sync_projects(self, projects: Sequence[tuple[str, str]], now: float) -> None:
        """Make ``projects`` (name, canonical folder) the configured ones, in one
        transaction: every row is deactivated first (so a name moving between folders
        never trips the unique active-name index), then a known folder takes the name
        and is active again, keeping its id. A new folder takes the id of a migrated
        name without a folder (path NULL: it had a snooze or events but no clock), so
        that snooze survives; otherwise it gets a new id. Then the name -> id map
        every per-project method uses is rebuilt from the active rows."""
        with self._tx() as db:
            db.execute("UPDATE projects SET active = 0 WHERE active = 1")
            for name, path in projects:
                cur = db.execute(
                    "UPDATE projects SET name = ?, active = 1 WHERE path = ?", (name, path)
                )
                if cur.rowcount == 0:
                    cur = db.execute(
                        "UPDATE projects SET path = ?, active = 1 WHERE path IS NULL AND name = ?",
                        (path, name),
                    )
                if cur.rowcount == 0:
                    db.execute(
                        "INSERT INTO projects (path, name, active, created_at) VALUES (?, ?, 1, ?)",
                        (path, name, now),
                    )
            # Only after the sync, and only for rows still inactive: a renamed project
            # (same folder, same id) keeps its snapshots, a removed one doesn't.
            db.execute(
                "DELETE FROM snapshots WHERE project_id IN "
                "(SELECT id FROM projects WHERE active = 0)"
            )
            rows = db.execute("SELECT name, id FROM projects WHERE active = 1").fetchall()
            self._bump_all_snapshot_epochs(db)
        self._ids = {str(name): int(pid) for name, pid in rows}

    def move_project_path(self, old: str, new: str) -> int | None:
        """Move a project's folder from ``old`` to ``new`` (both canonical, as
        ``config.resolve_projects`` makes them), keeping its id and with it every
        event, hash, snapshot and snooze (ADR 0016). Its clock names the old folder,
        so it is deleted: the next subscription re-baselines the project silently
        (watcher: no clock, REBASELINE), keeping its id and with it every event,
        snooze and the history; a file changed during the move itself is therefore
        not reported. Idempotent: with no row at ``old`` and one at
        ``new`` the move is already done and that id is returned; with neither there
        is nothing to move (None). Raises ProjectPathTaken when both exist (two
        projects' histories are never merged). Used only by rename_install."""
        with self._tx() as db:
            at_old = db.execute("SELECT id FROM projects WHERE path = ?", (old,)).fetchone()
            at_new = db.execute("SELECT id FROM projects WHERE path = ?", (new,)).fetchone()
            if at_old is None:
                return None if at_new is None else int(at_new[0])
            if at_new is not None:
                raise ProjectPathTaken(new)
            pid = int(at_old[0])
            db.execute("UPDATE projects SET path = ? WHERE id = ?", (new, pid))
            db.execute("DELETE FROM clocks WHERE project_id = ?", (pid,))
            return pid

    def _project_id(self, project: str, expect: int | None = None) -> int:
        """The configured project's id, for a write. Raises UnknownProject, also when
        ``expect`` (a project_token) is given and the name now names another id."""
        try:
            pid = self._ids[project]
        except KeyError:
            raise UnknownProject(project) from None
        if expect is not None and pid != expect:
            raise UnknownProject(project)
        return pid

    def project_token(self, project: str) -> int:
        """An opaque token for the folder a configured name names now. The watcher
        takes it when it subscribes and passes it back as ``expect`` with every write
        for that subscription: after a sync moved the name to another folder (a swap),
        the write raises UnknownProject instead of landing on the wrong project.
        Raises UnknownProject for a name that is not configured."""
        return self._project_id(project)

    def _names(self) -> dict[int, str]:
        """id -> configured name: the inverse of the map, for results keyed by name."""
        return {pid: name for name, pid in self._ids.items()}

    # -- clocks & hashes -------------------------------------------------

    def get_clock(self, project: str) -> ClockState | None:
        pid = self._ids.get(project)
        if pid is None:
            return None
        row = self._db.execute(
            "SELECT watch_root, relative_root, clock, expr_hash FROM clocks WHERE project_id = ?",
            (pid,),
        ).fetchone()
        return ClockState(*row) if row else None

    def get_hashes(self, project: str) -> dict[str, str]:
        pid = self._ids.get(project)
        if pid is None:
            return {}
        rows = self._db.execute(
            "SELECT path, fingerprint FROM hashes WHERE project_id = ?", (pid,)
        ).fetchall()
        return {str(path): str(fp) for path, fp in rows}

    def _put_clock(self, db: sqlite3.Connection, pid: int, state: ClockState, now: float) -> None:
        db.execute(
            """INSERT INTO clocks (project_id, watch_root, relative_root, clock, expr_hash,
                                   updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 watch_root = excluded.watch_root, relative_root = excluded.relative_root,
                 clock = excluded.clock, expr_hash = excluded.expr_hash,
                 updated_at = excluded.updated_at""",
            (pid, state.watch_root, state.relative_root, state.clock, state.expr_hash, now),
        )

    def record_observation(
        self,
        project: str,
        state: ClockState,
        obs: Observation,
        now: float,
        *,
        expect: int | None = None,
    ) -> list[Change]:
        """One transaction: journal rows + hash upserts/deletes + clock (§5 step 5).
        Raises UnknownProject for a project that is not configured, or whose
        current id is not ``expect`` (see project_token)."""
        pid = self._project_id(project, expect)
        created: list[Change] = []
        with self._tx() as db:
            for path, fp in obs.upserts.items():
                db.execute(
                    """INSERT INTO hashes (project_id, path, fingerprint) VALUES (?, ?, ?)
                       ON CONFLICT(project_id, path) DO UPDATE SET
                         fingerprint = excluded.fingerprint""",
                    (pid, path, fp),
                )
            for path in obs.deletes:
                db.execute("DELETE FROM hashes WHERE project_id = ? AND path = ?", (pid, path))
            for path, kind in obs.changes:
                cur = db.execute(
                    "INSERT INTO changes (project_id, path, kind, observed_at) VALUES (?, ?, ?, ?)",
                    (pid, path, str(kind), now),
                )
                created.append(Change(int(cur.lastrowid or 0), project, path, ChangeKind(kind)))
            self._put_clock(db, pid, state, now)
        return created

    def replace_baseline(
        self,
        project: str,
        state: ClockState,
        hashes: dict[str, str],
        now: float,
        *,
        expect: int | None = None,
        snapshots: Mapping[str, bytes] | None = None,
        snapshots_epoch: int | None = None,
    ) -> None:
        """Silent re-baseline: replace all fingerprints and the clock; journal nothing.
        Raises UnknownProject for a project that is not configured, or whose
        current id is not ``expect`` (see project_token).

        The project's snapshots are deleted first, like its hashes: a path that left
        the watch list must not keep a stale "before" (content snapshots spec §2.1).
        ``snapshots`` (read before this transaction) are then stored in path order
        within PROJECT_SNAPSHOT_BUDGET, without eviction; a file whose bytes no
        longer hash to its new fingerprint (it changed during the read) gets none.

        The flush is the only writer that advances snapshots from captured afters,
        so the baseline never stores one that could stand in for it: a path with a
        journal row not yet in an event gets none (the read may hold the bytes that
        row's flush will capture as its after; it falls back once), and with
        ``snapshots_epoch`` (snapshot_epoch, taken before the read) nothing is
        stored if any other snapshot write happened meanwhile (it may have stored
        newer bytes than the read's). Without ``snapshots_epoch`` (a caller with no
        read in flight) only the pending paths are left out."""
        pid = self._project_id(project, expect)
        with self._tx() as db:
            db.execute("DELETE FROM hashes WHERE project_id = ?", (pid,))
            db.executemany(
                "INSERT INTO hashes (project_id, path, fingerprint) VALUES (?, ?, ?)",
                [(pid, path, fp) for path, fp in hashes.items()],
            )
            self._put_clock(db, pid, state, now)
            db.execute("DELETE FROM snapshots WHERE project_id = ?", (pid,))
            if snapshots is not None:
                current = snapshots_epoch is None or self._snap_epoch.get(pid, 0) == snapshots_epoch
                pending = {
                    str(path)
                    for (path,) in db.execute(
                        "SELECT DISTINCT path FROM changes "
                        "WHERE project_id = ? AND event_id IS NULL",
                        (pid,),
                    )
                }
                used = 0
                for path in sorted(snapshots) if current else ():
                    data = snapshots[path]
                    if path in pending or hashes.get(path) != hashlib.sha1(data).hexdigest():
                        continue
                    if used + len(data) > PROJECT_SNAPSHOT_BUDGET:
                        continue
                    used += len(data)
                    db.execute(
                        "INSERT INTO snapshots (project_id, path, content, captured_at) "
                        "VALUES (?, ?, ?, ?)",
                        (pid, path, data, now),
                    )
                self._mark_snapshots_seen(db, now)

    # -- journal & outbox --------------------------------------------------

    def unflushed_changes(self) -> list[Change]:
        """Every journal row not yet in an event, under its project's current name."""
        rows = self._db.execute(
            f"SELECT c.id, p.name, c.path, c.kind FROM {_CHANGES_FROM} "
            "WHERE c.event_id IS NULL ORDER BY c.id"
        ).fetchall()
        return [Change(int(i), str(p), str(path), ChangeKind(k)) for i, p, path, k in rows]

    def split_by_project(self, changes: list[Change]) -> list[list[Change]]:
        """Journal rows grouped by their project (folder), in order of first
        appearance. Rows of two folders can share a name (one no longer configured,
        the other configured under its old name) and must never be collapsed or sent
        together. A row missing from the journal goes with the first group, so
        create_event reports it."""
        groups: dict[int | None, list[Change]] = {}
        owners = self._owners([c.id for c in changes])
        for change in changes:
            groups.setdefault(owners.get(change.id), []).append(change)
        missing = groups.pop(None, [])
        result = list(groups.values())
        if missing:
            if result:
                result[0].extend(missing)
            else:
                result.append(missing)
        return result

    def _owners(self, change_ids: Sequence[int]) -> dict[int, int]:
        """journal row id -> its project_id, for the rows that exist."""
        if not change_ids:
            return {}
        rows = self._db.execute(
            "SELECT id, project_id FROM changes WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(change_ids)),),
        )
        return {int(i): int(pid) for i, pid in rows}

    def create_event(
        self,
        project: str,
        kept: list[Change],
        superseded: list[int],
        now: float,
        *,
        contents: Sequence[Captured] | None = None,
        branch: str | None = None,
    ) -> int | None:
        """One transaction: delete superseded journal rows, create the pending event and
        link the kept rows. Returns None (and creates no event) if nothing is kept.

        The event belongs to its journal rows' project, whatever ``project`` (the
        batch's name when it was queued) says: a batch queued before a reload renamed
        or removed its project is still recorded, under the project's current name.
        Rows of more than one project raise ValueError (nothing written): callers
        split a batch with split_by_project first.

        With ``contents`` (content snapshots spec §2.2), the same transaction stores
        each path's before and after, the frozen note and ``branch``, advances the
        snapshots and enforces the budgets (_store_contents). ``contents`` None (no
        snapshotter) leaves snapshots and contents alone."""
        with self._tx() as db:
            kept_ids = {c.id for c in kept}
            db.executemany(
                "DELETE FROM changes WHERE id = ?",
                [(i,) for i in superseded if i not in kept_ids],
            )
            if not kept:
                return None
            owners = set(self._owners([c.id for c in kept]).values())
            if len(owners) > 1:
                raise ValueError("journal rows of more than one project in one event")
            if not owners:
                raise ValueError(f"journal row {kept[0].id} missing")
            owner = owners.pop()
            cur = db.execute(
                """INSERT INTO events (project_id, created_at, status, next_attempt_at)
                   VALUES (?, ?, 'pending', ?)""",
                (owner, now, now),
            )
            event_id = int(cur.lastrowid or 0)
            for change in kept:
                cur = db.execute(
                    "UPDATE changes SET event_id = ?, kind = ? WHERE id = ?",
                    (event_id, str(change.kind), change.id),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"journal row {change.id} missing")
            if contents is not None:
                self._store_contents(db, owner, event_id, kept, contents, branch, now)
        return event_id

    def _store_contents(
        self,
        db: sqlite3.Connection,
        pid: int,
        event_id: int,
        kept: list[Change],
        contents: Sequence[Captured],
        branch: str | None,
        now: float,
    ) -> None:
        """create_event's content step (spec §1, §2.2). Every path's ``before`` is read
        before any eviction runs, and this event's paths are exempt from it. Paths are
        filled in path order within EVENT_CONTENT_BUDGET (before plus after); past it,
        or marked over_budget by the capture, a path is stored as "uncapturable" with
        neither side. A path without a capture (redacted, or a project with no
        folder) stores nothing. Then `snapshots` advances: "content" upserts the
        after, anything else deletes the row, so the next event falls back instead of
        diffing a stale "before". A project removed from the config (a reload during
        the capture) gets no snapshot at all, only deletions: the sync already
        dropped its snapshots, and nothing may bring them back."""
        self._bump_snapshot_epoch(pid)
        row = db.execute("SELECT active FROM projects WHERE id = ?", (pid,)).fetchone()
        active = row is not None and bool(row[0])
        paths = sorted({c.path for c in kept})
        befores = {
            str(path): bytes(content)
            for path, content in db.execute(
                "SELECT path, content FROM snapshots WHERE project_id = ? "
                "AND path IN (SELECT value FROM json_each(?))",
                (pid, json.dumps(paths)),
            )
        }
        captured = {c.path: c for c in contents}
        used = 0
        for path in paths:
            cap = captured.get(path)
            if cap is None:
                db.execute("DELETE FROM snapshots WHERE project_id = ? AND path = ?", (pid, path))
                continue
            before = befores.get(path)
            after = cap.after if cap.after_state == "content" else None
            state: AfterState = cap.after_state
            cost = len(before or b"") + len(after or b"")
            if cap.over_budget or used + cost > EVENT_CONTENT_BUDGET:
                before, after, state = None, None, "uncapturable"
            else:
                used += cost
            db.execute(
                "INSERT INTO event_contents (event_id, path, before, after, after_state, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, path, before, after, state, cap.note),
            )
            if state == "content" and active:
                db.execute(
                    """INSERT INTO snapshots (project_id, path, content, captured_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(project_id, path) DO UPDATE SET
                         content = excluded.content, captured_at = excluded.captured_at""",
                    (pid, path, after, now),
                )
            else:
                db.execute("DELETE FROM snapshots WHERE project_id = ? AND path = ?", (pid, path))
        db.execute("INSERT INTO event_meta (event_id, branch) VALUES (?, ?)", (event_id, branch))
        self._evict_snapshots(db, pid, set(paths))
        self._evict_contents(db, pid, event_id)
        self._mark_snapshots_seen(db, now)

    def _evict_snapshots(self, db: sqlite3.Connection, pid: int, exempt: set[str]) -> None:
        """Files that change win over files that don't (spec §1): over
        PROJECT_SNAPSHOT_BUDGET, the least recently captured snapshots of other
        files go first, by (captured_at, path). An evicted file's next event falls
        back once."""
        row = db.execute(
            "SELECT COALESCE(SUM(length(content)), 0) FROM snapshots WHERE project_id = ?",
            (pid,),
        ).fetchone()
        total = int(row[0])
        if total <= PROJECT_SNAPSHOT_BUDGET:
            return
        rows = db.execute(
            "SELECT path, length(content) FROM snapshots WHERE project_id = ? "
            "ORDER BY captured_at, path",
            (pid,),
        ).fetchall()
        for path, size in rows:
            if total <= PROJECT_SNAPSHOT_BUDGET:
                break
            if path in exempt:
                continue
            db.execute("DELETE FROM snapshots WHERE project_id = ? AND path = ?", (pid, path))
            total -= int(size)

    def _evict_contents(self, db: sqlite3.Connection, pid: int, event_id: int) -> None:
        """PROJECT_EVENT_CONTENT_BUDGET over all of a project's stored pairs (spec §1):
        other events' contents are deleted until it fits, settled events (sent,
        skipped) first, oldest first by event id; only then waiting ones (pending,
        held, failed), oldest first. Their event_reports stay."""
        sizes = db.execute(
            f"""SELECT ec.event_id,
                      e.status IN ({", ".join("?" * len(SETTLED_STATUSES))}),
                      SUM(COALESCE(length(ec.before), 0) + COALESCE(length(ec.after), 0))
               FROM event_contents ec JOIN events e ON e.id = ec.event_id
               WHERE e.project_id = ? GROUP BY ec.event_id""",
            (*SETTLED_STATUSES, pid),
        ).fetchall()
        total = sum(int(size) for _, _, size in sizes)
        if total <= PROJECT_EVENT_CONTENT_BUDGET:
            return
        victims = sorted(
            ((int(eid), bool(settled), int(size)) for eid, settled, size in sizes),
            key=lambda v: (not v[1], v[0]),
        )
        for eid, _, size in victims:
            if total <= PROJECT_EVENT_CONTENT_BUDGET:
                break
            if eid == event_id:
                continue
            db.execute("DELETE FROM event_contents WHERE event_id = ?", (eid,))
            total -= size

    def snapshot_epoch(self, project: str) -> int:
        """The configured project's snapshot epoch: it moves with every snapshot
        write but replace_baseline's (sync, create_event, forget_redacted, the
        downgrade guard). The watcher takes it before a baseline read and passes it
        back as ``snapshots_epoch``. A name that is not configured reads 0, like
        any other read (its replace_baseline raises UnknownProject anyway)."""
        pid = self._ids.get(project)
        return 0 if pid is None else self._snap_epoch.get(pid, 0)

    def _bump_snapshot_epoch(self, pid: int) -> None:
        self._snap_epoch[pid] = self._snap_epoch.get(pid, 0) + 1

    def _bump_all_snapshot_epochs(self, db: sqlite3.Connection) -> None:
        """For the writers that touch every project's snapshots: conservative, a
        bump only costs a baseline in flight its snapshots (they fall back once)."""
        for (pid,) in db.execute("SELECT id FROM projects"):
            self._bump_snapshot_epoch(int(pid))

    def _mark_snapshots_seen(self, db: sqlite3.Connection, now: float) -> None:
        db.execute(
            """INSERT INTO kv (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (SNAPSHOTS_SEEN, repr(now)),
        )

    def mark_snapshots_seen(self, now: float) -> None:
        """The maintenance tick's liveness floor for the downgrade guard."""
        with self._tx() as db:
            self._mark_snapshots_seen(db, now)

    def drop_stale_snapshots(self, now: float) -> int:
        """The downgrade guard, at daemon start (spec §2.5): if any clock was advanced
        more than SNAPSHOT_GAP after this code last wrote snapshots, older code ran in
        between without advancing them, and every snapshot is dropped (the next
        events fall back once). Records ``now`` as the new mark. Returns the number
        of snapshots dropped."""
        with self._tx() as db:
            row = db.execute("SELECT value FROM kv WHERE key = ?", (SNAPSHOTS_SEEN,)).fetchone()
            newest = db.execute("SELECT MAX(updated_at) FROM clocks").fetchone()[0]
            seen = None if row is None else float(row[0])
            dropped = 0
            if newest is not None and (seen is None or float(newest) > seen + SNAPSHOT_GAP):
                dropped = db.execute("DELETE FROM snapshots").rowcount
                self._bump_all_snapshot_epochs(db)
            self._mark_snapshots_seen(db, now)
        return dropped

    def forget_redacted(self, redact: Sequence[str]) -> int:
        """Delete the snapshots and stored contents of every path ``redact`` covers
        (spec §2.5: a redaction list that grew). A file that is redacted now keeps no
        content at rest. Returns the number of rows deleted."""
        with self._tx() as db:
            paths = [
                str(path)
                for (path,) in db.execute(
                    "SELECT path FROM snapshots UNION SELECT path FROM event_contents"
                )
            ]
            deleted = 0
            self._bump_all_snapshot_epochs(db)
            for path in paths:
                if is_redacted(path, redact):
                    deleted += db.execute("DELETE FROM snapshots WHERE path = ?", (path,)).rowcount
                    deleted += db.execute(
                        "DELETE FROM event_contents WHERE path = ?", (path,)
                    ).rowcount
        return deleted

    def owner(self, changes: Sequence[Change]) -> tuple[str, bool] | None:
        """(current name, configured now) of the project these journal rows belong
        to, by the first row that exists; None if none does. The notifier reads files
        only in a configured project's folder."""
        owners = self._owners([c.id for c in changes])
        pid = next((owners[c.id] for c in changes if c.id in owners), None)
        if pid is None:
            return None
        row = self._db.execute("SELECT name, active FROM projects WHERE id = ?", (pid,))
        found = row.fetchone()
        return None if found is None else (str(found[0]), bool(found[1]))

    def event_contents(self, event_id: int) -> dict[str, StoredContent]:
        """The event's stored before/after pairs by path ({} once pruned or evicted)."""
        rows = self._db.execute(
            "SELECT path, before, after, after_state, note FROM event_contents WHERE event_id = ?",
            (event_id,),
        )
        return {
            str(path): StoredContent(
                str(path),
                None if before is None else bytes(before),
                None if after is None else bytes(after),
                cast(AfterState, str(state)),  # guarded by the table's CHECK
                None if note is None else str(note),
            )
            for path, before, after, state, note in rows
        }

    def event_meta(self, event_id: int) -> EventMeta | None:
        """The context frozen at creation; None for an event created without contents
        (or once pruned)."""
        row = self._db.execute(
            "SELECT branch FROM event_meta WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else EventMeta(None if row[0] is None else str(row[0]))

    def get_snapshots(self, project: str) -> dict[str, bytes]:
        """A configured project's current snapshots by path (empty for any other)."""
        pid = self._ids.get(project)
        if pid is None:
            return {}
        rows = self._db.execute(
            "SELECT path, content FROM snapshots WHERE project_id = ?", (pid,)
        ).fetchall()
        return {str(path): bytes(content) for path, content in rows}

    def event_changes(self, event_id: int) -> list[Change]:
        rows = self._db.execute(
            f"SELECT c.id, p.name, c.path, c.kind FROM {_CHANGES_FROM} "
            "WHERE c.event_id = ? ORDER BY c.id",
            (event_id,),
        ).fetchall()
        return [Change(int(i), str(p), str(path), ChangeKind(k)) for i, p, path, k in rows]

    def get_event(self, event_id: int) -> EventRow:
        row = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM {_EVENTS_FROM} WHERE e.id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return _event(row)

    def due_events(self, now: float) -> list[EventRow]:
        rows = self._db.execute(
            f"""SELECT {_EVENT_COLUMNS} FROM {_EVENTS_FROM}
                WHERE e.status = 'pending' AND e.next_attempt_at <= ? ORDER BY e.id""",
            (now,),
        ).fetchall()
        return [_event(row) for row in rows]

    def recent_events(self, limit: int = 20) -> list[EventRow]:
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM {_EVENTS_FROM} ORDER BY e.id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_event(row) for row in rows]

    def mark_sent(self, event_id: int, message_id: int, note: str | None = None) -> None:
        """``note`` (why the plain-text fallback was sent) is kept in last_error, but
        it is no error: its kind and time are cleared."""
        self._db.execute(
            "UPDATE events SET status = 'sent', telegram_message_id = ?, last_error = ?, "
            "last_error_kind = NULL, last_error_at = NULL WHERE id = ?",
            (message_id, note, event_id),
        )

    def mark_retry(
        self, event_id: int, error: str, next_attempt_at: float, *, kind: str | None, at: float
    ) -> None:
        """``kind``: the error's ErrorKind value; ``at``: when it happened (settings
        redesign spec §7). Both are stored with the error."""
        self._db.execute(
            "UPDATE events SET attempts = attempts + 1, last_error = ?, last_error_kind = ?, "
            "last_error_at = ?, next_attempt_at = ? WHERE id = ?",
            (error, kind, at, next_attempt_at, event_id),
        )

    def mark_failed(self, event_id: int, error: str, *, kind: str | None, at: float) -> None:
        self._db.execute(
            "UPDATE events SET status = 'failed', attempts = attempts + 1, last_error = ?, "
            "last_error_kind = ?, last_error_at = ? WHERE id = ?",
            (error, kind, at, event_id),
        )

    # -- held and skipped events, catch-up batches (notify controls spec §3.2) --

    def mark_held(self, event_id: int, until: float, reason: str) -> None:
        """Hold a pending event until its pause ends (``until``: HELD_FOREVER for an
        indefinite snooze, or while Telegram is not set up). ``reason`` is "snooze",
        "quiet" or "setup". The retry state starts over: an error from before the hold
        is not the catch-up's. Only a pending event is held."""
        self._db.execute(
            "UPDATE events SET status = 'held', attempts = 0, last_error = NULL, "
            "last_error_kind = NULL, last_error_at = NULL, "
            "next_attempt_at = ?, held_reason = ? WHERE id = ? AND status = 'pending'",
            (until, reason, event_id),
        )

    def mark_skipped(self, event_ids: Sequence[int]) -> None:
        """A notify_on rule left nothing to send: recorded, never notified. Acts on
        pending events and on held ones (a catch-up the current rules empty). An
        error from an earlier attempt no longer applies: it is cleared."""
        with self._tx() as db:
            db.executemany(
                "UPDATE events SET status = 'skipped', last_error = NULL, "
                "last_error_kind = NULL, last_error_at = NULL, attempts = 0 "
                "WHERE id = ? AND status IN ('pending', 'held')",
                [(i,) for i in event_ids],
            )

    def due_held(self, now: float) -> list[EventRow]:
        """Held events whose pause has ended, oldest first."""
        rows = self._db.execute(
            f"""SELECT {_EVENT_COLUMNS} FROM {_EVENTS_FROM}
                WHERE e.status = 'held' AND e.next_attempt_at <= ? ORDER BY e.id""",
            (now,),
        ).fetchall()
        return [_event(row) for row in rows]

    def reschedule_held(self, event_ids: Sequence[int], until: float, reason: str) -> None:
        """Still paused at catch-up time: hold again until the new end. A new hold:
        the attempts and error of an earlier failed catch-up are cleared, so the
        Telegram health doesn't keep reporting a stale error while it waits."""
        with self._tx() as db:
            db.executemany(
                "UPDATE events SET next_attempt_at = ?, held_reason = ?, attempts = 0, "
                "last_error = NULL, last_error_kind = NULL, last_error_at = NULL "
                "WHERE id = ? AND status = 'held'",
                [(until, reason, i) for i in event_ids],
            )

    def release_held(
        self, now: float, *, reason: str | None = None, project: str | None = None
    ) -> int:
        """Make held events due now, so the next catch-up check re-evaluates their
        pause (and holds again whatever is still paused). Only those with this
        reason, or of this project, when given (none for a project that is not
        configured). Returns how many."""
        sql = "UPDATE events SET next_attempt_at = ? WHERE status = 'held'"
        params: list[object] = [now]
        if reason is not None:
            sql += " AND held_reason = ?"
            params.append(reason)
        if project is not None:
            pid = self._ids.get(project)
            if pid is None:
                return 0
            sql += " AND project_id = ?"
            params.append(pid)
        return self._db.execute(sql, params).rowcount

    def mark_batch_sent(
        self,
        event_ids: Sequence[int],
        message_id: int,
        batch_id: int,
        note: str | None = None,
    ) -> None:
        """Acts on held events only: the plain-text fallback of a rejected catch-up
        is sent while the group is still held, so it never meets a failed one. The
        note is no error: its kind and time are cleared."""
        with self._tx() as db:
            db.executemany(
                "UPDATE events SET status = 'sent', telegram_message_id = ?, batch_id = ?, "
                "last_error = ?, last_error_kind = NULL, last_error_at = NULL "
                "WHERE id = ? AND status = 'held'",
                [(message_id, batch_id, note, i) for i in event_ids],
            )

    def mark_batch_retry(
        self,
        event_ids: Sequence[int],
        error: str,
        next_attempt_at: float,
        *,
        kind: str | None,
        at: float,
    ) -> None:
        """A retryable error: the group stays held and is tried again later."""
        with self._tx() as db:
            db.executemany(
                "UPDATE events SET attempts = attempts + 1, last_error = ?, "
                "last_error_kind = ?, last_error_at = ?, next_attempt_at = ? "
                "WHERE id = ? AND status = 'held'",
                [(error, kind, at, next_attempt_at, i) for i in event_ids],
            )

    def mark_batch_failed(
        self,
        event_ids: Sequence[int],
        error: str,
        batch_id: int,
        *,
        kind: str | None,
        at: float,
    ) -> None:
        with self._tx() as db:
            db.executemany(
                "UPDATE events SET status = 'failed', attempts = attempts + 1, last_error = ?, "
                "last_error_kind = ?, last_error_at = ?, batch_id = ? "
                "WHERE id = ? AND status = 'held'",
                [(error, kind, at, batch_id, i) for i in event_ids],
            )

    def batch_size(self, batch_id: int) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM events WHERE batch_id = ?", (batch_id,))
        return int(row.fetchone()[0])

    # -- snoozes ---------------------------------------------------------------

    def set_snooze(self, project: str, since: float, until: float | None) -> Snooze:
        """Snooze a project (replacing any snooze). Its snooze-held events become due
        now, so a shortened snooze is re-evaluated at the next catch-up check. Raises
        UnknownProject for a project that is not configured."""
        pid = self._project_id(project)
        with self._tx() as db:
            db.execute(
                """INSERT INTO snoozes (project_id, since, until) VALUES (?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET since = excluded.since,
                                                         until = excluded.until""",
                (pid, since, until),
            )
            db.execute(
                "UPDATE events SET next_attempt_at = ? "
                "WHERE status = 'held' AND held_reason = 'snooze' AND project_id = ?",
                (since, pid),
            )
        return Snooze(project, since, until)

    def get_snooze(self, project: str) -> Snooze | None:
        """The row as stored: it may have expired (see Snooze.active)."""
        pid = self._ids.get(project)
        if pid is None:
            return None
        row = self._db.execute(
            "SELECT since, until FROM snoozes WHERE project_id = ?", (pid,)
        ).fetchone()
        if row is None:
            return None
        return Snooze(project, float(row[0]), None if row[1] is None else float(row[1]))

    def active_snoozes(self, now: float) -> dict[str, Snooze]:
        """The configured projects' snoozes still in force, by name."""
        names = self._names()
        rows = self._db.execute("SELECT project_id, since, until FROM snoozes")
        snoozes = (
            Snooze(names[int(pid)], float(since), None if until is None else float(until))
            for pid, since, until in rows
            if int(pid) in names
        )
        return {s.project: s for s in snoozes if s.active(now)}

    def delete_snooze(self, project: str, now: float) -> None:
        """Turn a project back on: its snooze-held events become due now. Nothing to
        do for a project that is not configured (drop_snoozes_except already dropped
        its snooze), so the web's unsnooze stays idempotent."""
        pid = self._ids.get(project)
        if pid is None:
            return
        with self._tx() as db:
            db.execute("DELETE FROM snoozes WHERE project_id = ?", (pid,))
            db.execute(
                "UPDATE events SET next_attempt_at = ? "
                "WHERE status = 'held' AND held_reason = 'snooze' AND project_id = ?",
                (now, pid),
            )

    def drop_snoozes_except(self, projects: Collection[str], now: float) -> list[str]:
        """Delete the snoozes of projects no longer configured (removed; a renamed
        project keeps its id and its snooze) and make their snooze-held events due
        now. ``projects`` are configured names. Returns the dropped projects' names."""
        keep = {self._ids[name] for name in projects if name in self._ids}
        with self._tx() as db:
            rows = db.execute(
                "SELECT s.project_id, p.name FROM snoozes s JOIN projects p ON p.id = s.project_id"
            ).fetchall()
            dropped = [(int(pid), str(name)) for pid, name in rows if int(pid) not in keep]
            for pid, _ in dropped:
                db.execute("DELETE FROM snoozes WHERE project_id = ?", (pid,))
                db.execute(
                    "UPDATE events SET next_attempt_at = ? "
                    "WHERE status = 'held' AND held_reason = 'snooze' AND project_id = ?",
                    (now, pid),
                )
        return [name for _, name in dropped]

    def count_events(self, since: float, until: float) -> int:
        """Events created in [since, until): the heartbeat's "changes yesterday"."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM events WHERE created_at >= ? AND created_at < ?",
            (since, until),
        ).fetchone()
        return int(row[0])

    def activity(
        self,
        now: float,
        projects: Collection[str],
        days: int = ACTIVITY_DAYS,
        *,
        tz: tzinfo | None = None,
    ) -> dict[str, list[DayCount]]:
        """Events per local day for each of ``projects`` (the configured ones; any
        other project's events are left out), exactly ``days`` entries each, oldest
        first, gaps filled with zeros. Every status counts in ``total``; ``failed``
        counts status 'failed'. One query (ACTIVITY_SQL). ``days`` must be at least 1.
        A name that is not configured gets zeros."""
        if days < 1:
            raise ValueError(f"activity needs at least one day, got days={days}")
        bounds = day_bounds(now, days, tz)
        pairs = json.dumps([[since, until] for _, since, until in bounds])
        ids = {name: self._ids.get(name, 0) for name in projects}  # 0: never an id
        found: dict[tuple[int, int], tuple[int, int]] = {}
        for pid, index, total, failed in self._db.execute(
            ACTIVITY_SQL, (pairs, bounds[0][1], bounds[-1][2], json.dumps(list(ids.values())))
        ):
            found[(int(pid), int(index))] = (int(total), int(failed or 0))
        return {
            name: [
                DayCount(day, since, until, *found.get((ids[name], i), (0, 0)))
                for i, (day, since, until) in enumerate(bounds)
            ]
            for name in projects
        }

    def last_event_times(self) -> dict[str, float]:
        """created_at of each configured project's newest event: the dashboard's "last
        change", read from the outbox so it survives a daemon restart. Keyed by the
        configured name: a renamed project keeps its id, so its last change stays."""
        names = self._names()
        rows = self._db.execute(
            "SELECT project_id, MAX(created_at) FROM events GROUP BY project_id"
        )
        return {names[int(pid)]: float(at) for pid, at in rows if int(pid) in names}

    def status_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "sent": 0, "failed": 0}
        for status, n in self._db.execute("SELECT status, COUNT(*) FROM events GROUP BY status"):
            counts[str(status)] = int(n)
        return counts

    def prune(self, older_than: float) -> int:
        """Delete the stored reports (event_reports, batch_reports) of every event
        created before a cutoff, whatever its status, so diffs at rest stay bounded
        even while a project is snoozed. Then delete those events unless they are
        still to be sent (pending or held), with their changes by cascade. Returns
        the number of events deleted. A catch-up batch is aged by its newest member and
        its still-open ones: it is kept whole (events and its batch_reports row) while
        any member is kept, so a Retry re-holds every member."""
        kept_batches = (
            "SELECT batch_id FROM events WHERE batch_id IS NOT NULL "
            "AND (created_at >= ? OR status IN ('pending', 'held'))"
        )
        with self._tx() as db:
            old = "SELECT id FROM events WHERE created_at < ?"
            db.execute(f"DELETE FROM event_reports WHERE event_id IN ({old})", (older_than,))
            # The contents a diff is built from go with it (content snapshots spec §5).
            db.execute(f"DELETE FROM event_contents WHERE event_id IN ({old})", (older_than,))
            db.execute(f"DELETE FROM event_meta WHERE event_id IN ({old})", (older_than,))
            db.execute(
                f"DELETE FROM batch_reports WHERE batch_id IN ({old}) "
                f"AND batch_id NOT IN ({kept_batches})",
                (older_than, older_than),
            )
            cur = db.execute(
                "DELETE FROM events WHERE created_at < ? AND status NOT IN ('pending', 'held') "
                f"AND (batch_id IS NULL OR batch_id NOT IN ({kept_batches}))",
                (older_than, older_than),
            )
            return cur.rowcount

    # -- stored reports (event_reports) ------------------------------------

    def put_event_report(
        self,
        event_id: int,
        branch: str | None,
        reports: Sequence[FileReport],
        *,
        telegram_lines: int,
        telegram_files: int,
        rule_skipped: Collection[str] = (),
    ) -> None:
        """Store (or replace) the redacted report a delivery attempt was built from."""
        encoded = encode_reports(reports, rule_skipped=rule_skipped)
        self._db.execute(
            """INSERT OR REPLACE INTO event_reports
               (event_id, branch, reports, added, removed, size, telegram_lines, telegram_files)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                branch,
                encoded.text,
                encoded.added,
                encoded.removed,
                len(encoded.text.encode()),
                telegram_lines,
                telegram_files,
            ),
        )

    def set_report_telegram(self, event_id: int, *, lines: int, files: int) -> None:
        """Record what the message that was actually sent contained (the plain-text
        fallback may differ from the HTML attempt)."""
        self._db.execute(
            "UPDATE event_reports SET telegram_lines = ?, telegram_files = ? WHERE event_id = ?",
            (lines, files, event_id),
        )

    def get_event_report(self, event_id: int) -> StoredReport | None:
        row = self._db.execute(
            """SELECT branch, reports, added, removed, telegram_lines, telegram_files
               FROM event_reports WHERE event_id = ?""",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        branch, text, added, removed, lines, files = row
        return StoredReport(
            None if branch is None else str(branch),
            json.loads(text),
            int(added),
            int(removed),
            int(lines),
            int(files),
        )

    # -- catch-up message copies (batch_reports) ------------------------------

    def put_batch_report(
        self,
        batch_id: int,
        reports: Sequence[FileReport],
        *,
        telegram_lines: int,
        telegram_files: int,
    ) -> None:
        """Store (or replace) the merged, redacted report a catch-up message was built
        from, keyed by the batch's first event id. Same format and caps as an event's."""
        encoded = encode_reports(reports)
        self._db.execute(
            """INSERT OR REPLACE INTO batch_reports
               (batch_id, reports, added, removed, size, telegram_lines, telegram_files)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_id,
                encoded.text,
                encoded.added,
                encoded.removed,
                len(encoded.text.encode()),
                telegram_lines,
                telegram_files,
            ),
        )

    def set_batch_report_telegram(self, batch_id: int, *, lines: int, files: int) -> None:
        self._db.execute(
            "UPDATE batch_reports SET telegram_lines = ?, telegram_files = ? WHERE batch_id = ?",
            (lines, files, batch_id),
        )

    def get_batch_report(self, batch_id: int) -> StoredReport | None:
        row = self._db.execute(
            """SELECT reports, added, removed, telegram_lines, telegram_files
               FROM batch_reports WHERE batch_id = ?""",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        text, added, removed, lines, files = row
        return StoredReport(
            None, json.loads(text), int(added), int(removed), int(lines), int(files)
        )

    # -- health: problems and kv (health spec §4.1) ---------------------------------

    def get_problem(self, key: str) -> Problem | None:
        row = self._db.execute(
            f"SELECT {_PROBLEM_COLUMNS} FROM problems WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else _problem(row)

    def all_problems(self) -> list[Problem]:
        """Every row, open, resolved-but-unsent and notices: oldest first."""
        rows = self._db.execute(
            f"SELECT {_PROBLEM_COLUMNS} FROM problems ORDER BY opened_at, key"
        ).fetchall()
        return [_problem(row) for row in rows]

    def put_problem(self, problem: Problem) -> None:
        """Insert the row, or replace every column of the row with its key."""
        self._db.execute(
            f"""INSERT INTO problems ({_PROBLEM_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 severity = excluded.severity, project = excluded.project,
                 message = excluded.message, fix = excluded.fix,
                 opened_at = excluded.opened_at, notified_open = excluded.notified_open,
                 attempts = excluded.attempts, next_try_at = excluded.next_try_at,
                 last_error = excluded.last_error, resolved_at = excluded.resolved_at""",
            (
                problem.key,
                problem.severity,
                problem.project,
                problem.message,
                problem.fix,
                problem.opened_at,
                int(problem.notified_open),
                problem.attempts,
                problem.next_try_at,
                problem.last_error,
                problem.resolved_at,
            ),
        )

    def delete_problem(self, key: str) -> None:
        self._db.execute("DELETE FROM problems WHERE key = ?", (key,))

    def kv_get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def kv_set(self, key: str, value: str) -> None:
        self._db.execute(
            """INSERT INTO kv (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )

    def kv_delete(self, key: str) -> None:
        self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -- web activity feed ---------------------------------------------------

    def query_events(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        redact: tuple[str, ...] | None = None,
        limit: int = 50,
        offset: int = 0,
        before_id: int | None = None,
    ) -> tuple[list[EventDetail], int]:
        """Newest first. ``q`` is a case-insensitive (ASCII) substring of a change path
        or, given today's redaction patterns ``redact`` and at least DIFF_SEARCH_MIN
        characters, of a stored diff line of a file ``redact`` does not cover (diff
        readability spec §2.1); such rows carry ``q_in_diff``. Without ``redact`` only
        paths are searched. ``status`` "attention" selects ATTENTION_STATUSES (failed
        and held). ``since`` is inclusive and ``until`` exclusive (epoch
        seconds). ``before_id`` is a keyset cursor: only rows with a smaller id are
        returned. ``total`` counts the filtered rows without the cursor. ``q`` is
        trimmed first (a whitespace-only ``q`` is no ``q``): the length rule, both
        LIKE patterns and the memo key all use the trimmed text."""
        q = (q or "").strip() or None
        where: list[str] = []
        params: list[object] = []
        if project:
            pid = self._ids.get(project)
            if pid is None:  # not configured: a read by name finds nothing
                return [], 0
            where.append("e.project_id = ?")
            params.append(pid)
        if status == ATTENTION:
            where.append(f"e.status IN ({', '.join('?' * len(ATTENTION_STATUSES))})")
            params.extend(ATTENTION_STATUSES)
        elif status:
            where.append("e.status = ?")
            params.append(status)
        if since is not None:
            where.append("e.created_at >= ?")
            params.append(since)
        if until is not None:
            where.append("e.created_at < ?")
            params.append(until)
        in_diff: frozenset[int] = frozenset()
        if q:
            path_match = (
                "EXISTS (SELECT 1 FROM changes c WHERE c.event_id = e.id "
                "AND c.path LIKE ? ESCAPE '\\')"
            )
            params.append(f"%{_like_escape(q)}%")
            if redact is not None and len(q) >= DIFF_SEARCH_MIN:
                in_diff = self._diff_search(q, redact)
                # One JSON array parameter, whatever the number of ids
                # (SQLITE_MAX_VARIABLE_NUMBER never applies).
                where.append(f"({path_match} OR e.id IN (SELECT value FROM json_each(?)))")
                params.append(json.dumps(sorted(in_diff)))
            else:
                where.append(path_match)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(
            self._db.execute(f"SELECT COUNT(*) FROM events e {clause}", params).fetchone()[0]
        )
        if before_id is not None:
            where.append("e.id < ?")
            params.append(before_id)
            clause = f"WHERE {' AND '.join(where)}"
        rows = self._db.execute(
            f"SELECT {_EVENT_COLUMNS} FROM {_EVENTS_FROM} {clause} "
            "ORDER BY e.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        events = [_event(row) for row in rows]
        by_event: dict[int, list[Change]] = {e.id: [] for e in events}
        summaries: dict[int, ReportSummary] = {}
        if events:
            marks = ",".join("?" * len(events))
            for i, p, path, kind, event_id in self._db.execute(
                f"SELECT c.id, p.name, c.path, c.kind, c.event_id FROM {_CHANGES_FROM} "
                f"WHERE c.event_id IN ({marks}) ORDER BY c.id",
                list(by_event),
            ):
                by_event[int(event_id)].append(Change(int(i), str(p), str(path), ChangeKind(kind)))
            for event_id, branch, added, removed in self._db.execute(
                f"SELECT event_id, branch, added, removed FROM event_reports "
                f"WHERE event_id IN ({marks})",
                list(by_event),
            ):
                summaries[int(event_id)] = ReportSummary(
                    None if branch is None else str(branch), int(added), int(removed)
                )
        return [
            EventDetail(e, by_event[e.id], summaries.get(e.id), e.id in in_diff) for e in events
        ], total

    def _diff_search(self, q: str, redact: tuple[str, ...]) -> frozenset[int]:
        """The ids of the events with a stored diff line containing ``q`` in a file
        today's ``redact`` does not cover. Memoized per ``q`` while the redaction list
        and the reports epoch (the triggers' kv counter) stay the same, so the page's
        5 s poll does not rescan every stored line."""
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (REPORTS_EPOCH,)).fetchone()
        epoch = 0 if row is None else int(row[0])
        cached = self._diff_memo.get(q)
        if cached is not None and cached[0] == redact and cached[1] == epoch:
            self._diff_memo.move_to_end(q)
            return cached[2]
        ids = self._scan_diff_lines(q, redact)
        self._diff_memo[q] = (redact, epoch, ids)
        self._diff_memo.move_to_end(q)
        while len(self._diff_memo) > DIFF_SEARCH_MEMO:
            self._diff_memo.popitem(last=False)
        return ids

    def _scan_diff_lines(self, q: str, redact: tuple[str, ...]) -> frozenset[int]:
        """Steps 1 and 2: the matching (event, path) pairs from SQL, then the pairs
        whose path today's redaction list covers are dropped (the same check the
        detail applies before returning a stored report, ADR 0010), so the search is
        never an oracle for a now-redacted file's contents."""
        covered: dict[str, bool] = {}
        ids: set[int] = set()
        for event_id, path in self._db.execute(DIFF_SEARCH_SQL, (f"%{_like_escape(q)}%",)):
            name = str(path)
            if name not in covered:
                covered[name] = is_redacted(name, redact)
            if not covered[name]:
                ids.add(int(event_id))
        return frozenset(ids)

    def retry_event(self, event_id: int, now: float) -> bool:
        """Re-queue a failed event for the maintenance retry loop. A failed catch-up
        goes back as a whole: every failed event of its batch is held again, due now
        with its reason kept, so it is sent as one catch-up message, never as one
        message per event. Returns False (and changes nothing) unless the event
        exists and is ``failed``."""
        with self._tx() as db:
            row = db.execute("SELECT status, batch_id FROM events WHERE id = ?", (event_id,))
            found = row.fetchone()
            if found is None or found[0] != "failed":
                return False
            if found[1] is None:
                db.execute(
                    "UPDATE events SET status = 'pending', attempts = 0, next_attempt_at = ? "
                    "WHERE id = ?",
                    (now, event_id),
                )
            else:
                db.execute(
                    "UPDATE events SET status = 'held', attempts = 0, next_attempt_at = ?, "
                    "batch_id = NULL WHERE batch_id = ? AND status = 'failed'",
                    (now, found[1]),
                )
            return True

    def telegram_health(self) -> tuple[float | None, str | None]:
        """(created_at of the newest sent event, the newest delivery error if it is
        newer than that). Read from the outbox: no Bot API call. A catch-up stuck on
        retryable errors (held, attempts > 0) is reported whatever was sent since: a held
        event is older than any send made after it. An error recorded before the last
        bot save (kv TELEGRAM_SAVED_AT; a row written before v7 has no time and counts as
        older) is the replaced bot's: history, as in telegram_failure. So the header,
        the daemon's `telegram` warning and the card agree after Replace bot."""
        row = self._db.execute("SELECT MAX(created_at) FROM events WHERE status = 'sent'")
        last_ok = row.fetchone()[0]
        last_ok_at = None if last_ok is None else float(last_ok)
        saved_at = _float_or_none(self.kv_get(TELEGRAM_SAVED_AT))
        stuck = self._db.execute(
            """SELECT last_error FROM events
               WHERE status = 'held' AND attempts > 0 AND last_error IS NOT NULL
                 AND (? IS NULL OR last_error_at > ?)
               ORDER BY id DESC LIMIT 1""",
            (saved_at, saved_at),
        ).fetchone()
        if stuck is not None:
            return last_ok_at, str(stuck[0])
        err = self._db.execute(
            """SELECT created_at, last_error FROM events
               WHERE last_error IS NOT NULL
                 AND (status = 'failed' OR (status = 'pending' AND attempts > 0))
                 AND (? IS NULL OR last_error_at > ?)
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (saved_at, saved_at),
        ).fetchone()
        last_error: str | None = None
        if err is not None and (last_ok_at is None or float(err[0]) > last_ok_at):
            last_error = str(err[1])
        return last_ok_at, last_error

    def telegram_failure(self) -> TelegramFailure | None:
        """The Settings card's failing state (settings redesign spec §7), from the
        outbox: None unless telegram_health reports an error. Over the undelivered
        events (failed, or pending/held with attempts) whose error was recorded after
        the cutoff: the later of the last bot save (kv TELEGRAM_SAVED_AT, written by
        complete_setup) and the last successful send (telegram_health's last_ok_at,
        the newest sent event's created_at: the schema keeps no delivery time). An
        error older than either is history, not the current failure. With neither,
        nothing is filtered; with a cutoff, a row with no last_error_at (written
        before v7) counts as older.

        ``kind`` by precedence (auth > bad_request = legacy_failed > retryable; a tie
        goes to the newest error), so a revoked token outranks a newer network blip;
        ``error`` is the newest text of that kind (newest by last_error_at). A row with
        no kind (written before v7) is legacy_failed when failed, retryable while
        retried. The status filter keeps out a re-queued row (attempts 0, old kind
        kept) and a sent row's fallback note.

        KNOWN LIMIT (ADR 0019, Consequences): with no delivery time, last_ok_at is the
        sent event's created_at, so after a long-held event is delivered late the
        cutoff can be earlier than that delivery and an error recorded in between may
        be over-reported. It fails safe (it never hides a current failure, and the
        telegram_health gate still needs an error); a sent_at column would fix it."""
        last_ok_at, last_error = self.telegram_health()
        if last_error is None:
            return None
        cutoffs = [
            t for t in (_float_or_none(self.kv_get(TELEGRAM_SAVED_AT)), last_ok_at) if t is not None
        ]
        cutoff = max(cutoffs) if cutoffs else None
        rows = self._db.execute(
            f"""SELECT status, created_at, last_error, last_error_kind FROM events
                WHERE ({_UNDELIVERED}) AND (? IS NULL OR last_error_at > ?)
                ORDER BY last_error_at IS NULL, last_error_at DESC, created_at DESC,
                         id DESC""",
            (cutoff, cutoff),
        ).fetchall()
        if not rows:
            return None
        kinds = [_failure_kind(str(status), kind) for status, _, _, kind in rows]
        best = max(kinds, key=lambda k: _FAILURE_RANK[k])  # the first maximum: the newest
        error = next(
            (
                str(row[2])
                for row, kind in zip(rows, kinds, strict=True)
                if kind == best and row[2] is not None
            ),
            "",
        )
        failed = sum(1 for row in rows if row[0] == "failed")
        since = min(float(row[1]) for row in rows)
        return TelegramFailure(best, since, failed, len(rows) - failed, error)

    # -- web logins, sessions and requests ------------------------------------

    def add_login(self, token_hash: str, expires_at: float, secret_hash: str) -> None:
        self._db.execute(
            "INSERT INTO web_logins (token_hash, expires_at, secret_hash) VALUES (?, ?, ?)",
            (token_hash, expires_at, secret_hash),
        )

    def consume_login(self, token_hash: str, now: float, secret_hash: str) -> bool:
        """Delete the login token; True only if it existed, had not expired and was
        minted for the server whose secret hashes to ``secret_hash``."""
        with self._tx() as db:
            row = db.execute(
                "SELECT expires_at, secret_hash FROM web_logins WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            db.execute("DELETE FROM web_logins WHERE token_hash = ?", (token_hash,))
        if row is None or float(row[0]) <= now or not isinstance(row[1], str):
            return False
        return hmac.compare_digest(row[1].encode(), secret_hash.encode())

    def delete_logins(self, token_hash: str | None = None) -> int:
        """Delete one pending login token (by hash) or, without an argument, all of them."""
        if token_hash is None:
            return self._db.execute("DELETE FROM web_logins").rowcount
        return self._db.execute(
            "DELETE FROM web_logins WHERE token_hash = ?", (token_hash,)
        ).rowcount

    def add_session(self, token_hash: str, csrf: str, now: float, expires_at: float) -> None:
        self._db.execute(
            """INSERT INTO web_sessions (token_hash, csrf, created_at, expires_at, last_used_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_hash, csrf, now, expires_at, now),
        )

    def get_session(self, token_hash: str, now: float) -> WebSession | None:
        row = self._db.execute(
            "SELECT csrf, created_at, expires_at, last_used_at FROM web_sessions "
            "WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or float(row[2]) <= now:
            return None
        return WebSession(str(row[0]), float(row[1]), float(row[2]), float(row[3]))

    def touch_session(self, token_hash: str, now: float) -> None:
        self._db.execute(
            "UPDATE web_sessions SET last_used_at = ? WHERE token_hash = ?", (now, token_hash)
        )

    def delete_sessions(self, token_hash: str | None = None) -> int:
        """Delete one session (by hash) or, without an argument, every session."""
        if token_hash is None:
            return self._db.execute("DELETE FROM web_sessions").rowcount
        return self._db.execute(
            "DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,)
        ).rowcount

    def add_web_request(self, kind: str, now: float) -> None:
        self._db.execute("INSERT INTO web_requests (kind, created_at) VALUES (?, ?)", (kind, now))

    def take_web_requests(self, now: float, max_age: float = WEB_REQUEST_MAX_AGE) -> list[str]:
        """Delete every request; return the kinds of those younger than ``max_age``.
        The age is checked here too, so the result never depends on prune_web."""
        with self._tx() as db:
            rows = db.execute("SELECT kind, created_at FROM web_requests ORDER BY id").fetchall()
            db.execute("DELETE FROM web_requests")
        return [str(kind) for kind, created in rows if now - float(created) < max_age]

    def prune_web(self, now: float) -> None:
        """Remove expired logins and sessions and stale web requests."""
        with self._tx() as db:
            db.execute("DELETE FROM web_logins WHERE expires_at <= ?", (now,))
            db.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
            db.execute(
                "DELETE FROM web_requests WHERE created_at < ?", (now - WEB_REQUEST_MAX_AGE,)
            )
