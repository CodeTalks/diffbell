<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/diffbell-lockup-dark.svg">
  <img alt="Diffbell" src="docs/brand/diffbell-lockup.svg" height="48">
</picture>

[![CI](https://github.com/CodeTalks/diffbell/actions/workflows/ci.yml/badge.svg)](https://github.com/CodeTalks/diffbell/actions/workflows/ci.yml)

# Diffbell

Watches **key files** (manifests, lockfiles, env files, CI workflows, build files,
agent config, entitlements…) across an explicit list of your projects using
[Watchman](https://facebook.github.io/watchman/), and sends a Telegram message when
any of them change on disk, whether the change came from you, a script or an AI agent.

- One message per burst of changes (debounced), with a short diff of what changed
  in that burst, with or without git (a "committed" / "uncommitted" / "untracked" note)
- Secrets are never sent: `.env*`, keys, certificates and credentials get metadata only
- Survives restarts, Watchman restarts and network outages without losing changes
- Runs as a launchd agent; credentials live in the macOS Keychain

## Install

With Homebrew (Watchman comes with it):

```bash
brew install codetalks/tap/diffbell
diffbell install         # starts the agent and opens the web UI
# then set up Telegram in the page that opens, or run `diffbell setup`
```

Or from a clone of this repository:

```bash
brew install watchman
uv tool install .
diffbell install
```

Watchman comes first: without it `diffbell install` stops with exit code 2 and the
same command (`brew install watchman`), before it starts anything.

Diffbell sends to a **dedicated** Telegram bot of your own. The page opens on
Settings → Telegram, which walks you through it in four steps: create the bot with
@BotFather, check its token, send the bot `/start` so the page finds your chat, then
send a test message and save. The token and chat id go into the macOS Keychain; the
page never shows them again. The same steps, with troubleshooting, are in
[docs/telegram-setup.md](docs/telegram-setup.md).

Until a bot is saved, Diffbell runs **not set up**: it watches your projects and
records every change (the dashboard shows them and says how many are waiting) but
sends nothing. Once you save, each project with waiting changes gets one catch-up
message, marked "held until Telegram was set up". Stored diff contents are kept
30 days, so a change held longer than that shows the current `git diff` for its
files instead, as after an indefinite snooze.

`install` writes a default config if none exists. While Telegram isn't set up it
turns the web UI on, unless your config already says `web.enabled` either way
(`diffbell web --disable` turns it off again); with `web.enabled: false` it
leaves the UI off and prints how to continue.

In a terminal instead: `diffbell setup` asks for the token (hidden input), reads
the chat id from your `/start` message (or asks for it if the bot has a webhook),
stores both in the Keychain, writes a default config if none exists and sends a test
message. A daemon that is already running keeps its previous bot (or none) until you
run `diffbell install` again; `setup` reminds you.

**More than one Mac:** several Macs can share one bot and chat. Set up each Mac with
the same token (send the bot `/start` again so the page finds the chat, or type the
chat id). Each Mac signs what it sends with its name, macOS's Computer Name by
default: a notification's first line ends with "· 💻 Work MBP", its link reads
"Open #42 on Work MBP" (it opens that Mac's page, so click it there), and alerts
and the heartbeat read "Diffbell (Work MBP)". `notify.machine` (or Settings →
Notifications) sets another name, at most 40 characters; `""` names no Mac. See
[docs/telegram-setup.md](docs/telegram-setup.md#using-diffbell-on-more-than-one-mac).

Optional: give the bot Diffbell's name and face. In @BotFather send `/setname` and
choose "Diffbell", then `/setuserpic` and send `docs/brand/diffbell-avatar-512.png`.
The bot's @username can't be changed and stays as it is.

### Coming from dev-watchman  <!-- old-name -->

Diffbell's pre-release name. `diffbell install` moves an old install over (its
config, state, logs and Keychain items); ADR 0020 describes the move.

## Configure

Edit `~/.config/diffbell/config.yaml` (created by `install` or `setup`). It is reloaded
automatically while the daemon runs (and again after every Watchman reconnect); an
invalid edit is rejected, the last good config stays active, and you get one alert (and one "the config is valid again" once it is fixed). An invalid config at startup makes `run` exit 2.

```yaml
debounce:
  quiet_seconds: 10        # flush after 10s without new changes
  max_wait_seconds: 60     # ...or after 60s at most
diff:
  max_lines: 20            # per file (0–200)
redact: ["config/prod.yaml"]   # added to the built-in redaction list
defaults: ["composer.lock"]    # added to the built-in watch list
ignore: ["app.json"]           # excluded in every project
web:
  editor: cursor               # the dashboard's "Open ↗" links: cursor, vscode or "none"
notify:
  quiet_hours: "23:00-08:00"   # optional: hold notifications, then one catch-up
  heartbeat: "09:00"           # a silent daily "all OK" (the default); null: off
  machine: "Work MBP"          # this Mac's name in messages (default: its Computer Name; "": none)
projects:
  - path: ~/projects/myapp
    patterns: ["migrations/**"]
    ignore: ["examples/**"]
    notify_on: [created, deleted]   # optional; default: created, modified, deleted
  - path: ~/projects/website
    name: site                 # defaults to the directory name
    use_defaults: false
    patterns: ["content/**"]
```

Project paths must be absolute or start with `~` and must be unique (also by name).
Unknown keys are errors. A project whose directory does not exist (an unmounted
volume, a deleted checkout) is not an error: it is skipped, its card shows a warning
(page only, no Telegram message), the other projects are watched as usual, and
`status` lists it as `missing`. While a path is missing the daemon re-checks it every
60 s and starts watching it as soon as the directory reappears. A project whose watch
is lost while running (its directory deleted, or the watch removed by another tool)
gets one "watch lost" alert and is retried every 60 s; once it is watched again you
get one "watching again".

**Patterns:** a pattern without `/` matches the file name at any depth (`.env*` matches
`packages/api/.env.local`). A pattern with `/` is anchored at the project root, and `**`
spans directories. `*`, `?` and `[abc]` / `[!abc]` classes never match `/`. Dotfiles
always match. Matching is case-insensitive (as Watchman's is on APFS) and insensitive
to Unicode normalization (NFC/NFD). Rejected: empty or whitespace-padded
patterns, patterns wrapped in quotes (`"main.py"`: write `main.py`), NUL bytes, absolute paths (leading `/`), `..` segments, empty segments
(`a//b`, a trailing `/`), backslash escapes, POSIX classes (`[:alpha:]`), a class
starting with `]`, an unclosed `[`, ranges that are invalid (also after Unicode
decomposition), and other character classes that do not compile. `.git`, `node_modules`,
`.venv`, `dist`, `build`, `.next`, `.wrangler` and `.worktrees` are always excluded at
any depth.

**Lists are additive.** `defaults` and `redact` extend built-in sets that can never be
removed. Use `ignore` to exclude something explicitly, and run `diffbell status` to
see each project's effective lists.

**Built-in watch list:** `package.json`, `pyproject.toml`, `*.lock`, `package-lock.json`,
`pnpm-lock.yaml`, `.env*`, `Dockerfile*`, `docker-compose*.yml`, `Makefile`,
`.github/workflows/**`, `CLAUDE.md`, `.claude/settings*.json`, `wrangler.toml`,
`*.entitlements`, `Info.plist`, `app.json`, `eas.json`.

**Built-in redaction list:** `.env*`, `.dev.vars*`, `*.pem`, `*.key`, `*.p8`, `*.p12`,
`*.pfx`, `*.keystore`, `*.jks`, `id_rsa*`, `id_ed25519*`, `*credentials*`, `*secret*`,
`*service-account*.json`, `*.mobileprovision`, `google-services.json`,
`GoogleService-Info.plist`, `.npmrc`, `.netrc`, `.pypirc`, `*.tfvars`, `*.tfstate*`. It covers every
`.env*` file the default watch list matches, and over-redacts on purpose (e.g. a
`secrets.py` module). For redaction, a pattern without `/` also matches any directory
in the path, so `*secret*` hides `config/secrets/db.yaml` and `*credentials*` hides
`credentials/aws.json` (watch patterns keep matching only the file name). A pattern with
`/` is anchored and gets no directory-component matching, so to redact a whole
directory write `config/private/**`, not `config/private`.

### Snooze, quiet hours and notify-on

Three controls decide when a change reaches Telegram. Changes are always recorded
and shown on the dashboard; only the message waits or is skipped.

- **Snooze** (web page only): a card's 🔕 menu pauses one project for 1 hour,
  4 hours, until tomorrow 9:00, or until you turn it back on (**Resume**, or
  **Undo** on the toast). Snoozes last at most 30 days and survive restarts.
- **Quiet hours** (`notify.quiet_hours`, or Settings): one daily window for every
  project, in the Mac's local time, `HH:MM-HH:MM`, allowed to cross midnight
  (start and end must differ).
- While a project is paused its changes are **held** ("Held until 16:00" on the
  dashboard). When the pause ends you get **one catch-up message per project**:
  "🔕 Catch-up · shop · held 14:05–15:40 (snoozed) · 3 changes in 2 files", with
  each file's net change (created then modified reads created; a file created
  then deleted is left out): the file before the first held change against the
  file after the last one, with the last change's branch. A project removed
  meanwhile still gets its catch-up, marked "(removed)"; one whose folder is
  only missing keeps its `notify_on`.
- **`notify_on`** (per project, or "Notify me about" in the project editor): the
  kinds of change that notify, from `created`, `modified` and `deleted` (all by
  default; at least one; use snooze to silence a project). A change of other kinds
  only is recorded as "Not notified"; in a mixed one, the other files are
  left out of the message and marked in the web UI.
- **Deep link:** while the web UI is running, each message ends with
  "🔗 Open #42", a link to `http://127.0.0.1:<port>/?event=42` (a catch-up names
  its newest event: "Open #42 (+6 more)"). It works only on this Mac, carries no
  token, and link previews are off. With the web UI off (or not yet up, or
  stopping) there is no link line; the test notification never has one.

### The message, alerts and the daily heartbeat

A notification reads, on the lock screen, as its header and summary:

```
🔑 myapp · main · 11:42
4 files · +2 −1
✏️ package.json +1 −1
➕ migrations/x.sql +1 −0
🗑 Dockerfile.old
🔒 .env.local · contents hidden
▸ (the diffs, folded into one quote: tap to expand)
🔗 Open #42
```

Files are listed changed, new, deleted, then hidden (redacted, never shown). A file
at the 200-line collection cap may have more lines than counted: its counts (and the
totals) read "+150+ −50+". In the quote, each file's diff lines (and its "… (N more
lines)" marker) are one monospace `<code>` block, so markers and indents line up;
path lines stay plain (a message with very many files quotes its lines without the
blocks, since Telegram stops formatting past about 100 entities). Never `<pre>`, which splits the quote on
Android (test sends on Android and the Mac, ADR 0013). Invisible bidi control characters in a name
are shown as `\u202E` and the like, so a file name can't pretend to be another. A
long message first shortens the diffs, then drops the quote, then collapses the last
files into "…and 3 more files".

**Alerts** are for problems that need you: Watchman down for more than 5 minutes, a
project's watch lost, the config rejected or no longer watched, the web UI down. Each
sends **one** "⚠️ Diffbell: …" and, once fixed, **one** "✅ Diffbell: …
(down 10:02–10:40)"; nothing repeats, even when the problem's text changes meanwhile.
A problem fixed within its grace period (5 minutes for Watchman) sends nothing.
Smaller problems (a Watchman recrawl, a missing folder, Telegram failing) show only
on the page. A project's problems put a ⓘ (warning) or ⚠️ badge on its card; hover
or focus it for the message and, when there is one, a fix command with a **Copy**
button ("Fix copied"; it is never run), such as `watchman watch-del … ; watchman
watch-project …` after a recrawl. Global problems are named next to "needs
attention"; their fix is in `/api/dashboard` only. A serious problem outside any project (the config,
the web UI) is named in the status sentence, also next to "N projects need
attention". While an alert can't reach Telegram, the page says "alert not delivered
yet"; it is retried with a backoff (30 s up to 30 min, or as long as Telegram asks).

After a crash, a kill, a background task that died or a failed start, the next start
sends one "⚠️ Diffbell restarted after an unexpected stop (down HH:MM–HH:MM)"
(with the reason when one is known, e.g. the task that died), or "⚠️ Diffbell could not
start earlier (HH:MM–HH:MM): reason" when starting failed (the Keychain, an invalid
config, a failed migration, any other error). A start that keeps failing under
launchd sends at most one such message every 10 minutes, and it keeps the time the
outage began.

**Daily heartbeat** (`notify.heartbeat`, or Settings): every day after `09:00` (the
default; local time) a **silent** message says "✅ Diffbell · watching 3 projects
· 5 changes yesterday · all OK" (or "· 2 warnings: …", "· ⚠️ 1 problem: …"), so a
quiet day is never mistaken for a dead daemon. A problem still in its grace period
(Watchman restarting) is left out. A Mac asleep at that time sends it after waking; a
missed day is skipped; quiet hours and snooze don't apply. Once today's heartbeat
has gone out, moving the time to one at or before the time it went out at sends
nothing more today; moving it to a later time sends one more silent heartbeat
today, at that time (at once if it has already passed). `notify.heartbeat: null`
turns it off (Settings writes exactly that; turning it back on writes the quoted time and
waits for its next occurrence).

> **Upgrade note:** the heartbeat is on by default, so the first silent ✅ arrives at
> the next 09:00 after you install this version. Set `notify.heartbeat: null` (or
> switch it off in Settings → Schedule) to turn it off.

### What redaction protects

Redacted files are never opened and never passed to git; the message shows only the
name and the kind of change. For other files diffbell never follows symlinks, refuses
hard-linked files and special files (an `lstat` check), and runs git without
textconv/external diff drivers; for untracked new files it also refuses anything whose
real location is outside the project or redacted and verifies the opened file
descriptor. It guards
against **accidental** disclosure; a local attacker who can write to your projects is
out of scope (they can read the secrets anyway). To diff what changed in a burst,
Diffbell keeps the last-seen content of each watched, non-redacted text file in its
local database (0600, up to 4 MiB per file and about 100 MiB per project at most;
stored pairs 30 days). Known residuals: git *clean* filters
may run when a diff falls back to `git diff HEAD`, and an iCloud "dataless" file can delay a
read. See [ADR 0007](docs/adr/0007-threat-model-accidental-disclosure.md).

diffbell's own config, state and log directories are 0700 and their files 0600;
an older install is tightened automatically at the next daemon start or `install`
(which lists what it changed). Parent directories are never changed.

## Run

| Command | What it does |
|---|---|
| `diffbell run` | Foreground daemon (logs to stderr; `--log-file`, `-v`) |
| `diffbell install` | Install and start the launchd agent `io.688.diffbell`, then wait (≤ 30 s) until the daemon is up: running, connected to Watchman, every project's subscription tried, the web UI listening if enabled. Prints the pid, the project count and the web address; exits 1 naming what is missing on timeout. While Telegram isn't set up it also opens Settings → Telegram (see Install) |
| `diffbell uninstall` | Stop and remove the agent |
| `diffbell status` | Daemon, Watchman, Telegram, per-project state and patterns, delivery counts, recent failures (exit 1 for an invalid config or a database the daemon must migrate) |
| `diffbell test` | Send a sample notification |
| `diffbell setup` | (Re)store bot credentials and send a test message (the terminal alternative to Settings → Telegram) |
| `diffbell web` | Open the web UI logged in (`--disable` turns it off, `--logout` ends every session) |
| `diffbell doctor` | Read-only health check: install, config, Watchman, agent, daemon, database, problems, Keychain, Telegram, web UI, permissions, log (`--json`; exit 2 if anything failed; Telegram not set up is a warning) |
| `diffbell upgrade` | Install from a checkout, back up the database, restart the agent, run `doctor` (`--from PATH`, `--allow-dirty`, `--dry-run`, `--no-backup`) |

Only one daemon can run at a time; a second `run` exits 3 with "already running".
After an upgrade, the first open of `state.db` may migrate its schema. A failed
migration is rolled back (the database is unchanged) and `run` exits 2 with
"database migration failed: …"; launchd then retries every 10 s with the same
result, so run `diffbell uninstall`, check the log, and report it. If
another diffbell process holds the database for more than 30 s, the open
fails with "database busy (another diffbell process is using it); retry"
instead (exit 2); retrying is safe.
`run` exits 2 with an invalid config (a missing project directory is not invalid;
see Configure) or when the Keychain can't be read, and 1 if the daemon
stopped abnormally (launchd then restarts it). Without bot credentials it does not
exit: it runs not set up (see Install), watching and holding notifications. `status` shows per-project states only
from the running daemon's snapshot; a snapshot left by a stopped daemon is labelled
as such. Its `telegram:` line reads "not set up (Settings → Telegram, or
`diffbell setup`)" until a bot is saved (naming the missing item when only one of
the two Keychain items exists, or "an earlier save didn't finish"), "@yourbot ok"
once the bot answers, and adds "saved, but the running Diffbell started before that:
run `diffbell install`" after a terminal `setup` the running agent hasn't picked
up yet.

Locations: config `~/.config/diffbell/config.yaml`, state (SQLite, lock,
`status.json`) `~/.local/share/diffbell/`, agent logs
`~/Library/Logs/diffbell/diffbell.log` (rotated, 5 MB × 3). Override with
`DIFFBELL_CONFIG`, `DIFFBELL_STATE_DIR` and `DIFFBELL_LOG_DIR`.

### Upgrading

A Homebrew install upgrades with Homebrew alone:

```bash
brew update               # fetches the tap: brew upgrade alone may not see a new version yet
brew upgrade diffbell
```

Within about 20 seconds the agent notices the new version, stops and is started again
by launchd on it; Telegram tells you (silently) "⬆️ Diffbell updated: 0.1.2 → 0.1.3".
The page's footer shows the version that is running.

`diffbell upgrade` is for an install from a checkout; on a Homebrew install it stops
with exit 2 and names the two commands above.

```bash
diffbell upgrade --from ~/projects/diffbell   # the first time
diffbell upgrade                                  # later: the recorded checkout
```

`upgrade` installs exactly what is checked out (it never pulls, checks out or
commits; uncommitted changes are refused unless `--allow-dirty`), copies the
database to `state-upgrade-<UTC time>.db` in the state directory (owner-only, the
three newest kept), reinstalls the tool with exactly the versions in the checkout's
`uv.lock` (what CI tested; a lock out of date with `pyproject.toml` is refused), restarts the agent, waits for the new
daemon and runs its `doctor`, which judges the checkout being installed (not the
previous record). On success it records the checkout in
`install-source.json`. On a failure from the backup on, it prints the commands to
roll back and to restore the backup; it never rolls back by itself.

`diffbell doctor` changes nothing: it never sends a message (use
`diffbell test` for that), never starts Watchman and never prints the token or
chat id. Each problem comes with a fix to run yourself. Telegram not set up (no bot
yet, one of the two Keychain items missing, or an earlier save that didn't finish)
is a **warning**, never a failure: `doctor` exits 0 and `upgrade` passes on an
install that has no bot yet.

### Troubleshooting the agent

Check the agent's state, pid and last exit code, then read its log:

```bash
launchctl print gui/$(id -u)/io.688.diffbell | grep -E "state|pid =|last exit"
tail -n 50 ~/Library/Logs/diffbell/diffbell.log
```

- The first start after login can find the Keychain slow to answer: `run` retries
  the read (3 attempts, 30 s timeout each, 5 s apart; a warning per failed attempt)
  and exits 2 only if all three fail. launchd then restarts it after its 10 s
  throttle.
- The agent runs with `ProcessType` `Standard` (not `Background`, whose throttling
  made Keychain reads and startup many times slower). An agent installed by an
  older version still has the old plist: re-run `diffbell install` to apply
  plist changes.
- `run` exits via `os._exit` after flushing its logs, so a failed or finished
  daemon always actually exits and launchd can restart it.
- A missing Watchman is named in three places:
  - `install`: exits 2 with `brew install watchman`;
  - `doctor`: fail when the agent can't run it; warn when only your shell can't,
    which means check your PATH;
  - the dashboard and Telegram alert after 5 minutes, with the fix `brew install watchman`.

## Web UI

An optional local page served by the daemon at `http://127.0.0.1:8765` (the port
is `web.port` in the config). It is off until you run `diffbell web`, except
that `install` turns it on while Telegram isn't set up and your config doesn't
mention `web.enabled`. The page refreshes every 5 s while its tab is
visible and pauses while it is hidden.

- **Header:** one status sentence ("All good · watching 3 projects · 1 snoozed ·
  last change 4 min ago", "Quiet hours until 08:00 · 2 notifications held", or the
  first problem: daemon unreachable, Watchman not running, Telegram unreachable or
  failing, a project needing attention).
- **Dashboard:** a card per project (hover a cut-off name for the full name and
  folder; plain status, files watched, last change; in
  its corner the problem badges and the 🔕 snooze menu, or **Resume** while the
  status reads "Watching · snoozed until …"; at its foot a **14-day activity
  strip**, one bar per day with a red tick under a day with a failed delivery:
  click a bar, or focus the strip and use ←/→ and Enter, to list that project's
  events of that day; Back returns to the previous filters), then the events
  grouped by day (filter, search, **Load more**; the status filter has **Needs
  attention**, **Held** and **Not notified**; an empty From/To reads "Any date",
  and **Clear** shows how many filters are on, the dates counting as one). **Needs attention** lists failed and
  held events with the open problems above them (each fix with Copy); the header's
  "N projects need attention" links there. Selecting an
  event (↑/↓ or j/k; the URL keeps `?event=ID`) shows its **stored diff**: the
  redacted report the notification was built from, with a line marking where
  the Telegram excerpt ended, changed words highlighted and light colour for
  JSON, YAML and TOML. **Retry** re-queues a failed delivery. Each file has
  **Open ↗** (opens it in Cursor or VS Code at the first changed line, through the
  editor's `cursor://` / `vscode://` link; also for a redacted file, which only
  opens your own copy; not for a deleted file, nor for a name ending in
  `:<digits>`, which the editor would read as a line), **Copy diff** (the stored lines; not for a redacted or
  truncated file) and **git log** (copies
  `git -C <folder> --literal-pathspecs log -p --follow -- <file>`, shown in its
  tooltip). The page only builds links and copies text: it never runs anything.
- **Search (`/`):** matches file names and, from 2 characters, the contents of the
  stored diffs (the last 30 days; ASCII letters ignore case). A match in a diff
  tags the row "in diff" and is highlighted in the detail. Files redacted today
  are never searched, even if their lines were stored before.
- **Projects:** choose "Key files + my additions" or "Only the files I list",
  type patterns (each line is checked as you type), or tick files in the
  folder tree (glob characters in a ticked name are escaped); Save reads
  "Save · watching N files". Remove can be undone for 10 s.
- **Settings:** one column with an index (Telegram · Notifications · Schedule ·
  Patterns · This page; `?view=settings&section=schedule` opens one).
  **Notifications:** wait for quiet, never wait longer than, diff lines per file,
  and **Name this Mac in messages** (a switch; the name field's placeholder is the
  Computer Name, used while the field is empty; off writes `machine: ""`).
  **Schedule:** quiet hours and the daily heartbeat, each a switch with its times
  and a preview ("Active now · ends 08:00", "Next: tomorrow 09:00").
  **Patterns:** the extra key files, ignore and extra redactions (each line checked
  as you type), each with its built-in list and count. **This page:** **Open files
  in** (Cursor, VS Code or None: `web.editor`); **Theme** (System, Light or Dark,
  also the header's quick toggle on every view;
  System follows macOS; remembered per browser in a `diffbell_theme` cookie, nothing
  secret, and applied by the server, so the page never flashes the other theme);
  the web port; "Log out all sessions" (confirmed inline). One **save bar**
  appears while anything differs from the saved config ("2 unsaved changes",
  **Discard**, **Save changes**, or ⌘S). The daemon checks the values as you type
  and its message shows under the field; Save waits until they are fixed. After a
  save the bar says "Saved and applied by the daemon." or that the daemon rejected
  the config, and if the file changed on disk meanwhile it offers **Reload**.
  Telegram and Theme save on their own. Leaving the page with unsaved changes asks
  first. Press `?` for the keyboard shortcuts.
- **Settings → Telegram:** while no bot is set up, a four-step wizard (create a bot,
  check the token, find your chat, send a test message and save), and on the
  dashboard "Telegram isn't set up — N changes are waiting. Set up ▸". Each step's
  "How? ▸" opens the **guide** (`?view=guide`, with "← Back to Settings"), the same
  text as [docs/telegram-setup.md](docs/telegram-setup.md). Only a `/start` sent
  after you click Check counts, so a bot other people have messaged can't offer you
  their chat; if the bot has a webhook, type the chat id instead. Once saved, the
  card names the chat and the bot ("Notifications go to Alex via @my_bot") and
  when the last one was sent,
  with **Send test message** and **Replace bot**; **Replace bot** runs the wizard
  again, and the current bot keeps sending until you save. When sending fails the
  card says why and how many changes it affects: Telegram rejected the bot token
  (replace the bot), Diffbell can't reach Telegram (it keeps retrying; nothing is
  lost) or Telegram refused a message (with Telegram's error); **Show failed
  changes** lists them with **Retry**. Only errors since the bot was last saved
  count, so replacing a rejected bot clears the warning at once. After a save that
  didn't finish it offers **Set up again**.
- A request that gets no answer within 15 s (60 s for the test notification)
  counts as the daemon being unreachable; the page keeps polling.

```bash
diffbell web            # enables it if needed, then opens the browser logged in
diffbell web --disable  # sets web.enabled: false (the server stops on reload)
diffbell web --logout   # ends every web session and pending login link
```

- A login lasts 30 days. `diffbell web` opens a single-use link valid for 60 s,
  handed to `osascript` on stdin (never as an argument, which `ps` would show). If
  the browser can't be opened, the link is deleted again and nothing is printed
  that contains it.
- It sends the link only after checking that the server on the port is this
  daemon's: the server must answer a fresh challenge with an HMAC under a secret
  that only the daemon and the owner-only `status.json` know. It keeps probing
  for up to 15 s (a restarting daemon may briefly still answer with the old
  secret); if the last probe got any byte at all without a valid answer, the port
  is treated as another process's, and the command refuses with exit 1. The link is bound to that server start: a link
  minted before a web restart no longer works.
- It needs a running daemon (`diffbell install` or `diffbell run`).
- After upgrading, restart the agent (`diffbell install`): a daemon started
  before the upgrade has no web UI, and `diffbell web` says so (exit 2).
- The page never shows the contents of **redacted** files, the bot token or the
  chat id. The token you paste is sent once, to check it, and kept only in the
  daemon's memory until you save (30 minutes at most since your last step; a reload
  starts at the token again); chats are offered by name only. It shows the stored diff each notification was built from (up to 200
  lines per file, kept for 30 days in `state.db`, 0600, also for an event still
  held; see ADR 0010 and 0011); a file redacted since then is shown without its
  lines. A catch-up event also shows the catch-up message's own diff. It listens on
  127.0.0.1 only and checks the `Host` header; changes need a CSRF token and a
  same-origin (or absent) `Origin`. Error messages name projects (or entry
  numbers), never paths.
- Edits are written back to `config.yaml` with its comments kept (a symlinked
  config stays a symlink; its target is rewritten). If the file changed since the
  page loaded it, the save is refused and the page offers a reload.
- The preview, the file tree and the dashboard's "files watched" counts walk the
  directory locally (the preview and the counts are capped at 500 listed files,
  100 000 entries and 3 s; the tree lists one folder at a time, up to 2,000
  entries); they never query Watchman. A count cut short by a cap shows as "N+".
  Counts run only while the dashboard is open, one project at a time, and are
  reused for 60 s.
- A web UI failure never stops file watching: it sends one alert (`web`) and is shown as
  `web: down` in `diffbell status`. `diffbell web` restarts it.

## Development

```bash
uv sync
make check      # ruff + ruff format --check + mypy --strict + pytest
make fmt        # auto-format
make guide      # docs/telegram-setup.md from src/diffbell/web/static/guide.json
```

Live checks of a branch never touch your installed Diffbell: `scripts/live/` runs the
daemon with scratch config, state, logs and Keychain items
(`DIFFBELL_KEYCHAIN_SERVICE=io.688.diffbell-scratch`) on a private Watchman.
Never run `install`, `uninstall` or `upgrade` from a worktree.

Integration tests use the real `watchman` binary, but on a private Watchman the test
session starts and stops itself (never your own: your watch list is compared before
and after, and a difference fails the run). They run by default whenever watchman is
installed (skipped without it). Deselect them with `-m 'not integration'`.
The web page's JS tests need node ≥ 22.7 and are skipped without it; set
`DIFFBELL_REQUIRE_NODE=1` to make a missing or old node fail them instead. Architecture: [`docs/architecture.md`](docs/architecture.md). Decisions:
[`docs/adr/`](docs/adr/).

### Releasing

1. On a branch, set the new version in both `pyproject.toml` and
   `src/diffbell/__init__.py` (`tests/test_release_check.py` keeps them equal), run
   `uv lock`, then merge as usual.
2. Tag the merge on `main` and push the tag:
   `git tag -a v0.2.0 -m "Diffbell 0.2.0" && git push origin v0.2.0`.
3. `.github/workflows/release.yml` checks the tag, runs the gate (`ci.yml`, the same
   job every push to `main` and every pull request runs), builds the sdist and the
   wheel, checks the sdist (`scripts/release_check.py`), and publishes a GitHub
   release with both files and `SHA256SUMS`.
4. Update the Homebrew tap: `uv run python scripts/bump_formula.py 0.2.0 --dry-run`
   shows the formula diff; without `--dry-run` it opens the tap PR. Once the tap's
   `brew test-bot` check passes, publish it with the tap's **brew pr-pull** workflow
   (`gh workflow run publish.yml --repo CodeTalks/homebrew-tap -f pull_request=<PR>`),
   not GitHub's merge button: it uploads the bottles (prebuilt for macOS 15 and 26 on
   Apple silicon) and adds the formula's `bottle do` block.

## Security

Report a vulnerability privately: see [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). The wordmark's outlines come from Figtree, under the
SIL Open Font License ([docs/brand/Figtree-OFL.txt](docs/brand/Figtree-OFL.txt)).
