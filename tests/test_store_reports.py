"""Stored reports (event_reports, schema v3): the redacted diff each delivery
attempt was built from (UI refine spec §3.1)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from diffbell.diffing import FileReport
from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import (
    REPORT_LINE_MAX,
    REPORT_MAX_BYTES,
    SCHEMA_VERSION,
    SIZE_LIMIT_NOTE,
    ClockState,
    Store,
    encode_reports,
    stored_first_line,
)
from tests.helpers import sync_names

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")
M = ChangeKind.MODIFIED
MARKER = "REDACTED-MARKER-41d7"


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "p")
    return store


def add_event(store: Store, paths: list[str], now: float = 10.0, project: str = "p") -> int:
    obs = Observation()
    for path in paths:
        obs.upserts[path] = f"sha-{path}"
        obs.changes.append((path, M))
    changes = store.record_observation(project, STATE, obs, now=now)
    event_id = store.create_event(project, changes, [], now=now)
    assert event_id is not None
    return event_id


def diff(path: str, *lines: str) -> FileReport:
    return FileReport(path, M, lines=lines, note="vs HEAD")


def test_schema_and_foreign_keys_are_on(store: Store) -> None:
    assert SCHEMA_VERSION == 7
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert store._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_report_round_trip(store: Store) -> None:
    event_id = add_event(store, ["package.json", ".env.local"])
    reports = [
        diff("package.json", '-  "a": 1', '+  "a": 2', '+  "b": 3'),
        FileReport(".env.local", M, redacted=True),
    ]
    store.put_event_report(event_id, "main", reports, telegram_lines=20, telegram_files=2)
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert (stored.branch, stored.added, stored.removed) == ("main", 2, 1)
    assert (stored.telegram_lines, stored.telegram_files) == (20, 2)
    assert stored.reports == [
        {
            "path": "package.json",
            "kind": "modified",
            "redacted": False,
            "note": "vs HEAD",
            "lines": ['-  "a": 1', '+  "a": 2', '+  "b": 3'],
            "truncated": False,
        },
        {
            "path": ".env.local",
            "kind": "modified",
            "redacted": True,
            "note": None,
            "lines": [],
            "truncated": False,
        },
    ]


def test_an_event_without_a_report_reads_as_none(store: Store) -> None:
    assert store.get_event_report(add_event(store, ["a"])) is None
    assert store.get_event_report(999) is None


def test_each_attempt_replaces_the_report(store: Store) -> None:
    event_id = add_event(store, ["a"])
    store.put_event_report(event_id, "main", [diff("a", "+1")], telegram_lines=20, telegram_files=1)
    store.put_event_report(
        event_id, "feat", [diff("a", "+1", "+2")], telegram_lines=10, telegram_files=1
    )
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert (stored.branch, stored.added, stored.telegram_lines) == ("feat", 2, 10)
    count = store._db.execute("SELECT COUNT(*) FROM event_reports").fetchone()[0]
    assert count == 1


def test_set_report_telegram_updates_only_the_excerpt_fields(store: Store) -> None:
    event_id = add_event(store, ["a"])
    store.put_event_report(event_id, None, [diff("a", "+1")], telegram_lines=20, telegram_files=1)
    store.set_report_telegram(event_id, lines=5, files=0)
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert (stored.branch, stored.added, stored.telegram_lines, stored.telegram_files) == (
        None,
        1,
        5,
        0,
    )


def test_prune_cascades_to_the_report(store: Store) -> None:
    event_id = add_event(store, ["a"], now=1.0)
    store.put_event_report(event_id, None, [diff("a", "+1")], telegram_lines=20, telegram_files=1)
    store.mark_sent(event_id, 1)
    assert store.prune(older_than=100.0) == 1
    assert store.get_event_report(event_id) is None
    assert store._db.execute("SELECT COUNT(*) FROM event_reports").fetchone()[0] == 0


def test_a_report_needs_an_existing_event(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.put_event_report(42, None, [], telegram_lines=0, telegram_files=0)


def test_long_lines_are_clipped() -> None:
    encoded = encode_reports([diff("a", "+" + "x" * 2000, "-short")])
    (entry,) = json.loads(encoded.text)
    long_line, short = entry["lines"]
    assert len(long_line) == REPORT_LINE_MAX and long_line.endswith("…")
    assert long_line.startswith("+xxx") and short == "-short"


def test_redacted_files_never_store_lines() -> None:
    """Defence in depth: build_reports returns no lines for a redacted file, and
    the writer drops them anyway."""
    leaky = FileReport(".env.local", M, redacted=True, lines=(f"+TOKEN={MARKER}",))
    encoded = encode_reports([leaky])
    assert MARKER not in encoded.text
    assert json.loads(encoded.text)[0]["lines"] == []
    assert (encoded.added, encoded.removed) == (0, 0)


def test_the_size_cap_truncates_the_remaining_files() -> None:
    big = tuple("+" + "y" * 400 for _ in range(200))  # about 80 KiB on its own
    reports = [diff("first", "+1", "-2"), diff("big", *big), diff("after", "+3")]
    encoded = encode_reports(reports)
    first, second, third = json.loads(encoded.text)
    assert first["truncated"] is False and first["lines"] == ["+1", "-2"]
    for entry in (second, third):  # "after" would fit, but report order is kept
        assert entry["truncated"] is True and entry["lines"] == []
        assert entry["note"] == SIZE_LIMIT_NOTE
    assert (second["path"], second["kind"], second["redacted"]) == ("big", "modified", False)
    assert len(encoded.text.encode()) <= REPORT_MAX_BYTES
    assert (encoded.added, encoded.removed) == (1, 1)  # counts what is stored


def test_files_within_the_cap_are_kept_whole() -> None:
    lines = tuple(f"+{i}" for i in range(200))
    encoded = encode_reports([diff("a", *lines), diff("b", "-x")])
    a, b = json.loads(encoded.text)
    assert len(a["lines"]) == 200 and b["lines"] == ["-x"]
    assert not a["truncated"] and not b["truncated"]
    assert (encoded.added, encoded.removed) == (200, 1)


def test_the_cap_reserves_room_for_the_stubs_that_follow() -> None:
    """A file kept whole must leave room for the stubs of every later file."""
    a_lines = ("+" + "y" * 400,) * 160 + ("+" + "y" * 394,)
    reports = [diff("a", *a_lines), diff("b" * 300, "+" + "z" * 200)]
    encoded = encode_reports(reports)
    assert len(encoded.text.encode()) <= REPORT_MAX_BYTES
    _, b = json.loads(encoded.text)
    assert b["truncated"] is True and b["lines"] == [] and b["path"] == "b" * 300


@pytest.mark.parametrize("count", [200, 2000])
def test_many_files_stay_within_the_cap(count: int) -> None:
    """Within the cap whenever the stubs alone fit (200 files); otherwise (2,000)
    every entry is a stub, and the overshoot is the paths themselves."""
    reports = [diff(f"dir/{i:04d}/" + "p" * 110, "+" + "x" * 300) for i in range(count)]
    encoded = encode_reports(reports)
    entries = json.loads(encoded.text)
    assert len(entries) == count
    size = len(encoded.text.encode())
    assert size <= REPORT_MAX_BYTES or all(e["truncated"] for e in entries)
    if count == 200:
        assert size <= REPORT_MAX_BYTES and not entries[0]["truncated"]


def test_an_exact_boundary_keeps_both_files_whole() -> None:
    # b's whole entry must cost more than its stub (the stub's note is longer).
    reports = [diff("a", "+1", "-2"), diff("b", "+" + "3" * 60)]
    total = len(encode_reports(reports).text.encode())
    whole = json.loads(encode_reports(reports, max_bytes=total).text)
    assert [e["truncated"] for e in whole] == [False, False]
    one_short = encode_reports(reports, max_bytes=total - 1)
    a, b = json.loads(one_short.text)
    assert a["truncated"] is False and a["lines"] == ["+1", "-2"]
    assert b["truncated"] is True and b["lines"] == []  # "a" fits beside b's stub
    assert len(one_short.text.encode()) <= total - 1


def test_a_stubbed_redacted_file_stays_redacted() -> None:
    big = tuple("+" + "y" * 400 for _ in range(200))
    leaky = FileReport(".env.local", M, redacted=True, lines=(f"+TOKEN={MARKER}",))
    encoded = encode_reports([diff("big", *big), leaky])
    _, env = json.loads(encoded.text)
    assert env["truncated"] is True and env["redacted"] is True and env["lines"] == []
    assert MARKER not in encoded.text


def test_the_size_column_is_the_json_byte_length(store: Store) -> None:
    event_id = add_event(store, ["a"])
    store.put_event_report(event_id, None, [diff("a", "+é")], telegram_lines=20, telegram_files=1)
    text, size = store._db.execute(
        "SELECT reports, size FROM event_reports WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert size == len(text.encode()) and "é" in text


def test_query_events_rows_carry_the_report_summary(store: Store) -> None:
    old = add_event(store, ["a"], now=1.0)
    new = add_event(store, ["b"], now=2.0)
    store.put_event_report(
        new, "feat", [diff("b", "+1", "+2", "-3")], telegram_lines=20, telegram_files=1
    )
    rows, _ = store.query_events()
    by_id = {r.event.id: r for r in rows}
    summary = by_id[new].summary
    assert summary is not None
    assert (summary.branch, summary.added, summary.removed) == ("feat", 2, 1)
    assert by_id[old].summary is None


# -- first_line (Plan 7 spec §2.1) ----------------------------------------------------


def test_first_line_is_stored_and_read_back(store: Store) -> None:
    event_id = add_event(store, ["a", "b", ".env"])
    reports = [
        FileReport("a", M, lines=("+1",), note="vs HEAD", first_line=12),
        diff("b", "-2"),
        FileReport(".env", M, redacted=True, first_line=3),  # never stored for a redacted file
    ]
    store.put_event_report(event_id, "main", reports, telegram_lines=20, telegram_files=3)
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert [e.get("first_line", "absent") for e in stored.reports] == [12, "absent", "absent"]
    assert [stored_first_line(e) for e in stored.reports] == [12, None, None]


@pytest.mark.parametrize(
    "entry",
    [
        {},  # stored before Plan 7
        {"first_line": None},
        {"first_line": "3"},
        {"first_line": 3.0},
        {"first_line": True},
        {"first_line": 0},
        {"first_line": -2},
        {"first_line": [3]},
    ],
)
def test_a_missing_or_odd_first_line_reads_as_none(entry: dict[str, object]) -> None:
    assert stored_first_line(entry) is None


def test_a_valid_first_line_reads_as_itself() -> None:
    assert stored_first_line({"first_line": 1}) == 1
    assert stored_first_line({"first_line": 4096}) == 4096


def test_the_size_cap_counts_the_first_line_key() -> None:
    """A kept entry's cost is its own JSON, first_line included: the bound stays exact.
    A stub (truncated) never carries the key."""
    with_line = [FileReport("a", M, lines=("+x",), note="vs HEAD", first_line=123456)]
    without = [diff("a", "+x")]
    exact = len(encode_reports(with_line).text.encode())
    assert exact == len(encode_reports(without).text.encode()) + len(',"first_line":123456')
    [kept] = json.loads(encode_reports(with_line, max_bytes=exact).text)
    assert (kept["truncated"], kept["first_line"]) == (False, 123456)
    one_short = encode_reports(with_line, max_bytes=exact - 1)
    [stub] = json.loads(one_short.text)
    assert stub["truncated"] is True and "first_line" not in stub


def test_v2_database_is_migrated_to_the_current_version(tmp_dir: Path) -> None:
    db = tmp_dir / "state.db"
    v2 = Store(db)
    sync_names(v2, "p")
    event_id = add_event(v2, ["package.json"])
    v2.close()
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE event_reports")
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()
    store = Store(db)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    rows, total = store.query_events()
    assert total == 1 and [r.event.id for r in rows] == [event_id]
    assert [c.path for c in rows[0].changes] == ["package.json"]
    assert rows[0].summary is None  # predates diff history
    store.put_event_report(
        event_id, "main", [diff("package.json", "+1")], telegram_lines=20, telegram_files=1
    )
    assert store.get_event_report(event_id) is not None
    store.close()
