"""Wire the pipeline together and run until stopped (spec §3)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from diffbell import __version__, liveness, machine, selfupdate
from diffbell.config import (
    Config,
    ConfigError,
    MissingProject,
    ResolvedProject,
    load_and_resolve,
    project_identities,
    project_name,
    redact_patterns,
)
from diffbell.debouncer import Debouncer, run_debouncer
from diffbell.health import SETUP_KEY, Health
from diffbell.models import Change
from diffbell.notifier import Notifier, NotifierSettings
from diffbell.pausing import QuietHours
from diffbell.render import sample_message
from diffbell.secrets import Missing, save_credentials
from diffbell.sender_slot import SenderSlot
from diffbell.snapshots import Snapshotter
from diffbell.store import RETENTION, TELEGRAM_SAVED_AT, Store
from diffbell.telegram import TelegramClient
from diffbell.watcher import Watcher
from diffbell.web import config_edit
from diffbell.web.app import WebContext, create_app
from diffbell.web.preview import Previewer
from diffbell.web.server import WebServer, WebStartError
from diffbell.wmclient import WatchmanClient

log = logging.getLogger(__name__)

PRUNE_AFTER = RETENTION
PRUNE_EVERY = 24 * 3600.0
# A failed forget_redacted is retried by the maintenance tick at most this often.
FORGET_RETRY_EVERY = 60.0
# While a configured project path is missing, the config is re-resolved this often
# so a re-mounted or re-created directory is picked up without a restart (§8).
MISSING_RECHECK = 60.0
# The Health key and messages of a state dir inside a git work tree, or one the
# check could not clear (content snapshots spec §3.1): content diffs then use
# difflib, marked approximate. Advice only: no fix command.
SNAPSHOTS_PROBLEM = "snapshots"
SNAPSHOTS_WORKTREE = "the state folder is inside a git repository: diffs are approximate"
SNAPSHOTS_UNVERIFIED = (
    "could not verify the state folder is outside a git repository: diffs are approximate"
)


class Daemon:
    def __init__(
        self,
        config_path: Path,
        store: Store,
        sender: SenderSlot,
        *,
        state_dir: Path,
        wm_factory: Callable[[], Any] | None = None,
        recv_timeout: float = 0.2,
        maintenance_interval: float = 5.0,
        missing_recheck_interval: float = MISSING_RECHECK,
        clock: Callable[[], float] = time.time,
        preview_home: Path | None = None,
        # Why the slot is empty at start (from the Keychain); None when configured.
        setup_missing: Missing | None = None,
        # Watches for a newer install under the running daemon (ADR 0022); tests inject.
        self_update: Callable[[str], selfupdate.SelfUpdate] = selfupdate.SelfUpdate.at_start,
    ) -> None:
        self._self_update_factory = self_update
        self._self_update: selfupdate.SelfUpdate | None = None
        self._config_path = config_path
        self._store = store
        self._sender = sender
        self._setup_missing = setup_missing
        # Why Telegram is not set up, for GET /api/telegram (settings redesign spec §7):
        # from the Keychain read at start, "unfinished save" after a failed wizard
        # save, None after complete_setup. Cached: the page never triggers a read.
        self._setup_reason: str | None = None if setup_missing is None else setup_missing.what
        self._state_dir = state_dir
        # Content snapshots: the diff engine and its temp dirs (under the state dir),
        # shared by the notifier (capture at creation) and the watcher (the baseline).
        self._snapshots = Snapshotter(state_dir)
        self._wm_factory = wm_factory
        self._recv_timeout = recv_timeout
        self._maintenance_interval = maintenance_interval
        self._missing_recheck = missing_recheck_interval
        self._clock = clock
        self._config = Config()
        # This Mac's name in messages (ADR 0023): resolved from notify.machine at start
        # and at every reload (_apply_config); None: no name.
        self._machine: str | None = None
        self._projects: dict[str, ResolvedProject] = {}
        self._missing: dict[str, MissingProject] = {}
        # Serializes reloads: the config self-watch and the missing-path re-check
        # may otherwise interleave across awaits and apply an older read last.
        self._reload_lock = asyncio.Lock()
        self._debouncer = Debouncer(10, 60)
        # Operational problems: opened and resolved here and by the watcher, sent by
        # the maintenance tick (health spec §4).
        self._health = Health(store, sender, clock=clock, signature=self._signature)
        self._heartbeat = liveness.Heartbeat(
            store,
            sender,
            self._health,
            at=lambda: self._config.notify.heartbeat,
            projects=lambda: len(self._projects),
            configured=lambda: self._sender.configured,
            signature=self._signature,
        )
        # When this run started (status.json's last_started_at), and why it ended
        # (None: a normal stop, written as clean_shutdown).
        self._started_at: float | None = None
        self._exit_failure: str | None = None
        self._watcher: Watcher | None = None
        self._wm: WatchmanClient | None = None
        # The optional web UI. Never part of the supervised task set (W8): a web
        # failure is logged and recorded as a problem, and file watching carries on.
        self._web = WebServer()
        self._web_lock = asyncio.Lock()
        # Why the web UI is down (failed start or crash); None while it is fine.
        self._web_error: str | None = None
        # Per-start secret of the running server, written only to status.json (0600)
        # and never served or logged: /healthz answers a challenge with an HMAC under
        # it, so the CLI sends a login link only to this daemon's server.
        self._web_secret: bytes | None = None
        # Previews must stay under this directory (default: the user's home).
        self._previewer = Previewer(home=preview_home)
        self._background: set[asyncio.Task[Any]] = set()
        # (mtime_ns, size) of the config file the last rejection was about, so the
        # missing-path re-check does not report the same invalid file again.
        self._rejected_stat: tuple[int, int] | None = None
        # sha256 of the config bytes last applied / rejected (config_edit.version_of),
        # so the web editor can tell when its save took effect.
        self._config_version: str | None = None
        self._config_rejected_version: str | None = None
        # The first config applied (at start) counts as a quiet-hours change: they
        # may have been changed or removed while the daemon was down.
        self._config_applied = False
        # A forget_redacted that failed: retried by the next apply and by the
        # maintenance tick (at most once per FORGET_RETRY_EVERY), until one succeeds
        # (content snapshots spec §2.5). The stack is logged for the first of a run
        # of consecutive failures only.
        self._forget_pending = False
        self._forget_failures = 0
        self._forget_tried_at = 0.0

    def _apply_config(self, config: Config, projects: list[ResolvedProject]) -> None:
        # First (stable project ids spec §2): every name below, and the watcher's
        # reconcile after this, finds its project's id. A failure leaves the previous
        # config (and map) in place: at start the daemon exits, a reload is skipped.
        self._store.sync_projects(project_identities(config), self._clock())
        self._forget_redacted(config)
        quiet_changed = (
            not self._config_applied or config.notify.quiet_hours != self._config.notify.quiet_hours
        )
        self._config = config
        self._machine = machine.resolve(config.notify.machine)
        self._projects = {p.name: p for p in projects}
        self._debouncer.configure(config.debounce.quiet_seconds, config.debounce.max_wait_seconds)
        # The start release counts as done only once it succeeded; until then every
        # apply releases again.
        if self._apply_pause_changes(config, quiet_changed=quiet_changed):
            self._config_applied = True
        try:
            # A removed or renamed project's problems go with it, without a message.
            self._health.forget_projects_except([project_name(p) for p in config.projects])
        except sqlite3.Error:
            log.exception("dropping the problems of removed projects failed")

    def _signature(self) -> str:
        """How alerts, notices and the heartbeat sign: "Diffbell (Work MBP)"."""
        return machine.signature(self._machine)

    @property
    def machine_name(self) -> str | None:
        """This Mac's name in messages, as last resolved; None: no name."""
        return self._machine

    def _forget_redacted(self, config: Config) -> None:
        """A redaction list that gained a pattern (or the first apply: it may have
        grown while the daemon was down) deletes the snapshots and stored contents of
        every path it covers: a file redacted now keeps no content at rest (content
        snapshots spec §2.5). Never raises."""
        redact = redact_patterns(config)
        grown = not self._config_applied or bool(set(redact) - set(redact_patterns(self._config)))
        if grown or self._forget_pending:
            self._run_forget(redact)

    def _run_forget(self, redact: tuple[str, ...]) -> None:
        """Delete what ``redact`` covers; on failure keep it pending (retried by the
        next apply and maintenance tick). Never raises."""
        self._forget_tried_at = self._clock()
        try:
            deleted = self._store.forget_redacted(redact)
        except Exception as exc:
            self._forget_pending = True
            self._forget_failures += 1
            if self._forget_failures == 1:
                log.exception("deleting the contents of newly redacted files failed")
            else:
                log.warning(
                    "deleting the contents of newly redacted files failed again (%s)",
                    type(exc).__name__,
                )
            return
        self._forget_pending = False
        self._forget_failures = 0
        if deleted:
            log.info("deleted %d stored contents now covered by the redaction list", deleted)

    def _read_baseline(
        self, project: ResolvedProject, hashes: Mapping[str, str]
    ) -> Mapping[str, bytes]:
        """The watcher's baseline reader (in a worker thread): the redaction list
        current now, never a stale one."""
        return self._snapshots.read_baseline(project.path, hashes, redact_patterns(self._config))

    async def _start_snapshots(self) -> None:
        """At start, before anything is flushed or baselined (content snapshots spec
        §2.5, §3.1): the downgrade guard, the work-tree check (a Health warning while
        the state dir sits in a git work tree or the check can't clear it). The temp
        sweep is the start prune's (_prune). Never raises."""
        try:
            dropped = self._store.drop_stale_snapshots(self._clock())
        except Exception as exc:
            log.error("the snapshot downgrade check failed (%s)", type(exc).__name__)
        else:
            if dropped:
                log.warning(
                    "older code ran since the last snapshot write: dropped %d snapshots", dropped
                )
        try:
            isolation = await asyncio.to_thread(self._snapshots.check_isolation)
            if isolation == "isolated":
                self._health.resolve(SNAPSHOTS_PROBLEM)
            elif isolation == "work_tree":
                self._health.open(SNAPSHOTS_PROBLEM, "warning", SNAPSHOTS_WORKTREE)
            else:
                self._health.open(SNAPSHOTS_PROBLEM, "warning", SNAPSHOTS_UNVERIFIED)
        except Exception:
            log.exception("the snapshot work-tree check failed")

    def _prune(self) -> None:
        """The daily retention prune, and the temp dirs a crash left behind (at start
        too). Each step on its own: one failing never skips the other. Never raises."""
        try:
            self._snapshots.sweep(self._clock())
        except Exception:
            log.exception("sweeping leftover temp dirs failed")
        try:
            self._store.prune(self._clock() - PRUNE_AFTER)
        except Exception:
            log.exception("pruning old rows failed")

    def _apply_pause_changes(self, config: Config, *, quiet_changed: bool) -> bool:
        """Held events follow the config: new quiet hours make **every** held event
        due now, whatever its reason (an event held as "snooze" may be waiting on the
        quiet hours' later end), and the next catch-up check holds again whatever is
        still paused, until its new end. A removed project's snooze is deleted, its
        events due now; a renamed project keeps its id (synced first), so it stays
        snoozed under its new name (stable project ids spec §4). Never raises: a store
        error must not keep the config from being applied. Returns False if the store
        update failed."""
        now = self._clock()
        try:
            if quiet_changed:
                self._store.release_held(now)
            self._store.drop_snoozes_except([project_name(p) for p in config.projects], now)
        except sqlite3.Error:
            log.exception("updating held events for the new config failed")
            return False
        return True

    def _update_missing(
        self, missing: list[MissingProject], watched: Mapping[str, str] | None = None
    ) -> None:
        """Record the projects whose path is missing: a page-only warning each
        (`missing:<name>`), resolved once the path is back. Call it after reconcile.

        ``watched`` holds the watcher's statuses from before that reconcile: a project
        that was watched ("subscribed" or "error") when its folder went has lost its
        watch. The reconcile unsubscribed it before the watcher's tick could notice, so
        the lost watch is recorded here (serious), or a reload would swallow its ⚠️.
        The watcher's subscribe, once the folder is back, resolves it."""
        previous, self._missing = self._missing, {m.name: m for m in missing}
        statuses = self._watcher_statuses()
        for name in self._projects:
            # Every present project, not only those this process saw missing: a
            # warning saved before a restart is resolved by the first check after it.
            if name not in self._missing:
                self._health.resolve(f"missing:{name}")
        for name, gone in previous.items():
            if name not in self._missing:
                log.info(
                    "project %s: path reappeared (%s); watcher status %s",
                    name,
                    gone.path,
                    statuses.get(name),
                )
        for name, project in self._missing.items():
            if previous.get(name) == project:
                continue
            if (watched or {}).get(name) in ("subscribed", "error"):
                self._health.open(
                    f"project:{name}",
                    "serious",
                    f"project {name}: watch lost: project folder removed",
                    project=name,
                )
            self._health.open(
                f"missing:{name}",
                "warning",
                # Advice, not a command: `fix` holds only shell commands to copy.
                f"project {name}: path {project.path} does not exist; skipped until it "
                "reappears. Reconnect the drive or fix the path",
                project=name,
            )

    def _watcher_statuses(self) -> dict[str, str]:
        if self._watcher is None:
            return {}
        return {str(row["name"]): str(row["status"]) for row in self._watcher.status()}

    def _config_stat(self) -> tuple[int, int] | None:
        try:
            st = self._config_path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def _file_version(self) -> str | None:
        try:
            return config_edit.config_version(self._config_path)
        except OSError:
            return None

    def _stable_version(self, before: str | None) -> str | None:
        """``before`` if the file still hashes the same after it was parsed (so the
        version names the bytes that were applied or rejected), else None. A file that
        changed in between triggers another reload, which records its own version."""
        if before is not None and self._file_version() == before:
            return before
        return None

    def _notifier_settings(self) -> NotifierSettings:
        """Read per delivery. The deep link and the "see web UI" hint follow the
        running server, not the config: none while it is not (yet) up or is being
        stopped (the link would be dead when tapped), and the port it is bound to."""
        quiet = self._config.notify.quiet_hours
        running = self._web.running and not self._web.stopping
        return NotifierSettings(
            redact=redact_patterns(self._config),
            max_lines=self._config.diff.max_lines,
            web_enabled=running,
            web_port=self._web.port if running else None,
            quiet=QuietHours.parse(quiet) if quiet is not None else None,
            machine=self._machine,
        )

    def _projects_get(self, name: str) -> ResolvedProject | None:
        return self._projects.get(name)

    def _configured_notify_on(self, name: str) -> tuple[str, ...] | None:
        """The notify_on of the configured project ``name``, also while its folder is
        missing (it is then not resolved); None when no such project is configured."""
        if name in self._projects:
            return self._projects[name].notify_on
        for project in self._config.projects:
            if project_name(project) == name:
                return tuple(project.notify_on)
        return None

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set. Raises ConfigError if the initial config is invalid,
        and RuntimeError if a background task (pump, debouncer, maintenance) ends while
        ``stop`` is unset, so the process exits non-zero and launchd restarts it.

        On shutdown, batches still inside their debounce window are not flushed: their
        journal rows stay unflushed and are reloaded on the next start (§5 step 6).
        The last status.json written on the way out says ``"running": false``.
        """
        before = self._file_version()
        config, projects, missing = load_and_resolve(self._config_path)
        self._apply_config(config, projects)
        self._health.resolve("config")  # started with a valid config
        self._record_setup()
        self._config_version = self._stable_version(before)
        # The previous status.json, read before this run writes its own (§5.1).
        self._started_at = self._clock()
        previous = liveness.read_status(self._state_dir)
        liveness.queue_restart_notice(
            self._store, self._health, previous, self._started_at, brand=self._signature()
        )
        try:
            selfupdate.queue_update_notice(
                self._store, self._health, __version__, self._signature()
            )
        except Exception:
            log.exception("recording the running version failed")
        try:
            # What is installed now, so a later change means a newer install (ADR 0022).
            self._self_update = self._self_update_factory(__version__)
        except Exception:
            log.exception("reading the install failed: no restart after an upgrade")
        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_message(msg: dict[str, Any]) -> None:
            # Runs on the Watchman connection thread.
            with contextlib.suppress(RuntimeError):  # loop already closed at shutdown
                loop.call_soon_threadsafe(inbox.put_nowait, msg)

        def enqueue(changes: list[Change]) -> None:
            self._debouncer.add(changes, loop.time())

        wm = self._wm = WatchmanClient(
            on_message, client_factory=self._wm_factory, recv_timeout=self._recv_timeout
        )
        notifier = Notifier(
            self._store,
            self._sender,
            projects=self._projects_get,
            settings=self._notifier_settings,
            configured=self._configured_notify_on,
            clock=self._clock,
            requeue=enqueue,
            telegram_configured=lambda: self._sender.configured,
            snapshots=self._snapshots,
        )
        watcher = self._watcher = Watcher(
            wm,
            self._store,
            enqueue,
            health=self._health,
            config_path=self._config_path,
            on_config_change=self._reload,
            clock=self._clock,
            baseline_reader=self._read_baseline,
        )

        async def flush(project: str, changes: list[Change]) -> None:
            await notifier.flush(project, changes)

        # Not connected yet: this only records the desired projects; subscribing
        # happens when the connection thread reports "connected". No await since
        # _apply_config's sync_projects (CLAUDE.md: sync and reconcile with no await
        # between).
        await watcher.reconcile(projects)
        # The start checks: after the reconcile, and still before anything writes
        # snapshots: nothing is baselined (not connected) or flushed (the debouncer
        # isn't running) until wm.start and the tasks below.
        await self._start_snapshots()
        # Crash recovery (§5 step 6): changes journaled but never flushed.
        enqueue(self._store.unflushed_changes())
        self._prune()  # with the temp sweep: before anything creates a temp dir
        wm.start()
        # At once, not at the first tick: a crash now must not leave the previous
        # file behind, or the next start would report the same stop again.
        self._publish_status()
        # From now on a failure is an unexpected stop, not a failed start.
        liveness.mark_started(self._started_at)
        if self._sender.configured:
            # Not on the start path and not supervised: a failed or slow getMe only
            # leaves the username None (bot onboarding spec §1).
            self._spawn(self._sender.fetch_username())
        log.info("diffbell started with %d project(s), %d missing", len(projects), len(missing))
        stop_task = asyncio.create_task(stop.wait(), name="stop")
        tasks: set[asyncio.Task[None]] = set()
        failure: str | None = None
        # From here on the connection thread runs: every exit path (including a
        # cancellation while the web server starts) goes through the cleanup below.
        try:
            self._update_missing(missing)
            await self._sync_web()
            tasks = {
                asyncio.create_task(self._pump(watcher, inbox), name="pump"),
                asyncio.create_task(
                    run_debouncer(self._debouncer, flush, stop, clock=loop.time), name="debouncer"
                ),
                asyncio.create_task(self._maintenance(watcher, notifier, stop), name="maintenance"),
            }
            done, _ = await asyncio.wait({stop_task, *tasks}, return_when=asyncio.FIRST_COMPLETED)
            if not stop.is_set():
                # A background task ended on its own: the pipeline is broken. Stop and
                # make the process exit non-zero so launchd (KeepAlive) restarts it.
                failed = next(t for t in done if t is not stop_task)
                failure = _describe_failure(failed)
                exc = None if failed.cancelled() else failed.exception()
                log.error("%s; shutting down", failure, exc_info=exc)
                stop.set()
        finally:
            # Nested try/finally: any cancellation of run() landing in any await
            # below must never skip wm.stop or the final status write.
            try:
                # Cancel every task first, so nothing awaits wm.call once wm.stop
                # has run.
                for task in (stop_task, *tasks):
                    task.cancel()
                await asyncio.gather(stop_task, *tasks, return_exceptions=True)
                # Then the background work (e.g. a web sync a reload spawned): a
                # start still in flight must not bind the port after the stop below.
                await self._cancel_background()
                # Before wm.stop: shutdown must never hang on the web server, and the
                # port must be free for the next start.
                try:
                    await self._web.stop()
                except Exception:
                    log.exception("stopping the web UI failed")
            finally:
                try:
                    await self._cancel_background()  # e.g. a web sync a reload spawned
                finally:
                    await asyncio.to_thread(wm.stop)
                    # A normal stop (SIGTERM included) is a clean shutdown; a dead
                    # background task is not (§5.1).
                    if failure is not None:
                        self._exit_failure = failure
                    elif not stop.is_set():
                        self._exit_failure = "stopped unexpectedly"
                    try:
                        self._write_status(watcher, running=False)
                    except Exception:
                        log.exception("writing the final status snapshot failed")
                    log.info("diffbell stopped")
        if failure is not None:
            raise RuntimeError(failure)

    async def _pump(self, watcher: Watcher, inbox: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            msg = await inbox.get()
            try:
                await watcher.handle(msg)
            except Exception:
                log.exception("failed to handle watchman message")
            if msg.get("_diffbell") in ("connected", "disconnected"):
                # The connection changed and (on connect) every project's subscribe
                # was tried: publish it now, not at the next maintenance tick, so
                # `install` (which waits for this) returns promptly.
                self._publish_status()

    async def _reload(self, *, recheck: bool = False) -> None:
        """Self-watch callback: apply a valid config, keep the last good one otherwise.

        Also called by maintenance (``recheck=True``) while a project path is missing.
        """
        async with self._reload_lock:
            await self._reload_locked(recheck=recheck)

    async def _reload_locked(self, *, recheck: bool) -> None:
        watcher = self._watcher
        if watcher is None:
            return
        stat = self._config_stat()
        before = self._file_version()
        try:
            config, projects, missing = load_and_resolve(self._config_path)
        except ConfigError as exc:
            if (rejected := self._stable_version(before)) is not None:
                self._config_rejected_version = rejected
            if recheck and stat is not None and stat == self._rejected_stat:
                # The periodic missing-path re-check re-read the file that was
                # already rejected (and reported): nothing new to say.
                log.debug("config rejected (unchanged): %s", exc)
                return
            self._rejected_stat = stat
            log.error("config rejected: %s", exc)
            self._health.open(
                "config",
                "serious",
                "config rejected, keeping the last good config: "
                + config_edit.describe_config_error(exc),
            )
            return
        except Exception:
            log.exception("config reload failed; keeping the last good config")
            return
        self._rejected_stat = None
        version = self._stable_version(before)
        try:
            self._apply_config(config, projects)
            watched = self._watcher_statuses()  # before the reconcile drops the missing
            await watcher.reconcile(projects)
            self._update_missing(missing, watched)
            self._health.resolve("config")
        except Exception:
            log.exception("applying the reloaded config failed")
            return
        if version is not None:
            self._config_version = version
            self._config_rejected_version = None  # it no longer describes the file
        # A sync in flight read the config before this reload: queue another, which
        # reads it again once it gets the lock (out_of_sync can't see a pending
        # start or stop, e.g. one enabling the server this reload disables).
        if self._web_lock.locked() or self._web_out_of_sync():
            # In the background: a web start/stop must not hold up the pump, which
            # runs this reload. _sync_web serializes on the web lock, reads the
            # config current when it gets the lock, and never raises.
            self._spawn(self._sync_web())
        log.log(
            logging.DEBUG if recheck else logging.INFO,
            "config reloaded: %d project(s), %d missing",
            len(projects),
            len(missing),
        )

    async def _maintenance(self, watcher: Watcher, notifier: Notifier, stop: asyncio.Event) -> None:
        """Periodic work. Each step is isolated: one failing never skips the others."""
        last_prune = last_recheck = self._clock()
        while not stop.is_set():
            if self._missing and self._clock() - last_recheck >= self._missing_recheck:
                last_recheck = self._clock()
                try:
                    await self._reload(recheck=True)
                except Exception:
                    log.exception("maintenance: re-checking missing project paths failed")
            try:
                await notifier.retry_due()
            except Exception:
                log.exception("maintenance: retrying due events failed")
            try:
                await notifier.catch_up_due()
            except Exception:
                log.exception("maintenance: sending catch-up messages failed")
            try:
                await watcher.tick()
            except Exception:
                log.exception("maintenance: watcher tick failed")
            try:
                self._check_telegram()
            except Exception:
                log.exception("maintenance: checking Telegram's health failed")
            try:
                await self._health.tick(self._clock(), sending=self._sender.configured)
            except Exception:
                log.exception("maintenance: sending alerts failed")
            try:
                await self._heartbeat.tick(self._clock())
            except Exception:
                log.exception("maintenance: the heartbeat failed")
            try:
                if "restart" in self._store.take_web_requests(self._clock()):
                    log.info("web UI restart requested")
                    await self._sync_web(restart=True)
                self._store.prune_web(self._clock())
            except Exception:
                log.exception("maintenance: web requests failed")
            try:
                self._write_status(watcher)
            except Exception:
                log.exception("maintenance: writing status.json failed")
            try:
                # A newer install: stop normally, and launchd's KeepAlive starts it.
                if self._self_update is not None and self._self_update.check(self._clock()):
                    stop.set()
            except Exception:
                log.exception("maintenance: checking for a newer install failed")
            try:
                # The downgrade guard's liveness floor (content snapshots spec §2.5).
                self._store.mark_snapshots_seen(self._clock())
            except Exception:
                log.exception("maintenance: recording the snapshot mark failed")
            # A reload's forget_redacted failed: again, backed off (a locked or
            # failing database would otherwise be hit, and logged, every tick).
            if self._forget_pending and self._clock() - self._forget_tried_at >= FORGET_RETRY_EVERY:
                self._run_forget(redact_patterns(self._config))
            if self._clock() - last_prune >= PRUNE_EVERY:
                last_prune = self._clock()  # a failing prune is retried tomorrow, not every tick
                self._prune()  # never raises: each step logs its own failure
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._maintenance_interval)

    def _check_telegram(self) -> None:
        """Telegram failing (from the outbox, no API call) is a page-only warning,
        resolved by the next successful send. While Telegram is not set up there is no
        delivery to fail: resolved, so only `telegram.setup` shows."""
        if not self._sender.configured:
            self._health.resolve("telegram")
            return
        _, error = self._store.telegram_health()
        if error is None:
            self._health.resolve("telegram")
        else:
            self._health.open("telegram", "warning", f"Telegram failing: {error}")

    def _record_setup(self) -> None:
        """At start: Telegram not set up is a page-only warning (`SETUP_KEY`) naming
        what is missing; a start with credentials resolves one saved earlier."""
        if self._sender.configured:
            self._health.resolve(SETUP_KEY)
            return
        missing = self._setup_missing or Missing("both")
        self._health.open(
            SETUP_KEY,
            "warning",
            f"Telegram {missing.message}. "
            "Set up Telegram in Settings → Telegram, or run the command.",
            fix="diffbell setup",
        )

    def setup_reason(self) -> str | None:
        """secrets.Missing.what while Telegram is not set up ("both" when the start
        gave no reason, as _record_setup words it), else None. No Keychain read."""
        if self._sender.configured:
            return None
        return self._setup_reason or "both"

    def note_unfinished_save(self) -> None:
        """The wizard's Keychain write failed: the save-pending marker may be left, so
        the next start would read "unfinished save". Said now, not at the next start."""
        self._setup_reason = "unfinished save"

    async def complete_setup(self, token: str, chat_id: str, bot_username: str | None) -> None:
        """The wizard saved a bot (bot onboarding spec §2, after the Keychain write):
        swap the client in, resolve `telegram.setup`, rearm unannounced problems,
        mark today's heartbeat done if its time passed (only on a first setup: a
        replaced bot must not skip a heartbeat the old one failed to send), and
        release the setup holds (the next catch-up check re-evaluates them).
        ``bot_username`` comes from the caller's getMe: no fetch after a swap.
        The swap is what counts; each later step is best effort and logged. Resolve,
        rearm and release are safe to repeat, and a restart redoes all of them."""
        was_configured = self._sender.configured
        await self._sender.swap(token, chat_id, bot_username=bot_username)
        now = self._clock()
        self._setup_reason = None
        # The card's failing state starts over (Store.telegram_failure): an error the
        # replaced bot got is not this one's. Best effort, like every step after the
        # swap: a failed kv write never fails a save whose swap already happened.
        try:
            self._store.kv_set(TELEGRAM_SAVED_AT, repr(now))
        except Exception as exc:
            log.warning("complete_setup: recording the save time failed (%s)", type(exc).__name__)
        steps: list[tuple[str, Callable[[], object]]] = [
            ("resolving the setup warning", lambda: self._health.resolve(SETUP_KEY)),
            ("rearming alerts", lambda: self._health.rearm(now)),
        ]
        if not was_configured:
            steps.append(
                ("recording the heartbeat slot", lambda: self._heartbeat.mark_done_if_past(now))
            )
        steps.append(
            ("releasing held events", lambda: self._store.release_held(now, reason="setup"))
        )
        # The replaced bot's errors are history now (Store.telegram_health's cutoff):
        # the `telegram` warning goes at once, not at the next maintenance tick.
        steps.append(("rechecking Telegram's health", self._check_telegram))
        for what, step in steps:
            try:
                step()
            except Exception:
                log.exception("after Telegram setup: %s failed", what)

    # -- web UI ----------------------------------------------------------------

    def _web_context(self, port: int, secret: bytes) -> WebContext:
        return WebContext(
            store=self._store,
            config_path=self._config_path,
            port=port,
            secret=secret,
            snapshot=self.status_snapshot,
            current=lambda: (
                self._config,
                list(self._projects.values()),
                list(self._missing.values()),
            ),
            send_test=self._send_test_notification,
            previewer=self._previewer,
            telegram_status=self._telegram_status,
            configured=lambda: self._sender.configured,
            complete_setup=self.complete_setup,
            make_probe=self._make_probe,
            save_credentials=save_credentials,
            setup_reason=self.setup_reason,
            note_unfinished_save=self.note_unfinished_save,
            clock=self._clock,
        )

    def _telegram_status(self) -> dict[str, Any]:
        """Settings → Telegram's view of the slot: no secret, no Bot API call."""
        return {"configured": self._sender.configured, "bot": self._sender.bot_username}

    @staticmethod
    def _make_probe(token: str, chat_id: str | None) -> TelegramClient:
        """A client for the wizard's draft token, separate from the slot's: the
        current bot keeps sending until the swap."""
        return TelegramClient(token, chat_id)

    async def _send_test_notification(self) -> None:
        await self._sender.send(sample_message(datetime.now()), parse_mode="HTML")

    def _web_out_of_sync(self) -> bool:
        """True if the web server doesn't match ``web.enabled`` / ``web.port``."""
        want, port = self._config.web.enabled, self._config.web.port
        if self._web.running:
            return not want or self._web.port != port
        return want

    async def _sync_web(self, *, restart: bool = False) -> None:
        """Make the web server match ``web.enabled`` / ``web.port``; with ``restart``
        stop it first (a `web_requests` row). Never raises."""
        async with self._web_lock:
            try:
                await self._sync_web_locked(restart=restart)
            except Exception:
                log.exception("web UI: start/stop failed")

    async def _sync_web_locked(self, *, restart: bool) -> None:
        want, port = self._config.web.enabled, self._config.web.port
        if self._web.running and (restart or not want or self._web.port != port):
            await self._web.stop()
            log.info("web UI stopped")
            self._publish_status()  # never advertise a stopped server's secret
        if not want:
            self._web_error = None
            self._health.forget("web")  # turned off: nothing is down any more
            return
        if self._web.running:
            return
        secret = secrets.token_bytes(32)
        # Set before start(): `running` turns true as soon as uvicorn has started, a
        # loop turn before start() returns, and the snapshot must then show this
        # server's secret, never the stopped one's.
        self._web_secret = secret
        try:
            task = await self._web.start(create_app(self._web_context(port, secret)), port)
        except WebStartError as exc:
            self._web_secret = None
            self._web_error = str(exc)
            self._publish_status()
            # Recorded, not sent: the maintenance tick sends it, so the web lock (and
            # with it the pump or maintenance) never waits on Telegram.
            self._health.open(
                "web", "serious", f"web UI could not start on 127.0.0.1:{port}: {exc}"
            )
            return
        self._web_error = None
        self._health.resolve("web")
        task.add_done_callback(self._on_web_done)
        # Publish the new secret now, not at the next maintenance tick: the CLI needs
        # it to verify /healthz before it sends a login link.
        self._publish_status()

    def _publish_status(self) -> None:
        """Rewrite status.json now (outside the maintenance tick): at start, and when
        the web server's state changed. Never raises."""
        if self._watcher is None:
            return
        try:
            self._write_status(self._watcher)
        except Exception:
            log.exception("writing status.json outside the maintenance tick failed")

    def _on_web_done(self, task: asyncio.Task[None]) -> None:
        """A web task ended. A requested stop is fine; anything else is a crash:
        log it, open the `web` problem, and record it for the status snapshot. Never
        fatal."""
        exc = None if task.cancelled() else task.exception()  # also marks it retrieved
        if not self._web.stop_requested(task):
            reason = f"{type(exc).__name__}: {exc}" if exc else "stopped unexpectedly"
            log.error("web UI stopped: %s", reason, exc_info=exc)
            self._web_error = reason
            try:
                self._health.open("web", "serious", f"web UI stopped: {reason}")
            except Exception:  # a done callback must never raise
                log.exception("recording the web UI problem failed")
        # A dead server's secret must not stay in status.json until the next tick.
        self._publish_status()

    async def _cancel_background(self) -> None:
        while pending := list(self._background):
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._background.difference_update(pending)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _web_block(self) -> dict[str, Any]:
        running = self._web.running
        return {
            "enabled": self._config.web.enabled,
            "running": running,
            "port": self._web.port if running else self._config.web.port,
            "last_error": self._web_error,
            # Hex; only in status.json (0600). The dashboard never passes it on.
            "secret": self._web_secret.hex() if running and self._web_secret else None,
        }

    def _snapshot(self, watcher: Watcher | None, *, running: bool) -> dict[str, Any]:
        rows = watcher.status() if watcher is not None else []
        snapshot = {
            "pid": os.getpid(),
            "running": running,
            "updated_at": self._clock(),
            "last_started_at": self._started_at,
            "connected": watcher.connected if watcher is not None else False,
            # From the connect-time `version` query: no extra Watchman call (None
            # while disconnected).
            "watchman_version": self._wm.version if self._wm is not None else None,
            "config_version": self._config_version,
            "config_rejected_version": self._config_rejected_version,
            "projects": rows
            + [
                {"name": m.name, "path": str(m.path), "status": "missing"}
                for m in self._missing.values()
            ],
            "counts": self._store.status_counts(),
            "web": self._web_block(),
            "telegram_configured": self._sender.configured,
        }
        if not running:  # the final snapshot: how this run ended (§5.1)
            if self._exit_failure is None:
                snapshot["clean_shutdown"] = True
            else:
                snapshot["last_failure"] = self._exit_failure
        return snapshot

    def status_snapshot(self) -> dict[str, Any]:
        """The live status: the same dict ``status.json`` holds (the web dashboard's
        single health source)."""
        return self._snapshot(self._watcher, running=True)

    def _write_status(self, watcher: Watcher, *, running: bool = True) -> None:
        """Atomically replace status.json. ``running`` is False only in the final
        snapshot written on shutdown, so `status` can tell a stale file apart."""
        liveness.write_status_file(self._state_dir, self._snapshot(watcher, running=running))


def _describe_failure(task: asyncio.Task[Any]) -> str:
    name = task.get_name()
    if task.cancelled():
        return f"background task {name} stopped: cancelled"
    exc = task.exception()
    if exc is None:
        return f"background task {name} stopped: returned unexpectedly"
    return f"background task {name} stopped: {type(exc).__name__}: {exc}"
