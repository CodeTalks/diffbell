"""The file tree's folder listing (web/browse.py, UI refine spec §3.2)."""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
from pathlib import Path

import pytest

from diffbell.config import BUILTIN_REDACT, Config, effective_lists
from diffbell.config import ProjectConfig as PC
from diffbell.web import browse as browse_mod
from diffbell.web import preview as preview_mod
from diffbell.web.browse import (
    BrowseError,
    BrowseLimits,
    BrowseRequest,
    BrowseResult,
    always_excluded,
    list_dir,
    open_dir,
    run,
)
from diffbell.web.preview import Previewer, PreviewError, PreviewRequest, PreviewResult, scan
from tests.web.test_preview import make_tree, project

WATCHED = ("package.json", "*.lock", ".env*", ".github/workflows/**", ".claude/settings*.json")
IGNORED = ("*.lock", "examples/**")


def request(root: Path, rel: str = "", **changes: object) -> BrowseRequest:
    fields: dict[str, object] = {
        "path": str(root),
        "dir": rel,
        "patterns": [],
        "ignore": [],
        "use_defaults": True,
    }
    fields.update(changes)
    return BrowseRequest(**fields)  # type: ignore[arg-type]


def listing(root: Path, rel: str = "", **kw: object) -> BrowseResult:
    return list_dir(root, rel, WATCHED, IGNORED, BUILTIN_REDACT, **kw)  # type: ignore[arg-type]


def selected_by_browsing(root: Path, rel: str = "") -> set[str]:
    """Every file the tree shows as watched (matched, not ignored), folder by folder."""
    found: set[str] = set()
    for entry in listing(root, rel).entries:
        path = f"{rel}/{entry.name}" if rel else entry.name
        if entry.type == "dir":
            if not entry.excluded:
                found |= selected_by_browsing(root, path)
        elif entry.matched_by is not None and entry.ignored_by is None:
            found.add(path)
    return found


def test_the_tree_agrees_with_the_preview(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p")
    previewed = scan(project(root, WATCHED, IGNORED), BUILTIN_REDACT)
    assert selected_by_browsing(root) == {f.path for f in previewed.files}


def test_entries_are_sorted_folders_first_then_by_name(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["beta.txt", "Alpha.txt", "zeta/x", "Delta/x", "alpine.txt"])
    assert [(e.name, e.type) for e in listing(root).entries] == [
        ("Delta", "dir"),
        ("zeta", "dir"),
        ("Alpha.txt", "file"),
        ("alpine.txt", "file"),
        ("beta.txt", "file"),
    ]


def test_matched_by_and_ignored_by_name_the_first_pattern(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["x.lock", "package.json", "README.md", ".env.local"])
    entries = {e.name: e for e in listing(root).entries}
    assert (entries["x.lock"].matched_by, entries["x.lock"].ignored_by) == ("*.lock", "*.lock")
    assert (entries["package.json"].matched_by, entries["package.json"].ignored_by) == (
        "package.json",
        None,
    )
    assert (entries["README.md"].matched_by, entries["README.md"].ignored_by) == (None, None)
    assert entries[".env.local"].redacted and not entries["package.json"].redacted


def test_built_in_patterns_are_prefixed(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["x.lock", "notes.txt"])
    result = run(request(root, patterns=["*.txt"]), Config(), tmp_dir)
    by_name = {e.name: e.matched_by for e in result.entries}
    assert by_name == {"x.lock": "built-in: *.lock", "notes.txt": "*.txt"}
    only_mine = run(request(root, patterns=["*.txt"], use_defaults=False), Config(), tmp_dir)
    assert {e.name: e.matched_by for e in only_mine.entries} == {
        "x.lock": None,
        "notes.txt": "*.txt",
    }


def test_global_extra_defaults_are_not_marked_built_in(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["Procfile"])
    result = run(request(root), Config(defaults=["Procfile"]), tmp_dir)
    assert [e.matched_by for e in result.entries] == ["Procfile"]


def test_folders_carry_no_match_and_excluded_folders_are_flagged(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["node_modules/package.json", "src/package.json"])
    entries = {e.name: e for e in listing(root).entries}
    assert entries["node_modules"].excluded and not entries["src"].excluded
    assert entries["src"].matched_by is None and entries["src"].ignored_by is None


def test_a_redacted_folder_is_flagged(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["secrets/app.json"])
    (entry,) = listing(root).entries
    assert entry.redacted
    (inner,) = listing(root, "secrets").entries
    assert inner.redacted


def test_symlinks_are_listed_never_followed(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["real/package.json"])
    (root / "link").symlink_to(root / "real")
    (root / "package.json").symlink_to(root / "real" / "package.json")
    entries = {e.name: e for e in listing(root).entries}
    assert entries["link"].type == "symlink" and entries["package.json"].type == "symlink"
    assert entries["package.json"].matched_by == "package.json"
    with pytest.raises(BrowseError, match="symlink"):
        listing(root, "link")
    with pytest.raises(BrowseError, match="symlink"):
        listing(root, "link/deeper")


@pytest.mark.parametrize(
    "rel",
    [
        "..",
        "../MARKER",
        "MARKER/../b",
        "/MARKER",
        "MARKER//b",
        "MARKER/",
        "./MARKER",
        "MARKER/\0",
        "node_modules",
        "MARKER/.git",
    ],
)
def test_bad_folders_are_refused_without_naming_them(tmp_dir: Path, rel: str) -> None:
    root = make_tree(tmp_dir / "p", ["MARKER/b/c"])
    with pytest.raises(BrowseError) as caught:
        open_dir(root, rel)
    assert "MARKER" not in str(caught.value)


def test_a_file_is_not_a_folder(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["a/file.txt"])
    with pytest.raises(BrowseError, match="not a directory"):
        open_dir(root, "a/file.txt")


def test_a_vanished_folder_is_unreadable(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["package.json"])
    result = listing(root, "gone")
    assert (result.entries, result.error, result.truncated) == ([], "unreadable", False)


def test_an_unreadable_folder_is_reported(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["locked/package.json"])
    os.chmod(root / "locked", 0o000)
    try:
        assert listing(root, "locked").error == "unreadable"
    finally:
        os.chmod(root / "locked", 0o755)


def test_the_entry_cap_truncates(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", [f"f{i:02}.txt" for i in range(30)])
    result = listing(root, limits=BrowseLimits(max_entries=10))
    assert len(result.entries) == 10 and result.truncated


def test_the_time_cap_truncates(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", [f"f{i:02}.txt" for i in range(30)])
    ticks = itertools.count()
    result = listing(root, limits=BrowseLimits(max_seconds=5), clock=lambda: next(ticks))
    assert result.truncated and len(result.entries) < 30


def test_run_checks_the_path_and_the_patterns_like_the_preview(tmp_dir: Path) -> None:
    home = tmp_dir / "home"
    root = make_tree(home / "p", ["package.json"])
    for bad in (str(tmp_dir), str(home), "relative", str(home / "missing")):
        with pytest.raises(PreviewError):
            run(request(Path(bad)), Config(), home)
    with pytest.raises(PreviewError, match="relative to the project root"):
        run(request(root, patterns=["/abs"]), Config(), home)
    # Nothing watched yet is fine for the tree (the first file gets ticked here).
    result = run(request(root, use_defaults=False), Config(), home)
    assert [(e.name, e.matched_by) for e in result.entries] == [("package.json", None)]


def test_a_symlinked_project_root_inside_home_is_accepted(tmp_dir: Path) -> None:
    home = tmp_dir / "home"
    make_tree(home / "real", ["package.json"])
    (home / "alias").symlink_to(home / "real")
    result = run(request(home / "alias"), Config(), home)
    assert [e.name for e in result.entries] == ["package.json"]


def test_effective_lists_match_the_daemon(tmp_dir: Path) -> None:
    """browse.run builds the same lists as config.effective_lists."""
    config = Config(defaults=["Procfile"], ignore=["vendor/**"])
    watched, excluded = effective_lists(
        config, PC(path=str(tmp_dir), patterns=["x"], ignore=["y/**"])
    )
    root = make_tree(tmp_dir / "p", ["x", "Procfile", "vendor/x"])
    result = run(request(root, patterns=["x"], ignore=["y/**"]), config, tmp_dir)
    assert {e.name: e.matched_by for e in result.entries if e.type == "file"} == {
        "Procfile": "Procfile",
        "x": "x",
    }
    assert watched[-2:] == ("Procfile", "x") and excluded == ("vendor/**", "y/**")


async def test_browse_shares_the_preview_slots(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_tree(tmp_dir / "p", ["package.json"])
    lock = threading.Lock()
    running = peak = 0

    def slow(*args: object, **kwargs: object) -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.1)
        with lock:
            running -= 1

    def slow_scan(*args: object, **kwargs: object) -> PreviewResult:
        slow()
        return PreviewResult([], 0, False)

    def slow_list(*args: object, **kwargs: object) -> BrowseResult:
        slow()
        return BrowseResult("", [], False, None)

    monkeypatch.setattr(preview_mod, "scan", slow_scan)
    monkeypatch.setattr(browse_mod, "list_dir", slow_list)
    previewer = Previewer(home=tmp_dir)
    preview_req = PreviewRequest(str(root), [], [], use_defaults=True)
    await asyncio.gather(
        *(previewer.preview(preview_req, Config()) for _ in range(3)),
        *(previewer.in_slot(run, request(root), Config(), tmp_dir) for _ in range(3)),
    )
    assert peak == 2


def test_excluded_folders_are_matched_like_watchman(tmp_dir: Path) -> None:
    """Case- and form-insensitive, as the preview's prune probe and Watchman."""
    root = make_tree(tmp_dir / "p", ["Node_Modules/x", ".GIT/x", "src/.Worktrees/x", "src/a"])
    entries = {e.name: e.excluded for e in listing(root).entries}
    assert entries == {"Node_Modules": True, ".GIT": True, "src": False}
    assert {e.name: e.excluded for e in listing(root, "src").entries} == {
        ".Worktrees": True,
        "a": False,
    }
    for rel in ("Node_Modules", ".GIT", "src/.Worktrees", "Node_Modules/deeper"):
        with pytest.raises(BrowseError, match="always excluded"):
            open_dir(root, rel)
    assert always_excluded("a/NODE_MODULES") and not always_excluded("node_modules_x")


def test_a_component_that_is_a_symlink_is_refused_by_the_open(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open itself (O_NOFOLLOW, relative to the parent's descriptor) refuses a
    symlinked component, so a folder swapped for a symlink after any earlier
    check can't be entered. The walk lists the descriptor, never a path."""
    root = make_tree(tmp_dir / "p", ["real/inner/x", "outside/secret"])
    (root / "real" / "swapped").symlink_to(root / "outside")
    with pytest.raises(BrowseError, match="symlink"):
        open_dir(root, "real/swapped")
    seen: list[object] = []
    real_scandir = browse_mod._scandir

    def spy(target: int) -> object:
        seen.append(target)
        return real_scandir(target)

    monkeypatch.setattr(browse_mod, "_scandir", spy)
    assert [e.name for e in listing(root, "real/inner").entries] == ["x"]
    assert len(seen) == 1 and isinstance(seen[0], int)


def test_every_descriptor_is_closed(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["a/b/c", "a/file.txt"])
    (root / "a" / "link").symlink_to(root / "a" / "b")
    before = len(os.listdir("/dev/fd"))
    for _ in range(20):
        listing(root, "a/b")
        listing(root, "a/gone")
        # A name that can't be encoded fails in os.open with a ValueError, not an
        # OSError: still a refusal, and the parent's descriptor is still closed.
        for bad in ("a/link", "a/file.txt", "\ud800", "a/\ud800"):
            with pytest.raises(BrowseError):
                listing(root, bad)
    assert len(os.listdir("/dev/fd")) == before
