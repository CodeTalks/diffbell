# 0005: Explicit project list

- Status: accepted (2026-10-08)

## Decision
Only projects listed in `config.yaml` are watched; there is no auto-discovery under a
root directory.

## Consequences
Nothing is watched by surprise, and each project's patterns are reviewable in one
place. New repos must be added by hand (the web UI in Plan 2 makes this one click).
