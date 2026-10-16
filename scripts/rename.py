"""One-shot rename of dev-watchman / Keyward to diffbell / Diffbell (rename spec §2).

Run once from a clean tree at the repository root:

    python3 scripts/rename.py

It `git mv`s every tracked path whose name contains an old name, then applies one
ordered replacement table to every tracked text file outside the history allowlist.
Kept in the repository as the record of commit 1; never run again.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Records: never rewritten (rename spec §2). Matched against the repo-relative path.
HISTORY = re.compile(r"^(docs/adr/00(0\d|1\d)-|docs/superpowers/|scripts/rename\.py$)")

# Path renames, applied to each path's string (order matters: longest first).
PATH_RENAMES: list[tuple[str, str]] = [
    ("src/dev_watchman", "src/diffbell"),
    ("keyward", "diffbell"),
    ("scripts/live/dw", "scripts/live/dbell"),
]

# Text replacements, in order. Plain strings unless marked as a regex.
TEXT: list[tuple[str, str, bool]] = [
    ("io.688.dev-watchman", "io.688.diffbell", False),
    ("DEV_WATCHMAN_", "DIFFBELL_", False),
    ("dev_watchman", "diffbell", False),
    ("dev-watchman", "diffbell", False),
    ("Keyward", "Diffbell", False),
    ("keyward", "diffbell", False),
    ("dw_session", "diffbell_session", False),
    ("dw_theme", "diffbell_theme", False),
    ("x-dw-token", "x-diffbell-token", False),
    ("X-DW-Token", "X-Diffbell-Token", False),
    ("dw-config", "diffbell-config", False),
    ('"_dw"', '"_diffbell"', False),
    ("dwwm-", "dfwm-", False),
    ("dw-probe", "df-probe", False),
    ("/dw-test/", "/df-test/", False),
    (r"(?<![\w-])dwt-", "dft-", True),
    (r"(?<![\w])dw:", "diffbell:", True),
    (r"scripts/live/dw\b", "scripts/live/dbell", True),
    (r'"dw"', '"dbell"', True),
    (r"`dw`", "`dbell`", True),
    (r"(?<![\w/.-])dw (?=status|doctor|test)", "dbell ", True),
]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def main() -> int:
    if git("status", "--porcelain").strip():
        print("refusing: the tree has uncommitted changes", file=sys.stderr)
        return 1
    moved = 0
    for path in git("ls-files").splitlines():
        if HISTORY.match(path):
            continue
        new = path
        for old, repl in PATH_RENAMES:
            new = new.replace(old, repl)
        if new != path:
            (ROOT / new).parent.mkdir(parents=True, exist_ok=True)
            git("mv", path, new)
            moved += 1
    counts = {old: 0 for old, _, _ in TEXT}
    for path in git("ls-files").splitlines():
        if HISTORY.match(path) or path == "uv.lock":
            continue
        file = ROOT / path
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue  # binary (png, ttf)
        out = text
        for old, repl, is_regex in TEXT:
            if is_regex:
                out, n = re.subn(old, repl, out)
            else:
                n = out.count(old)
                out = out.replace(old, repl)
            counts[old] += n
        if out != text:
            file.write_text(out, encoding="utf-8")
    print(f"moved {moved} paths")
    for old, n in counts.items():
        print(f"{n:6d}  {old}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
