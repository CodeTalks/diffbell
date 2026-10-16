"""One folder of a project, for the editor's file tree (UI refine spec §3.2, U7).

Names only: no file is ever opened. The folder is opened component by component
from the project root with ``O_NOFOLLOW | O_DIRECTORY`` relative to the previous
descriptor, so a component swapped for a symlink between checks is refused by
the open itself, and the walk is ``os.scandir`` of that descriptor, never
following a symlink, capped at MAX_ENTRIES entries and MAX_SECONDS. Each
file's ``matched_by`` / ``ignored_by`` come from the same ``patterns.matches``
the preview uses, so the client never matches globs itself. Runs in a worker
thread through the Previewer's slots (at most two walks at a time).
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from diffbell.config import (
    ALWAYS_EXCLUDED_DIRS,
    BUILTIN_DEFAULTS,
    Config,
    ProjectConfig,
    check_pattern,
    effective_lists,
    redact_patterns,
)
from diffbell.patterns import is_redacted, matches
from diffbell.web.preview import PreviewError, check_path

MAX_ENTRIES = 2000
MAX_SECONDS = 1.0
BUILTIN_PREFIX = "built-in: "

EntryType = Literal["dir", "file", "symlink"]

_scandir = os.scandir  # a seam for tests


class BrowseError(Exception):
    """A bad ``dir`` (400). The message never contains the path."""


@dataclass(frozen=True, slots=True)
class BrowseRequest:
    path: str
    dir: str
    patterns: list[str]
    ignore: list[str]
    use_defaults: bool


@dataclass(frozen=True, slots=True)
class BrowseEntry:
    name: str
    type: EntryType
    redacted: bool
    excluded: bool
    matched_by: str | None
    ignored_by: str | None


@dataclass(frozen=True, slots=True)
class BrowseResult:
    dir: str
    entries: list[BrowseEntry]
    truncated: bool
    error: Literal["unreadable"] | None


@dataclass(frozen=True, slots=True)
class BrowseLimits:
    max_entries: int = MAX_ENTRIES
    max_seconds: float = MAX_SECONDS


DEFAULT_LIMITS = BrowseLimits()


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def always_excluded(rel_dir: str) -> bool:
    """The preview's probe (``_pruned``): case- and NFC/NFD-insensitive, like
    Watchman's match, so ``Node_Modules`` and ``.GIT`` count too."""
    probe = rel_dir + "/_"
    return any(matches(probe, f"**/{d}/**") for d in ALWAYS_EXCLUDED_DIRS)


def _refusal(parent: int, part: str) -> BrowseError | None:
    """Why ``part`` (under the open folder ``parent``) could not be opened as a
    folder: a symlink or a non-folder is the caller's mistake (BrowseError); a
    vanished or unreadable one is None. Used only after the open failed. A name
    that can't be encoded (a lone surrogate) raises ValueError, not OSError."""
    try:
        mode = os.stat(part, dir_fd=parent, follow_symlinks=False).st_mode
    except ValueError:
        return BrowseError("the folder name is not valid text")
    except OSError:
        return None
    if stat.S_ISLNK(mode):
        return BrowseError("the folder must not be a symlink")
    if not stat.S_ISDIR(mode):
        return BrowseError("the folder is not a directory")
    return None


def open_dir(root: Path, rel: str) -> int | None:
    """A descriptor of ``root / rel``, opened component by component without
    following a symlink and without entering an always-excluded folder; the
    caller closes it. None if a component vanished or can't be read; BrowseError
    for input that is wrong in itself."""
    if "\0" in rel:
        raise BrowseError("the folder must not contain a NUL byte")
    parts = rel.split("/") if rel else []
    if rel.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise BrowseError("the folder must be a relative path inside the project")
    if any(always_excluded("/".join(parts[: i + 1])) for i in range(len(parts))):
        raise BrowseError("this folder is always excluded")
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except (OSError, ValueError):
        return None
    for part in parts:
        try:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except (OSError, ValueError):  # ValueError: a name that can't be encoded
            try:
                refusal = _refusal(fd, part)
            finally:
                os.close(fd)
            if refusal is not None:
                raise refusal from None
            return None
        os.close(fd)
        fd = child
    return fd


def _first(rel: str, patterns: tuple[str, ...]) -> str | None:
    return next((p for p in patterns if matches(rel, p)), None)


def _sort_key(entry: BrowseEntry) -> tuple[bool, str, str]:
    return (entry.type != "dir", entry.name.casefold(), entry.name)


def list_dir(
    root: Path,
    rel: str,
    watched: tuple[str, ...],
    excluded: tuple[str, ...],
    redact: tuple[str, ...],
    *,
    builtin: frozenset[str] = frozenset(),
    limits: BrowseLimits = DEFAULT_LIMITS,
    clock: Callable[[], float] = time.monotonic,
) -> BrowseResult:
    """List one folder (synchronous; run it in a worker thread)."""
    fd = open_dir(root, rel)
    if fd is None:
        return BrowseResult(rel, [], False, "unreadable")
    try:
        return _list_fd(fd, rel, watched, excluded, redact, builtin, limits, clock)
    finally:
        os.close(fd)


def _list_fd(
    fd: int,
    rel: str,
    watched: tuple[str, ...],
    excluded: tuple[str, ...],
    redact: tuple[str, ...],
    builtin: frozenset[str],
    limits: BrowseLimits,
    clock: Callable[[], float],
) -> BrowseResult:
    deadline = clock() + limits.max_seconds
    entries: list[BrowseEntry] = []
    truncated = False
    try:
        with _scandir(fd) as it:
            for count, item in enumerate(it, start=1):
                if count > limits.max_entries or clock() > deadline:
                    truncated = True
                    break
                try:
                    if item.is_symlink():
                        kind: EntryType = "symlink"
                    elif item.is_dir(follow_symlinks=False):
                        kind = "dir"
                    elif item.is_file(follow_symlinks=False):
                        kind = "file"
                    else:
                        continue  # FIFOs, sockets, devices: never candidates
                except OSError:
                    continue
                path = f"{rel}/{item.name}" if rel else item.name
                matched_by = ignored_by = None
                if kind != "dir":
                    first = _first(path, watched)
                    if first is not None:
                        matched_by = BUILTIN_PREFIX + first if first in builtin else first
                    ignored_by = _first(path, excluded)
                entries.append(
                    BrowseEntry(
                        name=item.name,
                        type=kind,
                        redacted=is_redacted(path, redact),
                        excluded=kind == "dir" and always_excluded(path),
                        matched_by=matched_by,
                        ignored_by=ignored_by,
                    )
                )
    except OSError:
        return BrowseResult(rel, [], False, "unreadable")
    return BrowseResult(rel, sorted(entries, key=_sort_key), truncated, None)


def run(
    request: BrowseRequest,
    config: Config,
    home: Path,
    limits: BrowseLimits = DEFAULT_LIMITS,
) -> BrowseResult:
    """Validate like the preview (the same check_path and pattern checks) and list
    the folder. Unlike the preview, an empty watch list is fine: the tree is how
    the first file gets picked."""
    root = check_path(request.path, home)
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
    builtin = frozenset(BUILTIN_DEFAULTS) if request.use_defaults else frozenset()
    return list_dir(
        root,
        request.dir,
        watched,
        excluded,
        redact_patterns(config),
        builtin=builtin,
        limits=limits,
    )
