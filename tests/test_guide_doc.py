"""The Telegram setup guide has one source, static/guide.json (bot onboarding spec
D5): docs/telegram-setup.md must equal scripts/gen_guide.py's rendering of it. A
pure Python test, so it always runs in `make check` (node or not)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
GUIDE = ROOT / "src" / "diffbell" / "web" / "static" / "guide.json"
DOC = ROOT / "docs" / "telegram-setup.md"


def gen_guide() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_guide", ROOT / "scripts" / "gen_guide.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guide() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(GUIDE.read_text(encoding="utf-8"))
    return data


def test_the_markdown_guide_is_generated_from_the_json() -> None:
    expected = gen_guide().render_markdown(guide())
    assert DOC.read_text(encoding="utf-8") == expected, "run `make guide` and commit the result"


def test_the_generator_paths_are_the_repo_files() -> None:
    module = gen_guide()
    assert module.SOURCE == GUIDE
    assert module.TARGET == DOC


def test_every_step_the_page_links_to_exists() -> None:
    """The wizard's "How? ▸" links and ?step= name these ids."""
    ids = [s["id"] for s in guide()["sections"]]
    assert ids == ["create", "token", "start", "test", "more-macs", "privacy", "troubleshooting"]
    assert all(re.fullmatch(r"[a-z-]{1,32}", i) for i in ids)
    index = (GUIDE.parent / "index.html").read_text(encoding="utf-8")
    for step in re.findall(r'data-step="([a-z-]+)"', index):
        assert step in ids, step


def test_the_guide_covers_what_the_spec_lists() -> None:
    text = json.dumps(guide(), ensure_ascii=False)
    for needle in (
        "/newbot",
        'end in \\"bot\\"',
        "5 to 32 characters",
        "password",
        "/revoke",
        "after you clicked Check",
        "only ever sends to the one chat you picked",
        "strangers can find a bot",
        "That doesn't look like a bot token",
        "Telegram rejected this token",
        "webhook",
        "another program reads its updates",
        "Replace bot",
        "An earlier save didn't finish",
        "diffbell setup",
        "Using Diffbell on more than one Mac",
        "the same bot token",
        "send /start to the bot again",
        "Settings → Notifications",
    ):
        assert needle in text, needle


def test_the_guide_is_plain_text_with_telegram_links_only() -> None:
    data = guide()
    texts = [data["title"], *data["intro"]]
    for section in data["sections"]:
        texts += [section["title"], *section["paragraphs"]]
        for item in section.get("items", []):
            texts += [item["title"], *item["paragraphs"]]
        for link in section.get("links", []):
            assert link["url"].startswith("https://t.me/"), link
    for text in texts:
        assert not re.search(r"[<>`*]|\]\(|https?://", text), text


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        ("my_diffbell_bot", "my\\_diffbell\\_bot"),
        ("a <b> `c` *d* [e]", "a \\<b\\> \\`c\\` \\*d\\* \\[e\\]"),
        ("back\\slash", "back\\\\slash"),
    ],
)
def test_markdown_markup_is_escaped(text: str, escaped: str) -> None:
    assert gen_guide().escape(text) == escaped


def test_render_markdown_shape() -> None:
    data = {
        "title": "T",
        "intro": ["Intro."],
        "sections": [
            {
                "id": "a",
                "title": "1. A",
                "paragraphs": ["P1.", "P2."],
                "links": [{"text": "Open", "url": "https://t.me/BotFather"}],
            },
            {
                "id": "b",
                "title": "B",
                "paragraphs": [],
                "items": [{"title": "Q", "paragraphs": ["A."]}],
            },
        ],
    }
    module = gen_guide()
    assert module.render_markdown(data) == (
        f"{module.HEADER}\n\n# T\n\nIntro.\n\n## 1. A\n\nP1.\n\nP2.\n\n"
        "- [Open](https://t.me/BotFather)\n\n## B\n\n### Q\n\nA.\n"
    )
