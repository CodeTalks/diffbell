"""The dashboard's "watching N files" per project (UI refine spec §3.2).

Counted on demand: only a dashboard fetch starts a count, never a timer, so with
no tab open no walk runs. A count is the preview walk (``preview.scan``) of the
project's effective lists, in one of the Previewer's slots, cached per (project,
the lists walked) for TTL seconds. The fetch never waits for it: until the first
count of a key finishes the value is unknown (None), and a stale value is served
while it is recounted. A count that fails is not retried for TTL seconds either.
While a project's watch is lost the dashboard ``forget``s its count (diff
readability spec §4.5), a count in flight included, and starts none.
Counts run one at a time (the Previewer's ``count_gate``, shared across web
restarts), so of its two slots one always stays free for the editor's preview and
tree.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from diffbell.config import ResolvedProject
from diffbell.web.preview import Previewer, PreviewResult, scan

TTL = 60.0

log = logging.getLogger(__name__)


# What a count walks: name, path, watched, excluded, redact.
Key = tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Count:
    total: int
    exact: bool
    at: float


class WatchedCounts:
    def __init__(
        self,
        previewer: Previewer,
        *,
        ttl: float = TTL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._previewer = previewer
        self._ttl = ttl
        self._clock = clock
        self._done: dict[Key, Count] = {}
        self._failed: dict[Key, float] = {}  # when the last count of a key failed
        # Not pruned by retain(): an entry only lives while its walk runs, and
        # finished() removes it then. forget() detaches it earlier.
        self._running: dict[Key, asyncio.Task[PreviewResult]] = {}

    def retain(self, names: set[str]) -> None:
        """Forget the counts of projects no longer configured."""
        for table in (self._done, self._failed):
            for key in [k for k in table if k[0] not in names]:
                del table[key]

    def forget(self, name: str) -> None:
        """Drop everything known about one project's count: its cached and failed
        counts (as ``retain`` does for a removed project) and a count in flight,
        which is cancelled (its walk, if already started, still holds the gate until
        its thread ends) and whose result is never stored: finished() stores only
        for the task still registered under its key. A no-op for an unknown name."""
        for table in (self._done, self._failed):
            for key in [k for k in table if k[0] == name]:
                del table[key]
        for key in [k for k in self._running if k[0] == name]:
            self._running.pop(key).cancel()

    def get(self, project: ResolvedProject, redact: tuple[str, ...]) -> Count | None:
        """The latest count of exactly these lists (None while the first one runs).
        Starts a count when there is none or it is older than the TTL, unless one
        failed within the TTL."""
        key: Key = (project.name, str(project.path), project.watched, project.excluded, redact)
        now = self._clock()
        cached = self._done.get(key)
        failed_at = self._failed.get(key)
        if (
            (cached is None or now - cached.at >= self._ttl)
            and key not in self._running
            and (failed_at is None or now - failed_at >= self._ttl)
        ):
            self._start(key, project, redact)
        return cached

    def _start(self, key: Key, project: ResolvedProject, redact: tuple[str, ...]) -> None:
        task = asyncio.ensure_future(self._count(project, redact))
        self._running[key] = task

        def finished(done: asyncio.Task[PreviewResult]) -> None:
            if self._running.get(key) is not done:
                return  # forgotten meanwhile: store nothing, leave a newer count alone
            del self._running[key]
            if done.cancelled():
                return
            # A failure of the project's earlier lists is stale once these finish.
            for old in [k for k in self._failed if k[0] == key[0] and k != key]:
                del self._failed[old]
            if (error := done.exception()) is not None:
                log.warning("counting %s failed (%s)", key[0], type(error).__name__)
                self._failed[key] = self._clock()
                return
            result = done.result()
            self._failed.pop(key, None)
            # Keep only this key's entry for the project: an edit replaces it.
            for old in [k for k in self._done if k[0] == key[0] and k != key]:
                del self._done[old]
            self._done[key] = Count(result.total, result.total_exact, self._clock())

        task.add_done_callback(finished)

    async def _count(self, project: ResolvedProject, redact: tuple[str, ...]) -> PreviewResult:
        # The gate is held until the walk's thread ends, even if this task is
        # cancelled first: otherwise the next count could take the editor's slot.
        previewer = self._previewer
        return await previewer.in_gated_slot(
            previewer.count_gate, scan, project, redact, previewer.limits
        )
