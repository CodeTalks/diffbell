# 0020: Rename to Diffbell

- Status: accepted (2026-10-16). Supersedes the brand rule of the branding work
  ("machine names stay dev-watchman"). Spec:
  `2026-10-16-rename-diffbell-design.md` (in the maintainer's local design notes, not published).

## Context
Before a public release (Homebrew tap, later PyPI) the product needs one name that
users type and read. The name check of 2026-10-16: "keyward" is taken on PyPI by a
secrets broker, and two SSH-key repositories use it on GitHub, in the same "keys and
secrets" space (which also made "key files" read as secrets). "diffbell" is free on
Homebrew (formula and cask), PyPI, npm and GitHub. Until now the product also had two
names: Keyward in prose and dev-watchman for everything typed. No public release existed
yet, so this is the cheapest moment to rename everything with no aliases.

## Decision
Diffbell in prose (`diffbell.BRAND`), `diffbell` for everything typed or read by a
machine. The table below is how the history docs (ADR 0001-0019 and the local design notes),
which keep the old names, are read.

| What | Old | New |
|---|---|---|
| Prose / brand (`BRAND`) | Keyward | Diffbell |
| Command, PyPI and formula name | `dev-watchman` | `diffbell` |
| Python package | `dev_watchman` | `diffbell` |
| launchd label | `io.688.dev-watchman` | `io.688.diffbell` |
| Keychain service | `io.688.dev-watchman` | `io.688.diffbell` |
| Scratch Keychain service / label | `io.688.dev-watchman-scratch` | `io.688.diffbell-scratch` |
| Config folder | `~/.config/dev-watchman` | `~/.config/diffbell` |
| State folder | `~/.local/share/dev-watchman` | `~/.local/share/diffbell` |
| Log folder | `~/Library/Logs/dev-watchman` | `~/Library/Logs/diffbell` |
| Log file | `dev-watchman.log` (+ `.1`-`.3`) | `diffbell.log` (+ `.1`-`.3`) |
| Environment variables | `DEV_WATCHMAN_*` | `DIFFBELL_*` |
| Cookies | `dw_session`, `dw_theme` | `diffbell_session`, `diffbell_theme` |
| CSRF header | `x-dw-token` | `x-diffbell-token` |
| Watchman subscriptions | `dw:<project>#<n>`, `dw-config`, `dw-config-target` | `diffbell:<project>#<n>`, `diffbell-config`, `diffbell-config-target` |
| Brand files, icon symbols | `keyward-*.svg/png`, `#keyward`, `#keyward-wordmark` | `diffbell-*`, `#diffbell`, `#diffbell-wordmark` |
| GitHub repo | `CodeTalks/dev-watchman` | `CodeTalks/diffbell` (GitHub redirects the old URL) |
| Local checkout | `~/projects/dev-watchman` | `~/projects/diffbell` |

Not renamed: `.watchmanconfig` contents, `state.db`, the Keychain account names
(`bot-token`, `chat-id`, `save-pending`), `status.json`, `install-source.json`,
`launchd.err.log`.

The tree is renamed by one scripted commit (`scripts/rename.py`); a guard test
(`tests/test_old_names_gone.py`) keeps old names out of everything except the history
docs, the move's own files and lines marked `old-name`.

## The move
`diffbell install` calls `rename_install.run` before anything else. It is
idempotent and prints one line per step:

Preamble. Guard: the real old install is acted on only with `switchover=True`, which
only `install` passes, never under a `DIFFBELL_*` scratch override and never in tests
(`REAL_SWITCHOVER_ALLOWED` is False there). Find what is left (existence only, no
Keychain value read); with something left, check HOME.

1. Stop the old agent and wait until it is unloaded and its lock is free; set the old
   plist aside outside LaunchAgents (`~/Library/io.688.dev-watchman.plist.moving`),
   so a re-login can't start a half-moved install.
2. Preflight the three folders (no symlink, no non-empty destination, same volume),
   then rename them: never merge.
3. Rename the log files.
4. Copy the credentials through `save_credentials` to the new service; the old ones
   stay until the end. An unfinished-save marker is carried over. A different Diffbell
   pair already set up is kept, and the old one is deleted at the end.
5. Follow a moved checkout (`~/projects/dev-watchman` to its sibling `diffbell`):
   database, then config, then `install-source.json`. The project keeps its id and
   history but re-baselines: a file changed during the move itself is not reported.
6. Install as usual and wait for the new agent.
7. Only then delete the old plist, the aside file and the old Keychain items.

**Old deleted last.** Rollback: while no old folder is missing, the old install goes
back as it was (a loaded agent is started again; one that wasn't loaded only gets its
plist back). Otherwise the agent stays stopped with its plist set aside, and rerunning
`diffbell install` finishes the move. `diffbell upgrade` refuses while an old install
is still present ("run `diffbell install` first"), and `doctor` has an "Old install"
row (WARN, fix `diffbell install`; never delete by hand, the Keychain items may be
the only copy of the credentials).

**The `service=` exception.** `get_secret`, `delete_secret` and `has_item` take an
explicit Keychain `service=`: the one exception to the `service()` guard, used only by
`rename_install.py` (`tests/test_secrets.py` checks call sites with `ast`).

**Scratch move (live check R).** `python -m diffbell.rename_install` with
`DIFFBELL_RENAME_FROM` set to an absolute scratch root holding the marker file
`.diffbell-rename-scratch`; the guard refuses anything overlapping a real path,
compared ignoring case and Unicode form.

## Consequences
- The web page logs out once (the session cookie's name changed) and the theme goes
  back to System once.
- The in-process wmclient-to-daemon envelope key `"_dw"` became `"_diffbell"`. Nothing
  persisted or published uses it, so there is no compatibility to keep.
- History docs (ADR 0001-0019 and the local design notes) keep the old names.
- The bell mark is spec 2; the shield is a placeholder until then.
- Supersedes the rule "machine names stay dev-watchman".
