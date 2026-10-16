# 0011: Pause and rules live in the notifier (held events + catch-up)

- Status: accepted (2026-10-09)

## Context
Plan 3b (spec `2026-10-09-dev-watchman-notify-controls-design.md`) adds three
controls over when a change reaches Telegram: a per-project **snooze** from the
web page, daily **quiet hours** for all projects, and per-project **`notify_on`**
rules (created / modified / deleted). Each needs a place in the pipeline and a
rule for what happens to the changes it stops.

## Decision
- **N6, where:** in the notifier, at delivery. Watching, journaling, debouncing and
  event creation are unchanged: every change still becomes an event, so the
  dashboard shows it at once. `notifier._deliver` decides, after `build_reports`:
  1. **rules** (`notify_on`): files of a kind the project doesn't notify about are
     left out of the message, kept in the stored report with `rule_skipped`, and
     listed after the sent files; an event left with nothing is `skipped`;
  2. **pause** (`pausing.pause_until`, a pure function of the snooze row, the quiet
     window and the time): the event is `held` until the pause ends
     (`next_attempt_at`, `HELD_FOREVER` for an indefinite snooze), with
     `held_reason` `snooze`, `quiet` or `setup` (Telegram not set up, checked
     first and held forever until a save; amended 2026-10-15, ADR 0018);
  3. otherwise it is sent, with the deep link when the web UI is on.
- **N8, order:** rules first, then the pause, so a held event always has something
  to send and a catch-up message is never empty.
- **Not set up is a hold reason** (amended 2026-10-15,
  [ADR 0018](0018-unconfigured-start-and-web-bot-setup.md)): `pause_until` takes the
  sender slot's `configured` and, when it is false, returns `HELD_FOREVER` with reason
  `setup` before it looks at the snooze or the quiet hours. A successful save in the
  web wizard calls `release_held(now, reason="setup")`; the next catch-up check
  re-checks each released group's pause, so a snooze or the quiet hours re-hold it
  under their own reason and its catch-up goes out when they end. A catch-up's reason
  is `setup` if any of its events was held for setup, else `snooze` if any was
  snoozed, else `quiet`; the header renders `setup` as "held until Telegram was set
  up". A send that raises `NotSetUp` (a race with the slot) holds the event, or
  re-holds the batch, with reason `setup` and `HELD_FOREVER`, never `failed`.
  `held_reason` has no CHECK, so there is no migration.
- **N1, held then caught up:** the maintenance tick (`catch_up_due`, every 5 s)
  groups due held events by project and re-checks the pause (a group still paused
  is rescheduled; an error in that check backs the group off like a send error).
  Each group becomes **one** catch-up message: changes merged per path by **net
  effect** (created→modified reads created, deleted→created reads modified,
  created→deleted is dropped, as `models.collapse` drops it within one event;
  the latest kind alone would let a rule drop a file it allows), the current `notify_on` applied, and a header naming the held
  span and the reason. The group succeeds, retries (stays held, backoff) or fails
  as a unit and shares one `telegram_message_id` and `batch_id` (its first event
  id). On a permanent error the plain-text fallback is tried while the events are
  still held, so there is no `failed` window in which a web Retry could queue them
  again. Retrying a failed batch holds every event of it again (`batch_id`
  cleared, due now), so it is re-sent as one catch-up, never as one message per
  event. Only `pending` events can become `held`; `skipped` takes `pending` events
  and `held` ones (a catch-up the current rules empty); the batch transitions act
  only on `held` events. Holding a group again (still paused at catch-up time)
  clears its attempts and error, so a stale Telegram error isn't reported while it
  waits.
- **Rules of a project whose folder is missing:** it is still configured, so it
  keeps its `notify_on` and is not marked "(removed)"; only a project no longer in
  the config is "(removed)" (every kind, file names only).
- **N9, the catch-up diff:** built against HEAD at send time from the merged
  paths, not by concatenating the per-event diffs (stale and repetitive). The
  merged report the message was built from is stored in `batch_reports` (same
  encoding, caps and redaction as `event_reports`), written before the send; each
  event keeps its own hold-time report in `event_reports`.
- **N7, state:** snoozes are runtime state in `state.db` (`snoozes`); quiet hours
  and `notify_on` are deliberate settings in `config.yaml`, edited through
  `web/config_edit.py`. Unsnoozing, a new snooze and a removed project's snooze
  being dropped make that project's snooze-held events due now; a quiet-hours
  change on reload makes **every** held event due now, whatever its reason (an
  event held as "snooze" may be waiting on the quiet hours' later end). The next
  catch-up check holds again whatever is still paused.
- **Schema v4:** the new statuses need a new `CHECK`, which SQLite can only change
  by rebuilding `events`. The Store turns foreign keys on at connect; the migration
  turns them off (verified) before `BEGIN IMMEDIATE` (the pragma is a no-op inside
  a transaction, and with it on, `DROP TABLE events` would cascade into `changes`
  and `event_reports`), re-checks under the lock that no concurrent opener migrated
  first, checks the row counts and `foreign_key_check` before `COMMIT` as runtime
  guards (a mismatch raises and rolls back), keeps the AUTOINCREMENT sequence,
  creates `snoozes`, `batch_reports` and the `events_batch` index in the same
  transaction, and turns foreign keys back on, also after a failure. Openers wait
  up to 30 s for another process's write lock; past that the open fails with
  "database busy (another dev-watchman process is using it); retry", not as a
  migration failure.

## Alternatives rejected
- Holding changes before events are created (in the debouncer or the watcher): the
  dashboard would show nothing during a pause, and the journal would need a second
  kind of "waiting".
- Sending silently (Telegram's `disable_notification`) instead of holding: not a
  real pause, and nothing to catch up on.
- Dropping or only recording ("muted") paused changes: either a gap in the history
  or changes the user never hears about.
- Pause first, then rules: a held event could produce an empty catch-up message.
- Snoozes in `config.yaml`: rewriting the YAML on every snooze is noisy, and an
  expiry is runtime state.

## Consequences
- Events now end as `sent`, `failed`, `held` (waiting for a pause to end) or
  `skipped` (recorded, never notified). `prune` keeps `pending` and `held` events,
  and keeps a catch-up batch whole while any member is recent or still open, so a
  Retry re-holds every member; per-event reports are pruned at 30 days whatever the
  status (ADR 0007, "Diffs at rest"), a batch's report with its batch.
- A catch-up stuck on retryable errors (`held`, `attempts > 0`) turns the header to
  "Telegram failing", and `telegram_health` reports it first, even when something
  was sent since (a held event is older than any later send).
- Delivery is at-least-once, as for single events: if storing the outcome fails
  after a successful send (or the daemon dies in between), the group is still
  held and its catch-up is sent again.
- A migration failure leaves the database at v3, unchanged, and the daemon exits;
  launchd retries every 10 s with the same result until `dev-watchman uninstall`.
- Snoozing is web-only: Telegram stays send-only (no bot polling, no inline
  buttons), and the deep link carries only `?event=ID`, never a token. It follows
  the running web server (its bound port), not just the config, link previews
  stay off (`link_preview_options`), and the sample message never has one.
