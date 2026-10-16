"""Every endpoint in-process (httpx.ASGITransport), including the error paths."""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi.routing import APIRoute

from diffbell.config import load_config
from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import ClockState
from diffbell.telegram import ErrorKind, TelegramError
from diffbell.web import auth, config_edit
from diffbell.web.app import WebContext, create_app
from tests.web.support import BASE, SECRET, WEB_SECRET, Env, log_in, make_context

STATE = ClockState("/w", "", "c:1", "h")


def add_event(env: Env, paths: list[str], project: str = "proj") -> int:
    obs = Observation()
    for path in paths:
        obs.upserts[path] = "fp"
        obs.changes.append((path, ChangeKind.MODIFIED))
    changes = env.store.record_observation(project, STATE, obs, now=env.now)
    event_id = env.store.create_event(project, changes, [], now=env.now)
    assert event_id is not None
    return event_id


# -- pages, auth and headers ----------------------------------------------------


async def test_healthz_needs_no_session(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


async def test_healthz_answers_a_challenge_with_an_hmac(anon: httpx.AsyncClient) -> None:
    nonce = "Abc-_0123456789xyz"
    response = await anon.get(f"/healthz?challenge={nonce}")
    assert response.json() == {"ok": True, "proof": auth.prove(WEB_SECRET, nonce)}
    assert auth.proof_ok(WEB_SECRET, nonce, response.json()["proof"])


@pytest.mark.parametrize(
    "challenge", ["short", "x" * 65, "has%20space-padding", "a.b.c.d.e.f.g.h.i"]
)
async def test_healthz_rejects_a_bad_challenge(anon: httpx.AsyncClient, challenge: str) -> None:
    response = await anon.get("/healthz", params={"challenge": challenge})
    assert response.status_code == 400 and "challenge" in response.json()["error"]


async def test_wrong_host_is_refused(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/healthz", headers={"host": "evil.example:8765"})
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden host"}


async def test_security_headers_on_every_response(client: httpx.AsyncClient) -> None:
    for path in ("/healthz", "/api/dashboard", "/login?t=bad"):
        response = await client.get(path)
        for name, value in auth.SECURITY_HEADERS.items():
            assert response.headers[name] == value, (path, name)
    assert (await client.get("/api/dashboard")).headers["cache-control"] == "no-store"


async def test_login_sets_a_strict_cookie_and_redirects(env: Env, anon: httpx.AsyncClient) -> None:
    token = auth.issue_login(env.store, env.now, WEB_SECRET)
    response = await anon.get(f"/login?t={token}")
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.SESSION_COOKIE}=")
    for part in ("HttpOnly", "Max-Age=2592000", "Path=/", "SameSite=strict"):
        assert part in cookie
    # Single use: the same link again shows the expired page.
    again = await anon.get(f"/login?t={token}")
    assert again.status_code == 400 and "link has expired" in again.text


@pytest.mark.parametrize(
    ("query", "location"),
    [
        ("&next=telegram", "/?view=settings&section=telegram"),
        ("", "/"),
        ("&next=", "/"),
        ("&next=other", "/"),
        ("&next=TELEGRAM", "/"),
        ("&next=https://evil.example/", "/"),
        ("&next=//evil.example", "/"),
    ],
)
async def test_login_redirects_only_to_an_allowlisted_next(
    env: Env, anon: httpx.AsyncClient, query: str, location: str
) -> None:
    token = auth.issue_login(env.store, env.now, WEB_SECRET)
    response = await anon.get(f"/login?t={token}{query}")
    assert response.status_code == 303 and response.headers["location"] == location
    assert response.headers["set-cookie"].startswith(f"{auth.SESSION_COOKIE}=")


async def test_an_expired_link_with_next_still_shows_the_expired_page(
    anon: httpx.AsyncClient,
) -> None:
    response = await anon.get("/login?t=bad&next=telegram")
    assert response.status_code == 400 and "link has expired" in response.text


async def test_login_token_for_another_server_start_is_refused(
    env: Env, anon: httpx.AsyncClient
) -> None:
    """A token minted against an earlier server secret (before a restart) is dead."""
    token = auth.issue_login(env.store, env.now, bytes(32))
    response = await anon.get(f"/login?t={token}")
    assert response.status_code == 400 and "link has expired" in response.text
    assert auth.SESSION_COOKIE not in response.headers.get("set-cookie", "")


async def test_index_without_session_shows_the_login_hint(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/")
    assert response.status_code == 401
    assert "Run <code>diffbell web</code> to log in." in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/session"),
        ("GET", "/api/dashboard"),
        ("GET", "/api/projects"),
        ("GET", "/api/events"),
        ("GET", "/api/settings"),
        ("POST", "/api/preview"),
        ("POST", "/api/logout"),
    ],
)
async def test_api_needs_a_session(anon: httpx.AsyncClient, method: str, path: str) -> None:
    response = await anon.request(method, path, json={})
    assert response.status_code == 401
    assert response.json() == {"error": "not logged in: run `diffbell web`"}


async def test_expired_session_is_401(env: Env, client: httpx.AsyncClient) -> None:
    env.now += auth.SESSION_TTL
    assert (await client.get("/api/dashboard")).status_code == 401


async def test_mutation_needs_the_csrf_header(client: httpx.AsyncClient) -> None:
    del client.headers[auth.CSRF_HEADER]
    response = await client.post("/api/test-notification")
    assert response.status_code == 403 and "CSRF" in response.json()["error"]
    response = await client.post("/api/test-notification", headers={auth.CSRF_HEADER: "nope"})
    assert response.status_code == 403


async def test_mutation_from_a_foreign_origin_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/test-notification", headers={"origin": "http://evil.example"}
    )
    assert response.status_code == 403
    ok = await client.post("/api/test-notification", headers={"origin": "http://127.0.0.1:8765"})
    assert ok.status_code == 200


async def test_logout_ends_the_session(env: Env, client: httpx.AsyncClient) -> None:
    other = auth.redeem_login(
        env.store, auth.issue_login(env.store, env.now, WEB_SECRET), env.now, WEB_SECRET
    )
    assert other is not None
    assert (await client.post("/api/logout")).json() == {"ok": True}
    assert (await client.get("/api/dashboard")).status_code == 401
    assert auth.session_for(env.store, other[0], env.now) is not None  # only this one


async def test_logout_all_ends_every_session(env: Env, client: httpx.AsyncClient) -> None:
    other = auth.redeem_login(
        env.store, auth.issue_login(env.store, env.now, WEB_SECRET), env.now, WEB_SECRET
    )
    assert other is not None
    assert (await client.post("/api/logout?all=true")).json() == {"ok": True}
    assert (await client.get("/api/dashboard")).status_code == 401
    assert auth.session_for(env.store, other[0], env.now) is None


async def test_unknown_route_uses_the_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/nope")
    assert response.status_code == 404 and "error" in response.json()


# -- dashboard and events -------------------------------------------------------


async def test_dashboard_reads_the_snapshot_and_the_outbox(
    env: Env, client: httpx.AsyncClient
) -> None:
    sent = add_event(env, ["package.json"])
    env.store.mark_sent(sent, 1)
    env.now += 10
    failed = add_event(env, ["Makefile"])
    env.store.mark_failed(failed, "sendMessage: Unauthorized", kind="auth", at=0.0)
    body = (await client.get("/api/dashboard")).json()
    assert body["daemon"] == {
        "pid": 4242,
        "connected": True,
        "config_version": "v-applied",
        "config_rejected_version": None,
    }
    assert body["watchman"] == {"version": "2026.10.05.00"}
    assert body["web"] == {
        "running": True,
        "port": 8765,
        "last_error": None,
        "editor": "cursor",
    }
    assert body["telegram"] == {
        "configured": True,
        "last_ok": 1_000_000.0,
        "last_error": "sendMessage: Unauthorized",
    }
    assert [p["name"] for p in body["projects"]] == ["proj"]
    assert body["projects"][0]["status"] == "subscribed"
    assert body["missing"] == [
        {
            "name": "gone",
            "path": "/nowhere",
            "snooze": None,
            "notify_on": ["created", "modified", "deleted"],
        }
    ]


async def test_dashboard_version_is_null_while_disconnected(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.snapshot.update(connected=False, watchman_version=None)
    body = (await client.get("/api/dashboard")).json()
    assert body["daemon"]["connected"] is False and body["watchman"] == {"version": None}


async def test_events_filters_pages_and_marks_redaction(
    env: Env, client: httpx.AsyncClient
) -> None:
    first = add_event(env, ["package.json", ".env.local"])
    second = add_event(env, ["Makefile"], project="other")
    env.store.mark_failed(second, "boom", kind="bad_request", at=0.0)
    body = (await client.get("/api/events")).json()
    assert body["total"] == 2 and [e["id"] for e in body["events"]] == [second, first]
    files = body["events"][1]["files"]
    assert files == [
        {"path": "package.json", "kind": "modified", "redacted": False},
        {"path": ".env.local", "kind": "modified", "redacted": True},
    ]
    assert [e["id"] for e in (await client.get("/api/events?status=failed")).json()["events"]] == [
        second
    ]
    assert (await client.get("/api/events?project=proj")).json()["total"] == 1
    assert (await client.get("/api/events?q=ENV")).json()["total"] == 1
    paged = (await client.get("/api/events?limit=1&offset=1")).json()
    assert [e["id"] for e in paged["events"]] == [first]
    older = (await client.get(f"/api/events?before_id={second}")).json()
    assert [e["id"] for e in older["events"]] == [first] and older["total"] == 2


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "offset=-1", "status=bogus", "before_id=0", "before_id=abc"],
)
async def test_events_rejects_bad_parameters(client: httpx.AsyncClient, query: str) -> None:
    response = await client.get(f"/api/events?{query}")
    assert response.status_code == 400 and response.json()["error"].startswith("invalid request")


async def test_retry_only_failed_events(env: Env, client: httpx.AsyncClient) -> None:
    failed = add_event(env, ["package.json"])
    env.store.mark_failed(failed, "boom", kind="bad_request", at=0.0)
    pending = add_event(env, ["Makefile"])
    assert (await client.post(f"/api/events/{failed}/retry")).json() == {"ok": True}
    assert env.store.get_event(failed).status == "pending"
    conflict = await client.post(f"/api/events/{pending}/retry")
    assert conflict.status_code == 409
    assert (await client.post("/api/events/9999/retry")).status_code == 404


async def test_test_notification(env: Env, client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/test-notification")).json() == {"ok": True}
    assert env.sent == ["sample"]
    # By kind: only "can't reach" is an outage (503); the card shows each one.
    for kind, description, status, error in (
        (
            ErrorKind.AUTH,
            "sendMessage: Unauthorized",
            422,
            "Telegram rejected the bot token: it was probably revoked in @BotFather."
            " Use Replace bot.",
        ),
        (ErrorKind.RETRYABLE, "network error", 503, "Couldn't reach Telegram; try again."),
        (
            ErrorKind.BAD_REQUEST,
            "sendMessage: chat not found",
            422,
            "Telegram refused the message: sendMessage: chat not found",
        ),
    ):
        env.send_error = TelegramError(kind, description)
        response = await client.post("/api/test-notification")
        assert (response.status_code, response.json()) == (status, {"error": error}), kind


# -- projects, preview, settings --------------------------------------------------


async def test_projects_lists_effective_lists_and_status(
    env: Env, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/api/projects")).json()
    assert body["config_version"] == config_edit.config_version(env.config)
    (project,) = body["projects"]
    assert project["name"] == "proj" and project["status"] == "subscribed"
    assert "package.json" in project["watched"] and project["excluded"] == []


async def test_add_edit_remove_project(env: Env, client: httpx.AsyncClient) -> None:
    other = env.home / "other"
    other.mkdir()
    version = (await client.get("/api/projects")).json()["config_version"]
    added = await client.post(
        "/api/projects",
        json={
            "config_version": version,
            "path": str(other),
            "patterns": ["x/**"],
            "ignore": [],
            "use_defaults": True,
        },
    )
    assert added.status_code == 200
    version = added.json()["config_version"]
    assert [p.path for p in load_config(env.config).projects] == [str(env.proj), str(other)]
    assert env.config.read_text().startswith("# my config\n")
    edited = await client.put(
        "/api/projects/other",
        json={
            "config_version": version,
            "path": str(other),
            "name": "renamed",
            "patterns": [],
            "ignore": ["a/**"],
            "use_defaults": True,
        },
    )
    assert edited.status_code == 200
    version = edited.json()["config_version"]
    assert load_config(env.config).projects[1].name == "renamed"
    removed = await client.delete(f"/api/projects/renamed?config_version={version}")
    assert removed.status_code == 200
    assert [p.path for p in load_config(env.config).projects] == [str(env.proj)]


async def test_project_errors(env: Env, client: httpx.AsyncClient) -> None:
    version = config_edit.config_version(env.config)
    before = env.config.read_bytes()
    stale = await client.post(
        "/api/projects", json={"config_version": "stale", "path": str(env.home / "x")}
    )
    assert stale.status_code == 409 and stale.json() == {"error": "config changed on disk"}
    invalid = await client.post(
        "/api/projects",
        json={"config_version": version, "path": str(env.proj), "patterns": ["/abs"]},
    )
    assert invalid.status_code == 422 and "relative to the project root" in invalid.json()["error"]
    missing = await client.delete(f"/api/projects/nope?config_version={version}")
    assert missing.status_code == 404
    shape = await client.post("/api/projects", json={"path": 5})
    assert shape.status_code == 400
    assert env.config.read_bytes() == before


async def test_preview_lists_names_and_redaction(env: Env, client: httpx.AsyncClient) -> None:
    body = (
        await client.post(
            "/api/preview",
            json={"path": str(env.proj), "patterns": [], "ignore": [], "use_defaults": True},
        )
    ).json()
    assert body == {
        "files": [
            {"path": ".env.local", "redacted": True},
            {"path": "package.json", "redacted": False},
        ],
        "total": 2,
        "truncated": False,
        "total_exact": True,
    }


async def test_preview_errors_never_echo_the_typed_path(
    env: Env, client: httpx.AsyncClient
) -> None:
    for path in (str(env.home / "MARKER-missing"), str(env.proj / "package.json")):
        response = await client.post("/api/preview", json={"path": path})
        assert response.status_code == 422
        assert "MARKER" not in response.text and str(env.home) not in response.text


@pytest.mark.parametrize("path", ["~", "/", "relative"])
async def test_preview_rejects_paths_outside_home(
    env: Env, client: httpx.AsyncClient, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(env.home))
    response = await client.post("/api/preview", json={"path": path})
    assert response.status_code == 422


async def test_settings_round_trip(env: Env, client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/settings")).json()
    assert body["web"] == {"port": 8765, "editor": "cursor"}
    assert ".env*" in body["builtin_redact"] and "package.json" in body["builtin_defaults"]
    update = {
        "config_version": body["config_version"],
        "debounce": {"quiet_seconds": 5, "max_wait_seconds": 30},
        "diff": {"max_lines": 40},
        "defaults": ["Procfile"],
        "ignore": [],
        "redact": ["*.secret.json"],
    }
    assert (await client.put("/api/settings", json=update)).status_code == 200
    config = load_config(env.config)
    assert (config.debounce.quiet_seconds, config.diff.max_lines, config.defaults) == (
        5,
        40,
        ["Procfile"],
    )
    stale = await client.put("/api/settings", json=update)
    assert stale.status_code == 409


async def test_settings_cannot_change_the_port_or_builtins(
    env: Env, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/api/settings")).json()
    update = {
        "config_version": body["config_version"],
        "debounce": {"quiet_seconds": 10, "max_wait_seconds": 60},
        "diff": {"max_lines": 20},
        "defaults": [],
        "ignore": [],
        "redact": [],
        "web": {"port": 9999},
    }
    assert (await client.put("/api/settings", json=update)).status_code == 400
    del update["web"]
    update["debounce"] = {"quiet_seconds": 10, "max_wait_seconds": 5}
    response = await client.put("/api/settings", json=update)
    assert response.status_code == 422 and "max_wait_seconds" in response.json()["error"]


async def test_no_response_ever_contains_file_contents(env: Env, client: httpx.AsyncClient) -> None:
    event_id = add_event(env, ["package.json", ".env.local"])
    bodies = [
        await client.get("/api/dashboard"),
        await client.get("/api/events"),
        await client.get(f"/api/events/{event_id}"),
        await client.get("/api/projects"),
        await client.get("/api/settings"),
        await client.post("/api/preview", json={"path": str(env.proj)}),
        await client.post("/api/browse", json={"path": str(env.proj)}),
    ]
    for response in bodies:
        assert SECRET not in response.text, response.request.url


async def test_no_response_ever_contains_the_web_secret(
    env: Env, client: httpx.AsyncClient
) -> None:
    secret = WEB_SECRET.hex()
    responses = [
        await client.get("/healthz"),
        await client.get("/healthz?challenge=0123456789abcdefXYZ"),
        await client.get("/api/session"),
        await client.get("/api/dashboard"),
        await client.get("/api/events"),
        await client.get("/api/projects"),
        await client.get("/api/settings"),
        await client.post("/api/preview", json={"path": str(env.proj)}),
        await client.post("/api/browse", json={"path": str(env.proj)}),
        await client.post("/api/test-notification"),
        await client.get("/login?t=bad"),
        await client.get("/nope"),
    ]
    for response in responses:
        assert secret not in response.text, response.request.url
        assert secret not in str(response.headers), response.request.url


# -- review hardening ---------------------------------------------------------------


def settings_update(version: str, **changes: object) -> dict[str, object]:
    update: dict[str, object] = {
        "config_version": version,
        "debounce": {"quiet_seconds": 10, "max_wait_seconds": 60},
        "diff": {"max_lines": 20},
        "defaults": [],
        "ignore": [],
        "redact": [],
    }
    update.update(changes)
    return update


@pytest.mark.parametrize(
    "changes",
    [
        {"diff": {"max_lines": True}},
        {"diff": {"max_lines": 20.0}},
        {"diff": {"max_lines": None}},
        {"debounce": {"quiet_seconds": True, "max_wait_seconds": 60}},
    ],
)
async def test_settings_rejects_values_of_the_wrong_type(
    env: Env, client: httpx.AsyncClient, changes: dict[str, object]
) -> None:
    before = env.config.read_bytes()
    update = settings_update(config_edit.config_version(env.config), **changes)
    response = await client.put("/api/settings", json=update)
    assert response.status_code == 400, response.text
    assert response.json()["error"].startswith("invalid request")
    assert env.config.read_bytes() == before


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        ({"diff": {"max_lines": -1}}, {"max_lines": "Enter 0 to 200 lines."}),
        ({"diff": {"max_lines": 201}}, {"max_lines": "Enter 0 to 200 lines."}),
        ({"diff": {"max_lines": "20"}}, {}),
        (
            {"debounce": {"quiet_seconds": 0, "max_wait_seconds": 60}},
            {"quiet_seconds": "Must be more than 0 seconds."},
        ),
        (
            {"debounce": {"quiet_seconds": -1, "max_wait_seconds": 60}},
            {"quiet_seconds": "Must be more than 0 seconds."},
        ),
        (
            {"debounce": {"quiet_seconds": 10, "max_wait_seconds": "inf"}},
            {"max_wait_seconds": "Enter a number of seconds."},
        ),
    ],
)
async def test_settings_out_of_range_values_are_the_tables_422(
    env: Env, client: httpx.AsyncClient, changes: dict[str, object], errors: dict[str, str]
) -> None:
    before = env.config.read_bytes()
    update = settings_update(config_edit.config_version(env.config), **changes)
    response = await client.put("/api/settings", json=update)
    if not errors:
        assert response.status_code == 200, response.text
        return
    assert response.status_code == 422, response.text
    assert response.json()["errors"] == errors
    assert env.config.read_bytes() == before


@pytest.mark.parametrize("literal", ["Infinity", "NaN", "-Infinity"])
async def test_settings_rejects_non_finite_seconds(
    env: Env, client: httpx.AsyncClient, literal: str
) -> None:
    version = config_edit.config_version(env.config)
    raw = (
        f'{{"config_version": "{version}", '
        f'"debounce": {{"quiet_seconds": {literal}, "max_wait_seconds": {literal}}}, '
        '"diff": {"max_lines": 20}, "defaults": [], "ignore": [], "redact": []}'
    )
    response = await client.put(
        "/api/settings", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"].startswith("invalid request: body.debounce.quiet_seconds")


async def test_settings_accepts_the_bounds(env: Env, client: httpx.AsyncClient) -> None:
    update = settings_update(
        config_edit.config_version(env.config),
        debounce={"quiet_seconds": 0.5, "max_wait_seconds": 0.5},
        diff={"max_lines": 200},
    )
    assert (await client.put("/api/settings", json=update)).status_code == 200
    config = load_config(env.config)
    assert (config.debounce.quiet_seconds, config.diff.max_lines) == (0.5, 200)


async def test_events_default_and_maximum_page(env: Env, client: httpx.AsyncClient) -> None:
    for _ in range(55):
        add_event(env, ["package.json"])
    assert len((await client.get("/api/events")).json()["events"]) == 50
    body = (await client.get("/api/events?limit=200")).json()
    assert (len(body["events"]), body["total"]) == (55, 55)


async def test_editing_a_project_whose_directory_is_missing(
    env: Env, client: httpx.AsyncClient
) -> None:
    gone = env.home / "gone"
    env.config.write_text(f"# my config\nprojects:\n  - path: {env.proj}\n  - path: {gone}\n")
    before = env.config.read_bytes()
    version = config_edit.config_version(env.config)
    response = await client.put(
        "/api/projects/gone",
        json={"config_version": version, "path": str(gone), "patterns": ["x/**"]},
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": "project directory does not exist (or is not a directory); "
        "fix the path or remove the project"
    }
    assert env.config.read_bytes() == before
    removed = await client.delete(f"/api/projects/gone?config_version={version}")
    assert removed.status_code == 200
    assert [p.path for p in load_config(env.config).projects] == [str(env.proj)]


async def test_project_name_and_path_are_validated(env: Env, client: httpx.AsyncClient) -> None:
    other = env.home / "other"
    other.mkdir()
    before = env.config.read_bytes()
    version = config_edit.config_version(env.config)
    bad_name = await client.post(
        "/api/projects", json={"config_version": version, "path": str(other), "name": "a/b"}
    )
    assert bad_name.status_code == 422 and "project name" in bad_name.json()["error"]
    nul = await client.post("/api/projects", json={"config_version": version, "path": "/a\u0000b"})
    assert nul.status_code == 400 and "NUL" in nul.json()["error"]
    assert env.config.read_bytes() == before


async def test_no_config_write_422_names_a_path(env: Env, client: httpx.AsyncClient) -> None:
    """Plan 2's rule: 422 bodies carry no paths (the home path least of all)."""
    version = config_edit.config_version(env.config)
    attempts = [
        {"path": str(env.home / "gone")},  # missing directory
        {"path": "relative/dir", "name": "rel"},  # not absolute
        {"path": str(env.proj), "name": "again"},  # duplicate path
    ]
    for body in attempts:
        response = await client.post("/api/projects", json={"config_version": version, **body})
        assert response.status_code == 422, body
        assert str(env.home) not in response.text, response.text
        assert body["path"] not in response.text, response.text
    duplicate = await client.post(
        "/api/projects", json={"config_version": version, "path": str(env.proj), "name": "again"}
    )
    assert "projects proj and again" in duplicate.json()["error"]


async def test_preview_with_a_nul_byte_is_a_clean_error(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/preview", json={"path": "/a\u0000b"})
    assert response.status_code == 422
    assert "NUL" in response.json()["error"]


async def test_every_route_handler_is_async(env: Env) -> None:
    routes = [r for r in create_app(make_context(env)).routes if isinstance(r, APIRoute)]
    assert routes
    for route in routes:
        assert inspect.iscoroutinefunction(route.endpoint), route.path


# -- hardening ------------------------------------------------------------------

MARKER = "MARKER-7c1e9d"
HUGE = "99999999999999999999999"  # > 2**63 - 1: SQLite would raise OverflowError


@contextlib.asynccontextmanager
async def _logged_in(env: Env, ctx: WebContext) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await log_in(env, client)
        yield client


async def test_unhandled_error_is_a_json_500_with_the_security_headers(env: Env) -> None:
    def broken_snapshot() -> dict[str, object]:
        raise RuntimeError(f"boom {MARKER}")

    ctx = dataclasses.replace(make_context(env), snapshot=broken_snapshot)
    async with _logged_in(env, ctx) as client:
        response = await client.get("/api/dashboard")
    assert response.status_code == 500
    assert response.json() == {"error": "internal error"}
    for name, value in auth.SECURITY_HEADERS.items():
        assert response.headers[name] == value, name
    assert response.headers["cache-control"] == "no-store"
    assert MARKER not in response.text and "Traceback" not in response.text


@pytest.mark.parametrize(
    "request_line",
    [
        ("GET", f"/api/events?offset={HUGE}"),
        ("POST", f"/api/events/{HUGE}/retry"),
        ("POST", "/api/events/0/retry"),
    ],
)
async def test_out_of_range_integers_are_a_clean_400(
    client: httpx.AsyncClient, request_line: tuple[str, str]
) -> None:
    method, path = request_line
    response = await client.request(method, path)
    assert response.status_code == 400, response.text
    assert response.json()["error"].startswith("invalid request")


async def test_the_largest_offset_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/api/events?offset={2**63 - 1}")
    assert response.status_code == 200 and response.json()["events"] == []


@pytest.fixture
def restore_modes(env: Env) -> Iterator[None]:
    yield
    os.chmod(env.config.parent, 0o755)
    os.chmod(env.config, 0o644)


@pytest.mark.usefixtures("restore_modes")
async def test_unreadable_config_is_a_503(env: Env, client: httpx.AsyncClient) -> None:
    os.chmod(env.config, 0)
    for path in ("/api/projects", "/api/settings"):
        response = await client.get(path)
        assert response.status_code == 503, response.text
        assert response.json() == {"error": "cannot read config: Permission denied"}
    removed = await client.delete("/api/projects/proj?config_version=x")
    assert removed.status_code == 503
    assert removed.json()["error"].startswith("cannot read config")


@pytest.mark.usefixtures("restore_modes")
async def test_read_only_config_directory_is_a_503(env: Env, client: httpx.AsyncClient) -> None:
    before = env.config.read_bytes()
    version = config_edit.config_version(env.config)
    os.chmod(env.config.parent, 0o555)
    response = await client.delete(f"/api/projects/proj?config_version={version}")
    assert response.status_code == 503, response.text
    assert response.json() == {"error": "cannot write config: Permission denied"}
    assert env.config.read_bytes() == before


async def test_an_oversized_body_is_a_413(client: httpx.AsyncClient) -> None:
    body = b'{"path": "' + b"a" * (1024 * 1024) + b'"}'
    response = await client.post(
        "/api/preview", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json() == {"error": "request body too large (limit 1 MiB)"}
    assert response.headers["cache-control"] == "no-store"


async def test_a_body_without_content_length_is_refused(client: httpx.AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"path": "/x"}'

    response = await client.post(
        "/api/preview", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 411
    assert "error" in response.json()


# "\xb2" is "²" in latin-1 (how the server decodes headers): str.isdigit() is True
# for it, but int() rejects it.
@pytest.mark.parametrize("length", [b"lots", b"\xb2", b"1" * 30, b"-1", b" 1"])
async def test_a_bad_content_length_is_a_400(client: httpx.AsyncClient, length: bytes) -> None:
    response = await client.post("/api/preview", headers={b"content-length": length})
    assert response.status_code == 400 and response.json() == {"error": "invalid Content-Length"}


@pytest.mark.parametrize(
    "body",
    [
        {"path": "/x", "patterns": ["a"] * 201},
        {"path": "/x", "ignore": ["a"] * 201},
        {"path": "/" + "a" * 1024},
        {"path": "/x", "patterns": ["a" * 1025]},
    ],
)
async def test_preview_input_sizes_are_bounded(
    client: httpx.AsyncClient, body: dict[str, object]
) -> None:
    response = await client.post("/api/preview", json=body)
    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "n" * 1025},
        {"patterns": ["a"] * 201},
        {"ignore": ["a" * 1025]},
        {"config_version": "v" * 1025},
    ],
)
async def test_project_input_sizes_are_bounded(
    env: Env, client: httpx.AsyncClient, changes: dict[str, object]
) -> None:
    body = {"config_version": "x", "path": str(env.proj), **changes}
    assert (await client.post("/api/projects", json=body)).status_code == 400
    assert (await client.put("/api/projects/proj", json=body)).status_code == 400


@pytest.mark.parametrize(
    "changes",
    [{"defaults": ["a"] * 201}, {"ignore": ["a"] * 201}, {"redact": ["a" * 1025]}],
)
async def test_settings_input_sizes_are_bounded(
    env: Env, client: httpx.AsyncClient, changes: dict[str, object]
) -> None:
    update = settings_update(config_edit.config_version(env.config), **changes)
    assert (await client.put("/api/settings", json=update)).status_code == 400


async def test_query_and_path_strings_are_bounded(client: httpx.AsyncClient) -> None:
    long = "a" * 1025
    assert (await client.get(f"/api/events?q={long}")).status_code == 400
    assert (await client.get(f"/api/events?project={long}")).status_code == 400
    assert (await client.delete(f"/api/projects/{long}?config_version=x")).status_code == 400
    assert (await client.delete(f"/api/projects/proj?config_version={long}")).status_code == 400


async def test_a_422_never_echoes_submitted_values(env: Env, client: httpx.AsyncClient) -> None:
    version = config_edit.config_version(env.config)
    bad_name = await client.post(
        "/api/projects",
        json={"config_version": version, "path": str(env.proj), "name": f"a/{MARKER}"},
    )
    assert bad_name.status_code == 422
    assert MARKER not in bad_name.text
    bad_settings = await client.put(
        "/api/settings",
        json=settings_update(version, debounce={"quiet_seconds": 10, "max_wait_seconds": 5}),
    )
    assert bad_settings.status_code == 422
    assert "max_wait_seconds" in bad_settings.json()["error"]
    assert "input_value" not in bad_settings.text


@pytest.mark.parametrize(
    "text",
    [
        f"projects:\n  - path: /x\n    name: 'a/{MARKER}'\n",
        f"debounce: {{quiet_seconds: '{MARKER}'}}\n",
        f"projects: [\n  {MARKER}: [\n",
    ],
)
async def test_a_422_never_echoes_the_config_file(
    env: Env, client: httpx.AsyncClient, text: str
) -> None:
    env.config.write_text(text)
    version = config_edit.config_version(env.config)
    for response in (await client.get("/api/projects"), await client.get("/api/settings")):
        assert response.status_code == 422, response.text
        assert MARKER not in response.text and "input_value" not in response.text
    removed = await client.delete(f"/api/projects/proj?config_version={version}")
    assert removed.status_code in (404, 422), removed.text
    assert MARKER not in removed.text


async def test_405_keeps_the_allow_header(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/dashboard")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert "error" in response.json()


async def test_the_home_directory_itself_cannot_be_a_project(
    env: Env, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(env.home))
    before = env.config.read_bytes()
    version = config_edit.config_version(env.config)
    for path in (str(env.home), "~", f"{env.home}/proj/.."):
        response = await client.post(
            "/api/projects", json={"config_version": version, "path": path, "name": "h"}
        )
        assert response.status_code == 422, path
        assert "home directory" in response.json()["error"]
    assert env.config.read_bytes() == before


@pytest.mark.parametrize("content", [None, b"\xff\xfe broken\n", b"- a\n- b\n"])
async def test_config_errors_never_name_the_config_path(
    env: Env, client: httpx.AsyncClient, content: bytes | None
) -> None:
    if content is None:
        env.config.unlink()
    else:
        env.config.write_bytes(content)
    version = config_edit.config_version(env.config)
    responses = [
        await client.get("/api/projects"),
        await client.get("/api/settings"),
        await client.delete(f"/api/projects/proj?config_version={version}"),
        await client.post("/api/projects", json={"config_version": version, "path": str(env.proj)}),
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        assert str(env.config.parent) not in response.text, response.text
