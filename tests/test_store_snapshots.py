"""The Store side of content snapshots (content snapshots spec §1, §2, §5):
the tables, create_event's contents in one transaction, the budgets and eviction,
the baseline's snapshots, housekeeping, retention and the downgrade guard."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from diffbell import store as store_mod
from diffbell.diffing import FileReport
from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.snapshots import Captured, StoredContent
from diffbell.store import SCHEMA_VERSION, SNAPSHOTS_SEEN, ClockState, Store
from tests.helpers import sync_names

STATE = ClockState("/w", "", "c:1", "h")
M, C, D = ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED
MiB = 1024 * 1024


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "proj", "other")
    return store


def sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def journal(store: Store, *paths: str, kind: ChangeKind = M, project: str = "proj") -> list[Change]:
    obs = Observation(changes=[(p, kind) for p in paths])
    return store.record_observation(project, STATE, obs, now=1.0)


def event(
    store: Store,
    contents: dict[str, bytes | None],
    *,
    now: float = 10.0,
    branch: str | None = "main",
    project: str = "proj",
) -> int:
    """One event with these afters: bytes = "content", None = "deleted"."""
    kept = journal(store, *contents, project=project)
    caps = [
        Captured(p, "deleted") if data is None else Captured(p, "content", data, "uncommitted")
        for p, data in contents.items()
    ]
    event_id = store.create_event(project, kept, [], now, contents=caps, branch=branch)
    assert event_id is not None
    return event_id


def total_contents(store: Store) -> int:
    row = store._db.execute(
        "SELECT COALESCE(SUM(COALESCE(length(before), 0) + COALESCE(length(after), 0)), 0) "
        "FROM event_contents"
    ).fetchone()
    return int(row[0])


# -- tables ----------------------------------------------------------------------------


def test_the_tables_are_created_on_open_without_a_version_bump(store: Store) -> None:
    tables = {
        r[0] for r in store._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"snapshots", "event_contents", "event_meta"} <= tables
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7


# -- create_event ------------------------------------------------------------------------


def test_the_first_event_has_no_before_and_the_next_one_chains(store: Store) -> None:
    e1 = event(store, {"package.json": b"v1\n"})
    assert store.event_contents(e1) == {
        "package.json": StoredContent("package.json", None, b"v1\n", "content", "uncommitted")
    }
    assert store.get_snapshots("proj") == {"package.json": b"v1\n"}
    e2 = event(store, {"package.json": b"v2\n"}, now=20.0)
    row = store.event_contents(e2)["package.json"]
    assert (row.before, row.after) == (b"v1\n", b"v2\n")
    meta = store.event_meta(e2)
    assert meta is not None and meta.branch == "main"


def test_deleted_and_uncapturable_delete_the_snapshot(store: Store) -> None:
    event(store, {"a": b"1\n", "b": b"2\n"})
    kept = journal(store, "a", "b")
    caps = [Captured("a", "deleted"), Captured("b", "uncapturable")]
    e2 = store.create_event("proj", kept, [], 20.0, contents=caps, branch=None)
    assert e2 is not None
    rows = store.event_contents(e2)
    assert (rows["a"].before, rows["a"].after, rows["a"].after_state) == (b"1\n", None, "deleted")
    assert (rows["b"].before, rows["b"].after, rows["b"].after_state) == (
        b"2\n",
        None,
        "uncapturable",
    )
    assert store.get_snapshots("proj") == {}
    meta = store.event_meta(e2)
    assert meta is not None and meta.branch is None


def test_a_path_without_a_capture_stores_nothing_and_drops_its_snapshot(store: Store) -> None:
    """A redacted path (never read) or a project without a folder: no row, and no
    stale "before" left for a later event."""
    event(store, {"a": b"1\n"})
    kept = journal(store, "a")
    e2 = store.create_event("proj", kept, [], 20.0, contents=[], branch=None)
    assert e2 is not None
    assert store.event_contents(e2) == {}
    assert store.get_snapshots("proj") == {}


def test_without_contents_nothing_is_tracked(store: Store) -> None:
    event(store, {"a": b"1\n"})
    e2 = store.create_event("proj", journal(store, "a"), [], 20.0)
    assert e2 is not None
    assert store.event_contents(e2) == {} and store.event_meta(e2) is None
    assert store.get_snapshots("proj") == {"a": b"1\n"}


def test_create_event_with_contents_is_atomic(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    event(store, {"a": b"1\n"})
    kept = journal(store, "a")

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "_evict_contents", broken)
    with pytest.raises(sqlite3.OperationalError):
        store.create_event("proj", kept, [], 20.0, contents=[Captured("a", "content", b"2\n")])
    assert [c.id for c in store.unflushed_changes()] == [kept[0].id]
    assert len(store.recent_events()) == 1
    assert store.get_snapshots("proj") == {"a": b"1\n"}
    # The contents, the meta and the downgrade mark rolled back with it.
    assert store._db.execute("SELECT COUNT(*) FROM event_contents").fetchone()[0] == 1
    assert store._db.execute("SELECT COUNT(*) FROM event_meta").fetchone()[0] == 1
    assert store.kv_get(SNAPSHOTS_SEEN) == "10.0"


def test_the_event_budget_fills_in_path_order(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "EVENT_CONTENT_BUDGET", 10)
    e = event(store, {"c": b"333\n", "a": b"1111\n", "b": b"22222\n"})
    rows = store.event_contents(e)
    assert [(p, rows[p].after_state) for p in sorted(rows)] == [
        ("a", "content"),
        ("b", "uncapturable"),  # 5 + 6 > 10
        ("c", "content"),  # 5 + 4 fits
    ]
    assert rows["b"].before is None and rows["b"].after is None
    assert store.get_snapshots("proj") == {"a": b"1111\n", "c": b"333\n"}


def test_a_3_mb_lockfile_is_captured_and_two_fit_in_one_event(store: Store) -> None:
    lock = b"x" * (3 * MiB)
    e = event(store, {"yarn.lock": lock, "pnpm-lock.yaml": lock})
    rows = store.event_contents(e)
    assert {p: r.after_state for p, r in rows.items()} == {
        "yarn.lock": "content",
        "pnpm-lock.yaml": "content",
    }
    e2 = event(store, {"yarn.lock": lock + b"y", "pnpm-lock.yaml": lock + b"y"}, now=20.0)
    assert {r.after_state for r in store.event_contents(e2).values()} == {"content"}


# -- snapshot eviction -------------------------------------------------------------------


def test_a_churning_lockfile_evicts_the_oldest_static_snapshots(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "PROJECT_SNAPSHOT_BUDGET", 30)
    store.replace_baseline(
        "proj",
        STATE,
        {p: sha(b"static!!!\n") for p in ("a.json", "b.json", "c.json")},
        1.0,
        snapshots={p: b"static!!!\n" for p in ("a.json", "b.json", "c.json")},
    )
    assert len(store.get_snapshots("proj")) == 3  # 30 bytes: the budget is full
    for i in range(3):
        event(store, {"z.lock": b"lock %d...\n" % i}, now=10.0 + i)
        snaps = store.get_snapshots("proj")
        assert snaps["z.lock"] == b"lock %d...\n" % i  # never starved
    # Equal captured_at (one baseline transaction): ties broken by path.
    assert sorted(store.get_snapshots("proj")) == ["b.json", "c.json", "z.lock"]


def test_the_current_events_own_paths_are_never_evicted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "PROJECT_SNAPSHOT_BUDGET", 10)
    event(store, {"a": b"aaaa\n", "b": b"bbbb\n"}, now=1.0)
    e = event(store, {"a": b"AAAA\n", "b": b"BBBB\n", "c": b"cc\n"}, now=2.0)
    rows = store.event_contents(e)
    assert (rows["a"].before, rows["b"].before) == (b"aaaa\n", b"bbbb\n")
    # Over the budget with only this event's paths: none of them is evicted.
    assert sorted(store.get_snapshots("proj")) == ["a", "b", "c"]


# -- the event_contents cap ----------------------------------------------------------------


def test_the_contents_cap_evicts_settled_events_first_and_keeps_their_reports(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "PROJECT_EVENT_CONTENT_BUDGET", 40)
    held = event(store, {"h": b"held....\n"}, now=1.0)  # 9 bytes
    store.mark_held(held, 99.0, "quiet")
    failed = event(store, {"f": b"failed..\n"}, now=2.0)
    store.mark_failed(failed, "boom", kind="bad_request", at=0.0)
    sent = event(store, {"s": b"sent....\n"}, now=3.0)
    store.mark_sent(sent, 1)
    store.put_event_report(sent, "main", [FileReport("s", M)], telegram_lines=0, telegram_files=1)
    skipped = event(store, {"k": b"skipped.\n"}, now=4.0)
    store.mark_skipped([skipped])
    assert total_contents(store) == 36
    new = event(store, {"n": b"new.....\n"}, now=5.0)  # 45 > 40: one must go
    assert store.event_contents(sent) == {}  # the oldest settled one
    assert store.get_event_report(sent) is not None  # its stored diff stays
    assert store.event_contents(skipped) != {}
    newer = event(store, {"m": b"newer...\n"}, now=6.0)
    assert store.event_contents(skipped) == {}  # then the next settled one
    assert store.event_contents(held) != {} and store.event_contents(failed) != {}
    store.mark_sent(new, 2)
    store.mark_sent(newer, 3)
    store.mark_held(event(store, {"o": b"other...\n"}, now=7.0), 99.0, "quiet")
    assert store.event_contents(new) == {}  # settled before any waiting one
    assert store.event_contents(held) != {} and store.event_contents(failed) != {}


def test_waiting_events_go_oldest_first_when_nothing_settled_is_left(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "PROJECT_EVENT_CONTENT_BUDGET", 20)
    first = event(store, {"a": b"first...\n"}, now=1.0)
    second = event(store, {"b": b"second..\n"}, now=2.0)
    third = event(store, {"c": b"third...\n"}, now=3.0)
    assert store.event_contents(first) == {}
    assert store.event_contents(second) != {} and store.event_contents(third) != {}


def test_the_cap_is_per_project(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "PROJECT_EVENT_CONTENT_BUDGET", 10)
    mine = event(store, {"a": b"12345678\n"}, now=1.0)
    event(store, {"b": b"12345678\n"}, now=2.0, project="other")
    assert store.event_contents(mine) != {}


# -- the baseline ----------------------------------------------------------------------------


def test_replace_baseline_deletes_old_snapshots_first(store: Store) -> None:
    event(store, {"gone.json": b"old\n"})
    store.replace_baseline(
        "proj", STATE, {"a.json": sha(b"a\n")}, 2.0, snapshots={"a.json": b"a\n"}
    )
    assert store.get_snapshots("proj") == {"a.json": b"a\n"}
    store.replace_baseline("proj", STATE, {}, 3.0)  # no reader: still no stale snapshot
    assert store.get_snapshots("proj") == {}


def test_a_file_that_changed_during_the_read_gets_no_snapshot(store: Store) -> None:
    store.replace_baseline(
        "proj",
        STATE,
        {"a.json": sha(b"a\n"), "b.json": sha(b"b\n")},
        2.0,
        snapshots={"a.json": b"a\n", "b.json": b"b changed meanwhile\n", "c.json": b"c\n"},
    )
    assert store.get_snapshots("proj") == {"a.json": b"a\n"}


def test_the_baseline_fills_in_path_order_without_eviction(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "PROJECT_SNAPSHOT_BUDGET", 10)
    files = {"a": b"aaaa\n", "b": b"bbbbbbb\n", "c": b"cc\n"}
    store.replace_baseline(
        "proj", STATE, {p: sha(d) for p, d in files.items()}, 2.0, snapshots=files
    )
    assert store.get_snapshots("proj") == {"a": b"aaaa\n", "c": b"cc\n"}


# -- housekeeping --------------------------------------------------------------------------


def test_a_removed_projects_snapshots_go_after_the_sync_and_a_rename_keeps_them(
    store: Store,
) -> None:
    event(store, {"a": b"1\n"})
    event(store, {"b": b"2\n"}, project="other")
    store.sync_projects([("renamed", "/df-test/proj")], 5.0)  # "other" removed
    assert store.get_snapshots("renamed") == {"a": b"1\n"}
    rows = store._db.execute("SELECT path FROM snapshots").fetchall()
    assert rows == [("a",)]


def test_a_redaction_change_deletes_covered_contents(store: Store) -> None:
    e = event(store, {"config/app.json": b"x\n", "keep.json": b"k\n"})
    assert store.forget_redacted(("*app*",)) == 2  # one snapshot, one event_contents row
    assert store.get_snapshots("proj") == {"keep.json": b"k\n"}
    assert set(store.event_contents(e)) == {"keep.json"}


def test_prune_removes_contents_and_meta_with_the_reports(store: Store) -> None:
    old = event(store, {"a": b"1\n"}, now=10.0)
    new = event(store, {"a": b"2\n"}, now=100.0)
    store.mark_held(old, 1e12, "snooze")  # kept by prune, its contents are not
    store.prune(50.0)
    assert store.event_contents(old) == {} and store.event_meta(old) is None
    assert store.event_contents(new) != {} and store.event_meta(new) is not None


def test_owner_names_the_rows_project_and_whether_it_is_configured(store: Store) -> None:
    kept = journal(store, "a")
    assert store.owner(kept) == ("proj", True)
    store.sync_projects([("other", "/df-test/other")], 5.0)
    assert store.owner(kept) == ("proj", False)
    assert store.owner([Change(999, "proj", "x", M)]) is None


# -- the downgrade guard ----------------------------------------------------------------------


def test_fresh_creation_writes_keep_the_guard_quiet_without_maintenance(store: Store) -> None:
    """A dead maintenance tick never fires the guard while events keep being created."""
    store.mark_snapshots_seen(0.0)
    store.record_observation("proj", STATE, Observation(), now=1000.0)
    event(store, {"a": b"1\n"}, now=1001.0)  # marks inside its transaction
    assert store.kv_get(SNAPSHOTS_SEEN) == "1001.0"
    assert store.drop_stale_snapshots(1002.0) == 0
    assert store.get_snapshots("proj") == {"a": b"1\n"}


def test_a_clock_advanced_long_after_the_last_snapshot_write_drops_them(store: Store) -> None:
    event(store, {"a": b"1\n"}, now=1000.0)
    # Older code ran: it advanced the clock without touching the snapshots.
    store.record_observation("proj", STATE, Observation(), now=1000.0 + 601.0)
    assert store.drop_stale_snapshots(2000.0) == 1
    assert store.get_snapshots("proj") == {}
    assert store.kv_get(SNAPSHOTS_SEEN) == "2000.0"


def test_a_gap_within_ten_minutes_keeps_the_snapshots(store: Store) -> None:
    event(store, {"a": b"1\n"}, now=1000.0)
    store.record_observation("proj", STATE, Observation(), now=1000.0 + 599.0)
    assert store.drop_stale_snapshots(2000.0) == 0
    assert store.get_snapshots("proj") == {"a": b"1\n"}


# -- the baseline never regresses a pending or concurrent snapshot (may fall back once) ----------


def test_the_baseline_stores_no_snapshot_for_a_pending_path(store: Store) -> None:
    """A row for a.json waits in the debouncer (or was crash-recovered) when a
    rebaseline reads a.json's NEW bytes: storing them as the "before" would make the
    flush diff A1 against A1. The pending path gets none and falls back once."""
    pending = journal(store, "a.json")
    a1, b = b"a new\n", b"b\n"
    store.replace_baseline(
        "proj",
        STATE,
        {"a.json": sha(a1), "b.json": sha(b)},
        2.0,
        snapshots={"a.json": a1, "b.json": b},
        snapshots_epoch=store.snapshot_epoch("proj"),
    )
    assert store.get_snapshots("proj") == {"b.json": b}
    caps = [Captured("a.json", "content", a1, "uncommitted")]
    event_id = store.create_event("proj", pending, [], 3.0, contents=caps, branch="main")
    assert event_id is not None
    row = store.event_contents(event_id)["a.json"]
    assert (row.before, row.after) == (None, a1)  # a fallback, never identical sides


def test_without_an_epoch_the_pending_paths_are_still_excluded(store: Store) -> None:
    journal(store, "a.json")
    store.replace_baseline(
        "proj",
        STATE,
        {"a.json": sha(b"a\n"), "b.json": sha(b"b\n")},
        2.0,
        snapshots={"a.json": b"a\n", "b.json": b"b\n"},
    )
    assert store.get_snapshots("proj") == {"b.json": b"b\n"}


def test_a_snapshot_written_during_the_baseline_read_is_never_regressed(store: Store) -> None:
    """The baseline read a.json's older bytes; meanwhile a flush advanced its snapshot.
    The baseline then inserts nothing: an older "before" would repeat the diff."""
    a0, a1 = b"a old\n", b"a new\n"
    epoch = store.snapshot_epoch("proj")
    event(store, {"a.json": a1}, now=5.0)  # the flush, while the read is in progress
    assert store.snapshot_epoch("proj") != epoch
    store.replace_baseline(
        "proj",
        STATE,
        {"a.json": sha(a0), "b.json": sha(b"b\n")},
        6.0,
        snapshots={"a.json": a0, "b.json": b"b\n"},
        snapshots_epoch=epoch,
    )
    assert store.get_snapshots("proj") == {}


def test_every_snapshot_writer_but_the_baseline_moves_the_epoch(store: Store) -> None:
    assert store.snapshot_epoch("nope") == 0  # not configured: 0, like other reads
    epochs = [store.snapshot_epoch("proj")]
    event(store, {"a.json": b"a\n", "config/app.json": b"x\n"})
    epochs.append(store.snapshot_epoch("proj"))
    store.replace_baseline("proj", STATE, {}, 2.0, snapshots={})
    assert store.snapshot_epoch("proj") == epochs[-1]  # the baseline itself doesn't
    store.forget_redacted(("*app*",))
    epochs.append(store.snapshot_epoch("proj"))
    store.drop_stale_snapshots(3.0)  # nothing to drop: the epoch stays
    assert store.snapshot_epoch("proj") == epochs[-1]
    store.record_observation("proj", STATE, Observation(), now=5000.0)
    store.drop_stale_snapshots(5000.0)  # a gap: every snapshot is dropped
    epochs.append(store.snapshot_epoch("proj"))
    sync_names(store, "proj", "other")
    epochs.append(store.snapshot_epoch("proj"))
    assert len(set(epochs)) == len(epochs)


# -- review follow-ups -------------------------------------------------------------------------


def test_an_inactive_owner_stores_the_events_contents_but_no_snapshot(store: Store) -> None:
    """A reload removed the project while the capture ran: the event keeps its
    contents (they belong to the event), but no snapshot is stored for a project
    the sync already cleared, and its stale row is deleted."""
    event(store, {"a": b"1\n"})
    kept = journal(store, "a", "b")
    sync_names(store, "other")  # "proj" removed: its snapshots go with the sync
    store._db.execute(
        "INSERT INTO snapshots (project_id, path, content, captured_at) "
        "SELECT project_id, 'b', ?, 1.0 FROM changes WHERE id = ?",
        (b"stale\n", kept[0].id),
    )
    caps = [Captured("a", "content", b"2\n"), Captured("b", "content", b"b2\n")]
    event_id = store.create_event("proj", kept, [], 20.0, contents=caps, branch="main")
    assert event_id is not None
    assert {p: r.after for p, r in store.event_contents(event_id).items()} == {
        "a": b"2\n",
        "b": b"b2\n",
    }
    assert store._db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_a_path_past_the_event_budget_keeps_neither_side(store: Store) -> None:
    """Past the budget (the capture's marker): both sides NULL. Within it, an
    uncapturable path (turned binary) keeps its before for a catch-up's opening."""
    event(store, {"big": b"1\n", "bin": b"2\n"})
    kept = journal(store, "big", "bin")
    caps = [Captured("big", "uncapturable", over_budget=True), Captured("bin", "uncapturable")]
    event_id = store.create_event("proj", kept, [], 20.0, contents=caps, branch=None)
    assert event_id is not None
    rows = store.event_contents(event_id)
    assert (rows["big"].before, rows["big"].after, rows["big"].after_state) == (
        None,
        None,
        "uncapturable",
    )
    assert (rows["bin"].before, rows["bin"].after_state) == (b"2\n", "uncapturable")
    assert store.get_snapshots("proj") == {}


@pytest.mark.parametrize(
    "fields",
    [
        {"after_state": "content"},
        {"after_state": "deleted", "after": b"x"},
        {"after_state": "uncapturable", "after": b""},
        {"after_state": "content", "after": b"x", "over_budget": True},
        {"after_state": "deleted", "over_budget": True},
    ],
)
def test_an_inconsistent_capture_is_refused_when_built(fields: dict[str, object]) -> None:
    """Caught in the capture, never as an IntegrityError inside create_event."""
    with pytest.raises(ValueError):
        Captured("a", **fields)  # type: ignore[arg-type]


def test_an_event_of_exactly_the_budget_fits(store: Store) -> None:
    """16 MiB of before plus after is within the budget (the comparison is strict):
    one byte more makes the next path uncapturable."""
    quarter = 4 * MiB
    event(store, {"a": b"a" * quarter, "b": b"b" * quarter})
    e = event(store, {"a": b"A" * quarter, "b": b"B" * quarter, "c": b"c"}, now=20.0)
    rows = store.event_contents(e)
    assert {p: r.after_state for p, r in rows.items()} == {
        "a": "content",
        "b": "content",  # 4 + 4 + 4 + 4 MiB: exactly the budget
        "c": "uncapturable",
    }


def test_contents_exactly_at_the_cap_evict_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "PROJECT_EVENT_CONTENT_BUDGET", 18)
    first = event(store, {"a": b"12345678\n"}, now=1.0)
    store.mark_sent(first, 1)
    event(store, {"b": b"12345678\n"}, now=2.0)  # 18 bytes: at the cap, not over
    assert store.event_contents(first) != {}


def test_eviction_orders_by_captured_at_before_path(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The older snapshot goes first even though its path sorts later."""
    monkeypatch.setattr(store_mod, "PROJECT_SNAPSHOT_BUDGET", 10)
    event(store, {"z": b"zzzz\n"}, now=1.0)
    event(store, {"a": b"aaaa\n"}, now=5.0)
    event(store, {"m": b"mmmm\n"}, now=9.0)
    assert sorted(store.get_snapshots("proj")) == ["a", "m"]


def test_a_gap_of_exactly_ten_minutes_keeps_the_snapshots(store: Store) -> None:
    """The guard fires only past SNAPSHOT_GAP (strict comparison)."""
    event(store, {"a": b"1\n"}, now=1000.0)
    store.record_observation("proj", STATE, Observation(), now=1000.0 + 600.0)
    assert store.drop_stale_snapshots(2000.0) == 0
    assert store.get_snapshots("proj") == {"a": b"1\n"}
