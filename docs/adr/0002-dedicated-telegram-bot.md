# 0002: Dedicated Telegram bot

- Status: accepted (2026-10-08)

## Context
An existing bot may already carry production alerts for another project, with its token
kept only in write-only CI secrets.

## Decision
dev-watchman uses its own bot.

## Consequences
- File-change chatter can be muted without muting production incidents.
- Rotating the laptop-held token never breaks another project's alerting.
- `getUpdates` (used by `setup`) can't collide with another consumer's webhook. If the
  bot has a webhook anyway, `setup` leaves it untouched and asks for the chat id.
