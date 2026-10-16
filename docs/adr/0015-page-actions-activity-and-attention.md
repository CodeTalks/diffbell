# 0015: Editor links and copied commands on the page, a day-bucketed activity strip, a "Needs attention" view, `first_line` in the stored report

- Status: accepted (2026-10-10)

## Context
Plan 7 (spec `2026-10-10-dev-watchman-page-actions-design.md`) adds quick actions
to each file of the event detail (open it in the editor, copy its diff, copy a
`git log` command), a 14-day activity strip on each project card, and a view of
what needs attention (failed and held events plus the open health problems). The
page is served on 127.0.0.1 under a strict CSP (`default-src 'self'`), the server
never runs anything on behalf of the page, and the database stays at schema v5.

## Decision
- **Editor links, not an editor launcher.** "Open ↗" is a plain link,
  `cursor://file/<folder>/<path>:<line>` or `vscode://file/…`, each path segment
  percent-encoded (a space, `#`, `?` or `:` in a name can't change the URL), and
  always with a line (1 without a first changed line). The editors read a trailing
  `:<digits>` (or `:<digits>:<digits>`) of the decoded path as line and column
  whatever follows, so a file whose name ends like that (`build:12`) gets no link,
  nor does a name that can't be percent-encoded or a deleted file (nothing to
  open); Copy diff and git log stay. The
  browser hands it to the app registered for the scheme; the server never builds
  or opens it. The editor is the config key `web.editor: cursor | vscode | none`
  (default `cursor`; the string `"none"` hides the link; an empty or unknown value
  is a config error naming the key), edited from Settings through
  `config_edit.update_settings`, whose explicit whitelist gains only this field
  (the port stays out of reach: `WebBody` accepts `editor` alone).
- **Commands are text.** "git log" copies
  `git -C <folder> --literal-pathspecs log -p --follow -- <path>`, built on the page
  by `static/js/shell.js`: `shellQuote` mirrors Python's `shlex.quote` exactly
  (tested side by side, including that a word of only safe characters stays
  unquoted), and `--literal-pathspecs` makes a name with `*`, `?` or `[` mean that
  file, as in `diffing._git`. Copy diff copies the stored lines as listed. Nothing
  is ever executed by the page or the server.
- **`first_line` in the report JSON.** `diffing` records the **first changed
  line** (`first_changed_line`): from the first hunk header's new-side start `c`
  (`@@ -a,b +c,d @@` or the comma-less `+c`) it counts git's context lines to the
  first `+` or `-` line, so a change on line 20 opens at 20, not at the hunk's 17;
  a deletion records where it sat, at least 1 (an emptied file: 1); an untracked
  new file with lines starts at 1.
  `store.encode_reports` writes it per kept, unredacted entry and omits it
  otherwise (a size-limit stub never has it; a kept entry's cost is its own JSON,
  so the 64 KiB bound stays exact). Readers (`stored_first_line`, the page) treat a
  missing key, a non-integer (a bool included) or a value below 1 as none, so older
  reports keep working. The detail API drops it for a file today's redaction list
  covers, like the lines and the note. No schema change, no Telegram change.
- **Activity in server-made buckets.** `Store.activity(now, projects, days=14, tz)`
  counts events per project and **local day** in one query: `day_bounds` builds
  the 14 local midnights in Python (DST-aware, from dates, never `+86400`), and SQL
  joins `events` against those `[since, until)` pairs (`json_each`).
  `/api/dashboard` returns each bucket's `since`/`until`; a bar's click filters the
  feed with exactly those values, so a bar and the list it opens count the same
  events (tested against the feed's `total`, over a 25-hour day and a bucket of
  more than one page). Unconfigured projects are left out; every status counts,
  failed apart.
- **"Needs attention"** is a value of the existing status filter
  (`/api/events?status=attention` = failed and held), with the open problems
  already in `/api/dashboard` listed above the results, plus a row for each
  missing project without its own `missing:` problem (the header counts it). The
  header sentence for projects in trouble (or a serious problem outside any
  project) links to it with a new history entry, so Back restores the filters it
  cleared.

## Alternatives rejected
- **Opening files from the server** (`open -a Cursor`, a local helper endpoint):
  a page-triggered process start on the user's machine, behind a CSRF token but
  still an execution path the web UI has never had; the URL scheme needs none.
- **Grouping by `date(created_at, 'unixepoch', 'localtime')` in SQLite:** the C
  library's zone can't be injected in tests, and the page would have to rebuild the
  day's bounds itself, which drifts from the server's across a DST change.
- **A `first_line` column or a re-diff at view time:** a schema change, or git run
  for the page (the page never triggers git); the report JSON already carries
  per-file fields and old rows read as "no line".
- **A separate "attention" page:** the feed's filters, paging and detail already
  exist; a status value reuses them.

## Consequences
- The editor link works only where the editor registered its URL scheme (Cursor
  and VS Code do on macOS); `none` hides it. The page has no way to tell whether
  the app opened.
- A copied command is only as safe as `shellQuote`; it is kept equal to
  `shlex.quote` by tests, and CLAUDE.md says so.
- One extra grouped query per dashboard poll (events are pruned after 30 days).
- Reports stored before Plan 7 have no `first_line`: Open goes to the top of the
  file.
