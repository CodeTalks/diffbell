.PHONY: check lint type test fmt brand guide

check: lint type test

lint:
	uv run ruff check .
	uv run ruff format --check .

type:
	uv run mypy src tests scripts

test:
	uv run pytest -q

fmt:
	uv run ruff format .
	uv run ruff check --fix .

BRAND_TEAL := \#0e7c66

# Rebuild the brand assets (manual; needs `brew install librsvg`). marks.py writes every
# SVG copy of the mark first; the lockups need the font (docs/brand/tools/wordmark.py).
# The avatar and the touch icon fill the tile's corners with teal: Telegram crops to a
# circle and iOS applies its own rounded mask, so they must be full-bleed squares.
brand:
	@command -v rsvg-convert >/dev/null || { echo "make brand needs rsvg-convert: brew install librsvg" >&2; exit 1; }
	python3 -I docs/brand/tools/marks.py
	rsvg-convert -w 512 -h 512 -b '$(BRAND_TEAL)' docs/brand/diffbell-tile.svg -o docs/brand/diffbell-avatar-512.png
	rsvg-convert -w 1024 -h 1024 docs/brand/diffbell-tile.svg -o docs/brand/diffbell-icon-1024.png
	rsvg-convert -w 180 -h 180 -b '$(BRAND_TEAL)' docs/brand/diffbell-tile.svg -o src/diffbell/web/static/apple-touch-icon.png

# Regenerate docs/telegram-setup.md from the guide's single source,
# src/diffbell/web/static/guide.json (tests/test_guide_doc.py fails while they differ).
guide:
	uv run python scripts/gen_guide.py
