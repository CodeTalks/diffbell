"""theme.js under node: the three choices, the header button's cycle, labels and
icons, and the cookie strings (theme switch spec §3; settings redesign spec §5)."""

from __future__ import annotations

from typing import Any

import pytest

from diffbell.web import auth
from tests.web.node import needs_node, run_js

pytestmark = needs_node


def js_value(expression: str) -> Any:
    return run_js(f'const m = await import(js("theme.js"));\ndone({expression});')


def test_the_three_choices() -> None:
    assert js_value("m.THEMES") == ["system", "light", "dark"]


def test_the_cycle() -> None:
    assert js_value('["system", "light", "dark", "bogus", undefined].map(m.nextTheme)') == [
        "light",
        "dark",
        "system",
        "light",
        "light",
    ]


def test_labels_say_the_state_and_the_next_one() -> None:
    assert js_value("m.THEMES.map(m.themeLabel)") == [
        "Theme: System (click for Light)",
        "Theme: Light (click for Dark)",
        "Theme: Dark (click for System)",
    ]


def test_icons_come_from_the_sprite() -> None:
    assert js_value("m.THEMES.map(m.themeIcon)") == ["theme-system", "theme-light", "theme-dark"]


@pytest.mark.parametrize(
    ("theme", "cookie"),
    [
        ("light", "diffbell_theme=light; Path=/; Max-Age=31536000; SameSite=Strict"),
        ("dark", "diffbell_theme=dark; Path=/; Max-Age=31536000; SameSite=Strict"),
        ("system", "diffbell_theme=; Path=/; Max-Age=0; SameSite=Strict"),
    ],
)
def test_the_cookie_strings(theme: str, cookie: str) -> None:
    assert js_value(f'm.themeCookie("{theme}")') == cookie
    assert cookie.startswith(f"{auth.THEME_COOKIE}=")  # the name the server reads


def test_the_current_theme_is_read_from_the_attribute() -> None:
    assert js_value('["light", "dark", undefined, "x"].map(m.currentTheme)') == [
        "light",
        "dark",
        "system",
        "system",
    ]
