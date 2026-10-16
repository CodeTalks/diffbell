"""Write every copy of Diffbell's mark from one geometry (bell-mark spec).

Stdlib only. Run from the repository root (`make brand` runs it first):

    python3 -I docs/brand/tools/marks.py

Writes docs/brand/diffbell-mark.svg, docs/brand/diffbell-tile.svg,
src/diffbell/web/static/favicon.svg and the `#diffbell` symbol in
src/diffbell/web/static/icons.svg. The lockups are written by wordmark.py, which
loads these constants from this file. The mark keeps its own two colours in every
theme: amber on the dark theme's bright teal would be 1.08:1.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BELL = (
    "M16 3.5a1.75 1.75 0 0 1 1.75 1.75v.4C21.9 6.5 24.5 10 24.5 14.4v4.8l2.6 3.5"
    "c.6.8 0 1.8-1 1.8H5.9c-1 0-1.6-1-1-1.8l2.6-3.5v-4.8C7.5 10 10.1 6.5 14.25 5.65"
    "v-.4A1.75 1.75 0 0 1 16 3.5z"
)
REMOVED = "M11.4 11.4h9.2a1.4 1.4 0 0 1 0 2.8h-9.2a1.4 1.4 0 0 1 0-2.8z"  # a hole
ADDED = "M11.4 16.2h9.2a1.4 1.4 0 0 1 0 2.8h-9.2a1.4 1.4 0 0 1 0-2.8z"
CLAPPER = "M12.5 26.5h7a3.5 3.5 0 0 1-7 0z"
BAND = "M11.5 14h9a2 2 0 0 1 0 4h-9a2 2 0 0 1 0-4z"  # the 16 px favicon's one line
BODY, ACCENT = BELL + REMOVED, ADDED + CLAPPER
TEAL, AMBER, WHITE = "#0e7c66", "#f59e0b", "#fff"

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">{}</svg>\n'
SYMBOL = re.compile(r'<symbol id="diffbell" viewBox="0 0 32 32">.*?</symbol>')


def mark(body_fill: str = TEAL) -> str:
    """The mark's two paths: the bell with the removed line cut out, then the accent."""
    return (
        f'<path fill="{body_fill}" fill-rule="evenodd" d="{BODY}"/>'
        f'<path fill="{AMBER}" d="{ACCENT}"/>'
    )


def outputs(root: Path) -> dict[Path, str]:
    brand = root / "docs" / "brand"
    static = root / "src" / "diffbell" / "web" / "static"
    sprite = (static / "icons.svg").read_text(encoding="utf-8")
    symbol = f'<symbol id="diffbell" viewBox="0 0 32 32">{mark()}</symbol>'
    sprite, count = SYMBOL.subn(lambda _: symbol, sprite)
    if count != 1:
        raise SystemExit(f"icons.svg: expected one #diffbell symbol, found {count}")
    tile = (
        f'<rect width="32" height="32" rx="8" fill="{TEAL}"/>'
        f'<g transform="translate(4 3.6) scale(.75)">{mark(WHITE)}</g>'
    )
    small = f'<path fill="{TEAL}" d="{BELL}"/><path fill="{AMBER}" d="{BAND + CLAPPER}"/>'
    return {
        brand / "diffbell-mark.svg": SVG.format(mark()),
        brand / "diffbell-tile.svg": SVG.format(tile),
        static / "favicon.svg": SVG.format(small),
        static / "icons.svg": sprite,
    }


def main(root: Path) -> None:
    for file, text in outputs(root).items():
        part = file.with_name(file.name + ".part")
        part.write_text(text, encoding="utf-8")
        os.replace(part, file)  # atomic, like the repo's other generated files


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd())
