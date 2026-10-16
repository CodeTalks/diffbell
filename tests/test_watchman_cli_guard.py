"""No test runs a `watchman` CLI command that could reach the user's daemon
(Plan 6 spec §2): the CLI ignores WATCHMAN_SOCK, so only read-only `--no-spawn`
commands may appear in tests/."""

from __future__ import annotations

import ast
import os
from pathlib import Path

from tests import private_watchman as pw

TESTS = Path(__file__).parent
READ_ONLY_FLAGS = {"--no-spawn", "--no-local", "--no-pretty"}


def watchman_argvs() -> list[tuple[str, int, list[str]]]:
    """Every list literal in tests/ of two or more items whose first is "watchman"."""
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if (
                isinstance(node, ast.List)
                and len(node.elts) > 1  # ["watchman"] alone is a problem-key list
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == "watchman"
            ):
                items = [str(e.value) if isinstance(e, ast.Constant) else "?" for e in node.elts]
                found.append((os.path.relpath(path, TESTS), node.lineno, items))
    return found


def test_no_test_runs_a_watchman_cli_command_that_could_reach_the_users_daemon() -> None:
    """The CLI ignores WATCHMAN_SOCK: only read-only `--no-spawn` commands may appear."""
    argvs = watchman_argvs()
    assert pw.USER_WATCH_LIST in [argv for _, _, argv in argvs]  # the scan finds guard 2's
    for file, line, argv in argvs:
        if "--foreground" in argv:  # starting the private daemon (start_argv's shape)
            continue
        assert "--no-spawn" in argv, f"{file}:{line}: {argv}"
        assert set(argv[1:-1]) <= READ_ONLY_FLAGS, f"{file}:{line}: {argv}"
        assert argv[-1] in ("version", "watch-list"), f"{file}:{line}: {argv}"
