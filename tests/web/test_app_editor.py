"""The editor setting on the API (Plan 7 spec §2.4): read in /api/settings and
/api/dashboard, written through config_edit; the port stays out of reach."""

from __future__ import annotations

import httpx
import pytest

from diffbell.config import load_config
from tests.web.support import Env
from tests.web.test_app_notify import settings_body


async def put_editor(client: httpx.AsyncClient, web: object) -> httpx.Response:
    version = (await client.get("/api/settings")).json()["config_version"]
    return await client.put("/api/settings", json=settings_body(version, web=web))


@pytest.mark.parametrize("value", ["vscode", "none", "cursor"])
async def test_the_editor_is_saved_and_read_back(
    env: Env, client: httpx.AsyncClient, value: str
) -> None:
    assert (await put_editor(client, {"editor": value})).status_code == 200
    assert load_config(env.config).web.editor == value
    assert (await client.get("/api/settings")).json()["web"] == {"port": 8765, "editor": value}
    assert (await client.get("/api/dashboard")).json()["web"]["editor"] == value


async def test_without_web_the_editor_is_left_as_it_is(env: Env, client: httpx.AsyncClient) -> None:
    await put_editor(client, {"editor": "vscode"})
    version = (await client.get("/api/settings")).json()["config_version"]
    assert (await client.put("/api/settings", json=settings_body(version))).status_code == 200
    assert load_config(env.config).web.editor == "vscode"


@pytest.mark.parametrize(
    "web",
    [
        {"editor": "vim"},
        {"editor": None},
        {"editor": "vscode", "port": 9999},  # the port can't be changed here
        {"port": 9999},
        {},
    ],
)
async def test_a_bad_web_block_is_refused(
    env: Env, client: httpx.AsyncClient, web: dict[str, object]
) -> None:
    before = env.config.read_bytes()
    response = await put_editor(client, web)
    assert response.status_code == 400
    assert env.config.read_bytes() == before


async def test_an_empty_editor_in_the_file_is_a_422_naming_the_key(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.config.write_text(env.config.read_text() + "web:\n  editor:\n")
    response = await client.get("/api/settings")
    assert response.status_code == 422 and "web.editor" in response.json()["error"]
