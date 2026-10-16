"""POST /api/browse in-process (UI refine spec §3.2)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from diffbell.web import auth
from tests.web.support import SECRET, Env


def body(env: Env, **changes: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(env.proj),
        "dir": "",
        "patterns": [],
        "ignore": [],
        "use_defaults": True,
    }
    payload.update(changes)
    return payload


async def test_browse_lists_one_folder_with_badges(env: Env, client: httpx.AsyncClient) -> None:
    response = await client.post("/api/browse", json=body(env))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "dir": "",
        "entries": [
            {
                "name": "node_modules",
                "type": "dir",
                "redacted": False,
                "excluded": True,
                "matched_by": None,
                "ignored_by": None,
            },
            {
                "name": ".env.local",
                "type": "file",
                "redacted": True,
                "excluded": False,
                "matched_by": "built-in: .env*",
                "ignored_by": None,
            },
            {
                "name": "package.json",
                "type": "file",
                "redacted": False,
                "excluded": False,
                "matched_by": "built-in: package.json",
                "ignored_by": None,
            },
        ],
        "truncated": False,
        "error": None,
    }
    assert SECRET not in response.text


async def test_browse_agrees_with_the_preview(env: Env, client: httpx.AsyncClient) -> None:
    (env.proj / "sub").mkdir()
    (env.proj / "sub" / "x.lock").write_text("x\n")
    (env.proj / "notes.txt").write_text("x\n")
    lists = {"patterns": ["*.txt"], "ignore": ["sub/**"]}
    preview = (await client.post("/api/preview", json={"path": str(env.proj), **lists})).json()
    watched: set[str] = set()
    for folder in ("", "sub"):
        listed = (await client.post("/api/browse", json=body(env, dir=folder, **lists))).json()
        for entry in listed["entries"]:
            path = f"{folder}/{entry['name']}" if folder else entry["name"]
            if entry["type"] != "dir" and entry["matched_by"] and not entry["ignored_by"]:
                watched.add(path)
    assert watched == {f["path"] for f in preview["files"]}


async def test_browse_needs_a_session_and_csrf(env: Env, client: httpx.AsyncClient) -> None:
    del client.headers[auth.CSRF_HEADER]
    assert (await client.post("/api/browse", json=body(env))).status_code == 403


async def test_browse_needs_a_login(env: Env, anon: httpx.AsyncClient) -> None:
    assert (await anon.post("/api/browse", json=body(env))).status_code == 401


@pytest.mark.parametrize("folder", ["../MARKER", "MARKER/..", "/MARKER", "node_modules"])
async def test_browse_refuses_a_bad_folder_without_naming_it(
    env: Env, client: httpx.AsyncClient, folder: str
) -> None:
    response = await client.post("/api/browse", json=body(env, dir=folder))
    assert response.status_code == 400
    assert "MARKER" not in response.text and str(env.home) not in response.text


async def test_browse_checks_the_path_and_patterns_like_the_preview(
    env: Env, client: httpx.AsyncClient
) -> None:
    outside = await client.post("/api/browse", json=body(env, path=str(env.home.parent)))
    assert outside.status_code == 422 and "home directory" in outside.json()["error"]
    missing = await client.post("/api/browse", json=body(env, path=str(env.home / "MARKER")))
    assert missing.status_code == 422 and "MARKER" not in missing.text
    pattern = await client.post("/api/browse", json=body(env, patterns=["/abs"]))
    assert pattern.status_code == 422 and "relative to the project root" in pattern.text


async def test_browse_reports_an_unreadable_folder(env: Env, client: httpx.AsyncClient) -> None:
    response = await client.post("/api/browse", json=body(env, dir="vanished"))
    assert response.status_code == 200
    assert response.json() == {
        "dir": "vanished",
        "entries": [],
        "truncated": False,
        "error": "unreadable",
    }


async def test_browse_input_sizes_are_bounded(env: Env, client: httpx.AsyncClient) -> None:
    for changes in ({"dir": "a" * 1025}, {"patterns": ["a"] * 201}, {"extra": 1}):
        response = await client.post("/api/browse", json=body(env, **changes))
        assert response.status_code == 400, changes


@pytest.mark.parametrize("folder", ["\ud800", "a/\ud800"])
async def test_a_name_that_cannot_be_encoded_is_a_400(
    env: Env, client: httpx.AsyncClient, folder: str
) -> None:
    """Request parsing already refuses it (browse.open_dir refuses it as well, for
    a direct caller). httpx can't encode a lone surrogate: send it JSON-escaped."""
    payload = json.dumps(body(env, dir=folder))
    response = await client.post(
        "/api/browse", content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"].startswith("invalid request: body.dir")
    assert str(env.proj) not in response.text
