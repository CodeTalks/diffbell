# 0012: Health registry: alerts open once, resolve once; liveness by unexpected-stop and heartbeat

- Status: accepted (2026-10-09)

## Context
Plan 5a (spec `2026-10-09-dev-watchman-telegram-health-design.md`) has three goals:
a Telegram message that scans on a phone, telling a dead daemon from a quiet week,
and making Watchman trouble visible. Before it, problems went to the log, plus an
`Alerter` that re-sent the same alert at most once an hour per key (the `alerts`
table), from several places (the watcher queued alerts under its lock and flushed
them after; the daemon alerted directly). A dead daemon was silent, and so was a
quiet week.

## Decision
- **T4, one registry:** `health.Health` holds every operational problem, keyed
  (`watchman`, `project:<name>`, `recrawl:<name>`, `missing:<name>`, `config-watch`,
  `config`, `web`, `telegram`, `telegram.setup`), with a severity (`serious` or `warning`; the store's
  `Severity` literal adds `notice` for one-shot messages), a message and an optional
  fix. It feeds Telegram, the page (`problems` in `/api/dashboard`) and the
  heartbeat. Sources only `open` and `resolve`; neither ever sends nor waits, so the
  watcher may report while it holds its lock. Every source resolves its healthy keys
  at start, so a problem saved before a restart that has cleared since is resolved
  too. `forget` (no message) is only for a source the user removed or turned off: a
  project dropped from the config, the web UI switched off.
- **T3, page always, Telegram only for serious problems, once on open and once on
  resolve.** `Health.tick` (the 5 s maintenance tick) sends a serious problem's
  "⚠️ dev-watchman: …" once it is past its grace period (300 s for Watchman: a
  restart or an upgrade takes 1-3 minutes; logged at INFO during the grace and at
  WARNING once it is over) and "✅ dev-watchman: … (down HH:MM–HH:MM)" once it is
  resolved. A problem resolved within its grace period is deleted silently;
  flapping inside the grace never sends. If Telegram was down and the problem
  resolved first, only the resolution goes out ("was down a–b: …"): never "opened"
  after "resolved". A problem re-opened while its resolution is unsent or in flight
  keeps its grace for its ⚠️, but a `resolve` makes the ✅ due at once (or after the
  backoff of a failed send), so a start that re-opens `watchman` with its 300 s
  grace never holds back a pending ✅. Warnings (a recrawl, a missing folder, Telegram failing) are
  page-only.
- **Muted while Telegram is not set up** (amended 2026-10-15,
  [ADR 0018](0018-unconfigured-start-and-web-bot-setup.md)): the daemon calls
  `tick(now, sending=slot.configured)`. With `sending=False` each tick deletes every
  notice row and every resolved row unsent, whatever its due time (a restart notice
  is queued up to 10 minutes ahead, and an unannounced resolution would send a
  backlog "was down"), and leaves open problems untouched. A successful save calls
  `rearm(now)`: an open serious problem whose ⚠️ never went out is announced once on
  a coming tick. One with failed send attempts starts over (`next_try_at = now`,
  `attempts = 0`); one with none keeps its `next_try_at`, so a running grace still
  runs (one past its grace is already due). One already announced is not re-sent;
  one that opened and resolved while muted sends neither ⚠️ nor ✅. `rearm` is best
  effort and never raises. The heartbeat is skipped without writing its kv slot
  while not set up; on a first setup (not a bot replacement) a save past today's
  time records today's slot, so the first heartbeat is the next scheduled one. The
  `telegram.setup` warning (fix `dev-watchman setup`) is open while not set up and
  is never sent.
- **One problem, one ⚠️:** a key whose message changes stays the same problem.
  `tick` re-reads each row before sending it, and a send's outcome is recorded only
  if the row still has the `opened_at` it was read with: every fresh row gets an
  `opened_at` unique per key (nudged past the previous one within a clock reading),
  so a forgotten or replaced problem never inherits another's delivery state. The
  message text is compared only for notices. A severity change is a fresh problem,
  unless the row is serious and its ⚠️ already went out: it stays serious, so its ✅
  goes out too.
- **Persisted:** the `problems` table (schema v5) keeps open problems, their
  delivery state (`notified_open`, `attempts`, `next_try_at`, `last_error`, the bot
  token redacted) and a resolution still to be sent (`resolved_at`), so a restart
  neither forgets a problem nor sends "opened" again. Schema v5 is additive (`CREATE
  TABLE IF NOT EXISTS problems`, `kv`). The old `alerts` table is not in `SCHEMA` and
  is dropped (`DROP TABLE IF EXISTS alerts`) on **every** open, not only in the
  v4 → v5 step: code from before v5 recreates it and keeps `user_version` 5, so a
  rollback followed by a re-upgrade would otherwise leave it behind.
- **Shared classification, different policy:** `delivery.py` classifies a failed
  send and computes the delay (a 429's `retry_after`, else 30 s → 30 min); the
  notifier and health both import it and never each other. Events treat AUTH and
  BAD_REQUEST as terminal (with one plain-text retry); alerts retry every kind on
  the backoff, because an alert that is never resent is worse than a lost event and
  alert texts are plain, so a 400 should not recur. After a retryable failure in a
  tick (Telegram down), the remaining due rows are deferred without a call, each on
  its own backoff step (or the 429's `retry_after`), so a tick never stalls for one
  timeout per problem. A `retry_after` that is not a finite, non-negative number is
  ignored (the normal backoff applies). A message sent whose store write then fails
  is never sent again: `Health` keeps it in memory and retries only the write (the
  heartbeat likewise for its slot), logging the failure once, type name only.
  Messages are clipped to 1000 characters so an alert always fits.
- **The old machinery is deleted, not bypassed:** `alerts.py` (`Alerter`),
  `Store.alert_due` / `record_alert` and the watcher's `_pending_alerts`,
  `_queue_alert`, `_flush_alerts`, `_down_since` and `_down_alerted` (and the old
  `_alert`, `_check_down`, `DOWN_ALERT_AFTER`) are gone, and a static test keeps
  them gone, so no path can send a problem twice.
- **Lost watches:** Watchman does not cancel a watch whose folder is deleted below
  its watch root, so `Watcher.tick()` stats the subscribed folders (in
  `asyncio.to_thread`) and opens `project:<name>` ("watch lost: project folder
  removed") once one is missing at two ticks in a row. A reload that finds a watched
  project's folder missing opens it too, since that reload unsubscribes the project
  before the tick could notice.
- **T2, liveness:** a normal stop writes `status.json` with `"clean_shutdown": true`.
  Any start that fails (a Keychain read error, an invalid config, a failed
  migration, any other exception, never while another daemon holds the lock;
  missing credentials no longer fail a start, ADR 0018)
  writes `"running": true`, `"last_failure"` and `"failed_start": true`
  (`liveness.write_failure_status`, which needs no watcher); launchd's loop keeps the
  first down-from time, the last reason and `last_started_at`. A dead background
  task writes `"last_failure"` in the final snapshot; a failure after the daemon
  started (a dead task, or any exception once `Daemon.run` wrote its first
  snapshot, which marks the process as started) keeps that snapshot, never
  `failed_start`, adding `"last_failure"` if it has none and dropping
  `clean_shutdown`, so the next start reads "restarted after an unexpected stop
  (down a–b): <reason>". The next successful start reads
  the previous file before writing its own and, unless the previous run ended
  cleanly, queues one notice through Health (key `restart`, retried while Telegram is
  down): "⚠️ dev-watchman could not start earlier (a–b): <reason>" when the file says
  `failed_start` (or, from before the flag, has a failure but no `last_started_at`),
  else "⚠️ dev-watchman restarted after an unexpected stop (down a–b)[: reason]".
- **No crash-loop flood:** a restart notice is due at most once per 10 minutes (kv
  `last_restart_notice`); one inside that window is held until it ends, never
  dropped. A start that finds the previous notice still unsent replaces it with the
  latest reason but keeps its down-from time and its due time.
- **The heartbeat:** `notify.heartbeat` ("HH:MM", default "09:00", `null` off). Once
  the local time is past it and the day's slot (`"YYYY-MM-DD HH:MM"` in
  `kv.last_heartbeat`) is not already covered (`last >= slot` skips: a time moved
  later than the one today's went out at sends one more, at or before it nothing
  more today), a **silent**
  (`disable_notification`) "✅ dev-watchman · watching N projects · M changes
  yesterday · all OK" (or the warnings or problems, leaving out serious problems
  still in their grace) goes out. A Mac asleep at that time sends at the first tick
  after waking; a missed day is not caught up; the first run waits for the next
  slot; turning it off deletes the kv row, so turning it on again also waits; quiet
  hours and snooze don't apply. A failed send waits a 429's `retry_after`, else the
  shared backoff.
- **The quote is not monospace:** the Bot API forbids `code` and `pre` inside a
  blockquote ("All other entities can't contain each other"), so the expandable quote
  holds plain escaped lines (each file's path, then its lines indented by three
  spaces), the spec's fallback. Plan 5a's final task confirms it with a real send and
  a one-off probe of the nested form. Bidi control characters in a project, branch,
  path or note are shown as `\uXXXX`, never obeyed; counts that may be cut short by
  diffing's 200-line cap read "+N+".
  Superseded for the quote's lines by [ADR 0013](0013-diff-search-and-readability.md):
  `<code>` inside the quote is accepted and renders monospace on the Mac and Android
  (probes 2026-10-10); each file's lines share one `<code>` block.
- **Fix suggestions are runnable commands, text only:** `Problem.fix` holds only a
  command built by dev-watchman (`watchman version` for Watchman down; for a recrawl
  `watchman watch-del <root> ; watchman watch-project <root>`, the root
  `shlex.quote`d), shown and copied by the page on a project's badge, never run.
  Global problems are named next to "needs attention"; their fix is in
  `/api/dashboard` only. Advice that is not a
  command (a missing folder's "reconnect the drive or fix the path") is part of the
  message.

## Alternatives rejected
- **Patching the hourly alerter** and adding page badges and a heartbeat beside it:
  three places deciding what is wrong, and the hourly repeat stays noisy.
- **A restart message only, or a heartbeat only on quiet days:** a heartbeat only on
  quiet days can't tell a dead daemon from a quiet day by its absence alone; a
  restart message can't report a daemon that never comes back.
- **Telegram for every problem:** recrawls and a missing external drive are routine;
  the page shows them and the heartbeat mentions them.

## Consequences
- Every alert goes through `Health`: CLAUDE.md says so, and the deletion test holds
  the old names out.
- The daemon's maintenance tick gains three isolated steps (the `telegram` warning
  from the outbox, `Health.tick`, the heartbeat); the watcher's tick notices a
  vanished folder, so a lost watch is reported whether Watchman cancels it or not.
- The page lists only the problems of configured projects and global ones; a
  removed project's rows wait for the next reload to forget them. A serious global
  problem stays named in the status sentence even while a project needs attention.
- A config edit that turns the heartbeat off writes `heartbeat: null` (a visible
  null): the default is on, so removing the key would turn it back on. Turning it on
  writes the time double-quoted.
- After the upgrade the first silent ✅ arrives at the next 09:00.
- Accepted: during a crash loop the starts between two notices are not itemized;
  the one notice says when the outage began and the latest reason.
- A watched folder counts as gone only when it is missing at two ticks in a row (a
  folder moved away and straight back is not a lost watch).
