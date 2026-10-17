# Security

## Reporting a vulnerability

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/CodeTalks/diffbell/security/advisories/new).
Please don't open a public issue, and never include a real bot token or chat id in
a report: describe where it leaked instead.

Every report gets a reply. A confirmed problem is fixed in a new release, credited
to you in the advisory unless you'd rather not be named.

## Supported versions

Only the latest release gets fixes. Upgrade with `brew upgrade diffbell` (then
`diffbell install`), or with `diffbell upgrade` for an install from a checkout.

## What counts

Diffbell's threat model is accidental disclosure
([ADR 0007](docs/adr/0007-threat-model-accidental-disclosure.md)). In scope:

- The bot token or the chat id leaving the Keychain: in a log, a process argument, a
  web response, browser storage or an error message.
- A redacted file's contents (or a file the redaction list should cover) reaching
  Telegram, the web UI, stored diffs or git.
- The local web UI: getting a login token, or a session, without the per-start
  secret's proof; breaking its Content Security Policy; making the page run
  something on the machine.
- Files diffbell creates being readable by other local users.
- The release and the Homebrew formula: anything that lets someone other than the
  maintainer change what `brew install codetalks/tap/diffbell` installs.

Out of scope: an attacker who already runs code as your macOS user (they can read
the Keychain and your files directly), and Telegram's own security.
