"""Homebrew `resource` blocks for the diffbell formula, from `uv.lock` (ADR 0021).

    uv run python scripts/formula_resources.py [uv.lock] [--python 3.15]

The runtime dependency closure of `diffbell` (no dev group), with environment
markers evaluated for macOS on the formula's Python, minus what Homebrew formulae
provide (`HOMEBREW_PROVIDED`: certifi, and pydantic with its own dependencies).
Each block names the locked version's sdist and its sha256, so the formula builds
exactly what CI tested. `scripts/bump_formula.py` uses `render`.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any

from packaging.markers import Marker

# The formula depends on these Homebrew formulae instead of building them.
HOMEBREW_PROVIDED = frozenset(
    {
        "certifi",
        "pydantic",
        "pydantic-core",
        "annotated-types",
        "typing-extensions",
        "typing-inspection",
    }
)
PROJECT = "diffbell"


class LockError(Exception):
    pass


def environment(python: str) -> dict[str, str]:
    """Marker variables for Homebrew on Apple silicon with Python `python` (X.Y)."""
    return {
        "implementation_name": "cpython",
        "implementation_version": f"{python}.0",
        "os_name": "posix",
        "platform_machine": "arm64",
        "platform_python_implementation": "CPython",
        "platform_system": "Darwin",
        "python_full_version": f"{python}.0",
        "python_version": python,
        "sys_platform": "darwin",
    }


def _applies(dep: dict[str, Any], env: dict[str, str]) -> bool:
    marker = dep.get("marker")
    return marker is None or Marker(marker).evaluate(env)


def runtime_packages(lock: dict[str, Any], python: str) -> list[dict[str, Any]]:
    """The locked packages the installed tool needs, sorted by name."""
    packages: dict[str, dict[str, Any]] = {}
    for package in lock["package"]:
        if package["name"] in packages:
            raise LockError(f"{package['name']} is locked more than once (forked resolution)")
        packages[package["name"]] = package
    env = environment(python)
    seen: set[str] = set()
    todo = [d for d in packages[PROJECT].get("dependencies", []) if _applies(d, env)]
    while todo:
        dep = todo.pop()
        name = dep["name"]
        package = packages[name]
        extras = dep.get("extra", [])
        if name in seen and not extras:
            continue
        seen.add(name)
        todo += [d for d in package.get("dependencies", []) if _applies(d, env)]
        optional = package.get("optional-dependencies", {})
        for extra in extras:
            todo += [d for d in optional.get(extra, []) if _applies(d, env)]
    return [packages[name] for name in sorted(seen - HOMEBREW_PROVIDED)]


def block(package: dict[str, Any]) -> str:
    sdist = package.get("sdist")
    if not sdist or "url" not in sdist:
        raise LockError(f"{package['name']} {package['version']} has no sdist in uv.lock")
    sha256 = str(sdist["hash"]).removeprefix("sha256:")
    return (
        f'  resource "{package["name"]}" do\n'
        f'    url "{sdist["url"]}"\n'
        f'    sha256 "{sha256}"\n'
        "  end\n"
    )


def render(lock_text: str, python: str) -> str:
    """Every resource block, separated by blank lines, ending in a newline."""
    lock = tomllib.loads(lock_text)
    return "\n".join(block(p) for p in runtime_packages(lock, python))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lock", nargs="?", type=Path, default=Path("uv.lock"))
    parser.add_argument("--python", default="3.15", help="the formula's Python (X.Y)")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.lock.read_text(encoding="utf-8"), args.python))
    except LockError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
