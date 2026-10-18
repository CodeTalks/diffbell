"""This Mac's name on the API (machine name spec §4, ADR 0023): GET /api/settings
returns `notify.machine` and the detected Computer Name; PUT writes it through
config_edit like the heartbeat; validation comes only from settings_check."""

from __future__ import annotations

import httpx
import pytest

from diffbell import machine
from diffbell.config import load_config, write_default_config
from tests.web.support import Env
from tests.web.test_app_notify import settings_body

MESSAGE = "Use at most 40 characters, without control characters."


async def put_machine(env: Env, client: httpx.AsyncClient, value: object) -> httpx.Response:
    version = (await client.get("/api/settings")).json()["config_version"]
    notify = {"quiet_hours": None, "machine": value}
    return await client.put("/api/settings", json=settings_body(version, notify=notify))


async def notify(client: httpx.AsyncClient) -> dict[str, object]:
    body = (await client.get("/api/settings")).json()
    return dict(body["notify"])


async def test_settings_return_the_configured_name_and_the_detected_one(
    env: Env, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = await notify(client)
    assert (got["machine"], got["machine_default"]) == (None, "Test Mac")
    monkeypatch.setattr(machine, "computer_name", lambda run=None: " Studio​ ")
    assert (await notify(client))["machine_default"] == "Studio"  # cleaned
    monkeypatch.setattr(machine, "computer_name", lambda run=None: None)
    assert (await notify(client))["machine_default"] is None


async def test_a_name_is_written_quoted_and_read_back(env: Env, client: httpx.AsyncClient) -> None:
    assert (await put_machine(env, client, "Work MBP")).status_code == 200
    assert load_config(env.config).notify.machine == "Work MBP"
    assert '  machine: "Work MBP"' in env.config.read_text()
    assert (await notify(client))["machine"] == "Work MBP"


async def test_off_writes_an_empty_name_and_null_removes_the_key(
    env: Env, client: httpx.AsyncClient
) -> None:
    assert (await put_machine(env, client, "")).status_code == 200
    assert load_config(env.config).notify.machine == ""
    assert 'machine: ""' in env.config.read_text()
    assert (await notify(client))["machine"] == ""
    assert (await put_machine(env, client, None)).status_code == 200
    assert load_config(env.config).notify.machine is None
    assert "machine" not in env.config.read_text()
    assert "notify:" not in env.config.read_text()  # the emptied block goes too


async def test_the_computer_name_already_in_force_writes_nothing(
    env: Env, client: httpx.AsyncClient
) -> None:
    assert (await put_machine(env, client, None)).status_code == 200
    text = env.config.read_text()
    assert "machine" not in text and "notify:" not in text


async def test_an_absent_name_is_left_as_it_is(env: Env, client: httpx.AsyncClient) -> None:
    await put_machine(env, client, "Work MBP")
    version = (await client.get("/api/settings")).json()["config_version"]
    body = settings_body(version, notify={"quiet_hours": "23:00-08:00"})
    assert (await client.put("/api/settings", json=body)).status_code == 200
    config = load_config(env.config)
    assert (config.notify.quiet_hours, config.notify.machine) == ("23:00-08:00", "Work MBP")


async def test_the_default_configs_comment_stays(env: Env, client: httpx.AsyncClient) -> None:
    env.config.unlink()
    write_default_config(env.config)
    assert (await put_machine(env, client, "Work MBP")).status_code == 200
    text = env.config.read_text()
    assert '# machine: "Work MBP"' in text  # the commented example is kept
    assert load_config(env.config).notify.machine == "Work MBP"


@pytest.mark.parametrize("bad", ["x" * 41, "Work\nMBP", "a‮b"])
async def test_an_invalid_name_is_refused_with_the_tables_message(
    env: Env, client: httpx.AsyncClient, bad: str
) -> None:
    before = env.config.read_text()
    response = await put_machine(env, client, bad)
    assert response.status_code == 422
    assert response.json()["errors"] == {"machine": MESSAGE}
    assert env.config.read_text() == before
    version = (await client.get("/api/settings")).json()["config_version"]
    body = settings_body(version, notify={"quiet_hours": None, "machine": bad})
    del body["config_version"]
    check = await client.post("/api/settings/check", json=body)
    assert check.json()["errors"] == {"machine": MESSAGE}
