"""Live preview of the files a pattern set matches (web UI spec §3, W10).

Computed locally: an ``os.scandir`` walk in a worker thread plus the same glob
matcher the daemon's redaction uses. It never touches Watchman (no queries on the
daemon's single connection, no new roots) and never opens a file: only names.

Parity with ``patterns.build_expression`` (checked against real Watchman by
``tests/integration/test_preview_parity.py``):
- candidates are regular files and symlinks (Watchman ``type f`` / ``type l``),
  including symlinks to directories, which are never descended into;
- hidden entries are walked like any other (the terms use ``includedotfiles``);
- a directory is pruned only when everything beneath it is provably excluded: its
  name is one of ``ALWAYS_EXCLUDED_DIRS``, or a user ignore pattern containing
  "/" and ending in "/**" matches a child probe ``D + "/_"``;
- everything else is evaluated per file, exactly like the expression.

The walk is targeted (Plan 6 spec §5): when every watched pattern contains "/", it
starts only where a pattern can match (``plan_starts``): a literal path's parent
folder, an anchored glob's literal prefix. One pattern without "/" walks the whole
tree, as before. The matched set and each redacted flag stay equal to the whole-tree
walk's (``tests/web/test_preview_targeted.py`` keeps that walk as its oracle).
"""

from __future__ import annotations

import asyncio
import functools
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diffbell.config import (
    ALWAYS_EXCLUDED_DIRS,
    Config,
    ProjectConfig,
    ResolvedProject,
    check_pattern,
    effective_lists,
    redact_patterns,
)
from diffbell.patterns import is_redacted, matches

MAX_FILES = 500
MAX_SCANNED = 100_000
MAX_SECONDS = 3.0
CONCURRENCY = 2

_scandir = os.scandir  # a seam for tests (a directory that vanishes mid-walk)


class PreviewError(Exception):
    """Unusable preview input (bad path or pattern). The message is user-facing."""


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    path: str
    patterns: list[str]
    ignore: list[str]
    use_defaults: bool


@dataclass(frozen=True, slots=True)
class PreviewFile:
    path: str
    redacted: bool


@dataclass(frozen=True, slots=True)
class PreviewResult:
    files: list[PreviewFile]
    total: int
    truncated: bool  # the file list is incomplete (the file cap, or the walk stopped)
    # False only when the walk stopped at its entry or time cap: `total` is then a
    # lower bound ("N+"). The file cap alone keeps counting, so `total` stays exact.
    total_exact: bool = True


@dataclass(frozen=True, slots=True)
class Limits:
    max_files: int = MAX_FILES
    max_scanned: int = MAX_SCANNED
    max_seconds: float = MAX_SECONDS


DEFAULT_LIMITS = Limits()


def check_path(raw: str, home: Path) -> Path:
    """The realpath of an existing directory under ``home`` (not ``home`` itself).
    No message names the typed path (no input values in 422 messages)."""
    if not raw.startswith(("/", "~")):
        raise PreviewError("the path must be absolute or start with ~")
    if "\0" in raw:
        raise PreviewError("the path must not contain a NUL byte")
    try:
        real = Path(os.path.realpath(Path(raw).expanduser()))
        home_real = Path(os.path.realpath(home))
        is_dir = real.is_dir()
    except OSError as exc:  # ELOOP, ENAMETOOLONG, …: the reason, not the path again
        reason = exc.strerror or type(exc).__name__
        raise PreviewError(f"the folder cannot be read: {reason}") from None
    except (ValueError, RuntimeError) as exc:
        raise PreviewError(f"the folder cannot be read: {type(exc).__name__}") from None
    if not is_dir:
        raise PreviewError("not an existing directory under your home folder")
    if real == home_real or home_real not in real.parents:
        raise PreviewError("the path must be a directory inside your home directory")
    return real


def build_project(root: Path, request: PreviewRequest, config: Config) -> ResolvedProject:
    """The ResolvedProject the daemon would build for this unsaved input."""
    errors = [msg for p in (*request.patterns, *request.ignore) if (msg := check_pattern(p))]
    if errors:
        raise PreviewError("\n".join(errors))
    unsaved = ProjectConfig(
        path=str(root),
        patterns=request.patterns,
        ignore=request.ignore,
        use_defaults=request.use_defaults,
    )
    watched, excluded = effective_lists(config, unsaved)
    if not watched:
        raise PreviewError("nothing to watch (use_defaults is off and patterns is empty)")
    return ResolvedProject(root.name, root, watched, excluded)


def _entries(directory: Path) -> Iterator[os.DirEntry[str]]:
    """The directory's entries; an unreadable or vanishing directory yields what it
    could (Watchman can't crawl it either)."""
    try:
        with _scandir(directory) as it:
            yield from it
    except OSError:
        return


def _selected(rel: str, project: ResolvedProject) -> bool:
    """The local equivalent of ``build_expression`` for one candidate path."""
    if not any(matches(rel, p) for p in project.watched):
        return False
    if any(matches(rel, p) for p in project.excluded):
        return False
    return not any(matches(rel, f"**/{d}/**") for d in ALWAYS_EXCLUDED_DIRS)


def _pruned(rel_dir: str, prune_ignores: list[str]) -> bool:
    """True only if everything beneath the directory is excluded. The child probe
    ``rel_dir + "/_"`` is needed: "examples/**" compiles to ``examples/.*``, which
    does not match "examples" itself."""
    probe = rel_dir + "/_"
    if any(matches(probe, f"**/{d}/**") for d in ALWAYS_EXCLUDED_DIRS):
        return True
    return any(matches(probe, p) for p in prune_ignores)


# The characters the matcher treats as special, plus "{" (literal for the matcher,
# but a brace in a pattern only ever widens the walk). A pattern component holding
# none of them is a literal name.
GLOB_CHARS = frozenset("*?[{")


@dataclass(frozen=True, slots=True)
class Start:
    """Where a pattern can match: ``parts`` are the literal folder names from the
    project root (resolved against the disk with the matcher's name comparison);
    ``descend`` walks everything below, else only the folder's own entries."""

    parts: tuple[str, ...]
    descend: bool


def _is_literal(part: str) -> bool:
    return not any(c in GLOB_CHARS for c in part)


def plan_starts(watched: tuple[str, ...]) -> list[Start] | None:
    """The starting points for ``watched``, or None for one whole-tree walk.

    - no "/" ("*.lock", "package.json"): matches at any depth: the whole tree;
    - no glob character ("src/a/main.py"): its parent folder's entries only;
    - otherwise ("src/**/x.py", "docs/*.md"): below its literal prefix (an empty
      prefix, as in "*/x.json", is the whole tree)."""
    starts: list[Start] = []
    for pattern in watched:
        if "/" not in pattern:
            return None
        parts = pattern.split("/")
        if all(_is_literal(part) for part in parts):
            starts.append(Start(tuple(parts[:-1]), descend=False))
            continue
        prefix: list[str] = []
        for part in parts:
            if not _is_literal(part):
                break
            prefix.append(part)
        if not prefix:
            return None
        starts.append(Start(tuple(prefix), descend=True))
    return starts


def _join(rel_dir: str, name: str) -> str:
    return f"{rel_dir}/{name}" if rel_dir else name


def _inside(rel: str, folder: str) -> bool:
    return folder == "" or rel == folder or rel.startswith(folder + "/")


def _dedupe(resolved: set[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Drop a start that a descending start already covers (itself, or a folder
    inside it); sorted, so the walk order is stable."""
    descending = [rel for rel, descend in resolved if descend]
    kept = []
    for rel, descend in sorted(resolved):
        covered = any(_inside(rel, other) and (other != rel or not descend) for other in descending)
        if not covered:
            kept.append((rel, descend))
    return kept


class _Walk:
    """One scan's state: the limits, the counters and the files found."""

    def __init__(
        self,
        project: ResolvedProject,
        limits: Limits,
        clock: Callable[[], float],
    ) -> None:
        self.project = project
        self.limits = limits
        self.clock = clock
        self.deadline = clock() + limits.max_seconds
        self.prune_ignores = [p for p in project.excluded if "/" in p and p.endswith("/**")]
        self.found: list[str] = []
        self.total = self.scanned = 0
        self.truncated = self.stopped = False  # past the file cap the walk goes on
        self._folders: dict[str, list[tuple[str, bool]]] = {}

    def _path(self, rel_dir: str) -> Path:
        return self.project.path / rel_dir if rel_dir else self.project.path

    def _tick(self) -> bool:
        """Count one entry; False (and stop) once a limit is reached."""
        self.scanned += 1
        if self.scanned > self.limits.max_scanned:
            self.truncated = self.stopped = True
            return False
        return self._in_time()

    def _in_time(self) -> bool:
        """False (and stop) once the deadline has passed; counts nothing."""
        if self.clock() > self.deadline:
            self.truncated = self.stopped = True
            return False
        return True

    def _folders_in(self, rel_dir: str) -> list[tuple[str, bool]]:
        """(name, is a real folder) for each entry, listed once per scan. A symlink
        is never a folder here: it is never descended into."""
        if rel_dir not in self._folders:
            entries: list[tuple[str, bool]] = []
            for entry in _entries(self._path(rel_dir)):
                if not self._tick():
                    break
                try:
                    entries.append((entry.name, entry.is_dir(follow_symlinks=False)))
                except OSError:
                    continue
            self._folders[rel_dir] = entries
        return self._folders[rel_dir]

    def resolve(self, start: Start) -> list[str]:
        """The real folders ``start.parts`` names. Each name is compared with the
        matcher's rule (case and Unicode form ignored), never by ``stat``. A missing
        name, a symlink, a file or a pruned folder ends that start."""
        current = [""]
        for part in start.parts:
            following: list[str] = []
            for rel_dir in current:
                for name, is_folder in self._folders_in(rel_dir):
                    # A cached listing ticks no entry: check the deadline here too.
                    if self.stopped or not self._in_time():
                        return []
                    rel = _join(rel_dir, name)
                    if is_folder and matches(name, part) and not _pruned(rel, self.prune_ignores):
                        following.append(rel)
            current = following
        return current

    def walk(self, rel_start: str, descend: bool) -> None:
        """Today's walk from ``rel_start``; below it only if ``descend``."""
        stack = [rel_start]
        while stack and not self.stopped:
            rel_dir = stack.pop()
            for entry in _entries(self._path(rel_dir)):
                if not self._tick():
                    break
                rel = _join(rel_dir, entry.name)
                try:
                    if entry.is_symlink():
                        candidate = True  # Watchman reports it as type "l"; never followed
                    elif entry.is_dir(follow_symlinks=False):
                        if descend and not _pruned(rel, self.prune_ignores):
                            stack.append(rel)
                        continue
                    else:
                        candidate = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if candidate and _selected(rel, self.project):
                    self.total += 1
                    if len(self.found) < self.limits.max_files:
                        self.found.append(rel)
                    else:
                        self.truncated = True


def scan(
    project: ResolvedProject,
    redact: tuple[str, ...],
    limits: Limits = DEFAULT_LIMITS,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> PreviewResult:
    """Walk ``project.path`` where the watched patterns can match (synchronous; run
    it in a worker thread). The matched files and their redacted flags equal a
    whole-tree walk's: targeting only skips folders where no pattern can match."""
    state = _Walk(project, limits, clock)
    starts = plan_starts(project.watched)
    if starts is None:
        targets = [("", True)]
    else:
        resolved: set[tuple[str, bool]] = set()
        for start in starts:
            resolved.update((rel, start.descend) for rel in state.resolve(start))
        targets = _dedupe(resolved)
    for rel_start, descend in targets:
        if state.stopped:
            break
        state.walk(rel_start, descend)
    files = [PreviewFile(rel, is_redacted(rel, redact)) for rel in sorted(state.found)]
    return PreviewResult(files, state.total, state.truncated, total_exact=not state.stopped)


def run(
    request: PreviewRequest, config: Config, home: Path, limits: Limits = DEFAULT_LIMITS
) -> PreviewResult:
    """Validate the input and walk (synchronous; run it in a worker thread). The
    path checks and pattern compilation run here too, so a pathological input
    can't block the event loop."""
    root = check_path(request.path, home)
    project = build_project(root, request, config)
    return scan(project, redact_patterns(config), limits)


class Previewer:
    """Runs previews, tree listings and dashboard counts (validation and walk) off
    the event loop, at most ``concurrency`` at a time. Work in ``to_thread`` can't
    be cancelled (only its deadline stops it), so a walk keeps its slot until its
    thread finishes, even if the awaiting request is cancelled."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        limits: Limits = DEFAULT_LIMITS,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self._home = home
        self._limits = limits
        self._semaphore = asyncio.Semaphore(concurrency)
        # Dashboard counts run one at a time (web/counts.py). The gate lives here, not
        # in WatchedCounts: after a web restart a new WatchedCounts shares this
        # Previewer, and an orphaned count plus a new one must not hold both slots.
        # (asyncio.Semaphore binds to the running loop on first use.)
        self.count_gate = asyncio.Semaphore(1)

    @property
    def home(self) -> Path:
        return self._home or Path.home()

    @property
    def limits(self) -> Limits:
        return self._limits

    async def preview(self, request: PreviewRequest, config: Config) -> PreviewResult:
        return await self.in_slot(run, request, config, self.home, self._limits)

    async def in_slot[**P, T](self, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Run ``fn`` in a worker thread once one of the slots is free."""
        return await self._in_slot(functools.partial(fn, *args, **kwargs), None)

    async def in_gated_slot[**P, T](
        self, gate: asyncio.Semaphore, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        """Like ``in_slot``, holding ``gate`` too. Once the walk has started, the gate
        is released when its thread ends, not when the caller is cancelled."""
        return await self._in_slot(functools.partial(fn, *args, **kwargs), gate)

    async def _in_slot[T](self, call: Callable[[], T], gate: asyncio.Semaphore | None) -> T:
        if gate is not None:
            await gate.acquire()
        try:
            await self._semaphore.acquire()
        except BaseException:
            if gate is not None:
                gate.release()
            raise
        try:
            walk = asyncio.ensure_future(asyncio.to_thread(call))
        except BaseException:
            self._semaphore.release()
            if gate is not None:
                gate.release()
            raise
        walk.add_done_callback(functools.partial(self._walk_done, gate))
        # shield: cancelling this request must not abandon the slot while the thread runs.
        return await asyncio.shield(walk)

    def _walk_done(self, gate: asyncio.Semaphore | None, walk: asyncio.Future[Any]) -> None:
        self._semaphore.release()
        if gate is not None:
            gate.release()
        if not walk.cancelled():
            walk.exception()  # retrieved, so an orphaned walk's error isn't logged as lost
