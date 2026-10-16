"""The compiled expression must behave in real Watchman the way §4 says, including
when the project sits under an already-watched ancestor (relative_root).

Every project here is a directory under the session's shared watch root, so all
queries go through relative_root, which is exactly how the watcher queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pywatchman

from diffbell.config import BUILTIN_REDACT, ResolvedProject
from diffbell.patterns import build_expression, glob_term, is_redacted, matches

pytestmark = pytest.mark.integration

FILES = [
    "package.json",
    "sub/package.json",
    "node_modules/package.json",
    "a/node_modules/package.json",
    "a/b/node_modules/package.json",
    ".worktrees/feat/package.json",
    "x/dist/package.json",
    ".env.local",
    "deep/.env",
    ".github/workflows/ci.yml",
    "x/.github/workflows/ci.yml",
    "README.md",
]


def make_files(root: Path) -> None:
    for rel in FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")


def query_names(client: Any, root: str, rel: str, expr: Any) -> set[str]:
    opts: dict[str, Any] = {"expression": expr, "fields": ["name"]}
    if rel:
        opts["relative_root"] = rel
    return set(client.query("query", root, opts)["files"])


def test_expression_matches_and_excludes_at_any_depth(shared_dir: Path) -> None:
    make_files(shared_dir)
    proj = ResolvedProject("p", shared_dir, ("package.json", ".env*", ".github/workflows/**"), ())
    client = pywatchman.client(timeout=5)
    try:
        res = client.query("watch-project", str(shared_dir))
        names = query_names(
            client, res["watch"], res.get("relative_path", ""), build_expression(proj)
        )
    finally:
        client.close()
    assert names == {
        "package.json",
        "sub/package.json",
        ".env.local",
        "deep/.env",
        ".github/workflows/ci.yml",
    }


def test_nested_project_under_watched_ancestor(shared_root: Path, shared_dir: Path) -> None:
    child = shared_dir / "par" / "child"
    child.mkdir(parents=True)
    make_files(child)
    client = pywatchman.client(timeout=5)
    try:
        res = client.query("watch-project", str(child))  # the shared root is the ancestor
        assert res["watch"] == str(shared_root)
        assert res["relative_path"] == f"{shared_dir.name}/par/child"
        proj = ResolvedProject("c", child, ("sub/package.json",), ())
        names = query_names(client, res["watch"], res["relative_path"], build_expression(proj))
    finally:
        client.close()
    assert names == {"sub/package.json"}  # project-relative, anchored glob still works


@pytest.mark.parametrize(
    "pattern", ["package.json", ".env*", "*.json", ".github/workflows/**", "sub/*.json"]
)
def test_local_matcher_agrees_with_watchman(shared_dir: Path, pattern: str) -> None:
    make_files(shared_dir)
    client = pywatchman.client(timeout=5)
    try:
        res = client.query("watch-project", str(shared_dir))
        from_watchman = query_names(
            client,
            res["watch"],
            res.get("relative_path", ""),
            ["allof", ["type", "f"], glob_term(pattern)],
        )
    finally:
        client.close()
    local = {f for f in FILES if matches(f, pattern)}
    assert from_watchman == local


def test_everything_watchman_matches_for_secret_globs_is_redacted_locally(
    shared_dir: Path,
) -> None:
    """Watchman's match is case-insensitive on APFS; redaction must cover all of it."""
    for rel in (".Env.Local", "Server.PEM"):
        (shared_dir / rel).write_text("secret\n")
    client = pywatchman.client(timeout=5)
    try:
        res = client.query("watch-project", str(shared_dir))
        names = query_names(
            client,
            res["watch"],
            res.get("relative_path", ""),
            ["allof", ["type", "f"], ["anyof", glob_term(".env*"), glob_term("*.pem")]],
        )
    finally:
        client.close()
    assert names  # sanity: watchman returned something
    assert all(is_redacted(name, BUILTIN_REDACT) for name in names), names
