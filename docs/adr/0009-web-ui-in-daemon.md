# 0009: Web UI runs inside the daemon

- Status: accepted (2026-10-08)

## Context
The optional web UI (spec `2026-10-08-dev-watchman-web-ui-design.md`) needs the
daemon's live state (subscriptions, connection, outbox) and must be able to change
the config. The main spec's §7 sketched an in-memory session store and a Unix
control socket for minting login tokens.

## Decision
- **W6:** FastAPI + uvicorn run inside `dev-watchman run`, on its asyncio loop
  (`web/server.py`, a fresh `uvicorn.Server` per start, on a socket bound to
  127.0.0.1 by `WebServer` itself).
- **W7:** no control socket. `dev-watchman web` writes a SHA-256-hashed,
  single-use, 60-second login token into `state.db` (shared through WAL), and a
  `web_requests` row when it needs a restart. If the browser can't be opened, it
  deletes that token again.
- **W3:** sessions last 30 days (absolute) and are stored as hashes in
  `state.db`, so they survive daemon restarts; `dev-watchman web --logout` deletes
  them all, and every pending login token.
- **W8:** the web task is outside the supervised set `{pump, debouncer,
  maintenance}`. A web failure is logged, alerted (`web`) and shown as
  `web: down`; it never stops file watching.
- **Port squatting: challenge–response with a per-start secret in the owner-only
  `status.json`.** Every server start generates a 32-byte secret, written only into
  `status.json` (created 0600 in the user's state directory, rewritten right after
  a start and removed from it when the server stops or crashes), never returned by
  any endpoint and never logged. `GET /healthz?challenge=<nonce>` returns
  HMAC-SHA256(secret, nonce). `dev-watchman web` sends a fresh nonce on every probe
  and mints a login token only after the proof verifies (`hmac.compare_digest`). A
  process that grabbed the port while our server was down (for example another
  macOS user's) can't produce a proof for a new nonce, even if it recorded earlier
  answers, so it never receives a token. The CLI keeps probing with fresh nonces
  for up to 15 s; if the last probe received any byte without a valid proof →
  "served by another process" (exit 1), otherwise → "did not answer". The retry
  window is needed: during a daemon restart the old server can still answer with
  the old secret. The token row also stores the secret's SHA-256, and
  `/login` redeems it only on the server start it was minted for. A public
  instance id was rejected: anyone can read it from `/healthz` and replay it.

## Alternatives rejected
- A separate web process with IPC: duplicate state, and a second process to keep alive.
- stdlib `http.server` in a thread: blocking, and every handler would need locking
  around the store.
- The Unix control socket: unnecessary once the store is shared; one more
  listener to secure.
- In-memory sessions: a daemon restart (or a launchd KeepAlive restart) would log
  the user out.

## Consequences
- The daemon imports FastAPI/uvicorn (a little more startup time and memory).
- The web layer never touches Watchman: it receives no `WatchmanClient`, and the
  preview walks the disk locally (W10). Status comes from the daemon's snapshot.
- Handlers must be `async def`: the SQLite connection belongs to the loop thread.
- uvicorn's signal handlers are disabled (the daemon owns SIGINT/SIGTERM), and its
  `sys.exit()` on a failed start is contained.
- `status.json` now holds a secret, so it is written owner-only (0600) from its
  first byte.
