"""Noticing a newer Diffbell installed over the running daemon, and saying so (ADR 0022).

At start the daemon records what is installed (`Install.current`). Its maintenance
loop asks `SelfUpdate.check` at most every SELF_CHECK_EVERY: when the install on disk
differs from that start snapshot (Homebrew's `opt/diffbell` now names another Cellar
version, or the environment's installed version changed), the daemon stops normally
and launchd's KeepAlive starts the new version. Only the start snapshot is compared,
so a freshly started process never sees a change: no restart loop. Outside launchd
(its parent isn't pid 1: `diffbell run` by hand), or with no plist program to come back to, it only warns.

At the next start `queue_update_notice` compares the running version with the one
recorded last time and queues a silent Health notice ("updated: Y → X"), whatever
did the update (this restart, `diffbell upgrade`, a reinstall).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import plistlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from diffbell import BRAND, launchd
from diffbell.health import UPDATE_KEY, Health
from diffbell.store import Store

log = logging.getLogger(__name__)

SELF_CHECK_EVERY = 60.0
RUNNING_VERSION_KEY = "running_version"  # Store kv: the version that ran last


@dataclass(frozen=True, slots=True)
class Install:
    """What is installed on disk: the package version this environment would import,
    and, for a Homebrew install, the resolved `<prefix>/opt/diffbell`."""

    version: str | None
    brew: str | None

    @classmethod
    def current(cls) -> Install:
        return cls(installed_version(), brew_target(launchd.running_from_homebrew()))


def installed_version() -> str | None:
    importlib.invalidate_caches()  # metadata finders cache directory listings
    try:
        return importlib.metadata.version("diffbell")
    except Exception:
        return None


def brew_target(prefix: Path | None) -> str | None:
    if prefix is None:
        return None
    try:
        return str((prefix / "opt" / "diffbell").resolve(strict=True))
    except OSError:
        return None


def replaced(start: Install, now: Install) -> str | None:
    """The version now installed, when it isn't the one this process started with.
    A Homebrew install is judged by its `opt` link alone (its Cellar folder is named
    after the version); a link that went away (an uninstall) is no new version."""
    if start.brew is not None:
        if now.brew is not None and now.brew != start.brew:
            return Path(now.brew).name
        return None
    if start.version is not None and now.version is not None and now.version != start.version:
        return now.version
    return None


def under_launchd(parent: Callable[[], int] = os.getppid) -> bool:
    """launchd (pid 1) is the parent of every job it starts; a daemon run by hand has
    a shell for a parent. (XPC_SERVICE_NAME can't be checked instead: a process that
    claims a job name launchd didn't give it is aborted by libxpc.)"""
    return parent() == 1


def plist_program(plist: Path | None = None) -> Path | None:
    """The program launchd would start next, when the plist names one that exists."""
    try:
        with (plist or launchd.plist_path()).open("rb") as fh:
            program = Path(plistlib.load(fh)["ProgramArguments"][0])
    except Exception:
        return None
    return program if os.access(program, os.X_OK) else None


class SelfUpdate:
    def __init__(
        self,
        start: Install,
        running: str,
        *,
        current: Callable[[], Install] | None = None,
        managed: Callable[[], bool] | None = None,
        program: Callable[[], Path | None] | None = None,
    ) -> None:
        # The defaults are looked up at call time, so tests' belt (conftest) holds.
        self._start = start
        self._running = running
        self._current = current or (lambda: Install.current())
        self._managed = managed or (lambda: under_launchd())
        self._program = program or (lambda: plist_program())
        self._last: float | None = None
        self._warned = False

    @classmethod
    def at_start(cls, running: str) -> SelfUpdate:
        return cls(Install.current(), running)

    def check(self, now: float) -> str | None:
        """The new version when the daemon should stop now so launchd starts it, else
        None. Looks at the disk at most every SELF_CHECK_EVERY."""
        if self._last is not None and now - self._last < SELF_CHECK_EVERY:
            return None
        self._last = now
        new = replaced(self._start, self._current())
        if new is None:
            return None
        if not self._managed() or self._program() is None:
            if not self._warned:
                self._warned = True
                log.warning(
                    "%s %s is installed (running %s): restart it to run the new version",
                    BRAND,
                    new,
                    self._running,
                )
            return None
        log.info("%s %s is installed (running %s): restarting", BRAND, new, self._running)
        return new


def _numbers(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def update_text(previous: str, running: str) -> str:
    before, after = _numbers(previous), _numbers(running)
    if before is not None and after is not None and after > before:
        return f"⬆️ {BRAND} updated: {previous} → {running}"
    return f"{BRAND} changed version: {previous} → {running}"


def queue_update_notice(store: Store, health: Health, running: str) -> None:
    """At start: a notice when the version differs from the one that ran last (none on
    a first start), then record the running one."""
    previous = store.kv_get(RUNNING_VERSION_KEY)
    if previous and previous != running:
        health.notice(UPDATE_KEY, update_text(previous, running))
    store.kv_set(RUNNING_VERSION_KEY, running)
