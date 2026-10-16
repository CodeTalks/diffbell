"""The health registry on the API (health spec §6): `problems` in /api/dashboard,
with the undelivered-alert fields, and the heartbeat setting through config_edit."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import httpx
import pytest

from diffbell.config import load_config, write_default_config
from diffbell.health import Health
from diffbell.telegram import ErrorKind, TelegramError
from tests.fakes import FakeSender
from tests.web.support import Env
from tests.web.test_app_notify import settings_body


def health(env: Env, sender: FakeSender | None = None) -> Health:
    return Health(env.store, sender or FakeSender(), clock=lambda: env.now, tz=ZoneInfo("UTC"))


async def problems(client: httpx.AsyncClient) -> list[dict[str, object]]:
    body = (await client.get("/api/dashboard")).json()
    return list(body["problems"])


async def test_the_dashboard_lists_current_problems(env: Env, client: httpx.AsyncClient) -> None:
    assert await problems(client) == []
    h = health(env)
    h.open(
        "recrawl:proj",
        "warning",
        "project proj: Watchman had to recrawl the folder",
        project="proj",
        fix="watchman watch-del '/w/a b' ; watchman watch-project '/w/a b'",
    )
    env.now += 10
    h.open("config", "serious", "config rejected, keeping the last good config: x")
    h.notice("restart", "⚠️ Diffbell restarted after an unexpected stop")  # never listed
    assert await problems(client) == [
        {
            "key": "recrawl:proj",
            "severity": "warning",
            "project": "proj",
            "message": "project proj: Watchman had to recrawl the folder",
            "fix": "watchman watch-del '/w/a b' ; watchman watch-project '/w/a b'",
            "since": 1_000_000.0,
            "notified": False,
            "attempts": 0,
            "last_error": None,
        },
        {
            "key": "config",
            "severity": "serious",
            "project": None,
            "message": "config rejected, keeping the last good config: x",
            "fix": None,
            "since": 1_000_010.0,
            "notified": False,
            "attempts": 0,
            "last_error": None,
        },
    ]


async def test_an_undelivered_alert_shows_its_attempts_and_error(
    env: Env, client: httpx.AsyncClient
) -> None:
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "sendMessage: network error")])
    h = health(env, sender)
    h.open("project:proj", "serious", "project proj: watch lost", project="proj")
    await h.tick(env.now)
    [row] = await problems(client)
    assert (row["notified"], row["attempts"], row["last_error"]) == (
        False,
        1,
        "sendMessage: network error",
    )
    await h.tick(env.now + 30)
    [row] = await problems(client)
    assert (row["notified"], row["attempts"], row["last_error"]) == (True, 0, None)


async def test_a_resolved_problem_leaves_the_page_before_its_message_is_sent(
    env: Env, client: httpx.AsyncClient
) -> None:
    h = health(env)
    h.open("project:proj", "serious", "project proj: watch lost", project="proj")
    h.resolve("project:proj")  # its "resolved" message is still to be sent
    assert await problems(client) == []


# -- the heartbeat setting -------------------------------------------------------------


async def put_heartbeat(
    env: Env, client: httpx.AsyncClient, value: object, *, quiet: object = None
) -> httpx.Response:
    version = (await client.get("/api/settings")).json()["config_version"]
    notify = {"quiet_hours": quiet, "heartbeat": value}
    return await client.put("/api/settings", json=settings_body(version, notify=notify))


async def test_the_default_heartbeat_writes_nothing(env: Env, client: httpx.AsyncClient) -> None:
    assert (await put_heartbeat(env, client, "09:00")).status_code == 200
    text = env.config.read_text()
    assert "heartbeat:" not in text and "notify:" not in text  # already in force


async def test_heartbeat_off_is_an_explicit_null_and_on_again_writes_the_time(
    env: Env, client: httpx.AsyncClient
) -> None:
    assert (await put_heartbeat(env, client, None)).status_code == 200
    assert load_config(env.config).notify.heartbeat is None
    assert "heartbeat:" in env.config.read_text()
    settings = (await client.get("/api/settings")).json()
    assert settings["notify"] == {"quiet_hours": None, "heartbeat": None}
    assert (await put_heartbeat(env, client, "07:45")).status_code == 200
    assert load_config(env.config).notify.heartbeat == "07:45"


async def test_an_absent_heartbeat_is_left_as_it_is(env: Env, client: httpx.AsyncClient) -> None:
    await put_heartbeat(env, client, "21:00")
    version = (await client.get("/api/settings")).json()["config_version"]
    body = settings_body(version, notify={"quiet_hours": "23:00-08:00"})
    assert (await client.put("/api/settings", json=body)).status_code == 200
    config = load_config(env.config)
    assert (config.notify.quiet_hours, config.notify.heartbeat) == ("23:00-08:00", "21:00")


async def test_the_default_configs_heartbeat_line_keeps_its_comment(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.config.unlink()
    write_default_config(env.config)
    assert (await put_heartbeat(env, client, "06:30")).status_code == 200
    [line] = [x for x in env.config.read_text().splitlines() if "heartbeat:" in x]
    assert line.startswith('  heartbeat: "06:30"')  # its quotes are kept
    assert line.endswith("null turns it off")
    assert load_config(env.config).notify.heartbeat == "06:30"


@pytest.mark.parametrize("bad", ["9:00", "24:00", "noon", ""])
async def test_an_invalid_heartbeat_is_refused(
    env: Env, client: httpx.AsyncClient, bad: str
) -> None:
    before = env.config.read_text()
    response = await put_heartbeat(env, client, bad)
    assert response.status_code == 422
    assert response.json()["errors"] == {"heartbeat": "Use a time like 23:00."}
    assert env.config.read_text() == before


# -- review fixes, batch 4 ------------------------------------------------------------


async def test_problems_of_projects_no_longer_configured_are_not_listed(
    env: Env, client: httpx.AsyncClient
) -> None:
    """The daemon forgets them at its next reload; until then the page ignores them."""
    h = health(env)
    h.open("recrawl:old", "warning", "project old: Watchman had to recrawl", project="old")
    h.open("recrawl:proj", "warning", "project proj: Watchman had to recrawl", project="proj")
    h.open("missing:gone", "warning", "project gone: path /nowhere does not exist", project="gone")
    h.open("config", "serious", "config rejected, keeping the last good config: x")
    listed = sorted(str(p["key"]) for p in await problems(client))
    assert listed == ["config", "missing:gone", "recrawl:proj"]


async def test_heartbeat_off_writes_a_visible_null_and_on_a_quoted_time(
    env: Env, client: httpx.AsyncClient
) -> None:
    assert (await put_heartbeat(env, client, None)).status_code == 200
    [line] = [x for x in env.config.read_text().splitlines() if "heartbeat:" in x]
    assert line == "  heartbeat: null"
    assert (await put_heartbeat(env, client, "07:45")).status_code == 200
    [line] = [x for x in env.config.read_text().splitlines() if "heartbeat:" in x]
    assert line == '  heartbeat: "07:45"'


async def test_heartbeat_off_in_the_default_config_keeps_its_comment(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.config.unlink()
    write_default_config(env.config)
    assert (await put_heartbeat(env, client, None)).status_code == 200
    [line] = [x for x in env.config.read_text().splitlines() if "heartbeat:" in x]
    assert line.startswith("  heartbeat: null ")
    assert line.endswith("null turns it off")
    assert load_config(env.config).notify.heartbeat is None
