# 0008: launchd ProcessType Standard

- Status: accepted (2026-10-08)

## Context
The LaunchAgent plist originally set `ProcessType` to `Background`, which makes
launchd run the daemon under the darwinbg policy (throttled CPU, I/O and
scheduling). Measured on a test Mac with `taskpolicy -b`, which applies the same
throttling:

- a Keychain read via `/usr/bin/security`: 0.20 s -> 2.19 s
- `dev-watchman --version` startup: 0.19 s -> 5.02 s

Under login-time contention this exceeded the Keychain timeout on the first
launchd start. In one install the first start timed out after 10 s; a later start
took about 36 s.

## Decision
`build_plist` sets `ProcessType` to `Standard`. Everything else in the plist is
unchanged. Existing installs pick it up by re-running `dev-watchman install`.

## Consequences
- Resource impact is negligible: the daemon is mostly idle, waiting on Watchman
  events.
- Startup and Keychain reads are no longer throttled, so the first start after
  login succeeds reliably.
- The Keychain read retries and the `os._exit` exit path stay as the safety net
  for a slow or stuck start.
