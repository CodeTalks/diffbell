"""The git note frozen at creation (content snapshots spec §3.2): committed,
uncommitted or untracked, from one ls-tree (by blob id, no -l) and one ls-files per
event, never a blob read, with the object format cached per repository."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from diffbell.diffing import ObjectFormats, blob_id, git_notes
from tests.helpers import HAS_GIT, git, init_repo

pytestmark = pytest.mark.skipif(not HAS_GIT, reason="git not installed")


def commit_all(repo: Path, message: str = "c") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_project: Path) -> Path:
    init_repo(tmp_project)
    (tmp_project / "package.json").write_text('{"v": 1}\n')
    (tmp_project / "Makefile").write_text("all:\n")
    commit_all(tmp_project)
    return tmp_project


def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(args: list[str], **kw: Any) -> Any:
        seen.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    return seen


def test_every_note_state_against_a_real_repository(repo: Path) -> None:
    (repo / "package.json").write_text('{"v": 2}\n')  # modified
    (repo / "Makefile").write_text("changed:\n")
    (repo / "Makefile").write_text("all:\n")  # edited, then reverted
    (repo / "staged.json").write_text("{}\n")
    git(repo, "add", "staged.json")  # staged, never committed
    (repo / "new.json").write_text("{}\n")  # untracked
    notes = git_notes(
        repo,
        {
            "package.json": b'{"v": 2}\n',
            "Makefile": b"all:\n",
            "staged.json": b"{}\n",
            "new.json": b"{}\n",
            "gone.json": None,  # deleted, never in HEAD
        },
        ObjectFormats(),
    )
    assert notes == {
        "package.json": "uncommitted",
        "Makefile": "committed",
        "staged.json": "uncommitted",
        "new.json": "untracked",
        "gone.json": "committed",
    }


def test_a_deleted_file_still_in_head_is_uncommitted(repo: Path) -> None:
    (repo / "Makefile").unlink()
    assert git_notes(repo, {"Makefile": None}, ObjectFormats()) == {"Makefile": "uncommitted"}


def test_a_change_committed_before_the_read_is_committed(repo: Path) -> None:
    """The `+0 −0` case: a merge or pull already committed the new content."""
    (repo / "package.json").write_text('{"v": 3}\n')
    commit_all(repo)
    assert git_notes(repo, {"package.json": b'{"v": 3}\n'}, ObjectFormats()) == {
        "package.json": "committed"
    }


def test_two_calls_per_event_no_l_and_no_blob_reads(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    formats = ObjectFormats()
    formats.lookup(repo)  # the format is cached from here on
    seen = calls(monkeypatch)
    git_notes(repo, {"package.json": b"x\n", "Makefile": b"all:\n", "x.json": None}, formats)
    trees = [c for c in seen if "ls-tree" in c]
    files = [c for c in seen if "ls-files" in c]
    assert len(trees) == 1 and len(files) == 1
    assert "-l" not in trees[0] and "--long" not in trees[0]
    assert not any("cat-file" in c or "show" in c for c in seen)
    assert all("--literal-pathspecs" in c for c in trees + files)
    assert not any("--full-tree" in c for c in seen)


def test_a_literal_pathspec_never_matches_a_redacted_sibling(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mig = repo / "migrations"
    mig.mkdir()
    (mig / ".env.prod").write_text("TOKEN=SECRET\n")
    (mig / "*").write_text("harmless\n")
    commit_all(repo)
    outputs: list[str] = []
    real = subprocess.run

    def spy(args: list[str], **kw: Any) -> Any:
        result = real(args, **kw)
        if "ls-tree" in args or "ls-files" in args:
            outputs.append(result.stdout.decode("utf-8", errors="replace"))
        return result

    monkeypatch.setattr(subprocess, "run", spy)
    notes = git_notes(repo, {"migrations/*": b"harmless\n"}, ObjectFormats())
    assert notes == {"migrations/*": "committed"}
    assert outputs and not any(".env.prod" in out for out in outputs)


def test_a_project_in_a_subdirectory_of_its_repository(tmp_project: Path) -> None:
    """A monorepo package: git applies the cwd prefix (never --full-tree)."""
    init_repo(tmp_project)
    pkg = tmp_project / "packages" / "web"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"v": 1}\n')
    commit_all(tmp_project)
    (pkg / "Makefile").write_text("all:\n")
    notes = git_notes(pkg, {"package.json": b'{"v": 1}\n', "Makefile": b"all:\n"}, ObjectFormats())
    assert notes == {"package.json": "committed", "Makefile": "untracked"}


def test_no_note_outside_git(tmp_project: Path) -> None:
    (tmp_project / "package.json").write_text("{}\n")
    assert git_notes(tmp_project, {"package.json": b"{}\n"}, ObjectFormats()) == {}


def test_the_eol_normalised_limit(repo: Path) -> None:
    """Documented limit: with core.autocrlf=input the blob is LF while the working
    file stays CRLF; `git status` is clean, the note says "uncommitted"."""
    git(repo, "config", "core.autocrlf", "input")
    (repo / "win.txt").write_bytes(b"a\r\nb\r\n")
    commit_all(repo)
    assert git(repo, "status", "--porcelain") == ""
    notes = git_notes(repo, {"win.txt": b"a\r\nb\r\n"}, ObjectFormats())
    assert notes == {"win.txt": "uncommitted"}


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_blob_id_equals_git_hash_object(tmp_project: Path, object_format: str) -> None:
    git(tmp_project, "init", "-q", f"--object-format={object_format}")
    data = b"hello\r\nworld\x00?"
    (tmp_project / "f").write_bytes(data)
    expected = git(tmp_project, "hash-object", "--no-filters", "f").strip()
    assert blob_id(data, object_format) == expected


def test_a_sha1_and_a_sha256_repository_each_keep_their_format(tmp_dir: Path) -> None:
    formats = ObjectFormats()
    repos = {}
    for fmt in ("sha1", "sha256"):
        repo = tmp_dir / fmt
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main", f"--object-format={fmt}")
        (repo / "package.json").write_text("{}\n")
        commit_all(repo)
        repos[fmt] = repo
    for fmt, repo in repos.items():
        assert formats.lookup(repo) == fmt
        assert git_notes(repo, {"package.json": b"{}\n"}, formats) == {"package.json": "committed"}


def test_a_retargeted_project_folder_gets_a_fresh_lookup(tmp_dir: Path) -> None:
    """The same project path, now a symlink to another repository (no config edit):
    a new toplevel, so the cached format of the old one is never reused."""
    one, two = tmp_dir / "one", tmp_dir / "two"
    for repo, fmt in ((one, "sha1"), (two, "sha256")):
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main", f"--object-format={fmt}")
        (repo / "package.json").write_text("{}\n")
        commit_all(repo)
    link = tmp_dir / "project"
    link.symlink_to(one)
    formats = ObjectFormats()
    assert formats.lookup(link) == "sha1"
    link.unlink()
    link.symlink_to(two)
    assert formats.lookup(link) == "sha256"
    assert git_notes(link, {"package.json": b"{}\n"}, formats) == {"package.json": "committed"}


def test_forget_drops_the_cached_entry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    formats = ObjectFormats()
    formats.lookup(repo)
    seen = calls(monkeypatch)
    formats.lookup(repo)
    assert not any("--show-object-format" in c for c in seen)
    formats.forget(repo)
    formats.lookup(repo)
    assert sum("--show-object-format" in c for c in seen) == 1


def test_a_git_without_the_flag_defaults_to_sha1(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old git echoes the unknown flag back instead of a format."""
    real = subprocess.run

    def old_git(args: list[str], **kw: Any) -> Any:
        if "--show-object-format" in args:
            return subprocess.CompletedProcess(
                args, 0, f"{repo}\n--show-object-format\n".encode(), b""
            )
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", old_git)
    assert ObjectFormats().lookup(repo) == "sha1"


def test_a_repository_with_no_commits_gets_no_notes(tmp_project: Path) -> None:
    """An unborn HEAD: ls-tree fails, and a failing call is no note (only a positive
    signal is trusted). Catches a regression that reads an empty or failed ls-tree as
    "nothing in HEAD" and labels every file "untracked" or "committed"."""
    init_repo(tmp_project)
    (tmp_project / "package.json").write_text("{}\n")
    git(tmp_project, "add", "package.json")
    assert (
        git_notes(tmp_project, {"package.json": b"{}\n", "gone.json": None}, ObjectFormats()) == {}
    )


def test_paths_with_spaces_tabs_and_newlines(repo: Path) -> None:
    """-z keeps odd names whole: a regression to newline-split output (or to
    core.quotepath's quoted names) would mislabel each of them."""
    names = ["a b.json", "tab\there.json", "new\nline.json"]
    for name in names:
        (repo / name).write_text("{}\n")
    commit_all(repo)
    (repo / "tab\there.json").write_text('{"v": 2}\n')
    (repo / "fresh\nfile.json").write_text("{}\n")
    notes = git_notes(
        repo,
        {
            "a b.json": b"{}\n",
            "tab\there.json": b'{"v": 2}\n',
            "fresh\nfile.json": b"{}\n",
            "new\nline.json": None,
        },
        ObjectFormats(),
    )
    assert notes == {
        "a b.json": "committed",
        "tab\there.json": "uncommitted",
        "fresh\nfile.json": "untracked",
        "new\nline.json": "uncommitted",
    }


def test_a_gitlink_is_not_a_file_in_head(repo: Path) -> None:
    """A submodule's gitlink ("160000 commit <sha>") is not a blob: a deleted path
    named like it reads as absent from HEAD. Catches dropping the blob-type check
    (the gitlink's commit id would then count as the path's HEAD content)."""
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},sub")
    git(repo, "commit", "-q", "-m", "gitlink")
    git(repo, "rm", "-q", "--cached", "sub")
    assert git_notes(repo, {"sub": None, "Makefile": b"all:\n"}, ObjectFormats()) == {
        "sub": "committed",
        "Makefile": "committed",
    }


def test_the_format_is_read_from_the_last_line(tmp_dir: Path) -> None:
    """A toplevel whose name holds a newline: the format is the LAST line of
    `rev-parse --show-toplevel --show-object-format`, never the second."""
    repo = tmp_dir / "x\nsha256"
    repo.mkdir()
    init_repo(repo)
    assert ObjectFormats().lookup(repo) == "sha1"


def test_a_toplevel_ending_in_a_space_is_cached_and_forgotten_verbatim(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only git's trailing "\\n" is stripped from the toplevel: a stripped space would
    key the cache on another path, and forget() would never drop the entry."""
    repo = tmp_dir / "repo "
    repo.mkdir()
    init_repo(repo)
    formats = ObjectFormats()
    formats.lookup(repo)
    formats.forget(repo)
    seen = calls(monkeypatch)
    formats.lookup(repo)
    assert sum("--show-object-format" in c for c in seen) == 1
