"""Outbox driver: batch → event → message → Telegram → status (spec §5, §8).

Notification controls (notify controls spec §4) are decided here and nowhere
else: events are always created; at delivery the project's notify_on rules come
first (nothing left: `skipped`), then a pause (Telegram not set up, a snooze or
quiet hours: `held`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sqlite3
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from diffbell.config import NOTIFY_KINDS, ResolvedProject
from diffbell.delivery import backoff as backoff  # re-exported: tests use it
from diffbell.delivery import classify
from diffbell.diffing import FileReport, build_reports, current_branch
from diffbell.models import Change, ChangeKind, collapse
from diffbell.patterns import is_redacted
from diffbell.pausing import HELD_FOREVER, PauseReason, QuietHours, pause_until
from diffbell.render import CatchUp, Composed, MessageContext, compose, message_order
from diffbell.snapshots import EventCapture, Snapshotter, StoredContent, content_based
from diffbell.store import EventRow, Snooze, Store
from diffbell.telegram import ErrorKind, Sender, TelegramError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotifierSettings:
    redact: tuple[str, ...]
    max_lines: int
    web_enabled: bool
    web_port: int | None = None  # the deep link's port; None while the web UI is disabled
    quiet: QuietHours | None = None  # the daily quiet hours, None when off
    machine: str | None = None  # this Mac's name in messages (ADR 0023); None: no name


NOTHING_SENT = Composed("", 0, 0)  # what a held or skipped event's report records


def split_by_rules(
    reports: list[FileReport], notify_on: Collection[str]
) -> tuple[list[FileReport], list[FileReport]]:
    """(the files to send, the files a notify_on rule leaves out), in report order."""
    kept = [r for r in reports if r.kind.value in notify_on]
    return kept, [r for r in reports if r.kind.value not in notify_on]


def catch_up_reason(events: Collection[EventRow]) -> PauseReason:
    """What a catch-up's header says: "setup" if any event waited for Telegram to be
    set up, else "snooze" if any was snoozed, else "quiet"."""
    reasons = {e.held_reason for e in events}
    if "setup" in reasons:
        return "setup"
    return "snooze" if "snooze" in reasons else "quiet"


class Notifier:
    def __init__(
        self,
        store: Store,
        sender: Sender,
        *,
        projects: Callable[[str], ResolvedProject | None],
        settings: Callable[[], NotifierSettings],
        clock: Callable[[], float] = time.time,
        requeue: Callable[[list[Change]], None] | None = None,
        # The notify_on of a configured project by name, also one whose folder is
        # missing (not resolved); None when no project of that name is configured.
        configured: Callable[[str], tuple[str, ...] | None] = lambda name: None,
        tz: tzinfo | None = None,  # quiet hours' zone; None: the Mac's local time
        # Whether Telegram is set up (the daemon's SenderSlot). While it is not,
        # every event is held with reason "setup" (bot onboarding spec §1).
        telegram_configured: Callable[[], bool] = lambda: True,
        # Content snapshots (content snapshots spec): None leaves contents untracked,
        # and every report comes from git at send time, as before.
        snapshots: Snapshotter | None = None,
    ) -> None:
        self._store = store
        self._snapshots = snapshots
        self._sender = sender
        self._projects = projects
        self._settings = settings
        self._clock = clock
        self._requeue = requeue
        self._configured = configured
        self._tz = tz
        self._telegram_configured = telegram_configured
        self._inflight: set[int] = set()
        # (name, active) of the projects whose catch-up is being sent.
        self._catching_up: set[tuple[str, bool]] = set()

    async def flush(self, project: str, changes: list[Change]) -> int | None:
        """One event per project (folder) in the batch: rows of two folders can share
        a name (one no longer configured), and are never collapsed or sent together.
        Returns the first event's id (None if nothing is kept). A group that fails is
        handled alone (see _flush_one); the first failure is raised after the rest."""
        try:
            groups = self._store.split_by_project(changes)
        except sqlite3.OperationalError:
            self._requeue_after_error(project, changes)
            raise
        first: int | None = None
        error: Exception | None = None
        for group in groups:
            try:
                event_id = await self._flush_one(project, group)
            except Exception as exc:
                error = error or exc
                continue
            if first is None:
                first = event_id
        if error is not None:
            raise error
        return first

    def _requeue_after_error(self, project: str, changes: list[Change]) -> None:
        # Transient (locked/busy/IO), nothing persisted: hand the batch back so it is
        # not lost until restart.
        log.exception("could not create event for %s; requeueing %d changes", project, len(changes))
        if self._requeue is not None:
            self._requeue(changes)

    async def _flush_one(self, project: str, changes: list[Change]) -> int | None:
        kept, superseded = collapse(changes)
        # Read, then create_event: the only writer that advances the snapshots from
        # captured afters. Flushes stay serial (the debouncer awaits each one), so the
        # next event's "before" is exactly this one's "after" (content snapshots spec
        # §2.3). A REBASELINE also stores snapshots, but never for a path with a
        # pending journal row, and never if a snapshot write happened during its
        # read (Store.replace_baseline): it never regresses what this flush stores
        # (a path may fall back once).
        capture = await self._capture(project, kept)
        # No await from here to create_event: a reload that grew the redaction list
        # while the capture ran is honoured here, never after the bytes are stored.
        capture = self._still_unredacted(capture)
        try:
            event_id = self._store.create_event(
                project,
                kept,
                superseded,
                self._clock(),
                contents=None if capture is None else capture.contents,
                branch=None if capture is None else capture.branch,
            )
        except sqlite3.OperationalError:
            self._requeue_after_error(project, changes)
            raise
        except Exception:
            # Deterministic failure: requeueing would loop forever.
            log.exception("could not create event for %s; not requeueing", project)
            raise
        if event_id is not None:
            await self.deliver(event_id)
        return event_id

    async def _capture(self, project: str, kept: list[Change]) -> EventCapture | None:
        """The contents, note and branch to freeze with the event (spec §2.2), read off
        the loop; None without a snapshotter. Only a configured project's folder is
        read: the rows' project may be one no longer configured whose name is now
        another folder's. A failure is logged by type only: the event is created
        without contents, and its paths fall back. That includes a capture bug an
        invalid Captured raises (ValueError) before create_event could see it."""
        if self._snapshots is None or not kept:
            return None
        try:
            owner = self._store.owner(kept)
            resolved = self._projects(owner[0]) if owner is not None and owner[1] else None
            root = resolved.path if resolved is not None else None
            redact = self._settings().redact
            return await asyncio.to_thread(self._snapshots.capture, root, kept, redact)
        except Exception as exc:
            log.warning("could not capture the contents of %s (%s)", project, type(exc).__name__)
            return EventCapture((), None)

    def _still_unredacted(self, capture: EventCapture | None) -> EventCapture | None:
        """``capture`` without the paths the redaction list current now covers (spec
        §2.5): the capture read them under the list of its start, and a reload may
        have added a covering pattern since. A dropped path is stored as never
        captured (no contents, its snapshot deleted). If the list can't be read,
        nothing captured is kept."""
        if capture is None or not capture.contents:
            return capture
        try:
            redact = self._settings().redact
        except Exception as exc:
            log.warning("could not re-check the redaction list (%s)", type(exc).__name__)
            return EventCapture((), capture.branch)
        kept = tuple(c for c in capture.contents if not is_redacted(c.path, redact))
        if len(kept) == len(capture.contents):
            return capture
        return EventCapture(kept, capture.branch)

    async def retry_due(self) -> None:
        for event in self._store.due_events(self._clock()):
            try:
                await self.deliver(event.id)
            except Exception:
                log.exception("unexpected error delivering event %d", event.id)

    async def catch_up_due(self) -> None:
        """Send one catch-up message per project whose held events are due (spec
        §4.3); the daemon's maintenance tick calls it. Projects are isolated."""
        # Keyed on the active flag too: a project no longer configured may share its
        # name with a configured one (another folder), and is never merged with it.
        groups: dict[tuple[str, bool], list[EventRow]] = {}
        for event in self._store.due_held(self._clock()):
            groups.setdefault((event.project, event.project_active), []).append(event)
        for key, events in groups.items():
            if key in self._catching_up:
                continue
            self._catching_up.add(key)
            try:
                await self._catch_up(key[0], events)
            except Exception:
                log.exception("unexpected error sending the catch-up of %s", key[0])
            finally:
                self._catching_up.discard(key)

    def _merged_changes(self, events: list[EventRow]) -> tuple[list[Change], dict[str, int]]:
        """The group's changes merged per path by net effect, in order of first
        appearance; and how many journal rows each path had. The file existed before
        the hold unless its first change created it, and exists after it unless its
        latest change deleted it: created then modified reads created, deleted then
        created reads modified, and created then deleted is dropped (it never existed
        outside the hold), as models.collapse drops it within one event."""
        first: dict[str, Change] = {}
        latest: dict[str, Change] = {}
        rows: dict[str, int] = {}
        for event in events:
            for change in self._store.event_changes(event.id):
                first.setdefault(change.path, change)
                latest[change.path] = change
                rows[change.path] = rows.get(change.path, 0) + 1
        merged: list[Change] = []
        for path, last in latest.items():
            existed = first[path].kind is not ChangeKind.CREATED
            exists = last.kind is not ChangeKind.DELETED
            if not existed and not exists:
                continue
            if existed and exists:
                kind = ChangeKind.MODIFIED
            elif exists:
                kind = ChangeKind.CREATED
            else:
                kind = ChangeKind.DELETED
            merged.append(dataclasses.replace(last, kind=kind))
        return merged, rows

    def _held_contents(
        self, events: list[EventRow], kept: list[Change]
    ) -> dict[str, StoredContent]:
        """A catch-up's contents, per path (spec §3.3): the first held event that
        touched the path gives the "before", the last one the "after" and the note.
        A side that is gone is never taken from another event: the path then falls
        back on its own."""
        wanted = {c.path for c in kept}
        opening: dict[str, StoredContent | None] = {}
        closing: dict[str, StoredContent | None] = {}
        for event in events:
            touched = {c.path for c in self._store.event_changes(event.id)} & wanted
            if not touched:
                continue
            rows = self._store.event_contents(event.id)
            for path in touched:
                opening.setdefault(path, rows.get(path))
                closing[path] = rows.get(path)
        merged: dict[str, StoredContent] = {}
        for path in wanted:
            first, last = opening.get(path), closing.get(path)
            if last is None:
                continue
            before = None if first is None else first.before
            merged[path] = StoredContent(path, before, last.after, last.after_state, last.note)
        return merged

    def _reports(
        self,
        root: Path | None,
        changes: list[Change],
        contents: Mapping[str, StoredContent],
        redact: tuple[str, ...],
    ) -> list[FileReport]:
        """One report per change, in order (content snapshots spec §3.1): from the
        stored contents where they allow it, else today's git path (vs HEAD, a new
        untracked file's first lines, or name and kind), per path; an engine that
        raises is that path's fallback. Runs off-loop."""
        built: list[FileReport | None] = []
        fallback: list[Change] = []
        for change in changes:
            row = contents.get(change.path)
            report: FileReport | None = None
            if (
                self._snapshots is not None
                and row is not None
                and not is_redacted(change.path, redact)
                and content_based(change.kind, row)
            ):
                before = None if change.kind is ChangeKind.CREATED else row.before
                try:
                    report = self._snapshots.report(
                        change.path, change.kind, before, row.after, row.note
                    )
                except Exception as exc:
                    # That path falls back; the rest of the message still goes out (an
                    # escaping exception would fail every retry with nothing sent).
                    # The type only: the message could quote content.
                    log.warning("content diff raised (%s); using the fallback", type(exc).__name__)
                    report = None
            if report is None:
                fallback.append(change)
            built.append(report)
        rest = iter(build_reports(root, fallback, redact) if fallback else [])
        return [report if report is not None else next(rest) for report in built]

    async def _branch(self, event_id: int, root: Path | None) -> str | None:
        """The branch frozen at creation; git's current one for an event created
        without contents (older code) or whose meta was pruned."""
        meta = self._store.event_meta(event_id)
        if meta is not None:
            return meta.branch
        return await asyncio.to_thread(current_branch, root)

    def _lookup(self, event: EventRow) -> ResolvedProject | None:
        """The event's configured project, or None for a project no longer configured
        (an inactive row, whose name may now be another folder's)."""
        return self._projects(event.project) if event.project_active else None

    def _snooze(self, event: EventRow) -> Snooze | None:
        """As _lookup: a project no longer configured has no snooze of its own (its
        snooze was dropped) and never takes a same-named project's."""
        return self._store.get_snooze(event.project) if event.project_active else None

    def _rules(
        self, name: str, resolved: ResolvedProject | None, active: bool = True
    ) -> tuple[tuple[str, ...], bool]:
        """(the notify_on in force, whether the project was removed from the config).
        A configured project whose folder is missing is not resolved, but keeps its
        rules and is not "removed". ``active`` False: removed, whatever the name."""
        if not active:
            return NOTIFY_KINDS, True
        if resolved is not None:
            return resolved.notify_on, False
        configured = self._configured(name)
        if configured is not None:
            return configured, False
        return NOTIFY_KINDS, True

    async def _catch_up(self, project: str, events: list[EventRow]) -> None:
        ids = [e.id for e in events]
        attempts = max(e.attempts for e in events)
        try:
            settings = self._settings()
            # Re-checked now: a pause may have been extended, or quiet hours begun.
            # The setup hold applies before the project lookup, so it also holds
            # removed projects.
            snooze = self._snooze(events[0])
            pause = pause_until(
                snooze,
                settings.quiet,
                self._clock(),
                self._tz,
                configured=self._telegram_configured(),
            )
            if pause is not None:
                self._store.reschedule_held(ids, pause.until, pause.reason)
                return
            resolved = self._lookup(events[0])
            merged, rows = self._merged_changes(events)
            # The rules in force now (spec §6): a group they empty is skipped.
            notify_on, removed = self._rules(project, resolved, events[0].project_active)
            kept = [c for c in merged if c.kind.value in notify_on]
            if not kept:
                self._store.mark_skipped(ids)
                return
            path = resolved.path if resolved else None
            # The hold's first "before" vs its last "after", per path, from the stored
            # contents (content snapshots spec §3.3); in the message's order, so the
            # stored copy's telegram_files counts from the top.
            contents = self._held_contents(events, kept)
            reports = message_order(
                await asyncio.to_thread(self._reports, path, kept, contents, settings.redact)
            )
            first, last = events[0], events[-1]
            branch = await self._branch(last.id, path)
            held = CatchUp(
                datetime.fromtimestamp(first.created_at),
                datetime.fromtimestamp(last.created_at),
                catch_up_reason(events),
                sum(rows[c.path] for c in kept),
                removed=removed,
            )
            ctx = MessageContext(
                project,
                branch,
                datetime.fromtimestamp(last.created_at),
                settings.web_enabled,
                event_id=last.id,
                more=len(events) - 1,
                web_port=settings.web_port,
                catch_up=held,
                machine=settings.machine,
            )
            composed = compose(ctx, reports, max_lines=settings.max_lines, html=True)
            batch_id = first.id
            # Before the send, like an event's report: the copy the message came from.
            self._store_batch_report(batch_id, reports, composed)
            try:
                message_id = await self._sender.send(composed.text, parse_mode="HTML")
            except TelegramError as exc:
                await self._handle_batch_error(ids, batch_id, attempts, exc, ctx, reports, settings)
                return
            self._store.mark_batch_sent(ids, message_id, batch_id)
        except Exception as exc:
            log.exception("unexpected error sending the catch-up of %s", project)
            self._store.mark_batch_retry(
                ids,
                f"unexpected error: {type(exc).__name__}: {exc}",
                self._clock() + backoff(attempts),
                kind=ErrorKind.RETRYABLE.value,
                at=self._clock(),
            )

    def _store_batch_report(
        self, batch_id: int, reports: list[FileReport], composed: Composed
    ) -> None:
        """Best effort, like _store_report."""
        try:
            self._store.put_batch_report(
                batch_id, reports, telegram_lines=composed.lines, telegram_files=composed.files
            )
        except Exception as exc:
            log.warning(
                "could not store the catch-up report of batch %d (%s)", batch_id, type(exc).__name__
            )

    async def _handle_batch_error(
        self,
        ids: list[int],
        batch_id: int,
        attempts: int,
        exc: TelegramError,
        ctx: MessageContext,
        reports: list[FileReport],
        settings: NotifierSettings,
    ) -> None:
        """As _handle_error, for the whole group: it is retried, fails or falls back
        to plain text as one message. The fallback is tried while the group is still
        held, so there is no `failed` window in which a web Retry could queue the
        batch again while the fallback is being sent."""
        if exc.kind is ErrorKind.NOT_SET_UP:
            # The belt (bot onboarding spec §1): held again, never failed.
            self._store.reschedule_held(ids, HELD_FOREVER, "setup")
            try:  # the report was stored before the send: nothing went out
                self._store.set_batch_report_telegram(batch_id, lines=0, files=0)
            except Exception as err:
                log.warning(
                    "could not update the catch-up report of batch %d (%s)",
                    batch_id,
                    type(err).__name__,
                )
            log.info("Telegram isn't set up; catch-up batch %d held", batch_id)
            return
        failure = classify(exc, attempts)
        if failure.kind is ErrorKind.RETRYABLE:
            self._store.mark_batch_retry(
                ids,
                str(exc),
                self._clock() + failure.delay,
                kind=failure.kind.value,
                at=self._clock(),
            )
            log.warning(
                "telegram catch-up deferred %.0fs (batch %d): %s", failure.delay, batch_id, exc
            )
            return
        log.error("telegram catch-up failed (batch %d): %s", batch_id, exc)
        if failure.kind is not ErrorKind.BAD_REQUEST:
            self._store.mark_batch_failed(
                ids, str(exc), batch_id, kind=failure.kind.value, at=self._clock()
            )
            return
        plain = compose(ctx, reports, max_lines=settings.max_lines, html=False)
        try:
            message_id = await self._sender.send(plain.text, parse_mode=None)
        except TelegramError as fallback_exc:
            log.error("plain-text fallback failed (batch %d): %s", batch_id, fallback_exc)
            self._store.mark_batch_failed(
                ids, str(exc), batch_id, kind=failure.kind.value, at=self._clock()
            )
            return
        self._store.mark_batch_sent(
            ids, message_id, batch_id, note=f"plain-text fallback after: {exc}"
        )
        try:
            self._store.set_batch_report_telegram(batch_id, lines=plain.lines, files=plain.files)
        except Exception as fallback_store_exc:
            log.warning(
                "could not update the catch-up report of batch %d (%s)",
                batch_id,
                type(fallback_store_exc).__name__,
            )

    async def deliver(self, event_id: int) -> None:
        if event_id in self._inflight:
            return
        self._inflight.add(event_id)
        try:
            await self._deliver(event_id)
        finally:
            self._inflight.discard(event_id)

    async def _deliver(self, event_id: int) -> None:
        event = self._store.get_event(event_id)
        # retry_due works from a snapshot: honour a deferral recorded since it was taken.
        if event.status != "pending" or event.next_attempt_at > self._clock():
            return
        try:
            changes = self._store.event_changes(event_id)
            settings = self._settings()
            project = self._lookup(event)
            path = project.path if project else None
            # From the contents and branch frozen at creation (content snapshots spec
            # §3.3); git runs off-loop so a slow repository can't stall Watchman.
            contents = self._store.event_contents(event_id)
            reports = await asyncio.to_thread(
                self._reports, path, changes, contents, settings.redact
            )
            branch = await self._branch(event_id, path)
            # Rules first (N8): a file notify_on leaves out is stored, flagged, and
            # listed after the sent ones (telegram_files counts only those). The sent
            # ones are stored in the message's order (health spec §3.1).
            rules = self._rules(event.project, project, event.project_active)[0]
            kept, ruled_out = split_by_rules(reports, rules)
            kept = message_order(kept)
            stored = kept + ruled_out
            flagged = {r.path for r in ruled_out}
            if not kept:
                self._store_report(event_id, branch, stored, NOTHING_SENT, flagged)
                self._store.mark_skipped([event_id])
                return
            # Then the pause: held until it ends, with the report as it was now.
            snooze = self._snooze(event)
            pause = pause_until(
                snooze,
                settings.quiet,
                self._clock(),
                self._tz,
                configured=self._telegram_configured(),
            )
            if pause is not None:
                self._store_report(event_id, branch, stored, NOTHING_SENT, flagged)
                self._store.mark_held(event_id, pause.until, pause.reason)
                return
            ctx = MessageContext(
                event.project,
                branch,
                datetime.fromtimestamp(event.created_at),
                settings.web_enabled,
                event_id=event_id,
                web_port=settings.web_port,
                machine=settings.machine,
            )
            composed = compose(ctx, kept, max_lines=settings.max_lines, html=True)
            # Before the send, so a failed attempt has its diff too (U12).
            self._store_report(event_id, branch, stored, composed, flagged)
            try:
                message_id = await self._sender.send(composed.text, parse_mode="HTML")
            except TelegramError as exc:
                await self._handle_error(event_id, event.attempts, exc, ctx, kept, settings)
                return
            self._store.mark_sent(event_id, message_id)
        except Exception as exc:
            # Poison event: back off instead of blocking every later event each tick.
            log.exception("unexpected error delivering event %d", event_id)
            self._store.mark_retry(
                event_id,
                f"unexpected error: {type(exc).__name__}: {exc}",
                self._clock() + backoff(event.attempts),
                kind=ErrorKind.RETRYABLE.value,
                at=self._clock(),
            )

    def _store_report(
        self,
        event_id: int,
        branch: str | None,
        reports: list[FileReport],
        composed: Composed,
        rule_skipped: Collection[str] = (),
    ) -> None:
        """Best effort: the web UI's copy of the diff never blocks or fails a send.
        Logged without the exception text, which could quote a line."""
        try:
            self._store.put_event_report(
                event_id,
                branch,
                reports,
                telegram_lines=composed.lines,
                telegram_files=composed.files,
                rule_skipped=rule_skipped,
            )
        except Exception as exc:
            log.warning("could not store the report of event %d (%s)", event_id, type(exc).__name__)

    def _store_fallback_excerpt(self, event_id: int, composed: Composed) -> None:
        try:
            self._store.set_report_telegram(event_id, lines=composed.lines, files=composed.files)
        except Exception as exc:
            log.warning(
                "could not update the report of event %d (%s)", event_id, type(exc).__name__
            )

    async def _handle_error(
        self,
        event_id: int,
        attempts: int,
        exc: TelegramError,
        ctx: MessageContext,
        reports: list[FileReport],
        settings: NotifierSettings,
    ) -> None:
        if exc.kind is ErrorKind.NOT_SET_UP:
            # The belt (bot onboarding spec §1): a race with the slot holds the
            # event until setup releases it; it is never marked failed.
            self._store.mark_held(event_id, HELD_FOREVER, "setup")
            self._store_fallback_excerpt(event_id, NOTHING_SENT)  # nothing went out
            log.info("Telegram isn't set up; event %d held", event_id)
            return
        # The shared classification; events treat AUTH and BAD_REQUEST as terminal.
        failure = classify(exc, attempts)
        if failure.kind is ErrorKind.RETRYABLE:
            self._store.mark_retry(
                event_id,
                str(exc),
                self._clock() + failure.delay,
                kind=failure.kind.value,
                at=self._clock(),
            )
            log.warning("telegram send deferred %.0fs (event %d): %s", failure.delay, event_id, exc)
            return
        self._store.mark_failed(event_id, str(exc), kind=failure.kind.value, at=self._clock())
        log.error("telegram send failed (event %d): %s", event_id, exc)
        if failure.kind is not ErrorKind.BAD_REQUEST:
            return
        # Exactly one plain-text attempt, rendered from the same redaction-filtered reports.
        plain = compose(ctx, reports, max_lines=settings.max_lines, html=False)
        try:
            message_id = await self._sender.send(plain.text, parse_mode=None)
        except TelegramError as fallback_exc:
            log.error("plain-text fallback failed (event %d): %s", event_id, fallback_exc)
            return
        self._store.mark_sent(event_id, message_id, note=f"plain-text fallback after: {exc}")
        self._store_fallback_excerpt(event_id, plain)
