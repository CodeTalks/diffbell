"""The sdist is an allowlist of what git tracks, so the local design notes
(`docs/superpowers/`, gitignored) never ship."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def sdist() -> dict[str, list[str]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets: dict[str, dict[str, list[str]]] = config["tool"]["hatch"]["build"]["targets"]
    return targets["sdist"]


def covered(path: str, include: list[str]) -> bool:
    return any(path == entry[1:] or path.startswith(entry[1:] + "/") for entry in include)


def test_the_local_notes_are_excluded_and_never_included() -> None:
    assert "/docs/superpowers" in sdist()["exclude"]
    assert not any(covered("docs/superpowers/x.md", [entry]) for entry in sdist()["include"])


def test_every_tracked_file_is_in_the_allowlist() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    missing = [path for path in tracked if not covered(path, sdist()["include"])]
    assert missing == []
