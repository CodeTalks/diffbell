"""The health registry (health spec §4.3, §8 "Health" and "Health delivery")."""

from __future__ import annotations

import ast
import dataclasses
import logging
import math
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import diffbell
from diffbell.delivery import backoff
from diffbell.health import SETUP_KEY, Health, Problem
from diffbell.store import Store
from diffbell.telegram import ErrorKind, TelegramError
from tests.fakes import FakeSender

UTC = ZoneInfo("UTC")
T0 = 1_791_540_000.0  # 2026-10-09 10:00:00 UTC
WATCHMAN = "Watchman is unreachable; key files are not being watched"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    return Store(tmp_dir / "state.db")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def health(store: Store, sender: FakeSender, clock: Clock) -> Health:
    return Health(store, sender, clock=clock, tz=UTC)


def texts(sender: FakeSender) -> list[str]:
    return [text for text, _ in sender.sent]


async def tick(health: Health, clock: Clock, minutes: float = 0) -> None:
    clock.now += minutes * 60
    await health.tick(clock.now)


# -- open and resolve ---------------------------------------------------------------


async def test_a_serious_problem_is_sent_once_as_plain_text(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("project:p", "serious", "project p: watch lost", project="p")
    await tick(health, clock)
    await tick(health, clock, 60)
    await tick(health, clock, 600)
    assert sender.sent == [("⚠️ Diffbell: project p: watch lost", None)]


async def test_the_resolution_is_sent_once_with_the_down_times(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    health.open("project:p", "serious", "project p: watch lost", project="p")
    await tick(health, clock)
    clock.now += 20 * 60
    health.resolve("project:p")
    await tick(health, clock)
    await tick(health, clock, 5)
    assert texts(sender)[1:] == ["✅ Diffbell: project p: watching again (down 10:00–10:20)"]
    assert store.all_problems() == []
    assert health.problems() == []


@pytest.mark.parametrize(
    ("key", "words"),
    [
        ("watchman", "Watchman is reachable again"),
        ("config-watch", "config edits are picked up again"),
        ("config", "the config is valid again"),
        ("web", "the web UI is running again"),
        ("other", "resolved: it broke"),
    ],
)
async def test_resolution_words_by_key(
    health: Health, sender: FakeSender, clock: Clock, key: str, words: str
) -> None:
    health.open(key, "serious", "it broke")
    await tick(health, clock)
    health.resolve(key)
    await tick(health, clock)
    assert texts(sender) == [
        "⚠️ Diffbell: it broke",
        f"✅ Diffbell: {words} (down 10:00–10:00)",
    ]


async def test_an_open_problem_keeps_its_start_and_takes_a_new_message(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("config", "serious", "config rejected: a")
    await tick(health, clock)
    clock.now += 60
    health.open("config", "serious", "config rejected: b", fix="fix it")
    await tick(health, clock)
    [problem] = health.problems()
    assert (problem.message, problem.fix, problem.opened_at) == ("config rejected: b", "fix it", T0)
    assert texts(sender) == ["⚠️ Diffbell: config rejected: a"]  # no second "opened"


async def test_a_long_message_is_clipped_so_the_alert_always_fits(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("config", "serious", "config rejected: " + "x" * 5000)
    await tick(health, clock)
    [(text, _)] = sender.sent
    assert len(health.problems()[0].message) == 1000 and text.endswith("x…")


async def test_resolving_an_unknown_key_does_nothing(health: Health, store: Store) -> None:
    health.resolve("nothing")
    assert store.all_problems() == []


# -- grace and flapping --------------------------------------------------------------


async def test_the_open_message_waits_for_the_grace_period(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("watchman", "serious", WATCHMAN, grace=300)
    clock.now += 299
    await health.tick(clock.now)
    assert sender.sent == []
    assert [p.key for p in health.problems()] == ["watchman"]  # the page shows it at once
    clock.now += 1
    await health.tick(clock.now)
    assert texts(sender) == [f"⚠️ Diffbell: {WATCHMAN}"]


async def test_a_problem_resolved_within_its_grace_is_deleted_silently(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    health.open("watchman", "serious", WATCHMAN, grace=300)
    await tick(health, clock, 2)
    health.resolve("watchman")
    await tick(health, clock, 10)
    assert sender.sent == []
    assert store.all_problems() == []


async def test_flapping_inside_the_grace_never_sends(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    for _ in range(5):
        health.open("watchman", "serious", WATCHMAN, grace=300)
        await tick(health, clock, 2)
        health.resolve("watchman")
        await tick(health, clock, 2)
    assert sender.sent == []


async def test_past_the_grace_the_resolution_is_sent_even_before_any_tick(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("watchman", "serious", WATCHMAN, grace=300)
    clock.now += 400
    health.resolve("watchman")
    await health.tick(clock.now)
    assert texts(sender) == [
        f"✅ Diffbell: Watchman is reachable again (was down 10:00–10:06: {WATCHMAN})"
    ]


# -- warnings and notices ---------------------------------------------------------------


async def test_a_warning_is_page_only(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    health.open("recrawl:p", "warning", "project p: recrawled", project="p", fix="watchman …")
    await tick(health, clock, 60)
    assert [(p.key, p.severity, p.fix) for p in health.problems()] == [
        ("recrawl:p", "warning", "watchman …")
    ]
    health.resolve("recrawl:p")
    await tick(health, clock, 60)
    assert sender.sent == []
    assert store.all_problems() == []


async def test_a_notice_is_sent_once_verbatim_and_never_listed(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    text = "⚠️ Diffbell restarted after an unexpected stop (down 09:58–10:00)"
    health.notice("restart", text)
    assert health.problems() == []
    health.resolve("restart")  # a notice can't be resolved away
    await tick(health, clock)
    await tick(health, clock, 60)
    assert sender.sent == [(text, None)]
    assert store.all_problems() == []


async def test_problems_lists_open_ones_oldest_first(health: Health, clock: Clock) -> None:
    health.open("b", "warning", "b")
    clock.now += 1
    health.open("a", "serious", "a")
    health.notice("restart", "restarted")
    clock.now += 1
    health.open("c", "serious", "c")
    await health.tick(clock.now)
    health.resolve("c")
    assert [p.key for p in health.problems()] == ["b", "a"]


async def test_forget_and_forget_projects_drop_rows_silently(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("project:old", "serious", "old", project="old")
    health.open("recrawl:old", "warning", "old", project="old")
    health.open("project:kept", "serious", "kept", project="kept")
    health.open("watchman", "serious", WATCHMAN, grace=300)
    await tick(health, clock)
    health.forget_projects_except(["kept"])
    health.forget("watchman")
    await tick(health, clock, 10)
    assert [p.key for p in health.problems()] == ["project:kept"]
    # Same start: by key. Nothing more after the forget (no "resolved" message).
    assert texts(sender) == ["⚠️ Diffbell: kept", "⚠️ Diffbell: old"]


# -- persistence -------------------------------------------------------------------------


async def test_a_restart_keeps_open_problems_and_does_not_send_opened_again(
    store: Store, sender: FakeSender, clock: Clock
) -> None:
    first = Health(store, sender, clock=clock, tz=UTC)
    first.open("project:p", "serious", "project p: watch lost", project="p")
    await first.tick(clock.now)
    again = Health(store, sender, clock=clock, tz=UTC)  # the daemon restarted
    clock.now += 600
    again.open("project:p", "serious", "project p: watch lost", project="p")
    await again.tick(clock.now)
    assert len(sender.sent) == 1
    again.resolve("project:p")
    await again.tick(clock.now)
    assert texts(sender)[-1] == "✅ Diffbell: project p: watching again (down 10:00–10:10)"


async def test_the_backoff_is_persisted_across_a_restart(
    store: Store, sender: FakeSender, clock: Clock
) -> None:
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")]
    first = Health(store, sender, clock=clock, tz=UTC)
    first.open("web", "serious", "web UI stopped")
    await first.tick(clock.now)
    again = Health(store, sender, clock=clock, tz=UTC)
    await again.tick(clock.now + 29)
    assert len(sender.sent) == 1
    await again.tick(clock.now + 30)
    assert len(sender.sent) == 2


# -- Telegram down, retries ------------------------------------------------------------------


async def test_telegram_down_then_resolved_sends_only_the_resolution(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: network error")]
    health.open("project:p", "serious", "project p: watch lost", project="p")
    await health.tick(clock.now)  # fails: the "opened" message stays pending
    [pending] = health.problems()
    assert (pending.notified_open, pending.attempts, pending.next_try_at) == (False, 1, T0 + 30)
    assert pending.last_error == "sendMessage: network error"
    clock.now += 20
    health.resolve("project:p")
    await health.tick(clock.now)  # still backing off: no call
    assert len(sender.sent) == 1
    clock.now += 10
    await health.tick(clock.now)
    await health.tick(clock.now + 600)
    assert texts(sender) == [
        "⚠️ Diffbell: project p: watch lost",  # the failed attempt
        "✅ Diffbell: project p: watching again (was down 10:00–10:00: project p: watch lost)",
    ]


async def test_a_429_waits_its_retry_after(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 429", retry_after=120)]
    health.open("web", "serious", "web UI stopped")
    await health.tick(clock.now)
    for seconds in (30, 60, 119):
        await health.tick(clock.now + seconds)
    assert len(sender.sent) == 1
    await health.tick(clock.now + 120)
    assert len(sender.sent) == 2
    assert health.problems()[0].notified_open is True


@pytest.mark.parametrize("kind", [ErrorKind.AUTH, ErrorKind.BAD_REQUEST])
async def test_auth_and_bad_request_are_retried_on_the_backoff_for_alerts(
    health: Health, sender: FakeSender, clock: Clock, kind: ErrorKind
) -> None:
    sender.outcomes = [TelegramError(kind, "sendMessage: Forbidden")]
    health.open("web", "serious", "web UI stopped")
    await health.tick(clock.now)
    await health.tick(clock.now + 29)
    assert len(sender.sent) == 1
    await health.tick(clock.now + 30)
    [problem] = health.problems()
    assert (problem.notified_open, problem.attempts, problem.last_error) == (True, 0, None)


async def test_the_backoff_doubles_and_no_tick_hammers(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    down = TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")
    sender.outcomes = [down, down, down]
    health.open("web", "serious", "web UI stopped")
    ticks = 0
    for second in range(0, 211, 5):  # every 5 s maintenance tick for 3.5 minutes
        await health.tick(clock.now + second)
        ticks += 1
    # Tries at 0, 30 (after 30 s), 90 (after 60 s) and 210 (after 120 s): 4 of 43 ticks.
    assert (ticks, len(sender.sent)) == (43, 4)
    assert health.problems()[0].notified_open is True


async def test_an_unexpected_sender_error_backs_off_too(store: Store, clock: Clock) -> None:
    class Broken:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            raise ValueError("chat_id is required to send messages")

    health = Health(store, Broken(), clock=clock, tz=UTC)
    health.open("web", "serious", "web UI stopped")
    await health.tick(clock.now)  # never raises
    [problem] = health.problems()
    assert problem.attempts == 1 and problem.next_try_at == T0 + 30
    assert (
        problem.last_error == "unexpected error: ValueError: chat_id is required to send messages"
    )


async def test_re_opened_while_the_resolution_waits_is_one_outage(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("project:p", "serious", "project p: watch lost", project="p")
    await tick(health, clock)
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")]
    health.resolve("project:p")
    await tick(health, clock)  # the resolution fails
    health.open("project:p", "serious", "project p: watch lost", project="p")
    await tick(health, clock, 10)
    assert [p.key for p in health.problems()] == ["project:p"]
    assert texts(sender) == [
        "⚠️ Diffbell: project p: watch lost",
        "✅ Diffbell: project p: watching again (down 10:00–10:00)",  # the failed one
    ]


async def test_resolved_while_its_open_message_is_in_flight(store: Store, clock: Clock) -> None:
    """The send awaits: a resolve landing meanwhile is kept, and sent next."""
    sent: list[str] = []
    health: Health

    class Resolving:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text.startswith("⚠️"):
                health.resolve("project:p")
            return 1

    health = Health(store, Resolving(), clock=clock, tz=UTC)
    health.open("project:p", "serious", "project p: watch lost", project="p")
    await health.tick(clock.now)
    assert health.problems() == []
    await health.tick(clock.now)
    assert sent == [
        "⚠️ Diffbell: project p: watch lost",
        "✅ Diffbell: project p: watching again (down 10:00–10:00)",
    ]
    assert store.all_problems() == []


async def test_re_opened_while_its_resolution_is_in_flight_is_a_new_problem(
    store: Store, clock: Clock
) -> None:
    sent: list[str] = []
    health: Health

    class Reopening:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text.startswith("✅"):
                health.open("project:p", "serious", "project p: watch lost again", project="p")
            return 1

    health = Health(store, Reopening(), clock=clock, tz=UTC)
    health.open("project:p", "serious", "project p: watch lost", project="p")
    await health.tick(clock.now)
    health.resolve("project:p")
    clock.now += 60
    await health.tick(clock.now)
    await health.tick(clock.now)
    assert sent[-1] == "⚠️ Diffbell: project p: watch lost again"
    [problem] = health.problems()
    assert (problem.opened_at, problem.notified_open) == (T0 + 60, True)


# -- races and edge cases (review fixes) ----------------------------------------------


async def test_re_opened_while_its_resolution_is_in_flight_keeps_its_grace(
    store: Store, clock: Clock
) -> None:
    sent: list[str] = []
    health: Health

    class Reopening:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text.startswith("✅"):
                health.open("watchman", "serious", "Watchman down again", grace=300)
            return 1

    health = Health(store, Reopening(), clock=clock, tz=UTC)
    health.open("watchman", "serious", WATCHMAN, grace=300)
    clock.now += 400
    await health.tick(clock.now)
    health.resolve("watchman")
    await health.tick(clock.now)  # the resolution is in flight when it re-opens
    clock.now += 5
    await health.tick(clock.now)
    assert len(sent) == 2  # inside the new grace: nothing yet
    clock.now += 300
    await health.tick(clock.now)
    assert sent[-1] == "⚠️ Diffbell: Watchman down again"


async def test_a_key_forgotten_during_another_send_is_not_sent(store: Store, clock: Clock) -> None:
    sent: list[str] = []
    health: Health

    class Forgetting:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if "A lost" in text:
                health.forget("project:b")
            return 1

    health = Health(store, Forgetting(), clock=clock, tz=UTC)
    health.open("project:a", "serious", "A lost", project="a")
    clock.now += 1
    health.open("project:b", "serious", "B lost", project="b")
    await health.tick(clock.now)
    assert sent == ["⚠️ Diffbell: A lost"]
    assert [p.key for p in store.all_problems()] == ["project:a"]


async def test_a_key_resolved_during_another_send_gets_only_its_resolution(
    store: Store, clock: Clock
) -> None:
    sent: list[str] = []
    health: Health

    class Resolving:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if "A lost" in text:
                health.resolve("project:b")
            return 1

    health = Health(store, Resolving(), clock=clock, tz=UTC)
    health.open("project:a", "serious", "A lost", project="a")
    clock.now += 1
    health.open("project:b", "serious", "B lost", project="b")
    await health.tick(clock.now)
    await health.tick(clock.now)
    assert sent == [
        "⚠️ Diffbell: A lost",
        "✅ Diffbell: project b: watching again (was down 10:00–10:00: B lost)",
    ]
    assert [p.key for p in store.all_problems()] == ["project:a"]


async def test_forgotten_and_re_opened_during_its_send_is_a_fresh_problem(
    store: Store, clock: Clock
) -> None:
    sent: list[str] = []
    health: Health

    class Replacing:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text == "⚠️ Diffbell: P lost":
                clock.now += 1
                health.forget("project:p")
                health.open("project:p", "serious", "P lost #2", project="p")
            return 1

    health = Health(store, Replacing(), clock=clock, tz=UTC)
    health.open("project:p", "serious", "P lost", project="p")
    clock.now += 1
    await health.tick(clock.now)
    await health.tick(clock.now)
    assert sent == ["⚠️ Diffbell: P lost", "⚠️ Diffbell: P lost #2"]


async def test_a_failure_does_not_land_on_a_problem_replaced_during_the_send(
    store: Store, clock: Clock
) -> None:
    sent: list[str] = []
    health: Health

    class FailingAndReplacing:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text == "⚠️ Diffbell: P lost":
                clock.now += 1
                health.forget("project:p")
                health.open("project:p", "serious", "P lost #2", project="p")
                raise TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")
            return 1

    health = Health(store, FailingAndReplacing(), clock=clock, tz=UTC)
    health.open("project:p", "serious", "P lost", project="p")
    clock.now += 1
    await health.tick(clock.now)
    [problem] = store.all_problems()
    assert (problem.attempts, problem.last_error) == (0, None)
    await health.tick(clock.now)
    assert sent[-1] == "⚠️ Diffbell: P lost #2"


async def test_a_notice_replaced_while_in_flight_is_sent_too(store: Store, clock: Clock) -> None:
    sent: list[str] = []
    health: Health

    class Replacing:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text == "first":
                health.notice("restart", "second")
            return 1

    health = Health(store, Replacing(), clock=clock, tz=UTC)
    health.notice("restart", "first")
    await health.tick(clock.now)
    await health.tick(clock.now + 60)
    assert sent == ["first", "second"]
    assert store.all_problems() == []


async def test_a_sent_serious_problem_turned_warning_stays_serious_for_its_resolution(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("k", "serious", "broke")
    await tick(health, clock)
    health.open("k", "warning", "now just a warning")
    [problem] = health.problems()
    assert (problem.severity, problem.message) == ("serious", "now just a warning")
    health.resolve("k")
    await tick(health, clock, 1)
    assert texts(sender) == [
        "⚠️ Diffbell: broke",
        "✅ Diffbell: resolved: now just a warning (down 10:00–10:00)",
    ]


async def test_a_severity_change_is_a_fresh_problem(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("k", "warning", "odd", grace=0)
    clock.now += 600
    health.open("k", "serious", "broke")
    [problem] = health.problems()
    assert (problem.severity, problem.opened_at) == ("serious", T0 + 600)
    # Serious but not sent yet (in its grace), turned warning: fresh, never sent.
    health.forget("k")
    health.open("k", "serious", "broke", grace=300)
    clock.now += 10
    health.open("k", "warning", "odd again")
    await tick(health, clock, 10)
    [problem] = health.problems()
    assert (problem.severity, problem.opened_at) == ("warning", T0 + 610)
    assert texts(sender) == []


async def test_telegram_down_costs_one_call_per_tick_whatever_the_number_of_problems(
    store: Store, clock: Clock
) -> None:
    down = TelegramError(ErrorKind.RETRYABLE, "sendMessage: network error (timeout)")
    sender = FakeSender([down] * 100)
    health = Health(store, sender, clock=clock, tz=UTC)
    for i in range(10):
        health.open(f"project:{i}", "serious", f"{i} lost", project=str(i))
    await health.tick(clock.now)
    assert len(sender.sent) == 1
    rows = store.all_problems()
    assert {p.next_try_at for p in rows} == {clock.now + backoff(0)}
    assert all(p.last_error == "sendMessage: network error (timeout)" for p in rows)
    sender.outcomes = []
    clock.now += backoff(0)
    await health.tick(clock.now)
    assert len(sender.sent) == 11


async def test_a_lone_surrogate_is_replaced_not_fatal(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("k", "serious", "bad \ud800 name")
    health.notice("n", "also \udcff bad")
    await tick(health, clock)
    assert texts(sender) == ["⚠️ Diffbell: bad ? name", "also ? bad"]


async def test_a_failed_send_never_stores_or_logs_the_bot_token(
    store: Store, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://api.telegram.org/bot123456:AAsecretTOKEN/sendMessage"
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, f"{url}: timed out")])
    health = Health(store, sender, clock=clock, tz=UTC)
    health.open("k", "serious", "broke")
    await health.tick(clock.now)
    [problem] = store.all_problems()
    assert problem.last_error == "https://api.telegram.org/bot<redacted>/sendMessage: timed out"
    assert "AAsecretTOKEN" not in caplog.text


def test_problem_is_the_store_row() -> None:
    assert Problem.__module__ == "diffbell.store"


def test_health_and_the_notifier_never_import_each_other() -> None:
    src = Path(diffbell.__file__).parent

    def modules(name: str) -> set[str]:
        tree = ast.parse((src / f"{name}.py").read_text(encoding="utf-8"))
        return {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}

    assert "diffbell.notifier" not in modules("health")
    assert "diffbell.health" not in modules("notifier")
    assert "diffbell.delivery" in modules("health") & modules("notifier")


# -- review fixes, batch 3 ------------------------------------------------------------


async def test_a_deferred_row_keeps_its_own_backoff(store: Store, clock: Clock) -> None:
    """After a retryable failure, the other due rows wait their own backoff step, not
    the failing row's: a new alert never inherits an old one's 30 minutes."""
    down = TelegramError(ErrorKind.RETRYABLE, "sendMessage: network error (timeout)")
    sender = FakeSender([down] * 100)
    health = Health(store, sender, clock=clock, tz=UTC)
    health.open("project:old", "serious", "old lost", project="old")
    old: Problem | None = None
    for _ in range(7):
        await health.tick(clock.now)
        old = store.get_problem("project:old")
        assert old is not None
        clock.now = old.next_try_at
    assert (
        old is not None and backoff(old.attempts - 1) == 1800
    )  # the old row is at the longest step
    health.open("project:new", "serious", "new lost", project="new")
    calls = len(sender.sent)
    await health.tick(clock.now)
    assert len(sender.sent) == calls + 1  # only the old row called Telegram
    new = store.get_problem("project:new")
    assert new is not None
    assert (new.attempts, new.next_try_at - clock.now) == (1, backoff(0))
    assert new.last_error == "sendMessage: network error (timeout)"
    old = store.get_problem("project:old")
    assert old is not None and old.next_try_at - clock.now == 1800


async def test_a_deferred_row_waits_a_429s_retry_after(store: Store, clock: Clock) -> None:
    limited = TelegramError(ErrorKind.RETRYABLE, "sendMessage: 429", retry_after=7)
    sender = FakeSender([limited])
    health = Health(store, sender, clock=clock, tz=UTC)
    health.open("a", "serious", "A")
    health.open("b", "serious", "B")
    await health.tick(clock.now)
    assert len(sender.sent) == 1
    assert {p.next_try_at - clock.now for p in store.all_problems()} == {7}


async def test_forgotten_and_re_opened_at_the_same_instant_during_its_send(
    store: Store, clock: Clock
) -> None:
    """The clock need not move between the send and the re-open: the fresh problem's
    "opened" message still goes out."""
    sent: list[str] = []
    health: Health

    class Replacing:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text == "⚠️ Diffbell: P lost":
                health.forget("project:p")
                health.open("project:p", "serious", "P lost #2", project="p")
            return 1

    health = Health(store, Replacing(), clock=clock, tz=UTC)
    health.open("project:p", "serious", "P lost", project="p")
    await health.tick(clock.now)
    await health.tick(clock.now)
    assert sent == ["⚠️ Diffbell: P lost", "⚠️ Diffbell: P lost #2"]


async def test_a_problem_in_its_grace_logs_info_and_warns_once_the_grace_is_over(
    health: Health, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    def levels() -> list[int]:
        return [r.levelno for r in caplog.records if "watchman" in r.getMessage()]

    with caplog.at_level(logging.INFO, logger="diffbell.health"):
        health.open("watchman", "serious", WATCHMAN, grace=300)
        health.open("watchman", "serious", WATCHMAN, grace=300)  # still down: no new line
        await tick(health, clock, 4)
        assert levels() == [logging.INFO]
        await tick(health, clock, 1)
        assert levels() == [logging.INFO, logging.WARNING]
        await tick(health, clock, 1)
        assert levels() == [logging.INFO, logging.WARNING]
        caplog.clear()
        health.open("config", "serious", "config rejected")
        assert [r.levelno for r in caplog.records] == [logging.WARNING]


# -- review fixes, batch 4 ------------------------------------------------------------


async def test_a_message_changed_while_its_alert_is_in_flight_is_one_alert(
    store: Store, clock: Clock
) -> None:
    """The user saves the config again while its ⚠️ is being sent: the problem
    continues (same start), so its new text never gets a second ⚠️."""
    sent: list[str] = []
    health: Health

    class Reopening:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            sent.append(text)
            if text == "⚠️ Diffbell: config rejected: X":
                health.open("config", "serious", "config rejected: Y")
            return 1

    health = Health(store, Reopening(), clock=clock, tz=UTC)
    health.open("config", "serious", "config rejected: X")
    await health.tick(clock.now)
    clock.now += 5
    await health.tick(clock.now)
    assert sent == ["⚠️ Diffbell: config rejected: X"]
    problem = store.get_problem("config")
    assert problem is not None
    assert (problem.message, problem.notified_open) == ("config rejected: Y", True)


async def test_a_failed_send_whose_message_changed_still_backs_off(
    store: Store, clock: Clock
) -> None:
    health: Health

    class FailingAndReopening:
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            health.open("config", "serious", "config rejected: Y")
            raise TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")

    health = Health(store, FailingAndReopening(), clock=clock, tz=UTC)
    health.open("config", "serious", "config rejected: X")
    await health.tick(clock.now)
    problem = store.get_problem("config")
    assert problem is not None
    assert (problem.message, problem.attempts) == ("config rejected: Y", 1)
    assert (problem.next_try_at, problem.last_error) == (
        clock.now + backoff(0),
        "sendMessage: 502",
    )


async def test_fresh_rows_of_one_key_never_share_a_start(
    health: Health, store: Store, clock: Clock
) -> None:
    health.open("k", "serious", "one")
    first = store.get_problem("k")
    health.forget("k")
    health.open("k", "serious", "two", grace=60)
    second = store.get_problem("k")
    assert first is not None and second is not None
    assert second.opened_at > first.opened_at
    assert second.next_try_at == clock.now + 60


# -- review fixes, round 6 ------------------------------------------------------------


class FlakyStore(Store):
    """A store whose next ``fail`` writes (put_problem / delete_problem) raise, as a
    locked database does."""

    fail = 0

    def _maybe_fail(self) -> None:
        if self.fail:
            self.fail -= 1
            raise sqlite3.OperationalError("database is locked")

    def put_problem(self, problem: Problem) -> None:
        self._maybe_fail()
        super().put_problem(problem)

    def delete_problem(self, key: str) -> None:
        self._maybe_fail()
        super().delete_problem(key)


@pytest.fixture
def flaky(tmp_dir: Path) -> FlakyStore:
    return FlakyStore(tmp_dir / "flaky.db")


async def test_an_unrecorded_open_is_never_sent_again(
    flaky: FlakyStore, sender: FakeSender, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    health = Health(flaky, sender, clock=clock, tz=UTC)
    health.open("web", "serious", "web UI stopped")
    flaky.fail = 2  # the write after the send, and its first retry
    for _ in range(4):
        await tick(health, clock, minutes=1 / 12)  # every 5 s
    assert texts(sender) == ["⚠️ Diffbell: web UI stopped"]
    [row] = health.problems()
    assert row.notified_open is True
    unrecorded = [r for r in caplog.records if "not recorded" in r.getMessage()]
    assert len(unrecorded) == 1  # logged once per row
    assert "OperationalError" in unrecorded[0].getMessage()
    assert "database is locked" not in unrecorded[0].getMessage()  # type name only
    health.resolve("web")
    await tick(health, clock)
    assert texts(sender)[-1].startswith("✅ Diffbell: the web UI is running again (down")


async def test_an_unrecorded_resolution_is_never_sent_again(
    flaky: FlakyStore, sender: FakeSender, clock: Clock
) -> None:
    health = Health(flaky, sender, clock=clock, tz=UTC)
    health.open("web", "serious", "web UI stopped")
    await tick(health, clock)
    health.resolve("web")
    flaky.fail = 1  # deleting the resolved row
    for _ in range(3):
        await tick(health, clock, minutes=1 / 12)
    assert len(texts(sender)) == 2
    assert flaky.all_problems() == []


async def test_an_unrecorded_notice_is_never_sent_again(
    flaky: FlakyStore, sender: FakeSender, clock: Clock
) -> None:
    health = Health(flaky, sender, clock=clock, tz=UTC)
    health.notice("restart", "⚠️ Diffbell restarted")
    flaky.fail = 1
    for _ in range(3):
        await tick(health, clock, minutes=1 / 12)
    assert texts(sender) == ["⚠️ Diffbell restarted"]
    assert flaky.all_problems() == []


async def test_an_unrecorded_open_resolved_meanwhile_sends_only_the_resolution(
    flaky: FlakyStore, sender: FakeSender, clock: Clock
) -> None:
    health = Health(flaky, sender, clock=clock, tz=UTC)
    health.open("web", "serious", "web UI stopped")
    flaky.fail = 1
    await tick(health, clock)
    health.resolve("web")
    await tick(health, clock, minutes=1)
    assert texts(sender) == [
        "⚠️ Diffbell: web UI stopped",
        "✅ Diffbell: the web UI is running again (down 10:00–10:00)",
    ]


async def test_a_failed_write_after_a_failed_send_never_raises(
    flaky: FlakyStore, sender: FakeSender, clock: Clock
) -> None:
    health = Health(flaky, sender, clock=clock, tz=UTC)
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")]
    health.open("web", "serious", "web UI stopped")
    flaky.fail = 1
    await tick(health, clock)  # must not raise
    await tick(health, clock)
    assert len(sender.sent) == 2  # not backed off (nothing stored), then delivered
    assert health.problems()[0].notified_open is True


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
async def test_a_non_finite_retry_after_waits_the_normal_backoff(
    health: Health, sender: FakeSender, clock: Clock, value: float
) -> None:
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 429", retry_after=value)]
    health.open("web", "serious", "web UI stopped")
    await tick(health, clock)
    [row] = health.problems()
    assert row.next_try_at == T0 + backoff(0)


async def test_after_a_restart_a_pending_resolution_is_not_held_by_the_grace(
    store: Store, sender: FakeSender, clock: Clock
) -> None:
    """Watcher.__init__ opens `watchman` with a 300 s grace at every start; the
    resolve() that follows makes the unsent ✅ due at once."""
    first = Health(store, sender, clock=clock, tz=UTC)
    first.open("watchman", "serious", WATCHMAN, grace=300)
    await tick(first, clock, minutes=5)  # ⚠️ sent at 10:05
    clock.now += 60
    first.resolve("watchman")  # 10:06, then the daemon stops before a tick
    again = Health(store, sender, clock=clock, tz=UTC)
    clock.now += 10
    again.open("watchman", "serious", WATCHMAN, grace=300)
    again.resolve("watchman")
    await tick(again, clock)
    assert texts(sender) == [
        f"⚠️ Diffbell: {WATCHMAN}",
        "✅ Diffbell: Watchman is reachable again (down 10:00–10:06)",
    ]


async def test_after_a_restart_a_resolution_keeps_the_backoff_of_its_failed_send(
    store: Store, sender: FakeSender, clock: Clock
) -> None:
    first = Health(store, sender, clock=clock, tz=UTC)
    first.open("watchman", "serious", WATCHMAN)
    await tick(first, clock)
    first.resolve("watchman")
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 429", retry_after=120)]
    await tick(first, clock, minutes=1)  # fails at 10:01: due 10:03
    again = Health(store, sender, clock=clock, tz=UTC)
    clock.now += 10
    again.open("watchman", "serious", WATCHMAN, grace=300)
    again.resolve("watchman")
    await tick(again, clock, minutes=1)  # 10:02: still waiting its retry_after
    assert len(sender.sent) == 2
    clock.now = T0 + 180
    await again.tick(clock.now)
    assert len(sender.sent) == 3
    assert texts(sender)[-1].startswith("✅ Diffbell: Watchman is reachable again")


async def test_after_a_restart_an_unsent_outage_past_its_grace_is_still_reported(
    store: Store, sender: FakeSender, clock: Clock
) -> None:
    """Past its grace and resolved, but no tick ran before the stop: the re-open's
    grace must not turn that into a silent "resolved within the grace"."""
    first = Health(store, sender, clock=clock, tz=UTC)
    first.open("watchman", "serious", WATCHMAN, grace=300)
    clock.now += 301
    first.resolve("watchman")
    again = Health(store, sender, clock=clock, tz=UTC)
    clock.now += 10
    again.open("watchman", "serious", WATCHMAN, grace=300)
    again.resolve("watchman")
    await tick(again, clock)
    assert texts(sender) == [
        f"✅ Diffbell: Watchman is reachable again (was down 10:00–10:05: {WATCHMAN})"
    ]


async def test_a_resolution_after_a_failed_re_open_alert_keeps_its_retry_after(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    """The pre-grace due time a re-open replaced is restored only while the row has
    not been tried since: a ⚠️ that failed with a 429 after the grace sets the ✅'s
    due time, never the stale one."""
    health.open("watchman", "serious", WATCHMAN, grace=300)
    clock.now += 301
    health.resolve("watchman")  # ✅ unsent, due 10:05:01
    health.open("watchman", "serious", WATCHMAN, grace=300)  # re-open: one outage
    sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 429", retry_after=3600)]
    await tick(health, clock, minutes=5.1)  # the ⚠️ fails: retry in an hour
    failed_at = clock.now
    assert len(sender.sent) == 1
    health.resolve("watchman")
    [row] = [p for p in health._store.all_problems() if p.key == "watchman"]
    assert row.next_try_at >= failed_at + 3600
    await tick(health, clock, minutes=1)
    assert len(sender.sent) == 1  # the 429 is honored


async def test_forgetting_drops_the_pre_grace_due_times(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    for key, project in (("project:old", "old"), ("watchman", None)):
        health.open(key, "serious", "down", project=project)
        await tick(health, clock)
        health.resolve(key)
        health.open(key, "serious", "down", project=project, grace=300)  # ✅ still unsent
    assert set(health._before_grace) == {"project:old", "watchman"}
    health.forget_projects_except([])
    assert set(health._before_grace) == {"watchman"}


async def test_a_sent_resolution_drops_its_pre_grace_due_time(store: Store, clock: Clock) -> None:
    """Re-opened while its ✅ is in flight: once the ✅ is recorded, the re-open is a
    fresh problem and the old due time is stale."""

    class ReopeningSender(FakeSender):
        async def send(
            self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
        ) -> int:
            if text.startswith("✅"):
                registry.open("watchman", "serious", WATCHMAN, grace=300)
            return await super().send(text, parse_mode=parse_mode, silent=silent)

    registry = Health(store, ReopeningSender(), clock=clock, tz=UTC)
    registry.open("watchman", "serious", WATCHMAN)
    await tick(registry, clock)
    registry.resolve("watchman")
    await tick(registry, clock, minutes=1)
    assert registry._before_grace == {}


# -- muted while Telegram is not set up (bot onboarding spec §1) --------------------


async def test_a_muted_tick_drops_notices_and_resolutions_and_keeps_open_problems(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    health.notice("restart", "⚠️ Diffbell restarted", not_before=clock.now + 600)
    health.notice("other", "hello")
    health.open("watchman", "serious", WATCHMAN)
    health.open("project:p", "serious", "project p: watch lost", project="p")
    clock.now += 60
    health.resolve("project:p")  # past its grace: a resolved row waiting for its ✅
    health.open("missing:p", "warning", "project p: path gone", project="p")
    await health.tick(clock.now, sending=False)
    assert sender.sent == []
    assert sorted(p.key for p in store.all_problems()) == ["missing:p", "watchman"]
    watchman = store.get_problem("watchman")
    assert watchman is not None
    assert (watchman.notified_open, watchman.attempts) == (False, 0)


async def test_a_notice_queued_for_later_is_not_sent_after_the_swap(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.notice("restart", "⚠️ Diffbell restarted", not_before=clock.now + 600)
    await health.tick(clock.now, sending=False)
    clock.now += 700
    await health.tick(clock.now)
    assert sender.sent == []


async def test_rearm_announces_an_unannounced_problem_once_and_never_an_announced_one(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    health.open("watchman", "serious", WATCHMAN, grace=120)
    clock.now += 300
    await health.tick(clock.now, sending=False)  # past its grace, but muted
    row = store.get_problem("watchman")
    assert row is not None and not row.notified_open
    # As if earlier sends had failed: rearm must start the backoff over.
    store.put_problem(dataclasses.replace(row, attempts=3, next_try_at=clock.now + 3600))
    store.put_problem(
        Problem("project:p", "serious", "p", "project p: watch lost", None, T0, notified_open=True)
    )
    assert health.rearm(clock.now) == 1
    rearmed = store.get_problem("watchman")
    assert rearmed is not None and (rearmed.attempts, rearmed.next_try_at) == (0, clock.now)
    await tick(health, clock)
    await tick(health, clock, 1)
    assert texts(sender) == [f"⚠️ Diffbell: {WATCHMAN}"]


async def test_opened_and_resolved_while_muted_sends_neither(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    health.open("watchman", "serious", WATCHMAN)
    clock.now += 600
    await health.tick(clock.now, sending=False)
    health.resolve("watchman")
    await health.tick(clock.now, sending=False)
    assert health.rearm(clock.now) == 0
    await tick(health, clock, 1)
    assert sender.sent == [] and store.all_problems() == []


async def test_the_setup_warning_is_never_sent(
    health: Health, sender: FakeSender, clock: Clock, store: Store
) -> None:
    assert SETUP_KEY == "telegram.setup"
    health.open(
        SETUP_KEY,
        "warning",
        "Telegram not set up. Set up Telegram in Settings → Telegram, or run the command.",
        fix="diffbell setup",
    )
    await health.tick(clock.now, sending=False)
    health.rearm(clock.now)
    health.resolve(SETUP_KEY)
    await tick(health, clock, 1)
    assert sender.sent == [] and store.get_problem(SETUP_KEY) is None


async def test_rearm_keeps_a_running_grace_and_a_resolve_inside_it_sends_nothing(
    health: Health, sender: FakeSender, clock: Clock
) -> None:
    health.open("watchman", "serious", WATCHMAN, grace=300)
    await health.tick(clock.now, sending=False)
    clock.now += 100
    assert health.rearm(clock.now) == 1
    await health.tick(clock.now)
    assert sender.sent == []  # still inside the grace
    clock.now += 250
    await health.tick(clock.now)
    assert texts(sender) == [f"⚠️ Diffbell: {WATCHMAN}"]
    health.open("w2", "serious", "w2 down", grace=300)
    clock.now += 50
    health.rearm(clock.now)
    health.resolve("w2")
    await health.tick(clock.now + 1000)
    assert texts(sender) == [f"⚠️ Diffbell: {WATCHMAN}"]


class _BrokenStore:
    """A Store stand-in whose every call raises (a locked database)."""

    def __getattr__(self, name: str) -> object:
        def boom(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        return boom


async def test_rearm_is_best_effort_and_logs_only_the_type_name(
    sender: FakeSender, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    broken = Health(_BrokenStore(), sender, clock=clock, tz=UTC)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="diffbell.health"):
        assert broken.rearm(clock.now) == 0
    assert "OperationalError" in caplog.text and "locked" not in caplog.text


async def test_a_muted_tick_survives_a_failing_store_and_logs_only_the_type_name(
    sender: FakeSender, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    broken = Health(_BrokenStore(), sender, clock=clock, tz=UTC)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="diffbell.health"):
        await broken.tick(clock.now, sending=False)
    assert "OperationalError" in caplog.text and "locked" not in caplog.text


async def test_a_muted_tick_and_rearm_leave_an_unrecorded_row_alone(
    health: Health,
    sender: FakeSender,
    clock: Clock,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def still_failing(*args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(health, "_sent", still_failing)
    health.notice("n", "hello")
    row = store.get_problem("n")
    assert row is not None
    health._unrecorded["n"] = row
    health.open("watchman", "serious", WATCHMAN)
    watchman = store.get_problem("watchman")
    assert watchman is not None
    store.put_problem(dataclasses.replace(watchman, attempts=2, next_try_at=clock.now + 99))
    health._unrecorded["watchman"] = watchman
    await health.tick(clock.now, sending=False)
    assert health.rearm(clock.now) == 0
    assert store.get_problem("n") is not None
    unchanged = store.get_problem("watchman")
    assert unchanged is not None and unchanged.attempts == 2
