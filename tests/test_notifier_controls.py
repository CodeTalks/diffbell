"""Delivery with notification controls: notify_on rules, then the pause (held),
then the send with its deep link (notify controls spec §4.2, §4.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from diffbell.config import BUILTIN_REDACT, NOTIFY_KINDS, ResolvedProject
from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.notifier import Notifier, NotifierSettings
from diffbell.pausing import HELD_FOREVER, QuietHours
from diffbell.store import ClockState, Store
from diffbell.telegram import ErrorKind, TelegramError
from tests.fakes import FakeSender
from tests.helpers import git, init_repo, sync_names

STATE = ClockState("/w", "", "c:1", "h")
M, C, D = ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC).timestamp()
# The whole message the notifier sends for the event in
# test_the_whole_message_with_the_web_ui_disabled: layout A2 (health spec §3). It
# replaces the sha256 pin of the layout before 5a, which changed on purpose.
GOLDEN_MESSAGE = "\n".join(
    [
        "<b>🔑 proj</b> · <code>main</code> · 12:00",
        "2 files · +1 −1",
        "✏️ <code>package.json</code> +1 −1",
        "🔒 <code>.env.local</code> · contents hidden",
        "<blockquote expandable>package.json",
        "<code>   -  &quot;version&quot;: &quot;1.0.0&quot;",
        "   +  &quot;version&quot;: &quot;1.0.1&quot;</code></blockquote>",
    ]
)


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "proj")
    return store


def journal(store: Store, *changes: tuple[str, ChangeKind]) -> list[Change]:
    return store.record_observation("proj", STATE, Observation(changes=list(changes)), now=1.0)


def make_notifier(
    store: Store,
    sender: FakeSender,
    path: Path,
    *,
    notify_on: tuple[str, ...] = NOTIFY_KINDS,
    quiet: str | None = None,
    web_port: int | None = None,
    now: float = NOW,
    machine: str | None = None,
) -> Notifier:
    project = ResolvedProject("proj", path, ("*",), (), notify_on)
    settings = NotifierSettings(
        redact=BUILTIN_REDACT,
        max_lines=20,
        web_enabled=web_port is not None,
        web_port=web_port,
        quiet=QuietHours.parse(quiet) if quiet else None,
        machine=machine,
    )
    return Notifier(
        store,
        sender,
        projects=lambda name: project if name == "proj" else None,
        settings=lambda: settings,
        clock=lambda: now,
        tz=ZoneInfo("UTC"),
    )


# -- rules ---------------------------------------------------------------------------


async def test_rules_that_leave_nothing_skip_the_event(store: Store, tmp_project: Path) -> None:
    sender = FakeSender()
    notifier = make_notifier(store, sender, tmp_project, notify_on=("deleted",))
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    assert event_id is not None
    assert sender.sent == []
    assert store.get_event(event_id).status == "skipped"
    stored = store.get_event_report(event_id)
    assert stored is not None and (stored.telegram_lines, stored.telegram_files) == (0, 0)
    assert [e.get("rule_skipped") for e in stored.reports] == [True]


async def test_a_mixed_event_sends_only_the_kinds_it_notifies_about(
    store: Store, tmp_project: Path
) -> None:
    (tmp_project / "new.json").write_text("{}\n")
    sender = FakeSender([7])
    notifier = make_notifier(store, sender, tmp_project, notify_on=("created",))
    changes = journal(store, ("package.json", M), ("new.json", C))
    event_id = await notifier.flush("proj", changes)
    assert event_id is not None
    (text, _) = sender.sent[0]
    assert text.splitlines()[1] == "1 file" and "new.json" in text and "package.json" not in text
    assert store.get_event(event_id).status == "sent"
    stored = store.get_event_report(event_id)
    assert stored is not None
    # The ruled-out file is kept, flagged and listed after the sent ones.
    assert [(e["path"], e.get("rule_skipped")) for e in stored.reports] == [
        ("new.json", None),
        ("package.json", True),
    ]
    assert stored.telegram_files == 1


async def test_a_notify_on_change_applies_at_retry_time(store: Store, tmp_project: Path) -> None:
    """The rules are read at each delivery attempt (review fix 4): an event whose
    first send failed retryably is skipped by the rules in force at the retry, and a
    skipped event carries no error (review fix 3)."""
    rules: list[tuple[str, ...]] = [NOTIFY_KINDS]
    clock = [NOW]
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")])
    notifier = Notifier(
        store,
        sender,
        projects=lambda name: ResolvedProject(name, tmp_project, ("*",), (), rules[0]),
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=lambda: clock[0],
        tz=ZoneInfo("UTC"),
    )
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    assert event_id is not None
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.last_error) == ("pending", 1, "sendMessage: 502")
    rules[0] = ("deleted",)
    clock[0] = ev.next_attempt_at
    await notifier.retry_due()
    assert len(sender.sent) == 1  # not sent again
    ev = store.get_event(event_id)
    assert (ev.status, ev.attempts, ev.last_error) == ("skipped", 0, None)
    stored = store.get_event_report(event_id)
    assert stored is not None and [e.get("rule_skipped") for e in stored.reports] == [True]


async def test_a_project_whose_folder_is_missing_keeps_its_rules(
    store: Store, tmp_project: Path
) -> None:
    """Not resolved (its folder is missing) but still configured (review fix 12):
    its own notify_on applies, not the all-kinds default of a removed project."""
    sender = FakeSender()
    notifier = Notifier(
        store,
        sender,
        projects=lambda name: None,
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=lambda: NOW,
        configured=lambda name: ("deleted",) if name == "proj" else None,
        tz=ZoneInfo("UTC"),
    )
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    assert event_id is not None and store.get_event(event_id).status == "skipped"
    assert sender.sent == []
    gone = await notifier.flush("proj", journal(store, ("old.json", D)))
    assert gone is not None and store.get_event(gone).status == "sent"


# -- the pause -------------------------------------------------------------------------


async def test_a_snoozed_project_holds_the_event(store: Store, tmp_project: Path) -> None:
    store.set_snooze("proj", NOW - 60, NOW + 3600)
    sender = FakeSender()
    event_id = await make_notifier(store, sender, tmp_project).flush(
        "proj", journal(store, ("package.json", M))
    )
    assert event_id is not None
    assert sender.sent == []
    ev = store.get_event(event_id)
    assert (ev.status, ev.next_attempt_at, ev.held_reason) == ("held", NOW + 3600, "snooze")
    stored = store.get_event_report(event_id)  # the hold-time diff, never sent
    assert stored is not None and (stored.telegram_lines, stored.telegram_files) == (0, 0)


async def test_an_indefinite_snooze_holds_forever(store: Store, tmp_project: Path) -> None:
    store.set_snooze("proj", NOW - 60, None)
    event_id = await make_notifier(store, FakeSender(), tmp_project).flush(
        "proj", journal(store, ("package.json", M))
    )
    assert event_id is not None
    assert store.get_event(event_id).next_attempt_at == HELD_FOREVER


async def test_quiet_hours_hold_the_event_until_they_end(store: Store, tmp_project: Path) -> None:
    late = datetime(2026, 10, 9, 23, 30, tzinfo=UTC).timestamp()
    sender = FakeSender()
    notifier = make_notifier(store, sender, tmp_project, quiet="23:00-08:00", now=late)
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    assert event_id is not None
    ev = store.get_event(event_id)
    end = datetime(2026, 10, 10, 8, 0, tzinfo=UTC).timestamp()
    assert (ev.status, ev.next_attempt_at, ev.held_reason) == ("held", end, "quiet")
    assert sender.sent == []


async def test_outside_quiet_hours_the_event_is_sent(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([5])
    notifier = make_notifier(store, sender, tmp_project, quiet="23:00-08:00")
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    assert event_id is not None and store.get_event(event_id).status == "sent"


async def test_rules_come_before_the_pause(store: Store, tmp_project: Path) -> None:
    """A snoozed project's event that no rule lets through is skipped, never held
    (so no catch-up message could ever be empty)."""
    store.set_snooze("proj", NOW - 60, None)
    notifier = make_notifier(store, FakeSender(), tmp_project, notify_on=("created",))
    event_id = await notifier.flush("proj", journal(store, ("package.json", D)))
    assert event_id is not None
    assert store.get_event(event_id).status == "skipped"


async def test_an_expired_snooze_does_not_hold(store: Store, tmp_project: Path) -> None:
    store.set_snooze("proj", NOW - 7200, NOW - 3600)
    sender = FakeSender([5])
    event_id = await make_notifier(store, sender, tmp_project).flush(
        "proj", journal(store, ("package.json", M))
    )
    assert event_id is not None and store.get_event(event_id).status == "sent"


# -- the deep link ------------------------------------------------------------------------


async def test_the_message_links_to_its_event_while_the_web_ui_is_on(
    store: Store, tmp_project: Path
) -> None:
    sender = FakeSender([5])
    notifier = make_notifier(store, sender, tmp_project, web_port=8765)
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    assert sender.sent[0][0].splitlines()[-1] == (
        f'🔗 <a href="http://127.0.0.1:8765/?event={event_id}">Open #{event_id}</a>'
    )


async def test_the_plain_text_fallback_carries_the_url(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"), 9])
    notifier = make_notifier(store, sender, tmp_project, web_port=8765)
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    plain, mode = sender.sent[1]
    assert mode is None
    assert plain.splitlines()[-1] == f"🔗 Open #{event_id}: http://127.0.0.1:8765/?event={event_id}"


async def test_the_message_names_the_mac_that_sent_it(store: Store, tmp_project: Path) -> None:
    """ADR 0023: the first line ends with the Mac's name and the link opens that Mac."""
    sender = FakeSender([5])
    notifier = make_notifier(store, sender, tmp_project, web_port=8765, machine="Work MBP")
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    lines = sender.sent[0][0].splitlines()
    assert lines[0].endswith(" · 💻 Work MBP")
    assert lines[-1] == (
        f'🔗 <a href="http://127.0.0.1:8765/?event={event_id}">Open #{event_id} on Work MBP</a>'
    )


async def test_the_plain_text_fallback_names_the_mac_too(store: Store, tmp_project: Path) -> None:
    sender = FakeSender([TelegramError(ErrorKind.BAD_REQUEST, "can't parse entities"), 9])
    notifier = make_notifier(store, sender, tmp_project, web_port=8765, machine="Work MBP")
    event_id = await notifier.flush("proj", journal(store, ("package.json", M)))
    plain, mode = sender.sent[1]
    assert mode is None and plain.splitlines()[0].endswith(" · 💻 Work MBP")
    assert plain.splitlines()[-1] == (
        f"🔗 Open #{event_id} on Work MBP: http://127.0.0.1:8765/?event={event_id}"
    )


def test_notifier_settings_have_no_mac_by_default() -> None:
    assert NotifierSettings(redact=(), max_lines=20, web_enabled=False).machine is None


async def test_the_whole_message_with_the_web_ui_disabled(
    store: Store, tmp_project: Path, utc: None
) -> None:
    init_repo(tmp_project)
    (tmp_project / "package.json").write_text('{\n  "version": "1.0.0"\n}\n')
    git(tmp_project, "add", ".")
    git(tmp_project, "commit", "-q", "-m", "init")
    (tmp_project / "package.json").write_text('{\n  "version": "1.0.1"\n}\n')
    (tmp_project / ".env.local").write_text("API_TOKEN=x\n")
    sender = FakeSender([7])
    notifier = make_notifier(store, sender, tmp_project)
    await notifier.flush("proj", journal(store, ("package.json", M), (".env.local", M)))
    (text, _) = sender.sent[0]
    assert text == GOLDEN_MESSAGE


async def test_the_report_is_stored_in_the_messages_order(store: Store, tmp_project: Path) -> None:
    """Modified, created, deleted, then redacted (health spec §3.1), so the stored
    report's telegram_files counts the files the message listed from the top."""
    (tmp_project / "new.json").write_text("{}\n")
    (tmp_project / "a.json").write_text("{}\n")
    sender = FakeSender([7])
    notifier = make_notifier(store, sender, tmp_project)
    changes = journal(store, ("gone.json", D), (".env", M), ("new.json", C), ("a.json", M))
    event_id = await notifier.flush("proj", changes)
    assert event_id is not None
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert [r["path"] for r in stored.reports] == ["a.json", "new.json", "gone.json", ".env"]
    assert stored.telegram_files == 4
