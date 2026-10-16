# 0003: Bot credentials in the macOS Keychain via /usr/bin/security

- Status: accepted (2026-10-08)

## Decision
The token and chat id are stored as generic passwords (service `io.688.dev-watchman`,
accounts `bot-token` and `chat-id`), plus, only while a save is in progress, a marker
item `save-pending`. The service is `secrets.service()`: `io.688.dev-watchman`, or
`DEV_WATCHMAN_KEYCHAIN_SERVICE`, an override that exists only for scratch live checks
(it must be `io.688.dev-watchman` or `io.688.dev-watchman-<suffix>`, and the
agent's plist never sets it; [ADR 0018](0018-unconfigured-start-and-web-bot-setup.md)).
Writes pass the value on stdin, twice (value plus confirmation), so it never appears in argv or `ps`. `security` runs in a new session
(`start_new_session=True`): without a controlling terminal it cannot open `/dev/tty`
and must read stdin. Every call has a 10 s timeout, and a timeout's partial output
(for a read, the secret) is never attached to the raised error.

## Two writers, one write order (amended 2026-10-15, ADR 0018)
The pair may be written by `dev-watchman setup` in a terminal or by the web wizard's
Save (Settings → Telegram), which runs inside the daemon, a launchd agent. Both call
`secrets.save_credentials`, in one order, after checking both values (non-empty,
one line; a `ValueError` writes nothing):

1. write `save-pending` (value: the current time);
2. write `chat-id`;
3. write `bot-token`;
4. delete `save-pending` (`security delete-generic-password`; "not found" counts as
   success).

Any failure stops the sequence and leaves the marker. While the marker exists the
pair is not trusted: `secrets.credentials()` reports "an earlier save didn't finish",
the daemon starts not set up, `doctor` warns, and the next successful save of either
writer clears it. A half-written pair is therefore never used after a restart.
`get_secret` keeps telling absent (exit 44 → `None`: not set up) from unreadable
(anything else → `SecretStoreError`: the start fails and is retried), so a locked
Keychain is never taken for a fresh install.

The agent updating (`add-generic-password -U`) and deleting items the terminal
created is the case the scratch-label probe verifies on a real Mac
(`scripts/live/launchd-keychain-probe.sh`): both writers run `/usr/bin/security`,
the binary the items' access-control list names.

## Why /usr/bin/security instead of a Python Keychain binding
The item's access-control list names the creating binary. With `/usr/bin/security`
that's stable, so upgrading Python or running under launchd never triggers an access
prompt that a background agent can't answer.

## Consequences
macOS only. A Linux port would need a secret-service backend.
