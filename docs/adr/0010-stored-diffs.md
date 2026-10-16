# 0010: Stored diffs over live diffs

- Status: accepted (2026-10-09)

## Context
The web UI shows what each notification was about (spec
`2026-10-09-dev-watchman-ui-refine-design.md`, item 12). The diff could be
computed when the page opens it, or kept from the moment the notification was
built.

## Decision
- **U2:** store the report at delivery. Before each Telegram attempt the
  notifier writes the redacted `FileReport`s it built the message from into
  `event_reports` (schema v3, one row per event, `INSERT OR REPLACE`, pruned with
  the event by `ON DELETE CASCADE`, and since schema v4 at 30 days whatever the
  event's status; see ADR 0007 and [ADR 0011](0011-pause-and-rules-in-the-notifier.md)). The write is best effort: it never blocks or
  fails a send, and a failure logs only the exception's type name.
- **Superset:** the stored copy holds up to `MAX_COLLECT_LINES` (200) lines per
  file, not only the `diff.max_lines` excerpt. `compose()` reports the effective
  budget and the number of files it listed (`Composed(text, lines, files)`), and
  the row keeps them (`telegram_lines`, `telegram_files`, updated after a
  plain-text fallback), so the page marks where the Telegram excerpt ended.
- **Caps:** lines clipped to 500 characters. A file is stored whole only if the
  JSON, plus a stub (path, kind, redacted, `truncated`) for every later file,
  stays within 64 KiB; from the first file that would not, every remaining file
  is a stub, in report order. The result is within 64 KiB whenever the stubs
  alone fit; otherwise the overshoot is bounded by the paths, which `changes`
  already stores. Redacted files never carry lines.
- **U11:** the web never returns the contents of redacted files; it may return
  the stored redacted diff the notification was built from. `GET
  /api/events/{id}` re-applies the **current** redaction list: a file redacted
  since the event loses its stored lines and note, and `added` / `removed` are
  recounted without it.

## Alternatives rejected
- A live `git diff` when the page opens an event: it shows the file against HEAD
  *now*, so after a commit or a further edit the history no longer matches the
  notification that was sent.
- Storing only the Telegram excerpt: the page could show no more than the
  message did, and a halved or collapsed message would leave files without lines.

## Consequences
- Diff lines now sit at rest in `state.db` (0600) for 30 days; ADR 0007's
  "Diffs at rest" section accepts this under the accidental-disclosure model.
- Events from before schema v3 have no row; the page says "Diff not recorded".
- `state.db` grows by at most 64 KiB per event (usually far less) unless an
  event's paths alone exceed that.
