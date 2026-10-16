"""The health registry (health spec §4): one place for operational problems.

Sources (the watcher, the daemon) `open` and `resolve` problems by key; the page and
the heartbeat read `problems()`; `tick` (the daemon's maintenance) sends Telegram
messages for **serious** problems only: once when one is open past its grace
period, and once when it is resolved. Warnings are page-only. Everything is
persisted in the `problems` table, so a restart neither forgets an open problem nor
sends its "opened" message again.

`open`, `resolve` and `forget` never send and never wait: a caller may hold a lock
(the watcher's) while it reports. Failed sends use the shared classification and
backoff (delivery.py); the alert policy retries every kind, AUTH and BAD_REQUEST
included, and keeps `attempts` and `next_try_at` per row, so a down Telegram is
tried once per backoff step, never on every 5 s tick.

Alert texts are plain text (``parse_mode=None``): no file contents, no secret.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from collections.abc import Callable, Collection, Iterable
from datetime import datetime, tzinfo
from typing import Final, Literal

from diffbell import BRAND
from diffbell.delivery import backoff, classify
from diffbell.store import Problem as Problem  # re-exported: the registry's row
from diffbell.store import Store
from diffbell.telegram import _TOKEN_IN_URL, ErrorKind, Sender, TelegramError

log = logging.getLogger(__name__)

Severity = Literal["serious", "warning"]
NOTICE: Final = "notice"  # the severity of a one-shot message (`notice`)
# Telegram not set up (bot onboarding spec §1): a page-only warning the daemon opens
# at a start without credentials and resolves when the bot is saved.
SETUP_KEY: Final = "telegram.setup"
# A longer message (e.g. a long config error) is clipped: an alert must always fit
# Telegram's 4096, or it would be retried forever.
MESSAGE_MAX = 1000


def _clip(message: str) -> str:
    # A lone surrogate (e.g. from an undecodable file name) cannot be stored or sent.
    message = message.encode("utf-8", "replace").decode("utf-8")
    return message if len(message) <= MESSAGE_MAX else message[: MESSAGE_MAX - 1] + "…"


def in_grace(p: Problem, now: float) -> bool:
    """``p`` is a serious problem whose "opened" message still waits for its grace
    period: nothing has been sent or tried yet."""
    return (
        p.severity == "serious"
        and not p.notified_open
        and p.resolved_at is None
        and p.attempts == 0
        and p.next_try_at > now
    )


def current(rows: Iterable[Problem]) -> list[Problem]:
    """The rows that are current problems: neither notices nor resolved ones whose
    resolution message is still to be sent."""
    return [p for p in rows if p.severity != NOTICE and p.resolved_at is None]


def _resolved_words(key: str, message: str) -> str:
    """What a resolution message says, by the kind of key."""
    kind, _, name = key.partition(":")
    if key == "watchman":
        return "Watchman is reachable again"
    if kind == "project" and name:
        return f"project {name}: watching again"
    if key == "config-watch":
        return "config edits are picked up again"
    if key == "config":
        return "the config is valid again"
    if key == "web":
        return "the web UI is running again"
    return f"resolved: {message}"


class Health:
    def __init__(
        self,
        store: Store,
        sender: Sender,
        *,
        clock: Callable[[], float] = time.time,
        tz: tzinfo | None = None,  # message times; None: the Mac's local time
    ) -> None:
        self._store = store
        self._sender = sender
        self._clock = clock
        self._tz = tz
        # The last fresh start per key: a fresh row never shares its opened_at with
        # the row it replaces, so a send in flight can tell the two apart.
        self._fresh_at: dict[str, float] = {}
        # Messages Telegram accepted whose store write then failed (e.g. "database is
        # locked"), by key, as sent: `tick` retries only the write, never the send.
        self._unrecorded: dict[str, Problem] = {}
        # The due time a re-open's grace replaced (`open` on a resolved row), by key,
        # with the row's opened_at: `resolve` goes back to it, so that grace delays
        # only the re-open's ⚠️, never the ✅ (Watcher.__init__ opens `watchman` with
        # a grace at every start). Valid only while the row has not been tried
        # since: a send attempt (failed or delivered) drops it, so a failed ⚠️'s
        # backoff or retry_after is never undone.
        self._before_grace: dict[str, tuple[float, float]] = {}

    def _fresh_start(self, key: str, now: float, replaced: Problem | None) -> float:
        """``now``, or the next float after the key's previous start when the clock
        has not moved past it (a forget + open within one clock reading)."""
        opened = now
        for prev in (self._fresh_at.get(key), replaced.opened_at if replaced else None):
            if prev is not None and opened <= prev:
                opened = math.nextafter(prev, math.inf)
        self._fresh_at[key] = opened
        return opened

    # -- sources ---------------------------------------------------------------

    def open(
        self,
        key: str,
        severity: Severity,
        message: str,
        *,
        project: str | None = None,
        fix: str | None = None,
        grace: float = 0.0,
    ) -> None:
        """Open ``key``, or update the message of the open problem with that key. An
        open problem keeps its start, its delivery state and its backoff. A problem
        re-opened while its resolution is still unsent simply continues (flapping
        while Telegram is down is one outage, not two). The "opened" message is due
        ``grace`` seconds after the start. The message is clipped to MESSAGE_MAX."""
        message = _clip(message)
        now = self._clock()
        stored = current = self._store.get_problem(key)
        if current is not None and current.severity != severity:
            if current.severity == "serious" and current.notified_open:
                severity = "serious"  # its ⚠️ went out: keep it, so the ✅ does too
            else:
                current = None  # a severity change is a fresh problem (forget + open)
        # In its grace a problem is only logged at INFO: tick warns once the grace is
        # over (a Watchman still starting must not read as trouble in the log).
        level = logging.INFO if grace > 0 else logging.WARNING
        if current is None or current.severity == NOTICE:
            log.log(level, "problem %s: %s", key, message)
            # Due at now + grace, not opened + grace: a nudged start must not
            # postpone a grace-free alert past this clock reading.
            opened = self._fresh_start(key, now, stored)
            self._store.put_problem(
                Problem(key, severity, project, message, fix, opened, next_try_at=now + grace)
            )
            return
        if current.message != message or current.resolved_at is not None:
            log.log(level, "problem %s: %s", key, message)
        if current.resolved_at is not None:
            self._before_grace[key] = (current.opened_at, current.next_try_at)
        self._store.put_problem(
            dataclasses.replace(
                current,
                severity=severity,
                project=project,
                message=message,
                fix=fix,
                resolved_at=None,
                # Continuing a resolved row (its resolution unsent or in flight): the
                # "opened" message of the re-open still waits for its grace.
                next_try_at=(
                    max(current.next_try_at, now + grace)
                    if current.resolved_at is not None
                    else current.next_try_at
                ),
            )
        )

    def resolve(self, key: str) -> None:
        """Resolve ``key`` (nothing if it is not open). A warning, or a serious
        problem resolved within its grace period before any attempt, is deleted
        silently. Otherwise the resolution message becomes due at once, or after the
        backoff (or retry_after) of a failed send: it is sent even if the "opened" one
        never got through. A re-open's grace never delays it."""
        current = self._store.get_problem(key)
        if current is None or current.severity == NOTICE or current.resolved_at is not None:
            return
        now = self._clock()
        log.info("problem %s resolved", key)
        due = current.next_try_at
        before = self._before_grace.pop(key, None)
        if before is not None and before[0] == current.opened_at:
            # Re-opened while its resolution was unsent: one outage, already past its
            # grace, so the re-open's grace neither hides it nor delays the ✅.
            due = min(due, before[1])
        short = not current.notified_open and current.attempts == 0 and now < due
        if current.severity != "serious" or short:
            self._store.delete_problem(key)
            return
        if current.attempts == 0:
            due = min(due, now)  # no failed send to wait for
        self._store.put_problem(dataclasses.replace(current, resolved_at=now, next_try_at=due))

    def forget(self, key: str) -> None:
        """Drop ``key`` without any message, for a source the user removed or turned
        off (a project dropped from the config, the web UI switched off). Never use it
        to resolve a problem: a sent ⚠️ would get no ✅. Use `resolve`, and have every
        source resolve its healthy keys at start, so a problem saved before a restart
        that has cleared since is resolved too."""
        self._before_grace.pop(key, None)
        self._store.delete_problem(key)

    def forget_projects_except(self, projects: Collection[str]) -> None:
        """Drop, without any message, the problems of projects no longer configured."""
        for problem in self._store.all_problems():
            if problem.project is not None and problem.project not in projects:
                self._before_grace.pop(problem.key, None)
                self._store.delete_problem(problem.key)

    def notice(
        self, key: str, text: str, *, since: float | None = None, not_before: float = 0.0
    ) -> None:
        """A one-shot message, sent verbatim by `tick` and then deleted; retried like
        an alert while Telegram is down. Never listed by `problems()`. It replaces an
        unsent notice with the same key. ``since`` is stored as its ``opened_at`` (when
        what it reports began; default now), and it is not sent before ``not_before``."""
        text = _clip(text)
        log.warning("%s", text)
        opened = self._clock() if since is None else since
        self._store.put_problem(
            Problem(key, NOTICE, None, text, None, opened, next_try_at=not_before)
        )

    # -- readers -----------------------------------------------------------------

    def problems(self) -> list[Problem]:
        """The current problems, oldest first: for the page and the heartbeat."""
        return current(self._store.all_problems())

    # -- delivery ------------------------------------------------------------------

    def _hhmm(self, ts: float) -> str:
        return f"{datetime.fromtimestamp(ts, self._tz):%H:%M}"

    def _text(self, p: Problem) -> str | None:
        """The message due for ``p`` now, or None."""
        if p.severity == NOTICE:
            return p.message
        if p.severity != "serious":
            return None
        if p.resolved_at is not None:
            span = f"{self._hhmm(p.opened_at)}–{self._hhmm(p.resolved_at)}"
            words = _resolved_words(p.key, p.message)
            if p.notified_open:
                return f"✅ {BRAND}: {words} (down {span})"
            # The "opened" message never got through: say what it was about.
            return f"✅ {BRAND}: {words} (was down {span}: {p.message})"
        if not p.notified_open:
            return f"⚠️ {BRAND}: {p.message}"
        return None

    async def tick(self, now: float, *, sending: bool = True) -> None:
        """Send every message that is due. Never raises. After a retryable failure
        (Telegram down), the other due rows are deferred without a call, so a tick
        never stalls for one timeout per problem; each waits its own backoff step (or
        the 429's retry_after), never the failing row's. A message sent whose store
        write failed is never sent again: only its write is retried.

        ``sending`` False (Telegram not set up, bot onboarding spec §1): nothing is
        sent; every notice and every resolved row is deleted unsent, whatever its due
        time, and open problems are left as they are (their ⚠️ waits for `rearm`)."""
        for key, sent in list(self._unrecorded.items()):
            try:
                self._sent(sent, now)
            except Exception:
                continue  # still unrecorded (logged once): retried next tick
            del self._unrecorded[key]
            log.info("problem %s: delivery recorded", key)
        if not sending:
            self._mute()
            return
        down: TelegramError | None = None  # this tick's retryable failure
        for listed in self._store.all_problems():
            if listed.key in self._unrecorded:
                continue
            # Re-read: a send awaited, and the row may have changed or gone meanwhile.
            problem = self._store.get_problem(listed.key)
            if problem is None or problem.next_try_at > now:
                continue
            text = self._text(problem)
            if text is None:
                continue
            if self._grace_over(problem):
                log.warning("problem %s: %s", problem.key, problem.message)
            if down is not None:
                self._failed(problem, classify(down, problem.attempts).delay, str(down), now)
                continue
            try:
                await self._sender.send(text, parse_mode=None)
            except TelegramError as exc:
                failure = classify(exc, problem.attempts)
                self._failed(problem, failure.delay, str(exc), now)
                if failure.kind is ErrorKind.RETRYABLE:
                    down = exc
            except Exception as exc:  # e.g. no chat id: never into the caller
                error = f"unexpected error: {type(exc).__name__}: {exc}"
                self._failed(problem, backoff(problem.attempts), error, now)
            else:
                try:
                    self._sent(problem, now)
                except Exception as exc:  # e.g. "database is locked"
                    self._unrecorded[problem.key] = problem
                    log.warning(
                        "problem %s: message sent but not recorded (%s); retrying the "
                        "write, never the send",
                        problem.key,
                        type(exc).__name__,
                    )

    def _mute(self) -> None:
        """A muted tick: drop what would otherwise go out once Telegram is set up.
        Notices (a restart notice may be queued up to RESTART_NOTICE_EVERY ahead) and
        resolutions (an unannounced one would report a backlog as "was down") are
        deleted, whatever their due time. Never raises."""
        try:
            for problem in self._store.all_problems():
                if problem.key in self._unrecorded:
                    continue
                if problem.severity == NOTICE or problem.resolved_at is not None:
                    self._before_grace.pop(problem.key, None)
                    self._store.delete_problem(problem.key)
                    log.info("problem %s: dropped unsent (Telegram isn't set up)", problem.key)
        except Exception as exc:  # e.g. "database is locked": the next tick tries again
            log.warning("muted health tick failed (%s)", type(exc).__name__)

    def rearm(self, now: float) -> int:
        """Telegram was just set up (bot onboarding spec §1): an open serious problem
        whose ⚠️ never went out is announced on a coming tick. A row with no failed
        attempts keeps its ``next_try_at`` (a running grace, or already past and so
        due); one with failed attempts starts over (``next_try_at = now``,
        ``attempts = 0``). An announced problem is never re-announced. Best effort:
        never raises (the save handler must not fail after the credentials were
        saved). Returns how many rows were examined and kept or rearmed."""
        count = 0
        try:
            for problem in self._store.all_problems():
                if (
                    problem.severity != "serious"
                    or problem.resolved_at is not None
                    or problem.notified_open
                    or problem.key in self._unrecorded
                ):
                    continue
                if problem.attempts > 0:
                    self._store.put_problem(
                        dataclasses.replace(problem, next_try_at=now, attempts=0)
                    )
                count += 1
        except Exception as exc:  # e.g. "database is locked"
            log.warning("rearming health problems failed (%s)", type(exc).__name__)
            return 0
        return count

    @staticmethod
    def _grace_over(p: Problem) -> bool:
        """``p``'s "opened" message is due for the first time after a grace period:
        the moment its WARNING is logged (open() logged it at INFO)."""
        return (
            p.severity == "serious"
            and not p.notified_open
            and p.resolved_at is None
            and p.attempts == 0
            and p.next_try_at > p.opened_at
        )

    def _current(self, sent: Problem) -> Problem | None:
        """The row ``sent`` was read from, re-read after the send; None if it was
        forgotten, or replaced by a fresh problem or notice, meanwhile. A fresh problem
        always has a new ``opened_at`` (`_fresh_start`), so a problem whose message
        changed meanwhile is still the same problem: its ⚠️ went out once. A notice
        may be replaced within one clock reading, so it is also compared by its text."""
        current = self._store.get_problem(sent.key)
        if current is None or current.opened_at != sent.opened_at:
            return None
        if sent.severity == NOTICE and current.message != sent.message:
            return None
        return current

    def _failed(self, problem: Problem, delay: float, error: str, now: float) -> None:
        # Tried since the re-open: its backoff now sets the ✅'s due time.
        self._before_grace.pop(problem.key, None)
        error = _TOKEN_IN_URL.sub("bot<redacted>", error)
        log.warning(
            "could not deliver the alert %s (retry in %.0fs): %s", problem.key, delay, error
        )
        try:
            current = self._current(problem)
            if current is None:
                return
            self._store.put_problem(
                dataclasses.replace(
                    current,
                    attempts=current.attempts + 1,
                    next_try_at=now + delay,
                    last_error=error,
                )
            )
        except Exception as exc:  # e.g. "database is locked": retried next tick
            log.warning(
                "problem %s: failed attempt not recorded (%s)", problem.key, type(exc).__name__
            )

    def _sent(self, sent: Problem, now: float) -> None:
        """Record a delivered message. The row is re-read: the send awaited, and the
        problem may have been resolved or re-opened meanwhile."""
        self._before_grace.pop(sent.key, None)  # tried: the pre-grace due time is stale
        current = self._current(sent)
        if current is None:
            return
        delivered = dataclasses.replace(current, attempts=0, next_try_at=0.0, last_error=None)
        if sent.severity == NOTICE or sent.resolved_at is not None:
            if current.resolved_at is not None or current.severity == NOTICE:
                self._store.delete_problem(sent.key)
            else:
                # Re-opened while its resolution was being sent: a new problem.
                self._store.put_problem(
                    dataclasses.replace(
                        delivered,
                        notified_open=False,
                        opened_at=self._fresh_start(sent.key, now, current),
                        next_try_at=current.next_try_at,  # set by open(): its grace
                    )
                )
            return
        self._store.put_problem(dataclasses.replace(delivered, notified_open=True))
