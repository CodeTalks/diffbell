from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

import pytest

from diffbell import diffing
from diffbell import render as render_mod
from diffbell.config import BUILTIN_REDACT, ResolvedProject
from diffbell.diffing import FileReport
from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.notifier import Notifier, NotifierSettings, backoff
from diffbell.pausing import HELD_FOREVER
from diffbell.render import Composed
from diffbell.store import ClockState, Store
from diffbell.telegram import ErrorKind, NotSetUp, TelegramError
from tests.fakes import FakeSender
from tests.helpers import error_at, error_kind, git, init_repo, sync_names

STATE = ClockState("/w", "", "c:1", "h")


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "proj")
    return store


def journal(store: Store, *paths: str, kind: ChangeKind = ChangeKind.MODIFIED) -> list[Change]:
    obs = Observation(changes=[(p, kind) for p in paths])
    return store.record_observation("proj", STATE, obs, now=1.0)


def make_notifier(
    store: Store, sender: FakeSender, project_path: Path, now: float = 1000.0
) -> Notifier:
    project = ResolvedProject("proj", project_path, ("*",), ())
    return Notifier(
        store,
        sender,
        projects=lambda name: project if name == "proj" else None,
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=lambda: now,
    )


async def test_flush_sends_once_and_marks_sent(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([7])
    changes = journal(store, "package.json")
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", changes)
    assert event_id is not None
    assert len(sender.sent) == 1 and sender.sent[0][1] == "HTML"
    ev = store.get_event(event_id)
    assert (ev.status, ev.telegram_message_id) == ("sent", 7)


async def test_batch_collapsing_to_nothing_sends_nothing(store: Store, tmp_project: Path) -> None:
    sender = FakeSender()
    created = journal(store, "a", kind=ChangeKind.CREATED)
    deleted = journal(store, "a", kind=ChangeKind.DELETED)
    assert await make_notifier(store, sender, tmp_project).flush("proj", created + deleted) is None
    assert sender.sent == []
    assert store.unflushed_changes() == []


async def test_retryable_error_keeps_pending_with_retry_after(
    store: Store, tmp_project: Path
) -> None:
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "429", retry_after=30)])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.next_attempt_at) == ("pending", 1, 1030.0)


async def test_retryable_without_retry_after_uses_backoff(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "502")])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert store.get_event(event_id).next_attempt_at == 1000.0 + backoff(0)


async def test_auth_error_fails_without_fallback(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([TelegramError(ErrorKind.AUTH, "401")])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert store.get_event(event_id).status == "failed"
    assert len(sender.sent) == 1


async def test_bad_request_sends_one_redacted_plain_text_fallback(
    store: Store, tmp_project: Path
) -> None:
    (tmp_project / ".env.local").write_text("API_KEY=SECRET-VALUE\n")
    sender = FakeSender([TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"), 9])
    changes = journal(store, ".env.local", "package.json")
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", changes)
    assert event_id is not None
    assert len(sender.sent) == 2
    plain, mode = sender.sent[1]
    assert mode is None
    assert "<" not in plain
    assert "🔒 .env.local · contents hidden" in plain
    assert "SECRET-VALUE" not in plain
    ev = store.get_event(event_id)
    assert ev.status == "sent" and ev.telegram_message_id == 9
    assert ev.last_error is not None and "plain-text fallback" in ev.last_error


async def test_failed_fallback_is_not_retried(store: Store, tmp_project: Path) -> None:
    sender = FakeSender(
        [TelegramError(ErrorKind.BAD_REQUEST, "bad"), TelegramError(ErrorKind.RETRYABLE, "500")]
    )
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert len(sender.sent) == 2
    assert store.get_event(event_id).status == "failed"
    assert store.due_events(now=10**9) == []


async def test_retry_due_redelivers_pending(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "500", retry_after=1), 5])
    notifier = make_notifier(store, sender, tmp_project, now=1000.0)
    event_id = await notifier.flush("proj", journal(store, "a"))
    assert event_id is not None
    later = make_notifier(store, sender, tmp_project, now=2000.0)
    await later.retry_due()
    assert store.get_event(event_id).status == "sent"
    assert len(sender.sent) == 2


async def test_unknown_project_still_reports_metadata(store: Store, tmp_project: Path) -> None:
    """Journaled while "gone" was configured, flushed after a reload removed it: the
    event still takes its journal rows' project."""
    sender = FakeSender()
    obs = Observation(changes=[("a", ChangeKind.MODIFIED)])
    sync_names(store, "proj", "gone")
    changes = store.record_observation("gone", STATE, obs, now=1.0)
    sync_names(store, "proj")
    await make_notifier(store, sender, tmp_project).flush("gone", changes)
    assert sender.sent[0][0].startswith("<b>🔑 gone</b>")


async def test_create_event_failure_requeues_changes_and_does_not_send(
    store: Store, tmp_project: Path
) -> None:
    class FlakyStore(Store):
        def create_event(
            self,
            project: str,
            kept: list[Change],
            superseded: list[int],
            now: float,
            **kwargs: Any,
        ) -> int | None:
            raise sqlite3.OperationalError("disk I/O error")

    flaky = FlakyStore(tmp_project.parent / "flaky.db")
    sender = FakeSender()
    requeued: list[list[Change]] = []
    project = ResolvedProject("proj", tmp_project, ("*",), ())
    notifier = Notifier(
        flaky,
        sender,
        projects=lambda name: project,
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=lambda: 1000.0,
        requeue=requeued.append,
    )
    changes = journal(store, "a")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        await notifier.flush("proj", changes)
    assert requeued == [changes]
    assert sender.sent == []


class GatedSender:
    """Sends block on per-path gates so a test controls the interleaving."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.entered: dict[str, asyncio.Event] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.outcomes: dict[str, TelegramError] = {}

    def _ev(self, table: dict[str, asyncio.Event], key: str) -> asyncio.Event:
        return table.setdefault(key, asyncio.Event())

    async def send(self, text: str, *, parse_mode: str | None = "HTML") -> int:
        key = "B" if "zzb" in text else "A"
        self.order.append(key)
        self._ev(self.entered, key).set()
        await self._ev(self.gates, key).wait()
        if key in self.outcomes:
            raise self.outcomes.pop(key)
        return 1


async def test_retry_due_does_not_resend_event_deferred_during_the_pass(
    store: Store, tmp_project: Path
) -> None:
    sender = GatedSender()
    sender.outcomes["B"] = TelegramError(ErrorKind.RETRYABLE, "429", retry_after=600)
    notifier = make_notifier(store, sender, tmp_project)  # type: ignore[arg-type]
    a = store.create_event("proj", journal(store, "zza"), [], 900.0)
    assert a is not None
    flush_task = asyncio.create_task(notifier.flush("proj", journal(store, "zzb")))
    await sender._ev(sender.entered, "B").wait()  # flush's delivery of B is in flight
    retry_task = asyncio.create_task(notifier.retry_due())  # snapshot is [A, B]
    await sender._ev(sender.entered, "A").wait()
    sender._ev(sender.gates, "B").set()  # B gets 429 and is deferred while A is in flight
    await flush_task
    sender._ev(sender.gates, "A").set()
    await retry_task
    assert sender.order == ["B", "A"]
    ev = store.get_event(a + 1)
    assert (ev.status, ev.next_attempt_at) == ("pending", 1600.0)


async def test_flush_first_delivery_happens_with_fixed_clock(
    store: Store, tmp_project: Path
) -> None:
    sender = FakeSender([3])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert len(sender.sent) == 1 and store.get_event(event_id).status == "sent"


async def test_deliver_skips_event_not_yet_due(store: Store, tmp_project: Path) -> None:
    sender = FakeSender()
    event_id = store.create_event("proj", journal(store, "a"), [], 900.0)
    assert event_id is not None
    store.mark_retry(event_id, "429", 2000.0, kind="retryable", at=0.0)
    await make_notifier(store, sender, tmp_project).deliver(event_id)
    assert sender.sent == []
    assert store.get_event(event_id).attempts == 1


class RaisingOnceSender(FakeSender):
    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        if not self.sent:
            self.sent.append((text, parse_mode))
            raise ValueError("chat_id is required to send messages")
        return await super().send(text, parse_mode=parse_mode, silent=silent)


async def test_unexpected_error_marks_retry_and_does_not_block_later_events(
    store: Store, tmp_project: Path
) -> None:
    sender = RaisingOnceSender()
    e1 = store.create_event("proj", journal(store, "a"), [], 1.0)
    e2 = store.create_event("proj", journal(store, "b"), [], 1.0)
    assert e1 is not None and e2 is not None
    await make_notifier(store, sender, tmp_project).retry_due()  # must not raise
    first, second = store.get_event(e1), store.get_event(e2)
    assert (first.status, first.attempts, first.next_attempt_at) == (
        "pending",
        1,
        1000.0 + backoff(0),
    )
    assert first.last_error == "unexpected error: ValueError: chat_id is required to send messages"
    assert (error_kind(store, e1), error_at(store, e1)) == ("retryable", 1000.0)
    assert second.status == "sent"


async def test_retry_due_survives_an_exception_escaping_deliver(
    store: Store, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = FakeSender()
    e1 = store.create_event("proj", journal(store, "a"), [], 1.0)
    e2 = store.create_event("proj", journal(store, "b"), [], 1.0)
    assert e1 is not None and e2 is not None
    notifier = make_notifier(store, sender, tmp_project)
    real = notifier._deliver

    async def flaky(event_id: int) -> None:
        if event_id == e1:
            raise RuntimeError("boom")
        await real(event_id)

    monkeypatch.setattr(notifier, "_deliver", flaky)
    await notifier.retry_due()
    assert store.get_event(e2).status == "sent"


async def test_flush_requeues_only_on_transient_sqlite_errors(
    store: Store, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requeued: list[list[Change]] = []
    project = ResolvedProject("proj", tmp_project, ("*",), ())
    notifier = Notifier(
        store,
        FakeSender(),
        projects=lambda name: project,
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=lambda: 1000.0,
        requeue=requeued.append,
    )
    changes = journal(store, "a")

    def locked(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "create_event", locked)
    with pytest.raises(sqlite3.OperationalError):
        await notifier.flush("proj", changes)
    assert requeued == [changes]

    def missing(*args: object, **kwargs: object) -> int:
        raise ValueError("journal row 1 missing")

    monkeypatch.setattr(store, "create_event", missing)
    with pytest.raises(ValueError):
        await notifier.flush("proj", changes)
    assert requeued == [changes]  # unchanged: no requeue for deterministic failures


# -- stored reports (UI refine spec §3.1) -----------------------------------------

MARKER = "MARKER-0c4f2a"


def committed_repo(path: Path, files: dict[str, str]) -> Path:
    init_repo(path)
    for name, text in files.items():
        (path / name).write_text(text)
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    return path


async def test_a_sent_event_stores_its_report(store: Store, tmp_project: Path) -> None:
    committed_repo(tmp_project, {"package.json": '{"v": 1}\n'})
    (tmp_project / "package.json").write_text('{"v": 2}\n')
    (tmp_project / ".env.local").write_text(f"TOKEN={MARKER}\n")
    sender = FakeSender([7])
    changes = journal(store, "package.json", ".env.local")
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", changes)
    assert event_id is not None
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert (stored.branch, stored.added, stored.removed) == ("main", 1, 1)
    assert (stored.telegram_lines, stored.telegram_files) == (20, 2)
    package, env = stored.reports
    assert package["lines"] == ['-{"v": 1}', '+{"v": 2}']
    assert env["redacted"] is True and env["lines"] == []
    raw = store._db.execute("SELECT reports FROM event_reports").fetchone()[0]
    assert MARKER not in raw


async def test_a_failed_attempt_stores_its_report(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([TelegramError(ErrorKind.AUTH, "401")])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert store.get_event(event_id).status == "failed"
    stored = store.get_event_report(event_id)
    assert stored is not None and [r["path"] for r in stored.reports] == ["a"]


async def test_a_retry_replaces_the_report(store: Store, tmp_project: Path) -> None:
    committed_repo(tmp_project, {"a": "1\n"})
    (tmp_project / "a").write_text("2\n")
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "500", retry_after=1), 5])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    first = store.get_event_report(event_id)
    assert first is not None and first.reports[0]["lines"] == ["-1", "+2"]
    (tmp_project / "a").write_text("3\n")
    await make_notifier(store, sender, tmp_project, now=2000.0).retry_due()
    assert store.get_event(event_id).status == "sent"
    rows = store._db.execute("SELECT COUNT(*) FROM event_reports").fetchone()[0]
    assert rows == 1
    second = store.get_event_report(event_id)
    assert second is not None and second.reports[0]["lines"] == ["-1", "+3"]


async def test_a_report_write_that_raises_never_stops_the_send(
    store: Store,
    tmp_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError(f"disk I/O error near {MARKER}")

    monkeypatch.setattr(store, "put_event_report", broken)
    sender = FakeSender([7])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert len(sender.sent) == 1
    assert store.get_event(event_id).status == "sent"
    assert "could not store the report" in caplog.text
    assert MARKER not in caplog.text  # never the exception text


async def test_the_plain_text_fallback_updates_the_excerpt(
    store: Store, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each "&" is five characters in HTML ("&amp;"), so the HTML attempt halves the
    # budget while the plain-text fallback fits all 20 lines.
    committed_repo(tmp_project, {"package.json": ""})
    (tmp_project / "package.json").write_text("".join("&" * 150 + "\n" for _ in range(20)))
    sender = FakeSender([TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"), 9])
    attempts: list[tuple[int, int]] = []
    real_put = store.put_event_report

    def spy(
        event_id: int,
        branch: str | None,
        reports: Sequence[FileReport],
        *,
        telegram_lines: int,
        telegram_files: int,
        rule_skipped: Collection[str] = (),
    ) -> None:
        attempts.append((telegram_lines, telegram_files))
        real_put(
            event_id,
            branch,
            reports,
            telegram_lines=telegram_lines,
            telegram_files=telegram_files,
            rule_skipped=rule_skipped,
        )

    monkeypatch.setattr(store, "put_event_report", spy)
    changes = journal(store, "package.json")
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", changes)
    assert event_id is not None
    assert store.get_event(event_id).status == "sent"
    assert attempts and attempts[0][0] < 20  # the HTML attempt was shrunk
    stored = store.get_event_report(event_id)
    assert stored is not None and (stored.telegram_lines, stored.telegram_files) == (20, 1)


async def test_a_failing_excerpt_update_keeps_the_event_sent(
    store: Store, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "set_report_telegram", broken)
    sender = FakeSender([TelegramError(ErrorKind.BAD_REQUEST, "bad"), 9])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert store.get_event(event_id).status == "sent"


async def test_the_fallbacks_own_composition_is_what_gets_stored(
    store: Store, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an HTML rejection the stored excerpt is the plain-text Composed's
    (lines, files), not the HTML attempt's."""
    real = render_mod.compose

    def compose(*args: Any, html: bool, **kwargs: Any) -> Composed:
        composed = real(*args, html=html, **kwargs)
        return composed if html else Composed(composed.text, 7, 3)

    monkeypatch.setattr("diffbell.notifier.compose", compose)
    sender = FakeSender([TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"), 9])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    assert store.get_event(event_id).status == "sent"
    stored = store.get_event_report(event_id)
    assert stored is not None and (stored.telegram_lines, stored.telegram_files) == (7, 3)


async def test_an_inactive_projects_event_never_reads_a_same_named_configured_folder(
    store: Store, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Journaled for folder A as "proj"; then A is removed and folder B is configured
    as "proj" (stable project ids plan, Task 2b B). A's event takes the path of a
    project no longer configured: B's folder is never diffed and B's snooze never
    holds it."""
    changes = journal(store, "package.json")  # folder /df-test/proj
    store.sync_projects([("proj", "/df-test/b")], 0.0)
    store.set_snooze("proj", 0.0, None)  # B's pause, not A's
    read: list[Path | None] = []

    def build_reports(path: Path | None, *args: Any) -> list[FileReport]:
        read.append(path)
        return diffing.build_reports(path, *args)

    def current_branch(path: Path | None) -> str | None:
        read.append(path)
        return diffing.current_branch(path)

    monkeypatch.setattr("diffbell.notifier.build_reports", build_reports)
    monkeypatch.setattr("diffbell.notifier.current_branch", current_branch)
    sender = FakeSender()
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", changes)
    assert event_id is not None
    assert read == [None, None]
    assert store.get_event(event_id).status == "sent"
    assert sender.sent[0][0].startswith("<b>🔑 proj</b>")


async def test_a_batch_of_two_folders_rows_under_one_name_makes_one_event_each(
    store: Store, tmp_project: Path
) -> None:
    """After a restart, the unflushed rows of folder A (no longer configured) and of
    folder B (configured under A's old name) come back under the same name, in one
    batch. They are never collapsed together or put in one event (Task 2b F)."""
    obs = Observation(changes=[("a", ChangeKind.MODIFIED)])
    store.record_observation("proj", STATE, obs, now=1.0)  # folder /df-test/proj
    store.sync_projects([("proj", "/df-test/b")], 0.0)
    store.record_observation("proj", STATE, obs, now=2.0)  # folder B, same file
    changes = store.unflushed_changes()
    sender = FakeSender()
    notifier = make_notifier(store, sender, tmp_project)
    first = await notifier.flush("proj", changes)
    assert store.unflushed_changes() == []
    events = store.recent_events()
    assert first == events[-1].id and len(events) == 2
    assert [[c.id for c in store.event_changes(e.id)] for e in reversed(events)] == [
        [changes[0].id],
        [changes[1].id],
    ]
    assert [e.project_active for e in reversed(events)] == [False, True]
    assert {e.status for e in events} == {"sent"}


async def test_a_send_raising_not_set_up_holds_the_event_for_setup(
    store: Store, tmp_project: Path
) -> None:
    """The belt (bot onboarding spec §1): a race with the slot never fails a change."""
    sender = FakeSender([NotSetUp()])
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    ev = store.get_event(event_id)
    assert (ev.status, ev.held_reason, ev.next_attempt_at, ev.attempts, ev.last_error) == (
        "held",
        "setup",
        HELD_FOREVER,
        0,
        None,
    )
    assert len(sender.sent) == 1  # no plain-text fallback
    report = store.get_event_report(event_id)
    assert report is not None and (report.telegram_lines, report.telegram_files) == (0, 0)


# -- the stored error kind and time (settings redesign spec §7) ----------------------


@pytest.mark.parametrize(
    ("outcomes", "status", "kind"),
    [
        ([TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")], "pending", "retryable"),
        ([TelegramError(ErrorKind.AUTH, "sendMessage: Unauthorized")], "failed", "auth"),
        (
            [
                TelegramError(ErrorKind.BAD_REQUEST, "bad"),
                TelegramError(ErrorKind.RETRYABLE, "500"),
            ],
            "failed",
            "bad_request",
        ),
        ([TelegramError(ErrorKind.BAD_REQUEST, "bad"), 9], "sent", None),
    ],
)
async def test_each_failed_send_stores_its_error_kind_and_time(
    store: Store,
    tmp_project: Path,
    outcomes: list[int | TelegramError],
    status: str,
    kind: str | None,
) -> None:
    sender = FakeSender(outcomes)
    event_id = await make_notifier(store, sender, tmp_project).flush("proj", journal(store, "a"))
    assert event_id is not None
    at = None if kind is None else 1000.0  # make_notifier's clock
    assert (store.get_event(event_id).status, error_kind(store, event_id)) == (status, kind)
    assert error_at(store, event_id) == at
