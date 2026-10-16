# 0001: Long-lived subscriber daemon

- Status: accepted (2026-10-08)

## Context
We need near-real-time, debounced notifications of key-file changes across several
repos, and they must survive restarts without losing or flooding events.

## Decision
One long-lived process holds one Watchman `subscribe` per project (expression built
from globs, `defer_vcs`, `relative_root`, generation-suffixed names
`dw:<project>#<n>`). pywatchman is synchronous with no lock, so a single
connection-owner thread makes every Watchman call (through `WatchmanClient.call()`)
and forwards PDUs into an asyncio loop.

## Alternatives rejected
- Watchman `trigger`s: debouncing across separate processes needs shared state,
  config edits mean re-registering triggers, and failures are silent in Watchman's log.
- Periodic `since` polling: adds latency and throws away push delivery.

## Consequences
We own a daemon (managed by launchd with `KeepAlive`). Clocks and hashes persist in
SQLite so restarts catch up exactly. The daemon supervises its own background tasks:
if one dies, it exits non-zero so launchd restarts it rather than running half-broken.
