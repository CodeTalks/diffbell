# 0004: Additive-only pattern lists

- Status: accepted (2026-10-08)

## Decision
`defaults` and `redact` only add to the built-in sets; neither can remove a built-in.
Coverage shrinks only through an explicit `ignore` line (global or per project) or
`use_defaults: false`, and `dev-watchman status` prints each project's effective
watched and excluded lists.

## Why
For a watcher, the failure that matters is silent: if a user's `defaults` list replaced
the built-ins, adding `composer.lock` would quietly stop watching `.env*` and CI
workflows everywhere, and the only symptom would be messages that never arrive. The
same fail-closed reasoning applies to redaction, where over-redaction (e.g. a
`secrets.py` source file, every file under a directory matching a redact pattern
without `/` such as `config/secrets/`, or case-insensitive matching on a
case-sensitive volume) is accepted and documented. Patterns the local redaction matcher cannot honor exactly are
rejected at load time instead of silently redacting nothing. Component matching is
only for patterns without `/`: an anchored pattern such as `config/private` matches
that one path, so a user redacting a directory writes `config/private/**`.
