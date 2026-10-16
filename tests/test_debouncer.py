from __future__ import annotations

import asyncio

from diffbell.debouncer import Debouncer, run_debouncer
from diffbell.models import Change, ChangeKind


def ch(id_: int, project: str = "p") -> Change:
    return Change(id_, project, f"f{id_}", ChangeKind.MODIFIED)


def test_flushes_after_quiet_window() -> None:
    d = Debouncer(quiet_seconds=10, max_wait_seconds=60)
    d.add([ch(1)], now=0)
    assert d.due(now=9.9) == []
    assert d.due(now=10) == ["p"]
    assert [c.id for c in d.pop("p")] == [1]
    assert d.due(now=100) == []


def test_new_changes_extend_quiet_window() -> None:
    d = Debouncer(quiet_seconds=10, max_wait_seconds=60)
    d.add([ch(1)], now=0)
    d.add([ch(2)], now=8)
    assert d.due(now=12) == []
    assert d.due(now=18) == ["p"]


def test_max_wait_caps_a_continuous_stream() -> None:
    d = Debouncer(quiet_seconds=10, max_wait_seconds=60)
    for t in range(0, 70, 5):
        d.add([ch(t + 1)], now=t)
        if t < 60:
            assert d.due(now=t) == []
    assert d.due(now=60) == ["p"]


def test_projects_are_independent() -> None:
    d = Debouncer(quiet_seconds=10, max_wait_seconds=60)
    d.add([ch(1, "a")], now=0)
    d.add([ch(2, "b")], now=5)
    assert d.due(now=10) == ["a"]
    d.pop("a")
    assert d.due(now=15) == ["b"]


def test_empty_add_is_ignored() -> None:
    d = Debouncer(quiet_seconds=1, max_wait_seconds=2)
    d.add([], now=0)
    assert d.due(now=100) == []
    assert d.pop("a") == []


async def test_run_loop_flushes_due_batches() -> None:
    d = Debouncer(quiet_seconds=0.05, max_wait_seconds=1)
    flushed: list[tuple[str, list[int]]] = []

    async def flush(project: str, changes: list[Change]) -> None:
        flushed.append((project, [c.id for c in changes]))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    d.add([ch(1), ch(2)], now=loop.time())
    task = asyncio.create_task(run_debouncer(d, flush, stop, clock=loop.time, tick=0.01))
    await asyncio.sleep(0.2)
    stop.set()
    await task
    assert flushed == [("p", [1, 2])]


async def test_run_loop_survives_flush_errors() -> None:
    d = Debouncer(quiet_seconds=0.01, max_wait_seconds=1)
    calls: list[str] = []

    async def flush(project: str, changes: list[Change]) -> None:
        calls.append(project)
        raise RuntimeError("boom")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    d.add([ch(1, "a")], now=loop.time())
    d.add([ch(2, "b")], now=loop.time())
    task = asyncio.create_task(run_debouncer(d, flush, stop, clock=loop.time, tick=0.01))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert sorted(calls) == ["a", "b"]
