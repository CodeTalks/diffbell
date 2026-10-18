"""Catch-up messages: one per project when a pause ends (notify controls spec §4.3)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from diffbell import diffing
from diffbell import notifier as notifier_mod
from diffbell import render as render_mod
from diffbell.config import BUILTIN_REDACT, NOTIFY_KINDS, ResolvedProject
from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.notifier import Notifier, NotifierSettings, backoff
from diffbell.pausing import HELD_FOREVER, QuietHours
from diffbell.render import Composed
from diffbell.store import ClockState, Store
from diffbell.telegram import ErrorKind, NotSetUp, TelegramError
from tests.fakes import FakeSender
from tests.helpers import error_at, error_kind, git, init_repo, sync_names

STATE = ClockState("/w", "", "c:1", "h")
M, C, D = ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "proj", "other")
    return store


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


def journal(store: Store, project: str, *changes: tuple[str, ChangeKind]) -> list[Change]:
    return store.record_observation(project, STATE, Observation(changes=list(changes)), now=1.0)


class Setup:
    """A notifier for project "proj" (and "other") whose settings a test can change."""

    def __init__(self, store: Store, path: Path, clock: Clock) -> None:
        self.store = store
        self.sender = FakeSender()
        self.notify_on: tuple[str, ...] = NOTIFY_KINDS
        self.quiet: str | None = None
        self.web_port: int | None = 8765
        self.machine: str | None = None
        self.known = {"proj", "other"}
        # Configured but not resolved (a missing folder): name -> notify_on.
        self.configured: dict[str, tuple[str, ...]] = {}
        self.telegram_ok = True  # the SenderSlot's `configured`
        self.notifier = Notifier(
            store,
            self.sender,
            projects=lambda name: (
                ResolvedProject(name, path, ("*",), (), self.notify_on)
                if name in self.known
                else None
            ),
            settings=lambda: NotifierSettings(
                redact=BUILTIN_REDACT,
                max_lines=20,
                web_enabled=self.web_port is not None,
                web_port=self.web_port,
                quiet=QuietHours.parse(self.quiet) if self.quiet else None,
                machine=self.machine,
            ),
            clock=clock,
            configured=lambda name: self.configured.get(name),
            tz=ZoneInfo("UTC"),
            telegram_configured=lambda: self.telegram_ok,
        )

    async def held(self, project: str, *changes: tuple[str, ChangeKind]) -> int:
        """Flush a batch while the project is snoozed: the event is held."""
        event_id = await self.notifier.flush(project, journal(self.store, project, *changes))
        assert event_id is not None and self.store.get_event(event_id).status == "held"
        return event_id


@pytest.fixture
def setup(store: Store, tmp_project: Path, clock: Clock) -> Setup:
    return Setup(store, tmp_project, clock)


async def test_one_catch_up_per_project_with_the_net_effect_per_file(
    setup: Setup, store: Store, clock: Clock, tmp_project: Path, utc: None
) -> None:
    (tmp_project / "package.json").write_text("{}\n")
    store.set_snooze("proj", T0, None)
    store.set_snooze("other", T0, None)
    first = await setup.held("proj", ("package.json", M), ("new.json", C))
    clock.now += 120
    second = await setup.held("proj", ("new.json", D), ("old.json", D))
    other = await setup.held("other", ("x.json", M))
    clock.now += 60
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    assert len(setup.sender.sent) == 1  # one message for proj; other is still snoozed
    text, mode = setup.sender.sent[0]
    lines = text.splitlines()
    assert mode == "HTML"
    assert lines[:2] == [
        "<b>🔕 Catch-up · proj</b> · held 12:00–12:02 (snoozed)",
        "2 changes in 2 files",
    ]
    assert "🗑 <code>old.json</code>" in lines
    assert "new.json" not in text  # created, then deleted: dropped, as within one event
    assert lines[-1] == (
        f'🔗 <a href="http://127.0.0.1:8765/?event={second}">Open #{second} (+1 more)</a>'
    )
    rows = [store.get_event(i) for i in (first, second)]
    assert {(e.status, e.telegram_message_id, e.batch_id) for e in rows} == {
        ("sent", setup.sender._next_id, first)
    }
    assert store.get_event(other).status == "held"
    batch = store.get_batch_report(first)
    assert batch is not None and [r["path"] for r in batch.reports] == ["package.json", "old.json"]
    assert batch.telegram_files == 2
    own = store.get_event_report(first)  # each event keeps its hold-time diff
    assert own is not None and [r["path"] for r in own.reports] == ["package.json", "new.json"]


async def test_a_catch_up_names_the_mac_that_sent_it(
    setup: Setup, store: Store, clock: Clock, utc: None
) -> None:
    setup.machine = "Work MBP"
    store.set_snooze("proj", T0, None)
    first = await setup.held("proj", ("a.json", M))
    second = await setup.held("proj", ("b.json", M))
    clock.now += 60
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    lines = setup.sender.sent[0][0].splitlines()
    assert lines[0].startswith("<b>🔕 Catch-up · proj</b>")
    assert lines[0].endswith(" (snoozed) · 💻 Work MBP")
    assert lines[-1] == (
        f'🔗 <a href="http://127.0.0.1:8765/?event={second}">'
        f"Open #{second} (+1 more) on Work MBP</a>"
    )
    assert first < second


async def test_the_catch_up_diff_is_built_against_head_at_send_time(
    setup: Setup, store: Store, clock: Clock, tmp_project: Path
) -> None:
    init_repo(tmp_project)
    (tmp_project / "package.json").write_text('{"v": 1}\n')
    git(tmp_project, "add", ".")
    git(tmp_project, "commit", "-q", "-m", "init")
    store.set_snooze("proj", T0, None)
    (tmp_project / "package.json").write_text('{"v": 2}\n')
    await setup.held("proj", ("package.json", M))
    (tmp_project / "package.json").write_text('{"v": 3}\n')  # changed again, not flushed
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    text = setup.sender.sent[0][0]
    assert "+{&quot;v&quot;: 3}" in text and "&quot;v&quot;: 2" not in text


async def test_still_paused_at_catch_up_time_is_held_again(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, T0 + 60)
    event_id = await setup.held("proj", ("package.json", M))
    clock.now = datetime(2026, 10, 9, 23, 30, tzinfo=UTC).timestamp()
    setup.quiet = "23:00-08:00"  # the snooze is over, but quiet hours have begun
    await setup.notifier.catch_up_due()
    assert setup.sender.sent == []
    ev = store.get_event(event_id)
    end = datetime(2026, 10, 10, 8, 0, tzinfo=UTC).timestamp()
    assert (ev.status, ev.next_attempt_at, ev.held_reason) == ("held", end, "quiet")


async def test_a_retryable_error_keeps_the_group_held_with_backoff(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    ids = [await setup.held("proj", (name, M)) for name in ("a", "b")]
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")]
    await setup.notifier.catch_up_due()
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.attempts, e.next_attempt_at, e.last_error) for e in rows} == {
        ("held", 1, clock.now + backoff(0), "sendMessage: 502")
    }
    assert store.telegram_health()[1] == "sendMessage: 502"
    await setup.notifier.catch_up_due()  # not due yet
    assert len(setup.sender.sent) == 1
    clock.now += backoff(0)
    await setup.notifier.catch_up_due()
    assert len(setup.sender.sent) == 2
    assert {store.get_event(i).status for i in ids} == {"sent"}


async def test_a_failing_catch_up_held_again_drops_its_stale_error(
    setup: Setup, store: Store, clock: Clock
) -> None:
    """Snoozed again after a retryable failure: the group is held anew, so the old
    error must not keep the header saying Telegram is failing (final review 1)."""
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("a", M))
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")]
    await setup.notifier.catch_up_due()
    assert store.telegram_health()[1] == "sendMessage: 502"
    store.set_snooze("proj", clock.now, None)  # snoozed until turned back on
    clock.now += backoff(0)
    await setup.notifier.catch_up_due()
    ev = store.get_event(event_id)
    assert (ev.status, ev.next_attempt_at, ev.attempts, ev.last_error) == (
        "held",
        HELD_FOREVER,
        0,
        None,
    )
    assert store.telegram_health()[1] is None


async def test_a_permanent_error_fails_the_whole_group(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    ids = [await setup.held("proj", (name, M)) for name in ("a", "b")]
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [TelegramError(ErrorKind.AUTH, "sendMessage: Forbidden")]
    await setup.notifier.catch_up_due()
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.batch_id, e.last_error) for e in rows} == {
        ("failed", ids[0], "sendMessage: Forbidden")
    }
    # Retry re-queues the whole batch, which goes out again as one message.
    assert store.retry_event(ids[1], clock.now) is True
    await setup.notifier.catch_up_due()
    assert len(setup.sender.sent) == 2
    assert {(store.get_event(i).status, store.get_event(i).batch_id) for i in ids} == {
        ("sent", ids[0])
    }


async def test_a_rejected_catch_up_falls_back_to_plain_text(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("package.json", M))
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"), 9]
    await setup.notifier.catch_up_due()
    plain, mode = setup.sender.sent[1]
    assert mode is None and plain.startswith("🔕 Catch-up · proj · held")
    assert plain.endswith(f"http://127.0.0.1:8765/?event={event_id}")
    ev = store.get_event(event_id)
    assert (ev.status, ev.telegram_message_id, ev.batch_id) == ("sent", 9, event_id)
    assert ev.last_error == "plain-text fallback after: can't parse entities"


async def test_the_fallback_runs_while_the_group_is_still_held(
    setup: Setup, store: Store, clock: Clock
) -> None:
    """No `failed` window before the fallback (review fix 10): a web Retry in
    between finds nothing to retry, so the batch can't be sent twice."""
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("package.json", M))
    store.delete_snooze("proj", clock.now)
    seen: list[tuple[str, bool]] = []
    real_send = setup.sender.send

    async def send(text: str, *, parse_mode: str | None = "HTML", silent: bool = False) -> int:
        if parse_mode is None:  # the fallback: what a web Retry would see now
            seen.append((store.get_event(event_id).status, store.retry_event(event_id, clock.now)))
        return await real_send(text, parse_mode=parse_mode, silent=silent)

    setup.sender.send = send  # type: ignore[method-assign]
    setup.sender.outcomes = [TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities")]
    await setup.notifier.catch_up_due()
    assert seen == [("held", False)]
    assert store.get_event(event_id).status == "sent"
    await setup.notifier.catch_up_due()
    assert len(setup.sender.sent) == 2  # the HTML attempt and the fallback, nothing more


async def test_a_failed_fallback_fails_the_group_with_the_first_error(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    ids = [await setup.held("proj", (name, M)) for name in ("a", "b")]
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [
        TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"),
        TelegramError(ErrorKind.BAD_REQUEST, "message is too long"),
    ]
    await setup.notifier.catch_up_due()
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.batch_id, e.last_error) for e in rows} == {
        ("failed", ids[0], "can't parse entities")
    }


async def test_rules_changed_while_held_can_skip_the_whole_group(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("package.json", M))
    setup.notify_on = ("deleted",)
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    assert setup.sender.sent == []
    assert store.get_event(event_id).status == "skipped"


async def test_created_then_deleted_while_held_is_dropped(
    setup: Setup, store: Store, clock: Clock
) -> None:
    """A temp file created and deleted across held events never existed outside the
    hold: dropped, as models.collapse drops it within one event. A `deleted` rule then
    has nothing left, so the group is skipped."""
    store.set_snooze("proj", T0, None)
    first = await setup.held("proj", ("tmp.swp", C))
    clock.now += 60
    second = await setup.held("proj", ("tmp.swp", D))
    setup.notify_on = ("deleted",)  # in force at catch-up time
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    assert setup.sender.sent == []
    assert {store.get_event(i).status for i in (first, second)} == {"skipped"}


async def test_a_removed_project_still_catches_up(
    setup: Setup, store: Store, clock: Clock, utc: None
) -> None:
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("package.json", M))
    setup.known = {"other"}  # removed from the config
    store.drop_snoozes_except(["other"], clock.now)
    await setup.notifier.catch_up_due()
    text = setup.sender.sent[0][0]
    assert text.startswith("<b>🔕 Catch-up · proj</b> (removed) · held 12:00–12:00 (snoozed)")
    assert "✏️ <code>package.json</code>" in text  # names only: no path to diff in
    assert store.get_event(event_id).status == "sent"


async def test_quiet_hours_catch_up_says_so(setup: Setup, store: Store, clock: Clock) -> None:
    clock.now = datetime(2026, 10, 9, 23, 30, tzinfo=UTC).timestamp()
    setup.quiet = "23:00-08:00"
    event_id = await setup.held("proj", ("package.json", M))
    assert store.get_event(event_id).held_reason == "quiet"
    clock.now = datetime(2026, 10, 10, 8, 0, tzinfo=UTC).timestamp()
    await setup.notifier.catch_up_due()
    assert "(quiet hours)" in setup.sender.sent[0][0].splitlines()[0]


async def test_an_unexpected_error_backs_off(
    setup: Setup, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("package.json", M))
    store.delete_snooze("proj", clock.now)

    def broken(*args: object) -> list[object]:
        raise OSError("git exploded")

    monkeypatch.setattr(notifier_mod, "build_reports", broken)
    await setup.notifier.catch_up_due()  # must not raise
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.next_attempt_at) == ("held", 1, clock.now + backoff(0))
    assert ev.last_error == "unexpected error: OSError: git exploded"


async def test_an_error_in_the_pause_check_backs_off(
    setup: Setup, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snooze lookup is inside the error handling (review fix 11): a failure
    there backs the group off instead of retrying every maintenance tick."""
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("package.json", M))
    store.delete_snooze("proj", clock.now)

    def broken(project: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "get_snooze", broken)
    await setup.notifier.catch_up_due()  # must not raise
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.next_attempt_at) == ("held", 1, clock.now + backoff(0))
    assert ev.last_error == "unexpected error: OperationalError: disk I/O error"
    assert setup.sender.sent == []


async def test_created_then_modified_reads_created(
    setup: Setup, store: Store, clock: Clock, tmp_project: Path
) -> None:
    """Merged by net effect (review fix 9): a file created then modified while held
    is new, so a `created` rule still lets it through."""
    (tmp_project / "new.txt").write_text("x\n")
    setup.notify_on = ("created", "deleted")
    store.set_snooze("proj", T0, None)
    await setup.held("proj", ("new.txt", C))
    clock.now += 60
    await setup.held("proj", ("new.txt", M), ("gone.txt", D))
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    text = setup.sender.sent[0][0]
    assert "➕ <code>new.txt</code>" in text
    assert "🗑 <code>gone.txt</code>" in text.splitlines()
    assert text.splitlines()[1].startswith("3 changes in 2 files")


async def test_deleted_then_created_reads_modified(
    setup: Setup, store: Store, clock: Clock, tmp_project: Path
) -> None:
    """The file existed before the hold and exists after it: modified."""
    (tmp_project / "f.txt").write_text("x\n")
    store.set_snooze("proj", T0, None)
    await setup.held("proj", ("f.txt", D))
    clock.now += 60
    await setup.held("proj", ("f.txt", C))
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    text = setup.sender.sent[0][0]
    assert "✏️ <code>f.txt</code>" in text and "➕" not in text and "🗑" not in text


async def test_a_project_whose_folder_is_missing_is_not_removed(
    setup: Setup, store: Store, clock: Clock, utc: None
) -> None:
    """Configured, but its folder is missing (review fix 12): it keeps its rules
    and its catch-up is not labelled "(removed)"."""
    store.set_snooze("proj", T0, None)
    kept = await setup.held("proj", ("package.json", M))
    setup.known = {"other"}
    setup.configured = {"proj": NOTIFY_KINDS}
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    text = setup.sender.sent[0][0]
    assert text.startswith("<b>🔕 Catch-up · proj</b> · held")
    assert store.get_event(kept).status == "sent"
    store.set_snooze("proj", clock.now, None)
    ruled = await setup.held("proj", ("package.json", M))
    setup.configured = {"proj": ("deleted",)}
    store.delete_snooze("proj", clock.now)
    await setup.notifier.catch_up_due()
    assert len(setup.sender.sent) == 1
    assert store.get_event(ruled).status == "skipped"


async def test_a_catch_up_fallbacks_own_composition_is_what_gets_stored(
    setup: Setup, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = render_mod.compose

    def compose(*args: Any, html: bool, **kwargs: Any) -> Composed:
        composed = real(*args, html=html, **kwargs)
        return composed if html else Composed(composed.text, 7, 3)

    monkeypatch.setattr("diffbell.notifier.compose", compose)
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("package.json", M))
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"), 9]
    await setup.notifier.catch_up_due()
    assert store.get_event(event_id).status == "sent"
    stored = store.get_batch_report(event_id)
    assert stored is not None and (stored.telegram_lines, stored.telegram_files) == (7, 3)


async def test_an_inactive_projects_catch_up_is_its_own_and_never_reads_the_new_folder(
    setup: Setup, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch, utc: None
) -> None:
    """Folder A ("proj") has a held event; A is removed and folder B is configured as
    "proj", snoozed, with a held event of its own (stable project ids plan, Task 2b
    B). A's group catches up as removed, without B's snooze or folder; B's waits."""
    store.set_snooze("proj", T0, None)
    old = await setup.held("proj", ("package.json", M))
    store.sync_projects([("proj", "/df-test/b"), ("other", "/df-test/other")], clock.now)
    store.drop_snoozes_except(["proj", "other"], clock.now)  # A's snooze: A's events due
    store.set_snooze("proj", T0, None)
    new = await setup.held("proj", ("b.json", M))
    read: list[Path | None] = []

    def build_reports(path: Path | None, *args: Any) -> Any:
        read.append(path)
        return diffing.build_reports(path, *args)

    monkeypatch.setattr("diffbell.notifier.build_reports", build_reports)
    await setup.notifier.catch_up_due()
    assert read == [None]
    ((text, _),) = setup.sender.sent
    assert text.startswith("<b>🔕 Catch-up · proj</b> (removed)")
    assert "package.json" in text and "b.json" not in text
    assert store.get_event(old).status == "sent"
    assert store.get_event(new).status == "held"


# -- held while Telegram is not set up (bot onboarding spec §1) ---------------------


async def test_not_set_up_holds_and_catches_up_once_per_project_after_save(
    setup: Setup, store: Store, clock: Clock, utc: None
) -> None:
    setup.telegram_ok = False
    first = await setup.held("proj", ("a", M))
    clock.now += 120
    second = await setup.held("proj", ("b", M))
    other = await setup.held("other", ("c", M))
    rows = [store.get_event(i) for i in (first, second, other)]
    assert {(e.held_reason, e.next_attempt_at) for e in rows} == {("setup", HELD_FOREVER)}
    await setup.notifier.catch_up_due()
    assert setup.sender.sent == []
    setup.telegram_ok = True  # saved: the daemon releases the setup holds
    assert store.release_held(clock.now, reason="setup") == 3
    await setup.notifier.catch_up_due()
    heads = sorted(text.splitlines()[0] for text, _ in setup.sender.sent)
    assert heads == [
        "<b>🔕 Catch-up · other</b> · held until Telegram was set up (12:02–12:02)",
        "<b>🔕 Catch-up · proj</b> · held until Telegram was set up (12:00–12:02)",
    ]
    assert {store.get_event(i).status for i in (first, second, other)} == {"sent"}


async def test_setup_wins_over_a_snooze_in_a_mixed_group(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    snoozed = await setup.held("proj", ("a", M))
    store.delete_snooze("proj", clock.now)  # due now, still held as "snooze"
    setup.telegram_ok = False
    clock.now += 60
    await setup.held("proj", ("b", M))
    assert store.get_event(snoozed).held_reason == "snooze"
    setup.telegram_ok = True
    store.release_held(clock.now, reason="setup")
    await setup.notifier.catch_up_due()
    [(text, _)] = setup.sender.sent
    assert "held until Telegram was set up" in text.splitlines()[0]


async def test_a_snoozed_project_is_held_again_as_snoozed_after_setup(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, T0 + 3600)
    setup.telegram_ok = False
    event_id = await setup.held("proj", ("a", M))
    assert store.get_event(event_id).held_reason == "setup"
    setup.telegram_ok = True
    store.release_held(clock.now, reason="setup")
    await setup.notifier.catch_up_due()
    assert setup.sender.sent == []
    ev = store.get_event(event_id)
    assert (ev.status, ev.held_reason, ev.next_attempt_at) == ("held", "snooze", T0 + 3600)
    clock.now = T0 + 3600
    await setup.notifier.catch_up_due()
    [(text, _)] = setup.sender.sent
    assert "(snoozed)" in text.splitlines()[0]


async def test_rules_still_skip_first_while_not_set_up(setup: Setup, store: Store) -> None:
    setup.telegram_ok = False
    setup.notify_on = ("created",)
    event_id = await setup.notifier.flush("proj", journal(store, "proj", ("a", M)))
    assert event_id is not None and store.get_event(event_id).status == "skipped"


async def test_a_catch_up_raising_not_set_up_is_held_for_setup(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    ids = [await setup.held("proj", (name, M)) for name in ("a", "b")]
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [NotSetUp()]
    await setup.notifier.catch_up_due()
    rows = [store.get_event(i) for i in ids]
    assert {(e.status, e.held_reason, e.next_attempt_at, e.attempts, e.batch_id) for e in rows} == {
        ("held", "setup", HELD_FOREVER, 0, None)
    }
    assert len(setup.sender.sent) == 1  # no plain-text fallback
    report = store.get_batch_report(ids[0])
    assert report is not None and (report.telegram_lines, report.telegram_files) == (0, 0)


# -- the stored error kind and time (settings redesign spec §7) ----------------------


@pytest.mark.parametrize(
    ("outcomes", "status", "kind"),
    [
        ([TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")], "held", "retryable"),
        ([TelegramError(ErrorKind.AUTH, "sendMessage: Forbidden")], "failed", "auth"),
        (
            [
                TelegramError(ErrorKind.BAD_REQUEST, "bad"),
                TelegramError(ErrorKind.RETRYABLE, "500"),
            ],
            "failed",
            "bad_request",
        ),
        ([TelegramError(ErrorKind.BAD_REQUEST, "bad"), 77], "sent", None),
    ],
)
async def test_a_catch_up_stores_its_error_kind_and_time_on_every_event(
    setup: Setup,
    store: Store,
    clock: Clock,
    outcomes: list[int | TelegramError],
    status: str,
    kind: str | None,
) -> None:
    store.set_snooze("proj", T0, None)
    ids = [await setup.held("proj", (name, M)) for name in ("a", "b")]
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = list(outcomes)
    await setup.notifier.catch_up_due()
    at = None if kind is None else clock.now
    assert {(store.get_event(i).status, error_kind(store, i), error_at(store, i)) for i in ids} == {
        (status, kind, at)
    }


async def test_an_unexpected_catch_up_error_is_stored_as_retryable(
    setup: Setup, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("a", M))
    store.delete_snooze("proj", clock.now)

    async def broken(text: str, *, parse_mode: str | None = "HTML", silent: bool = False) -> int:
        raise ValueError("chat_id is required to send messages")

    monkeypatch.setattr(setup.sender, "send", broken)
    await setup.notifier.catch_up_due()
    ev = store.get_event(event_id)
    assert ev.status == "held" and (ev.last_error or "").startswith("unexpected error: ValueError")
    assert (error_kind(store, event_id), error_at(store, event_id)) == ("retryable", clock.now)


async def test_a_catch_up_held_again_clears_kind_and_time(
    setup: Setup, store: Store, clock: Clock
) -> None:
    store.set_snooze("proj", T0, None)
    event_id = await setup.held("proj", ("a", M))
    store.delete_snooze("proj", clock.now)
    setup.sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")]
    await setup.notifier.catch_up_due()
    assert error_kind(store, event_id) == "retryable"
    store.set_snooze("proj", clock.now, None)
    clock.now += backoff(0)
    await setup.notifier.catch_up_due()
    assert (error_kind(store, event_id), error_at(store, event_id)) == (None, None)
