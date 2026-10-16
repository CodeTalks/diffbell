"""Diffbell's marks come from one geometry (`docs/brand/tools/marks.py`), every copy
carries both paths in the mark's fixed colours, the lockups share one wordmark, and
the PNGs have the sizes the spec names (bell-mark spec)."""

from __future__ import annotations

import importlib.util
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
BRAND_DIR = ROOT / "docs" / "brand"
TOOLS = BRAND_DIR / "tools"
STATIC = ROOT / "src" / "diffbell" / "web" / "static"
SVG = "{http://www.w3.org/2000/svg}"

# The spec's geometry, pinned here so an edit to marks.py can't move it silently.
BELL = (
    "M16 3.5a1.75 1.75 0 0 1 1.75 1.75v.4C21.9 6.5 24.5 10 24.5 14.4v4.8l2.6 3.5"
    "c.6.8 0 1.8-1 1.8H5.9c-1 0-1.6-1-1-1.8l2.6-3.5v-4.8C7.5 10 10.1 6.5 14.25 5.65"
    "v-.4A1.75 1.75 0 0 1 16 3.5z"
)
REMOVED = "M11.4 11.4h9.2a1.4 1.4 0 0 1 0 2.8h-9.2a1.4 1.4 0 0 1 0-2.8z"
ADDED = "M11.4 16.2h9.2a1.4 1.4 0 0 1 0 2.8h-9.2a1.4 1.4 0 0 1 0-2.8z"
CLAPPER = "M12.5 26.5h7a3.5 3.5 0 0 1-7 0z"
BAND = "M11.5 14h9a2 2 0 0 1 0 4h-9a2 2 0 0 1 0-4z"
BODY, ACCENT = BELL + REMOVED, ADDED + CLAPPER
TEAL, AMBER, WHITE = "#0e7c66", "#f59e0b", "#fff"
SHIELD = "M16 2 4 6.2v8.6C4 22 9 27.6 16 30c7-2.4 12-8 12-15.2V6.2z"  # the old mark
LOCKUPS = ["diffbell-lockup.svg", "diffbell-lockup-dark.svg"]
TEXT_SUFFIXES = {".svg", ".py", ".md", ".html", ".css", ".js", ".json", ".txt"}

Painted = tuple[str | None, str | None, str | None]  # (d, fill, fill-rule)


def load_marks() -> ModuleType:
    spec = importlib.util.spec_from_file_location("diffbell_brand_marks", TOOLS / "marks.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def path_elements(file: Path) -> list[ET.Element]:
    return list(ET.parse(file).getroot().iter(f"{SVG}path"))


def path_data(file: Path) -> list[str | None]:
    return [p.get("d") for p in path_elements(file)]


def painted(elements: list[ET.Element]) -> list[Painted]:
    return [(p.get("d"), p.get("fill"), p.get("fill-rule")) for p in elements]


def wordmark() -> str:
    [d] = path_data(BRAND_DIR / "diffbell-wordmark.svg")
    assert d
    return d


def sprite_symbol(symbol: str) -> list[ET.Element]:
    root = ET.parse(STATIC / "icons.svg").getroot()
    [element] = [s for s in root.iter(f"{SVG}symbol") if s.get("id") == symbol]
    return list(element.iter(f"{SVG}path"))


def test_marks_py_holds_the_spec_geometry() -> None:
    marks = load_marks()
    assert (marks.BELL, marks.REMOVED, marks.ADDED, marks.CLAPPER, marks.BAND) == (
        BELL,
        REMOVED,
        ADDED,
        CLAPPER,
        BAND,
    )
    assert (marks.TEAL, marks.AMBER, marks.WHITE) == (TEAL, AMBER, WHITE)


def test_every_generated_copy_is_up_to_date() -> None:
    for file, text in load_marks().outputs(ROOT).items():
        assert file.read_text(encoding="utf-8") == text, f"{file.relative_to(ROOT)}: run make brand"


@pytest.mark.parametrize(
    ("name", "body"),
    [("diffbell-mark.svg", TEAL), ("diffbell-tile.svg", WHITE)] + [(n, TEAL) for n in LOCKUPS],
)
def test_every_mark_carries_both_paths_in_its_fixed_colours(name: str, body: str) -> None:
    marks = [p for p in painted(path_elements(BRAND_DIR / name)) if p[0] in (BODY, ACCENT)]
    assert marks == [(BODY, body, "evenodd"), (ACCENT, AMBER, None)]


def test_the_sprite_mark_has_fixed_fills() -> None:
    # The header's CSS no longer colours the mark: a currentColor body would turn
    # grey in light mode and near-white in dark (--text).
    assert painted(sprite_symbol("diffbell")) == [(BODY, TEAL, "evenodd"), (ACCENT, AMBER, None)]
    assert [p.get("d") for p in sprite_symbol("diffbell-wordmark")] == [wordmark()]


def test_the_favicon_is_the_small_mark() -> None:
    assert painted(path_elements(STATIC / "favicon.svg")) == [
        (BELL, TEAL, None),
        (BAND + CLAPPER, AMBER, None),
    ]
    assert "<style" not in (STATIC / "favicon.svg").read_text(encoding="utf-8")


def test_no_brand_file_keeps_the_shield() -> None:
    # Scoped: the ADRs and the rename specs and plans keep the shield as history.
    files = [*BRAND_DIR.rglob("*"), *STATIC.rglob("*")]
    stale = [
        str(f.relative_to(ROOT))
        for f in files
        if f.is_file() and f.suffix in TEXT_SUFFIXES and SHIELD in f.read_text(encoding="utf-8")
    ]
    assert stale == []


def test_wordmark_py_takes_the_mark_from_marks_py() -> None:
    source = (TOOLS / "wordmark.py").read_text(encoding="utf-8")
    assert 'with_name("marks.py")' in source
    assert BELL[:20] not in source and SHIELD not in source


@pytest.mark.parametrize("name", LOCKUPS)
def test_the_lockups_use_the_wordmark(name: str) -> None:
    assert wordmark() in path_data(BRAND_DIR / name)


def test_the_wordmark_follows_the_text_colour() -> None:
    [element] = path_elements(BRAND_DIR / "diffbell-wordmark.svg")
    assert element.get("fill") == "currentColor"


def png_size(file: Path) -> tuple[int, int]:
    head = file.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", file
    width, height = struct.unpack(">II", head[16:24])
    return width, height


@pytest.mark.parametrize(
    ("file", "size"),
    [
        (BRAND_DIR / "diffbell-avatar-512.png", 512),
        (BRAND_DIR / "diffbell-icon-1024.png", 1024),
        (STATIC / "apple-touch-icon.png", 180),
    ],
)
def test_the_pngs_have_their_sizes(file: Path, size: int) -> None:
    assert png_size(file) == (size, size)


def test_the_font_license_is_kept() -> None:
    text = (BRAND_DIR / "Figtree-OFL.txt").read_text(encoding="utf-8")
    assert "Figtree" in text and "SIL Open Font License" in text


def corner_alpha(file: Path) -> int:
    """Alpha of the top-left pixel of an 8-bit PNG, read with the stdlib.

    rsvg-convert writes colour type 2 (RGB, no alpha channel: opaque) when `-b` gives a
    background and type 6 (RGBA) otherwise."""
    data = file.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", file
    pos, idat, colour = 8, b"", 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            assert body[8] == 8 and body[9] in (2, 6), "expected 8-bit RGB or RGBA"
            colour = body[9]
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    # Byte 0 of row 0 is the filter type, bytes 1..4 the first pixel's RGBA. In the
    # first row's first pixel every PNG filter leaves the raw value unchanged (no left
    # or upper neighbour), so the decoded alpha is the byte at index 4.
    return zlib.decompress(idat)[4] if colour == 6 else 255


@pytest.mark.parametrize(
    ("file", "alpha"),
    [
        (BRAND_DIR / "diffbell-avatar-512.png", 255),
        (STATIC / "apple-touch-icon.png", 255),
        (BRAND_DIR / "diffbell-icon-1024.png", 0),
    ],
)
def test_the_corners_are_full_bleed_or_transparent(file: Path, alpha: int) -> None:
    assert corner_alpha(file) == alpha
