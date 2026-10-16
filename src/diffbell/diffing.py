"""Per-file content for notifications (spec §6). Redacted files are never read.

Every git call runs with ``--literal-pathspecs`` so a file literally named
``migrations/*`` or ``[.]env`` cannot match (and leak) a redacted sibling, and
``git diff`` runs with ``--no-textconv --no-ext-diff`` so no user-configured
driver (e.g. ``sops -d``) executes or injects decrypted content. Residual risk:
git may still run *clean* filters (``filter.<name>.clean``) on the worktree side
of a diff; their output is only shown for files that are not redacted.

git's output is split on "\\n" only (``split_lines``, used by ``parse_diff``), and
``_git`` decodes the bytes itself instead of text mode: a "\\r" or another line
separator in a file's content can't fake a ``diff --git`` header, a hunk header or
a changed line.

Content snapshots (ADR 0017) change what "safe" means for content-based reports:

- They never run git on the working tree. ``snapshots.Snapshotter`` diffs two
  private copies with ``git diff --no-index`` under a minimal environment (private
  ``HOME`` and ``XDG_CONFIG_HOME``, no system or global config, no attributes,
  ``core.autocrlf=false``) in a temp dir verified to be outside any work tree
  (difflib, marked approximate, otherwise). So the clean-filter residual above
  applies only to this module's fallback (``build_reports``).
- ``read_capturable`` is the only reader of contents for snapshots: the baseline,
  event creation and the note all go through it.
- The "committed" note (``git_notes``) compares the captured bytes with ``HEAD``'s
  blob id. In a repository that normalises content on commit
  (``core.autocrlf=input``, ``.gitattributes`` ``eol``/``text``, ``clean``
  filters) the stored blob differs from the working file, so such a file reads
  "uncommitted" even when ``git status`` is clean. Running the filters to compare
  would bring back the clean-filter residual, so the plain byte comparison stays.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import subprocess
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from diffbell.models import Change, ChangeKind
from diffbell.patterns import is_redacted

MAX_COLLECT_LINES = 200  # render.py truncates further to diff.max_lines
GIT_TIMEOUT = 10.0
HEAD_READ_BYTES = 64 * 1024
BINARY_SNIFF_BYTES = 8192
# A hunk header's new-file side: "+c,d", or the comma-less "+c" of a one-line side
# ("@@ -0,0 +1 @@", a new one-line file).
HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True, slots=True)
class FileReport:
    path: str
    kind: ChangeKind
    redacted: bool = False
    lines: tuple[str, ...] = ()
    note: str | None = None
    # The new file's first changed line (Plan 7 spec §2.1, §9): the page's "Open"
    # link goes there. None without a hunk (binary, redacted, unreadable).
    first_line: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    """A unified diff reduced to what a FileReport keeps: the first file's changed
    lines (context, headers and "\\ No newline" markers dropped), capped at
    MAX_COLLECT_LINES; its first changed line; whether git called it binary."""

    lines: tuple[str, ...]
    first_line: int | None
    binary: bool


def split_lines(text: str) -> list[str]:
    """Lines of git output or file content, split on "\\n" only (a terminal "\\n"
    ends the last line). Never ``str.splitlines()``: it also breaks on "\\r",
    "\\x0b", "\\x0c", "\\x1c"-"\\x1e", "\\x85", U+2028 and U+2029, so a file whose
    content holds one could fake a "diff --git" header (hiding every later change),
    a hunk header or a changed line. Those characters stay in the line."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def parse_diff(text: str) -> ParsedDiff:
    """The one hunk parser: the HEAD fallback (_git_report) and the content engine
    (snapshots.Snapshotter.report) both extract their lines with it."""
    lines = split_lines(text)
    body: list[str] = []
    in_hunk = False
    binary = False
    seen_header = False
    for line in lines:
        if line.startswith("diff --git "):
            if seen_header:
                break  # only ever report the first (and only expected) file
            seen_header = True
        elif not in_hunk and line.startswith("Binary files "):
            binary = True
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith(("+", "-")):
            body.append(line)
    first = first_changed_line(lines) if body else None
    return ParsedDiff(tuple(body[:MAX_COLLECT_LINES]), first, binary)


def hunk_new_start(line: str) -> int | None:
    """The new-file start ``c`` of a hunk header ``@@ -a,b +c,d @@`` (or the
    comma-less ``+c``), as written (0 for an empty new side), or None: not a header."""
    match = HUNK_HEADER.match(line)
    return None if match is None else int(match.group(1))


def first_changed_line(diff: Sequence[str]) -> int | None:
    """The new-side line number of a diff's first changed line, or None without a
    hunk. ``c`` counts git's context lines too, so the context lines (" ") above the
    first "+" or "-" line are counted from it; a deletion-only change gives the
    position where the deletion sits, kept at least 1 (an emptied file: 1)."""
    position: int | None = None
    for line in diff:
        start = hunk_new_start(line) if line.startswith("@@") else None
        if start is not None:
            position = start
        elif position is None:
            continue  # the file headers before the first hunk
        elif line.startswith(("+", "-")):
            return max(1, position)
        elif line.startswith(" "):
            position += 1
        # "\ No newline at end of file" is not a line of either side
    return None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "--literal-pathspecs", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=False,
            # git's messages ("Binary files ... differ") stay English for the parser,
            # as in the content engine (content snapshots spec §3.1).
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Decoded here, not with text=True: text mode turns every "\r" into "\n", and a
    # "\r" in a file's content would split a diff line (see split_lines).
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def is_git_repo(path: Path) -> bool:
    result = _git(path, "rev-parse", "--is-inside-work-tree")
    return result is not None and result.returncode == 0 and result.stdout.strip() == "true"


def current_branch(path: Path | None) -> str | None:
    """Branch name; the short SHA on a detached HEAD; None outside git."""
    if path is None:
        return None
    result = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if result is None or result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if branch != "HEAD":
        return branch
    sha = _git(path, "rev-parse", "--short", "HEAD")
    return sha.stdout.strip() if sha is not None and sha.returncode == 0 else None


def build_reports(
    project_path: Path | None, changes: Sequence[Change], redact: Sequence[str]
) -> list[FileReport]:
    in_repo = project_path is not None and is_git_repo(project_path)
    reports: list[FileReport] = []
    for change in changes:
        if is_redacted(change.path, redact):
            reports.append(FileReport(change.path, change.kind, redacted=True))
        elif project_path is None or change.kind is ChangeKind.DELETED or not in_repo:
            reports.append(FileReport(change.path, change.kind))
        else:
            reports.append(_git_report(project_path, change, redact))
    return reports


def _content_problem(st: os.stat_result) -> str | None:
    """Why a file with this lstat result must not be read or diffed, or None.

    Symlinks pass (callers never follow them; git shows only the target string).
    A hard-linked regular file may share content with a redacted name such as
    ``.env``; anything else (FIFO, device, socket) must never be opened.
    """
    if stat.S_ISREG(st.st_mode):
        return "hard-linked file" if st.st_nlink > 1 else None
    if stat.S_ISLNK(st.st_mode):
        return None
    return "not a regular file"


def _git_report(root: Path, change: Change, redact: Sequence[str]) -> FileReport:
    # Checked before any git call: git would read (and diff) a tracked
    # package.json that is a hard link to .env, or open a device node.
    try:
        problem = _content_problem(os.lstat(root / change.path))
    except OSError:
        return FileReport(change.path, change.kind, note="unreadable")
    if problem is not None:
        return FileReport(change.path, change.kind, note=problem)
    tracked = _git(root, "ls-files", "--error-unmatch", "--", change.path)
    if tracked is None:
        return FileReport(change.path, change.kind, note="diff unavailable")
    if tracked.returncode != 0:  # untracked
        if change.kind is ChangeKind.CREATED:
            return _head_report(root, change, redact)
        return FileReport(change.path, change.kind)
    diff = _git(
        root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD", "--", change.path
    )
    if diff is None or diff.returncode != 0:
        return FileReport(change.path, change.kind, note="diff unavailable")
    parsed = parse_diff(diff.stdout)
    if not parsed.lines:
        note = "binary file changed" if parsed.binary else "matches HEAD"
        return FileReport(change.path, change.kind, note=note)
    return FileReport(
        change.path,
        change.kind,
        lines=parsed.lines,
        note="vs HEAD",
        first_line=parsed.first_line,
    )


def _location_problem(real: str, real_root: str, redact: Sequence[str]) -> str | None:
    """Why the resolved path ``real`` must not be read, or None.

    Both sides are compared in NFC: APFS is normalization-insensitive, so an NFD
    path on disk and an NFC root typed by the user name the same directory.
    """
    real = unicodedata.normalize("NFC", real)
    real_root = unicodedata.normalize("NFC", real_root)
    if os.path.commonpath([real_root, real]) != real_root or real == real_root:
        return "outside project"
    if is_redacted(Path(os.path.relpath(real, real_root)).as_posix(), redact):
        return "redacted"
    return None


def _fd_real_path(fd: int, fallback: str) -> str:
    """The real path of an open fd (macOS F_GETPATH); ``fallback``'s realpath elsewhere."""
    f_getpath = getattr(fcntl, "F_GETPATH", None)
    if f_getpath is not None:
        try:
            raw = fcntl.fcntl(fd, f_getpath, bytes(1024))
            return os.fsdecode(raw.rstrip(b"\0"))
        except OSError:
            pass
    return os.path.realpath(fallback)


def _canonical_dir(directory: Path) -> str:
    """A directory's real path as the filesystem spells it (case, normalization).

    ``os.path.realpath`` keeps the user-typed spelling (``~/Projects/x`` when the
    disk has ``~/projects/x``), but F_GETPATH on an opened file reports the
    on-disk one; deriving directories the same way keeps the two comparable.
    """
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return os.path.realpath(directory)
    try:
        return _fd_real_path(fd, str(directory))
    finally:
        os.close(fd)


def _verify_opened(
    fd: int,
    before: os.stat_result,
    real_root: str,
    redact: Sequence[str],
    fallback_path: str,
) -> str | None:
    """Post-open checks on the fd itself, closing the check/open race window.

    Returns a reason not to read (``"redacted"`` means report as redacted), or None.
    """
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
        return "changed while reading"
    if st.st_nlink > 1:  # another name (e.g. .env.local) may share this content
        return "hard-linked file"
    return _location_problem(_fd_real_path(fd, fallback_path), real_root, redact)


def _head_report(root: Path, change: Change, redact: Sequence[str]) -> FileReport:
    """First lines of an untracked new file.

    Never follows a symlink (its target may be outside the project or a redacted
    file), refuses paths whose real location is outside the project or redacted
    (a symlinked parent directory), refuses hard-linked files, never blocks on
    FIFOs/devices, and re-verifies everything on the opened fd before reading
    (a parent directory swapped for a symlink after the first check).
    """
    path = root / change.path
    try:
        st = os.lstat(path)
    except OSError:
        return FileReport(change.path, change.kind, note="unreadable")
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return FileReport(change.path, change.kind, note=f"symlink → {target}")
    if (content_problem := _content_problem(st)) is not None:
        return FileReport(change.path, change.kind, note=content_problem)  # never opened
    real_root = _canonical_dir(root)
    # The parent resolved like the root (symlinked parents followed); the name
    # itself is a regular file per lstat above.
    real_path = os.path.join(_canonical_dir(path.parent), path.name)
    problem = _location_problem(real_path, real_root, redact)
    if problem == "redacted":
        return FileReport(change.path, change.kind, redacted=True)
    if problem is not None:
        return FileReport(change.path, change.kind, note=problem)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return FileReport(change.path, change.kind, note="unreadable")
    try:
        problem = _verify_opened(fd, st, real_root, redact, str(path))
        if problem == "redacted":
            return FileReport(change.path, change.kind, redacted=True)
        if problem is not None:
            return FileReport(change.path, change.kind, note=problem)
        chunks: list[bytes] = []
        remaining = HEAD_READ_BYTES
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return FileReport(change.path, change.kind, note="unreadable")
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return FileReport(change.path, change.kind, note="binary file")
    text = data.decode("utf-8", errors="replace")
    lines = tuple("+" + line for line in split_lines(text)[:MAX_COLLECT_LINES])
    # The whole file is new: it starts at line 1, as git's "@@ -0,0 +1,N @@" would say.
    first_line = 1 if lines else None
    return FileReport(change.path, change.kind, lines=lines, note="new file", first_line=first_line)


# A snapshot holds at most this much of one file (content snapshots spec §1).
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_capturable(
    root: Path, path: str, redact: Sequence[str], max_bytes: int = SNAPSHOT_MAX_BYTES
) -> bytes | None:
    """The bytes of a capturable file (content snapshots spec §2.4), or None.

    The one reader of contents for snapshots: the baseline, event creation and the
    git note all use it. A redacted name is refused before anything is opened. The
    rest is _head_report's guard: never follows a symlink, refuses a hard-linked or
    special file and a real path outside the project or redacted, never blocks
    (O_NONBLOCK), and re-verifies type, inode, link count and real path on the
    opened fd. Size (``max_bytes``) and text (no NUL in the first
    BINARY_SNIFF_BYTES) are decided on the bytes actually read, with a bounded read.
    """
    if is_redacted(path, redact):
        return None
    full = root / path
    try:
        st = os.lstat(full)
    except OSError:
        return None
    if _content_problem(st) is not None or not stat.S_ISREG(st.st_mode):
        return None
    real_root = _canonical_dir(root)
    real_path = os.path.join(_canonical_dir(full.parent), full.name)
    if _location_problem(real_path, real_root, redact) is not None:
        return None
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if _verify_opened(fd, st, real_root, redact, str(full)) is not None:
            return None
        data = _read_bounded(fd, max_bytes + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > max_bytes or b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    return data


def blob_id(data: bytes, object_format: str) -> str:
    """The id git gives ``data`` as a blob (``git hash-object --no-filters``)."""
    algorithm = hashlib.sha256 if object_format == "sha256" else hashlib.sha1
    return algorithm(b"blob %d\0" % len(data) + data).hexdigest()


class ObjectFormats:
    """Each repository's object format, cached by its toplevel (content snapshots
    spec §3.2): two watched projects can be different repositories, and a folder
    retargeted to another repository gets a new toplevel and a fresh lookup."""

    # Thread safety: capture (the notifier) and read_baseline (the watcher) call this
    # from worker threads. Each access is one dict operation (get, set, del of a
    # list() copy's key), atomic under the GIL; two threads missing the same key
    # both look it up and store the same value, a benign re-insert.
    def __init__(self) -> None:
        self._by_toplevel: dict[str, str] = {}

    def lookup(self, root: Path) -> str | None:
        """The object format ("sha1" or "sha256") of the repository ``root`` is in;
        None outside git."""
        top = _git(root, "rev-parse", "--show-toplevel")
        # Only git's own "\n" is stripped: a toplevel may end in a space, and the
        # key must be the path forget() compares against.
        toplevel = top.stdout.removesuffix("\n") if top is not None else ""
        if top is None or top.returncode != 0 or not toplevel:
            return None
        cached = self._by_toplevel.get(toplevel)
        if cached is not None:
            return cached
        found = _git(root, "rev-parse", "--show-toplevel", "--show-object-format")
        lines = split_lines(found.stdout) if found is not None and found.returncode == 0 else []
        # The format is the LAST line (the toplevel before it may itself hold a
        # newline). A git too old for the flag echoes it back: sha1, the only format
        # it reads.
        fmt = "sha256" if lines and lines[-1] == "sha256" else "sha1"
        self._by_toplevel[toplevel] = fmt
        return fmt

    def forget(self, root: Path) -> None:
        """Drop the entry of the repository holding ``root`` (a rebaseline)."""
        real = os.path.realpath(root)
        for toplevel in list(self._by_toplevel):
            if real == toplevel or real.startswith(toplevel.rstrip(os.sep) + os.sep):
                del self._by_toplevel[toplevel]


def git_notes(
    root: Path, afters: Mapping[str, bytes | None], formats: ObjectFormats
) -> dict[str, str]:
    """The note of each path (content snapshots spec §3.2), frozen at creation:
    "committed" (the after is HEAD's blob, or a deleted file absent from HEAD),
    "uncommitted" (tracked or in HEAD, and differing), "untracked". ``afters``: the
    captured content of each created or modified path, None for a deleted one;
    only non-redacted, capturable paths are ever passed.

    No blob is fetched: one ``ls-tree -r -z HEAD`` (no ``-l``) gives HEAD's blob
    ids, one ``ls-files -z`` the tracked set, both through _git (literal pathspecs,
    timeout) in the project folder, never ``--full-tree``. Empty outside git, and
    when either call fails (only a positive signal is trusted)."""
    if not afters:
        return {}
    object_format = formats.lookup(root)
    if object_format is None:
        return {}
    paths = sorted(afters)
    tree = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", *paths)
    files = _git(root, "ls-files", "-z", "--", *paths)
    if tree is None or files is None or tree.returncode != 0 or files.returncode != 0:
        return {}
    in_head: dict[str, str] = {}
    for entry in tree.stdout.split("\0"):
        meta, tab, name = entry.partition("\t")
        fields = meta.split()
        if tab and len(fields) == 3 and fields[1] == "blob":
            in_head[name] = fields[2]
    tracked = {name for name in files.stdout.split("\0") if name}
    notes: dict[str, str] = {}
    for path in paths:
        data = afters[path]
        if data is None:
            notes[path] = "uncommitted" if path in in_head else "committed"
        elif path in in_head and blob_id(data, object_format) == in_head[path]:
            notes[path] = "committed"
        elif path in in_head or path in tracked:
            notes[path] = "uncommitted"
        else:
            notes[path] = "untracked"
    return notes
