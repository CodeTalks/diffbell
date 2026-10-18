# 0014: Read-only doctor, upgrade without git writes or automatic rollback, a private Watchman for tests, a targeted preview walk

- Status: accepted (2026-10-10)

## Context
Verifying Plans 5a and 5b met the same friction again and again (Plan 6 spec
`2026-10-10-dev-watchman-ops-speed-design.md`):
- The integration tests used the user's real Watchman. Its FSEvents streams ran
  out twice, and each time the fix was `watchman shutdown-server`, which drops
  every other tool's watches. A review rehearsal that relied on `WATCHMAN_SOCK`
  sent `shutdown-server` to the user's Watchman: the `watchman` CLI ignores that
  variable (pywatchman honours it).
- Every live verification ran the same dozen checks by hand (launchctl, status.json,
  `PRAGMA quick_check`, the log, the Keychain, `/healthz`).
- "Install from a checkout, back up, restart, check" was run by hand about eight
  times.
- The editor's "Will be watched" and the card's count walked the whole project:
  a large project (over 100,000 entries) hit the walk's 100,000-entry / 3 s limits
  and showed "6+" although all its patterns were literal paths.

## Decision
- **A private test Watchman.** A session fixture (`tests/conftest.py`
  `test_watchman`, helpers in `tests/private_watchman.py`) starts
  `watchman --foreground --unix-listener-path=<D>/sock --statefile --pidfile
  --logfile=-` in a short temp directory, with `WATCHMAN_CONFIG_FILE` allowing a
  niced shell, waits for `version` through pywatchman on that socket, and sets
  `WATCHMAN_SOCK` for the session. Every `integration`-marked test gets it; a
  unit-only run never starts one. Fixture commands go through pywatchman on the
  private socket, never the CLI; the fixture stops only the process it started
  (no `shutdown-server`) and then leaves `WATCHMAN_SOCK` on the dead socket for
  the rest of the process (a leaked client must not fall back to
  `watchman get-sockname`). If it does not start, every integration test fails
  quoting its output: there is no fallback. **Guard 2** reads the user's watch
  list (`watchman --no-spawn --no-local --no-pretty watch-list`, default socket;
  `--no-local` makes "no server" a plain exit 1) before the
  start and after the stop and fails the session on any difference, naming
  "root set changed" or "running state changed" and printing both lists.
- **`dev-watchman doctor` is read-only.** Twelve checks (install source, config,
  Watchman, launchd, liveness, database, open problems, Keychain, Telegram
  `getMe`, web `/healthz`, permissions, log), each with a 5 s timeout, run side by
  side. It never opens the `Store` (opening migrates), reads SQLite with
  `mode=ro`, never tightens a mode, never sends a message, never prints a secret,
  and never starts Watchman: no `WatchmanClient` (pywatchman resolves its socket
  with `watchman get-sockname`, which spawns a server), only
  `watchman --no-spawn --no-local version` and `… --no-pretty watch-list`. A
  network failure of `getMe` is a warning (it must not fail an upgrade whose
  install worked); a bad token is a failure. A failed check shows a fix; the
  exit code is 2 when anything failed.
- **`dev-watchman upgrade` builds what is on disk.** Git is only read (status,
  HEAD, branch); uncommitted changes are refused unless `--allow-dirty`. It backs
  the database up with SQLite's backup API into a 0600 `state-upgrade-<UTC>.db`
  (3 kept), runs `uv tool install --force --reinstall`, then the newly installed
  `dev-watchman install` and `dev-watchman doctor --json` with the uv tool bin dir
  first on `PATH`, checks that the agent's plist runs the uv tool's executable,
  waits for a fresh start in `status.json`, and records the source
  in `install-source.json` (0600) only on success. A failure from the backup on
  prints the rollback commands and the restore steps (a refused source or checkout
  changed nothing); nothing is rolled back automatically.
- **Amended 2026-10-18: the install is pinned to the checkout's `uv.lock`.**
  `uv tool install` resolves afresh and ignores the lock, so an upgrade could run
  versions CI never tested (seen live: fastapi 0.144.0 and starlette 1.8.0 against a
  lock of 0.143.0 and 1.7.0). `diffbell upgrade` now reads the lock first with
  `uv export --locked --no-dev` (read-only; a lock out of date with `pyproject.toml`
  is refused before the backup, so nothing changes) and passes those versions to
  `uv tool install --constraints`, through a 0600 temp file in the state folder that
  is removed whatever happens. A dry run checks the lock too.
- **`dev-watchman install` returns once the restarted daemon is up** (amended
  2026-10-10). A `doctor` run a second after `install` used to report the daemon
  stopped, nothing on the web port and projects not watched, all resolved two
  seconds later. `install` now waits up to 30 s (`liveness.wait_until_ready`, the
  same wait as `upgrade` step 6) for `status.json` to show a start after the
  launchd install, `running`, `connected`, no project `pending`, and the web UI
  running when enabled; it prints the pid, the watched count and the web address,
  or names what is missing and exits 1. For that to be prompt, the daemon writes
  `status.json` right after handling the connection's `connected` and
  `disconnected` markers instead of only at the next 5 s maintenance tick.
- **The preview walks only where a pattern can match.** A pattern without "/",
  or without a literal first folder (`*/x.json`), keeps the whole-tree walk; otherwise each pattern gives a start: a literal
  path's parent folder (its entries only), an anchored glob's literal prefix
  (everything below). Prefix names are resolved against the folder listing with
  the matcher's comparison (case and Unicode form ignored), never `stat`; a
  symlinked, missing or pruned prefix ends that start. Every candidate is still
  decided by `_selected`; the limits are unchanged.

## Alternatives rejected
- **Relying on `WATCHMAN_SOCK` for the CLI:** the CLI ignores it; that assumption
  shut the user's Watchman down once.
- **Falling back to the real Watchman when the private one does not start:** it
  would silently bring back the exhaustion and the risk.
- **Watchman for the preview (from the web layer):** the web layer never talks to
  Watchman (ADR 0009, `tests/web/test_boundaries.py`), and a query would add roots.
- **Bigger limits or a cache for the walk:** slower first answers and stale counts;
  targeting removes the work instead.
- **`upgrade` pulling or checking out:** it would change the user's checkout behind
  their back; building what is on disk is predictable.
- **Automatic rollback:** a rollback that reinstalls and restores can fail in new
  ways and lose events; printing the exact commands keeps the user in charge.
- **An `install --wait` flag, or waiting in `upgrade` only:** every `install` is a
  restart someone checks next; waiting by default costs about a second.
- **`doctor` through `WatchmanClient`:** its socket resolution starts a Watchman
  server; a health check must observe, not repair.

## Consequences
- `make check` can no longer exhaust or touch the user's Watchman, and it fails
  when the user's watch list changes during a run (also when the user starts or
  stops their Watchman meanwhile: rerun).
- `shutdown-server` is only for exhaustion caused by other tools, and still needs
  the user's consent.
- `doctor` and `upgrade` print fixes and rollback commands; they never act on
  them.
- `install` exits 1 when the daemon is not ready within 30 s (Watchman down, the
  web port taken, a crash loop); launchd keeps the agent installed regardless.
- A project of literal patterns previews at once with an exact count.
