# 0023: Name the Mac in every message

- Status: accepted (2026-10-18).

## Context
The daemon only sends to Telegram: `getUpdates` is called only during setup, and no
message has inline buttons. So several Macs can already share one bot and one chat.
What was missing: no message, alert or heartbeat said which Mac sent it, and the
"Open #42" deep link opens 127.0.0.1 on whatever device it is tapped on, while event
ids are numbered per Mac. The setup guide never explained a second Mac.

## Decision
- A new setting, `notify.machine`. Absent or null: macOS's Computer Name. `""`: no
  name. Any other text: that name, as given.
- `machine.py` owns the name. `computer_name()` runs `/usr/sbin/scutil --get
  ComputerName` (2 s timeout, `check=False`), else uses the host name without a
  trailing `.local`, and returns None if both fail; it never raises. `clean()` strips
  the text, drops control and format characters (Cc, Cf, which include the bidi
  controls), collapses inner whitespace and cuts the result to 40 characters. `resolve()`
  turns the setting into the name messages carry (None: none). `signature()` returns
  "Diffbell" or "Diffbell (Work MBP)".
- The config refuses a name that, after strip, is longer than 40 characters or
  holds a control or format character (`config.machine_name_ok`). It is refused,
  not cleaned, so the user sees what is wrong. The message is in
  `settings_check.MESSAGES["machine_name"]` (ADR 0019): "Use at most 40 characters,
  without control characters."
- The daemon resolves the name at start and at every reload (`_apply_config`). It
  passes the name to `NotifierSettings.machine` and a `signature` callable to
  `Health` and the `Heartbeat`; each reads the callable at send time. Every new
  parameter defaults to no name, so the texts read as before.
- Where the name appears:
  - an event's or a catch-up's first line ends with " · 💻 Work MBP" (escaped, not
    bold), in HTML and in the plain-text fallback;
  - the link reads "Open #42 on Work MBP" ("Open #42 (+3 more) on Work MBP");
  - alerts read "⚠️ Diffbell (Work MBP): …" and "✅ Diffbell (Work MBP): …";
  - the restart notice reads "⚠️ Diffbell (Work MBP) restarted after…";
  - the heartbeat reads "✅ Diffbell (Work MBP) · watching…";
  - the update notice reads "⬆️ Diffbell (Work MBP) updated: …";
  - `diffbell setup`'s test message reads "✅ Diffbell (Work MBP) is set up…";
  - `diffbell status` prints `machine:` after `daemon:` (or "(not named)").

  The sample message (Send test notification, `diffbell test`, the wizard's Send
  test message) stays nameless.
- Settings → Notifications has a switch, "Name this Mac in messages" (on by
  default), and a field, "This Mac's name", whose placeholder is the detected
  Computer Name (`GET /api/settings` returns `notify.machine` and
  `notify.machine_default`). Off writes `machine: ""`. On with an empty field
  removes the key, so the Computer Name is used. On with text writes the text,
  double-quoted. `config_edit` writes the key only when that changes what is in
  force, and keeps it as it is when the request doesn't send it.
- The guide (`guide.json`, `docs/telegram-setup.md`) has a section "Using Diffbell
  on more than one Mac".

## Consequences
- With several Macs on one bot, every message says where it came from, and the link
  says which Mac's page it opens.
- After an upgrade, messages carry the Computer Name at once. `notify.machine: ""`
  (or the switch) restores the old texts.
- A rename in System Settings is picked up at the next start or config reload, not
  live.
- Tests never run `scutil`: a conftest belt makes `machine.computer_name` return
  "Test Mac". Tests that pin the nameless texts use the `nameless_mac` fixture.
