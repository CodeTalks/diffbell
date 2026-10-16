"""Content snapshots (content snapshots spec): the isolated diff engine, its temp
dirs, and (later tasks) capture at creation and at the baseline.

The engine runs ``git diff --no-index`` on two private temp files with no user or
system config and no attributes (spec §3.1). Every part of the isolation is
load-bearing; don't "simplify" any of them:

- the private ``HOME`` and ``XDG_CONFIG_HOME`` are the primary guard (dropping the
  user's config also drops ``core.attributesFile``, and git then falls back to
  ``$XDG_CONFIG_HOME/git/attributes``);
- ``GIT_ATTR_NOSYSTEM`` and ``core.attributesFile=/dev/null`` close the other
  attribute sources;
- ``core.autocrlf=false``: with the user's ``autocrlf=input`` a CRLF-only edit
  reads as identical, with a stderr warning;
- ``env -i``: no inherited ``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``,
  ``GIT_CEILING_DIRECTORIES`` or ``GIT_ALTERNATE_OBJECT_DIRECTORIES``;
- ``LC_ALL=C`` keeps "Binary files … differ" in English for the parser;
- git is resolved once from the daemon's PATH, and ``PATH`` is its directory only;
- the temp dir must not sit inside a work tree (a repository's own
  ``.gitattributes`` applies whatever the flags): ``check_isolation`` runs once per
  daemon start, and if it fails every report uses difflib, marked approximate.

A result is trusted only on a positive signal (``classify_engine``); anything else
is the fallback (``report`` returns None), logged with the outcome kind only.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from diffbell.diffing import (
    GIT_TIMEOUT,
    SNAPSHOT_MAX_BYTES,
    FileReport,
    ObjectFormats,
    current_branch,
    git_notes,
    parse_diff,
    read_capturable,
    split_lines,
)
from diffbell.models import Change, ChangeKind
from diffbell.patterns import is_redacted

log = logging.getLogger(__name__)

# The temp dirs live here, under the state dir (0700), never under $TMPDIR.
TEMP_DIRNAME = "snapshot-tmp"
# A leftover dir (a crash) is swept once it is this old; a running diff creates and
# removes its dir within seconds.
TEMP_SWEEP_AGE = 3600.0
ENGINE_TIMEOUT = GIT_TIMEOUT
ENGINE_ARGS: tuple[str, ...] = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "diff.algorithm=myers",
    "-c",
    "diff.indentHeuristic=true",
    "diff",
    "--no-index",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--",
    "before",
    "after",
)
APPROXIMATE = "approximate"

MiB = 1024 * 1024
# The budgets (spec §1): before plus after of one event; a project's snapshots; a
# project's stored before/after pairs. Files past them fall back.
EVENT_CONTENT_BUDGET = 16 * MiB
PROJECT_SNAPSHOT_BUDGET = 32 * MiB
PROJECT_EVENT_CONTENT_BUDGET = 64 * MiB
# At most this many files are read per baseline (the PDU pump awaits the read);
# past it a file has no snapshot and its first event falls back once.
BASELINE_MAX_FILES = 2000

Outcome = Literal["diff", "binary", "identical", "failed"]
AfterState = Literal["content", "deleted", "uncapturable"]
# check_isolation's answer: no work tree around the temp dirs, one found, or the
# check could not run (git failed to start or timed out).
Isolation = Literal["isolated", "work_tree", "unknown"]


@dataclass(frozen=True, slots=True)
class Captured:
    """One kept change as read when its event is created (spec §2.2). ``after`` is
    set only for "content"; ``note`` is the git note frozen with it (None outside
    git, and for a path that isn't capturable). ``over_budget``: "uncapturable"
    because the event budget was spent, so create_event stores neither side (a
    file that turned binary within the budget keeps its "before")."""

    path: str
    after_state: AfterState
    after: bytes | None = None
    note: str | None = None
    over_budget: bool = False

    def __post_init__(self) -> None:
        # Raised here, in the capture (the notifier then stores no contents and the
        # paths fall back), never later as an IntegrityError inside create_event,
        # which would leave the journal rows unlinked and retried at every start.
        if (self.after_state == "content") != (self.after is not None):
            raise ValueError(f"{self.after_state!r} with after={self.after is not None}")
        if self.over_budget and self.after_state != "uncapturable":
            raise ValueError(f"{self.after_state!r} marked over budget")


@dataclass(frozen=True, slots=True)
class StoredContent:
    """What one path's diff is built from: an event_contents row, or a catch-up's
    first "before" with its last "after" (spec §3.3). ``before`` None: no usable
    snapshot (or a created file)."""

    path: str
    before: bytes | None
    after: bytes | None
    after_state: AfterState
    note: str | None


@dataclass(frozen=True, slots=True)
class EventCapture:
    """What _flush_one reads before create_event: each kept, non-redacted path in
    path order, and the branch (None outside git)."""

    contents: tuple[Captured, ...]
    branch: str | None


@dataclass(frozen=True, slots=True)
class EngineOutput:
    """Stage 1: the raw engine output. ``why`` names a failed outcome's kind (for
    the log; never content)."""

    outcome: Outcome
    text: str = ""
    why: str = ""


def content_based(kind: ChangeKind, row: StoredContent) -> bool:
    """Whether a report is built from contents (spec §3.1): a modified file with a
    "before", a created one, a deleted one with a "before". Anything else falls
    back; "uncapturable" is never read as a deletion."""
    if row.after_state == "content":
        return kind is ChangeKind.CREATED or (
            row.before is not None and kind is ChangeKind.MODIFIED
        )
    if row.after_state == "deleted":
        return row.before is not None and kind is ChangeKind.DELETED
    return False


def minimal_env(git: str, home: str) -> dict[str, str]:
    """The whole environment of every engine run (``env -i``): nothing inherited."""
    return {
        "PATH": os.path.dirname(git),
        "HOME": home,
        "XDG_CONFIG_HOME": home,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
    }


def classify_engine(returncode: int, stdout: bytes, stderr: bytes) -> EngineOutput:
    """The outcome table (spec §3.1): a result is trusted only on a positive signal.
    ``--no-index`` exits 1 both for "differ" and for "could not access", and 0 with
    no output for identical sides."""
    if stderr:
        return EngineOutput("failed", why="stderr")
    text = stdout.decode("utf-8", errors="replace")
    if returncode == 0:
        return EngineOutput("identical") if not text else EngineOutput("failed", why="rc 0 output")
    if returncode != 1:
        return EngineOutput("failed", why=f"rc {returncode}")
    lines = split_lines(text)
    if any(line.startswith("Binary files ") for line in lines):
        return EngineOutput("binary", text)
    if any(line.startswith(("diff --git ", "@@")) for line in lines):
        return EngineOutput("diff", text)
    return EngineOutput("failed", why="unparseable")


def _split(data: bytes) -> list[str]:
    """Lines without their "\\n" but with any "\\r": a line-ending change shows."""
    return split_lines(data.decode("utf-8", errors="replace"))


def difflib_output(before: bytes, after: bytes) -> EngineOutput:
    """The approximate engine, used only while the temp dir sits in a work tree."""
    diff = list(
        difflib.unified_diff(_split(before), _split(after), "a/before", "b/after", lineterm="")
    )
    if not diff:
        return EngineOutput("identical")
    return EngineOutput("diff", "\n".join(["diff --git a/before b/after", *diff]) + "\n")


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def _join(*parts: str | None) -> str | None:
    words = [p for p in parts if p]
    return " · ".join(words) if words else None


class Snapshotter:
    """The engine and its temp dirs, for one state dir. git is resolved once, here,
    from the daemon's PATH."""

    def __init__(self, state_dir: Path) -> None:
        self._temp_root = state_dir / TEMP_DIRNAME
        self.git = shutil.which("git")
        self._isolated = True  # until check_isolation says otherwise
        self.formats = ObjectFormats()

    def capture(
        self, root: Path | None, changes: Sequence[Change], redact: Sequence[str]
    ) -> EventCapture:
        """Off the loop, before create_event (spec §2.2): the "after" of each kept
        change, read by the hardened reader in path order within
        EVENT_CONTENT_BUDGET (a deletion is never read; a file that can't be read is
        "uncapturable"), then the git note of the captured paths and the branch, in
        the same pass. Each read is bounded by what is left of the budget, and a
        spent budget opens no further file: such a path is "uncapturable" marked
        over_budget (create_event stores neither side). A bounded read that fails
        while less than SNAPSHOT_MAX_BYTES is left counts as past the budget too
        (the reader can't say why it refused). Redacted paths are never read nor
        passed to git. ``root`` None (a project with no configured folder): nothing."""
        if root is None:
            return EventCapture((), None)
        states: dict[str, AfterState] = {}
        afters: dict[str, bytes | None] = {}
        over: set[str] = set()
        used = 0
        for change in sorted(changes, key=lambda c: c.path):
            path = change.path
            if is_redacted(path, redact):
                continue
            if change.kind is ChangeKind.DELETED:
                states[path], afters[path] = "deleted", None
                continue
            left = EVENT_CONTENT_BUDGET - used
            data = (
                read_capturable(root, path, redact, min(SNAPSHOT_MAX_BYTES, left))
                if left > 0
                else None
            )
            if data is None:
                states[path] = "uncapturable"
                if left < SNAPSHOT_MAX_BYTES:
                    over.add(path)
                continue
            used += len(data)
            states[path], afters[path] = "content", data
        notes = git_notes(root, afters, self.formats)
        contents = tuple(
            Captured(path, state, afters.get(path), notes.get(path), over_budget=path in over)
            for path, state in states.items()
        )
        return EventCapture(contents, current_branch(root))

    def read_baseline(
        self, root: Path, hashes: Mapping[str, str], redact: Sequence[str]
    ) -> dict[str, bytes]:
        """Off the loop, before replace_baseline's transaction (spec §2.1): each
        capturable watched file, read by the hardened reader in path order within
        PROJECT_SNAPSHOT_BUDGET, without eviction (a file that doesn't fit is
        skipped). Redacted files are never opened; replace_baseline keeps only the
        files whose bytes still hash to their new fingerprint. A rebaseline (a new
        project, a changed path or expression) also drops the cached object format
        of the project's repository. At most BASELINE_MAX_FILES files are read: the
        PDU pump awaits this read, and the byte budget alone doesn't bound the number
        of opens. Each read is bounded by what is left of the budget, and a full
        budget opens no further file."""
        self.formats.forget(root)
        contents: dict[str, bytes] = {}
        used = 0
        reads = 0
        for path in sorted(hashes):
            if is_redacted(path, redact):
                continue
            left = PROJECT_SNAPSHOT_BUDGET - used
            if reads >= BASELINE_MAX_FILES or left <= 0:
                break  # the rest get no snapshot: their first event falls back once
            reads += 1
            data = read_capturable(root, path, redact, min(SNAPSHOT_MAX_BYTES, left))
            if data is None:
                continue  # not capturable, or it doesn't fit what is left
            used += len(data)
            contents[path] = data
        return contents

    @property
    def approximate(self) -> bool:
        """True while reports use difflib (the temp dir sits in a work tree)."""
        return not self._isolated

    def _root(self) -> str:
        with contextlib.suppress(FileExistsError):
            os.mkdir(self._temp_root, 0o700)
        return str(self._temp_root)

    @contextlib.contextmanager
    def _tempdir(self) -> Iterator[str]:
        """A fresh 0700 dir under the temp root, removed in a ``finally``."""
        path = tempfile.mkdtemp(prefix="diff-", dir=self._root())
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def check_isolation(self) -> Isolation:
        """Once per daemon start: "isolated" when git, under the minimal environment,
        finds no work tree around the temp dirs (``rev-parse --is-inside-work-tree``
        must fail); "work_tree" when it finds one; "unknown" when the check could not
        run. Unless isolated, every report uses difflib, marked approximate. Without
        git there is nothing to isolate (the engine falls back)."""
        if self.git is None:
            self._isolated = True
            return "isolated"
        try:
            with self._tempdir() as tmp:
                home = os.path.join(tmp, "home")
                os.mkdir(home, 0o700)
                result = subprocess.run(
                    [self.git, "rev-parse", "--is-inside-work-tree"],
                    cwd=tmp,
                    env=minimal_env(self.git, home),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=ENGINE_TIMEOUT,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            self._isolated = False  # unknown: never trust attributes we can't rule out
            return "unknown"
        self._isolated = result.returncode != 0
        return "isolated" if self._isolated else "work_tree"

    def run_engine(self, before: bytes, after: bytes) -> EngineOutput:
        """Stage 1: ``git diff --no-index`` on two private copies (a missing side is
        an empty file, never /dev/null), mapped by classify_engine."""
        if self.git is None:
            return EngineOutput("failed", why="missing binary")
        try:
            with self._tempdir() as tmp:
                home = os.path.join(tmp, "home")
                os.mkdir(home, 0o700)
                _write_private(os.path.join(tmp, "before"), before)
                _write_private(os.path.join(tmp, "after"), after)
                result = subprocess.run(
                    [self.git, *ENGINE_ARGS],
                    cwd=tmp,
                    env=minimal_env(self.git, home),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=ENGINE_TIMEOUT,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return EngineOutput("failed", why="timeout")
        except FileNotFoundError:
            return EngineOutput("failed", why="missing binary")
        except OSError:
            return EngineOutput("failed", why="temp write failed")
        return classify_engine(result.returncode, result.stdout, result.stderr)

    def report(
        self,
        path: str,
        kind: ChangeKind,
        before: bytes | None,
        after: bytes | None,
        note: str | None,
    ) -> FileReport | None:
        """Stage 2: the content-based report of one file, or None: take the fallback
        (logged at WARNING with the outcome kind only). Created: all "+" lines;
        deleted: all "-" lines."""
        approx = APPROXIMATE if self.approximate else None
        if self.approximate:
            out = difflib_output(before or b"", after or b"")
        else:
            out = self.run_engine(before or b"", after or b"")
        if out.outcome == "failed":
            log.warning("content diff failed (%s); using the fallback", out.why)
            return None
        if out.outcome == "identical":
            same = "no content change" if kind is ChangeKind.MODIFIED else "empty file"
            return FileReport(path, kind, note=_join(same, note, approx))
        if out.outcome == "binary":
            return FileReport(path, kind, note=_join("binary file changed", note, approx))
        parsed = parse_diff(out.text)
        if not parsed.lines:
            log.warning("content diff failed (no changed lines); using the fallback")
            return None
        return FileReport(
            path, kind, lines=parsed.lines, note=_join(note, approx), first_line=parsed.first_line
        )

    def sweep(self, now: float) -> int:
        """Remove leftover temp dirs (a crash) older than TEMP_SWEEP_AGE by mtime: at
        daemon start and with every prune. Returns how many."""
        try:
            entries = list(os.scandir(self._temp_root))
        except OSError:
            return 0
        removed = 0
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or now - st.st_mtime < TEMP_SWEEP_AGE:
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
        return removed
