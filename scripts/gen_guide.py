"""Render the Telegram setup guide (static/guide.json) to docs/telegram-setup.md.

The JSON is the guide's single source (bot onboarding spec D5): the page renders it
as the guide view and this script renders it as Markdown. `make guide` runs it;
tests/test_guide_doc.py fails while the committed Markdown differs from its output.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "diffbell" / "web" / "static" / "guide.json"
TARGET = ROOT / "docs" / "telegram-setup.md"

HEADER = (
    "<!-- Generated from src/diffbell/web/static/guide.json by scripts/gen_guide.py:"
    " edit the JSON, then run `make guide`. -->"
)

# The guide is plain text: anything Markdown would read as markup is escaped.
_SPECIAL = re.compile(r"([\\`*_\[\]<>])")


def escape(text: str) -> str:
    return _SPECIAL.sub(r"\\\1", text)


def _paragraphs(block: dict[str, Any]) -> list[str]:
    return [escape(str(p)) for p in block.get("paragraphs", [])]


def render_markdown(data: dict[str, Any]) -> str:
    """The guide as Markdown: the title, the intro, then each section with its
    paragraphs, links and troubleshooting items. Ends with one newline."""
    parts = [HEADER, f"# {escape(str(data['title']))}"]
    parts += [escape(str(p)) for p in data.get("intro", [])]
    for section in data["sections"]:
        parts.append(f"## {escape(str(section['title']))}")
        parts += _paragraphs(section)
        links = section.get("links", [])
        if links:
            parts.append(
                "\n".join(f"- [{escape(str(link['text']))}]({link['url']})" for link in links)
            )
        for item in section.get("items", []):
            parts.append(f"### {escape(str(item['title']))}")
            parts += _paragraphs(item)
    return "\n\n".join(parts) + "\n"


def main() -> int:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    TARGET.write_text(render_markdown(data), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
