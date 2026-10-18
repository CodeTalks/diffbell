# diffbell

Python 3.13 for development (3.12+ supported) + uv. Watchman subscriber daemon → Telegram. Commit as
CodeTalks; the launchd label and bundle prefix are `io.688.*`.

## Commands
- `uv sync`: install dependencies
- `make check`: the quality gate (ruff, ruff format --check, mypy --strict, pytest);
  must pass before every commit and merge. Run it as `make check && git commit …`,
  never piped or chained with `;`.
- `make fmt`: auto-format
- Tests marked `integration` run on a **private** Watchman (`test_watchman` in
  `tests/conftest.py`, ADR 0014), never the user's: `WATCHMAN_SOCK` points
  pywatchman at it, and guard 2 fails the session if the user's watch list changed.
  The `watchman` CLI ignores `WATCHMAN_SOCK`: fixtures and tests talk to Watchman
  only through pywatchman; a CLI call against the default socket is only
  `--no-spawn --no-local version` or `… watch-list` (`tests/test_watchman_cli_guard.py`).
  After the session `WATCHMAN_SOCK` stays on the dead private socket: never restore
  or unset it.
  Use the `shared_dir` fixture (a unique subdir of ONE session-wide watch root). Only
  a test that needs its own root uses `watchman_cleanup`, which `watch-del`s every
  root appended to it on the private instance.
  Tests never touch the real Keychain, launchctl or Telegram: use `DIFFBELL_CONFIG` / `_STATE_DIR` /
  `_LOG_DIR`, fakes from `tests/fakes.py`, and injected runners.

## Rules
- Any architectural change updates `docs/architecture.md` (Mermaid diagrams) and
  adds or updates an ADR in `docs/adr/` **in the same branch**.
- Only `wmclient.py` and `watcher.py` import `pywatchman`. Every Watchman call goes
  through `WatchmanClient.call()`; never create a second pywatchman client in the daemon.
- After any pywatchman command or `receive()`, buffered PDUs must be drained with
  `getSubscription(name, root=pdu["root"], remove=True)`. `root` must be passed verbatim.
- Fingerprint precedence is deletion first, then `type == "l"`, then sha1, then
  `err:{mtime_ms}:{size}`. Don't reorder it.
- Journal rows, hash changes and the clock are written in ONE transaction. Event
  creation and superseded-row deletion are written in ONE transaction.
- Only an explicit REBASELINE records a fresh instance silently; a fresh instance in
  CATCHUP mode is always diffed against stored hashes.
- Redacted files are never opened and never passed to git. The plain-text fallback
  renders from the same redaction-filtered reports. Redaction matching must cover at
  least what Watchman's `match` covers (case, NFC/NFD); glob constructs the local
  matcher can't honor are rejected in `config.check_pattern`, never emulated
  loosely. Scope: `docs/adr/0007-threat-model-accidental-disclosure.md`.
- Content snapshots (ADR 0017): a diff's "before" is the previous event's snapshot
  (or the baseline's). Contents, note and branch are captured at creation, in the
  `create_event` transaction; a later send, retry or catch-up uses only stored
  data. Flushes stay serial (the debouncer awaits each flush; the read-then-
  `create_event` pair in `_flush_one` is the only writer that advances
  `snapshots` from captured afters). The REBASELINE's `replace_baseline(...,
  snapshots_epoch=)` never stores a path with a pending journal row and stores
  nothing if another snapshot write bumped the project's epoch during its read:
  it never regresses a snapshot (a path may fall back once). The
  hardened reader (`diffing.read_capturable`) is the only reader of contents.
  Redacted files are never read or stored; a capture is re-filtered by the
  current redaction list before `create_event`; a redaction change deletes the
  covered snapshots and stored contents (daemon only, a failed one retried).
  Every part of the engine's isolation (`snapshots.py` docstring) is
  load-bearing. Diff output is split on "\n" only
  (`diffing.split_lines`), never `splitlines()`. Unchanged: the `event_reports`
  write never blocks or fails a send.
- Never log or print the bot token. httpx/httpcore loggers stay at WARNING because
  they log request URLs, which contain the token; `telegram.py` also installs a
  redacting filter. Don't chain exceptions that may carry the URL or a secret.
- diffbell's own dirs are 0700 and its own files 0600 (ADR 0007, "Local file
  permissions"). Create them with an explicit mode, never via a process-wide umask
  (none in `src/`); tightening (`paths.tighten_install`, the log handler, the crash
  log) only clears bits, touches only our own paths (a config dir not named
  `diffbell` is not ours), never follows symlinks (the one exception: a
  symlinked config file's target) and skips files of another uid.
- Built-in `defaults`/`redact` lists are additive only. Never add a key that removes
  a built-in entry.
- Operational alerts go only through `health.Health` (`open`/`resolve`/`notice`;
  `Health.tick` is the only sender). Never send an alert directly. The old hourly
  `Alerter` machinery stays deleted (`tests/test_old_alerts_gone.py`).
- Use `Health.forget` only for a removed or disabled source; anything that recovers
  is `resolve`d (every source resolves its healthy keys at start).
- Fix suggestions (`Problem.fix`) hold only a runnable shell command, built by
  diffbell with `shlex.quote`d paths: shown and copied, never executed. Advice
  goes in the message.
- The heartbeat is sent silently (`silent=True` → `disable_notification`).
- Workflow: worktree in `.worktrees/<name>` on `feature/<name>`, `make check`, then
  `git merge --no-ff`. Never commit directly to `main`.
- The product is **Diffbell** in prose (`diffbell.BRAND`: Telegram texts, the page
  title and header, the README) and `diffbell` for everything typed or read by a
  machine (commands, the package, the launchd label, the Keychain service, paths,
  the log name) (`tests/test_brand.py`, ADR 0020). Outside the history docs, an
  old name (dev-watchman, Keyward, the dw- names) appears only on a line marked `old-name`
  (`tests/test_old_names_gone.py`). Every copy of the mark uses the one bell geometry
  from `docs/brand/tools/marks.py` and carries both paths (body and amber accent) in
  the mark's fixed colours, never `currentColor` (`tests/test_brand_assets.py`,
  `docs/brand/README.md`).
- Never `git commit` while another agent is committing in the same worktree.
- Commit messages end with exactly one trailer line,
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, and no other trailer.
- The web layer (`src/diffbell/web/`) never imports `pywatchman`, `wmclient` or
  `watcher` (`tests/web/test_boundaries.py`) and never receives the `WatchmanClient`
  (don't add it to `WebContext`).
  The preview never uses Watchman: it walks the disk locally. Keep
  `tests/integration/test_preview_parity.py` in `make check`.
- Every config edit from the web UI or `diffbell web` goes through
  `web/config_edit.py` (round-trip, validate, atomic replace, `config_version`).
- Settings validation rules and messages live only in `web/settings_check.py`
  (one table): `POST /api/settings/check`, `PUT /api/settings`'s 422 (`error`
  plus `errors`/`issues`) and `describe_validation_error` all use it. The page
  never re-implements a rule or writes its own message (ADR 0019).
- The web never returns the contents of **redacted** files, the bot token or the chat
  id. It may return the stored redacted diff the notification was built from
  (`event_reports`, ADR 0010); any file the current redaction list covers is
  returned without lines or note.
- The `event_reports` write never blocks or fails a send: it runs before the send
  in `try/except Exception` and logs only the exception's type name.
- Pausing and rules are decided only in the notifier (`pausing.py` plus
  `notifier.py`): events are always created; rules first (`skipped`), then the pause
  (`held`), then one catch-up per project (ADR 0011).
- The Telegram deep link carries only `?event=ID`, never a token or secret, and
  appears only while the web server is running (its bound port); the sample
  message never has one.
- Stored diffs (`event_reports`, `batch_reports`) live 30 days whatever the event's
  status; a catch-up's report goes with its batch (ADR 0007).
- Schema changes that alter a CHECK use SQLite's table rebuild:
  `PRAGMA foreign_keys=OFF` **before** `BEGIN IMMEDIATE`, re-check under the lock,
  rebuild, keep the AUTOINCREMENT sequence, row-count and `foreign_key_check`
  guards, `COMMIT`, then foreign keys back ON, verified (inside a transaction the
  pragma is a no-op and `DROP TABLE` would cascade).
- Every failure write of `events.last_error` also writes `last_error_kind` (an
  `ErrorKind` value) and `last_error_at` (schema v7); a sent row's fallback note
  has neither, and every clear clears all three. The card's `failing.kind` is a
  precedence (`auth` > `bad_request` = a kind-less `failed` row > `retryable`)
  over errors after the cutoff (`telegram.saved_at`, last send), never just the
  newest error's kind (ADR 0019).
- Per-project rows (`clocks`, `hashes`, `events`, `changes`, `snoozes`) key on
  `project_id`, the folder's stable id in `projects` (ADR 0016); the display name is
  never a storage key. The Store translates names through the map
  `Store.sync_projects` builds: the daemon calls it first in `_apply_config` (start
  and every reload), `status` and `web` with the config they load. A write for a
  name that isn't configured raises `UnknownProject`; a read returns nothing.
  Watcher writes carry the subscription token (`Store.project_token`, `expect=`):
  a stale one drops the PDU. Sync and reconcile happen with no `await` between.
- Manual runs on a feature branch never touch the live state: set
  `DIFFBELL_STATE_DIR`, `DIFFBELL_CONFIG` and `DIFFBELL_LOG_DIR` to
  scratch paths, `DIFFBELL_KEYCHAIN_SERVICE` to `io.688.diffbell-scratch`
  and `WATCHMAN_SOCK` to a private Watchman (`scripts/live/dbell` sets them all;
  `status` and `web` open the Store, which migrates the database). Never run
  `install`, `uninstall` or `upgrade` from a worktree: `launchd.LABEL` is
  hardcoded, so they act on the live agent, and `install` would point its plist at
  the worktree's `.venv`. The live-check scripts in `scripts/live/` touch only scratch
  state and may be run by an agent; steps that need
  the user (BotFather, their Telegram app, a browser login the agent can't drive) and
  the real switchover (ADR 0020) stay the user's.
- A Homebrew install (ADR 0021) is recognised only by `launchd.running_from_homebrew`;
  `upgrade` refuses it first (exit 2), and the plist names the `opt` path, never a
  versioned Cellar path (`launchd.stable_executable`).
- A newer install stops the daemon only through `selfupdate.SelfUpdate.check`, against the
  start snapshot, only under launchd (`under_launchd`, replaced by a conftest belt in
  tests) and with the plist's program present (ADR 0022). The update notice is a Health
  notice (`UPDATE_KEY`, in `health.SILENT_NOTICES`).
- `rename_install.py` is the only code naming the old dev-watchman install. It acts   <!-- old-name -->
  on the real one only with `switchover=True` (only `install` passes it, never under a
  scratch override; tests set `REAL_SWITCHOVER_ALLOWED = False`). Its explicit Keychain
  `service=` (on `get_secret`, `delete_secret`, `has_item`) is the one exception to the
  `service()` guard (`tests/test_secrets.py`). The old plist and Keychain items are
  deleted only after the new agent runs (ADR 0020).
- Glob matching goes through `globs.glob_matches` (linear time). `compile_glob` /
  `glob_to_regex` are only for validation and as the test oracle; never match
  names with the regex.
- `diffbell doctor` is read-only: it never opens the `Store` (SQLite
  `mode=ro`), never starts Watchman (no `WatchmanClient`; `--no-spawn --no-local` only), never
  edits, tightens or sends, never prints a secret. `upgrade` never writes to git,
  never opens the `Store` and never rolls back by itself; it imports everything at
  module level (step 4 replaces its own installed files).
- The preview walk is targeted (`preview.plan_starts`); its matched files and
  redacted flags must equal the whole-tree walk's (the oracle in
  `tests/web/test_preview_targeted.py`).
- Web walks (preview, browse, counts) run only in `Previewer` slots. Dashboard
  counts go through `count_gate` + `in_gated_slot` so they never take both slots.
- Config and preview error messages name projects or entries, never paths (they
  become 422 bodies).
- The web task is never part of the daemon's supervised task set; a web failure must
  never stop file watching.
- Web handlers are `async def` (the SQLite connection belongs to the loop thread).
- No inline `<script>`, `<style>`, `style="…"` or `on…=` in the web UI (the CSP is
  `default-src 'self'`); set untrusted text with `textContent`, never `innerHTML`.
- The page never runs anything on the user's machine, and neither does the server
  for it: editor links are `cursor://` / `vscode://` URLs (path segments
  percent-encoded), commands are text to copy. Shell commands built on the page
  quote every argument with `static/js/shell.js`'s `shellQuote`, kept equal to
  Python's `shlex.quote` (`tests/web/test_js_actions.py`), and `git` commands
  carry `--literal-pathspecs` (ADR 0015).
- The activity strip's buckets come from the server (`store.day_bounds`); the page
  filters the feed with a bucket's own `since`/`until`, never a day re-derived
  from its label.
- Diff lines are displayed through `render.visible` / `diffview.visible`, and file
  names through the matching `\uXXXX` name mapping (Telegram and page alike);
  storage, search, `event_reports`, Copy diff, editor links and commands keep raw
  text. One mapping per side, kept equal by the parity tests.
- Diff rendering on the page goes through `static/js/diffview.js` (pure segments),
  built with `h()` and text nodes. The diff search (`Store.query_events`) applies
  today's redaction list in Python before any match counts (ADR 0013).
- The web UI is ES modules under `static/js/`, no build step. `index.html` loads
  only `js/main.js`; every other module is import-safe (no top-level `document`,
  `window`, `fetch`, `location` or `localStorage`), so `tests/web/node.py` can
  import it under node. Icons come only from `static/icons.svg`.
- `diffbell web` mints a login token only after `/healthz?challenge=` returns a
  valid HMAC proof under the per-start secret in `status.json` (0600). The secret is
  never returned by any endpoint or logged. Any byte from the port without a valid
  proof means another process: refuse. Never weaken that check.
- The login URL (it carries the token) is never printed, logged or put in argv: it
  goes to `/usr/bin/osascript -` on stdin, after a full ASCII-only match. A token
  the browser didn't receive is deleted again.
- The bot token enters the web only through `POST /api/telegram/draft` and lives
  only in the in-memory draft (`web/telegram_setup.py`): never in SQLite, a log,
  browser storage or a response. The chat id never reaches the browser: chats are
  picked through the draft's opaque `choice` handles. Detected chats are only those
  whose message is dated at or after the draft's creation; a chat id typed by hand
  is also accepted (ADR 0018).
- Both Keychain writers (`diffbell setup` and the wizard's save) go through
  `secrets.save_credentials`: `save-pending` marker → `chat-id` → `bot-token` →
  delete the marker. While the marker exists the pair is not trusted.
- Telegram not set up (no items, one item, or the marker) is a warning everywhere
  (`status`, `doctor`, the page, Health `telegram.setup`), never a failure: the
  daemon starts, watches and holds events with reason `setup`; `upgrade` passes.
- `GET /api/telegram`'s `reason` is the daemon's cache (`Daemon.setup_reason`:
  set at start, `unfinished save` after a Keychain error in a wizard save, cleared
  by `complete_setup`); the status endpoint never reads the Keychain (ADR 0019).
- `static/guide.json` is the guide's single source: the page renders it and
  `make guide` (`scripts/gen_guide.py`) writes `docs/telegram-setup.md`, which is
  never edited by hand (the drift test in `make check` compares them).
- `DIFFBELL_KEYCHAIN_SERVICE` exists only for scratch runs: `secrets.service()`
  accepts only `io.688.diffbell` or `io.688.diffbell-<suffix>`, and the
  agent's plist (`launchd.build_plist`) never sets it (its `EnvironmentVariables`
  hold only `PATH`).

## Troubleshooting
- **Recrawl warnings after a merge:** `.watchmanconfig` ignores `.worktrees`, so
  creating or removing a worktree (with its `.venv`) no longer floods the watch of
  this repo. Watchman reads that file only when a watch starts: after changing it,
  the user re-watches the root (the recrawl fix command); never run it from here.
- **Watchman reports `FSEventStreamStart failed` for new watches**: test runs can
  no longer cause this or touch the user's Watchman (they use a private instance).
  If another tool exhausted the FSEvents streams, `watchman shutdown-server` fixes
  it, but **ask the user first**: it drops every other tool's watches, which must
  be re-watched afterwards. Never run
  `shutdown-server`, `watch`, `watch-del`, `watch-project`, `trigger` or `subscribe`
  against the default socket from a test or a probe.
- **`test Watchman did not start`**: the failure quotes the private daemon's
  output (for example a nice-value refusal or a socket path over 103 bytes: set
  `TMPDIR` to a shorter folder). There is no fallback to the user's Watchman.
- **`the user's Watchman changed during the test session`**: guard 2. "running
  state changed" usually means the user started or stopped their Watchman during
  the run (rerun); "root set changed" names the roots added or removed: find the
  test that reached the default socket.
