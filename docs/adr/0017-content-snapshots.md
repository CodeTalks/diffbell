# 0017: Content snapshots: a diff shows what changed in this burst

- Status: accepted (2026-10-15). Supersedes [0006](0006-diff-vs-head.md) for
  content-based reports; 0006 stays for the fallback.

## Context
A notification's diff was computed with git, against `HEAD`, at send time. A change
already committed (a merge, a pull, `commit -a`) read `+0 −0` ("matches HEAD"), a
project without git never showed lines, and held events and catch-ups were diffed
against whatever `HEAD`, branch and working tree were when the message went out.
Seen in use on 2026-10-15: the `+0 −0` events were git merges in a watched repository.
Spec: `2026-10-15-content-snapshots-design.md` (in the maintainer's local design notes, not published).

## Decision
- **The baseline is the previous event.** `state.db` keeps the last-seen content of
  every watched, non-redacted text file within the budgets (`snapshots`). An
  event's diff is that "before" against the file at the event's creation.
- **Frozen at creation.** The "after", the git note and the branch are captured
  when the event is created, in the `create_event` transaction
  (`event_contents`, `event_meta`), and the snapshots advance in that same
  transaction, also for events the rules skip or a pause holds. A message sent
  later, retried, or caught up is built only from stored data. The frozen branch
  is also what `event_reports.branch` stores, so the message and the page agree.
  An event whose project was removed from the config after its creation still
  renders from its stored contents and frozen branch; its folder is not read
  (paths without usable contents get name and kind only). A project removed
  during the capture keeps its event's contents, but no snapshot is stored for it.
- **A catch-up** diffs, per path, the first held event's "before" against the last
  one's "after", with the last event's note and branch (a catch-up message now
  shows a branch). A path whose opening "before" or closing "after" is gone falls
  back on its own; a later event's "before" is never substituted (it would show
  only part of the hold's change).
- **git is the diff engine and the source of a note, nothing more.** The engine is
  `git diff --no-index` on two private temp copies, so lockfiles keep git's diff
  quality (difflib produced wrong diffs on repeated lines). The note says
  "committed" (the captured content is `HEAD`'s blob, by blob id from one
  `ls-tree` without `-l`: no blob is ever read), "uncommitted" or "untracked". The
  object format is cached per repository, keyed by its toplevel.
- **Isolation, every part load-bearing.** The engine runs with a minimal
  environment (`env -i`): a private empty `HOME` and `XDG_CONFIG_HOME` (the primary
  guard: dropping the user's config also drops `core.attributesFile`, and git then
  falls back to `$XDG_CONFIG_HOME/git/attributes`), `GIT_CONFIG_NOSYSTEM`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_ATTR_NOSYSTEM`, `LC_ALL=C`, and `PATH` set to
  the directory of the git resolved at start. It pins `core.autocrlf=false`,
  `core.attributesFile=/dev/null`, the diff algorithm and the indent heuristic, and
  passes `--no-color --no-ext-diff --no-textconv`. The temp dir must not sit in a
  work tree (a repository's own `.gitattributes` would apply): checked once per
  start under the same environment, with three outcomes: isolated, inside a work
  tree, or unknown (the check could not tell). Either non-isolated outcome makes
  every content report use difflib, marked "approximate", and Health shows a
  warning ("inside a git repository", or "could not verify" for unknown).
- **Attributes are ignored on purpose**, not only for hygiene: a user whose global
  attributes mark `*.lock` binary now gets a real text diff for lockfiles. **Line
  endings are diffed as they are** (`core.autocrlf=false`): a CRLF-only edit, which
  could look like no change under `autocrlf=input`, is a real diff.
- **A result is trusted only on a positive signal.** `--no-index` exits 1 both for
  "differ" and for "could not access": a diff needs empty stderr and a hunk or
  `diff --git` line, a binary verdict needs git's "Binary files" line, and rc 0
  with no output is "no content change". Anything else (stderr, another rc, a
  timeout, a missing git, unparseable output, a failed temp write) takes the
  fallback, logged by outcome kind only; an exception out of the engine is that
  path's fallback too (logged by type only), never a failed delivery. git's output is split on "\n" only
  (`diffing.split_lines`, and `_git` decodes the bytes itself instead of text
  mode): a "\r" or another line separator in a file's content can't fake a
  `diff --git` header, a hunk header or a changed line.
- **Display of control characters.** A changed line may therefore hold "\r", other
  C0 controls, DEL, C1 (U+0085 among them), U+2028/U+2029 and the bidi controls,
  which a Telegram client or the page would draw as a line break, a space, a box or
  nothing, or which reorder the text (Trojan Source). At display time only
  (storage, search, `event_reports` and "Copy diff" keep the raw text), each
  becomes ONE visible character, one UTF-16 unit for one so search-mark and
  word-diff offsets and `tg_len` hold: C0 but "\t" its Control Picture (U+2400 +
  code point, "\r" → "␍"), DEL "␡" (U+2421), C1, U+2028, U+2029, U+061C, U+200E,
  U+200F, U+202A–U+202E and U+2066–U+2069 "⍰" (U+2370). `render.visible` and
  `diffview.js`'s `visible` are the same mapping (a parity test over every BMP
  code point); the page applies it to segment text after the ranges are worked
  out. When every line of a file's report ends in "\r" (a CRLF file), the display
  drops that "\r"; with mixed endings each shows as "␍", so a CRLF→LF change reads
  `-a␍` / `+a`. In names and notes Telegram shows the same characters as `\uXXXX`, and the page's
  name display (`dom.shown`) uses the same set and form (display only; links,
  editor URLs and copied commands keep the raw name).
  `doctor`'s log check splits on "\n" only too, and counts only the daemon's own
  start record as a start.
- **The fallback** is today's behavior (vs `HEAD` for a tracked file, first lines
  for a new untracked file, name and kind otherwise), per path, for a file with no
  usable snapshot: the first event after a baseline that skipped it, an evicted or
  pruned content, an uncapturable file.
- **Why "after" is stored too.** A catch-up needs the last "after" even when
  intermediate events were pruned, and a pruned event can't break its successor's
  diff. Don't "optimize" it away.
- **Flushes must stay serial.** The next event's "before" is exactly this one's
  "after" only because `flush` runs from the debouncer task alone, awaited in
  order, and the read-then-`create_event` pair is the only writer that advances
  snapshots from captured afters. The REBASELINE also writes snapshots
  (`replace_baseline(..., snapshots_epoch=)`), but it may never regress one: it
  stores no content for a path with a journal row not yet in an event (that row's
  flush captures the after; the path falls back once), and it stores nothing if
  any other snapshot write (`create_event`, `sync_projects`,
  `drop_stale_snapshots`, `forget_redacted`, each bumping a per-project in-memory
  epoch) happened during its read. A snapshot never regresses; a path may fall
  back once.
- **One reader.** The hardened reader (`diffing.read_capturable`) is the only
  reader for the baseline, creation and the note: no redacted name is ever opened,
  no symlink followed, no hard-linked or special file read, the opened fd is
  re-verified, and size and text are decided on the bytes read.
- **Budgets and eviction.** 4 MiB per file, 16 MiB of before plus after per event
  (path order; each read is bounded by what is left, a spent budget opens no
  further file, and a file past it is "uncapturable" with neither side stored,
  while one that turned binary within it keeps its "before"), 32 MiB of snapshots per project
  (files that change win: the least recently captured snapshots of other files
  are evicted, by `(captured_at, path)`; the current event's paths never), and
  64 MiB of stored pairs per project (settled events' contents go first, then
  waiting ones, oldest first; their `event_reports` stay). The baseline fills in
  path order without eviction; its read stops once the 32 MiB budget is full and
  after at most 2000 files.
- **Retention.** `event_contents` and `event_meta` live 30 days, like
  `event_reports`, whatever the event's status. The 64 MiB cap can bind sooner
  (a long Telegram outage): those paths then fall back. A documented degradation
  under load, not a broken promise.
- **Housekeeping.** A removed project's snapshots go after the sync. A redaction
  list that grows deletes the covered snapshots and stored contents
  (`forget_redacted`, run only in the daemon process; a failed one stays pending
  and is retried by the next apply and by the maintenance tick, at most once a
  minute). A capture is
  re-filtered by the redaction list current after it, before `create_event`
  (`Notifier._still_unredacted`), so a reload during the capture stores nothing it
  redacts. Temp dirs live under the state dir, 0700/0600, and leftovers older than
  an hour are swept by `Daemon._prune` (at start and daily), independent of the
  store prune.
- **The downgrade guard.** No schema version bump (the tables are additive), so
  older code can run on the same database without advancing the snapshots. Every
  `create_event` and `replace_baseline` writing contents records
  `kv.snapshots_seen_at` in its transaction, and each maintenance tick records it
  too; at start (after the watcher's reconcile, before the recovered journal rows
  are enqueued and Watchman connects), a clock advanced more than 10 minutes after
  that mark means older code ran: every snapshot is dropped and the next events
  fall back once.

## Consequences
Messages show the burst's own change in every project, with or without git, and
a held or retried message shows what happened when its event was created.

- **The baseline read stalls the PDU pump.** It runs off the event loop, but the
  pump awaits it, so PDUs queue meanwhile (the inbox is unbounded: nothing is
  lost). The stall is bounded by the 32 MiB budget (the read stops when it is
  full) and by `BASELINE_MAX_FILES` (2000 files read per baseline); past the cap a
  file has no snapshot and its first event falls back once.
- **Delivery's IO profile.** Each content-based file costs one git process, one
  `mkdtemp` and two 0600 writes, repeated on every retry: the same process count
  as the old per-file `git diff HEAD`, but new disk IO under the state dir.
- **No git binary:** content reports take the fallback (names only for a git-less
  project), even though difflib exists for the work-tree case. On macOS
  `/usr/bin/git` is a CLT stub that fails when run, and the outcome mapping already
  sends that to the fallback; difflib stands in only where git exists but can't
  be trusted.

The cost is storage: up to about 100 MiB per project in the worst case (lockfiles
changing constantly), tens of KB typically; ADR 0007 covers it. The "committed"
note has a documented limit: in a repository that normalises content on commit
(`core.autocrlf=input`, `.gitattributes` `eol`/`text`, `clean` filters) a file can
read "uncommitted" while `git status` is clean; running the filters to compare
would bring back the `clean`-filter residual.
