"""POST /api/settings/check and PUT /api/settings's 422 (settings redesign spec §3):
one validation, one table of messages, nothing written by a check."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from diffbell.config import load_config
from diffbell.web import config_edit
from tests.web.support import Env

VALID: dict[str, Any] = {
    "debounce": {"quiet_seconds": "10", "max_wait_seconds": "60"},
    "diff": {"max_lines": "20"},
    "defaults": [],
    "ignore": [],
    "redact": [],
}


def body(**changes: Any) -> dict[str, Any]:
    return {**VALID, **changes}


async def check(client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    return await client.post("/api/settings/check", json=payload)


async def test_a_valid_body_checks_empty_and_writes_nothing(
    env: Env, client: httpx.AsyncClient
) -> None:
    before = env.config.read_bytes()
    response = await check(client, body())
    assert response.status_code == 200
    assert response.json() == {"errors": {}, "issues": {}}
    assert env.config.read_bytes() == before


async def test_the_check_reports_fields_and_pattern_lines(
    env: Env, client: httpx.AsyncClient
) -> None:
    before = env.config.read_bytes()
    payload = body(
        debounce={"quiet_seconds": "", "max_wait_seconds": "abc"},
        diff={"max_lines": "201"},
        redact=["ok.json", "/abs"],
        notify={"quiet_hours": "08:00-08:00", "heartbeat": "9:00"},
    )
    response = await check(client, payload)
    assert response.status_code == 200
    assert response.json() == {
        "errors": {
            "quiet_seconds": "Enter a number of seconds.",
            "max_wait_seconds": "Enter a number of seconds.",
            "max_lines": "Enter 0 to 200 lines.",
            "quiet_to": "Must differ from the start time.",
            "heartbeat": "Use a time like 23:00.",
        },
        "issues": {"redact": [{"line": 2, "reason": "patterns are relative to the project root"}]},
    }
    assert env.config.read_bytes() == before


async def test_the_check_takes_no_config_version(client: httpx.AsyncClient) -> None:
    response = await check(client, body(config_version="x"))
    assert response.status_code == 400


async def test_the_check_needs_a_session_and_the_csrf_token(anon: httpx.AsyncClient) -> None:
    assert (await check(anon, body())).status_code == 401


async def test_put_saves_numbers_sent_as_typed_text(env: Env, client: httpx.AsyncClient) -> None:
    version = config_edit.config_version(env.config)
    payload = body(
        config_version=version,
        debounce={"quiet_seconds": " 2.5 ", "max_wait_seconds": "30"},
        diff={"max_lines": "40"},
    )
    assert (await client.put("/api/settings", json=payload)).status_code == 200
    config = load_config(env.config)
    assert (
        config.debounce.quiet_seconds,
        config.debounce.max_wait_seconds,
        config.diff.max_lines,
    ) == (2.5, 30, 40)


@pytest.mark.parametrize(
    "changes",
    [
        {"debounce": {"quiet_seconds": "10", "max_wait_seconds": "5"}},
        {"debounce": {"quiet_seconds": 0, "max_wait_seconds": 60}},
        {"diff": {"max_lines": -1}},
        {"diff": {"max_lines": ""}},
        {"notify": {"quiet_hours": "-08:00"}},
        {"notify": {"quiet_hours": None, "heartbeat": "noon"}},
        {"ignore": ["[z-a]", "ok"], "defaults": ["a//b"]},
    ],
)
async def test_put_422_is_the_checks_result_plus_a_summary(
    env: Env, client: httpx.AsyncClient, changes: dict[str, Any]
) -> None:
    before = env.config.read_bytes()
    checked = (await check(client, body(**changes))).json()
    version = config_edit.config_version(env.config)
    response = await client.put("/api/settings", json=body(config_version=version, **changes))
    assert response.status_code == 422
    got = response.json()
    assert set(got) == {"error", "errors", "issues"}
    assert (got["errors"], got["issues"]) == (checked["errors"], checked["issues"])
    assert got["error"] and "\n" not in got["error"]
    assert env.config.read_bytes() == before


async def test_the_422_summary_names_the_field_and_its_message(
    env: Env, client: httpx.AsyncClient
) -> None:
    version = config_edit.config_version(env.config)
    payload = body(
        config_version=version, debounce={"quiet_seconds": "10", "max_wait_seconds": "5"}
    )
    response = await client.put("/api/settings", json=payload)
    assert response.json()["error"] == (
        "max_wait_seconds: Must be at least the quiet time (10 seconds)."
    )


async def test_a_config_file_rejection_says_the_tables_sentence(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.config.write_text(
        env.config.read_text() + "debounce:\n  quiet_seconds: 10\n  max_wait_seconds: 5\n"
    )
    response = await client.get("/api/settings")
    assert response.status_code == 422
    assert response.json() == {
        "error": "config.yaml is invalid: "
        "debounce.max_wait_seconds: Must be at least the quiet time (10 seconds)."
    }


@pytest.mark.parametrize("typed", [None, [10]])
@pytest.mark.parametrize(
    "changes",
    [
        lambda v: {"debounce": {"quiet_seconds": v, "max_wait_seconds": "60"}},
        lambda v: {"diff": {"max_lines": v}},
    ],
    ids=["seconds", "max_lines"],
)
async def test_null_or_a_list_for_a_number_is_a_400_never_a_500(
    env: Env, client: httpx.AsyncClient, changes: Any, typed: Any
) -> None:
    before = env.config.read_bytes()
    assert (await check(client, body(**changes(typed)))).status_code == 400
    version = config_edit.config_version(env.config)
    payload = body(config_version=version, **changes(typed))
    assert (await client.put("/api/settings", json=payload)).status_code == 400
    assert env.config.read_bytes() == before
