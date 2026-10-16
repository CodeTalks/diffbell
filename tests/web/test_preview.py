from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from diffbell.config import BUILTIN_REDACT, Config, ResolvedProject
from diffbell.web import preview as preview_mod
from diffbell.web.preview import (
    Limits,
    Previewer,
    PreviewError,
    PreviewRequest,
    build_project,
    check_path,
    scan,
)

FILES = [
    "package.json",
    "sub/package.json",
    "node_modules/package.json",
    "a/node_modules/package.json",
    ".worktrees/feat/package.json",
    ".git/package.json",
    ".venv/package.json",
    "examples/package.json",
    "nested/examples/package.json",
    "a.lock/package.json",
    "x.lock",
    ".env.local",
    "deep/.env",
    ".github/workflows/ci.yml",
    ".claude/settings.local.json",
    "README.md",
]


def make_tree(root: Path, files: list[str] = FILES) -> Path:
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("SECRET-CONTENT\n")
    return root


def project(
    root: Path, watched: tuple[str, ...], excluded: tuple[str, ...] = ()
) -> ResolvedProject:
    return ResolvedProject("p", root, watched, excluded)


def names(root: Path, watched: tuple[str, ...], excluded: tuple[str, ...] = ()) -> list[str]:
    return [f.path for f in scan(project(root, watched, excluded), BUILTIN_REDACT).files]


WATCHED = ("package.json", "*.lock", ".env*", ".github/workflows/**", ".claude/settings*.json")


def test_matches_like_the_watchman_expression(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p")
    assert names(root, WATCHED, ("*.lock", "examples/**")) == [
        ".claude/settings.local.json",
        ".env.local",
        ".github/workflows/ci.yml",
        "a.lock/package.json",  # a *.lock ignore excludes files, it never prunes a.lock/
        "deep/.env",
        "nested/examples/package.json",  # anchored ignore: only the top-level examples/
        "package.json",
        "sub/package.json",
    ]


def test_always_excluded_and_anchored_ignored_directories_are_pruned(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_tree(tmp_dir / "p")
    opened: list[str] = []
    real = preview_mod._scandir

    def spy(path: Path) -> Iterator[os.DirEntry[str]]:
        opened.append(os.path.relpath(path, root))
        return real(path)

    monkeypatch.setattr(preview_mod, "_scandir", spy)
    names(root, WATCHED, ("examples/**", "deep/**"))
    for pruned in (
        "node_modules",
        "a/node_modules",
        ".worktrees",
        ".git",
        ".venv",
        "examples",
        "deep",
    ):
        assert pruned not in opened, pruned
    for walked in (".", "a", "a.lock", "nested/examples", ".github", ".claude", "sub"):
        assert walked in opened, walked


def test_nested_ignore_prunes_nested_directories(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p")
    assert "nested/examples/package.json" not in names(root, WATCHED, ("**/examples/**",))


def test_symlinks_to_files_and_directories_are_listed_not_followed(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["sub/Makefile", "real.env"])
    (root / "Makefile").symlink_to(root / "sub")  # a symlink to a directory
    (root / ".env").symlink_to(root / "real.env")  # a symlink to a file
    assert names(root, ("Makefile", ".env*")) == [".env", "Makefile", "sub/Makefile"]


def test_redaction_is_marked(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["package.json", ".env.local", "secrets/app.json"])
    result = scan(project(root, ("package.json", ".env*", "*.json")), BUILTIN_REDACT)
    assert [(f.path, f.redacted) for f in result.files] == [
        (".env.local", True),
        ("package.json", False),
        ("secrets/app.json", True),  # *secret* covers directory components
    ]
    assert (result.total, result.truncated) == (3, False)


def test_file_cap_truncates_but_counts_everything(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", [f"d{i}/package.json" for i in range(5)])
    result = scan(project(root, ("package.json",)), (), Limits(max_files=3))
    assert (len(result.files), result.total, result.truncated) == (3, 5, True)
    assert result.total_exact  # the file cap keeps counting


def test_scan_cap_truncates(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", [f"f{i}.txt" for i in range(50)])
    result = scan(project(root, ("*.txt",)), (), Limits(max_scanned=10))
    assert result.truncated and result.total == 10
    assert not result.total_exact  # a lower bound: "10+"


def test_time_cap_truncates(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", [f"f{i}.txt" for i in range(50)])
    ticks = itertools.count()
    result = scan(project(root, ("*.txt",)), (), Limits(max_seconds=5), clock=lambda: next(ticks))
    assert result.truncated and result.total < 50
    assert not result.total_exact


def test_a_complete_walk_is_exact(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["package.json", "sub/package.json"])
    result = scan(project(root, ("package.json",)), ())
    assert (result.total, result.truncated, result.total_exact) == (2, False, True)


def test_unreadable_directory_is_skipped(tmp_dir: Path) -> None:
    root = make_tree(tmp_dir / "p", ["package.json", "locked/package.json"])
    os.chmod(root / "locked", 0o000)
    try:
        assert names(root, ("package.json",)) == ["package.json"]
    finally:
        os.chmod(root / "locked", 0o755)


def test_directory_deleted_mid_walk_is_skipped(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_tree(tmp_dir / "p", ["package.json", "gone/package.json"])
    real = preview_mod._scandir

    def vanishing(path: Path) -> Iterator[os.DirEntry[str]]:
        if Path(path).name == "gone":
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(preview_mod, "_scandir", vanishing)
    assert names(root, ("package.json",)) == ["package.json"]


def test_check_path_rules(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_dir / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    (home / "file.txt").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    assert check_path(str(proj), home) == proj
    assert check_path("~/proj", home) == proj
    for bad in (
        "~",
        str(home),
        str(tmp_dir),
        "relative/path",
        str(home / "file.txt"),
        str(home / "missing"),
    ):
        with pytest.raises(PreviewError):
            check_path(bad, home)
    (home / "escape").symlink_to(tmp_dir)  # a symlink out of home is judged by its realpath
    with pytest.raises(PreviewError):
        check_path(str(home / "escape"), home)


def test_check_path_messages_never_echo_the_typed_path(tmp_dir: Path) -> None:
    home = tmp_dir / "home"
    home.mkdir()
    (home / "file.txt").write_text("x")
    typed = [str(home / "MARKER-missing"), str(home / "file.txt"), "MARKER-relative", "~/MARKER"]
    for raw in typed:
        with pytest.raises(PreviewError) as caught:
            check_path(raw, home)
        message = str(caught.value)
        assert "MARKER" not in message and "file.txt" not in message, message
    with pytest.raises(PreviewError, match=r"^not an existing directory under your home folder$"):
        check_path(str(home / "MARKER-missing"), home)


def test_check_path_rejects_a_nul_byte(tmp_dir: Path) -> None:
    home = tmp_dir / "home"
    home.mkdir()
    for bad in ("/a\0b", "~/a\0b", f"{home}/\0"):
        with pytest.raises(PreviewError, match="NUL"):
            check_path(bad, home)


def test_check_path_turns_syscall_errors_into_preview_errors(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_dir / "home"
    (home / "proj").mkdir(parents=True)

    def broken(path: object) -> str:
        raise OSError(40, "Too many levels of symbolic links")

    monkeypatch.setattr(os.path, "realpath", broken)
    with pytest.raises(PreviewError, match="cannot be read: Too many levels") as caught:
        check_path(str(home / "proj"), home)
    assert str(home) not in str(caught.value)


def test_build_project_uses_the_global_lists_and_validates(tmp_dir: Path) -> None:
    config = Config(defaults=["Procfile"], ignore=["vendor/**"])
    req = PreviewRequest(str(tmp_dir), ["x.txt"], ["a/**"], use_defaults=True)
    built = build_project(tmp_dir, req, config)
    assert built.watched[-2:] == ("Procfile", "x.txt")
    assert built.excluded == ("vendor/**", "a/**")
    with pytest.raises(PreviewError, match="relative to the project root"):
        build_project(tmp_dir, PreviewRequest(str(tmp_dir), ["/abs"], [], True), config)
    with pytest.raises(PreviewError, match="nothing to watch"):
        build_project(tmp_dir, PreviewRequest(str(tmp_dir), [], [], False), config)


async def test_previewer_runs_at_most_two_walks_at_once(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_dir
    proj = make_tree(home / "p", ["package.json"])
    lock = threading.Lock()
    running = peak = 0

    def slow_scan(*args: object, **kwargs: object) -> preview_mod.PreviewResult:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.1)
        with lock:
            running -= 1
        return preview_mod.PreviewResult([], 0, False)

    monkeypatch.setattr(preview_mod, "scan", slow_scan)
    previewer = Previewer(home=home)
    req = PreviewRequest(str(proj), [], [], use_defaults=True)
    await asyncio.gather(*(previewer.preview(req, Config()) for _ in range(5)))
    assert peak == 2


async def test_previewer_end_to_end(tmp_dir: Path) -> None:
    proj = make_tree(tmp_dir / "p", ["package.json", ".env.local", "node_modules/package.json"])
    result = await Previewer(home=tmp_dir).preview(
        PreviewRequest(str(proj), [], [], use_defaults=True), Config()
    )
    assert [(f.path, f.redacted) for f in result.files] == [
        (".env.local", True),
        ("package.json", False),
    ]


async def _until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_cancelled_preview_keeps_its_slot_until_the_walk_ends(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = make_tree(tmp_dir / "p", ["package.json"])
    gates = [threading.Event() for _ in range(3)]
    started: list[int] = []
    counter = itertools.count()

    def blocking_scan(*args: object, **kwargs: object) -> preview_mod.PreviewResult:
        i = next(counter)
        started.append(i)
        gates[i].wait(5)
        return preview_mod.PreviewResult([], 0, False)

    monkeypatch.setattr(preview_mod, "scan", blocking_scan)
    previewer = Previewer(home=tmp_dir)
    req = PreviewRequest(str(proj), [], [], use_defaults=True)
    first = asyncio.create_task(previewer.preview(req, Config()))
    second = asyncio.create_task(previewer.preview(req, Config()))
    await _until(lambda: len(started) == 2)
    first.cancel()  # the client went away; its walk can't be stopped
    with contextlib.suppress(asyncio.CancelledError):
        await first
    third = asyncio.create_task(previewer.preview(req, Config()))
    await asyncio.sleep(0.2)
    assert started == [0, 1]  # the cancelled walk still holds its slot
    gates[0].set()
    await _until(lambda: len(started) == 3)
    gates[1].set()
    gates[2].set()
    await asyncio.gather(second, third)


async def test_previewer_validates_in_the_worker_thread(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = make_tree(tmp_dir / "p", ["package.json"])
    threads: dict[str, threading.Thread] = {}
    real_check, real_build = preview_mod.check_path, preview_mod.build_project

    def spy_check(raw: str, home: Path) -> Path:
        threads["check_path"] = threading.current_thread()
        return real_check(raw, home)

    def spy_build(root: Path, request: PreviewRequest, config: Config) -> ResolvedProject:
        threads["build_project"] = threading.current_thread()
        return real_build(root, request, config)

    monkeypatch.setattr(preview_mod, "check_path", spy_check)
    monkeypatch.setattr(preview_mod, "build_project", spy_build)
    await Previewer(home=tmp_dir).preview(PreviewRequest(str(proj), [], [], True), Config())
    assert set(threads) == {"check_path", "build_project"}
    assert all(t is not threading.main_thread() for t in threads.values())


async def test_rejected_previews_release_their_slots(tmp_dir: Path) -> None:
    proj = make_tree(tmp_dir / "p", ["package.json"])
    previewer = Previewer(home=tmp_dir, concurrency=1)
    for bad in ("relative", str(tmp_dir / "missing"), str(tmp_dir)):
        with pytest.raises(PreviewError):
            await previewer.preview(PreviewRequest(bad, [], [], True), Config())
    with pytest.raises(PreviewError, match="relative to the project root"):
        await previewer.preview(PreviewRequest(str(proj), ["/abs"], [], True), Config())
    result = await asyncio.wait_for(
        previewer.preview(PreviewRequest(str(proj), [], [], True), Config()), 5
    )
    assert [f.path for f in result.files] == ["package.json"]
