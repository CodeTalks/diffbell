# 0006: Diffs are shown against HEAD

- Status: superseded by [0017](0017-content-snapshots.md) for content-based reports
  (2026-10-15). Kept for the fallback: a file with no usable snapshot is still
  reported as below.

## Decision
For tracked files, the message shows `git diff HEAD` hunk lines, labelled "vs HEAD".
New untracked files show their first lines. A tracked symlink shows its target change
via `git diff` ("vs HEAD"); only an untracked symlink gets metadata ("symlink → target").
Deletions, binaries, non-git projects, hard-linked or special files and redacted files
get metadata only. git runs
with `--literal-pathspecs`, and `diff` with `--no-textconv --no-ext-diff`, so neither
pathspec magic nor a user-configured diff driver can pull in other content.

## Trade-off
Repeated edits before a commit repeat the growing cumulative diff. Snapshot-based
"since last notification" diffs were deferred: they would mean storing file contents
and more logic, and they'll only be added if the repetition proves annoying in practice.
They were added by [0017](0017-content-snapshots.md) after committed changes (merges)
read `+0 −0`.
