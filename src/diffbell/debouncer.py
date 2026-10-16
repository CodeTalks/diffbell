"""Per-project batching of journaled changes (spec §5 step 6)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from diffbell.models import Change

log = logging.getLogger(__name__)

FlushFn = Callable[[str, list[Change]], Awaitable[None]]


@dataclass
class _Bucket:
    first_at: float
    last_at: float
    changes: list[Change] = field(default_factory=list)


class Debouncer:
    def __init__(self, quiet_seconds: float, max_wait_seconds: float) -> None:
        self._quiet = quiet_seconds
        self._max_wait = max_wait_seconds
        self._buckets: dict[str, _Bucket] = {}

    def configure(self, quiet_seconds: float, max_wait_seconds: float) -> None:
        self._quiet = quiet_seconds
        self._max_wait = max_wait_seconds

    def add(self, changes: Iterable[Change], now: float) -> None:
        for change in changes:
            bucket = self._buckets.get(change.project)
            if bucket is None:
                bucket = self._buckets[change.project] = _Bucket(first_at=now, last_at=now)
            bucket.last_at = now
            bucket.changes.append(change)

    def due(self, now: float) -> list[str]:
        return [
            project
            for project, b in self._buckets.items()
            if now - b.last_at >= self._quiet or now - b.first_at >= self._max_wait
        ]

    def pop(self, project: str) -> list[Change]:
        bucket = self._buckets.pop(project, None)
        return bucket.changes if bucket else []


async def run_debouncer(
    debouncer: Debouncer,
    flush: FlushFn,
    stop: asyncio.Event,
    *,
    clock: Callable[[], float],
    tick: float = 0.25,
) -> None:
    """Flush due batches until ``stop`` is set. A failing flush is logged, never fatal:
    its journal rows stay unflushed and are retried after a restart."""
    while not stop.is_set():
        for project in debouncer.due(clock()):
            changes = debouncer.pop(project)
            try:
                await flush(project, changes)
            except Exception:
                log.exception("flush failed for project %s", project)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick)
