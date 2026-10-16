"""Architecture rules for the web package (CLAUDE.md): it never reaches Watchman."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import diffbell

WEB = Path(diffbell.__file__).parent / "web"
FORBIDDEN = ("pywatchman", "diffbell.wmclient", "diffbell.watcher")


def imported_names(source: str, package: str = "diffbell.web") -> set[str]:
    """Every dotted name a module imports. ``package`` is the importing file's package,
    used to resolve relative imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)]
                module = ".".join([*base, *([module] if module else [])])
            if module:
                names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}" if module else alias.name)
    return names


def forbidden_imports(source: str, package: str = "diffbell.web") -> set[str]:
    return {
        n
        for n in imported_names(source, package)
        if any(n == bad or n.startswith(bad + ".") for bad in FORBIDDEN)
    }


def package_of(path: Path) -> str:
    rel = path.parent.relative_to(WEB.parent.parent)
    return ".".join(rel.parts)


def test_web_package_never_imports_watchman() -> None:
    sources = sorted(WEB.rglob("*.py"))
    assert sources, "the web package is missing"
    for path in sources:
        bad = forbidden_imports(path.read_text(encoding="utf-8"), package_of(path))
        assert not bad, f"{path.relative_to(WEB)} imports {bad}"


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("import pywatchman", "diffbell.web"),
        ("import pywatchman.client", "diffbell.web"),
        ("from pywatchman import client", "diffbell.web"),
        ("import diffbell.wmclient", "diffbell.web"),
        ("import diffbell.watcher as w", "diffbell.web"),
        ("from diffbell.wmclient import X", "diffbell.web"),
        ("from diffbell import wmclient", "diffbell.web"),
        ("from diffbell import watcher", "diffbell.web"),
        ("from diffbell import store, watcher as w", "diffbell.web"),
        ("from .. import watcher", "diffbell.web"),
        ("from ..wmclient import X", "diffbell.web"),
        ("from ..watcher.sub import X", "diffbell.web"),
        ("from ... import wmclient", "diffbell.web.sub"),
        ("from ...wmclient import X", "diffbell.web.sub"),
        ("def f():\n    from diffbell import watcher\n", "diffbell.web"),
    ],
)
def test_scanner_catches_every_evasion_form(source: str, package: str) -> None:
    assert forbidden_imports(source, package), source


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("from diffbell import watcher_x", "diffbell.web"),
        ("import diffbell.watcher_x", "diffbell.web"),
        ("from diffbell.watcher_x import Y", "diffbell.web"),
        ("from diffbell import store, config", "diffbell.web"),
        ("from . import app", "diffbell.web"),
        ("from .sibling import X", "diffbell.web"),
        ("from ..store import Store", "diffbell.web"),
        ("from .. import watcher_x", "diffbell.web"),
        ("from .. import watcher", "diffbell.web.sub"),  # diffbell.web.watcher
        ("from . import watcher", "diffbell.web"),  # diffbell.web.watcher
    ],
)
def test_scanner_has_no_false_positives(source: str, package: str) -> None:
    assert not forbidden_imports(source, package), source
