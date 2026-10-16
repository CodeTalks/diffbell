"""/api/dashboard: on-demand watched counts and the last change from the store
(UI refine spec §3.2)."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from diffbell.config import ResolvedProject, load_config
from diffbell.web import preview as preview_mod
from diffbell.web.app import create_app
from diffbell.web.counts import WatchedCounts
from diffbell.web.preview import Limits, Previewer, PreviewRequest, PreviewResult
from tests.web.support import BASE, Env, log_in, make_context
from tests.web.test_app import add_event


async def project_row(client: httpx.AsyncClient, name: str = "proj") -> dict[str, object]:
    body = (await client.get("/api/dashboard")).json()
    return next(p for p in body["projects"] if p["name"] == name)


async def until_counted(client: httpx.AsyncClient) -> dict[str, object]:
    for _ in range(200):
        row = await project_row(client)
        if row["watched_count"] is not None:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("never counted")


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every count walk, by project name (the real scan still runs)."""
    seen: list[str] = []
    real: Callable[..., PreviewResult] = preview_mod.scan

    def spy(project: ResolvedProject, redact: tuple[str, ...], limits: Limits) -> PreviewResult:
        seen.append(project.name)
        return real(project, redact, limits)

    monkeypatch.setattr("diffbell.web.counts.scan", spy)
    return seen


async def test_the_count_is_null_until_the_first_walk_ends(
    env: Env, client: httpx.AsyncClient, walks: list[str]
) -> None:
    first = await project_row(client)
    assert (first["watched_count"], first["watched_exact"]) == (None, None)
    row = await until_counted(client)
    assert (row["watched_count"], row["watched_exact"]) == (2, True)  # package.json, .env.local
    assert walks == ["proj"]


async def test_no_walk_runs_without_a_dashboard_fetch(env: Env, walks: list[str]) -> None:
    transport = httpx.ASGITransport(app=create_app(make_context(env)))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await log_in(env, client)
        await client.get("/api/events")
        await client.get("/api/projects")
        await asyncio.sleep(0.05)
        assert walks == []
        await client.get("/api/dashboard")
        await asyncio.sleep(0.05)
    assert walks == ["proj"]


async def test_the_count_is_cached_for_a_minute(
    env: Env, client: httpx.AsyncClient, walks: list[str]
) -> None:
    await until_counted(client)
    (env.proj / "Makefile").write_text("all:\n")
    for _ in range(3):
        assert (await project_row(client))["watched_count"] == 2
    assert walks == ["proj"]
    env.now += 61
    stale = await project_row(client)
    assert stale["watched_count"] == 2  # served while the recount runs
    for _ in range(200):
        if (await project_row(client))["watched_count"] == 3:
            break
        await asyncio.sleep(0.01)
    assert (await project_row(client))["watched_count"] == 3
    assert walks == ["proj", "proj"]


async def test_a_changed_list_counts_again(
    env: Env, client: httpx.AsyncClient, walks: list[str]
) -> None:
    await until_counted(client)
    env.snapshot["config_version"] = "v-next"  # a new version alone keeps the count
    assert (await project_row(client))["watched_count"] == 2
    assert walks == ["proj"]
    (env.proj / "notes.txt").write_text("hi\n")
    env.config.write_text(env.config.read_text() + "    patterns: [notes.txt]\n")
    assert (await project_row(client))["watched_count"] is None
    assert (await until_counted(client))["watched_count"] == 3
    assert walks == ["proj", "proj"]


async def test_a_walk_cut_short_is_not_exact(env: Env) -> None:
    ctx = make_context(env)
    ctx = dataclasses.replace(
        ctx, previewer=preview_mod.Previewer(home=env.home, limits=Limits(max_scanned=1))
    )
    transport = httpx.ASGITransport(app=create_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await log_in(env, client)
        row = await until_counted(client)
    assert row["watched_exact"] is False


async def test_last_change_comes_from_the_store(env: Env, client: httpx.AsyncClient) -> None:
    assert (await project_row(client))["last_change_at"] is None
    add_event(env, ["package.json"])
    env.now += 30
    add_event(env, ["Makefile"])
    add_event(env, ["x"], project="other")
    assert (await project_row(client))["last_change_at"] == env.now


async def test_last_change_survives_a_restart(env: Env, client: httpx.AsyncClient) -> None:
    """A new server (a daemon restart) has a fresh snapshot without any in-memory
    time, and still reports the last change."""
    add_event(env, ["package.json"])
    transport = httpx.ASGITransport(app=create_app(make_context(env)))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as restarted:
        await log_in(env, restarted)
        assert (await project_row(restarted))["last_change_at"] == env.now


async def test_counts_run_one_at_a_time_and_leave_a_slot_free(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    started: list[str] = []

    def blocking_scan(project: ResolvedProject, *args: object) -> PreviewResult:
        started.append(project.name)
        gate.wait(5)
        return PreviewResult([], 1, False)

    monkeypatch.setattr("diffbell.web.counts.scan", blocking_scan)
    previewer = Previewer(home=env.home)
    counts = WatchedCounts(previewer, clock=lambda: env.now)
    for name in ("a", "b"):
        counts.get(ResolvedProject(name, env.proj, ("*",), ()), ())
    await asyncio.sleep(0.2)
    assert started == ["a"]  # the second count waits for the first
    # The editor's preview still gets a slot while a count runs.
    request = PreviewRequest(str(env.proj), [], [], use_defaults=True)
    result = await asyncio.wait_for(previewer.preview(request, load_config(env.config)), 5)
    assert result.total == 2
    gate.set()
    deadline = time.monotonic() + 5
    while len(started) < 2 or counts.get(ResolvedProject("b", env.proj, ("*",), ()), ()) is None:
        assert time.monotonic() < deadline, "the second count never ran"
        await asyncio.sleep(0.01)
    assert started == ["a", "b"]


async def test_counts_of_removed_projects_are_forgotten(env: Env) -> None:
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    for name in ("a", "b"):
        project = ResolvedProject(name, env.proj, ("package.json",), ())
        counts.get(project, ())
    for _ in range(200):
        if all(counts.get(ResolvedProject(n, env.proj, ("package.json",), ()), ()) for n in "ab"):
            break
        await asyncio.sleep(0.01)
    counts.retain({"a"})
    assert sorted(k[0] for k in counts._done) == ["a"]


def counted(counts: WatchedCounts, project: ResolvedProject) -> bool:
    return counts.get(project, ()) is not None


async def wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


async def test_a_changed_list_replaces_the_projects_entry(env: Env) -> None:
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    before = ResolvedProject("a", env.proj, ("package.json",), ())
    after = ResolvedProject("a", env.proj, ("package.json", "Makefile"), ())
    counts.get(before, ())
    await wait_until(lambda: counted(counts, before))
    counts.get(after, ())
    await wait_until(lambda: len(counts._done) == 1 and counted(counts, after))
    assert [k[0] for k in counts._done] == ["a"]


async def test_a_failed_count_is_not_retried_within_the_ttl(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def failing_scan(project: ResolvedProject, *args: object) -> PreviewResult:
        attempts.append(project.name)
        raise OSError("boom")

    monkeypatch.setattr("diffbell.web.counts.scan", failing_scan)
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    project = ResolvedProject("a", env.proj, ("*",), ())
    counts.get(project, ())
    await wait_until(lambda: attempts == ["a"] and not counts._running)
    for _ in range(3):  # the dashboard polls every 5 s
        env.now += 5
        assert counts.get(project, ()) is None
        await asyncio.sleep(0.02)
    assert attempts == ["a"]
    env.now += 60
    counts.get(project, ())
    await wait_until(lambda: attempts == ["a", "a"])


async def test_counts_of_two_servers_share_one_gate(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A web restart makes a new WatchedCounts on the daemon's Previewer: an orphaned
    count and a new one must still run one at a time."""
    gate = threading.Event()
    started: list[str] = []

    def blocking_scan(project: ResolvedProject, *args: object) -> PreviewResult:
        started.append(project.name)
        gate.wait(5)
        return PreviewResult([], 1, False)

    monkeypatch.setattr("diffbell.web.counts.scan", blocking_scan)
    previewer = Previewer(home=env.home)
    old = WatchedCounts(previewer, clock=lambda: env.now)
    new = WatchedCounts(previewer, clock=lambda: env.now)
    old.get(ResolvedProject("a", env.proj, ("*",), ()), ())
    new.get(ResolvedProject("b", env.proj, ("*",), ()), ())
    await asyncio.sleep(0.2)
    assert started == ["a"]
    gate.set()
    await wait_until(lambda: started == ["a", "b"])
    await wait_until(lambda: not old._running and not new._running)


async def test_a_cancelled_count_holds_the_gate_until_its_walk_ends(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a count can't stop its thread: the gate stays held until the walk
    ends, so a second count can't take the editor's slot meanwhile."""
    gate = threading.Event()
    started: list[str] = []

    def blocking_scan(project: ResolvedProject, *args: object) -> PreviewResult:
        started.append(project.name)
        gate.wait(5)
        return PreviewResult([], 1, False)

    monkeypatch.setattr("diffbell.web.counts.scan", blocking_scan)
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    counts.get(ResolvedProject("a", env.proj, ("*",), ()), ())
    await wait_until(lambda: started == ["a"])
    next(iter(counts._running.values())).cancel()
    await wait_until(lambda: not counts._running)
    counts.get(ResolvedProject("b", env.proj, ("*",), ()), ())
    await asyncio.sleep(0.2)
    assert started == ["a"]  # the cancelled count's walk still runs
    gate.set()
    await wait_until(lambda: started == ["a", "b"])
    await wait_until(lambda: not counts._running)


async def test_a_finished_count_drops_the_projects_stale_failures(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail = [True]

    def flaky_scan(project: ResolvedProject, *args: object) -> PreviewResult:
        if fail[0]:
            raise OSError("boom")
        return PreviewResult([], 1, True)

    monkeypatch.setattr("diffbell.web.counts.scan", flaky_scan)
    counts = WatchedCounts(Previewer(home=env.home), clock=lambda: env.now)
    before = ResolvedProject("a", env.proj, ("package.json",), ())
    after = ResolvedProject("a", env.proj, ("package.json", "Makefile"), ())
    other = ResolvedProject("b", env.proj, ("*",), ())
    counts.get(other, ())
    counts.get(before, ())
    await wait_until(lambda: len(counts._failed) == 2 and not counts._running)
    counts.get(after, ())  # a list edit, which fails too
    await wait_until(lambda: not counts._running)
    assert sorted(k[0] + str(len(k[2])) for k in counts._failed) == ["a2", "b1"]
    fail[0] = False
    counts.get(before, ())  # its failure was dropped, so it counts at once
    await wait_until(lambda: counted(counts, before))
    assert [k[0] for k in counts._failed] == ["b"]
