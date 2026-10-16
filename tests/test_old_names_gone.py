"""No old name outside the history allowlist (rename spec §2): dev-watchman,
dev_watchman, DEV_WATCHMAN, Keyward and the dw- harness names are gone. A line that
names the old install on purpose carries an `old-name` marker in a comment."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]

OLD = re.compile(
    r"dev-watchman|dev_watchman|DEV_WATCHMAN|[Kk]eyward|dwwm-|(?<![\w-])dwt-|dw-probe"
    r"|/dw-test/|\bdw_(session|theme)|x-dw-|\"_dw\"|(?<![\w])dw:|scripts/live/dw\b"
)
# One ADR more than scripts/rename.py's HISTORY on purpose: ADR 0020 doesn't exist
# when the script runs, and is history (naming the old install) once written.
HISTORY = re.compile(r"^(docs/adr/00(0\d|1\d|20)-|docs/superpowers/|scripts/rename\.py$)")
# The move must name the old install to find it (rename spec §2).
WHOLE_FILE = {
    "src/diffbell/rename_install.py",
    "tests/test_rename_install.py",
    "tests/test_old_names_gone.py",
    "scripts/live/rename-check.sh",
}
MARKER = "old-name"


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return out.splitlines()


def test_no_old_name_outside_the_allowlist() -> None:
    hits = []
    for path in tracked():
        if HISTORY.match(path) or path in WHOLE_FILE or path == "uv.lock":
            continue
        if OLD.search(path) and MARKER not in path:
            hits.append(f"{path}: (path)")
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if OLD.search(line) and MARKER not in line:
                hits.append(f"{path}:{number}: {line.strip()}")
    assert hits == []


def test_the_pattern_catches_each_old_name() -> None:
    for old in (
        "dev-watchman",
        "dev_watchman",
        "DEV_WATCHMAN_X",
        "Keyward",
        "keyward",
        "dwwm-x",
        "dwt-x",
        "dw-probe",
        "/dw-test/p",
        "dw_session",
        "x-dw-token",
        '"_dw"',
        '"dw:p#1"',
        "scripts/live/dw run",
    ):
        assert OLD.search(old), old
    for fine in ("diffbell", "dfwm-x", "dft-x", "bandwidth: 3", "endw:"):
        assert not OLD.search(fine), fine
