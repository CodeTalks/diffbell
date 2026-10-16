# Diffbell brand

**Diffbell** — someone's watching your key files.

## Name

- **Diffbell** is the product's name wherever text talks *about* it: Telegram
  messages, the web UI, the README, help text. In Python it is `diffbell.BRAND`.
- **`diffbell`** is the name wherever something is typed or read by a machine: the
  command, the package, the launchd label `io.688.diffbell`, the Keychain
  service, `~/.config/diffbell`, the state and log dirs.

## Marks

| File | Use |
|---|---|
| `diffbell-mark.svg` | The bell with the removed line cut out, and the added line and clapper in amber. The web header and anything 20 px and up. Fixed colours. |
| `diffbell-tile.svg` | The app tile: teal, white bell centred (the removed line shows teal), amber added line and clapper. App icons. |
| `diffbell-avatar-512.png` | The tile as a full-bleed square, for the Telegram bot picture (Telegram crops it to a circle). |
| `diffbell-icon-1024.png` | The tile with rounded corners, app-icon size. |
| `diffbell-lockup.svg` / `diffbell-lockup-dark.svg` | Mark + wordmark, for light and dark backgrounds. |
| `diffbell-wordmark.svg` | "Diffbell" alone, `currentColor`. |
| `static/favicon.svg` | The small mark: the bell with one amber band, for 16 px. |

The web UI uses the same shapes from `src/diffbell/web/static/icons.svg`
(`#diffbell`, `#diffbell-wordmark`), plus `favicon.svg` and `apple-touch-icon.png`.
`tests/test_brand_assets.py` checks every copy carries both paths of the one bell
geometry, and that each generated copy matches `tools/marks.py`.

The mark keeps its own two colours in every theme (amber on the dark theme's
`#2fbf9b` is 1.08:1, so the accent would vanish); only the wordmark follows the page.

## Colours

| | Light | Dark |
|---|---|---|
| Diffbell teal | `#0e7c66` | `#2fbf9b` |
| UI accent (text and fills) | `#0b7561` | `#2fbf9b` |
| Mark body (fixed) | `#0e7c66` | `#0e7c66` |
| Accent amber (every mark) | `#f59e0b` | `#f59e0b` |
| Ink (the word) | `#1f2328` | `#e6edf3` |

## Type

The wordmark is **Figtree ExtraBold** (weight 800, tracking −0.025 em), converted to
outlines by `tools/wordmark.py`. The page loads no font. Figtree is under the SIL Open
Font License (`Figtree-OFL.txt`), kept here for credit.

## Usage

- Clear space: ¼ of the mark's height on every side.
- Minimum size: the mark 20 px (below that, the small mark: the favicon), the tile 32 px.
- Don't recolour the mark: deep teal and amber, or white and amber on the tile.
- Don't move the bell off-centre on the tile.
- Don't put the mark on a busy background.

## Rebuilding

- `make brand` writes every SVG copy of the mark from `tools/marks.py`, then the
  PNGs (needs `brew install librsvg`; `rsvg-convert` must be on PATH, Homebrew's is
  in `/opt/homebrew/bin`). Change the mark only in `tools/marks.py`.
- Wordmark and lockups: see the docstring of `tools/wordmark.py` (it takes the mark
  from `tools/marks.py`). Afterwards, copy the
  new wordmark path into `icons.svg`'s `#diffbell-wordmark`; the tests catch a mismatch.
