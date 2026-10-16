"""The release workflow's checks (`.github/workflows/release.yml`). Stdlib only.

    python3 -I scripts/release_check.py version v0.1.0   # the tag names the version
    python3 -I scripts/release_check.py sdist dist        # the sdist is the tracked files

`version`: the tag is `v` + the version, and `pyproject.toml` and
`diffbell.__version__` carry the same one. `sdist`: the one `*.tar.gz` in the folder
holds exactly the files git tracks (plus the generated `PKG-INFO`), so nothing local,
such as the gitignored design notes, ships. Exit 1 names every problem; exit 2 is a
usage error.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


def versions(root: Path = ROOT) -> tuple[str, str]:
    """(pyproject's version, `diffbell.__version__`), read without importing."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    init = (root / "src" / "diffbell" / "__init__.py").read_text(encoding="utf-8")
    match = VERSION.search(init)
    if match is None:
        raise SystemExit("src/diffbell/__init__.py has no __version__ line")
    return str(pyproject["project"]["version"]), match.group(1)


def check_version(tag: str, root: Path = ROOT) -> list[str]:
    pyproject, package = versions(root)
    problems = []
    if package != pyproject:
        problems.append(f"__version__ {package} differs from pyproject's {pyproject}")
    if tag != f"v{pyproject}":
        problems.append(f"tag {tag} does not name version {pyproject}")
    return problems


def tracked(root: Path) -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return {path for path in out.split("\0") if path}


def members(archive: Path) -> set[str]:
    """The sdist's files, without its top folder and the generated PKG-INFO."""
    with tarfile.open(archive) as tar:
        names = {m.name.split("/", 1)[1] for m in tar.getmembers() if m.isfile() and "/" in m.name}
    return names - {"PKG-INFO"}


def check_sdist(dist: Path, root: Path = ROOT) -> list[str]:
    archives = sorted(dist.glob("*.tar.gz"))
    if len(archives) != 1:
        return [f"expected one sdist (*.tar.gz) in {dist.name}, found {len(archives)}"]
    shipped, files = members(archives[0]), tracked(root)
    return [f"in the sdist but not tracked: {p}" for p in sorted(shipped - files)] + [
        f"tracked but not in the sdist: {p}" for p in sorted(files - shipped)
    ]


def main(argv: list[str]) -> int:
    match argv:
        case ["version", tag]:
            problems = check_version(tag)
        case ["sdist", dist]:
            problems = check_sdist(Path(dist))
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
