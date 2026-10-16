"""Event rows with report summaries, and GET /api/events/{id} (UI refine spec §3.2)."""

from __future__ import annotations

import json

import httpx
import pytest

from diffbell.diffing import FileReport
from diffbell.models import ChangeKind
from tests.web.support import SECRET, Env
from tests.web.test_app import add_event

M = ChangeKind.MODIFIED


def store_report(env: Env, event_id: int, *reports: FileReport, branch: str = "main") -> None:
    env.store.put_event_report(
        event_id, branch, list(reports), telegram_lines=20, telegram_files=len(reports)
    )


async def test_list_rows_carry_branch_and_line_counts(env: Env, client: httpx.AsyncClient) -> None:
    old = add_event(env, ["Makefile"])
    new = add_event(env, ["package.json"])
    store_report(env, new, FileReport("package.json", M, lines=("+a", "+b", "-c")))
    events = {e["id"]: e for e in (await client.get("/api/events")).json()["events"]}
    assert (events[new]["branch"], events[new]["added"], events[new]["removed"]) == ("main", 2, 1)
    assert (events[old]["branch"], events[old]["added"], events[old]["removed"]) == (
        None,
        None,
        None,
    )
    assert events[new]["next_attempt_at"] == env.now


async def test_event_detail_returns_the_stored_report(env: Env, client: httpx.AsyncClient) -> None:
    event_id = add_event(env, ["package.json", ".env.local"])
    store_report(
        env,
        event_id,
        FileReport("package.json", M, lines=('-"v": 1', '+"v": 2', "+<script>x</script>")),
        FileReport(".env.local", M, redacted=True),
        branch="feat/x",
    )
    response = await client.get(f"/api/events/{event_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["id"] == event_id and body["project"] == "proj" and body["status"] == "pending"
    assert (body["branch"], body["added"], body["removed"]) == ("feat/x", 2, 1)
    assert (body["telegram_lines"], body["telegram_files"]) == (20, 2)
    assert body["files"] == [
        {"path": "package.json", "kind": "modified", "redacted": False},
        {"path": ".env.local", "kind": "modified", "redacted": True},
    ]
    package, env_file = body["reports"]
    assert package["lines"] == ['-"v": 1', '+"v": 2', "+<script>x</script>"]
    assert env_file == {
        "path": ".env.local",
        "kind": "modified",
        "redacted": True,
        "note": None,
        "lines": [],
        "truncated": False,
    }


async def test_an_old_event_has_no_reports(env: Env, client: httpx.AsyncClient) -> None:
    event_id = add_event(env, ["package.json"])
    body = (await client.get(f"/api/events/{event_id}")).json()
    assert body["reports"] is None and body["branch"] is None
    assert body["telegram_lines"] is None and body["telegram_files"] is None
    assert body["files"] == [{"path": "package.json", "kind": "modified", "redacted": False}]


async def test_unknown_and_pruned_events_are_404(env: Env, client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/events/9999")).status_code == 404
    event_id = add_event(env, ["package.json"])
    store_report(env, event_id, FileReport("package.json", M, lines=("+a",)))
    env.store.mark_sent(event_id, 1)
    env.store.prune(older_than=env.now + 1)
    response = await client.get(f"/api/events/{event_id}")
    assert response.status_code == 404 and response.json() == {"error": f"unknown event {event_id}"}


@pytest.mark.parametrize("event_id", ["0", "-1", "abc", str(2**63)])
async def test_event_ids_are_bounded(client: httpx.AsyncClient, event_id: str) -> None:
    response = await client.get(f"/api/events/{event_id}")
    assert response.status_code == 400
    assert response.json()["error"].startswith("invalid request")


async def test_a_file_redacted_since_the_event_loses_its_stored_lines(
    env: Env, client: httpx.AsyncClient
) -> None:
    event_id = add_event(env, ["config/app.yaml"])
    report = FileReport(
        "config/app.yaml", M, lines=(f"+key: {SECRET}",), note="symlink \u2192 /abs/target"
    )
    store_report(env, event_id, report)
    env.config.write_text(env.config.read_text() + "redact:\n  - app.yaml\n")
    response = await client.get(f"/api/events/{event_id}")
    body = response.json()
    assert SECRET not in str(body)
    assert "/abs/target" not in response.text  # the stored note named the link target
    assert body["reports"][0]["redacted"] is True and body["reports"][0]["lines"] == []
    assert body["reports"][0]["note"] is None
    assert body["files"][0]["redacted"] is True
    assert (body["added"], body["removed"]) == (0, 0)  # recounted from what is returned
    # The list keeps the stored counts (it never reads the JSON): numbers, no lines.
    listed = (await client.get("/api/events")).json()["events"][0]
    assert (listed["added"], listed["removed"]) == (1, 0)


async def test_the_detail_returns_a_valid_first_line_only(
    env: Env, client: httpx.AsyncClient
) -> None:
    """Plan 7 spec §2.1: a stored first_line >= 1 is returned as it is; a missing,
    odd or now-redacted one is left out (the page then opens the file without a line)."""
    event_id = add_event(env, ["a.txt", "b.txt", "c.txt", "d.txt", "config/app.yaml"])
    store_report(
        env,
        event_id,
        FileReport("a.txt", M, lines=("+a",), first_line=7),
        FileReport("b.txt", M, lines=("+b",)),
        FileReport("c.txt", M, lines=("+c",), first_line=2),
        FileReport("d.txt", M, lines=("+d",), first_line=3),
        FileReport("config/app.yaml", M, lines=("+k: v",), first_line=9),
    )
    text = env.store._db.execute(
        "SELECT reports FROM event_reports WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    entries = json.loads(text)
    entries[2]["first_line"] = "2"  # odd values written by hand: never trusted
    entries[3]["first_line"] = 0
    env.store._db.execute(
        "UPDATE event_reports SET reports = ? WHERE event_id = ?", (json.dumps(entries), event_id)
    )
    env.config.write_text(env.config.read_text() + "redact:\n  - app.yaml\n")
    reports = (await client.get(f"/api/events/{event_id}")).json()["reports"]
    assert [r.get("first_line", "absent") for r in reports] == [
        7,
        "absent",
        "absent",
        "absent",
        "absent",
    ]


async def test_event_detail_needs_a_session(env: Env, anon: httpx.AsyncClient) -> None:
    event_id = add_event(env, ["package.json"])
    assert (await anon.get(f"/api/events/{event_id}")).status_code == 401


# -- search inside stored diffs (diff readability spec §2.2) -------------------------


async def test_the_search_finds_diff_lines_and_flags_them(
    env: Env, client: httpx.AsyncClient
) -> None:
    by_line = add_event(env, ["docker-compose.yml"])
    store_report(env, by_line, FileReport("docker-compose.yml", M, lines=("+image: pg:16",)))
    by_path = add_event(env, ["pg:16.txt"])
    add_event(env, ["other.txt"])
    body = (await client.get("/api/events", params={"q": "pg:16"})).json()
    assert body["total"] == 2
    assert [(e["id"], e["q_in_diff"]) for e in body["events"]] == [
        (by_path, False),
        (by_line, True),
    ]
    plain = (await client.get("/api/events")).json()["events"]
    assert all(e["q_in_diff"] is False for e in plain)


async def test_one_character_searches_paths_only(env: Env, client: httpx.AsyncClient) -> None:
    event_id = add_event(env, ["a.json"])
    store_report(env, event_id, FileReport("a.json", M, lines=("+Q",)))
    body = (await client.get("/api/events", params={"q": "q"})).json()
    assert body == {"events": [], "total": 0}


async def test_the_search_applies_todays_redaction_list(
    env: Env, client: httpx.AsyncClient
) -> None:
    event_id = add_event(env, ["config/app.yaml"])
    store_report(env, event_id, FileReport("config/app.yaml", M, lines=("+token: hunter2",)))
    assert (await client.get("/api/events", params={"q": "hunter2"})).json()["total"] == 1
    env.config.write_text(env.config.read_text() + "redact:\n  - app.yaml\n")
    body = (await client.get("/api/events", params={"q": "hunter2"})).json()
    assert body == {"events": [], "total": 0}
    # Its path is still found, without the flag.
    body = (await client.get("/api/events", params={"q": "app.yaml"})).json()
    assert [(e["id"], e["q_in_diff"]) for e in body["events"]] == [(event_id, False)]


async def test_an_event_of_a_project_no_longer_configured_says_so(
    env: Env, client: httpx.AsyncClient
) -> None:
    """Folder /df-test/proj is removed and another folder is configured as "proj":
    the old event is marked inactive, so the page offers no folder actions for it
    (the same-named folder is another one; stable project ids, Task 2b)."""
    old = add_event(env, ["package.json"])
    env.store.sync_projects([("proj", "/df-test/elsewhere")], env.now)
    new = add_event(env, ["package.json"])
    assert (await client.get(f"/api/events/{old}")).json()["project_active"] is False
    assert (await client.get(f"/api/events/{new}")).json()["project_active"] is True
    rows = {e["id"]: e for e in (await client.get("/api/events")).json()["events"]}
    assert (rows[old]["project_active"], rows[new]["project_active"]) == (False, True)
