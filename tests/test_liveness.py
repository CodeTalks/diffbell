"""Liveness (health spec §5, §8 "Liveness", "Heartbeat", "Failure status")."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from diffbell.daemon import Daemon
from diffbell.fingerprint import Observation
from diffbell.health import Health
from diffbell.liveness import (
    HEARTBEAT_KEY,
    RESTART_KEY,
    RESTART_NOTICE_EVERY,
    Heartbeat,
    heartbeat_text,
    queue_restart_notice,
    read_status,
    restart_message,
    write_failure_status,
)
from diffbell.models import ChangeKind
from diffbell.store import ClockState, Problem, Store
from diffbell.telegram import ErrorKind, TelegramError
from tests.fakes import FakeSender, fake_slot
from tests.helpers import sync_names

UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")
T0 = 1_791_540_000.0  # 2026-10-09 10:00:00 UTC


def at(text: str, tz: ZoneInfo = UTC) -> float:
    """Epoch seconds of a local "YYYY-MM-DD HH:MM" in ``tz``."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()


# -- the restart message ------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        (None, None),
        ({"running": False, "clean_shutdown": True, "updated_at": T0 - 120}, None),
        # A file from before 5a: a normal stop wrote "running": false.
        ({"running": False, "updated_at": T0 - 120}, None),
        # A crash: the last snapshot still says running.
        (
            {"running": True, "updated_at": T0 - 120, "last_started_at": T0 - 3600},
            "⚠️ Diffbell restarted after an unexpected stop (down 09:58–10:00)",
        ),
        # A crash under the version before 5a (no last_started_at): still a restart.
        (
            {"running": True, "updated_at": T0 - 120},
            "⚠️ Diffbell restarted after an unexpected stop (down 09:58–10:00)",
        ),
        # A background task died: the final snapshot carries the reason.
        (
            {
                "running": False,
                "updated_at": T0 - 60,
                "last_started_at": T0 - 3600,
                "last_failure": "background task pump",
            },
            "⚠️ Diffbell restarted after an unexpected stop (down 09:59–10:00): background task pump",
        ),
        # Failed starts after a run that had succeeded once: the flag decides.
        (
            {
                "running": True,
                "updated_at": T0 - 600,
                "last_failure": "database migration failed: disk I/O error",
                "last_started_at": T0 - 7200,
                "failed_start": True,
            },
            "⚠️ Diffbell could not start earlier (09:50–10:00): "
            "database migration failed: disk I/O error",
        ),
        # A failure without the flag (a background task died): a restart, even though
        # it is still marked running.
        (
            {
                "running": True,
                "updated_at": T0 - 600,
                "last_failure": "background task pump",
                "last_started_at": T0 - 7200,
            },
            "⚠️ Diffbell restarted after an unexpected stop (down 09:50–10:00): background task pump",
        ),
        # No start ever succeeded.
        (
            {"running": True, "updated_at": T0 - 600, "last_failure": "config: bad"},
            "⚠️ Diffbell could not start earlier (09:50–10:00): config: bad",
        ),
    ],
)
def test_the_restart_message(previous: dict[str, Any] | None, expected: str | None) -> None:
    assert restart_message(previous, T0, UTC) == expected


# -- the failure status, written without a watcher ----------------------------------


def test_a_failed_start_writes_running_with_the_reason(tmp_dir: Path) -> None:
    write_failure_status(tmp_dir / "state", "database migration failed: x", T0)
    path = tmp_dir / "state" / "status.json"
    assert path.stat().st_mode & 0o777 == 0o600
    status = json.loads(path.read_text())
    assert status["running"] is True and "clean_shutdown" not in status
    assert (status["updated_at"], status["last_failure"]) == (T0, "database migration failed: x")
    assert status["failed_start"] is True
    assert "last_started_at" not in status


def test_launchds_loop_keeps_the_first_down_time_and_the_last_reason(tmp_dir: Path) -> None:
    state = tmp_dir / "state"
    state.mkdir()
    crash = {"running": True, "updated_at": T0 - 300, "last_started_at": T0 - 9000, "pid": 1}
    (state / "status.json").write_text(json.dumps(crash))
    for i, reason in enumerate(["first", "second", "third"]):
        write_failure_status(state, reason, T0 + 10 * i)
    status = read_status(state)
    assert status is not None
    assert (status["updated_at"], status["last_failure"]) == (T0 - 300, "third")
    assert status["last_started_at"] == T0 - 9000
    assert status["failed_start"] is True
    # A start had succeeded once (last_started_at), but this file is a failed start.
    assert restart_message(status, T0 + 60, UTC) == (
        "⚠️ Diffbell could not start earlier (09:55–10:01): third"
    )


def test_after_a_clean_stop_the_failure_starts_its_own_down_time(tmp_dir: Path) -> None:
    state = tmp_dir / "state"
    state.mkdir()
    clean = {"running": False, "clean_shutdown": True, "updated_at": T0 - 300}
    (state / "status.json").write_text(json.dumps(clean))
    write_failure_status(state, "config: bad", T0)
    assert read_status(state) == {
        "pid": read_status(state)["pid"],  # type: ignore[index]
        "running": True,
        "updated_at": T0,
        "last_failure": "config: bad",
        "failed_start": True,
    }


def test_a_failure_after_this_processs_start_is_an_unexpected_stop(tmp_dir: Path) -> None:
    """A daemon of this process started, then died: its own snapshot is kept (down
    from its last update), the reason added, and the next start says "restarted"."""
    state = tmp_dir / "state"
    state.mkdir()
    final = {
        "pid": os.getpid(),
        "running": False,
        "updated_at": T0 - 60,
        "last_started_at": T0 - 3600,
        "last_failure": "background task pump stopped: ValueError: boom",
        "connected": True,
    }
    (state / "status.json").write_text(json.dumps(final))
    write_failure_status(
        state, "background task pump stopped: ValueError: boom", T0, started_at=T0 - 3600
    )
    status = read_status(state)
    assert status == final  # kept as the daemon wrote it: no failed_start
    assert restart_message(status, T0 + 60, UTC) == (
        "⚠️ Diffbell restarted after an unexpected stop (down 09:59–10:01): "
        "background task pump stopped: ValueError: boom"
    )


def test_a_failure_after_a_clean_final_snapshot_is_not_clean(tmp_dir: Path) -> None:
    state = tmp_dir / "state"
    state.mkdir()
    final = {
        "pid": os.getpid(),
        "running": False,
        "clean_shutdown": True,
        "updated_at": T0 - 60,
        "last_started_at": T0 - 3600,
    }
    (state / "status.json").write_text(json.dumps(final))
    write_failure_status(state, "OSError: late", T0, started_at=T0 - 3600)
    status = read_status(state)
    assert status is not None
    assert "clean_shutdown" not in status and "failed_start" not in status
    assert (status["updated_at"], status["last_started_at"]) == (T0 - 60, T0 - 3600)
    assert status["last_failure"] == "OSError: late"
    assert restart_message(status, T0 + 60, UTC) == (
        "⚠️ Diffbell restarted after an unexpected stop (down 09:59–10:01): OSError: late"
    )


def test_a_started_failure_without_its_own_snapshot_is_still_a_stop(tmp_dir: Path) -> None:
    """The daemon started but its snapshot is not in the file (another pid's, or a
    write that failed): still "restarted", never "could not start"."""
    state = tmp_dir / "state"
    state.mkdir()
    other = {
        "pid": os.getpid() + 1,
        "running": False,
        "clean_shutdown": True,
        "updated_at": T0 - 9000,
        "last_started_at": T0 - 3600,
    }
    (state / "status.json").write_text(json.dumps(other))
    write_failure_status(state, "OSError: x", T0, started_at=T0 - 60)
    status = read_status(state)
    assert status is not None
    assert "failed_start" not in status
    assert (status["updated_at"], status["last_started_at"]) == (T0, T0 - 60)
    assert restart_message(status, T0 + 60, UTC) == (
        "⚠️ Diffbell restarted after an unexpected stop (down 10:00–10:01): OSError: x"
    )


def test_a_failure_before_any_start_in_this_process_is_a_failed_start(tmp_dir: Path) -> None:
    """Same pid and a last_started_at (tests share a process), but no start marker:
    still "could not start earlier"."""
    state = tmp_dir / "state"
    state.mkdir()
    old = {
        "pid": os.getpid(),
        "running": False,
        "clean_shutdown": True,
        "updated_at": T0 - 300,
        "last_started_at": T0 - 3600,
    }
    (state / "status.json").write_text(json.dumps(old))
    write_failure_status(state, "config: bad", T0)
    status = read_status(state)
    assert status is not None and status["failed_start"] is True
    assert restart_message(status, T0 + 60, UTC) == (
        "⚠️ Diffbell could not start earlier (10:00–10:01): config: bad"
    )


def test_an_unreadable_status_file_is_no_status(tmp_dir: Path) -> None:
    (tmp_dir / "status.json").write_text("{not json")
    assert read_status(tmp_dir) is None
    (tmp_dir / "status.json").write_text("[1, 2]")
    assert read_status(tmp_dir) is None


# -- the daemon: status.json at stop and at start -------------------------------------


def no_watchman() -> Any:
    raise OSError("watchman is not running (test)")


def make_daemon(tmp_dir: Path, sender: FakeSender) -> Daemon:
    proj = tmp_dir / "proj"
    proj.mkdir(exist_ok=True)
    config = tmp_dir / "cfg" / "config.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f"projects:\n  - path: {proj}\n")
    state = tmp_dir / "state"
    return Daemon(
        config,
        Store(state / "state.db"),
        fake_slot(sender),
        state_dir=state,
        wm_factory=no_watchman,
        recv_timeout=0.05,
        maintenance_interval=0.02,
    )


async def run_briefly(d: Daemon, ticks: int = 5) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    status = d._state_dir / "status.json"
    while not status.exists() or json.loads(status.read_text()).get("running") is not True:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02 * ticks)
    stop.set()
    await asyncio.wait_for(task, timeout=10)


async def test_a_normal_stop_is_a_clean_shutdown(tmp_dir: Path) -> None:
    d = make_daemon(tmp_dir, FakeSender())
    await run_briefly(d)
    status = read_status(tmp_dir / "state")
    assert status is not None
    assert (status["running"], status["clean_shutdown"]) == (False, True)
    assert isinstance(status["last_started_at"], float)
    assert "last_failure" not in status


async def test_a_dead_background_task_is_not_a_clean_shutdown(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dying_pump(self: Daemon, watcher: Any, inbox: Any) -> None:
        await asyncio.sleep(0.05)
        raise ValueError("pump boom")

    monkeypatch.setattr(Daemon, "_pump", dying_pump)
    d = make_daemon(tmp_dir, FakeSender())
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(d.run(asyncio.Event()), timeout=10)
    status = read_status(tmp_dir / "state")
    assert status is not None and "clean_shutdown" not in status
    assert status["last_failure"].startswith("background task pump stopped: ValueError")


async def test_a_start_after_a_crash_sends_one_restart_message(tmp_dir: Path) -> None:
    state = tmp_dir / "state"
    state.mkdir()
    crash = {"running": True, "updated_at": 1.0, "last_started_at": 0.5}
    (state / "status.json").write_text(json.dumps(crash))
    sender = FakeSender()
    d = make_daemon(tmp_dir, sender)
    await run_briefly(d)
    restarts = [t for t, _ in sender.sent if "restarted after an unexpected stop" in t]
    assert len(restarts) == 1 and sender.sent[0][1] is None
    # The next start follows a clean stop: nothing.
    d2 = make_daemon(tmp_dir, sender)
    await run_briefly(d2)
    assert len([t for t, _ in sender.sent if "unexpected stop" in t]) == 1


@pytest.mark.usefixtures("nameless_mac")
async def test_after_launchds_loop_the_first_good_start_sends_once_with_the_reason(
    tmp_dir: Path,
) -> None:
    state = tmp_dir / "state"
    for reason in ("database busy", "database migration failed: disk I/O error"):
        write_failure_status(state, reason, 100.0)
    sender = FakeSender()
    await run_briefly(make_daemon(tmp_dir, sender))
    [message] = [t for t, _ in sender.sent if "Diffbell" in t and "earlier" in t]
    assert message.startswith("⚠️ Diffbell could not start earlier (")
    assert message.endswith("): database migration failed: disk I/O error")


@pytest.mark.usefixtures("nameless_mac")
async def test_the_restart_message_goes_through_health(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram down at start: the message is kept and retried, not lost."""
    state = tmp_dir / "state"
    state.mkdir()
    (state / "status.json").write_text(json.dumps({"running": True, "updated_at": 1.0}))
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "sendMessage: network error")])
    d = make_daemon(tmp_dir, sender)
    await run_briefly(d)
    [row] = [p for p in d._store.all_problems() if p.key == "restart"]
    assert (row.severity, row.attempts) == ("notice", 1)
    assert row.message.startswith("⚠️ Diffbell restarted after an unexpected stop")


async def test_the_first_snapshot_is_written_at_start(tmp_dir: Path) -> None:
    """At once, so a crash right after the start is not reported twice."""
    state = tmp_dir / "state"
    state.mkdir()
    (state / "status.json").write_text(json.dumps({"running": True, "updated_at": 1.0}))
    d = make_daemon(tmp_dir, FakeSender())
    d._maintenance_interval = 60.0  # no maintenance tick writes it
    stop = asyncio.Event()
    task = asyncio.create_task(d.run(stop))
    status = state / "status.json"
    while (read_status(state) or {}).get("updated_at") == 1.0:
        await asyncio.sleep(0.01)
    assert json.loads(status.read_text())["last_started_at"] is not None
    stop.set()
    await asyncio.wait_for(task, timeout=10)


# -- the heartbeat ----------------------------------------------------------------


class Env:
    def __init__(self, tmp_dir: Path, *, tz: ZoneInfo = UTC, at_time: str | None = "09:00"):
        self.store = Store(tmp_dir / "state.db")
        sync_names(self.store, "p")
        self.sender = FakeSender()
        self.now = at("2026-10-09 08:00", tz)
        self.health = Health(self.store, self.sender, clock=lambda: self.now, tz=tz)
        self.at: str | None = at_time
        self.projects = 3
        self.configured = True
        self.heartbeat = Heartbeat(
            self.store,
            self.sender,
            self.health,
            at=lambda: self.at,
            projects=lambda: self.projects,
            tz=tz,
            configured=lambda: self.configured,
        )

    async def tick_at(self, when: float) -> None:
        self.now = when
        await self.heartbeat.tick(when)

    def texts(self) -> list[str]:
        return [t for t, _ in self.sender.sent]

    def add_event(self, created_at: float) -> None:
        obs = Observation(changes=[(f"f{created_at}", ChangeKind.MODIFIED)])
        state = ClockState("/w", "", "c:1", "h")
        changes = self.store.record_observation("p", state, obs, now=created_at)
        self.store.create_event("p", changes, [], created_at)


def heartbeats(env: Env) -> list[str]:
    return [t for t in env.texts() if t.startswith("✅ Diffbell · ")]


@pytest.fixture
def env(tmp_dir: Path) -> Env:
    return Env(tmp_dir)


async def test_sent_once_a_day_after_its_time_silently(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 08:59"))
    assert env.sender.sent == []
    await env.tick_at(at("2026-10-09 09:00"))
    await env.tick_at(at("2026-10-09 09:05"))
    await env.tick_at(at("2026-10-09 23:59"))
    assert env.sender.sent == [
        ("✅ Diffbell · watching 3 projects · 0 changes yesterday · all OK", None)
    ]
    assert env.sender.silent == [True]
    assert env.store.kv_get(HEARTBEAT_KEY) == "2026-10-09 09:00"
    await env.tick_at(at("2026-10-10 09:00"))
    assert len(env.sender.sent) == 2


async def test_a_mac_asleep_at_nine_sends_at_the_first_tick_after_waking(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 13:37"))
    assert len(env.sender.sent) == 1


async def test_a_missed_day_is_skipped_not_caught_up(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-06 09:00")  # asleep for two days
    await env.tick_at(at("2026-10-09 09:30"))
    await env.tick_at(at("2026-10-09 09:35"))
    assert len(env.sender.sent) == 1


async def test_the_first_run_waits_for_the_next_heartbeat_time(tmp_dir: Path) -> None:
    after = Env(tmp_dir / "a")
    await after.tick_at(at("2026-10-09 15:00"))  # installed in the afternoon
    await after.tick_at(at("2026-10-09 15:05"))
    assert after.sender.sent == []
    await after.tick_at(at("2026-10-10 09:00"))
    assert len(after.sender.sent) == 1

    before = Env(tmp_dir / "b")
    await before.tick_at(at("2026-10-09 07:00"))  # installed in the morning
    assert before.sender.sent == []
    await before.tick_at(at("2026-10-09 09:00"))
    assert len(before.sender.sent) == 1


async def test_null_turns_it_off(tmp_dir: Path) -> None:
    env = Env(tmp_dir, at_time=None)
    for hour in ("08:00", "09:00", "23:00"):
        await env.tick_at(at(f"2026-10-09 {hour}"))
    assert env.sender.sent == []
    assert env.store.kv_get(HEARTBEAT_KEY) is None


async def test_a_new_time_sends_again_that_day(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-09 09:00")  # today's already went out
    env.at = "15:31"
    await env.tick_at(at("2026-10-09 15:30"))
    assert env.sender.sent == []
    await env.tick_at(at("2026-10-09 15:31"))
    await env.tick_at(at("2026-10-09 15:32"))
    assert len(env.sender.sent) == 1
    env.at = "09:00"  # moved back earlier the same day: no second one
    await env.tick_at(at("2026-10-09 15:40"))
    assert len(env.sender.sent) == 1
    await env.tick_at(at("2026-10-10 09:00"))
    assert len(env.sender.sent) == 2


async def test_turning_it_off_forgets_the_slot_so_on_again_waits(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    env.at = None
    await env.tick_at(at("2026-10-09 10:00"))
    assert env.store.kv_get(HEARTBEAT_KEY) is None
    env.at = "09:00"  # back on after its time: today's is skipped
    await env.tick_at(at("2026-10-09 10:05"))
    await env.tick_at(at("2026-10-09 23:00"))
    assert env.sender.sent == []
    await env.tick_at(at("2026-10-10 09:00"))
    assert len(env.sender.sent) == 1


async def test_a_429_heartbeat_waits_its_retry_after(env: Env) -> None:
    env.sender.outcomes = [TelegramError(ErrorKind.RETRYABLE, "sendMessage: 429", retry_after=300)]
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    start = at("2026-10-09 09:00")
    for second in (0, 30, 60, 299):
        await env.tick_at(start + second)
    assert len(env.sender.sent) == 1
    await env.tick_at(start + 300)
    assert len(env.sender.sent) == 2


async def test_quiet_hours_and_snooze_do_not_apply(env: Env) -> None:
    env.store.set_snooze("p", T0, None)  # snoozed, and the heartbeat ignores it
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 09:00"))
    assert len(env.sender.sent) == 1


async def test_changes_yesterday_counts_the_previous_local_day(env: Env) -> None:
    for when in ("2026-10-07 23:59", "2026-10-08 00:00", "2026-10-08 12:00", "2026-10-08 23:59"):
        env.add_event(at(when))
    env.add_event(at("2026-10-09 00:00"))  # today
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 09:00"))
    assert heartbeats(env) == ["✅ Diffbell · watching 3 projects · 3 changes yesterday · all OK"]


async def test_a_dst_day_has_23_hours(tmp_dir: Path) -> None:
    """2026-03-29 in Berlin: 02:00 jumps to 03:00. "Yesterday" seen on the 30th is
    the 23 hours between the two local midnights; a 02:30 heartbeat (a skipped
    time) goes out at the first tick after it."""
    env = Env(tmp_dir, tz=BERLIN, at_time="02:30")
    env.add_event(at("2026-03-28 23:59", BERLIN))
    env.add_event(at("2026-03-29 00:30", BERLIN))
    env.add_event(at("2026-03-29 23:59", BERLIN))
    env.store.kv_set(HEARTBEAT_KEY, "2026-03-28 02:30")
    await env.tick_at(at("2026-03-29 01:59", BERLIN))
    assert env.sender.sent == []
    await env.tick_at(at("2026-03-29 03:00", BERLIN))  # 02:30 never existed that day
    assert len(env.sender.sent) == 1
    assert env.heartbeat.changes_yesterday(at("2026-03-30 09:00", BERLIN)) == 2
    assert at("2026-03-30 00:00", BERLIN) - at("2026-03-29 00:00", BERLIN) == 23 * 3600


async def test_the_warning_and_problem_variants(env: Env) -> None:
    env.projects = 1
    env.add_event(at("2026-10-08 10:00"))
    env.health.open("recrawl:p", "warning", "project p: Watchman had to recrawl", project="p")
    env.health.open("recrawl:q", "warning", "project q: Watchman had to recrawl", project="q")
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 09:00"))
    env.health.open("project:p", "serious", "project p: watch lost", project="p")
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 09:01"))
    assert heartbeats(env) == [
        "✅ Diffbell · watching 1 project · 1 change yesterday · "
        "2 warnings: project p: Watchman had to recrawl",
        "✅ Diffbell · watching 1 project · 1 change yesterday · ⚠️ 1 problem: project p: watch lost",
    ]


def test_heartbeat_text_all_ok_and_plurals() -> None:
    assert heartbeat_text(0, 1, []) == (
        "✅ Diffbell · watching 0 projects · 1 change yesterday · all OK"
    )
    warning = Problem("telegram", "warning", None, "Telegram failing: x", None, 1.0)
    assert heartbeat_text(2, 0, [warning]).endswith("· 1 warning: Telegram failing: x")


async def test_a_failed_heartbeat_backs_off_and_then_goes_out(env: Env) -> None:
    down = TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502")
    env.sender.outcomes = [down, down]
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    start = at("2026-10-09 09:00")
    for second in range(0, 91, 5):  # every 5 s tick
        await env.tick_at(start + second)
    # Tries at 0, 30 (after 30 s) and 90 (after 60 s): 3 of 19 ticks.
    assert len(env.sender.sent) == 3
    assert env.sender.silent == [True, True, True]
    assert env.store.kv_get(HEARTBEAT_KEY) == "2026-10-09 09:00"


# -- review fixes, batch 4 ------------------------------------------------------------


async def test_a_crash_loop_sends_at_most_one_restart_notice_per_10_minutes(
    tmp_dir: Path,
) -> None:
    """A daemon that starts, then crashes, every minute for an hour: at most one
    notice per 10 minutes; a held notice keeps the first down-from time and takes the
    latest reason; nothing is lost."""
    store = Store(tmp_dir / "state.db")
    sender = FakeSender()
    now = [T0]
    health = Health(store, sender, clock=lambda: now[0], tz=UTC)
    sends: list[float] = []
    for minute in range(60):
        now[0] = T0 + 60 * minute
        previous = {
            "running": True,
            "updated_at": now[0] - 30,  # the crashed run's last snapshot
            "last_started_at": now[0] - 60,
            "last_failure": f"crash {minute}",
        }
        queue_restart_notice(store, health, previous, now[0], UTC)
        before = len(sender.sent)
        await health.tick(now[0] + 1)
        if len(sender.sent) > before:
            sends.append(now[0])
    assert RESTART_NOTICE_EVERY == 600
    assert len(sends) == 6
    assert all(b - a >= 600 for a, b in itertools.pairwise(sends))
    texts = [t for t, _ in sender.sent]
    assert texts[0] == ("⚠️ Diffbell restarted after an unexpected stop (down 09:59–10:00): crash 0")
    # Held from the start at 10:01 (down from 10:00:30) until 10:10.
    assert texts[1] == (
        "⚠️ Diffbell restarted after an unexpected stop (down 10:00–10:10): crash 10"
    )


async def test_a_held_restart_notice_survives_a_clean_restart(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    sender = FakeSender()
    now = [T0]
    health = Health(store, sender, clock=lambda: now[0], tz=UTC)
    crash = {"running": True, "updated_at": T0 - 30, "last_started_at": T0 - 60}
    queue_restart_notice(store, health, crash, T0, UTC)
    await health.tick(T0)
    now[0] = T0 + 60
    queue_restart_notice(store, health, crash | {"updated_at": T0 + 30}, now[0], UTC)
    await health.tick(now[0])
    assert len(sender.sent) == 1  # held
    now[0] = T0 + 120
    clean = {"running": False, "clean_shutdown": True, "updated_at": T0 + 100}
    queue_restart_notice(store, health, clean, now[0], UTC)
    await health.tick(T0 + 599)
    assert len(sender.sent) == 1
    await health.tick(T0 + 600)
    assert [t for t, _ in sender.sent][1] == (
        "⚠️ Diffbell restarted after an unexpected stop (down 10:00–10:01)"
    )


async def test_the_heartbeat_skips_a_serious_problem_still_in_its_grace(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    env.now = at("2026-10-09 08:58")
    env.health.open("watchman", "serious", "Watchman is unreachable", grace=300)
    await env.tick_at(at("2026-10-09 09:00"))
    assert heartbeats(env) == ["✅ Diffbell · watching 3 projects · 0 changes yesterday · all OK"]
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 09:03"))  # its grace is over
    assert heartbeats(env)[1].endswith("· ⚠️ 1 problem: Watchman is unreachable")


# -- review fixes, round 6 ------------------------------------------------------------


async def test_a_time_later_than_the_one_sent_today_sends_one_more(env: Env) -> None:
    """The slot compares with the time today's heartbeat went out at, not with the
    clock: set to 08:31 (sent), then back to 09:00 before 09:00, one more silent
    heartbeat goes out at 09:00. A time at or before 08:31 sends nothing more today."""
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    env.at = "08:31"
    await env.tick_at(at("2026-10-09 08:31"))
    assert len(env.sender.sent) == 1
    env.at = "08:00"  # already passed, and before 08:31: nothing more today
    await env.tick_at(at("2026-10-09 08:33"))
    env.at = "09:00"  # later than 08:31: one more, at 09:00
    await env.tick_at(at("2026-10-09 08:35"))
    assert len(env.sender.sent) == 1
    await env.tick_at(at("2026-10-09 09:00"))
    await env.tick_at(at("2026-10-09 09:05"))
    assert len(env.sender.sent) == 2
    assert env.sender.silent == [True, True]
    env.at = "12:00"  # later still, and already passed: one more at once
    await env.tick_at(at("2026-10-09 15:00"))
    await env.tick_at(at("2026-10-09 15:05"))
    assert len(env.sender.sent) == 3


class FlakyKvStore(Store):
    fail = 0

    def kv_set(self, key: str, value: str) -> None:
        if self.fail:
            self.fail -= 1
            raise sqlite3.OperationalError("database is locked")
        super().kv_set(key, value)


async def test_a_heartbeat_whose_slot_write_failed_is_not_sent_again(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = FlakyKvStore(tmp_dir / "state.db")
    sender = FakeSender()
    health = Health(store, sender, clock=lambda: T0, tz=UTC)
    heartbeat = Heartbeat(store, sender, health, at=lambda: "09:00", projects=lambda: 1, tz=UTC)
    store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    store.fail = 2
    start = at("2026-10-09 09:00")
    for second in range(0, 30, 5):
        await heartbeat.tick(start + second)
    assert len(sender.sent) == 1
    assert store.kv_get(HEARTBEAT_KEY) == "2026-10-09 09:00"
    unrecorded = [r for r in caplog.records if "not recorded" in r.getMessage()]
    assert len(unrecorded) == 1
    assert "OperationalError" in unrecorded[0].getMessage()
    await heartbeat.tick(at("2026-10-10 09:00"))
    assert len(sender.sent) == 2


# -- the heartbeat while Telegram is not set up (bot onboarding spec §1) ------------


async def test_no_heartbeat_and_no_slot_write_while_not_set_up(env: Env) -> None:
    env.configured = False
    await env.tick_at(at("2026-10-09 15:00"))
    assert env.store.kv_get(HEARTBEAT_KEY) is None  # not even the first-run slot
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    await env.tick_at(at("2026-10-09 15:05"))
    assert env.sender.sent == []
    assert env.store.kv_get(HEARTBEAT_KEY) == "2026-10-08 09:00"


async def test_setup_past_todays_time_waits_for_tomorrows_heartbeat(env: Env) -> None:
    env.configured = False
    await env.tick_at(at("2026-10-09 15:00"))
    env.configured = True
    env.heartbeat.mark_done_if_past(at("2026-10-09 15:01"))
    assert env.store.kv_get(HEARTBEAT_KEY) == "2026-10-09 09:00"
    await env.tick_at(at("2026-10-09 15:02"))
    assert env.sender.sent == []
    await env.tick_at(at("2026-10-10 09:00"))
    assert len(heartbeats(env)) == 1


async def test_setup_before_todays_time_changes_nothing(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-08 09:00")
    env.heartbeat.mark_done_if_past(at("2026-10-09 08:00"))
    assert env.store.kv_get(HEARTBEAT_KEY) == "2026-10-08 09:00"
    await env.tick_at(at("2026-10-09 09:00"))
    assert len(heartbeats(env)) == 1


async def test_setup_never_moves_a_slot_back(env: Env) -> None:
    env.store.kv_set(HEARTBEAT_KEY, "2026-10-09 09:00")
    env.heartbeat.mark_done_if_past(at("2026-10-09 15:00"))
    assert env.store.kv_get(HEARTBEAT_KEY) == "2026-10-09 09:00"


def test_mark_done_does_nothing_while_the_heartbeat_is_off(tmp_dir: Path) -> None:
    env = Env(tmp_dir, at_time=None)
    env.heartbeat.mark_done_if_past(at("2026-10-09 15:00"))
    assert env.store.kv_get(HEARTBEAT_KEY) is None


def test_mark_done_is_best_effort(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret-bearing detail")

    env.store.kv_set = boom  # type: ignore[method-assign]
    env.heartbeat.mark_done_if_past(at("2026-10-09 15:00"))  # must not raise
    assert "RuntimeError" in caplog.text
    assert "secret-bearing detail" not in caplog.text


# -- the Mac's name (ADR 0023) -------------------------------------------------------------


def test_the_restart_messages_carry_the_signature() -> None:
    crashed = {"running": True, "updated_at": T0 - 60, "last_started_at": T0 - 3600}
    failed = {"running": True, "updated_at": T0 - 60, "failed_start": True}
    sig = "Diffbell (Work MBP)"
    assert restart_message(crashed, T0, UTC, brand=sig) == (
        "⚠️ Diffbell (Work MBP) restarted after an unexpected stop (down 09:59–10:00)"
    )
    assert restart_message(failed, T0, UTC, brand=sig) == (
        "⚠️ Diffbell (Work MBP) could not start earlier (09:59–10:00)"
    )


def test_the_heartbeat_text_carries_the_signature() -> None:
    assert heartbeat_text(2, 1, [], brand="Diffbell (Work MBP)") == (
        "✅ Diffbell (Work MBP) · watching 2 projects · 1 change yesterday · all OK"
    )
    assert heartbeat_text(2, 1, []).startswith("✅ Diffbell · watching")


async def test_the_heartbeat_is_signed_with_the_mac_read_at_send_time(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    name = ["Work MBP"]
    env.heartbeat = Heartbeat(
        env.store,
        env.sender,
        env.health,
        at=lambda: env.at,
        projects=lambda: env.projects,
        tz=UTC,
        signature=lambda: f"Diffbell ({name[0]})",
    )
    await env.tick_at(at("2026-10-09 08:00", UTC))
    name[0] = "Home Mini"
    await env.tick_at(at("2026-10-09 09:00", UTC))
    assert env.texts() == [
        "✅ Diffbell (Home Mini) · watching 3 projects · 0 changes yesterday · all OK"
    ]


async def test_the_restart_notice_is_queued_with_the_signature(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    health = Health(store, FakeSender(), clock=lambda: T0, tz=UTC)
    crashed = {"running": True, "updated_at": T0 - 60, "last_started_at": T0 - 3600}
    queue_restart_notice(store, health, crashed, T0, UTC, brand="Diffbell (Work MBP)")
    problem = store.get_problem(RESTART_KEY)
    assert problem is not None and problem.message.startswith("⚠️ Diffbell (Work MBP) restarted")
