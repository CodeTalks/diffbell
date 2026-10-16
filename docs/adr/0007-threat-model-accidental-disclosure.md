# 0007: Threat model: accidental disclosure

- Status: accepted (2026-10-08)

## Context
dev-watchman reads files in the user's projects and sends excerpts to a third party
(Telegram). Its redaction rules decide which files are never read. We need a clear
line between what redaction must guarantee and what it does not try to.

## Decision
Redaction protects against **accidental** disclosure: a secret reaching the chat
because of how a file happens to be named, linked or encoded, or how git is
configured. In scope, and handled:

- Names: built-in plus user `redact` patterns, matched case-insensitively (Watchman's
  `match` is case-insensitive on APFS, so `.Env.Local` is both watched and redacted).
  A pattern without `/` matches the file name or any directory component, so
  `config/secrets/db.yaml` (`*secret*`) and `credentials/aws.json`
  (`*credentials*`) are redacted although their file names are innocuous.
- Unicode forms: a pattern matches a name in NFC or NFD; ranges invalid in either
  form are rejected at config load.
- Unsupported glob constructs are rejected rather than half-emulated, so a redact
  pattern can never silently match nothing.
- Matching runs in linear time (`globs.glob_matches`, an NFA with the regex's
  semantics, no backtracking), so a pathological pattern such as
  `"*a" * 12 + "b"` can't stall redaction and with it every notification.
- Links: dev-watchman never follows symlinks; hard-linked files (`nlink > 1`) and
  non-regular files are never read (an `lstat` check for every file). For tracked
  files git does the diff and itself refuses paths beyond a symbolic link. For
  untracked new files a path whose real location is outside the project root or is
  redacted (e.g. through a symlinked parent directory) is refused, the file is opened
  with `O_NOFOLLOW`.
- git configuration: `--literal-pathspecs`, `--no-textconv`, `--no-ext-diff`,
  `stdin=DEVNULL`.
- The bot token never reaches logs, argv, exception chains or the chat.

Out of scope:

- A local attacker with write access to the user's projects. They can already read the
  secrets directly; dev-watchman adds no capability they lack.
- Adversarial time-of-check/time-of-use races. For untracked new files we re-verify
  the opened file descriptor (type, inode, link count, real path via `F_GETPATH`)
  after opening and before reading, and go no further; tracked files get the
  `lstat` check and then git.

Accepted residuals: git may run *clean* filters (`filter.<name>.clean`) on the
worktree side of a diff of a non-redacted file; an iCloud "dataless" placeholder may
block a read until the file is materialized. Since content snapshots
([ADR 0017](0017-content-snapshots.md)) the clean-filter residual is retired for
content-based reports: they run `git diff --no-index` on private copies with every
attribute source closed, a minimal environment and a temp dir verified to be outside
any work tree (if that check finds a work tree or can't tell, difflib is used, so the
claim still holds). It stays only for the fallback (`git diff HEAD`). git's output is
split on "\n" only (`diffing.split_lines`), so a line separator in a file's content
can't fake a diff header or a changed line.

## Diffs at rest
Since the web UI's diff view (spec `2026-10-09-dev-watchman-ui-refine-design.md`,
[ADR 0010](0010-stored-diffs.md)), each event's redacted report is stored in
`state.db` (`event_reports`, 0600 like the rest of the file), and since
notification controls ([ADR 0011](0011-pause-and-rules-in-the-notifier.md)) each
catch-up message's merged report is stored too (`batch_reports`). Stored diffs live
**30 days whatever the event's status**: `prune` deletes every report older than
that even when its event is still kept, so a held (snoozed) or pending event can
outlive its report, and an indefinite snooze can't grow them without bound. A
catch-up's report is aged with its batch: it stays while any event of the batch
is kept (newer than 30 days or still open), so 30 days after its newest event. A
report is the one the notification was built from: up to 200 lines per file, each
clipped to 500 characters, at most 64 KiB per event or catch-up (beyond that,
files are kept as path-only stubs). That is a superset of the
Telegram excerpt: the lines beyond `telegram_lines` never left the Mac, but they
now sit at rest in the local database. This is accepted under this threat model:
they come from the same redaction-filtered reports (a redacted file never has
lines, and the writer drops any anyway), and whoever can read `state.db` can read
the project files themselves. The web UI returns them only to a logged-in session
on 127.0.0.1, and never for a file the current redaction list covers (its lines
and note are dropped at response time).

## Contents at rest (content snapshots)
Since [ADR 0017](0017-content-snapshots.md), `state.db` also stores whole files:
the last-seen content of each watched file (`snapshots`) and each event's
before/after pair (`event_contents`). The *set* of files is the one notifications
already describe; the *amount* is new. The limits:

- **Size:** up to 4 MiB per file, 16 MiB per event, 32 MiB of snapshots and 64 MiB
  of `event_contents` per project. Typically tens of KB per project; the caps bound
  the worst case (lockfiles changing constantly) at about 100 MiB per project,
  roughly 1,000× a stored diff (≤ 64 KiB). That is the price of exact diffs.
- **Time:** 30 days for `event_contents` (like `event_reports`); `snapshots` hold
  only the current version.
- **Where:** only in `state.db` (0600 in a 0700 directory). Temp copies for the diff
  engine live in `snapshot-tmp/` under the state dir (0700 dirs, 0600 files,
  removed after each diff; leftovers swept after an hour).

Never stored: redacted files (never opened), hard-linked files, symlinks, devices
and binary files, anything past the budgets, and anything a newly added redaction
pattern covers (deleted by the daemon when the config is applied; a failed delete
is retried by the next apply and the maintenance tick). A capture is re-filtered by
the redaction list current after it, before the event is stored. The remaining
limit: a file read under the old list just before a reload that redacts it was
already passed to git for the note; it is never stored or sent. The web never
returns contents, only the rendered reports, as before.

## Local file permissions
Spec `2026-10-09-dev-watchman-private-permissions-design.md` (Plan 4). A file's own
mode does not decide whether it is exposed: **the mode of every directory on its
path does**. A default install could end up with a 644 `config.yaml` in a 755 dir under a 755
`~/.config`, inside a 750 home, so any `staff` member could read the project paths
and patterns it lists; the 644 logs were safe only because `~/Library/Logs` is 700.
So:

- dev-watchman's own **directories are 0700 and its own files 0600**: the config
  dir and file, the state dir and its files (`state.db` with `-wal`/`-shm`,
  `status.json`, `daemon.lock`), the log dir, the logs including rotated ones,
  and `launchd.err.log`.
- **New installs** create them that way: every creation site passes an explicit
  mode (`mkdir(mode=0o700)`, `os.open(…, 0o600)`), and the log handler narrows
  every file it opens, including the one `doRollover()` recreates (only a regular
  file of ours: a log symlinked to `/dev/null` is left alone and never fails
  logging). `install` empties `launchd.err.log` without following a symlink and
  refuses (a clean `Install failed:` message) one that is not a regular file of ours.
- **Existing installs** are tightened at daemon start (`run`, logged) and in
  `dev-watchman install` (`paths.tighten_install`, one printed line per change),
  best effort: a failure is logged and never stops the daemon.
- **Tighten only, own paths only.** Only bits outside 0o700/0o600 are cleared,
  so a 400 file stays 400. Parents (`~/.config`, `~/.local/share`,
  `~/Library/Logs`, `~/Library/LaunchAgents`) and the plist are never changed. The
  config dir is tightened only when it is ours (named `dev-watchman`): a config
  placed in a shared dir (`~/dotfiles`, `$HOME`) leaves that dir alone. Symlinks
  are never followed (`lstat`), and paths owned by another uid are skipped. The one
  deliberate exception is a symlinked config file (dotfiles): its resolved target
  is tightened, its directory is left alone.
- **No process-wide umask.** Children (git writing `.git/index`) would inherit it
  and it would change files in the user's repositories. A static test keeps
  `umask` out of `src/`.

## Consequences
Fixes for accidental-leak paths (a new naming, linking or encoding trick) are bugs and
are fixed. Reports that need an attacker who can already write to the projects are
closed with a reference to this ADR. Over-redaction is always acceptable.
