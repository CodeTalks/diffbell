# 0022: Self-restart after an upgrade, and the update notice

- Status: accepted (2026-10-18).

## Context
After `brew upgrade diffbell` the running daemon keeps the old code until something
restarts it, and Homebrew deletes the old version's Cellar folder under it. ADR 0021
asked the user to run `diffbell install` after every upgrade. `brew services` does not
help: `brew upgrade` restarts no service (only `brew bundle` with `restart_service`
does), and it would register a second agent under `homebrew.mxcl.diffbell`. Long-
running user agents commonly restart themselves when they notice they were replaced.

## Decision
- `selfupdate.Install.current()` records, at daemon start, what is installed: the
  version this environment would import (`importlib.metadata`) and, for a Homebrew
  install, the resolved `<prefix>/opt/diffbell`.
- The maintenance loop asks `SelfUpdate.check` at most every 10 s (a readlink and a
  metadata read; amended from 60 s on 2026-10-19: on a second Mac the old daemon's web
  page failed for the minute it ran from the deleted Cellar). It reports a newer
  install only against that start snapshot: Homebrew's `opt` link now names another
  Cellar version (an `opt` link that went away is no new version), or, otherwise, the
  installed version changed. A process started on the new version takes its snapshot
  from the same disk, so it never sees a change: no restart loop.
- On a change, only when launchd runs the daemon (its parent is launchd, pid 1;
  `XPC_SERVICE_NAME` can't be used: libxpc aborts a process that claims a job name
  it wasn't given) and the plist's program exists and is executable, the daemon logs it and sets its
  `stop` event: a normal stop (`status.json` says not running, so no "unexpected
  stop" notice), exit 0, and launchd's `KeepAlive` starts the new version after its
  10 s throttle. Otherwise (`diffbell run` by hand) it logs one warning. A failing
  check is isolated like every maintenance step.
- At every start, `selfupdate.queue_update_notice` compares `__version__` with the
  version that ran last (kv `running_version`) and queues a Health notice
  (`UPDATE_KEY`, "⬆️ Diffbell updated: Y → X"; "changed version" for a downgrade),
  then records the running one. A first start sends nothing. `health.SILENT_NOTICES`
  sends it without a sound, like the heartbeat. It reports every upgrade path:
  this restart, `diffbell upgrade`, a reinstall.
- The page's footer shows "Diffbell <version> · © 2026 CodeTalks · MIT License"; the
  version is the running daemon's (`/api/dashboard` `daemon.version`), so a pending
  restart is visible there.

## Consequences
- `brew upgrade diffbell` is the whole upgrade: within about 20 s (the check, then
  launchd's 10 s throttle) the agent runs
  the new version and Telegram says so, silently. The formula's caveat and the README
  drop "run `diffbell install` after every upgrade".
- A daemon run by hand never stops itself; it warns and keeps the old code.
- Tests never act as launchd runs them: `tests/conftest.py`'s belt replaces
  `selfupdate.under_launchd`.
