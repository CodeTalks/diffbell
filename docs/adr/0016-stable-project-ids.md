# 0016: A project is its folder: stable project ids in `state.db`

- Status: accepted (2026-10-14)

## Context
Every per-project row in `state.db` (`clocks`, `hashes`, `events`, `changes`,
`snoozes`) was keyed by the project's display name, and the optional `name:` in
`config.yaml` sets that name. Renaming a project therefore created a new project as
far as storage went: its history vanished from the page (the old events stayed under
a name no project had any more), it got a silent fresh baseline under the new name
(a key-file change made during the rename was never reported), and a name reused by
another folder would have inherited the old folder's history. Found in use on
2026-10-14: three renamed projects, each name pair sharing one folder in `clocks`.
Spec: `2026-10-14-stable-project-ids-design.md` (in the maintainer's local design notes, not published).

## Decision
- **Identity is the folder.** Schema v6 adds `projects (id, path UNIQUE, name,
  active, created_at)`: one stable integer id per canonical folder
  (`realpath(expanduser(path))`, no trailing slash; Watchman's roots are real paths
  too). The name is a label: the config name while configured, the last one it had
  afterwards. A partial unique index keeps active names unique.
- **Storage only.** `clocks`, `hashes`, `events`, `changes` and `snoozes` key on
  `project_id`. The Store translates names to ids with an in-memory map that
  `Store.sync_projects(pairs, now)` rebuilds; the watcher, notifier, health, the
  Telegram texts and the web API keep speaking configured names. Every query that
  returns a project name joins `projects`, so the feed shows a past event under the
  project's current (or last) name. `problems` keeps its names (short-lived, health's
  own keys). `config.yaml` is never written.
- **Sync points.** The daemon syncs in `_apply_config`, first: at start (after the
  migration, before the watcher subscribes) and on every reload (before the
  reconcile). `status` and `web` sync with the config they load, when it is valid.
  Every row is deactivated first, so a name moving between folders never trips the
  unique index.
- **Unknown names.** A write for a name that is not configured raises
  `UnknownProject` (the daemon always syncs first, so there it is a bug; the web
  reads the config file, which can name a project before the daemon's reload has
  synced it, and answers 404); a read by name
  returns nothing. An event takes its journal rows' project, so a batch queued
  under an old name before a reload is still recorded, under the current name.
- **Migration v5 → v6** with the documented rebuild procedure, after a backup with
  SQLite's backup API (`state-pre-v6-<UTC>.db`, 0600). Every stored name gets the id
  of its clock row's folder; names sharing a folder share an id named after the one
  with the latest clock `updated_at`, whose clock, hashes and snooze are the ones
  kept; a name without a clock row gets its own id with a NULL path, which the first
  sync that configures that name at a folder no id holds yet adopts (so a snooze set
  before the daemon ever subscribed the project survives). Every event and
  change keeps its id; row counts and `foreign_key_check` guard the rebuild. Events
  held by a merged-away name's snooze are released, since that snooze is dropped. A
  backup failure is a `MigrationError`: nothing is rebuilt without the copy.
- **Watcher subscription tokens.** A reload can rename or swap names while a
  subscription is in flight, so a PDU must never be written under the wrong id. Each
  subscription records its project's id (`Store.project_token`) before the
  `watch-project` await and re-checks it after, plus that the project is still in the
  latest reconcile's `_desired`. Its writes pass `expect=token`; a stale token raises
  `UnknownProject` and the PDU is dropped. That is safe: the clock is per id and the
  reconcile re-subscribes from it, so Watchman replays what was dropped. The first
  drop per name per reconcile logs a WARNING. An `UnknownProject` during subscribe
  leaves the state pending; it is never a Health problem.
- **One event, one folder.** The notifier splits each debouncer batch by project id
  (`Store.split_by_project`) before collapsing, so an event never holds rows of two
  folders; `create_event` refuses mixed rows. Events carry `project_active`: an
  inactive project's event takes the "project no longer configured" path (no folder
  read, no snooze, all kinds), catch-ups included (grouped by name and active), and
  the web page offers no folder actions (Open, git log) for it.

## Alternatives considered
- **Carrying rows over on rename** (match old and new names at reload and rewrite
  the `project` column): needs a rename detector, races with the debouncer and the
  outbox, and a name reused by another folder would still be ambiguous.
- **An id in `config.yaml`**: stable across moves, but the daemon would have to write
  the user's config (or the user would have to), and copying a project entry would
  copy its id.

## Consequences
- Renaming a project keeps its history, baseline, clock and snooze; a change during
  the rename is reported, under the new name.
- Removing a project deactivates its row; re-adding the same folder brings its
  history back. A name reused by another folder starts with that folder's history.
- **Moving a folder is a new project** with a fresh baseline; the old history stays
  under the old path's row. Re-attaching it (a `move-project` command) is out of
  scope.
- **Path case is not canonicalised.** A config path that differs in case from the
  on-disk folder is a different project.
- `status` and `web` sync the database rows from the config they load, so names and
  `active` may change before the daemon reloads. Writes stay correct: ids are per
  folder.
- The reconcile must run right after every sync, with no `await` between: the token
  guard relies on it.
- `doctor` expects schema v6. A database the new daemon has not opened yet is
  "not migrated yet; restart the daemon" (fix `dev-watchman install`); restoring a
  backup is advised only for a newer version or a failed integrity check.
- **Downgrading below v6 needs the pre-v6 backup** (`state-upgrade-*.db` or
  `state-pre-v6-*.db`): older code opens a v6 database without error, then every
  per-project query fails (no column `project`). `upgrade`'s rollback text says the
  restore is required when the upgrade migrated the database.
- `status` and `web` never migrate under a running daemon: on a pre-v6 database
  they tell the user to restart the daemon instead (`web` exits 2).
- The pre-v6 backup is written under a `.part` name and renamed; a launchd retry
  after a failed migration keeps the first one and adds no copy.
