"""Diffbell is the product's name in prose; everything typed or read by a machine is
diffbell (rename spec §1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from diffbell import BRAND, __version__
from diffbell.cli import main
from diffbell.liveness import heartbeat_text
from diffbell.web.app import LOGIN_HINT

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "diffbell"
COMMANDS = "web|setup|install|uninstall|run|doctor|status|test|upgrade"


def sources() -> list[Path]:
    files = [p for p in SRC.rglob("*") if p.suffix in {".py", ".js", ".html"}]
    return [
        *files,
        ROOT / "README.md",
        ROOT / "docs" / "brand" / "README.md",
        # The Telegram setup guide: its source and the Markdown generated from it.
        SRC / "web" / "static" / "guide.json",
        ROOT / "docs" / "telegram-setup.md",
    ]


def test_the_brand_is_diffbell() -> None:
    assert BRAND == "Diffbell"


def test_version_names_the_product(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"Diffbell {__version__}"


def test_the_heartbeat_says_diffbell() -> None:
    assert heartbeat_text(3, 0, []).startswith("✅ Diffbell · watching 3 projects")


def test_the_login_page_says_diffbell_and_the_command_diffbell() -> None:
    assert "<title>Diffbell</title>" in LOGIN_HINT
    assert "Run <code>diffbell web</code> to log in." in LOGIN_HINT


def test_no_telegram_text_still_says_diffbell() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(✅|⚠️|🔑) diffbell", text), path


def test_commands_are_lowercase() -> None:
    """A command is written `diffbell web`, never with the prose capital `Diffbell web`."""
    # Prose "diffbell web" can't be told apart from the command: the brand and the
    # command differ only in case, so only the capitalised form is checked.
    for path in sources():
        text = path.read_text(encoding="utf-8")
        assert not re.search(rf"\bDiffbell ({COMMANDS})\b", text), path


def test_the_guide_says_diffbell_and_names_the_commands() -> None:
    for path in (SRC / "web" / "static" / "guide.json", ROOT / "docs" / "telegram-setup.md"):
        text = path.read_text(encoding="utf-8")
        assert "Diffbell" in text, path
        assert "diffbell setup" in text, path
        assert not re.search(r"diffbell (sends|keeps|reads|stores)", text), path
