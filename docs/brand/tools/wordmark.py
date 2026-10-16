"""Generate Diffbell's wordmark and lockups from Figtree's outlines.

"Diffbell" is shaped by HarfBuzz (for kerning) at Figtree ExtraBold (wght 800) with
-0.025 em tracking, and its outlines are written as one SVG path, so no font is
loaded at runtime. Run by hand (neither tool is a project dependency):

    uv run --no-project --with fonttools --with uharfbuzz \
        python -I docs/brand/tools/wordmark.py <Figtree[wght].ttf> docs/brand

The font comes from https://github.com/google/fonts/tree/main/ofl/figtree (OFL).
Writes diffbell-wordmark.svg, diffbell-lockup.svg and diffbell-lockup-dark.svg.
"""

import math
import runpy
import sys
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

TEXT, WEIGHT, TRACK = "Diffbell", 800, -0.025
# The mark's geometry and colours live only in marks.py (stdlib, no package): load it
# by path, since `python -I` keeps the script's folder off sys.path.
MARKS = runpy.run_path(str(Path(__file__).with_name("marks.py")))
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="{}">{}</svg>\n'


def number(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def main(font_path: str, out_dir: str) -> None:
    out = Path(out_dir)
    blob = hb.Blob.from_file_path(font_path)
    hbfont = hb.Font(hb.Face(blob))
    hbfont.set_variations({"wght": WEIGHT})
    buf = hb.Buffer()
    buf.add_str(TEXT)
    buf.guess_segment_properties()
    hb.shape(hbfont, buf, {"kern": True, "liga": False})

    tt = instantiateVariableFont(TTFont(font_path), {"wght": WEIGHT})
    upem = tt["head"].unitsPerEm
    glyphs = tt.getGlyphSet()
    order = tt.getGlyphOrder()
    asc, desc = tt["hhea"].ascent, tt["hhea"].descent
    cap = tt["OS/2"].sCapHeight

    pen = SVGPathPen(glyphs, ntos=number)
    ink = BoundsPen(glyphs)
    x = 0.0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
        # Font units grow upwards and SVG's downwards: flip, baseline at y = asc.
        draw = TransformPen(pen, (1, 0, 0, -1, x + pos.x_offset, asc - pos.y_offset))
        glyphs[order[info.codepoint]].draw(draw)
        glyphs[order[info.codepoint]].draw(
            TransformPen(ink, (1, 0, 0, -1, x + pos.x_offset, asc - pos.y_offset))
        )
        x += pos.x_advance + TRACK * upem
    width = x - TRACK * upem
    d = pen.getCommands()

    cap_top, bottom = asc - cap, asc - desc  # cap height to descender
    # Anything above the capitals (the dot on "i", the hook of "f") stays inside.
    top = min(cap_top, math.floor(ink.bounds[1]))
    word_box = f"0 {top} {round(width)} {round(bottom - top)}"
    (out / "diffbell-wordmark.svg").write_text(
        SVG.format(word_box, f'<path fill="currentColor" d="{d}"/>')
    )

    mark_h = 1.4 * cap  # the mark is 1.4 x the cap height ...
    k = mark_h / 32
    mark_top = (cap_top + asc) / 2 - mark_h / 2  # ... centred on the capitals
    # The layout keeps the shield's x 4..28 of 32; the bell spans 4.7..27.3, so the
    # word stays where it was. Gap = 1/4 mark.
    word_x = 24 * k + mark_h / 4
    y0, y1 = min(mark_top, top), max(mark_top + mark_h, bottom)
    box = f"0 {y0:.0f} {word_x + width:.0f} {y1 - y0:.0f}"
    mark = (
        f'<g transform="translate({-4 * k:.2f} {mark_top:.2f}) scale({k:.4f})">'
        f"{MARKS['mark']()}</g>"
    )
    # The mark keeps its fixed colours on both; only the word's colour differs.
    for suffix, word_colour in (("", "#1f2328"), ("-dark", "#e6edf3")):
        body = mark + f'<path fill="{word_colour}" transform="translate({word_x:.2f} 0)" d="{d}"/>'
        (out / f"diffbell-lockup{suffix}.svg").write_text(SVG.format(box, body))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
