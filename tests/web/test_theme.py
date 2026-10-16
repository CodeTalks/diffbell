"""The page's theme choice: a diffbell_theme cookie the server turns into data-theme on
<html>, so the first paint is right (theme switch spec §2)."""

from __future__ import annotations

import httpx
import pytest

from diffbell.web import auth

PLAIN = '<html lang="en">'


async def page(client: httpx.AsyncClient, theme: str | None) -> httpx.Response:
    # An explicit Cookie header replaces the jar's, so the session goes in it too.
    cookies = f"{auth.SESSION_COOKIE}={client.cookies[auth.SESSION_COOKIE]}"
    if theme is not None:
        cookies += f"; {auth.THEME_COOKIE}={theme}"
    return await client.get("/", headers={"Cookie": cookies})


def test_the_cookie_is_named_like_the_session() -> None:
    assert auth.THEME_COOKIE == "diffbell_theme"
    assert auth.FORCED_THEMES == ("light", "dark")


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_a_forced_theme_is_on_the_html_element(client: httpx.AsyncClient, theme: str) -> None:
    response = await page(client, theme)
    assert response.status_code == 200
    assert f'<html lang="en" data-theme="{theme}">' in response.text
    assert response.text.count("data-theme") == 1


@pytest.mark.parametrize("theme", [None, "", "system", "Light", "bogus", "dark%22%3E%3Cb"])
async def test_anything_else_follows_the_system(
    client: httpx.AsyncClient, theme: str | None
) -> None:
    response = await page(client, theme)
    assert response.status_code == 200
    assert PLAIN in response.text
    assert "data-theme" not in response.text


async def test_the_page_is_never_cached(client: httpx.AsyncClient) -> None:
    response = await page(client, "dark")
    assert response.headers["cache-control"] == "no-store"


async def test_without_a_session_it_is_still_the_login_hint(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/", headers={"Cookie": f"{auth.THEME_COOKIE}=dark"})
    assert response.status_code == 401
    assert "data-theme" not in response.text
