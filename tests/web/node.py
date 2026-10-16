"""Run a test script under node against the real page modules (static/js/).

The script runs as an ES module with top-level await. It sees `js(name)` (the
file URL of a module, for `await import(js("feed.js"))`), the fake DOM helpers
from tests/web/fakedom.mjs, and `done(value)`, which hands a JSON value back
and ends the process.
Skipped when node >= 22.7 (ES module detection of .js files) is missing, unless
DIFFBELL_REQUIRE_NODE=1: then those tests fail instead (for CI and the merge
gate, where a silent skip would hide every JS test)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from diffbell.web.app import STATIC_DIR

NODE = shutil.which("node")
JS_DIR = STATIC_DIR / "js"
FAKEDOM = Path(__file__).parent / "fakedom.mjs"


# 22.7 is the first release that loads an ES module from a .js file without a
# package.json or a flag (module syntax detection on by default).
MIN_NODE = (22, 7)


def node_version() -> tuple[int, int]:
    if NODE is None:
        return (0, 0)
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=False).stdout
    try:
        major, minor = out.strip().lstrip("v").split(".")[:2]
        return (int(major), int(minor))
    except ValueError:
        return (0, 0)


REQUIRE_NODE = "DIFFBELL_REQUIRE_NODE"


def skip_js_tests(version: tuple[int, int], environ: Mapping[str, str]) -> bool:
    """Skip the node tests? Never when the environment requires node."""
    return version < MIN_NODE and environ.get(REQUIRE_NODE) != "1"


NODE_VERSION = node_version()
needs_node = pytest.mark.skipif(
    skip_js_tests(NODE_VERSION, os.environ), reason="node >= 22.7 is not installed"
)


def run_js(script: str, *, tz: str = "UTC") -> Any:
    assert NODE is not None and NODE_VERSION >= MIN_NODE, (
        f"node >= 22.7 is required ({REQUIRE_NODE}=1); found {NODE_VERSION} at {NODE}"
    )
    program = "\n".join(
        [
            f'import {{ FakeDocument, fakeFetch, settle }} from "{FAKEDOM.as_uri()}";',
            f"const JS = {json.dumps(JS_DIR.as_uri())};",
            "const js = (name) => `${JS}/${name}`;",
            # Exits at once: the page's poll and debounce timers would keep node alive.
            "const done = (value) => process.stdout.write(JSON.stringify(value), () => process.exit(0));",
            script,
        ]
    )
    env = {**os.environ, "TZ": tz, "NODE_NO_WARNINGS": "1"}
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)
