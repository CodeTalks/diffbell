"""Per-project Watchman subscriptions, reconcile, and PDU handling (spec §4, §5)."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import pywatchman

from diffbell.config import ResolvedProject
from diffbell.fingerprint import FIELDS, baseline, observe
from diffbell.health import Health, Severity
from diffbell.models import Change
from diffbell.patterns import Expr, build_expression, expression_hash
from diffbell.store import ClockState, Store, UnknownProject
from diffbell.wmclient import WatchmanUnavailable

log = logging.getLogger(__name__)

SUB_PREFIX = "diffbell:"
CONFIG_SUB = "diffbell-config"
CONFIG_TARGET_SUB = "diffbell-config-target"  # a symlinked config's resolved file
# Watchman down (health spec §4.2): an alert only past 5 minutes, deliberately longer
# than a Watchman restart or upgrade (1-3 min).
WATCHMAN_GRACE = 300.0
WATCHMAN_DOWN = (
    "Watchman is unreachable; key files are not being watched. "
    "Check that Watchman is installed and running."
)
# A runnable command: the page's Copy button puts it on the clipboard as is.
WATCHMAN_FIX = "watchman version"
WATCHMAN_MISSING = (
    "Watchman isn't installed: no `watchman` on the PATH it searches; "
    "key files are not being watched."
)
WATCHMAN_MISSING_FIX = "brew install watchman"


# Watchman on the daemon's own PATH (the agent's is LAUNCHD_PATH; a foreground
# `diffbell run` has the shell's): picks the "isn't installed" alert. A seam for tests.
def find_binary() -> str | None:
    return shutil.which("watchman")


# A recrawl warning is resolved by the next fresh subscription, or after this long
# without a new one.
RECRAWL_CLEAR_AFTER = 24 * 3600.0
# While connected, projects in "error" (a canceled watch, a failed subscribe) and a
# canceled or failed config subscription are retried this often (tick()).
RETRY_ERRORS_EVERY = 60.0


class Caller(Protocol):
    async def call(self, *args: Any) -> Any: ...


class Mode(StrEnum):
    CATCHUP = "catchup"  # diff a fresh instance against stored hashes
    REBASELINE = "rebaseline"  # record a fresh instance silently


@dataclass
class ProjectState:
    project: ResolvedProject
    expr: Expr
    expr_hash: str
    watch_root: str = ""
    relative_root: str = ""
    mode: Mode = Mode.CATCHUP
    sub_name: str = ""  # diffbell:<project>#<generation>; unique per subscribe call
    status: str = "pending"
    error: str | None = None
    note: str | None = None
    # Store.project_token of the name when subscribed: every write for this
    # subscription's PDUs expects it, so a sync that moved the name to another folder
    # (a swap) makes the write raise UnknownProject and the PDU is dropped.
    token: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.project.name,
            "path": str(self.project.path),
            "status": self.status,
            "error": self.error,
            "note": self.note,
            "watch_root": self.watch_root,
            "relative_root": self.relative_root,
        }


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _project_dir(watch_root: str, relative_root: str) -> str:
    return os.path.normpath(os.path.join(watch_root, relative_root))


def recrawl_fix(watch_root: str) -> str:
    """The fix shown for a recrawl warning: built here, never copied from Watchman's
    text, with the root quoted so a space, ``;`` or ``$`` stays one argument. Shown
    and copied only, never run."""
    q = shlex.quote(watch_root)
    return f"watchman watch-del {q} ; watchman watch-project {q}"


class Watcher:
    def __init__(
        self,
        wm: Caller,
        store: Store,
        on_changes: Callable[[list[Change]], None],
        *,
        health: Health | None = None,
        config_path: Path | None = None,
        on_config_change: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
        # Reads a REBASELINE's snapshots (content snapshots spec §2.1), off the loop:
        # (project, new fingerprints) -> contents. None: the baseline stores none.
        baseline_reader: Callable[[ResolvedProject, Mapping[str, str]], Mapping[str, bytes]]
        | None = None,
    ) -> None:
        self._wm = wm
        self._baseline_reader = baseline_reader
        self._store = store
        self._on_changes = on_changes
        self._health = health
        self._config_path = config_path
        self._on_config_change = on_config_change
        self._clock = clock
        self._desired: dict[str, ResolvedProject] = {}
        self._states: dict[str, ProjectState] = {}
        # Keyed by full subscription name: a late PDU from a replaced subscription
        # (same project, old expression) must never reach the new state.
        self._by_sub: dict[str, ProjectState] = {}
        self._generation = 0
        self._connected = False
        self._ever_connected = False
        # Project name -> when Watchman last warned about a recrawl.
        self._recrawls: dict[str, float] = {}
        # Subscribed projects whose folder was missing at the last tick (a lost watch
        # needs two ticks in a row: a folder moved away and back is not one).
        self._vanished: set[str] = set()
        # Names whose PDU was dropped as stale since the last reconcile: the first
        # drop is a warning (a reconcile that failed after a sync is not silent).
        self._stale_warned: set[str] = set()
        self._last_retry = clock()
        # True while the config subscription is known to be gone (canceled PDU, failed
        # watch/subscribe): the retry pass in tick() re-runs _subscribe_config().
        self._config_unwatched = False
        # Config subscription name -> its watch root, and the files last subscribed
        # (see _config_files): a retargeted symlink moves the target subscription.
        self._config_roots: dict[str, str] = {}
        self._config_files_seen: dict[str, Path] = {}
        # Serializes subscription changes: reconcile() (config reload) and handle()
        # (reconnect, error re-subscribe) may otherwise interleave across awaits.
        self._lock = asyncio.Lock()
        self._watchman_down()  # not connected yet

    # -- problems (health spec §4.2): recorded, never sent from here ---------

    def _open(
        self,
        key: str,
        severity: Severity,
        message: str,
        *,
        project: str | None = None,
        fix: str | None = None,
        grace: float = 0.0,
    ) -> None:
        if self._health is None:
            log.warning("%s", message)
            return
        self._health.open(key, severity, message, project=project, fix=fix, grace=grace)

    def _resolve(self, key: str) -> None:
        if self._health is not None:
            self._health.resolve(key)

    def _watchman_down(self) -> None:
        missing = find_binary() is None
        message, fix = (
            (WATCHMAN_MISSING, WATCHMAN_MISSING_FIX) if missing else (WATCHMAN_DOWN, WATCHMAN_FIX)
        )
        self._open("watchman", "serious", message, fix=fix, grace=WATCHMAN_GRACE)

    def _project_problem(self, project: ResolvedProject, message: str) -> None:
        name = project.name
        self._open(f"project:{name}", "serious", f"project {name}: {message}", project=name)

    @property
    def connected(self) -> bool:
        return self._connected

    def status(self) -> list[dict[str, Any]]:
        rows = [st.as_dict() for st in self._states.values()]
        rows += [
            {"name": name, "path": str(p.path), "status": "pending"}
            for name, p in self._desired.items()
            if name not in self._states
        ]
        return rows

    # -- desired state -------------------------------------------------------

    async def reconcile(self, projects: list[ResolvedProject]) -> None:
        self._desired = {p.name: p for p in projects}
        self._stale_warned.clear()
        if self._connected:
            async with self._lock:
                await self._apply()

    async def _apply(self) -> None:
        for name, st in list(self._states.items()):
            if self._desired.get(name) != st.project or self._token_is_stale(st):
                await self._unsubscribe(st)
                del self._states[name]
        for name, project in self._desired.items():
            if name not in self._states:
                await self._subscribe_guarded(project)

    def _token_is_stale(self, st: ProjectState) -> bool:
        """The sync gave the name another folder id while its ResolvedProject stayed
        equal (a symlinked project path retargeted): the subscription's token would
        drop every PDU, and a subscribe the sync left pending would never be redone.
        A name the Store doesn't map is left to the reconcile that removes it."""
        try:
            current = self._store.project_token(st.project.name)
        except UnknownProject:
            return False
        if st.token is None:
            return st.status == "pending"
        return current != st.token

    async def _subscribe_guarded(self, project: ResolvedProject) -> None:
        """_subscribe, but an unexpected exception only breaks this one project."""
        try:
            await self._subscribe(project)
        except Exception as exc:
            log.exception("unexpected error subscribing %s", project.name)
            st = self._states.get(project.name)
            if st is None or st.project is not project:
                st = ProjectState(project, [], "")
                self._states[project.name] = st
            st.status, st.error = "error", f"{type(exc).__name__}: {exc}"
            self._project_problem(project, f"cannot watch: {st.error}")

    async def _subscribe(self, project: ResolvedProject) -> None:
        expr = build_expression(project)
        st = ProjectState(project, expr, expression_hash(expr))
        self._generation += 1
        st.sub_name = f"{SUB_PREFIX}{project.name}#{self._generation}"
        self._states[project.name] = st
        self._by_sub[st.sub_name] = st
        try:
            # The folder this name names now. Taken before the first await and
            # checked again after it: a sync meanwhile (a rename, removal or swap)
            # makes this subscription stale, and the reconcile that follows the sync
            # redoes it. Until then it stays pending: nothing is read or written for
            # the name, which may now name another folder.
            if self._desired.get(project.name) != project:
                raise UnknownProject(project.name)
            st.token = self._store.project_token(project.name)
            res = await self._wm.call("watch-project", str(project.path))
            st.watch_root = str(res["watch"])
            st.relative_root = str(res.get("relative_path") or "")
            opts: dict[str, Any] = {"expression": expr, "fields": FIELDS, "defer_vcs": True}
            if st.relative_root:
                opts["relative_root"] = st.relative_root
            if (
                self._desired.get(project.name) != project
                or self._store.project_token(project.name) != st.token
            ):
                raise UnknownProject(project.name)
            prev = self._store.get_clock(project.name)
            if prev is None:
                st.mode, st.note = Mode.REBASELINE, "first run: baseline recorded"
            elif prev.expr_hash != st.expr_hash:
                st.mode, st.note = Mode.REBASELINE, "expression changed: re-baselined"
            elif _project_dir(prev.watch_root, prev.relative_root) != _project_dir(
                st.watch_root, st.relative_root
            ):
                # Same name, different directory: the stored hashes describe another
                # tree, so diffing against them would report spurious changes.
                st.mode, st.note = Mode.REBASELINE, "project path changed: re-baselined"
            elif (prev.watch_root, prev.relative_root) == (st.watch_root, st.relative_root):
                opts["since"] = prev.clock
                st.mode = Mode.CATCHUP
            else:
                # Same directory under a different watch root (an ancestor started or
                # stopped being watched): the old clock is useless, the hashes are not.
                st.mode = Mode.CATCHUP
                st.note = "watch root changed: caught up from stored hashes"
            await self._wm.call("subscribe", st.watch_root, st.sub_name, opts)
            st.status, st.error = "subscribed", None
            # A fresh subscription: the watch is back, and a recrawl is behind it.
            self._resolve(f"project:{project.name}")
            self._resolve(f"recrawl:{project.name}")
            self._recrawls.pop(project.name, None)
            self._vanished.discard(project.name)
        except UnknownProject:
            # Stale, not a failure: no problem, the coming reconcile subscribes it.
            log.debug("subscribe %s skipped: the config changed meanwhile", project.name)
            self._by_sub.pop(st.sub_name, None)
            st.status, st.error, st.token = "pending", None, None
        except WatchmanUnavailable:
            st.status = "disconnected"
        except (pywatchman.CommandError, KeyError) as exc:
            st.status, st.error = "error", str(exc)
            self._project_problem(project, f"cannot watch {project.path}: {exc}")

    async def _unsubscribe(self, st: ProjectState) -> None:
        self._by_sub.pop(st.sub_name, None)
        if not st.watch_root:
            return
        try:
            # Idempotent: an already-gone subscription returns {"deleted": false}.
            await self._wm.call("unsubscribe", st.watch_root, st.sub_name)
        except (pywatchman.CommandError, WatchmanUnavailable) as exc:
            log.debug("unsubscribe %s: %s", st.project.name, exc)

    def _config_files(self) -> dict[str, Path]:
        """Subscription name -> config file to watch. A symlinked config (dotfiles)
        is watched twice: at the link (the user may retarget it) and at its resolved
        target, the file a web save rewrites, possibly in another directory."""
        if self._config_path is None:
            return {}
        link = self._config_path
        files = {CONFIG_SUB: link}
        target = Path(os.path.realpath(link))
        if target != Path(os.path.realpath(link.parent)) / link.name:
            files[CONFIG_TARGET_SUB] = target
        return files

    async def _subscribe_config(self) -> None:
        files = self._config_files()
        old, roots = self._config_roots, {}
        failed = False
        for sub, path in files.items():
            ok, root = await self._subscribe_config_file(sub, path)
            failed |= not ok
            if root is not None:
                roots[sub] = root
            elif not ok and sub in old:
                roots[sub] = old[sub]  # still ours; the retry replaces it
        # A retargeted (or no longer symlinked) config: drop the subscription left on
        # the old target's root, or its PDUs would keep reloading for a foreign file.
        for sub, root in old.items():
            if roots.get(sub) != root:
                try:
                    await self._wm.call("unsubscribe", root, sub)
                except (pywatchman.CommandError, WatchmanUnavailable) as exc:
                    log.debug("unsubscribe %s: %s", sub, exc)
        self._config_roots = roots
        self._config_files_seen = files
        self._config_unwatched = failed
        if not failed:
            self._resolve("config-watch")

    async def _subscribe_config_file(self, sub: str, path: Path) -> tuple[bool, str | None]:
        """Subscribe ``sub`` to ``path`` in its directory. Returns (ok, watch root):
        ok is False when the watch failed and needs a retry; a missing directory only
        warns (ok, no root)."""
        config_dir = path.parent
        if not config_dir.is_dir():
            log.warning(
                "config directory %s does not exist; config edits will not be picked up",
                config_dir,
            )
            return True, None
        try:
            # `watch`, not `watch-project`: watch-project would widen the root to an
            # ancestor holding a .git (~/.config/.git, a dotfiles ~/.git), i.e.
            # possibly the whole home directory. The root is the config dir itself.
            res = await self._wm.call("watch", str(config_dir))
            opts: dict[str, Any] = {
                # wholename: the config file at the top level of the root only
                "expression": ["name", path.name, "wholename"],
                "fields": ["name", "exists"],
            }
            if res.get("relative_path"):
                opts["relative_root"] = str(res["relative_path"])
            root = str(res["watch"])
            # Config roots are deliberately never `watch-del`'d (only unsubscribed
            # on a retarget): the same root may also be a project's root or shared
            # with another tool, and Watchman roots aren't reference-counted. The
            # rare leak (a dotfiles symlink retargeted to a new directory leaves the
            # old root watched until Watchman restarts) is accepted.
            await self._wm.call("subscribe", root, sub, opts)
        except (pywatchman.CommandError, WatchmanUnavailable, KeyError) as exc:
            log.error("cannot watch the config directory %s: %s", config_dir, exc)
            if not isinstance(exc, WatchmanUnavailable):  # a reconnect re-subscribes anyway
                self._open("config-watch", "serious", f"config edits will not be picked up: {exc}")
            return False, None
        return True, root

    def _config_retargeted(self) -> bool:
        return self._config_files() != self._config_files_seen

    # -- messages from the connection thread ---------------------------------

    async def handle(self, msg: dict[str, Any]) -> None:
        marker = msg.get("_diffbell")
        if marker == "connected":
            await self._on_connected()
            return
        if marker == "disconnected":
            self._connected = False
            self._watchman_down()
            for state in self._states.values():
                state.status = "disconnected"
            return
        if marker == "error":
            await self._on_error(msg)
            return

        name = str(msg.get("subscription", ""))
        if name in (CONFIG_SUB, CONFIG_TARGET_SUB):
            if msg.get("canceled"):
                self._config_unwatched = True
                log.warning("config watch canceled by Watchman; retrying periodically")
                return
            if msg.get("is_fresh_instance"):
                return
            if self._config_retargeted():
                async with self._lock:
                    if self._connected:
                        await self._subscribe_config()
            if self._on_config_change is not None:
                await self._on_config_change()
            return
        st = self._by_sub.get(name)
        if st is None:
            return  # late PDU from a subscription we already replaced or removed
        if msg.get("canceled"):
            st.status, st.error = "error", "watch canceled by Watchman (folder removed?)"
            self._project_problem(st.project, f"watch lost: {st.error}")
            return
        if "warning" in msg:
            log.warning("watchman warning for %s: %s", st.project.name, msg["warning"])
            self._on_warning(st, str(msg["warning"]))
        await self._apply_pdu(st, msg)

    def _on_warning(self, st: ProjectState, warning: str) -> None:
        """A recrawl warning becomes a page-only problem with a fix built here."""
        if "recrawl" not in warning.lower():
            return
        name = st.project.name
        self._recrawls[name] = self._clock()
        self._open(
            f"recrawl:{name}",
            "warning",
            f"project {name}: Watchman had to recrawl the folder (events may have been missed)",
            project=name,
            fix=recrawl_fix(st.watch_root),
        )

    async def _on_error(self, msg: dict[str, Any]) -> None:
        """A unilateral error PDU (or a bare error message) from the connection thread.

        An error naming one of our live subscriptions means that subscription is
        broken: mark the project, record the problem, and re-subscribe it under a new
        generation
        (the stored clock still matches, so it resumes with ``since``).
        """
        pdu = msg.get("pdu")
        if not isinstance(pdu, dict):
            log.warning("watchman error: %s", msg.get("message"))
            return
        text = str(pdu.get("error", "unknown error"))
        st = self._by_sub.get(str(pdu.get("subscription", "")))
        if st is None or self._states.get(st.project.name) is not st:
            log.warning("watchman error (no live subscription): %s", text)
            return
        name = st.project.name
        st.status, st.error = "error", text
        self._project_problem(st.project, f"Watchman error: {text}")
        async with self._lock:
            if self._states.get(name) is st:  # else replaced meanwhile (reconcile/reconnect)
                await self._unsubscribe(st)
                await self._subscribe_guarded(st.project)
                new = self._states[name]
                if new.status == "subscribed":
                    new.note = f"re-subscribed after Watchman error: {text}"

    async def _read_baseline(
        self, project: ResolvedProject, hashes: Mapping[str, str]
    ) -> Mapping[str, bytes] | None:
        """The baseline's snapshots, read off the loop before replace_baseline's
        transaction; None without a reader. A failure is logged by type only: the
        baseline is still recorded, without snapshots."""
        if self._baseline_reader is None:
            return None
        try:
            return await asyncio.to_thread(self._baseline_reader, project, hashes)
        except Exception as exc:
            log.warning(
                "could not read the baseline contents of %s (%s)", project.name, type(exc).__name__
            )
            return {}

    async def _apply_pdu(self, st: ProjectState, msg: dict[str, Any]) -> None:
        if "clock" not in msg:
            return
        name = st.project.name
        now = self._clock()
        files: list[dict[str, Any]] = list(msg.get("files") or [])
        fresh = bool(msg.get("is_fresh_instance"))
        state = ClockState(st.watch_root, st.relative_root, str(msg["clock"]), st.expr_hash)
        if st.token is None:
            # Every subscribed state has one (_subscribe takes it before subscribing):
            # writing without it could land on another folder after a swap.
            raise RuntimeError(f"no project token for the subscription of {name!r}")
        try:
            # Checked before reading the hashes: never read another folder's.
            if self._store.project_token(name) != st.token:
                raise UnknownProject(name)
            stored = self._store.get_hashes(name)
            # Only an explicit REBASELINE records silently. An empty stored baseline is
            # a real baseline ("no matching files"): a fresh instance is diffed against it.
            if fresh and st.mode is Mode.REBASELINE:
                hashes = baseline(files)
                # The one await on this path. The flush is the only writer that
                # advances snapshots from captured afters: the baseline stores the
                # read only for paths with no pending journal row, and only if no
                # snapshot write happened during the read (the epoch, taken before
                # it). The write still expects the token, so a sync during the read
                # drops the PDU, and so does a subscription replaced meanwhile.
                epoch = self._store.snapshot_epoch(name)
                contents = await self._read_baseline(st.project, hashes)
                if self._by_sub.get(st.sub_name) is not st:
                    log.debug("dropped a baseline PDU for %r: its subscription was replaced", name)
                    return
                self._store.replace_baseline(
                    name,
                    state,
                    hashes,
                    now,
                    expect=st.token,
                    snapshots=contents,
                    snapshots_epoch=epoch,
                )
                st.mode = Mode.CATCHUP
                return
            changes = self._store.record_observation(
                name, state, observe(stored, files, fresh=fresh), now, expect=st.token
            )
        except UnknownProject:
            # The config was synced (a rename, removal or swap: the name is gone or
            # names another folder than the token) and the reconcile that
            # re-subscribes is still to come. Nothing was written: the clock stored
            # under the project's id is unchanged, so the new subscription resumes
            # from it and Watchman replays these changes (stable project ids, 2b A).
            level = logging.DEBUG if name in self._stale_warned else logging.WARNING
            self._stale_warned.add(name)
            log.log(level, "dropped a PDU for %r: the name no longer names its folder", name)
            return
        st.mode = Mode.CATCHUP
        if changes:
            self._on_changes(changes)

    async def _on_connected(self) -> None:
        reconnect, self._ever_connected = self._ever_connected, True
        self._resolve("watchman")
        self._last_retry = self._clock()
        async with self._lock:
            self._connected = True
            self._states.clear()  # subscriptions died with the previous connection
            self._by_sub.clear()
            self._config_roots = {}  # died with the connection too
            await self._subscribe_config()
            await self._apply()
        if reconnect and self._on_config_change is not None:
            # The config subscription's fresh instance is ignored, so edits made while
            # Watchman was down would be lost. Called outside the lock: it reconciles,
            # and asyncio.Lock is not re-entrant.
            await self._on_config_change()

    async def tick(self) -> None:
        """Called periodically by the daemon: notices a watched folder that is gone,
        clears recrawl warnings that are a day old, and while connected retries failed
        watches every RETRY_ERRORS_EVERY seconds."""
        await self._check_folders()
        self._expire_recrawls()
        if self._connected and self._clock() - self._last_retry >= RETRY_ERRORS_EVERY:
            self._last_retry = self._clock()
            await self._retry_failed()

    async def _check_folders(self) -> None:
        """A subscribed project whose folder is gone at two ticks in a row has lost
        its watch, whether or not Watchman cancels it (it does not when the folder is
        below a watch root). It is in "error" from here on, so _retry_failed
        re-subscribes it once the folder is back. The stats run off the loop."""
        subscribed = [st for st in self._states.values() if st.status == "subscribed"]
        paths = [st.project.path for st in subscribed]
        present = await asyncio.to_thread(lambda: [_is_dir(path) for path in paths])
        for st, there in zip(subscribed, present, strict=True):
            name = st.project.name
            if there:
                self._vanished.discard(name)
            elif name not in self._vanished:
                self._vanished.add(name)  # once could be a move away and back
            elif self._states.get(name) is st and st.status == "subscribed":
                self._vanished.discard(name)
                st.status, st.error = "error", "project folder removed"
                self._project_problem(st.project, f"watch lost: {st.error}")

    def _expire_recrawls(self) -> None:
        now = self._clock()
        for name, seen in list(self._recrawls.items()):
            if now - seen >= RECRAWL_CLEAR_AFTER:
                del self._recrawls[name]
                self._resolve(f"recrawl:{name}")

    async def _retry_failed(self) -> None:
        """Re-subscribe projects in "error" whose directory exists (a deleted and
        re-created project dir, a watch canceled by another tool) and a config
        subscription that is gone. Nothing else would: reconcile skips unchanged
        projects and only a reconnect re-subscribes everything."""
        config_back = False
        async with self._lock:
            if not self._connected:
                return
            for name, st in list(self._states.items()):
                if st.status != "error" or not _is_dir(st.project.path):
                    continue
                await self._unsubscribe(st)
                await self._subscribe_guarded(st.project)
                new = self._states[name]
                if new.status == "subscribed":
                    new.note = "watching again after error"
            if self._config_unwatched:
                await self._subscribe_config()
                config_back = not self._config_unwatched
        if config_back and self._on_config_change is not None:
            # Edits made while the config was unwatched would otherwise be lost.
            # Outside the lock: it reconciles, and asyncio.Lock is not re-entrant.
            await self._on_config_change()
