"""The notification-control API (notify controls spec §5.1): snooze routes, the
dashboard's snooze/notify_on/quiet fields, held and batch fields on events,
reports_state, and notify_on / quiet hours through the existing config routes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from diffbell.config import load_config, write_default_config
from diffbell.diffing import FileReport
from diffbell.models import ChangeKind
from diffbell.pausing import HELD_FOREVER
from diffbell.store import RETENTION
from diffbell.web import auth, config_edit
from diffbell.web.app import create_app
from tests.web.support import BASE, SECRET, Env, make_context
from tests.web.test_app import add_event

M, D = ChangeKind.MODIFIED, ChangeKind.DELETED
DAY = 24 * 3600.0


# -- snooze routes ---------------------------------------------------------------------


async def test_snooze_until_a_time_and_until_turned_back_on(
    env: Env, client: httpx.AsyncClient
) -> None:
    timed = await client.post("/api/projects/proj/snooze", json={"until": env.now + 3600})
    assert timed.status_code == 200
    assert timed.json() == {"snooze": {"since": env.now, "until": env.now + 3600}}
    forever = await client.post("/api/projects/proj/snooze", json={"until": None})
    assert forever.json() == {"snooze": {"since": env.now, "until": None}}
    snooze = env.store.get_snooze("proj")
    assert snooze is not None and snooze.until is None


@pytest.mark.parametrize(
    ("until", "status", "error"),
    [
        (-1.0, 422, "the snooze must end in the future"),
        (0.0, 422, "the snooze must end in the future"),
        (30 * DAY + 1, 422, "a snooze can last at most 30 days"),
    ],
)
async def test_snooze_end_is_checked(
    env: Env, client: httpx.AsyncClient, until: float, status: int, error: str
) -> None:
    response = await client.post("/api/projects/proj/snooze", json={"until": env.now + until})
    assert (response.status_code, response.json()) == (status, {"error": error})
    assert env.store.get_snooze("proj") is None


async def test_snoozing_a_project_the_daemon_has_not_synced_yet_is_a_404(
    env: Env, client: httpx.AsyncClient
) -> None:
    """The route reads the config file, the Store the daemon's last sync: a project
    added through the web is configured before the daemon's reload lands."""
    fresh = env.home / "fresh"
    fresh.mkdir()
    env.config.write_text(env.config.read_text() + f"  - path: {fresh}\n")
    response = await client.post("/api/projects/fresh/snooze", json={"until": None})
    assert (response.status_code, response.json()) == (404, {"error": "unknown project"})
    assert env.store.get_snooze("fresh") is None


async def test_thirty_days_is_allowed(env: Env, client: httpx.AsyncClient) -> None:
    response = await client.post("/api/projects/proj/snooze", json={"until": env.now + 30 * DAY})
    assert response.status_code == 200


@pytest.mark.parametrize("body", [{}, {"until": "soon"}, {"until": True}, {"until": 1, "x": 1}])
async def test_snooze_body_shape(client: httpx.AsyncClient, body: dict[str, object]) -> None:
    response = await client.post("/api/projects/proj/snooze", json=body)
    assert response.status_code == 400


async def test_snoozing_an_unknown_project_is_404_without_echo(
    env: Env, client: httpx.AsyncClient
) -> None:
    name = "MARKER-not-a-project"
    response = await client.post(f"/api/projects/{name}/snooze", json={"until": None})
    assert (response.status_code, response.json()) == (404, {"error": "unknown project"})
    assert "MARKER" not in response.text and str(env.home) not in response.text


async def test_unsnooze_is_idempotent_and_releases_held_events(
    env: Env, client: httpx.AsyncClient
) -> None:
    await client.post("/api/projects/proj/snooze", json={"until": None})
    event_id = add_event(env, ["package.json"])
    env.store.mark_held(event_id, HELD_FOREVER, "snooze")
    env.now += 60
    first = await client.delete("/api/projects/proj/snooze")
    assert (first.status_code, first.content) == (204, b"")
    assert env.store.get_snooze("proj") is None
    assert [e.id for e in env.store.due_held(env.now)] == [event_id]
    assert (await client.delete("/api/projects/proj/snooze")).status_code == 204


async def test_snooze_routes_need_a_session_and_the_csrf_header(
    env: Env, client: httpx.AsyncClient
) -> None:
    def body(method: str) -> dict[str, None] | None:
        return {"until": None} if method == "POST" else None

    url = "/api/projects/proj/snooze"
    # The `anon` fixture is the same client as `client` once logged in: a fresh one.
    transport = httpx.ASGITransport(app=create_app(make_context(env)))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as anon:
        for method in ("POST", "DELETE"):
            response = await anon.request(method, url, json=body(method))  # no session
            assert response.status_code == 401
    for method in ("POST", "DELETE"):
        foreign = await client.request(
            method, url, json=body(method), headers={"origin": "http://evil.example"}
        )
        assert foreign.status_code == 403
    del client.headers[auth.CSRF_HEADER]
    for method in ("POST", "DELETE"):
        response = await client.request(method, url, json=body(method))
        assert response.status_code == 403
    assert env.store.get_snooze("proj") is None


# -- dashboard -----------------------------------------------------------------------


async def test_dashboard_projects_carry_snooze_and_notify_on(
    env: Env, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/api/dashboard")).json()
    (row,) = body["projects"]
    assert (row["snooze"], row["notify_on"]) == (None, ["created", "modified", "deleted"])
    assert body["quiet"] == {"window": None, "active": False, "ends_at": None}
    env.store.set_snooze("proj", env.now, env.now + 60)
    (row,) = (await client.get("/api/dashboard")).json()["projects"]
    assert row["snooze"] == {"since": env.now, "until": env.now + 60}
    env.now += 60  # expired: no longer shown
    (row,) = (await client.get("/api/dashboard")).json()["projects"]
    assert row["snooze"] is None


async def test_dashboard_notify_on_follows_the_config(env: Env, client: httpx.AsyncClient) -> None:
    env.config.write_text(env.config.read_text() + "    notify_on: [deleted]\n")
    (row,) = (await client.get("/api/dashboard")).json()["projects"]
    assert row["notify_on"] == ["deleted"]


@pytest.mark.parametrize("status", ["missing", "subscribed"])
async def test_a_project_whose_folder_is_missing_keeps_its_notify_on_and_snooze(
    env: Env, client: httpx.AsyncClient, status: str
) -> None:
    """Not resolved (its folder is gone), but configured: its own notify_on, not every
    kind, and its snooze, so the card can still offer Resume. The snapshot may not
    have noticed yet ("subscribed")."""
    env.config.write_text(env.config.read_text() + "    notify_on: [deleted]\n")
    env.store.set_snooze("proj", env.now, None)
    for path in sorted(env.proj.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    env.proj.rmdir()
    env.snapshot["projects"][0]["status"] = status
    body = (await client.get("/api/dashboard")).json()
    rows = body["missing"] if status == "missing" else body["projects"]
    (row,) = [r for r in rows if r["name"] == "proj"]
    assert row["notify_on"] == ["deleted"]
    assert row["snooze"] == {"since": env.now, "until": None}


@pytest.mark.parametrize(
    ("window", "active", "ends"),
    [
        ("13:00-14:00", True, (1970, 1, 12, 14, 0)),  # env.now is 13:46:40 UTC
        ("23:00-13:46", False, None),
        ("13:47-13:00", False, None),
        ("13:46-13:00", True, (1970, 1, 13, 13, 0)),
    ],
)
async def test_dashboard_quiet_hours(
    env: Env,
    client: httpx.AsyncClient,
    utc: None,
    window: str,
    active: bool,
    ends: tuple[int, int, int, int, int] | None,
) -> None:
    env.config.write_text(env.config.read_text() + f'notify:\n  quiet_hours: "{window}"\n')
    body = (await client.get("/api/dashboard")).json()
    expected = None if ends is None else datetime(*ends, tzinfo=UTC).timestamp()
    assert body["quiet"] == {"window": window, "active": active, "ends_at": expected}


# -- events --------------------------------------------------------------------------


async def test_events_filter_held_and_skipped_with_hold_fields(
    env: Env, client: httpx.AsyncClient
) -> None:
    held = add_event(env, ["a"])
    forever = add_event(env, ["b"])
    skipped = add_event(env, ["c"])
    add_event(env, ["d"])
    env.store.mark_held(held, env.now + 600, "quiet")
    env.store.mark_held(forever, HELD_FOREVER, "snooze")
    env.store.mark_skipped([skipped])
    rows = (await client.get("/api/events?status=held")).json()["events"]
    assert [(r["id"], r["held_until"], r["held_reason"], r["batch_id"]) for r in rows] == [
        (forever, None, "snooze", None),  # until turned back on
        (held, env.now + 600, "quiet", None),
    ]
    rows = (await client.get("/api/events?status=skipped")).json()["events"]
    assert [r["id"] for r in rows] == [skipped]
    assert (await client.get("/api/events?status=bogus")).status_code == 400


async def test_a_held_event_already_due_has_no_held_until(
    env: Env, client: httpx.AsyncClient
) -> None:
    """A due hold (its catch-up retrying, or the tick not yet run) is not "held until"
    a time in the past."""
    due = add_event(env, ["a"])
    env.store.mark_held(due, env.now + 600, "quiet")
    env.now += 600
    (row,) = (await client.get("/api/events?status=held")).json()["events"]
    assert (row["id"], row["held_until"]) == (due, None)
    assert (await client.get(f"/api/events/{due}")).json()["held_until"] is None
    env.now -= 1
    (row,) = (await client.get("/api/events?status=held")).json()["events"]
    assert row["held_until"] == env.now + 1


def report(env: Env, event_id: int, *reports: FileReport, skipped: tuple[str, ...] = ()) -> None:
    env.store.put_event_report(
        event_id, "main", list(reports), telegram_lines=0, telegram_files=0, rule_skipped=skipped
    )


async def test_an_event_sent_in_a_catch_up_carries_the_batch(
    env: Env, client: httpx.AsyncClient
) -> None:
    first = add_event(env, ["package.json"])
    second = add_event(env, ["config/app.yaml"])
    report(env, first, FileReport("package.json", M, lines=("+1",)))
    env.store.put_batch_report(
        first,
        [
            FileReport("package.json", M, lines=("+2", "-1")),
            FileReport("config/app.yaml", M, lines=(f"+key: {SECRET}",)),
        ],
        telegram_lines=20,
        telegram_files=2,
    )
    for held in (first, second):  # a catch-up sends held events only
        env.store.mark_held(held, env.now, "quiet")
    env.store.mark_batch_sent([first, second], 7, first)
    env.config.write_text(env.config.read_text() + "redact:\n  - app.yaml\n")  # since then
    body = (await client.get(f"/api/events/{second}")).json()
    assert (body["batch_id"], body["batch_size"]) == (first, 2)
    assert body["reports"] is None and body["reports_state"] == "not_recorded"
    batch = body["batch_report"]
    assert (batch["telegram_lines"], batch["telegram_files"]) == (20, 2)
    assert [r["path"] for r in batch["reports"]] == ["package.json", "config/app.yaml"]
    assert batch["reports"][1]["redacted"] is True and batch["reports"][1]["lines"] == []
    assert (batch["added"], batch["removed"]) == (1, 1)  # recounted after the redaction
    assert SECRET not in str(body)
    alone = (await client.get(f"/api/events/{first}")).json()
    assert alone["reports_state"] == "stored" and alone["batch_report"] is not None


async def test_an_event_outside_a_batch_has_no_batch_fields(
    env: Env, client: httpx.AsyncClient
) -> None:
    event_id = add_event(env, ["package.json"])
    body = (await client.get(f"/api/events/{event_id}")).json()
    assert (body["batch_id"], body["batch_size"], body["batch_report"]) == (None, None, None)
    assert (body["held_until"], body["held_reason"]) == (None, None)


async def test_reports_state_tells_pruned_from_not_recorded(
    env: Env, client: httpx.AsyncClient
) -> None:
    old = add_event(env, ["package.json"])
    report(env, old, FileReport("package.json", M, lines=("+1",)))
    env.store.mark_held(old, HELD_FOREVER, "snooze")
    created = env.now - RETENTION - DAY  # snoozed for 31 days
    env.store._db.execute("UPDATE events SET created_at = ? WHERE id = ?", (created, old))
    env.store.prune(env.now - RETENTION)  # the report goes, the held event stays
    young = add_event(env, ["Makefile"])  # never had a report
    body = (await client.get(f"/api/events/{old}")).json()
    assert (body["status"], body["reports"], body["reports_state"]) == ("held", None, "pruned")
    assert (await client.get(f"/api/events/{young}")).json()["reports_state"] == "not_recorded"


async def test_rule_skipped_files_are_marked_in_the_detail(
    env: Env, client: httpx.AsyncClient
) -> None:
    event_id = add_event(env, ["a.json", "b.json"])
    report(env, event_id, FileReport("a.json", M, lines=("+1",)), FileReport("b.json", D),
           skipped=("b.json",))  # fmt: skip
    body = (await client.get(f"/api/events/{event_id}")).json()
    assert [r.get("rule_skipped") for r in body["reports"]] == [None, True]


# -- notify_on and quiet hours through the config routes ---------------------------------


async def put_project(client: httpx.AsyncClient, env: Env, **fields: object) -> httpx.Response:
    version = config_edit.config_version(env.config)
    body = {"config_version": version, "path": str(env.proj), **fields}
    return await client.put("/api/projects/proj", json=body)


async def test_projects_list_notify_on(env: Env, client: httpx.AsyncClient) -> None:
    (project,) = (await client.get("/api/projects")).json()["projects"]
    assert project["notify_on"] == ["created", "modified", "deleted"]


async def test_saving_notify_on_writes_only_what_differs(
    env: Env, client: httpx.AsyncClient
) -> None:
    assert (await put_project(client, env, notify_on=["created", "deleted"])).status_code == 200
    assert "notify_on: [created, deleted]" in env.config.read_text()
    assert (await put_project(client, env)).status_code == 200  # omitted: unchanged
    assert load_config(env.config).projects[0].notify_on == ["created", "deleted"]
    every = ["deleted", "created", "modified"]
    assert (await put_project(client, env, notify_on=every)).status_code == 200
    assert "notify_on:" not in env.config.read_text()  # the default needs no key


@pytest.mark.parametrize(
    ("notify_on", "status", "error"),
    [
        ([], 422, "notify_on needs at least one kind; use snooze to silence a project"),
        (["created", "created"], 422, "notify_on lists a kind more than once"),
        (["renamed"], 400, "invalid request"),
    ],
)
async def test_invalid_notify_on_is_refused(
    env: Env, client: httpx.AsyncClient, notify_on: list[str], status: int, error: str
) -> None:
    before = env.config.read_bytes()
    response = await put_project(client, env, notify_on=notify_on)
    assert response.status_code == status and error in response.json()["error"]
    assert env.config.read_bytes() == before


def settings_body(version: str, **extra: object) -> dict[str, object]:
    return {
        "config_version": version,
        "debounce": {"quiet_seconds": 10, "max_wait_seconds": 60},
        "diff": {"max_lines": 20},
        "defaults": [],
        "ignore": [],
        "redact": [],
        **extra,
    }


async def test_quiet_hours_round_trip_through_settings(env: Env, client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/settings")).json()
    assert body["notify"] == {"quiet_hours": None, "heartbeat": "09:00"}
    on = await client.put(
        "/api/settings",
        json=settings_body(body["config_version"], notify={"quiet_hours": "23:00-08:00"}),
    )
    assert on.status_code == 200
    assert load_config(env.config).notify.quiet_hours == "23:00-08:00"
    assert (await client.get("/api/settings")).json()["notify"] == {
        "quiet_hours": "23:00-08:00",
        "heartbeat": "09:00",
    }
    kept = await client.put("/api/settings", json=settings_body(on.json()["config_version"]))
    assert load_config(env.config).notify.quiet_hours == "23:00-08:00"  # absent: unchanged
    off = await client.put(
        "/api/settings",
        json=settings_body(kept.json()["config_version"], notify={"quiet_hours": None}),
    )
    assert off.status_code == 200
    assert load_config(env.config).notify.quiet_hours is None


async def test_turning_off_quiet_hours_that_were_never_set_adds_nothing(
    env: Env, client: httpx.AsyncClient
) -> None:
    version = (await client.get("/api/settings")).json()["config_version"]
    body = settings_body(version, notify={"quiet_hours": None})
    assert (await client.put("/api/settings", json=body)).status_code == 200
    assert "notify" not in env.config.read_text()


async def test_invalid_quiet_hours_are_refused(env: Env, client: httpx.AsyncClient) -> None:
    version = (await client.get("/api/settings")).json()["config_version"]
    before = env.config.read_bytes()
    body = settings_body(version, notify={"quiet_hours": "08:00-08:00"})
    response = await client.put("/api/settings", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "quiet_to: Must differ from the start time."
    assert response.json()["errors"] == {"quiet_to": "Must differ from the start time."}
    assert env.config.read_bytes() == before


def test_quiet_hours_keep_the_comment_of_the_default_config(tmp_dir: Path) -> None:
    path = tmp_dir / "config.yaml"
    write_default_config(path)
    settings = config_edit.SettingsInput(
        10.0, 60.0, 20, [], [], [], config_edit.NotifyInput("22:30-07:00")
    )
    config_edit.update_settings(path, config_edit.config_version(path), settings)
    assert '  quiet_hours: 22:30-07:00 # e.g. "23:00-08:00"' in path.read_text()
