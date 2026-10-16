"""The dashboard's activity strip data and folders, and the attention filter
(Plan 7 spec §2.2-§2.4). The agreement test: every bucket counts exactly the
events the feed shows for its since/until."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from diffbell.web.app import create_app
from tests.web.support import BASE, Env, log_in, make_context
from tests.web.test_app import add_event

BERLIN = ZoneInfo("Europe/Berlin")


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=BERLIN).timestamp()


NOW = at("2026-10-27 12:00")  # Sunday 25 Oct 2026 is the 25-hour day in Berlin


@pytest.fixture
async def berlin(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    """A logged-in client of an app whose local time is Berlin's."""
    env.now = NOW
    ctx = dataclasses.replace(make_context(env), tz=BERLIN)
    transport = httpx.ASGITransport(app=create_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await log_in(env, client)
        yield client


def event_at(env: Env, when: float, project: str = "proj", status: str = "pending") -> int:
    now, env.now = env.now, when
    try:
        event_id = add_event(env, ["package.json"], project=project)
    finally:
        env.now = now
    if status == "failed":
        env.store.mark_failed(event_id, "boom", kind="bad_request", at=0.0)
    elif status == "held":
        env.store.mark_held(event_id, when + 3600, "quiet")
    elif status == "sent":
        env.store.mark_sent(event_id, 1)
    return event_id


async def activity(client: httpx.AsyncClient) -> dict[str, list[dict[str, Any]]]:
    return dict((await client.get("/api/dashboard")).json()["activity"])


async def test_the_dashboard_has_14_days_per_configured_project(
    env: Env, berlin: httpx.AsyncClient
) -> None:
    event_at(env, at("2026-10-27 09:00"))
    event_at(env, at("2026-10-26 09:00"), status="failed")
    event_at(env, at("2026-10-27 09:00"), project="removed")  # no longer configured
    days = await activity(berlin)
    assert list(days) == ["proj"]  # "removed" has events but no config entry
    [*_, monday, today] = days["proj"]
    assert len(days["proj"]) == 14 and days["proj"][0]["day"] == "2026-10-14"
    assert today == {
        "day": "2026-10-27",
        "since": at("2026-10-27 00:00"),
        "until": at("2026-10-28 00:00"),
        "total": 1,
        "failed": 0,
    }
    assert (monday["day"], monday["total"], monday["failed"]) == ("2026-10-26", 1, 1)
    sunday = days["proj"][-3]
    assert sunday["until"] - sunday["since"] == 25 * 3600


async def test_every_bucket_agrees_with_the_feed_total(env: Env, berlin: httpx.AsyncClient) -> None:
    """For each bucket: `total` is the feed's `total` for its since/until (not the
    rows of one page: the feed pages at 50), and `failed` that of status=failed."""
    for i in range(60):  # more than a page on the 25-hour day, its last hour included
        event_at(env, at("2026-10-25 00:00") + i * 1490, status="failed" if i % 7 == 0 else "sent")
    event_at(env, at("2026-10-25 23:59:59"))
    event_at(env, at("2026-10-26 00:00"), status="held")
    event_at(env, at("2026-10-14 00:00"))
    event_at(env, at("2026-10-13 23:59:59"))  # before the window: in no bucket
    buckets = (await activity(berlin))["proj"]
    assert max(b["total"] for b in buckets) > 50
    for b in buckets:
        window = {"project": "proj", "since": str(b["since"]), "until": str(b["until"])}
        listed = (await berlin.get("/api/events", params=window)).json()
        assert b["total"] == listed["total"], b["day"]
        failed = (await berlin.get("/api/events", params={**window, "status": "failed"})).json()
        assert b["failed"] == failed["total"], b["day"]
    assert sum(b["total"] for b in buckets) == 63  # the 13th is outside


async def test_projects_carry_their_absolute_folder(env: Env, client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/dashboard")).json()
    [proj] = body["projects"]
    assert proj["folder"] == os.path.realpath(env.proj)


async def test_a_project_without_a_resolved_folder_has_none(
    env: Env, client: httpx.AsyncClient
) -> None:
    env.snapshot["projects"].insert(0, {"name": "ghost", "path": "/x", "status": "pending"})
    body = (await client.get("/api/dashboard")).json()
    assert {p["name"]: p["folder"] for p in body["projects"]} == {
        "ghost": None,
        "proj": os.path.realpath(env.proj),
    }


# -- needs attention (spec §2.3) -------------------------------------------------------


async def test_attention_is_failed_and_held_with_totals_and_paging(
    env: Env, client: httpx.AsyncClient
) -> None:
    ids = {
        status: event_at(env, env.now + n, status=status)
        for n, status in enumerate(("pending", "sent", "failed", "held"))
    }
    body = (await client.get("/api/events", params={"status": "attention"})).json()
    assert body["total"] == 2
    assert [e["id"] for e in body["events"]] == [ids["held"], ids["failed"]]
    paged = (
        await client.get("/api/events", params={"status": "attention", "limit": 1, "offset": 1})
    ).json()
    assert paged["total"] == 2 and [e["id"] for e in paged["events"]] == [ids["failed"]]
    other = event_at(env, env.now + 9, project="other", status="failed")
    mine = (
        await client.get("/api/events", params={"status": "attention", "project": "proj"})
    ).json()
    assert mine["total"] == 2 and other not in [e["id"] for e in mine["events"]]
