"""Liveness (health spec §5): tell a dead daemon from a quiet week.

- **status.json** says how the last run ended: a normal stop writes
  ``"clean_shutdown": true``; a start that fails before the daemon runs writes
  ``"running": true`` with ``"last_failure"`` and ``"failed_start"``
  (``write_failure_status``, which needs no watcher); a background task that dies,
  or an exception after the start, keeps the daemon's own snapshot with
  ``"last_failure"`` (an unexpected stop); a crash leaves ``"running": true`` behind. At start the daemon reads
  the previous file before writing its own (``restart_message``), and the message
  goes out through Health as a one-shot notice.
- **The heartbeat**: once a day after ``notify.heartbeat`` (local time), a silent
  "✅ Diffbell · watching N projects · M changes yesterday · all OK".
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from diffbell import BRAND
from diffbell.delivery import backoff, classify
from diffbell.health import NOTICE, Health, Problem, in_grace
from diffbell.store import Store
from diffbell.telegram import Sender, TelegramError

log = logging.getLogger(__name__)

STATUS_FILE = "status.json"
# The heartbeat slot last sent: "YYYY-MM-DD HH:MM" (the local date and the configured
# time). Slots sort as text: a time moved later than the one today's went out at sends
# one more today, one at or before it does not, and a restart never does. Deleted
# while the heartbeat is off.
HEARTBEAT_KEY = "last_heartbeat"
# The restart notice: its Health key, and the kv key holding when the last one was
# due (epoch seconds). A crash loop sends at most one per RESTART_NOTICE_EVERY.
RESTART_KEY = "restart"
LAST_RESTART_KEY = "last_restart_notice"
RESTART_NOTICE_EVERY = 600.0


# -- status.json -------------------------------------------------------------------


def read_status(state_dir: Path) -> dict[str, Any] | None:
    """The previous run's status.json, or None (absent, unreadable, not an object)."""
    try:
        data = json.loads((state_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_status_file(state_dir: Path, snapshot: dict[str, Any]) -> None:
    """Atomically replace status.json, owner-only (0600) from the first byte: it
    holds the web secret."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = state_dir / f"{STATUS_FILE}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # O_CREAT's mode does not apply to an existing file: a leftover tmp from an
        # older version may be wider. Narrow it before anything is written.
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, indent=2))
    os.replace(tmp, state_dir / STATUS_FILE)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# -- readiness after a restart (`install`, `upgrade`) --------------------------------

READY_WAIT_SECONDS = 30.0
READY_POLL_SECONDS = 0.5


def started_after(status: dict[str, Any] | None, started: float) -> bool:
    """status.json records a daemon start later than ``started``."""
    last = _number((status or {}).get("last_started_at"))
    return last is not None and last > started


def _failure_suffix(status: dict[str, Any]) -> str:
    failure = status.get("last_failure")
    return f" (last failure: {failure})" if isinstance(failure, str) and failure else ""


def not_ready(status: dict[str, Any] | None, started: float) -> list[str]:
    """What keeps ``status`` from describing a daemon, started after ``started``, that
    is up: running, connected to Watchman, every project's subscription tried (none
    "pending"), and the web UI listening if it is enabled. [] when it is ready."""
    if status is None or not started_after(status, started):
        return ["no new start reported" + _failure_suffix(status or {})]
    if status.get("running") is not True:
        return ["the daemon stopped after starting" + _failure_suffix(status)]
    missing: list[str] = []
    if status.get("connected") is not True:
        missing.append("not connected to Watchman (is it running?)")
    projects = status.get("projects")
    rows = projects if isinstance(projects, list) else []
    pending = [
        str(row.get("name"))
        for row in rows
        if isinstance(row, dict) and row.get("status") == "pending"
    ]
    if pending:
        missing.append("subscriptions not done yet: " + ", ".join(pending))
    web = status.get("web")
    if isinstance(web, dict) and web.get("enabled") is True and web.get("running") is not True:
        error = web.get("last_error")
        detail = f" ({error})" if isinstance(error, str) and error else ""
        missing.append(f"web UI not running on port {web.get('port')}{detail}")
    return missing


@dataclass(frozen=True, slots=True)
class Readiness:
    snapshot: dict[str, Any] | None  # the last status.json read (None: none)
    missing: list[str]  # `not_ready` of it; empty when ready

    @property
    def ready(self) -> bool:
        return not self.missing


def wait_until_ready(
    state_dir: Path,
    started: float,
    *,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    timeout: float = READY_WAIT_SECONDS,
) -> Readiness:
    """Poll status.json until the daemon started after ``started`` is ready
    (`not_ready`), or ``timeout`` seconds pass. Returns the last snapshot read and
    what was still missing then."""
    deadline = monotonic() + timeout
    while True:
        status = read_status(state_dir)
        missing = not_ready(status, started)
        if not missing or monotonic() >= deadline:
            return Readiness(status, missing)
        sleep(READY_POLL_SECONDS)


def _down(previous: dict[str, Any]) -> bool:
    """The previous run did not end normally: a crash (still "running"), a failed
    start, or a daemon whose background task died (``last_failure``)."""
    if previous.get("clean_shutdown") is True:
        return False
    return previous.get("running") is True or bool(previous.get("last_failure"))


# When this process's daemon started (its first snapshot), or None: set by
# `Daemon.run`, cleared by `cli.cmd_run` before each run, read when a run fails.
_started_at: float | None = None


def mark_started(at: float) -> None:
    """The daemon of this process has started (its first status.json is written)."""
    global _started_at
    _started_at = at


def clear_started() -> None:
    global _started_at
    _started_at = None


def started_at() -> float | None:
    """When this process's daemon started, or None (it never did)."""
    return _started_at


def write_failure_status(
    state_dir: Path, reason: str, now: float, *, started_at: float | None = None
) -> None:
    """Record why ``run`` failed, without a watcher.

    ``started_at`` None (the daemon never started: a failed migration, an invalid
    config, the Keychain): ``running`` true, no ``clean_shutdown``, and
    ``last_failure``, marked ``failed_start``, so the next start says "could not
    start earlier". launchd's restart loop rewrites it every attempt: the first
    down-from time (``updated_at``) is kept, the last reason wins, and
    ``last_started_at`` is carried over (absent: no start ever succeeded).

    ``started_at`` set (this process's daemon started, then a background task died
    or something raised): an unexpected stop, never ``failed_start``. The daemon's
    own snapshot (this pid, this ``last_started_at``) is kept with its ``updated_at``
    (the down-from time); ``clean_shutdown`` is dropped and ``last_failure`` added if
    it has none. Without that snapshot, a minimal one says the same."""
    previous = read_status(state_dir)
    snapshot: dict[str, Any]
    if started_at is not None:
        own = (
            previous is not None
            and previous.get("pid") == os.getpid()
            and _number(previous.get("last_started_at")) == started_at
            and previous.get("failed_start") is not True
        )
        if own and previous is not None:
            snapshot = dict(previous)
            snapshot.pop("clean_shutdown", None)
            if not snapshot.get("last_failure"):
                snapshot["last_failure"] = reason
        else:
            snapshot = {
                "pid": os.getpid(),
                "running": True,
                "updated_at": now,
                "last_started_at": started_at,
                "last_failure": reason,
            }
        write_status_file(state_dir, snapshot)
        return
    since = now
    if previous is not None and _down(previous):
        since = _number(previous.get("updated_at")) or now
    snapshot = {
        "pid": os.getpid(),
        "running": True,
        "updated_at": since,
        "last_failure": reason,
        "failed_start": True,
    }
    started = previous.get("last_started_at") if previous is not None else None
    if _number(started) is not None:
        snapshot["last_started_at"] = started
    write_status_file(state_dir, snapshot)


def _hhmm(ts: float, tz: tzinfo | None) -> str:
    return f"{datetime.fromtimestamp(ts, tz):%H:%M}"


def _down_from(previous: dict[str, Any], now: float) -> float:
    return _number(previous.get("updated_at")) or now


def restart_message(
    previous: dict[str, Any] | None,
    now: float,
    tz: tzinfo | None = None,
    *,
    since: float | None = None,
) -> str | None:
    """The message a successful start sends about the previous run, or None (no
    status.json, or a normal stop). The down-from time is ``since`` if given (an
    earlier notice still unsent), else the previous file's ``updated_at``; a failure
    reason is included. A file written by `write_failure_status` (``failed_start``)
    reads "could not start earlier", whatever ``last_started_at`` says."""
    if previous is None or not _down(previous):
        return None
    if since is None:
        since = _down_from(previous, now)
    span = f"{_hhmm(since, tz)}–{_hhmm(now, tz)}"
    failure = previous.get("last_failure")
    reason = f": {failure}" if isinstance(failure, str) and failure else ""
    failed_start = previous.get("failed_start") is True
    # A file from before the flag: a failure with no successful start ever.
    if failed_start or (reason and _number(previous.get("last_started_at")) is None):
        return f"⚠️ {BRAND} could not start earlier ({span}){reason}"
    return f"⚠️ {BRAND} restarted after an unexpected stop (down {span}){reason}"


def queue_restart_notice(
    store: Store,
    health: Health,
    previous: dict[str, Any] | None,
    now: float,
    tz: tzinfo | None = None,
) -> None:
    """Queue the restart message (`restart_message`) as a Health notice, at most one
    per RESTART_NOTICE_EVERY: a crash loop must not flood the chat. A notice still
    unsent is replaced, keeping its down-from time (its ``opened_at``) and its due
    time, with the latest reason. Otherwise one due within RESTART_NOTICE_EVERY of
    the last waits until then (``next_try_at``), so it is held, never dropped: it
    goes out once due, unless a later start replaces it first."""
    pending = store.get_problem(RESTART_KEY)
    if pending is not None and pending.severity != NOTICE:
        pending = None
    since = pending.opened_at if pending is not None else None
    text = restart_message(previous, now, tz, since=since)
    if text is None:
        return
    if pending is not None:
        due = pending.next_try_at
    else:
        try:
            last: float | None = float(store.kv_get(LAST_RESTART_KEY) or "")
        except ValueError:
            last = None
        held = last is not None and now - last < RESTART_NOTICE_EVERY
        due = last + RESTART_NOTICE_EVERY if held and last is not None else now
        store.kv_set(LAST_RESTART_KEY, repr(due))
    if since is None and previous is not None:
        since = _down_from(previous, now)
    health.notice(RESTART_KEY, text, since=since, not_before=due)


# -- the daily heartbeat ------------------------------------------------------------


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def heartbeat_text(projects: int, changes: int, problems: list[Problem]) -> str:
    head = (
        f"✅ {BRAND} · watching {_plural(projects, 'project')} · "
        f"{_plural(changes, 'change')} yesterday"
    )
    serious = [p for p in problems if p.severity == "serious"]
    warnings = [p for p in problems if p.severity == "warning"]
    if serious:
        return f"{head} · ⚠️ {_plural(len(serious), 'problem')}: {serious[0].message}"
    if warnings:
        return f"{head} · {_plural(len(warnings), 'warning')}: {warnings[0].message}"
    return f"{head} · all OK"


class Heartbeat:
    """Checked on every maintenance tick; sends at most once per local day (and
    configured time). Quiet hours and snooze don't apply. A failed send is retried on
    the shared backoff; a day that ends without a successful send is skipped."""

    def __init__(
        self,
        store: Store,
        sender: Sender,
        health: Health,
        *,
        at: Callable[[], str | None],  # notify.heartbeat: "HH:MM", or None for off
        projects: Callable[[], int],  # how many projects are watched
        tz: tzinfo | None = None,  # None: the Mac's local time
        # Whether Telegram is set up; while it is not, no heartbeat is sent and the
        # kv slot is left alone (bot onboarding spec §1).
        configured: Callable[[], bool] = lambda: True,
    ) -> None:
        self._store = store
        self._sender = sender
        self._health = health
        self._at = at
        self._projects = projects
        self._tz = tz
        self._configured = configured
        self._attempts = 0
        self._next_try = 0.0
        # A slot sent whose kv write failed: only the write is retried, never the send.
        self._unrecorded: str | None = None

    def _midnight(self, day: date) -> float:
        """The epoch seconds of a local midnight (DST-aware, also with tz=None)."""
        return datetime.combine(day, clock_time(), tzinfo=self._tz).timestamp()

    def changes_yesterday(self, now: float) -> int:
        """Events created during the previous local calendar day."""
        today = datetime.fromtimestamp(now, self._tz).date()
        start = self._midnight(today - timedelta(days=1))
        return self._store.count_events(start, self._midnight(today))

    def text(self, now: float) -> str:
        """The heartbeat. A serious problem still in its grace period is left out:
        its ⚠️ is not due yet (Watchman still starting is not trouble)."""
        problems = [p for p in self._health.problems() if not in_grace(p, now)]
        return heartbeat_text(self._projects(), self.changes_yesterday(now), problems)

    async def tick(self, now: float) -> None:
        """Send today's heartbeat if it is due. Never raises. Nothing at all (no send,
        no kv write) while Telegram is not set up."""
        if not self._configured():
            return
        at = self._at()
        if at is None:
            # Off: forget the last slot, so turning it back on waits for the next one.
            if self._store.kv_get(HEARTBEAT_KEY) is not None:
                self._store.kv_delete(HEARTBEAT_KEY)
            return
        local = datetime.fromtimestamp(now, self._tz)
        past = f"{local:%H:%M}" >= at  # zero-padded 24-hour times compare as text
        slot = f"{local:%Y-%m-%d} {at}"
        last = self._store.kv_get(HEARTBEAT_KEY)
        if last is None:
            # First run (an upgrade, or the heartbeat just turned on): the first
            # heartbeat is the next HH:MM, not one right now.
            first = slot if past else f"{local.date() - timedelta(days=1)} {at}"
            self._store.kv_set(HEARTBEAT_KEY, first)
            return
        if not past or last >= slot or now < self._next_try:
            return
        if self._unrecorded is not None and self._unrecorded >= slot:
            self._record(self._unrecorded)
            return
        try:
            await self._sender.send(self.text(now), parse_mode=None, silent=True)
        except Exception as exc:  # Telegram, or the store: retried, never raised
            if isinstance(exc, TelegramError):
                delay = classify(exc, self._attempts).delay  # a 429's retry_after
            else:
                delay = backoff(self._attempts)
            self._attempts += 1
            self._next_try = now + delay
            log.warning("heartbeat not sent (retry in %.0fs): %s", delay, exc)
            return
        self._attempts, self._next_try = 0, 0.0
        log.info("heartbeat sent")
        self._record(slot)

    def mark_done_if_past(self, now: float) -> None:
        """Telegram was just set up: if today's heartbeat time has passed, record
        today's slot as done, so the first heartbeat is the next scheduled one, not
        one right after the test message. Before today's time (or with the heartbeat
        off) nothing changes; a later slot already stored is never moved back.
        Best effort: never raises (the caller is the save handler)."""
        try:
            at = self._at()
            if at is None:
                return
            local = datetime.fromtimestamp(now, self._tz)
            if f"{local:%H:%M}" < at:
                return
            slot = f"{local:%Y-%m-%d} {at}"
            last = self._store.kv_get(HEARTBEAT_KEY)
            if last is None or last < slot:
                self._store.kv_set(HEARTBEAT_KEY, slot)
        except Exception as exc:
            log.warning("heartbeat slot not marked at setup (%s)", type(exc).__name__)

    def _record(self, slot: str) -> None:
        """Store ``slot`` as sent. A failed write (e.g. "database is locked") is
        logged once and retried on the next ticks, never followed by another send."""
        try:
            self._store.kv_set(HEARTBEAT_KEY, slot)
        except Exception as exc:
            if self._unrecorded != slot:
                log.warning(
                    "heartbeat sent but not recorded (%s); retrying the write, never the send",
                    type(exc).__name__,
                )
            self._unrecorded = slot
            return
        self._unrecorded = None
