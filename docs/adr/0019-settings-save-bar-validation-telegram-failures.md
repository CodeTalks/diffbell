# 0019: Settings: one save bar, server-owned validation, Telegram failure kinds

- Status: accepted (2026-10-15). Amends [0018](0018-unconfigured-start-and-web-bot-setup.md)
  (the Telegram card's states; `GET /api/telegram` gains `reason` and `failing`).

## Context
The Settings view grew one feature at a time. Its forms read as sentences with inputs
inside, which wrapped badly (units came loose from their numbers). One "Save
settings" button sat under the Timing column while the Patterns column, also a form,
had none, so it was unclear what the button saved; "Timing" held message content, a
schedule and a page preference. The page checked some values itself (`numberField`
and its messages in `settings.js`) and the server checked them again in its own
words, so one mistake could read two ways, and a rule changed in one place drifted
from the other. The Telegram card spent a full-width card on three lines, its test
button lived in another card, and its failing state showed only the raw error: the
outbox kept no error kind, so the page couldn't tell a revoked token (nothing will
ever send) from Telegram being unreachable (Keyward retries; nothing is lost). Spec:
`2026-10-15-settings-redesign-design.md` (in the maintainer's local design notes, not published).

## Decision
- **D1, five sections in one column.** Telegram · Notifications · Schedule · Patterns
  · This page, each a `section` with an `h2`, with an index of links (`#s-index`).
  `?view=settings&section=<id>` accepts exactly those five ids (`telegram`,
  `notifications`, `schedule`, `patterns`, `page`; `feed.SECTIONS`) and focuses the
  section's heading (only `settings.js` `focusSection` scrolls and focuses). One row
  per setting: label, field with its unit, help text below. Quiet hours and the
  heartbeat are switches (native `<input type="checkbox" role="switch">`); off keeps
  the times on the page until a save.
- **D2, one save bar.** It covers exactly the fields `PUT /api/settings` writes,
  counted as nine (`settings-model.js` `FIELDS`): quiet seconds, max wait, diff
  lines, quiet hours (on/off, from and to as one value), heartbeat (on/off and time
  as one value), the three pattern lists and the editor. It shows only while one of
  them differs from what was last loaded or saved (`dirtyFields`: text as typed; a
  list line by line, trailing empty lines removed). Its states (`barState`): hidden;
  dirty ("N unsaved changes", Discard, Save changes); dirty and invalid (Save
  `aria-disabled`, it focuses the first error); saving; applied ("Saved and applied
  by the daemon."); saved ("Saved: the daemon reloads the config.", when the daemon
  reports neither outcome within 3 polls); rejected; conflict (Reload, Discard).
  Applied and saved hide after 4 s. Applied and rejected come from the existing
  `saveOutcome` comparison of the saved `config_version` with the dashboard's
  `config_version` / `config_rejected_version`, never from the PUT's success;
  conflict is the PUT's 409. Telegram (its own wizard), Theme (applied at once, per
  browser) and Sessions are outside the bar. Edits survive leaving the view;
  unloading the page while dirty triggers the browser's `beforeunload` prompt. The
  existing ⌘S / Ctrl+S shortcut saves, as Save changes does.
- **D3, the server owns validation.** `web/settings_check.py` holds the one table of
  rules and messages (`MESSAGES`, `check_settings`). `POST /api/settings/check` takes
  the PUT body without `config_version`, runs the same function and writes nothing; it
  answers `{"errors": {field: message}, "issues": {list: [{line, reason}]}}`, empty
  when valid. `PUT /api/settings` runs it first (before the `config_version` check,
  so a stale and invalid body is a 422) and answers a failure with 422
  `{"error": summary, "errors": …, "issues": …}`: `error` stays, so every generic
  `ApiError` handling keeps working, and the two keys are additive.
  `config_edit.describe_validation_error` routes the config model's errors through
  the same table (`settings_check.message_for`), so a config the web rejects and a
  web save say the same sentence. Scope: the web only. The daemon's own config-load
  error (`config.load_config`'s `ConfigError`, in the log and the `config` problem)
  keeps its text. Numbers are sent as typed and parsed by the server: an empty or
  non-numeric field is an error, never 0 (a JSON bool, `null` or a list is a 400).
  The rules are the config model's, unchanged: seconds are floats above 0,
  `max_wait_seconds >= quiet_seconds`, 0 to 200 diff lines, `HH:MM` times, quiet
  hours whose end differs from their start, `config.check_pattern` for the lists.
  The page asks 300 ms after the last edit, one request in flight, and drops an
  answer older than the newest edit (sequence numbers). It never decides validity
  itself: its own validation strings are deleted.
- **D4, Telegram failure kinds (schema v7).** `events` gains two nullable columns
  with no CHECK: `last_error_kind TEXT` (an `ErrorKind` value: `retryable`, `auth`,
  `bad_request`) and `last_error_at REAL` (when the error was recorded). Every
  failure write of `last_error` writes both (`mark_retry`, `mark_failed`,
  `mark_batch_retry`, `mark_batch_failed`); a sent row's plain-text-fallback note
  (`mark_sent`, `mark_batch_sent`) has neither; every clear of `last_error` clears
  both. The migration (`_migrate_v7`) is `ALTER TABLE events ADD COLUMN` (no CHECK
  changes, so no table rebuild); the frozen templates of the older migrations stay as
  they were at their version. `complete_setup` also records the save time in kv
  `telegram.saved_at` (best effort, like its other steps after the swap).
  `Store.telegram_failure()` derives the card's data from the outbox, and is None
  unless `telegram_health()` reports an error (the existing rule):
  - undelivered: `status = 'failed'`, or `status IN ('pending','held') AND
    attempts > 0`, whose error was recorded after the cutoff: the later of the last
    bot save (`telegram.saved_at`) and the last successful send (`telegram_health`'s
    `last_ok_at`). An older error is history, not the current failure, so Replace
    bot clears a "token rejected" card at once. With neither, nothing is filtered;
    with a cutoff, a row with no `last_error_at` (written before v7) counts as older;
  - `kind` by precedence `auth` > `bad_request` = `legacy_failed` > `retryable` (a
    tie goes to the newest error). A revoked token means nothing will ever send, so
    an older `auth` failure wins over a newer retryable blip. A row written before
    v7 (no kind) counts as `retryable` while it is still being retried, and as
    `legacy_failed` once it is `failed` (terminal: never shown as "keeps retrying");
  - `since`: the earliest `created_at` of those events (the start of the backlog,
    not the last attempt); `failed` and `waiting`: the two counts over the same set;
    `error`: the newest stored text of the chosen kind (token-free, as every
    `last_error`).
  `GET /api/telegram` adds `failing` (`{kind, since, failed, waiting, error}`, or
  null) and `reason`.
- **D5, the setup reason is cached.** `reason` mirrors `secrets.Missing.what`
  (`both`, `bot token`, `chat id`, `unfinished save`) while Telegram is not set up,
  else null. The daemon holds it (`Daemon.setup_reason()`, `both` when the start gave
  none): set at start from the credential check, set to `unfinished save`
  (`note_unfinished_save`) by the wizard's save on any Keychain error while saving
  (`SecretStoreError` or `OSError`, including a cancelled save's write; never on
  `ValueError`, which fails before anything is written), and cleared by
  `complete_setup`. The web reads it through `WebContext`; the status endpoint never
  reads the Keychain.
- **D6, the Telegram card.** One component: `telegram-card.js` (`cardState`, pure)
  decides the row and its text, `telegram.js` renders it and keeps the wizard. Eight
  rows: Connected ("Notifications go to **{chat}** via **@{bot}**", "Last sent …");
  Connected without a chat name (the bot only, never the chat id); token rejected
  (`auth`: Replace bot first, Show failed changes); can't reach Telegram
  (`retryable`: "Keyward keeps retrying on its own; nothing is lost."); message
  refused (`bad_request`, with the stored error text); its pre-v7 variant ("Some
  changes couldn't be sent", `legacy_failed`); not set up (the wizard, with the
  dashboard's `counts.held`); an earlier save didn't finish (Set up again). A
  "change" is one event, as the dashboard banner counts. Show failed changes is the
  existing failed filter (`?status=failed`, the dashboard being the default view)
  with its per-event Retry; it is left out when no change failed. Send test message
  moves into the card and is the "try now" while Telegram can't be reached: there is
  no new retry endpoint.
- **D7, styling and the theme.** Every visual value is an `app.css` class on the
  existing tokens of both themes (the CSP forbids `style=`); one new token, `--knob`,
  defined in all three token blocks. This page gains a Theme control (a System /
  Light / Dark radiogroup, the same `dw_theme` cookie and first-paint read). The
  header's theme button (`#theme-toggle`) stays as the quick toggle on every view:
  removing it was tried and reverted in the live check (switching meant a trip to
  Settings). The two are one choice and always agree.
- **D7b, navigation.** From 900 px the header is sticky (the tabs and the theme stay
  one click away) and the section index stays in view beside the column; sections
  scroll to just below the header. The index is a scroll-spy: the section in view is
  marked (`aria-current="true"`), the last one at the end of the page, and a clicked
  section stays marked until the reader scrolls on.
- **D8, out of scope:** new settings, per-project settings (the Projects view), the
  login page, Telegram message formatting.

## Alternatives rejected
- **Validating on the page as well** (no request per edit): two copies of every rule
  and message drift apart. The check endpoint is local and cheap, and the page shows
  only what the server says.
- **A save button per section:** the old question (which form does Save save?) moves
  around instead of going away. One bar with a count of changed fields answers it.
- **Reporting "applied" on the PUT's success:** a PUT only writes the file; the
  daemon may still reject it at reload. The `config_version` comparison says what the
  daemon actually runs.
- **Deriving the kind from the error text** (no schema change): the text is for
  people, clipped and token-redacted, and its wording belongs to Telegram; matching it
  would break silently.
- **The newest error's kind instead of a precedence:** a retryable blip after a
  revoked token would say "keeps retrying" for a bot that will never send again.
- **Counting a kind-less (pre-v7) failed row as retryable:** it is terminal, so "keeps
  retrying" would be false.
- **Reading the Keychain for `reason` on each status request:** a Keychain call per
  page poll, and a locked Keychain would slow or fail the page; the daemon already
  knows the reason.
- **A retry endpoint for "can't reach Telegram":** the notifier already retries on its
  backoff; Send test message shows whether Telegram is back, and a failed event
  already has Retry.

## Consequences
- A config the web rejects and a web save say the same sentence; changing a rule or
  its message is one edit in `settings_check.py` (and its tests). The daemon's
  config-load error is still worded by the config model.
- `PUT /api/settings` 422 bodies gain `errors` and `issues`; a client reading only
  `error` is unaffected.
- `state.db` is schema v7. The bump only adds nullable columns, so `upgrade` treats
  it as additive (`upgrade.ADDITIVE_VERSIONS`): the previous code still runs on it
  and restoring the backup stays optional for that step. Events written before it
  have no kind and no error time: a pending or held one being retried reads as
  "can't reach Telegram", a failed one as "Some changes couldn't be sent" with its
  stored error, and once a bot was saved or a change delivered they count as older
  than the cutoff.
- The card says what to do: replace the bot, wait, or look at the refused change.
  After a bot is replaced, errors from before the save no longer count, so a
  token-rejected state clears at once; the failed changes stay listed in the feed.
- **Known limit:** the schema keeps no delivery time, so the cutoff's "last
  successful send" is the newest sent event's `created_at`. After a long-held event
  is delivered late (a catch-up, or a setup hold), the cutoff can be earlier than the
  delivery, and an error recorded between the two may still be reported. It fails
  safe: it never hides a current failure, and `failing` still needs
  `telegram_health()` to report an error. A `sent_at` column would fix it; out of
  scope here.
- `GET /api/telegram` still makes no Bot API call, and now no Keychain call either.
- `Store.telegram_health` (the header's "Telegram failing", the daemon's `telegram`
  warning, `/api/dashboard`) applies the same bot-save cutoff as `telegram_failure`:
  after Replace bot the replaced bot's errors are history everywhere at once
  (`complete_setup` re-checks), not only on the card (found in the live check).
- The theme can be chosen in two places (the header's cycle, This page's radios); both write the one `dw_theme` cookie.
- Live check S (`scripts/live/settings-check.sh`, run by the user): each card state
  and the save bar's applied, rejected and conflict states, against the scratch
  config, state, Keychain items and private Watchman of live check A.
