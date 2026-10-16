"""The shared "has the restarted daemon come up?" wait (`install`, `upgrade`): only
status.json files in a tmp state dir, a fake clock and sleep."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from diffbell import liveness
from diffbell.liveness import (
    READY_WAIT_SECONDS,
    not_ready,
    started_after,
    wait_until_ready,
    write_status_file,
)

STARTED = 1_800_000_000.0


def ready(**changes: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "pid": 4242,
        "running": True,
        "last_started_at": STARTED + 1,
        "connected": True,
        "projects": [
            {"name": "a", "status": "subscribed"},
            {"name": "gone", "status": "missing"},
        ],
        "web": {"enabled": True, "running": True, "port": 8765, "last_error": None},
    }
    status.update(changes)
    return status


def test_a_fully_started_daemon_is_ready() -> None:
    assert not_ready(ready(), STARTED) == []
    assert started_after(ready(), STARTED)


@pytest.mark.parametrize(
    "status",
    [
        None,
        {},
        ready(last_started_at=STARTED),  # the previous run's start
        ready(last_started_at=STARTED - 5),
        ready(last_started_at=True),  # a bool is not a time
        ready(last_started_at="1900000000"),
    ],
)
def test_no_new_start_is_named_alone(status: dict[str, Any] | None) -> None:
    assert not started_after(status, STARTED)
    assert not_ready(status, STARTED) == ["no new start reported"]


def test_a_failed_start_carries_its_reason() -> None:
    status = {
        "running": True,
        "failed_start": True,
        "last_started_at": STARTED - 60,
        "last_failure": "no bot token in the Keychain",
    }
    assert not_ready(status, STARTED) == [
        "no new start reported (last failure: no bot token in the Keychain)"
    ]


def test_a_daemon_that_stopped_again_is_not_ready() -> None:
    status = ready(running=False, last_failure="stopped unexpectedly")
    assert not_ready(status, STARTED) == [
        "the daemon stopped after starting (last failure: stopped unexpectedly)"
    ]


def test_watchman_unreachable_and_pending_subscriptions_are_named_plainly() -> None:
    status = ready(
        connected=False,
        projects=[
            {"name": "a", "status": "pending"},
            {"name": "b", "status": "pending"},
            {"name": "c", "status": "subscribed"},
        ],
    )
    assert not_ready(status, STARTED) == [
        "not connected to Watchman (is it running?)",
        "subscriptions not done yet: a, b",
    ]


def test_an_error_project_does_not_block_readiness() -> None:
    # Subscribing was tried and failed: doctor reports it; waiting won't help.
    status = ready(projects=[{"name": "a", "status": "error", "error": "boom"}])
    assert not_ready(status, STARTED) == []


def test_a_web_ui_that_is_not_up_is_named_with_its_error() -> None:
    web = {"enabled": True, "running": False, "port": 8765, "last_error": "address in use"}
    assert not_ready(ready(web=web), STARTED) == [
        "web UI not running on port 8765 (address in use)"
    ]
    web = {"enabled": True, "running": False, "port": 8765, "last_error": None}
    assert not_ready(ready(web=web), STARTED) == ["web UI not running on port 8765"]


def test_a_disabled_or_absent_web_block_is_fine() -> None:
    assert not_ready(ready(web={"enabled": False, "running": False}), STARTED) == []
    status = ready()
    del status["web"]
    assert not_ready(status, STARTED) == []


class Clock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []
        self.on_sleep: list[Any] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep:
            self.on_sleep.pop(0)()


def test_wait_returns_at_once_when_ready(tmp_dir: Path) -> None:
    write_status_file(tmp_dir, ready())
    clock = Clock()
    result = wait_until_ready(tmp_dir, STARTED, monotonic=clock.monotonic, sleep=clock.sleep)
    assert result.ready and result.missing == []
    assert result.snapshot is not None and result.snapshot["pid"] == 4242
    assert clock.sleeps == []


def test_wait_polls_until_the_daemon_is_ready(tmp_dir: Path) -> None:
    write_status_file(tmp_dir, ready(connected=False, last_started_at=STARTED - 9))
    clock = Clock()
    clock.on_sleep = [
        lambda: write_status_file(tmp_dir, ready(connected=False)),
        lambda: write_status_file(tmp_dir, ready()),
    ]
    result = wait_until_ready(tmp_dir, STARTED, monotonic=clock.monotonic, sleep=clock.sleep)
    assert result.ready
    assert clock.sleeps == [0.5, 0.5]


def test_wait_reports_what_is_missing_at_the_timeout(tmp_dir: Path) -> None:
    write_status_file(tmp_dir, ready(projects=[{"name": "a", "status": "pending"}]))
    clock = Clock()
    result = wait_until_ready(tmp_dir, STARTED, monotonic=clock.monotonic, sleep=clock.sleep)
    assert not result.ready
    assert result.missing == ["subscriptions not done yet: a"]
    assert clock.t - 100.0 == pytest.approx(READY_WAIT_SECONDS)


def test_wait_without_any_status_json(tmp_dir: Path) -> None:
    clock = Clock()
    result = wait_until_ready(tmp_dir, STARTED, monotonic=clock.monotonic, sleep=clock.sleep)
    assert result.snapshot is None and result.missing == ["no new start reported"]


def test_the_timeout_is_thirty_seconds() -> None:
    assert READY_WAIT_SECONDS == 30.0
    assert liveness.READY_WAIT_SECONDS is READY_WAIT_SECONDS
