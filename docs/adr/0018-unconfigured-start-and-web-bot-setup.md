# 0018: Unconfigured start and bot setup from the web

- Status: accepted (2026-10-15); amended 2026-10-15 by
  [0019](0019-settings-save-bar-validation-telegram-failures.md) (the Telegram
  card's states; `GET /api/telegram`'s `reason` and `failing`).

## Context
Keyward sends to one Telegram bot and chat whose token and chat id live in the macOS
Keychain (ADR 0003). Until now the only way to store them was `dev-watchman setup`,
an interactive terminal command, and `run` exited 2 without them, so the daemon and
with it the web UI (ADR 0009) never started until a bot existed. The web UI was also
off by default. The page could therefore not help with the one step a new user most
needs help with. Goal: easier onboarding for other people who install Keyward, each
setting up their own single bot in the browser, with a guide. Spec:
`2026-10-15-bot-onboarding-design.md` (in the maintainer's local design notes, not published).

## Decision
- **D1, start without credentials ("not set up").** `secrets.credentials()` returns
  the pair or why it is not set up (`Missing`): both items absent, one absent
  (named), or the `save-pending` marker present ("not set up: an earlier save didn't
  finish"). A Keychain read error or timeout still raises, is retried 3× and fails
  the start as before: a locked Keychain at login is never taken for a fresh install
  (which would show the wizard to a user who already has a bot). Not set up, the
  daemon watches, journals, creates events, runs Health and the web, and sends
  nothing. The one table is implemented once in `secrets.py`;
  `cli._credential_state()` (behind `run`, `status`, `test` and `install`) and
  `doctor.check_keychain` / `check_telegram` all go through it, so a leftover
  marker reads as not set up everywhere.
- **`SenderSlot`** (`sender_slot.py`) replaces the single `TelegramClient` the CLI
  handed the daemon. It implements `Sender`, holds a client or nothing, exposes
  `configured` and `bot_username` (no secret; a background `getMe` after start, never
  on the start path, and the draft's username at a swap; a failed fetch leaves it
  `None` and touches neither Health nor readiness), raises `NotSetUp`
  (`ErrorKind.NOT_SET_UP`) from `send` while empty, and
  `swap(token, chat_id, *, bot_username)` installs the new client on the loop thread
  with no `await` between installing it and marking it configured. The slot counts
  each client's in-flight sends: the old client is closed once its last send
  finishes (at once when it has none); a failing close is logged, never raised.
  `Daemon`, `Notifier`, `Health` and `Heartbeat` keep depending on `Sender` only; the
  slot's `configured` is read by the pause (hold), `Health.tick` (mute),
  `Heartbeat.tick` (skip) and `_check_telegram` (resolves `telegram`: there is no
  delivery to fail). The slot reads the Keychain once, at start: a terminal `setup`
  while a daemon runs is picked up only by a restart, so `setup` prints the restart
  hint whenever a daemon holds the lock, and `status` says "saved, but the running
  Keyward started before that: run `dev-watchman install`" when the live snapshot's
  `telegram_configured` is false while the Keychain has a complete pair.
- **D2, a hold reason.** Not set up is the pause reason `setup` (ADR 0011), checked
  before snooze and quiet hours: events are held forever with `held_reason = setup`.
  The rules-first order is unchanged. A successful save calls
  `store.release_held(now, reason="setup")`; the next catch-up check re-checks each
  group's pause, so a snooze or quiet hours re-hold it under their own reason. A
  catch-up's reason is `setup` if any event in it was held for setup, else `snooze`
  if any was snoozed, else `quiet`; `setup` reads "held until Telegram was set up".
  As a belt, a send that raises `NotSetUp` holds the event or batch with reason
  `setup` (never `failed`), so a race can't lose a change for good.
- **Muted health while not set up.** `Health.tick(now, sending=slot.configured)`.
  Muted, each tick deletes every notice row and every resolved row unsent, whatever
  its due time (a restart notice queued up to 10 minutes ahead would otherwise fire
  right after the save, and an unannounced resolution would send a backlog "✅ … was
  down"), and leaves open problems untouched (page and `doctor`). A save calls
  `Health.rearm(now)`: an open serious problem whose ⚠️ never went out is announced
  **once** on a coming tick. One with failed send attempts starts over
  (`next_try_at = now`, `attempts` 0); one with none keeps its `next_try_at`, so a
  running grace period still runs (one past its grace is already due). One already
  announced is not re-announced; one that opened and resolved while muted sends
  neither ⚠️ nor ✅. `rearm` is best effort and never raises. The heartbeat is
  skipped without writing its slot; on a **first** setup, a save past today's
  heartbeat time records today's slot (`mark_done_if_past`), so the first heartbeat
  is the next scheduled one, not one right after the test message. Replacing a bot
  doesn't: a heartbeat the old bot failed to send is still due.
- **`Daemon.complete_setup(token, chat_id, bot_username)`** runs after the Keychain
  write: the swap, then resolving `telegram.setup`, `rearm`, the heartbeat slot
  (first setup only) and `release_held`. The swap is what counts; each later step is
  best effort and logged, safe to repeat, and redone by a restart.
- **Health key `telegram.setup`:** a warning (never sent), opened at a not-set-up
  start with the reason ("Telegram not set up: the chat id is missing. …"), fix
  `dev-watchman setup` (a runnable command), message ending "Set up Telegram in
  Settings → Telegram, or run the command."; resolved by a successful save or by a
  start with credentials.
- **D3, the wizard.** Settings → Telegram has four steps: create a bot (a link to
  @BotFather and the guide), check the token, detect the chat, test and save. The
  same wizard replaces a bot from the Connected state (**Replace bot**); the current
  bot keeps sending until Save. The card's states are Not set up, Connected (`@bot`,
  the chat's name, last sent) and Failing (Connected plus the last delivery error).
  Amended 2026-10-15 by
  [ADR 0019](0019-settings-save-bar-validation-telegram-failures.md): one card with
  eight rows. Failing is split by error kind (token rejected, can't reach Telegram,
  message refused, and its variant for a failure stored before schema v7), and an
  unfinished save has its own row ("An earlier save didn't finish", Set up again).
  The dashboard says "Telegram isn't set up — N changes are waiting. Set up ▸" while
  not set up (from `/api/dashboard`'s `telegram.configured` and `counts.held`).
- **D4, the draft and the opaque choice.** The token reaches the server once, in
  `POST /api/telegram/draft`, and lives only in an in-memory draft on the web context
  (`web/telegram_setup.py`): the token, the bot's username, the creation time, a
  last-used time, the chat choices found, the `getUpdates` offset and a saving flag.
  Never in SQLite, a log, browser storage or a response. At most one draft exists (a
  new one replaces the previous one, whose next call gets 404); its TTL is 30
  minutes, sliding (every draft call refreshes it), and an expired draft is also
  swept when a draft is created and on `GET /api/telegram`; a failed save keeps it so
  Save can be retried; a successful save or expiry drops it. One save runs at a
  time across all drafts (a replaced draft's save may still be running, and two
  interleaved Keychain sequences could leave a trusted, mismatched pair): any Save
  while one runs is 409 "Saving is already in progress." A cancelled request (the
  client went away) keeps the mark until its Keychain thread ends. An unknown or
  expired draft is 404 "This setup expired or was restarted in another tab; check
  the token again." and the page returns to step 2. Chats are offered as `choice`
  handles (`secrets.token_urlsafe(8)`, random per draft) with a name: the numeric
  chat id never reaches the browser. Neither does the token after the request that
  carried it. `GET /api/telegram` reads only the slot, the kv, the outbox and (since
  ADR 0019) the daemon's cached setup reason: no API call, no Keychain read. The
  chat's name is kept as kv `telegram.chat_name` (not a secret; deleted for a
  manually entered id).
- **The setup reason and the failing fields** (amended 2026-10-15,
  [ADR 0019](0019-settings-save-bar-validation-telegram-failures.md)).
  `GET /api/telegram` adds `reason` and `failing`. `reason` mirrors `Missing.what`
  (`both`, `bot token`, `chat id`, `unfinished save`) while not set up, else null.
  The daemon caches it (`Daemon.setup_reason()`): set at start from the credential
  check, set to `unfinished save` (`note_unfinished_save`) by the wizard's save on
  any Keychain error while saving (`SecretStoreError` or `OSError`, including a
  cancelled save's write; never `ValueError`), cleared by `complete_setup`.
  `failing` is null unless `telegram_health()` reports an error; otherwise it is
  `{kind, since, failed, waiting, error}` from `Store.telegram_failure()`, over the
  undelivered events (`failed`, or pending/held with attempts) whose error was
  recorded after the cutoff: the later of the last bot save (kv
  `telegram.saved_at`, written by `complete_setup`) and the last successful send.
  `kind` by precedence over their `last_error_kind` (`auth` > `bad_request` =
  `legacy_failed` > `retryable`; a failed row with no kind is `legacy_failed`, a
  pending or held one being retried is `retryable`), `since` their earliest
  `created_at`, `failed` and `waiting` their counts of `failed` and of pending/held
  events, and `error` the newest stored text of that kind.
- **The after-draft chat rule.** Chat detection (`getUpdates`) keeps only private
  chats whose message is dated at or after the draft's creation, so a bot other
  people have already messaged can't hand the wizard someone else's chat. Each poll
  passes the draft's offset (one past the newest update seen), which confirms the
  updates it read: the bot is dedicated and the wizard is its only reader while a
  draft exists. Chats found accumulate in the draft, so a confirmed one stays on
  offer. A `getUpdates` 409 (a webhook, or another reader) is 409 "This bot has a
  webhook, or another program reads its updates; enter the chat id instead." A manual
  chat id (an optionally negative integer, the CLI's rule) is accepted for that case
  or for a user who knows their group's id; detection and the guide cover only a
  direct chat with the bot.
- **The pending marker.** Both writers, `dev-watchman setup` and the wizard's Save,
  call `secrets.save_credentials`: check both values (non-empty, one line; a
  `ValueError` writes nothing and is a 422 in the wizard), then write
  `save-pending`, then `chat-id`, then `bot-token`, then delete `save-pending`
  (ADR 0003). Any failure stops the sequence and leaves the marker; while it exists
  the pair is not trusted (D1). A failed save in the running daemon leaves the slot
  as it was. Blocking Keychain calls run in `asyncio.to_thread`, and nothing in that
  thread touches SQLite.
- **D5, one guide source.** `static/guide.json` (plain text: steps with an id, a
  title, paragraphs, links, troubleshooting) is rendered by the page (`guide.js`,
  `h()` and text nodes, `?view=guide&step=<id>`) and by `scripts/gen_guide.py` into
  `docs/telegram-setup.md` (`make guide`); a pure Python drift test in `make check`
  keeps them equal. No screenshots (they go stale).
- **D6, `setup` and `install`.** `dev-watchman setup` stays, with the same items, order
  and marker. `install` writes the default config when none exists, reads the
  Keychain before touching launchd and, once the agent is ready and Telegram is not
  set up: with `web.enabled` **absent** it turns the web UI on through `config_edit`
  (as `dev-watchman web` does) and opens the page logged in on Settings → Telegram
  (`/login?t=…&next=telegram`, `next` an allowlist of one value); with
  `web.enabled: true` it opens the page; with `web.enabled: false` it changes
  nothing and prints how to continue; a Keychain read error opens nothing (it prints
  the error and a hint). A failed open prints the reason and "Run `dev-watchman web`
  to open the setup page.", and `install` still exits 0.
  This changes the web UI's manual opt-in (web UI spec W4) for one case. To keep a
  user's own `false` authoritative, the default config template now carries
  `web.enabled` as a comment, so a present key is always the user's statement
  (`config_edit.web_enabled_set`); older configs with the template's explicit
  `false` belong to users who already ran `setup`.
- **D7, a warning, never a failure.** Not set up is a warning in `status`
  ("telegram: not set up (Settings → Telegram, or `dev-watchman setup`)", naming a
  missing item or an unfinished save), `doctor` (Keychain and Telegram rows, exit 0)
  and the page. `upgrade` fails only on `fail` rows, so an unconfigured install
  upgrades.
- **The Keychain service override.** `secrets.service()` returns
  `DEV_WATCHMAN_KEYCHAIN_SERVICE` or `io.688.dev-watchman`, read on every call; a
  value other than `io.688.dev-watchman` or `io.688.dev-watchman-<suffix>` is
  rejected, so a typo can never read or overwrite another app's item. It exists only
  so a branch can run the wizard end to end against scratch items
  (`io.688.dev-watchman-scratch`, live check A) and so a scratch launchd label can
  prove the agent's Keychain write (live check B). The agent's plist sets only
  `PATH`, so the installed agent can never inherit it. A launchd-label override,
  which would isolate `install` itself, is not added: it touches the plist path, the
  bootout/print call sites and doctor's agent check, more surface than probe B needs.
- **D8, out of scope:** several bots or destinations, a group-chat flow, a Disconnect
  button.

## Alternatives rejected
- **Fan-out to several destinations.** The outbox, the catch-up counts and the
  `telegram` health key all assume one destination; several would need
  per-destination delivery state. Not needed for onboarding.
- **Sending the token or chat id to the page** (e.g. to prefill a field or let the
  page call Telegram itself): the web's rule is that neither ever reaches the
  browser (ADR 0009); opaque handles cost nothing.
- **Persisting the draft** (SQLite or browser storage): a token at rest outside the
  Keychain for a half-finished setup. A daemon restart or reload costs one more
  "Check".
- **Accepting any chat from `getUpdates`:** a bot's update queue may hold strangers'
  messages from before the user started; the after-draft rule closes that.
- **Treating a Keychain read error as not set up:** a locked Keychain at login would
  show the wizard to a configured user and hold their notifications.
- **Writing the token first, or without a marker:** a crash between the two writes
  would leave a pair that looks complete and points at the wrong chat.
- **Reloading credentials from the Keychain on every send** (instead of the slot's
  swap): a Keychain call per message, and a terminal `setup` would take effect
  half-way through a catch-up.

## Consequences
- A fresh install runs at once: `install` → the page → the wizard. Changes made
  before the bot exists are not lost: they are held and arrive as one catch-up per
  project, marked "held until Telegram was set up". Stored diff contents live 30
  days (ADR 0007, 0017 retention): a change held for setup longer than that catches
  up from the current `git diff` for its paths, as after an indefinite snooze.
- Exit 2 for missing credentials is gone from `run`; a Keychain read error, an
  invalid config and a failed migration still exit 2.
- The web UI may be on without the user having run `dev-watchman web`, but only on
  a not-set-up install whose config never stated `web.enabled`.
- Problems that open and resolve before the bot exists leave no Telegram trace; a
  problem still open at setup is announced once.
- After a terminal `setup` the running daemon keeps its previous bot (or none) until
  `dev-watchman install` restarts it; `setup` and `status` say so.
- Live checks on a branch never run `install` and never touch the live Keychain
  items, agent or Watchman: `scripts/live/` runs the wizard against scratch items, a
  private Watchman and scratch dirs (check A), and proves the agent's Keychain write
  with a temporary scratch label (check B).
