"""No stale count after a lost watch (diff readability spec §4.5, §7 "Counts"): while a
project's `project:<name>` problem is open the dashboard forgets its count, a count
in flight included, and starts none; the first poll after it resolves recounts."""

from __future__ import annotations

import asyncio
import threading
from zoneinfo import ZoneInfo

import httpx
import pytest

from diffbell.config import ResolvedProject
from diffbell.health import Health
from diffbell.web.counts import WatchedCounts
from diffbell.web.preview import Previewer, PreviewResult
from tests.fakes import FakeSender
from tests.web.support import Env
from tests.web.test_app_dashboard import project_row, until_counted, wait_until, walks

__all__ = ["walks"]  # the fixture, used by name below


def health(env: Env) -> Health:
    return Health(env.store, FakeSender(), clock=lambda: env.now, tz=ZoneInfo("UTC"))


async def test_a_lost_watch_hides_the_count_until_it_is_back(
    env: Env, client: httpx.AsyncClient, walks: list[str]
) -> None:
    assert (await until_counted(client))["watched_count"] == 2
    h = health(env)
    h.open("project:proj", "serious", "project proj: watch lost: x", project="proj")
    for _ in range(3):  # every poll while it is open: no count, no new walk
        row = await project_row(client)
        assert (row["watched_count"], row["watched_exact"]) == (None, None)
    env.now += 120  # past the TTL: a count would be due, but none starts
    assert (await project_row(client))["watched_count"] is None
    await asyncio.sleep(0.05)
    assert walks == ["proj"]
    h.resolve("project:proj")
    first = await project_row(client)  # the first poll after: "counting…"
    assert first["watched_count"] is None
    assert (await until_counted(client))["watched_count"] == 2
    assert walks == ["proj", "proj"]


async def test_a_resolved_problem_still_waiting_for_its_message_is_not_lost(
    env: Env, client: httpx.AsyncClient
) -> None:
    """health.current drops a resolved row whose "resolved" message is unsent: the
    watch is back, so the count shows."""
    h = health(env)
    h.open("project:proj", "serious", "project proj: watch lost: x", project="proj")
    h.resolve("project:proj")
    assert (await until_counted(client))["watched_count"] == 2


async def test_another_projects_problem_or_a_warning_keeps_the_count(
    env: Env, client: httpx.AsyncClient
) -> None:
    h = health(env)
    h.open("recrawl:proj", "warning", "project proj: Watchman had to recrawl", project="proj")
    h.open("project:other", "serious", "project other: watch lost", project="other")
    assert (await until_counted(client))["watched_count"] == 2


def blocking(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, list[str]]:
    gate = threading.Event()
    started: list[str] = []

    def scan(project: ResolvedProject, *args: object) -> PreviewResult:
        started.append(project.name)
        gate.wait(5)
        return PreviewResult([], 7, True)

    monkeypatch.setattr("diffbell.web.counts.scan", scan)
    return gate, started


async def test_a_count_in_flight_when_forgotten_never_stores_its_result(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, started = blocking(monkeypatch)
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    project = ResolvedProject("a", env.proj, ("*",), ())
    counts.get(project, ())
    await wait_until(lambda: started == ["a"])  # its walk has started
    counts.forget("a")
    assert not counts._running
    gate.set()
    await asyncio.sleep(0.2)  # the walk's thread ends
    assert counts._done == {} and counts._failed == {}


async def test_a_count_waiting_for_its_slot_is_cancelled_by_forget(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, started = blocking(monkeypatch)
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    counts.get(ResolvedProject("a", env.proj, ("*",), ()), ())
    counts.get(ResolvedProject("b", env.proj, ("*",), ()), ())  # waits for the gate
    await wait_until(lambda: started == ["a"])
    waiting = next(t for k, t in counts._running.items() if k[0] == "b")
    counts.forget("b")
    await wait_until(waiting.cancelled)
    gate.set()
    await wait_until(lambda: not counts._running)
    await asyncio.sleep(0.1)
    assert started == ["a"]  # b's walk never ran
    assert [k[0] for k in counts._done] == ["a"]


async def test_a_new_count_after_forget_is_not_disturbed_by_the_old_one(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old task's finished() must neither store its number nor drop the newer
    task registered under the same key."""
    gate, started = blocking(monkeypatch)
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    project = ResolvedProject("a", env.proj, ("*",), ())
    counts.get(project, ())
    await wait_until(lambda: started == ["a"])
    old = next(iter(counts._running.values()))
    counts.forget("a")
    counts.get(project, ())  # the problem resolved: a fresh count, same key
    newer = next(iter(counts._running.values()))
    assert newer is not old
    await wait_until(old.done)
    assert next(iter(counts._running.values())) is newer  # still registered
    gate.set()
    await wait_until(lambda: counts.get(project, ()) is not None)
    assert started == ["a", "a"]


def test_forget_of_an_unknown_name_is_a_no_op(env: Env) -> None:
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    counts.forget("nobody")
    assert (counts._done, counts._failed, counts._running) == ({}, {}, {})
