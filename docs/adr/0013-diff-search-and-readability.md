# 0013: Substring search over stored diff lines; extension-based light colouring; probe-gated `<code>` blocks

- Status: accepted (2026-10-10)

## Context
The page's search (`/`) matched only file paths. Asked to also find what changed
(an image tag, a version), and to make stored diffs easier to read, without a
schema version change or a highlighting library (spec
`2026-10-10-dev-watchman-diff-readability-design.md`). Stored reports
(`event_reports`, [ADR 0010](0010-stored-diffs.md)) are JSON, at most 64 KiB per
event, kept 30 days.

## Decision
- **Search inside stored diffs:** `Store.query_events(q=…, redact=…)` also matches an
  event when a string in a file entry's `lines` array contains `q` (SQL `LIKE`,
  `_like_escape`, ASCII case-insensitive). Two steps: one query over
  `json_each(CASE WHEN json_valid(r.reports) THEN r.reports ELSE '[]' END)` (each
  entry also wrapped by a `CASE` so a non-object entry can never make a JSON
  function raise) finds the (event, path) pairs; Python then drops every pair whose
  path **today's** redaction list covers (`patterns.is_redacted`, the check the
  detail applies), so the search is never a yes/no oracle for a file redacted after
  it was stored. The ids go back into the events query as one JSON array parameter
  (`id IN (SELECT value FROM json_each(?))`), never hitting
  `SQLITE_MAX_VARIABLE_NUMBER`. Rows carry `q_in_diff`.
- **Cost bounds:** the diff part needs 2+ characters (code points, after trimming).
  The id set is memoized in the `Store`, one entry per `q` (16 at most, least
  recently used out), valid while the redact tuple and the **reports epoch** are
  unchanged. The epoch is `kv.reports_epoch`, bumped by three
  `CREATE TRIGGER IF NOT EXISTS` triggers (`AFTER INSERT`, `UPDATE`, `DELETE` on
  `event_reports`) in the writing statement's own transaction: a delivery retry's
  `INSERT OR REPLACE` (same `event_id`, new lines), the prune and the cascade from
  `events` all invalidate it. The triggers are in `SCHEMA` (every open creates
  them); `user_version` stays 5, and code without them ignores them.
- **Diff readability on the page:** `static/js/diffview.js`, pure and import-safe,
  turns each stored line into segments `{text, classes}`; `detail.js` builds them
  with `h()` (spans and `<mark>`s, text nodes only). A run of `-` lines directly
  followed by a run of `+` lines is paired by position and compared by a token
  LCS (word changes `wdel`/`wadd`; skipped over 400 characters or under 40% shared
  tokens). Light syntax colour is chosen by the path's extension only
  (`.json`, `.yaml`/`.yml`, `.toml`), per line, with no state across lines.
- **Telegram `<code>` lines, gated by a probe:** the Bot API documents that `code`
  can't be nested in other entities, yet the Plan 5a live check showed `<code>`
  inside an expandable quote accepted and monospace on the Mac and Android (while
  `<pre>` split the quote on Android). Plan 5b's first task sent one message with
  every quote diff line in its own `<code>`; the render change ships only if the
  phone and the Mac both showed one folded quote, aligned monospace lines and a
  working "Show more". Result: **passed** (2026-10-10, probe A: one unsplit
  quote, monospace aligned including YAML indents, folding works, on Android and
  the Mac).
- **One `<code>` block per file, not per line (second probe):** the per-line render
  shipped first, but Telegram apps ignore formatting entities past about 100 per
  message. Probe B (one `<code>` per quote line, 10 files × 10 short lines,
  about 123 entities) showed files 9 and 10 in plain text on the Mac and Android.
  Probe C (the same change with ONE multi-line `<code>…</code>` per file block
  inside the expandable quote, about 23 entities) rendered every file monospace,
  one unsplit quote, indents and the `-`/`+` column aligned, on both. So
  `render.py` wraps each file's indented lines, "… (N more lines)" / "… (N lines
  hidden)" marker included, in one `<code>` (lines escaped, then joined by `\n`);
  path lines stay plain.
- **Entity safety net:** `render.entity_count` counts the opening `<b>`, `<code>`,
  `<blockquote>` and `<a>` tags. When a rendering has more than `ENTITY_LIMIT` = 90
  (a message of many files, whose header `<code>` path labels alone add one each),
  `compose` renders the quote's lines without `<code>` (plain escaped lines), so the
  formatting degrades uniformly instead of half-way through the message. The check
  runs before the length check; the shrink order is unchanged. Details in spec §10
  (Plan 5b implementation notes).

## Alternatives rejected
- **FTS5:** a second copy of every stored line (more diff text at rest, ADR 0007),
  a schema version change, triggers to keep it in sync, and token-based matching
  that misses the substrings people search for (`postgres:16`, `1.0.4`). The
  `LIKE` scan stays bounded by the 30-day retention and the memo.
- **Filtering redaction in SQL:** the redaction matcher is `globs.glob_matches`
  with NFC/NFD and case folding; emulating it in SQL would match differently from
  the detail's check.
- **A memo keyed by `(MAX(event_id), COUNT(*))`:** a retry rewrites a report under
  the same `event_id`, so a stale "in diff" tag would survive.
- **A highlighting library or language detection:** a dependency and a build step
  for three config formats; guessing a language from content misfires on
  fragments. Per-line rules leave a value spanning lines plain instead of guessing.
- **`<pre>` lines in Telegram:** split the quote on Android.
- **One `<code>` per quote line:** past about 100 entities Telegram apps drop the
  formatting of the rest of the message (probe B).

## Consequences
- An idle tab with a search typed costs one `kv` read per 5 s poll; a new report
  (or a retry, prune or `set_report_telegram`) costs one rescan of at most 30
  days of stored lines (a synthetic 11 MB, 270 000-line store took about 120 ms
  cold and 2 ms memoized; the live database is checked in the plan's final task).
- Search limits are documented, not worked around: ASCII-only case folding, NFC and
  NFD don't match each other, clipped lines are searched as stored.
- Stored reports keep only the `+`/`-` lines of a diff (no `@@` headers, no
  context), so a pairing run can span two hunks; the 40% rule keeps a mismatched
  pair whole-line coloured.
- The page depends on the server's clips: `diffview.js`'s segment merge and
  some of its syntax regexes are quadratic in a line's length. They stay cheap only
  because the store clips each stored diff line to `REPORT_LINE_MAX` (500
  characters, `store.py`) and a report to 64 KiB (`REPORT_MAX_BYTES`). Raising either
  needs the page's cost checked first (a comment next to `REPORT_LINE_MAX` says so).
