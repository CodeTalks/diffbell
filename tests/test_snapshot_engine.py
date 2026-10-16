"""The content engine (content snapshots spec §3.1, §6): git diff --no-index on
private temp copies with no user/system config and no attributes; the outcome
mapping; difflib only when the temp dir sits in a work tree; temp-dir hygiene."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from diffbell import snapshots
from diffbell.diffing import MAX_COLLECT_LINES, is_git_repo, parse_diff
from diffbell.models import ChangeKind
from diffbell.snapshots import (
    TEMP_DIRNAME,
    TEMP_SWEEP_AGE,
    EngineOutput,
    Snapshotter,
    classify_engine,
    difflib_output,
)
from tests.helpers import HAS_GIT, HOSTILE_AFTER, HOSTILE_BEFORE, HOSTILE_CHANGED, init_repo

pytestmark = pytest.mark.skipif(not HAS_GIT, reason="git not installed")

M, C, D = ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED


@pytest.fixture
def snap(tmp_dir: Path) -> Snapshotter:
    state = tmp_dir / "state"
    state.mkdir(mode=0o700)
    return Snapshotter(state)


def pinned_env(home: Path) -> dict[str, str]:
    """The test's own pinned environment (not the module's, so a regression there
    shows)."""
    found = shutil.which("git")
    assert found is not None
    return {
        "PATH": os.path.dirname(found),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
    }


def oracle(tmp_dir: Path, before: bytes, after: bytes) -> bytes:
    """`git diff --no-index` run by hand, pinned the same way, outside any repo."""
    work = tmp_dir / "oracle"
    work.mkdir()
    home = work / "home"
    home.mkdir()
    (work / "before").write_bytes(before)
    (work / "after").write_bytes(after)
    result = subprocess.run(
        ["git", *snapshots.ENGINE_ARGS],
        cwd=work,
        env=pinned_env(home),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1 and result.stderr == b""
    return result.stdout


def apply_patch(tmp_dir: Path, before: bytes, patch: str) -> bytes:
    """Apply the raw engine output to ``before`` with `git apply` in a scratch dir."""
    work = tmp_dir / "apply"
    work.mkdir()
    home = work / "home"
    home.mkdir()
    (work / "before").write_bytes(before)
    (work / "p.diff").write_bytes(patch.encode())
    subprocess.run(
        ["git", "apply", "p.diff"], cwd=work, env=pinned_env(home), check=True, capture_output=True
    )
    target = work / "after"
    return (target if target.exists() else work / "before").read_bytes()


# -- stage 1: the raw engine output --------------------------------------------------


def test_the_lockfile_case_is_exactly_one_added_line(snap: Snapshotter) -> None:
    """The difflib regression the review demonstrated: "x"x8 -> "x"x3 + "y" + "x"x5."""
    before = b"x\n" * 8
    after = b"x\n" * 3 + b"y\n" + b"x\n" * 5
    report = snap.report("yarn.lock", M, before, after, None)
    assert report is not None
    assert report.lines == ("+y",)
    assert report.first_line == 4


def test_the_output_equals_git_diff_no_index_byte_for_byte(
    snap: Snapshotter, tmp_dir: Path
) -> None:
    before = b'{\n  "name": "a",\n  "version": "1.0.0"\n}\n'
    after = b'{\n  "name": "a",\n  "version": "1.0.1",\n  "private": true\n}\n'
    out = snap.run_engine(before, after)
    assert out.outcome == "diff"
    assert out.text.encode() == oracle(tmp_dir, before, after)


@pytest.mark.parametrize("seed", range(12))
def test_the_raw_patch_rebuilds_the_after_side(snap: Snapshotter, tmp_dir: Path, seed: int) -> None:
    """By property: random edits to line files (repeated lines like a lockfile, CRLF
    lines, no final newline) give a raw patch that `git apply` turns into "after"."""
    rng = random.Random(seed)
    pool = ["x", "y", "  version: 1", "dependencies:", "a\r", ""]
    lines = [rng.choice(pool) for _ in range(rng.randint(0, 40))]
    edited = list(lines)
    for _ in range(rng.randint(1, 6)):
        op = rng.choice(["ins", "del", "sub"])
        at = rng.randint(0, len(edited))
        if op == "ins":
            edited.insert(at, rng.choice(pool))
        elif edited and at < len(edited):
            if op == "del":
                del edited[at]
            else:
                edited[at] = rng.choice(pool) + "z"
    before = "\n".join(lines).encode() + (b"\n" if rng.random() < 0.7 and lines else b"")
    after = "\n".join(edited).encode() + (b"\n" if rng.random() < 0.7 and edited else b"")
    out = snap.run_engine(before, after)
    if before == after:
        assert out.outcome == "identical"
        return
    assert out.outcome == "diff", out
    assert apply_patch(tmp_dir, before, out.text) == after


def test_a_crlf_only_change_is_a_real_diff_even_with_autocrlf_input(
    snap: Snapshotter, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = tmp_dir / "hostile-home"
    hostile.mkdir()
    (hostile / ".gitconfig").write_text("[core]\n\tautocrlf = input\n")
    monkeypatch.setenv("HOME", str(hostile))
    out = snap.run_engine(b"a\r\nb\r\n", b"a\nb\n")
    assert out.outcome == "diff"
    report = snap.report("x.txt", M, b"a\r\nb\r\n", b"a\nb\n", None)
    assert report is not None and len(report.lines) == 4


def test_user_and_xdg_config_and_attributes_change_nothing(
    snap: Snapshotter, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = b"one\ntwo\n", b"one\n2\n"
    clean = snap.run_engine(before, after)
    hostile = tmp_dir / "hostile-home"
    (hostile / ".config" / "git").mkdir(parents=True)
    (hostile / "attrs").write_text("* -diff\n")
    (hostile / ".gitconfig").write_text(
        "[core]\n\tautocrlf = input\n\tattributesFile = ~/attrs\n"
        "[diff]\n\talgorithm = histogram\n\texternal = /bin/false\n\tnoprefix = true\n"
        "[color]\n\tui = always\n"
    )
    (hostile / ".config" / "git" / "attributes").write_text("* -diff\n")
    monkeypatch.setenv("HOME", str(hostile))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(hostile / ".config"))
    assert snap.run_engine(before, after) == clean
    assert clean.outcome == "diff" and "Binary" not in clean.text


def test_inherited_git_dir_and_work_tree_are_ignored(
    snap: Snapshotter, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_dir / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / ".gitattributes").write_text("* -diff\n")
    clean = snap.run_engine(b"a\n", b"b\n")
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/nonexistent")
    assert snap.run_engine(b"a\n", b"b\n") == clean
    assert clean.outcome == "diff"


def test_a_missing_git_is_the_fallback(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_dir / "no-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    state = tmp_dir / "state"
    state.mkdir()
    snap = Snapshotter(state)
    assert snap.git is None
    assert snap.run_engine(b"a\n", b"b\n") == EngineOutput("failed", why="missing binary")
    assert snap.report("a", M, b"a\n", b"b\n", None) is None


def test_git_is_resolved_from_the_daemons_path(snap: Snapshotter) -> None:
    assert snap.git == shutil.which("git")


# -- the outcome mapping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr", "want"),
    [
        (1, b"diff --git a/before b/after\n@@ -1 +1 @@\n-a\n+b\n", b"", "diff"),
        (1, b"diff --git a/before b/after\nBinary files before and after differ\n", b"", "binary"),
        (0, b"", b"", "identical"),
        (1, b"", b"error: Could not access 'before'\n", "failed"),  # "could not access"
        (1, b"diff --git a/before b/after\n@@ -1 +1 @@\n-a\n+b\n", b"warning: x\n", "failed"),
        (0, b"diff --git a/before b/after\n", b"", "failed"),  # rc 0 with output
        (128, b"", b"", "failed"),
        (1, b"something else\n", b"", "failed"),  # unparseable
    ],
)
def test_every_outcome_maps_to_one_result(rc: int, stdout: bytes, stderr: bytes, want: str) -> None:
    assert classify_engine(rc, stdout, stderr).outcome == want


def test_identical_content_is_a_note_only_report(snap: Snapshotter) -> None:
    report = snap.report("package.json", M, b"{}\n", b"{}\n", "committed")
    assert report is not None
    assert (report.lines, report.note) == ((), "no content change · committed")


def test_a_binary_answer_is_a_note(snap: Snapshotter) -> None:
    report = snap.report("a.dat", M, b"a\0b", b"a\0c", None)
    assert report is not None
    assert (report.lines, report.note) == ((), "binary file changed")


def test_could_not_access_falls_back_never_empty(
    snap: Snapshotter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = subprocess.run

    def broken(args: list[str], **kw: Any) -> Any:
        if args[-2:] == ["before", "after"]:
            return subprocess.CompletedProcess(args, 1, b"", b"error: Could not access\n")
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", broken)
    caplog.set_level("WARNING", logger="diffbell.snapshots")
    assert snap.report("a", M, b"SECRET-BEFORE\n", b"SECRET-AFTER\n", None) is None
    assert "stderr" in caplog.text and "SECRET" not in caplog.text


def test_a_timeout_falls_back(snap: Snapshotter, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(args: list[str], **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)

    monkeypatch.setattr(subprocess, "run", slow)
    assert snap.run_engine(b"a\n", b"b\n") == EngineOutput("failed", why="timeout")


def test_the_fallbacks_git_calls_run_with_lc_all_c(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback's "Binary files" check parses git's English messages too."""
    envs: list[dict[str, str] | None] = []
    real = subprocess.run

    def spy(args: list[str], **kw: Any) -> Any:
        envs.append(kw.get("env"))
        return real(args, **kw)

    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setattr(subprocess, "run", spy)
    assert is_git_repo(tmp_dir) is False
    assert envs and all(env is not None and env["LC_ALL"] == "C" for env in envs)


# -- stage 2: extraction ---------------------------------------------------------------


def test_created_is_all_plus_and_deleted_all_minus(snap: Snapshotter) -> None:
    created = snap.report("Makefile", C, None, b"all:\n\ttrue\n", "untracked")
    deleted = snap.report("Makefile", D, b"all:\n\ttrue\n", None, "committed")
    assert created is not None and deleted is not None
    assert (created.lines, created.first_line, created.note) == (
        ("+all:", "+\ttrue"),
        1,
        "untracked",
    )
    assert (deleted.lines, deleted.first_line) == (("-all:", "-\ttrue"), 1)


def test_extraction_drops_context_and_caps_the_lines(snap: Snapshotter) -> None:
    before = b"".join(b"line %d\n" % i for i in range(400))
    after = b"".join(b"LINE %d\n" % i for i in range(400))
    report = snap.report("big.txt", M, before, after, None)
    assert report is not None
    assert len(report.lines) == MAX_COLLECT_LINES
    assert all(line.startswith(("+", "-")) for line in report.lines)


def test_parse_diff_keeps_changed_lines_and_the_binary_flag() -> None:
    text = (
        "diff --git a/before b/after\nindex 1..2 100644\n--- a/before\n+++ b/after\n"
        "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n\\ No newline at end of file\n"
    )
    parsed = parse_diff(text)
    assert (parsed.lines, parsed.first_line, parsed.binary) == (("-b", "+B"), 2, False)
    binary = parse_diff("diff --git a/before b/after\nBinary files before and after differ\n")
    assert (binary.lines, binary.first_line, binary.binary) == ((), None, True)


def test_an_empty_created_file_is_a_note(snap: Snapshotter) -> None:
    report = snap.report("VERSION", C, None, b"", "untracked")
    assert report is not None and (report.lines, report.note) == ((), "empty file · untracked")


# -- never inside a work tree -------------------------------------------------------------


def test_a_state_dir_outside_any_repo_is_isolated(snap: Snapshotter) -> None:
    assert snap.check_isolation() == "isolated"
    assert snap.approximate is False


def test_a_state_dir_inside_a_work_tree_uses_difflib_marked_approximate(
    tmp_dir: Path,
) -> None:
    repo = tmp_dir / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / ".gitattributes").write_text("* -diff\n")
    state = repo / "state"
    state.mkdir()
    snap = Snapshotter(state)
    assert snap.check_isolation() == "work_tree"
    assert snap.approximate is True
    report = snap.report("a.txt", M, b"a\nb\n", b"a\nc\n", "uncommitted")
    assert report is not None
    assert (report.lines, report.note) == (("-b", "+c"), "uncommitted · approximate")


def test_the_work_tree_check_runs_under_the_minimal_environment(
    snap: Snapshotter, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inherited GIT_DIR would make rev-parse succeed anywhere."""
    repo = tmp_dir / "repo"
    repo.mkdir()
    init_repo(repo)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    assert snap.check_isolation() == "isolated"


@pytest.mark.parametrize("error", [OSError("no git"), subprocess.TimeoutExpired("git", 5)])
def test_a_work_tree_check_that_could_not_run_is_unknown_and_approximate(
    snap: Snapshotter, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Not the same answer as "inside a work tree": the daemon words it honestly."""
    if snap.git is None:
        snap.git = "/usr/bin/git"  # the check must reach subprocess.run

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(subprocess, "run", broken)
    assert snap.check_isolation() == "unknown"
    assert snap.approximate is True


def test_difflib_sees_a_line_ending_change() -> None:
    out = difflib_output(b"a\r\nb\r\n", b"a\nb\n")
    assert out.outcome == "diff"
    # The "\r" stays in the raw output and in the parsed lines: the shared parser
    # ends a line at "\n" only.
    assert "-a\r\n" in out.text
    assert parse_diff(out.text).lines == ("-a\r", "-b\r", "+a", "+b")
    assert difflib_output(b"same\n", b"same\n").outcome == "identical"


# -- temp files -----------------------------------------------------------------------


def test_temp_dirs_are_private_and_removed_after_each_diff(
    snap: Snapshotter, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int, int, int]] = []
    real = subprocess.run

    def spy(args: list[str], **kw: Any) -> Any:
        cwd = Path(kw["cwd"])
        seen.append(
            (
                cwd.name,
                cwd.stat().st_mode & 0o777,
                (cwd / "before").stat().st_mode & 0o777,
                (cwd / "after").stat().st_mode & 0o777,
            )
        )
        assert cwd.parent == tmp_dir / "state" / TEMP_DIRNAME
        assert sorted(p.name for p in cwd.iterdir()) == ["after", "before", "home"]
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    assert snap.run_engine(b"a\n", b"b\n").outcome == "diff"
    assert [mode for _, *mode in seen] == [[0o700, 0o600, 0o600]]
    root = tmp_dir / "state" / TEMP_DIRNAME
    assert root.stat().st_mode & 0o777 == 0o700
    assert list(root.iterdir()) == []


def test_the_sweep_removes_only_old_leftovers(snap: Snapshotter, tmp_dir: Path) -> None:
    root = tmp_dir / "state" / TEMP_DIRNAME
    root.mkdir(mode=0o700)
    old, fresh = root / "diff-old", root / "diff-fresh"
    old.mkdir()
    (old / "before").write_text("x")
    fresh.mkdir()
    stale = time.time() - TEMP_SWEEP_AGE - 60
    os.utime(old, (stale, stale))
    assert snap.sweep(time.time()) == 1
    assert sorted(p.name for p in root.iterdir()) == ["diff-fresh"]


def test_the_sweep_never_removes_a_running_diffs_dir(
    snap: Snapshotter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff (the debouncer task) and the sweep (prune, the maintenance task) share
    no lock: the age rule alone keeps the sweep off a dir in use."""
    real = subprocess.run
    swept: list[int] = []

    def during(args: list[str], **kw: Any) -> Any:
        worker = threading.Thread(target=lambda: swept.append(snap.sweep(time.time())))
        worker.start()
        worker.join()
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", during)
    assert snap.run_engine(b"a\n", b"b\n").outcome == "diff"
    assert swept == [0]


# -- the engine's output is LF-delimited -----------------------------------------------


def test_the_engine_report_keeps_line_separators_inside_a_line(snap: Snapshotter) -> None:
    report = snap.report("notes.txt", M, HOSTILE_BEFORE, HOSTILE_AFTER, None)
    assert report is not None
    assert (report.lines, report.first_line) == (HOSTILE_CHANGED, 2)


def test_the_approximate_report_keeps_line_separators_inside_a_line() -> None:
    out = difflib_output(HOSTILE_BEFORE, HOSTILE_AFTER)
    assert out.outcome == "diff"
    parsed = parse_diff(out.text)
    assert (parsed.lines, parsed.first_line) == (HOSTILE_CHANGED, 2)


def test_a_content_line_never_reads_as_a_binary_answer() -> None:
    stdout = (
        "diff --git a/before b/after\n--- a/before\n+++ b/after\n@@ -1 +1 @@\n"
        "-x\n+y\x85Binary files before and after differ\n"
    ).encode()
    assert classify_engine(1, stdout, b"").outcome == "diff"
