"""The web preview (a local walk) must list exactly what the daemon's Watchman
expression matches for the same tree: any pruning divergence fails here.

Runs in a directory under the session's shared watch root (no new Watchman root)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pywatchman

from diffbell.config import BUILTIN_REDACT, ResolvedProject
from diffbell.patterns import build_expression
from diffbell.web.preview import scan
from tests.integration.test_expression import query_names

pytestmark = pytest.mark.integration

TREE = [
    "package.json",
    "sub/package.json",
    "sub/Makefile",
    "sub/real.txt",
    "node_modules/package.json",
    "a/node_modules/package.json",
    ".worktrees/feat/package.json",
    ".venv/package.json",
    "dist/package.json",
    "examples/package.json",
    "examples/deep/package.json",
    "nested/examples/package.json",
    "a.lock/package.json",
    "x.lock",
    ".env.local",
    "deep/.env",
    ".github/workflows/ci.yml",
    "x/.github/workflows/ci.yml",
    ".claude/settings.local.json",
    "README.md",
]
WATCHED = (
    "package.json",
    "Makefile",
    "*.lock",
    ".env*",
    ".github/workflows/**",
    ".claude/settings*.json",
)
EXCLUDED = ("*.lock", "examples/**")


def test_preview_lists_exactly_what_watchman_matches(shared_dir: Path) -> None:
    for rel in TREE:
        target = shared_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    (shared_dir / "Makefile").symlink_to(shared_dir / "sub")  # symlink to a directory
    (shared_dir / ".env").symlink_to(shared_dir / "sub" / "real.txt")  # symlink to a file
    project = ResolvedProject("p", shared_dir, WATCHED, EXCLUDED)

    local = {f.path for f in scan(project, BUILTIN_REDACT).files}

    client = pywatchman.client(timeout=10)
    try:
        res = client.query("watch-project", str(shared_dir))
        remote = query_names(
            client, res["watch"], res.get("relative_path", ""), build_expression(project)
        )
    finally:
        client.close()
    assert local == remote
    # Sanity: the interesting cases are really in the matched set.
    assert {
        "Makefile",
        ".env",
        "a.lock/package.json",
        "nested/examples/package.json",
        ".env.local",
        ".github/workflows/ci.yml",
        ".claude/settings.local.json",
    } <= local
    assert "examples/package.json" not in local and "x.lock" not in local
