"""Stable project ids (stable project ids spec §2, §4): a project is its folder, and
Store.sync_projects keeps each folder's id while its name changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.store import ClockState, ProjectPathTaken, Store, UnknownProject

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    return Store(tmp_dir / "state.db")


def rows(store: Store) -> list[tuple[int, str | None, str, int]]:
    return [
        (int(i), path, str(name), int(active))
        for i, path, name, active in store._db.execute(
            "SELECT id, path, name, active FROM projects ORDER BY id"
        )
    ]


def add_event(store: Store, project: str, path: str = "package.json", now: float = 10.0) -> int:
    obs = Observation(upserts={path: f"sha-{path}"}, changes=[(path, ChangeKind.MODIFIED)])
    changes = store.record_observation(project, STATE, obs, now=now)
    event_id = store.create_event(project, changes, [], now=now)
    assert event_id is not None
    return event_id


def test_sync_inserts_one_active_row_per_folder(store: Store) -> None:
    store.sync_projects([("a", "/p/a"), ("b", "/p/b")], now=5.0)
    assert rows(store) == [(1, "/p/a", "a", 1), (2, "/p/b", "b", 1)]
    created = store._db.execute("SELECT created_at FROM projects").fetchall()
    assert created == [(5.0,), (5.0,)]
    assert store._ids == {"a": 1, "b": 2}


def test_a_rename_keeps_the_id_the_clock_the_hashes_and_the_snooze(store: Store) -> None:
    store.sync_projects([("old", "/p/a")], now=1.0)
    add_event(store, "old")
    store.set_snooze("old", 1.0, None)
    store.sync_projects([("new", "/p/a")], now=2.0)
    assert rows(store) == [(1, "/p/a", "new", 1)]
    assert store.get_clock("new") == STATE
    assert store.get_hashes("new") == {"package.json": "sha-package.json"}
    assert store.get_snooze("new") is not None
    assert store.drop_snoozes_except(["new"], now=3.0) == []  # renamed, not removed
    assert store.get_clock("old") is None  # the old name is nothing any more


def test_two_folders_can_swap_names_in_one_sync(store: Store) -> None:
    store.sync_projects([("x", "/p/a"), ("y", "/p/b")], now=1.0)
    store.sync_projects([("y", "/p/a"), ("x", "/p/b")], now=2.0)
    assert rows(store) == [(1, "/p/a", "y", 1), (2, "/p/b", "x", 1)]
    assert store._ids == {"y": 1, "x": 2}


def test_a_removed_project_is_deactivated_and_comes_back_with_its_id(store: Store) -> None:
    store.sync_projects([("a", "/p/a"), ("b", "/p/b")], now=1.0)
    event_id = add_event(store, "b")
    store.sync_projects([("a", "/p/a")], now=2.0)
    assert rows(store) == [(1, "/p/a", "a", 1), (2, "/p/b", "b", 0)]
    assert store.get_event(event_id).project == "b"  # its last name, in the feed
    store.sync_projects([("a", "/p/a"), ("b2", "/p/b")], now=3.0)
    assert rows(store) == [(1, "/p/a", "a", 1), (2, "/p/b", "b2", 1)]
    assert [r.event.id for r in store.query_events(project="b2")[0]] == [event_id]


def test_a_name_reused_by_another_folder_gets_that_folders_history(store: Store) -> None:
    store.sync_projects([("app", "/p/old")], now=1.0)
    old = add_event(store, "app")
    store.sync_projects([("app", "/p/new")], now=2.0)
    assert rows(store) == [(1, "/p/old", "app", 0), (2, "/p/new", "app", 1)]
    assert store.query_events(project="app") == ([], 0)
    assert store.get_clock("app") is None  # a new folder: a fresh baseline
    assert store.get_event(old).project == "app"  # still listed under "all projects"


def test_writes_for_an_unknown_name_raise(store: Store) -> None:
    store.sync_projects([("a", "/p/a")], now=1.0)
    obs = Observation(changes=[("x", ChangeKind.CREATED)])
    with pytest.raises(UnknownProject, match="nope"):
        store.record_observation("nope", STATE, obs, now=1.0)
    with pytest.raises(UnknownProject):
        store.replace_baseline("nope", STATE, {}, now=1.0)
    with pytest.raises(UnknownProject):
        store.set_snooze("nope", 1.0, None)
    assert store._db.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0


def test_reads_for_an_unknown_name_return_nothing(store: Store) -> None:
    store.sync_projects([("a", "/p/a")], now=1.0)
    add_event(store, "a")
    store.set_snooze("a", 1.0, None)
    assert store.get_clock("nope") is None
    assert store.get_hashes("nope") == {}
    assert store.get_snooze("nope") is None
    assert store.query_events(project="nope") == ([], 0)
    assert store.release_held(5.0, project="nope") == 0
    store.delete_snooze("nope", now=5.0)  # idempotent, as the web's unsnooze
    assert store.get_snooze("a") is not None


def test_a_store_knows_no_project_before_its_first_sync(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    store.sync_projects([("a", "/p/a")], now=1.0)
    store.close()
    reopened = Store(tmp_dir / "state.db")
    with pytest.raises(UnknownProject):
        reopened.set_snooze("a", 1.0, None)
    reopened.sync_projects([("a", "/p/a")], now=2.0)
    assert reopened.set_snooze("a", 1.0, None).project == "a"
    reopened.close()


def test_the_feed_shows_a_past_event_under_the_current_name(store: Store) -> None:
    store.sync_projects([("diffbell", "/p/dw")], now=1.0)
    event_id = add_event(store, "diffbell")
    store.sync_projects([("Diffbell", "/p/dw")], now=2.0)
    [row], total = store.query_events()
    assert (total, row.event.id, row.event.project) == (1, event_id, "Diffbell")
    assert [c.project for c in row.changes] == ["Diffbell"]
    assert [r.event.id for r in store.query_events(project="Diffbell")[0]] == [event_id]
    assert store.last_event_times() == {"Diffbell": 10.0}


def test_a_batch_queued_before_a_rename_is_recorded_under_the_new_name(store: Store) -> None:
    """The debouncer may hold changes under the old name across a reload: the event
    takes its journal rows' project, so nothing is lost."""
    store.sync_projects([("old", "/p/a")], now=1.0)
    changes = store.record_observation(
        "old", STATE, Observation(changes=[("a", ChangeKind.CREATED)]), now=1.0
    )
    store.sync_projects([("new", "/p/a")], now=2.0)
    assert [c.project for c in store.unflushed_changes()] == ["new"]
    event_id = store.create_event("old", changes, [], now=3.0)
    assert event_id is not None and store.get_event(event_id).project == "new"


def test_dashboard_reads_are_keyed_by_configured_name(store: Store) -> None:
    store.sync_projects([("a", "/p/a"), ("b", "/p/b")], now=1.0)
    add_event(store, "a", now=10.0)
    add_event(store, "b", now=20.0)
    store.set_snooze("a", 1.0, None)
    store.set_snooze("b", 1.0, None)
    store.sync_projects([("a", "/p/a")], now=2.0)  # b removed (its snooze not yet dropped)
    assert store.last_event_times() == {"a": 10.0}
    assert set(store.active_snoozes(5.0)) == {"a"}
    assert set(store.activity(15.0, ["a", "b"], days=1)) == {"a", "b"}


def same_name_rows(store: Store) -> list[Change]:
    """One unflushed row of folder /p/a (no longer configured) and one of /p/b, both
    named "x" now, for the same file (stable project ids plan, Task 2b F)."""
    store.sync_projects([("x", "/p/a")], now=1.0)
    obs = Observation(changes=[("f", ChangeKind.MODIFIED)])
    store.record_observation("x", STATE, obs, now=1.0)
    store.sync_projects([("x", "/p/b")], now=2.0)
    store.record_observation("x", STATE, obs, now=2.0)
    changes = store.unflushed_changes()
    assert [c.project for c in changes] == ["x", "x"]
    return changes


def test_create_event_never_mixes_two_projects_rows(store: Store) -> None:
    changes = same_name_rows(store)
    with pytest.raises(ValueError, match="more than one project"):
        store.create_event("x", changes, [], now=3.0)
    assert store.status_counts() == {"pending": 0, "sent": 0, "failed": 0}
    assert store.unflushed_changes() == changes


def test_split_by_project_groups_rows_by_folder_in_order(store: Store) -> None:
    a, b = same_name_rows(store)
    ghost = Change(999, "x", "g", ChangeKind.CREATED)  # gone: create_event reports it
    assert store.split_by_project([a, b, ghost]) == [[a, ghost], [b]]
    assert store.split_by_project([]) == []


def test_a_write_with_a_stale_project_token_raises(store: Store) -> None:
    """Task 2b follow-up: the watcher's writes carry the id it subscribed with. After
    a swap the name names the other folder: a stale token is refused, nothing written."""
    store.sync_projects([("a", "/p/a"), ("b", "/p/b")], now=1.0)
    token_a, token_b = store.project_token("a"), store.project_token("b")
    assert token_a != token_b
    store.sync_projects([("b", "/p/a"), ("a", "/p/b")], now=2.0)  # swapped
    obs = Observation(upserts={"f": "sha"}, changes=[("f", ChangeKind.MODIFIED)])
    with pytest.raises(UnknownProject):
        store.record_observation("a", STATE, obs, now=3.0, expect=token_a)
    with pytest.raises(UnknownProject):
        store.replace_baseline("a", STATE, {"f": "sha"}, now=3.0, expect=token_a)
    assert store.get_hashes("a") == {} and store.get_clock("a") is None
    assert store.unflushed_changes() == []
    assert store.project_token("a") == token_b
    store.record_observation("a", STATE, obs, now=3.0, expect=token_b)
    store.replace_baseline("b", STATE, {"g": "sha"}, now=3.0, expect=token_a)
    assert store.get_hashes("a") == {"f": "sha"} and store.get_hashes("b") == {"g": "sha"}
    with pytest.raises(UnknownProject):
        store.project_token("gone")


def store_with(tmp_dir: Path, *pairs: tuple[str, str]) -> Store:
    store = Store(tmp_dir / "state.db")
    store.sync_projects(list(pairs), 1.0)
    return store


def clock_rows(store: Store, pid: int) -> int:
    return int(
        store._db.execute("SELECT COUNT(*) FROM clocks WHERE project_id = ?", (pid,)).fetchone()[0]
    )


def test_a_moved_folder_keeps_its_id_and_drops_its_clock(tmp_dir: Path) -> None:
    store = store_with(tmp_dir, ("dw", "/p/old"))
    pid = store.project_token("dw")
    store._db.execute(
        "INSERT INTO clocks (project_id, watch_root, relative_root, clock, expr_hash, updated_at)"
        " VALUES (?, '/p/old', '', 'c:1', 'h', 1.0)",
        (pid,),
    )
    assert store.move_project_path("/p/old", "/p/new") == pid
    row = store._db.execute("SELECT path FROM projects WHERE id = ?", (pid,)).fetchone()
    assert row[0] == "/p/new"
    assert clock_rows(store, pid) == 0


def test_a_second_move_is_already_done(tmp_dir: Path) -> None:
    store = store_with(tmp_dir, ("dw", "/p/old"))
    pid = store.move_project_path("/p/old", "/p/new")
    assert store.move_project_path("/p/old", "/p/new") == pid


def test_another_project_at_the_new_path_is_never_merged(tmp_dir: Path) -> None:
    store = store_with(tmp_dir, ("a", "/p/old"), ("b", "/p/new"))
    with pytest.raises(ProjectPathTaken):
        store.move_project_path("/p/old", "/p/new")


def test_nothing_to_move(tmp_dir: Path) -> None:
    assert store_with(tmp_dir).move_project_path("/p/old", "/p/new") is None
