"""Command-line entrypoint (spec §9)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import urlencode

import httpcore

from diffbell import (
    BRAND,
    __version__,
    doctor,
    launchd,
    liveness,
    machine,
    paths,
    rename_install,
    upgrade,
)
from diffbell.config import (
    Config,
    ConfigError,
    MissingProject,
    ResolvedProject,
    load_and_resolve,
    load_config,
    project_identities,
    resolve_projects,
    write_default_config,
)
from diffbell.daemon import Daemon
from diffbell.lock import AlreadyRunning, LockUnreadable, holder_pid, singleton_lock
from diffbell.render import sample_message
from diffbell.secrets import (
    Credentials,
    Missing,
    SecretStoreError,
    credentials,
    get_secret,
)
from diffbell.sender_slot import SenderSlot
from diffbell.setup_cmd import SetupIO, run_setup
from diffbell.store import SCHEMA_VERSION, EventRow, MigrationError, Store
from diffbell.telegram import ErrorKind, TelegramClient, TelegramError
from diffbell.web import auth, config_edit
from diffbell.web.config_edit import ConfigConflict, ConfigInvalid, ConfigUnreadable

log = logging.getLogger("diffbell")

NO_CREDENTIALS = (
    "Telegram isn't set up: use Settings → Telegram in the web UI (`diffbell web`), "
    "or run `diffbell setup`."
)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Every log file it opens is narrowed to 0600: the first one and each one
    ``doRollover()`` recreates. Renamed backups keep their mode. Tighten only, and
    only a regular file of ours: a log symlinked elsewhere (e.g. /dev/null) is left
    alone and never fails logging."""

    def _open(self) -> TextIOWrapper:
        stream = super()._open()
        try:
            st = os.fstat(stream.fileno())
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                os.fchmod(stream.fileno(), stat.S_IMODE(st.st_mode) & 0o600)
        except OSError:
            pass
        return stream


def configure_logging(log_file: Path | None, verbose: bool) -> None:
    handler: logging.Handler
    if log_file is not None:
        log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = PrivateRotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    # Set explicitly (even with --verbose); telegram.py also adds a redacting filter.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _credential_state(**kwargs: Any) -> Credentials | Missing:
    """The Keychain's answer (bot onboarding spec §1): the pair, or why Telegram is
    not set up (both absent, one item, or an unfinished save). Raises
    SecretStoreError. ``kwargs`` (e.g. ``timeout``) are passed to ``get_secret``."""
    return credentials(get=lambda account: get_secret(account, **kwargs))


# -- run -----------------------------------------------------------------------

# Right after login (when launchd first starts the agent) the Keychain can take far
# longer than usual to answer, so `run` reads it patiently and more than once.
RUN_KEYCHAIN_ATTEMPTS = 3
RUN_KEYCHAIN_TIMEOUT = 30
RUN_KEYCHAIN_RETRY_DELAY = 5

_sleep: Callable[[float], None] = time.sleep  # a seam for tests


def _run_credentials() -> Credentials | Missing:
    """Like ``_credential_state`` with retries; "not set up" is final, not retried.

    Raises the last SecretStoreError once every attempt has failed."""
    for attempt in range(1, RUN_KEYCHAIN_ATTEMPTS + 1):
        try:
            return _credential_state(timeout=RUN_KEYCHAIN_TIMEOUT)
        except SecretStoreError as exc:
            log.warning(
                "keychain read failed (attempt %d/%d): %s", attempt, RUN_KEYCHAIN_ATTEMPTS, exc
            )
            if attempt == RUN_KEYCHAIN_ATTEMPTS:
                raise
            _sleep(RUN_KEYCHAIN_RETRY_DELAY)
    raise AssertionError("unreachable")  # pragma: no cover


def _record_start_failure(reason: str) -> None:
    """status.json says this run failed (health spec §5.1), so the first start that
    succeeds sends one message with the reason: "could not start earlier" if the
    daemon never started, "restarted after an unexpected stop" if it had. Never over
    a running daemon's file, and never raises."""
    state = paths.state_dir()
    try:
        if holder_pid(state / "daemon.lock") is None:
            liveness.write_failure_status(
                state, reason, time.time(), started_at=liveness.started_at()
            )
    except Exception:
        log.exception("could not record the failed start in status.json")


def cmd_run(args: argparse.Namespace) -> int:
    configure_logging(args.log_file, args.verbose)
    liveness.clear_started()  # set again by Daemon.run once this run's daemon starts
    # Best effort, never stops the daemon: narrows an install older than ADR 0007's
    # "Local file permissions" to 0700 dirs and 0600 files.
    paths.tighten_install(paths.config_path(), paths.state_dir(), paths.log_dir())
    try:
        creds = _run_credentials()
    except SecretStoreError as exc:
        # A Keychain that can't be read is never taken for a fresh install.
        log.error("giving up after %d keychain attempts: %s", RUN_KEYCHAIN_ATTEMPTS, exc)
        _record_start_failure(f"keychain read failed: {exc}")
        return 2
    if isinstance(creds, Missing):
        log.warning(
            "Telegram %s. Watching and holding changes until it is set up "
            "(Settings → Telegram, or `diffbell setup`)",
            creds.message,
        )
    try:
        with singleton_lock(paths.state_dir() / "daemon.lock"):
            return asyncio.run(_run_daemon(creds))
    except AlreadyRunning as exc:
        log.error("%s", exc)  # another daemon runs: its status.json is left alone
        return 3
    except ConfigError as exc:
        log.error("%s", exc)
        _record_start_failure(str(exc))
        return 2
    except MigrationError as exc:
        # Rolled back, the database is unchanged. launchd restarts the daemon every
        # 10 s and each attempt fails the same way: `diffbell uninstall` stops it.
        log.error("%s", exc)
        _record_start_failure(str(exc))
        return 2
    except RuntimeError as exc:
        # A background task died (Daemon.run): exit non-zero so launchd restarts us.
        # Its final snapshot already says why; recording keeps that snapshot (no
        # failed_start), so the next start says "restarted after an unexpected stop".
        # A RuntimeError before the daemon started is a failed start.
        log.exception("diffbell stopped abnormally: %s", exc)
        _record_start_failure(str(exc))
        return 1
    except Exception as exc:
        # Anything else that stops the start (sqlite3.DatabaseError from the Store, an
        # OSError on the state directory, a failure in Daemon.run before its first
        # snapshot): recorded, then raised as before (entrypoint logs it, exit 1).
        # After the daemon started, it is recorded as an unexpected stop instead.
        _record_start_failure(f"{type(exc).__name__}: {exc}")
        raise


async def _run_daemon(creds: Credentials | Missing) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    state = paths.state_dir()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(state / "state.db")
    # Empty while Telegram is not set up: the daemon watches and holds (spec §1).
    slot = SenderSlot(
        TelegramClient(creds.token, creds.chat_id) if isinstance(creds, Credentials) else None,
        make_client=TelegramClient,
    )
    try:
        await Daemon(
            paths.config_path(),
            store,
            slot,
            state_dir=state,
            setup_missing=creds if isinstance(creds, Missing) else None,
        ).run(stop)
    finally:
        await slot.aclose()
        store.close()
    return 0


# -- setup / test ----------------------------------------------------------------


def _daemon_running() -> bool:
    """True if a daemon holds the lock. An unreadable lock counts as running: the
    only use is `setup`'s restart hint, which is harmless when unneeded."""
    try:
        return holder_pid(paths.state_dir() / "daemon.lock") is not None
    except LockUnreadable:
        return True


def cmd_setup(args: argparse.Namespace) -> int:
    return asyncio.run(
        run_setup(
            SetupIO(),
            make_client=lambda token, chat: TelegramClient(token, chat),
            config_path=paths.config_path(),
            daemon_running=_daemon_running,
        )
    )


def cmd_test(args: argparse.Namespace) -> int:
    try:
        state = _credential_state()
    except SecretStoreError as exc:
        print(f"Keychain error: {exc}")
        return 2
    if isinstance(state, Missing):
        # A leftover marker reads as not set up here too: never send while the
        # daemon holds (bot onboarding spec §1, startup contract).
        print(NO_CREDENTIALS)
        if state.what != "both":
            print(f"({state.message})")
        return 2
    token, chat_id = state.token, state.chat_id
    text = sample_message(datetime.now())

    async def send() -> None:
        client = TelegramClient(token, chat_id)
        try:
            await client.send(text)
        finally:
            await client.aclose()

    try:
        asyncio.run(send())
    except TelegramError as exc:
        print(f"Send failed: {exc}")
        return 1
    print("Sample notification sent.")
    return 0


# -- status ------------------------------------------------------------------------


TELEGRAM_RESTART_HINT = (
    f"saved, but the running {BRAND} started before that: run `diffbell install`"
)


def status_report(
    *,
    daemon_pid: int | None,
    watchman_version: str | None,
    telegram: str,
    projects: list[ResolvedProject],
    missing: list[MissingProject] | None = None,
    config_error: str | None,
    snapshot: dict[str, Any] | None,
    counts: dict[str, int] | None,
    failures: list[EventRow],
    telegram_restart: bool = False,
    machine_name: str | None = None,
) -> str:
    out = [f"daemon:   {f'running (pid {daemon_pid})' if daemon_pid else 'not running'}"]
    out.append(f"machine:  {machine_name or '(not named)'}")
    out.append(f"watchman: {watchman_version or 'unreachable'}")
    out.append(f"telegram: {telegram}")
    if telegram_restart:
        out.append(f"          {TELEGRAM_RESTART_HINT}")
    if config_error:
        out.append(f"config:   INVALID\n{config_error}")
    states: dict[str, dict[str, Any]] = {}
    if snapshot:
        age = int(time.time() - float(snapshot.get("updated_at", 0)))
        if _snapshot_is_live(snapshot, daemon_pid):
            out.append(f"snapshot: {age}s old")
            if (web := _web_line(snapshot.get("web"))) is not None:
                out.append(web)
        else:
            # Written by a daemon that has stopped (or by an earlier instance): its
            # per-project states describe the past, so they are not shown.
            out.append(f"snapshot: from a stopped daemon ({age}s old), project states omitted")
            snapshot = None
    if snapshot:
        states = {
            str(s["name"]): s
            for s in snapshot.get("projects", [])
            if isinstance(s, dict) and "name" in s
        }
    out.append("projects:")
    if not projects and not missing:
        out.append("  (none configured)")
    for project in projects:
        state = states.get(project.name, {})
        out.append(f"  {project.name}  {state.get('status', 'unknown')}  {project.path}")
        for key in ("error", "note"):
            if state.get(key):
                out.append(f"    {key}: {state[key]}")
        out.append(f"    watched:  {', '.join(project.watched)}")
        out.append(f"    excluded: {', '.join(project.excluded) or '(none)'}")
    for gone in missing or []:
        out.append(f"  {gone.name}  missing  {gone.path}")
    if counts is not None:  # None: the database was not opened (not migrated yet)
        out.append(
            f"events:   pending {counts.get('pending', 0)} · sent {counts.get('sent', 0)} · "
            f"failed {counts.get('failed', 0)}"
        )
    if failures:
        out.append("recent failures:")
        out += [f"  #{e.id} {e.project}: {e.last_error}" for e in failures]
    return "\n".join(out)


def _web_line(web: Any) -> str | None:
    """The `web:` status line from a live snapshot's web block."""
    if not isinstance(web, dict):
        return None
    if not web.get("enabled"):
        return "web:      disabled (enable with `diffbell web`)"
    if web.get("running"):
        return f"web:      running on http://127.0.0.1:{web.get('port')}"
    error = web.get("last_error")
    return f"web:      down{f' ({error})' if error else ''}"


def _snapshot_is_live(snapshot: dict[str, Any], daemon_pid: int | None) -> bool:
    """True if the snapshot was written by the daemon that currently holds the lock."""
    if daemon_pid is None or snapshot.get("running") is False:
        return False
    pid = snapshot.get("pid")
    return pid is None or pid == daemon_pid


def _watchman_version() -> str | None:
    try:
        result = subprocess.run(
            # --no-local: without a server, exit 1 rather than the client's version.
            ["watchman", "--no-spawn", "--no-local", "version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return str(json.loads(result.stdout)["version"]) if result.returncode == 0 else None
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return None


def _read_gate() -> Credentials | Missing | SecretStoreError:
    """The one Keychain read of a `status` run: the gate's answer, or the read error
    as a value, so the Telegram line and the restart hint judge the same read."""
    try:
        return _credential_state()
    except SecretStoreError as exc:
        return exc


def _telegram_check(gate: Credentials | Missing | SecretStoreError) -> str:
    if isinstance(gate, SecretStoreError):
        return f"keychain error: {gate}"
    if isinstance(gate, Missing):
        return f"{gate.message} (Settings → Telegram, or `diffbell setup`)"
    token, chat_id = gate.token, gate.chat_id

    async def probe() -> str:
        client = TelegramClient(token, chat_id)
        try:
            me = await client.get_me()
            return f"@{me.get('username', '?')} ok"
        finally:
            await client.aclose()

    try:
        return asyncio.run(probe())
    except TelegramError as exc:
        return f"error: {exc}"


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sync_projects(store: Store, config: Config) -> None:
    """Sync the Store's projects with ``config``, as the daemon does at start and
    reload (stable project ids spec section 2), so this command's name lookups match
    the daemon's. Skipped for a config the daemon would reject too, and when the
    database stays locked past the busy timeout (the daemon's next sync does it)."""
    try:
        resolve_projects(config)
    except ConfigError:
        return
    try:
        store.sync_projects(project_identities(config), time.time())
    except sqlite3.OperationalError as exc:
        log.debug("projects not synced: %s", exc)


NEEDS_MIGRATING = "the database needs migrating: restart the daemon (`diffbell install`)"


def _needs_migrating(db: Path) -> bool:
    """True if ``db`` predates schema v6, read with ``mode=ro`` (opening the Store
    would migrate it). Under a running daemon older than v6 that migration breaks
    the daemon (every per-project query fails), so `status` and `web` leave it to
    the restart. False when it can't be read: the Store reports that itself."""
    if not db.exists():
        return False
    try:
        # absolute(): a relative DIFFBELL_STATE_DIR has no file URI.
        conn = sqlite3.connect(f"{db.absolute().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return version < SCHEMA_VERSION or bool(columns and "project_id" not in columns)


def cmd_status(args: argparse.Namespace) -> int:
    state = paths.state_dir()
    config: Config | None = None
    projects: list[ResolvedProject] = []
    missing: list[MissingProject] = []
    config_error: str | None = None
    try:
        config, projects, missing = load_and_resolve(paths.config_path())
    except ConfigError as exc:
        config_error = str(exc)
    counts: dict[str, int] = {}
    failures: list[EventRow] = []
    daemon_pid = holder_pid(state / "daemon.lock")
    # Never migrate under a running daemon: it may be the old code (ADR 0016).
    unmigrated = daemon_pid is not None and _needs_migrating(state / "state.db")
    if (state / "state.db").exists() and not unmigrated:  # never create it from `status`
        store = Store(state / "state.db")
        try:
            if config is not None:
                _sync_projects(store, config)
            counts = store.status_counts()
            failures = [e for e in store.recent_events(50) if e.status == "failed"][:5]
        finally:
            store.close()
    snapshot = _read_snapshot(state / "status.json")
    live = snapshot if snapshot is not None and _snapshot_is_live(snapshot, daemon_pid) else None
    # The running daemon already knows Watchman's version (connect-time query);
    # ask the binary only when there is no live snapshot or it is disconnected.
    live_version = live.get("watchman_version") if live is not None else None
    # The one stale-daemon case status can see (bot onboarding spec §1): the daemon
    # started not set up, and `setup` has saved a pair since.
    gate = _read_gate()
    restart = (
        live is not None
        and live.get("telegram_configured") is False
        and isinstance(gate, Credentials)  # a complete pair, no unfinished save
    )
    print(
        status_report(
            daemon_pid=daemon_pid,
            watchman_version=str(live_version) if live_version else _watchman_version(),
            telegram=_telegram_check(gate),
            projects=projects,
            missing=missing,
            config_error=config_error,
            snapshot=snapshot,
            counts=None if unmigrated else counts,
            failures=failures,
            telegram_restart=restart,
            machine_name=machine.resolve(config.notify.machine if config is not None else None),
        )
    )
    if unmigrated:
        print(NEEDS_MIGRATING)
    return 1 if config_error or unmigrated else 0


# -- install / uninstall -------------------------------------------------------------


WEB_TURNED_ON = "Turned on the web UI to set up Telegram (`diffbell web --disable` turns it off)."
SETUP_HINT = (
    "Telegram isn't set up: run `diffbell web` to open the setup page, or `diffbell setup`."
)
OPEN_HINT = "Run `diffbell web` to open the setup page."
NO_OLD_LOOKUP = (
    "A DIFFBELL_* override is set: not looking for an old dev-watchman install."  # old-name
)


def cmd_install(args: argparse.Namespace) -> int:
    """Move an old install first (rename spec section 3), before anything here creates
    or writes a new folder, reads the Keychain or checks the lock; then the install."""
    # Watchman first (Watchman detection spec section 1): before the move (whose step 6
    # is the agent install), before any config is written or agent started.
    if launchd.find_watchman() is None:
        print(launchd.watchman_missing_message("run `diffbell install` again"))
        return 2
    move = rename_install.for_install()
    if move is None:
        print(NO_OLD_LOOKUP)
        return _install(args)
    old, switchover = move
    try:
        return rename_install.run(
            old,
            switchover=switchover,
            install=lambda: _install(args),
            tools=rename_install.Tools.real(),
        )
    except rename_install.RenameError as exc:  # a safety net: run() catches its own
        print(f"Install refused: {exc}")
        return 1


def _install(args: argparse.Namespace) -> int:
    config_path = paths.config_path()
    # A fresh install may come here without `setup` (bot onboarding spec §1).
    if write_default_config(config_path):
        print(f"Wrote a default config: {config_path}")
    # Read before launchd (the agent's state doesn't change the answer). A read
    # error is never taken for "not set up": that would open the wizard over a bot.
    keychain: Credentials | Missing | None = None
    try:
        keychain = _credential_state()
    except SecretStoreError as exc:
        print(f"Keychain error: {exc}")
        print(
            "Telegram setup was not checked; if it isn't set up, run `diffbell web` "
            "to open the setup page, or `diffbell setup`."
        )
    try:
        _, projects, missing = load_and_resolve(config_path)
    except ConfigError as exc:
        print(exc)
        print("fix the config first (launchd would restart-loop)")
        return 2
    for gone in missing:
        print(
            f"Warning: project {gone.name}: path {gone.path} does not exist; "
            "it is skipped until it reappears"
        )
    if not projects and not missing:
        print(f"Warning: nothing to watch yet — add projects to {config_path}")
    running_pid = holder_pid(paths.state_dir() / "daemon.lock")
    # The installed agent holding the lock is fine: install restarts it.
    if running_pid is not None and running_pid != launchd.agent_pid():
        print(
            f"Warning: a diffbell daemon is already running (pid {running_pid}), "
            "probably a foreground `diffbell run`. Stop it, or the agent will keep "
            "exiting with exit code 3 (already running)."
        )
    for path, old, new in paths.tighten_install(config_path, paths.state_dir(), paths.log_dir()):
        print(f"tightened {_display(path)}: {old:o} -> {new:o}")
    try:
        executable = launchd.find_executable()
        if not _is_stable_agent_path(executable):
            print(
                f'Warning: the agent will run "{executable}". Removing that environment '
                "(e.g. a worktree .venv) breaks the agent; install it with Homebrew or "
                "`uv tool install .`, then run `diffbell install` again."
            )
        started = _install_now()
        target = launchd.install(executable, log_dir=paths.log_dir())
    except launchd.LaunchdError as exc:
        print(f"Install failed: {exc}")
        return 1
    print(f"Installed and started {launchd.LABEL} ({target}).")
    print(f"Logs: {paths.log_dir() / 'diffbell.log'}")
    code = _wait_until_ready(started)
    if code == 0 and isinstance(keychain, Missing):
        _offer_telegram_setup(config_path)
    return code


def _offer_telegram_setup(config_path: Path) -> None:
    """install's last step while Telegram isn't set up (bot onboarding spec §1). A
    stated ``web.enabled`` is the user's and is never overridden: true opens the
    page, false only prints the hint. An absent key turns the web UI on (through
    config_edit, as `web` does), then opens the page. Every failure prints a hint
    and nothing more: the agent is installed and running either way."""
    try:
        stated = config_edit.web_enabled_set(config_path)
        enabled = load_config(config_path).web.enabled
    except (ConfigError, ConfigInvalid, ConfigUnreadable) as exc:
        print(f"{config_path}: {exc}")
        print(SETUP_HINT)
        return
    if not stated:
        if _set_web_enabled(config_path, True) is None:
            print(SETUP_HINT)
            return
        print(WEB_TURNED_ON)
    elif not enabled:
        print(SETUP_HINT)
        return
    error = _open_page(config_path, next_="telegram")
    if error is not None:
        print(error)
        print(OPEN_HINT)
        return
    print("Opened the Telegram setup page in your browser (the login link is valid for 60 s).")


# Seams for tests: the start time compared with status.json, and the readiness wait.
_install_now: Callable[[], float] = time.time
_install_clock: Callable[[], float] = time.monotonic
_install_sleep: Callable[[float], None] = time.sleep


def _wait_until_ready(started: float) -> int:
    """Wait until the daemon launchd just (re)started is up (`liveness.not_ready`),
    so a `doctor` run right after `install` sees it running; 1 if it isn't within
    READY_WAIT_SECONDS. Prints the pid, the projects watched and the web UI's
    address, never its secret or a login link."""
    result = liveness.wait_until_ready(
        paths.state_dir(), started, monotonic=_install_clock, sleep=_install_sleep
    )
    if not result.ready or result.snapshot is None:
        print(
            f"Warning: the daemon did not report ready within "
            f"{liveness.READY_WAIT_SECONDS:g} s: {'; '.join(result.missing)}. "
            "Run `diffbell doctor` or see the log."
        )
        return 1
    snapshot = result.snapshot
    rows = snapshot.get("projects")
    watched = sum(
        1
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and row.get("status") == "subscribed"
    )
    print(f"Daemon running (pid {snapshot.get('pid')}), watching {watched} project(s).")
    web = snapshot.get("web")
    if isinstance(web, dict) and web.get("enabled") is True and web.get("running") is True:
        port = web.get("port")
        if isinstance(port, int) and not isinstance(port, bool):
            print(f"Web UI on http://127.0.0.1:{port}")
    return 0


def _display(path: Path) -> str:
    """``path`` with the home directory shown as ``~``."""
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def _is_stable_agent_path(executable: Path) -> bool:
    """A location the agent can keep running from: a uv tool, or Homebrew's `opt`
    path (it resolves into the diffbell Cellar; ADR 0021). A checkout's .venv is not."""
    if (Path.home() / ".local/share/uv/tools") in executable.parents:
        return True
    try:
        return launchd.homebrew_prefix(executable.resolve()) is not None
    except OSError:
        return False


def cmd_uninstall(args: argparse.Namespace) -> int:
    if launchd.uninstall():
        print(f"Stopped and removed {launchd.LABEL}.")
    else:
        print("diffbell agent was not installed.")
    return 0


# -- web -----------------------------------------------------------------------------

WEB_WAIT = 15.0  # how long `web` waits for the server to answer /healthz
WEB_PROBE = 1.0  # the "is it already up?" check before asking for a restart
START_HINT = (
    "No diffbell daemon is running: start the agent with `diffbell install` (or `diffbell run`)."
)

HEALTHZ_MAX_BODY = 64 * 1024  # a /healthz answer is ~100 bytes; never buffer more
# The only URLs `web` and `install` ever open. No quote or backslash can occur, so
# they are safe to splice into an AppleScript string literal. `next` has one
# allowed value (the Telegram setup card, bot onboarding spec §3).
# ASCII digits only ([0-9] under re.ASCII): no Unicode digit may reach the browser.
_LOGIN_URL = re.compile(
    r"http://127\.0\.0\.1:[0-9]+/login\?t=[A-Za-z0-9_-]+(&next=telegram)?", re.ASCII
)

_web_sleep: Callable[[float], None] = time.sleep  # seams for tests
_web_clock: Callable[[], float] = time.monotonic


def _open_url(url: str) -> bool:  # a seam for tests
    """Open the login link in the default browser; False if that failed.

    The URL carries the login token, so it goes to osascript on stdin: `ps` shows every
    process's argv to all local users, so it must never be an argument (no `open URL`,
    and no webbrowser.open, whose $BROWSER command would get it as one)."""
    if _LOGIN_URL.fullmatch(url) is None:
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=f'open location "{url}"',
            text=True,
            check=False,
            timeout=10,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _healthz(base: str, timeout: float, challenge: str | None = None) -> dict[str, Any] | None:
    """Probe /healthz (with ``?challenge=``).

    Returns None if nothing answered (no connection, or no response within
    ``timeout``); {} if something answered HTTP without a JSON object (any other
    status, a redirect, a body over HEALTHZ_MAX_BODY or one still arriving at the
    deadline); otherwise the JSON object. ``timeout`` bounds the whole probe: it runs
    in a daemon thread that also checks the deadline between reads, so a slow-loris
    server can't hold the CLI. Proxy settings from the environment are ignored."""
    params = {"challenge": challenge} if challenge is not None else None
    deadline = time.monotonic() + timeout
    answered = threading.Event()
    result: list[dict[str, Any] | None] = [None]

    def probe() -> None:
        result[0] = _fetch_healthz(f"{base}/healthz", params, timeout, deadline, answered)

    worker = threading.Thread(target=probe, name="healthz-probe", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return {} if answered.is_set() else None
    return result[0]


class _AnswerStream(httpcore.NetworkStream):
    """A connection that sets ``answered`` as soon as any byte arrives from the peer."""

    def __init__(self, inner: httpcore.NetworkStream, answered: threading.Event) -> None:
        self._inner = inner
        self._answered = answered

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data = self._inner.read(max_bytes, timeout)
        if data:
            self._answered.set()
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, timeout)

    def close(self) -> None:
        self._inner.close()

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _AnswerBackend(httpcore.NetworkBackend):
    """httpcore's sync backend, with every connection wrapped in an _AnswerStream."""

    def __init__(self, answered: threading.Event) -> None:
        self._inner = httpcore.SyncBackend()
        self._answered = answered

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        return _AnswerStream(stream, self._answered)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


_PROBE_ERRORS = (
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.ProtocolError,
    httpcore.UnsupportedProtocol,
    OSError,
)


def _fetch_healthz(
    url: str,
    params: dict[str, str] | None,
    timeout: float,
    deadline: float,
    answered: threading.Event,
) -> dict[str, Any] | None:
    """One /healthz request. ``answered`` is set by the first byte received (even a
    partial status line or a non-HTTP greeting), so any reply counts as foreign."""
    if params:
        url = f"{url}?{urlencode(params)}"
    timeouts = {"connect": timeout, "read": timeout, "write": timeout, "pool": timeout}
    body = bytearray()
    try:
        # httpcore directly (no httpx Client): it never reads HTTP(S)_PROXY / ALL_PROXY,
        # never follows redirects and never decompresses.
        with (
            httpcore.ConnectionPool(network_backend=_AnswerBackend(answered)) as pool,
            pool.stream("GET", url, extensions={"timeout": timeouts}) as response,
        ):
            answered.set()
            encodings = [v for k, v in response.headers if k.lower() == b"content-encoding"]
            if response.status != 200 or any(
                v.strip().lower() not in (b"", b"identity") for v in encodings
            ):
                return {}
            for chunk in response.iter_stream():  # raw bytes: no decompression bombs
                body += chunk
                if len(body) > HEALTHZ_MAX_BODY or time.monotonic() > deadline:
                    return {}
    except _PROBE_ERRORS:
        return {} if answered.is_set() else None
    except Exception:
        # Anything else (a library bug, an odd reply) must not kill the worker, which
        # would read as "nothing listening": a byte already arrived means foreign.
        log.debug("healthz probe failed", exc_info=True)  # never the URL
        return {} if answered.is_set() else None
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):  # RecursionError: deeply nested JSON
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _expected_secret(state: Path) -> bytes | None:
    """The running daemon's per-start web secret, from its owner-only status.json."""
    snapshot = _read_snapshot(state / "status.json")
    web = snapshot.get("web") if snapshot is not None else None
    raw = web.get("secret") if isinstance(web, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return bytes.fromhex(raw) or None
    except ValueError:
        return None


def _wait_for_our_server(
    base: str, state: Path, timeout: float
) -> bytes | Literal["foreign", "timeout"]:
    """Challenge /healthz with a fresh nonce until it proves knowledge of the secret our
    daemon wrote into status.json. Returns that verified secret, "foreign" (something
    answers HTTP but can't prove it) or "timeout" (nothing answers)."""
    deadline = _web_clock() + timeout
    while True:
        nonce = secrets.token_urlsafe(24)
        budget = max(0.05, min(WEB_PROBE, deadline - _web_clock()))
        body = _healthz(base, budget, challenge=nonce)
        secret = _expected_secret(state)
        if (
            body is not None
            and secret is not None
            and auth.proof_ok(secret, nonce, body.get("proof"))
        ):
            return secret
        if _web_clock() >= deadline:
            return "timeout" if body is None else "foreign"
        _web_sleep(0.25)


def _set_web_enabled(config_path: Path, enabled: bool) -> bool | None:
    """config_edit.set_web_enabled, or None (reason printed) if the config can't be
    read, parsed or written."""
    try:
        return config_edit.set_web_enabled(config_path, enabled)
    except (ConfigInvalid, ConfigConflict) as exc:
        print(f"{config_path}: {exc}")
    except ConfigUnreadable as exc:
        print(f"{config_path}: cannot read config: {exc.error.strerror or exc}")
    except OSError as exc:
        print(f"{config_path}: cannot update config: {exc.strerror or exc}")
    return None


def _refuse_to_migrate(state: Path) -> bool:
    """True (and says so) if a daemon runs on a database from before v6: opening
    the Store here would migrate it under that daemon (ADR 0016)."""
    if holder_pid(state / "daemon.lock") is None or not _needs_migrating(state / "state.db"):
        return False
    print(NEEDS_MIGRATING)
    return True


def _open_page(config_path: Path, *, next_: str | None) -> str | None:
    """`web`'s last steps, shared with `install`: wait for our web server (its
    /healthz HMAC proof under the secret in status.json), mint a login token bound
    to that secret, open the login link (``&next=telegram`` for the setup card).
    None when the browser took the link; otherwise the reason, never the URL (it
    carries the token). A token the browser didn't receive is deleted again."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return str(exc)
    base = f"http://127.0.0.1:{config.web.port}"
    state = paths.state_dir()
    outcome = _wait_for_our_server(base, state, WEB_WAIT)
    if outcome == "foreign":
        # Never hand a login token to a process that grabbed our port.
        return (
            f"Port {config.web.port} is served by another process; refusing to send a login link."
        )
    if not isinstance(outcome, bytes):  # "timeout"
        return (
            f"The web UI did not answer on {base} within {WEB_WAIT:.0f}s; "
            f"see {paths.log_dir() / 'diffbell.log'}"
        )
    try:
        store = Store(state / "state.db")
        try:
            # Bound to the secret just verified: only this server start can redeem it.
            token = auth.issue_login(store, time.time(), outcome)
            target = f"&next={next_}" if next_ else ""
            if not _open_url(f"{base}/login?t={token}{target}"):
                store.delete_logins(auth.hash_token(token))  # unused: don't let it linger
                return "Could not open the browser."
        finally:
            store.close()
    except (sqlite3.Error, OSError) as exc:
        # The type name only: the message may carry a path.
        return f"Could not create a login link ({type(exc).__name__})."
    return None


def cmd_web(args: argparse.Namespace) -> int:
    state = paths.state_dir()
    config_path = paths.config_path()
    if args.logout:
        if not (state / "state.db").exists():
            print("No web sessions.")
            return 0
        if _refuse_to_migrate(state):
            return 2
        store = Store(state / "state.db")
        try:
            count = store.delete_sessions()
            store.delete_logins()  # a pending link must not start a session afterwards
        finally:
            store.close()
        print(f"Logged out {count} web session(s).")
        return 0
    if args.disable:
        changed = _set_web_enabled(config_path, False)
        if changed is None:
            return 2
        print("Web UI disabled." if changed else "The web UI was already disabled.")
        return 0
    # The lock first: never edit the config when no daemon would act on it.
    daemon_pid = holder_pid(state / "daemon.lock")
    if daemon_pid is None:
        print(START_HINT)
        return 2
    snapshot = _read_snapshot(state / "status.json")
    if snapshot is not None and _snapshot_is_live(snapshot, daemon_pid) and "web" not in snapshot:
        # An older daemon would never start the server: don't enable it for nothing.
        print("The running daemon predates the web UI; restart it (`diffbell install`).")
        return 2
    if _refuse_to_migrate(state):
        return 2
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(exc)
        return 2
    base = f"http://127.0.0.1:{config.web.port}"
    store = Store(state / "state.db")
    try:
        _sync_projects(store, config)
        if not config.web.enabled:
            if _set_web_enabled(config_path, True) is None:
                return 2
            print(f"Enabled the web UI in {config_path}.")
        elif _healthz(base, timeout=WEB_PROBE) is None:
            # Enabled but not answering (e.g. the web task crashed): ask the daemon.
            store.add_web_request("restart", time.time())
    finally:
        store.close()
    error = _open_page(config_path, next_=None)
    if error is not None:
        print(error)  # never the URL: it carries the token
        return 1
    print(f"Opened {base}/ in your browser (the login link is valid for 60 s).")
    return 0


# -- doctor --------------------------------------------------------------------------


def _get_me(token: str) -> None:
    """Telegram getMe with ``token``; raises TelegramError. Sends no message.
    ``doctor.TELEGRAM_TIMEOUT`` bounds the whole call (httpx's timeout is per phase),
    so a hung network is a retryable error before the doctor's check deadline."""

    async def probe() -> None:
        budget = doctor.TELEGRAM_TIMEOUT
        client = TelegramClient(token, None, timeout=budget)
        try:
            async with asyncio.timeout(budget):
                await client.get_me()
        except TimeoutError:
            raise TelegramError(ErrorKind.RETRYABLE, "getMe timed out") from None
        finally:
            await client.aclose()

    asyncio.run(probe())


def doctor_env() -> doctor.Env:
    """The real runners for `doctor` (tests build their own Env with fakes)."""
    return doctor.Env(
        config_path=paths.config_path(),
        state_dir=paths.state_dir(),
        log_dir=paths.log_dir(),
        run=subprocess.run,
        secret=lambda account: get_secret(account, timeout=doctor.CHECK_TIMEOUT),
        get_me=_get_me,
        healthz=lambda port: _healthz(f"http://127.0.0.1:{port}", timeout=2.0),
        now=time.time,
        plist=launchd.plist_path(),
        uid=os.getuid(),
        agent_watchman=launchd.find_watchman,
        old_install=_old_install_probe(),
    )


def _old_install_probe() -> Callable[[], list[str] | None]:
    """Doctor's "Old install" row: not checked (None) while a scratch override is set
    (never the real Keychain or home), else what is left of the real old install, existence only."""
    if rename_install.for_install() is None:
        return lambda: None
    return lambda: rename_install.leftovers(
        rename_install.OldInstall.real(), rename_install.Tools.real()
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    env = doctor_env()
    if args.pending_source is not None:  # from `upgrade` only (hidden option)
        env = dataclasses.replace(env, pending_source=args.pending_source)
    checks = doctor.run_checks(env)
    print(doctor.to_json(checks) if args.json else doctor.render(checks))
    return doctor.exit_code(checks)


def cmd_upgrade(args: argparse.Namespace) -> int:
    options = upgrade.Options(
        state_dir=paths.state_dir(),
        from_path=args.from_path,
        allow_dirty=args.allow_dirty,
        dry_run=args.dry_run,
        no_backup=args.no_backup,
    )
    return upgrade.run_upgrade(options)


# -- entrypoint ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diffbell",
        description=f"{BRAND}: watch key files across your projects and notify a Telegram bot.",
    )
    parser.add_argument("--version", action="version", version=f"{BRAND} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=text, description=text)

    run = add("run", "run the daemon in the foreground")
    run.add_argument("--log-file", type=Path, help="log to a rotating file instead of stderr")
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    add("setup", "store bot credentials in the Keychain, send a test message")
    add("status", "daemon, Watchman, Telegram, projects and delivery status")
    add("test", "send a sample notification")
    add("install", "install and start the launchd agent")
    add("uninstall", "stop and remove the launchd agent")
    web = add("web", "open the web UI in your browser (enables it if needed)")
    mode = web.add_mutually_exclusive_group()
    mode.add_argument("--disable", action="store_true", help="turn the web UI off")
    mode.add_argument("--logout", action="store_true", help="end every web session")
    check = add("doctor", "read-only health check of the install, daemon and Watchman")
    check.add_argument("--json", action="store_true", help="print the checks as JSON")
    # `upgrade` passes the source it is installing (Plan 7 spec §4); not in --help.
    check.add_argument("--pending-source", metavar="JSON", help=argparse.SUPPRESS)
    up = add("upgrade", "install from a checkout, back up, restart the agent, check")
    up.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        metavar="PATH",
        help="the diffbell checkout (default: the one recorded at the last upgrade)",
    )
    up.add_argument("--allow-dirty", action="store_true", help="install uncommitted changes")
    up.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    up.add_argument("--no-backup", action="store_true", help="skip the database backup")
    return parser


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "run": cmd_run,
    "setup": cmd_setup,
    "status": cmd_status,
    "test": cmd_test,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "web": cmd_web,
    "doctor": cmd_doctor,
    "upgrade": cmd_upgrade,
}


def _dispatch(args: argparse.Namespace) -> int:
    try:
        return COMMANDS[args.command](args)
    except LockUnreadable as exc:
        print(f"Cannot tell whether diffbell is running: {exc}.")
        return 2
    except MigrationError as exc:  # `status` or `web` opened a database it can't migrate
        print(exc)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _dispatch(args)


def _hard_exit(code: int) -> NoReturn:  # a seam for tests
    os._exit(code)


def _exit_now(code: int) -> NoReturn:
    """Flush logs and std streams, then exit without interpreter finalization.

    A daemon whose finalization hung (seen in CPython's module-teardown GC after a
    failed start) never exits, so launchd's KeepAlive never restarts it."""
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    _hard_exit(code)


def entrypoint(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        raise SystemExit(_dispatch(args))
    try:
        code = COMMANDS["run"](args)
    except KeyboardInterrupt:
        code = 130
    except Exception:
        log.exception("diffbell run failed unexpectedly")
        code = 1
    _exit_now(code)
