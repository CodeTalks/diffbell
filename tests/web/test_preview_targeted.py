"""The targeted preview walk (Plan 6 spec §5): same files as the whole-tree walk,
far fewer entries when every pattern is anchored."""

from __future__ import annotations

import contextlib
import os
import random
import time
import unicodedata
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest

from diffbell.config import BUILTIN_REDACT, ResolvedProject
from diffbell.patterns import is_redacted
from diffbell.web import preview as preview_mod
from diffbell.web.preview import (
    Limits,
    PreviewFile,
    Start,
    _entries,
    _pruned,
    _selected,
    plan_starts,
    scan,
)


def oracle(project: ResolvedProject, redact: tuple[str, ...]) -> list[PreviewFile]:
    """The whole-tree walk as `scan` did it before Plan 6 (no limits): every folder
    not pruned, every file and symlink decided by `_selected`."""
    prune_ignores = [p for p in project.excluded if "/" in p and p.endswith("/**")]
    found: list[str] = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        for entry in _entries(project.path / rel_dir if rel_dir else project.path):
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_symlink():
                    candidate = True
                elif entry.is_dir(follow_symlinks=False):
                    if not _pruned(rel, prune_ignores):
                        stack.append(rel)
                    continue
                else:
                    candidate = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if candidate and _selected(rel, project):
                found.append(rel)
    return [PreviewFile(rel, is_redacted(rel, redact)) for rel in sorted(found)]


BIG = Limits(max_files=100_000, max_scanned=10_000_000, max_seconds=600)


def test_plan_starts_by_pattern_kind() -> None:
    assert plan_starts(("src/a/main.py", "docs/*.md", "src/**/x.py", "a/b[0-9]/c")) == [
        Start(("src", "a"), descend=False),
        Start(("docs",), descend=True),
        Start(("src",), descend=True),
        Start(("a",), descend=True),
    ]
    assert plan_starts(("src/a/main.py", "*.lock")) is None  # one "anywhere" pattern
    assert plan_starts(("src/a/main.py", "*/x.json")) is None  # an empty literal prefix
    assert plan_starts(("**/x.json",)) is None
    assert plan_starts(("a/{b}/c",)) == [Start(("a",), descend=True)]  # "{" widens only


def make(root: Path, files: list[str]) -> None:
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")


def test_literal_paths_list_only_their_folders(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_dir / "p"
    make(root, ["src/a/main.py", "src/a/other.py", "src/a/deep/main.py", "src/b/x.py", "z.py"])
    listed: list[str] = []
    real = preview_mod._scandir

    def spy(path: Path) -> Iterator[os.DirEntry[str]]:
        listed.append(os.path.relpath(path, root))
        return real(path)

    monkeypatch.setattr(preview_mod, "_scandir", spy)
    result = scan(ResolvedProject("p", root, ("src/a/main.py",), ()), ())
    assert [f.path for f in result.files] == ["src/a/main.py"]
    assert sorted(listed) == [".", "src", "src/a"]  # never src/b, never src/a/deep


def test_a_literal_pattern_finds_the_disks_spelling(tmp_dir: Path) -> None:
    root = tmp_dir / "p"
    nfd = unicodedata.normalize("NFD", "Café")
    make(root, [f"{nfd}/Settings.JSON"])
    pattern = unicodedata.normalize("NFC", "café/settings.json")
    result = scan(ResolvedProject("p", root, (pattern,), ()), ())
    assert [f.path for f in result.files] == [f"{nfd}/Settings.JSON"]


def test_a_symlinked_prefix_ends_the_start_like_the_whole_walk(tmp_dir: Path) -> None:
    root = tmp_dir / "p"
    make(root, ["real/x.py", "real/sub/y.py", "plain/x.py"])
    (root / "link").symlink_to(root / "real")
    (root / "plain" / "file-link").symlink_to(root / "real" / "x.py")
    watched = ("link/x.py", "link/**/y.py", "plain/file-link", "plain/file-link/z")
    project = ResolvedProject("p", root, watched, ())
    assert [f.path for f in scan(project, ()).files] == ["plain/file-link"]
    assert scan(project, ()).files == oracle(project, ())


def test_a_missing_start_contributes_nothing(tmp_dir: Path) -> None:
    root = tmp_dir / "p"
    make(root, ["src/x.py"])
    project = ResolvedProject("p", root, ("missing/x.py", "src/x.py", "src/nope/**"), ())
    assert [f.path for f in scan(project, ()).files] == ["src/x.py"]


def test_overlapping_starts_count_each_file_once(tmp_dir: Path) -> None:
    root = tmp_dir / "p"
    make(root, ["src/a/x.py", "src/a/b/x.py", "src/c.py"])
    watched = ("src/a/x.py", "src/**/x.py", "SRC/A/X.PY", "src/a/b/x.py", "src/*.py")
    result = scan(ResolvedProject("p", root, watched, ()), ())
    assert [f.path for f in result.files] == ["src/a/b/x.py", "src/a/x.py", "src/c.py"]
    assert result.total == 3


def test_pruned_prefixes_are_never_walked(tmp_dir: Path) -> None:
    root = tmp_dir / "p"
    make(root, ["node_modules/a/x.json", "examples/x.json", "ok/x.json"])
    watched = ("node_modules/a/x.json", "examples/x.json", "ok/x.json")
    project = ResolvedProject("p", root, watched, ("examples/**",))
    assert [f.path for f in scan(project, ()).files] == ["ok/x.json"]


def test_limits_still_apply_to_a_targeted_walk(tmp_dir: Path) -> None:
    root = tmp_dir / "p"
    make(root, [f"src/f{i}.py" for i in range(50)])
    result = scan(ResolvedProject("p", root, ("src/*.py",), ()), (), Limits(max_scanned=10))
    assert result.truncated and not result.total_exact and result.total < 50


def test_resolving_a_listed_folder_again_still_honours_the_deadline(tmp_dir: Path) -> None:
    """A second start through an already listed (cached) folder ticks no entry,
    so the per-entry loop must check the deadline itself."""
    root = tmp_dir / "p"
    make(root, [f"big/d{i}/x.py" for i in range(50)])
    now = [0.0]
    walk = preview_mod._Walk(
        ResolvedProject("p", root, (), ()), Limits(max_seconds=10), lambda: now[0]
    )
    assert walk.resolve(Start(("big", "d1"), descend=False)) == ["big/d1"]
    scanned = walk.scanned
    now[0] = 11.0
    assert walk.resolve(Start(("big", "d2"), descend=False)) == []
    assert walk.stopped and walk.truncated
    assert walk.scanned == scanned  # the deadline check counts no entry


# -- the property test --------------------------------------------------------------

NAMES = [
    "src",
    "Src",
    "a",
    "b",
    "docs",
    "café",
    unicodedata.normalize("NFD", "café"),
    "node_modules",
    "examples",
    ".github",
    "x.py",
    "X.PY",
    "main.py",
    "notes.md",
    "package.json",
    ".env",
    "secrets",
]
PATTERNS = [
    "src/a/main.py",
    "SRC/A/MAIN.PY",
    "src/x.py",
    "café/x.py",
    unicodedata.normalize("NFD", "café/notes.md"),
    "docs/notes.md",
    "missing/x.py",
    "link/x.py",
    "link/a/x.py",
    "a/link",
    "src/**/x.py",
    "docs/*.md",
    "src/a/**",
    ".github/**",
    "a/b/*",
    "secrets/*.json",
    "examples/package.json",
    "node_modules/package.json",
    "src/?.py",
    "src/a/mai?.py",
    "src/[a-c]/main.py",
    "boo[k]/notes.md",
    "[Ss]rc/x.py",
    "café/[!x]*.md",
]
ANYWHERE = ["x.py", "*.md", "package.json", ".env*"]
IGNORES = ["examples/**", "src/a/**", "*.md", "**/examples/**", "a/b/**", "secrets/**"]


def random_tree(root: Path, rng: random.Random) -> None:
    root.mkdir()
    folders = [root]
    for _ in range(rng.randint(5, 40)):
        parent = rng.choice(folders)
        name = rng.choice(NAMES)
        target = parent / name
        try:
            kind = rng.random()
            if kind < 0.45:
                target.mkdir()
                folders.append(target)
            elif kind < 0.85:
                target.write_text("x\n")
            else:
                target.symlink_to(rng.choice(folders))
        except OSError:  # taken: APFS ignores case and form ("Src" is "src")
            continue
    link = rng.choice(folders) / "link"
    if not os.path.lexists(link):
        link.symlink_to(rng.choice(folders))


@pytest.mark.parametrize("seed", range(150))
def test_the_targeted_walk_matches_the_whole_tree_walk(tmp_dir: Path, seed: int) -> None:
    rng = random.Random(seed)
    root = tmp_dir / f"t{seed}"
    random_tree(root, rng)
    watched = tuple(rng.sample(PATTERNS, rng.randint(1, 6)))
    if rng.random() < 0.25:
        watched += (rng.choice(ANYWHERE),)
    excluded = tuple(rng.sample(IGNORES, rng.randint(0, 2)))
    project = ResolvedProject("p", root, watched, excluded)
    redact = (*BUILTIN_REDACT, "*.py")
    result = scan(project, redact, BIG)
    assert result.files == oracle(project, redact), (watched, excluded)
    assert result.total == len(result.files) and result.total_exact


# -- the cost test ------------------------------------------------------------------

# 11 literal paths (no glob character), a fixed fixture rather than any live config,
# so the test keeps its meaning whatever the user's config says.
LITERALS = (
    "src/app/settings.py",
    "src/app/models/user.py",
    "src/app/models/order.py",
    "src/app/views/home.py",
    "docs/guide.md",
    "docs/install.md",
    "config/app.toml",
    "config/logging.yaml",
    "scripts/deploy.sh",
    "templates/base.html",
    "deploy/Procfile",
)


class Counting:
    """A `_scandir` stand-in that counts every entry it yields."""

    def __init__(self) -> None:
        self.touched = 0

    def __call__(self, path: Path) -> contextlib.closing[Generator[os.DirEntry[str]]]:
        return contextlib.closing(self._entries(path))

    def _entries(self, path: Path) -> Generator[os.DirEntry[str]]:
        with os.scandir(path) as it:
            for entry in it:
                self.touched += 1
                yield entry


def test_literal_patterns_touch_few_entries_among_50000_decoys(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_dir / "shop"
    make(root, list(LITERALS))
    for folder in range(50):
        decoys = root / "src" / "app" / "cache" / f"d{folder:02}"
        decoys.mkdir(parents=True)
        for i in range(1000):
            os.close(os.open(decoys / f"f{i}.bin", os.O_CREAT | os.O_WRONLY, 0o600))
    counter = Counting()
    monkeypatch.setattr(preview_mod, "_scandir", counter)
    start = time.monotonic()
    result = scan(ResolvedProject("shop", root, LITERALS, ()), BUILTIN_REDACT)
    elapsed = time.monotonic() - start
    assert (result.total, result.total_exact, result.truncated) == (11, True, False)
    assert counter.touched < 200, counter.touched
    assert elapsed < 1.0
    # The whole-tree walk of the same folder stops at the entry cap with "N+".
    whole = scan(
        ResolvedProject("shop", root, (*LITERALS, "*.lock"), ()), (), Limits(max_scanned=20_000)
    )
    assert not whole.total_exact
