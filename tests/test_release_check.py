"""The release workflow's checks (`scripts/release_check.py`): the tag names the
version both copies carry, and the sdist holds exactly the files git tracks."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_check", ROOT / "scripts" / "release_check.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project(tmp_path: Path, pyproject: str, package: str) -> Path:
    (tmp_path / "src" / "diffbell").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "diffbell"\nversion = "{pyproject}"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "diffbell" / "__init__.py").write_text(
        f'"""Diffbell."""\n\n__version__ = "{package}"\n', encoding="utf-8"
    )
    return tmp_path


def test_the_two_version_strings_agree_in_this_repository() -> None:
    pyproject, package = load().versions(ROOT)
    assert pyproject == package


def test_a_tag_that_names_the_version_passes(tmp_path: Path) -> None:
    assert load().check_version("v1.2.3", project(tmp_path, "1.2.3", "1.2.3")) == []


@pytest.mark.parametrize(
    ("tag", "pyproject", "package", "problem"),
    [
        ("v1.2.4", "1.2.3", "1.2.3", "tag v1.2.4 does not name version 1.2.3"),
        ("1.2.3", "1.2.3", "1.2.3", "tag 1.2.3 does not name version 1.2.3"),
        ("v1.2.3", "1.2.3", "1.2.2", "__version__ 1.2.2 differs from pyproject's 1.2.3"),
    ],
)
def test_a_mismatch_is_named(
    tmp_path: Path, tag: str, pyproject: str, package: str, problem: str
) -> None:
    assert problem in load().check_version(tag, project(tmp_path, pyproject, package))


def git_repo(tmp_path: Path, files: list[str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", *files], cwd=root, check=True)
    return root


def sdist(dist: Path, members: list[str]) -> None:
    dist.mkdir()
    with tarfile.open(dist / "diffbell-1.2.3.tar.gz", "w:gz") as tar:
        for name in [*members, "PKG-INFO"]:
            data = b"x\n"
            info = tarfile.TarInfo(f"diffbell-1.2.3/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_an_sdist_of_exactly_the_tracked_files_passes(tmp_path: Path) -> None:
    root = git_repo(tmp_path, ["README.md", "src/diffbell/__init__.py"])
    sdist(tmp_path / "dist", ["README.md", "src/diffbell/__init__.py"])
    assert load().check_sdist(tmp_path / "dist", root) == []


def test_an_untracked_or_missing_file_is_named(tmp_path: Path) -> None:
    root = git_repo(tmp_path, ["README.md", "LICENSE"])
    sdist(tmp_path / "dist", ["README.md", "docs/superpowers/plan.md"])
    assert load().check_sdist(tmp_path / "dist", root) == [
        "in the sdist but not tracked: docs/superpowers/plan.md",
        "tracked but not in the sdist: LICENSE",
    ]


def test_there_must_be_exactly_one_sdist(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    assert load().check_sdist(tmp_path / "dist", ROOT) == [
        "expected one sdist (*.tar.gz) in dist, found 0"
    ]


def test_the_command_line_exits_nonzero_on_a_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    check = load()
    assert check.main(["version", "v0.0.0-nope"]) == 1
    assert "does not name version" in capsys.readouterr().err
    assert check.main(["bogus"]) == 2
