"""Search inside stored diffs (diff readability spec §2.1, §7 "Store/API"): the
events query also matches a stored diff line of a file today's redaction list does
not cover, through a memo keyed by (q, redact, reports epoch)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from diffbell.config import BUILTIN_REDACT
from diffbell.diffing import FileReport
from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.store import (
    DIFF_SEARCH_MEMO,
    REPORTS_EPOCH,
    SCHEMA_VERSION,
    ClockState,
    Store,
)
from tests.helpers import sync_names

STATE = ClockState(watch_root="/w", relative_root="", clock="c:1", expr_hash="h1")
M = ChangeKind.MODIFIED
REDACT = BUILTIN_REDACT
TRIGGERS = [
    "event_reports_epoch_delete",
    "event_reports_epoch_insert",
    "event_reports_epoch_update",
]


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "p", "q")
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


def report(store: Store, event_id: int, *files: FileReport) -> None:
    store.put_event_report(event_id, "main", list(files), telegram_lines=20, telegram_files=9)


def diff(path: str, *lines: str, redacted: bool = False) -> FileReport:
    return FileReport(path, M, redacted=redacted, lines=() if redacted else lines)


def found(store: Store, q: str, redact: tuple[str, ...] | None = REDACT) -> list[tuple[int, bool]]:
    rows, total = store.query_events(q=q, redact=redact)
    assert total == len(rows)
    return [(r.event.id, r.q_in_diff) for r in rows]


def epoch(store: Store) -> int:
    value = store.kv_get(REPORTS_EPOCH)
    return 0 if value is None else int(value)


def counting(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every real scan of the stored lines (the memo's misses)."""
    scans: list[str] = []
    real = store._scan_diff_lines

    def scan(q: str, redact: tuple[str, ...]) -> frozenset[int]:
        scans.append(q)
        return real(q, redact)

    monkeypatch.setattr(store, "_scan_diff_lines", scan)
    return scans


# -- matching ------------------------------------------------------------------------


def test_a_diff_line_matches_and_flags_the_event(store: Store) -> None:
    e1 = add_event(store, ["docker-compose.yml"])
    report(store, e1, diff("docker-compose.yml", "-  image: postgres:15", "+  image: postgres:16"))
    e2 = add_event(store, ["package.json"])
    report(store, e2, diff("package.json", '+  "version": "1.0.4"'))
    assert found(store, "postgres:16") == [(e1, True)]
    assert found(store, "1.0.4") == [(e2, True)]


def test_a_path_match_is_unchanged_and_not_flagged(store: Store) -> None:
    e1 = add_event(store, ["package.json"])
    report(store, e1, diff("package.json", "+x"))
    assert found(store, "package") == [(e1, False)]


def test_a_path_and_a_diff_match_together_is_flagged(store: Store) -> None:
    e1 = add_event(store, ["version.txt"])
    report(store, e1, diff("version.txt", "+version 2"))
    assert found(store, "version") == [(e1, True)]


def test_one_character_searches_paths_only(store: Store) -> None:
    e1 = add_event(store, ["a.json"])
    report(store, e1, diff("a.json", "+z"))
    e2 = add_event(store, ["z.json"])
    report(store, e2, diff("z.json", "+1"))
    assert found(store, "z") == [(e2, False)]
    assert found(store, " z ") == [(e2, False)]  # trimmed, still one character: paths only


def test_q_is_trimmed_before_every_match(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2-code-point rule counts the trimmed q, and the LIKE patterns (paths and
    diff lines) and the memo key use that same trimmed q."""
    scans = counting(store, monkeypatch)
    e1 = add_event(store, ["a.json"])
    report(store, e1, diff("a.json", "+abc"))
    e2 = add_event(store, ["abz.txt"])
    report(store, e2, diff("abz.txt", "+1"))
    assert found(store, "ab ") == [(e2, False), (e1, True)]
    assert found(store, " ab") == [(e2, False), (e1, True)]
    assert scans == ["ab"]


def test_a_whitespace_only_q_is_no_q(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    scans = counting(store, monkeypatch)
    e1 = add_event(store, ["a.json"])
    report(store, e1, diff("a.json", "+  x"))
    e2 = add_event(store, ["b.json"])
    assert found(store, "  ") == [(e2, False), (e1, False)]
    assert store.query_events(q="  ", redact=REDACT) == store.query_events(redact=REDACT)
    assert scans == []


def test_without_a_redaction_list_only_paths_are_searched(store: Store) -> None:
    e1 = add_event(store, ["a.json"])
    report(store, e1, diff("a.json", "+needle"))
    assert found(store, "needle", redact=None) == []


@pytest.mark.parametrize(("q", "line"), [("%", "+100%"), ("_", "+a_b"), ("\\", "+back\\slash")])
def test_wildcards_match_literally(store: Store, q: str, line: str) -> None:
    hit = add_event(store, ["hit.txt"])
    report(store, hit, diff("hit.txt", line))
    miss = add_event(store, ["miss.txt"])
    report(store, miss, diff("miss.txt", "+plain words"))
    assert found(store, q + q[0]) == []  # doubled: no line holds it twice
    assert [i for i, _ in found(store, f"{line[1:]}")] == [hit]


def test_ascii_case_is_ignored_but_not_other_letters_or_forms(store: Store) -> None:
    e1 = add_event(store, ["a.txt"])
    report(store, e1, diff("a.txt", "+Postgres Ärger café"))
    assert found(store, "POSTGRES") == [(e1, True)]
    assert found(store, "ärger") == []  # non-ASCII letters match case-sensitively
    assert found(store, "Ärger") == [(e1, True)]
    assert found(store, "café") == []  # NFD never matches the stored NFC


def test_keys_notes_and_json_escapes_never_match(store: Store) -> None:
    e1 = add_event(store, ["a.json"])
    report(store, e1, FileReport("a.json", M, lines=('+"quoted"',), note="vs HEAD"))
    e2 = add_event(store, ["b.json"])
    report(store, e2, FileReport("b.json", M, lines=("+x",), note="uncommitted · approximate"))
    notes = ("vs HEAD", "uncommitted", "committed", "untracked", "approximate")
    for q in ("path", "kind", "redacted", "truncated", *notes, '\\"quoted', "lines"):
        assert found(store, q) == [], q
    assert found(store, '"quoted"') == [(e1, True)]  # the decoded line text does


def test_a_file_redacted_when_stored_never_matches(store: Store) -> None:
    e1 = add_event(store, [".env"])
    report(store, e1, diff(".env", "+SECRET=1", redacted=True))
    assert found(store, "SECRET") == []


def test_a_file_todays_redaction_list_covers_never_matches(store: Store) -> None:
    """Stored with lines, then a pattern was added: the search must not reveal what
    the now-redacted file contained, and its event is not flagged."""
    e1 = add_event(store, ["config/app.yaml", "README.md"])
    report(store, e1, diff("config/app.yaml", "+token: hunter2"), diff("README.md", "+hello"))
    assert found(store, "hunter2") == [(e1, True)]
    assert found(store, "hunter2", redact=(*REDACT, "app.yaml")) == []
    assert found(store, "hello", redact=(*REDACT, "app.yaml")) == [(e1, True)]
    # The path stays searchable through `changes`, as today.
    assert found(store, "app.yaml", redact=(*REDACT, "app.yaml")) == [(e1, False)]


def test_an_entry_marked_redacted_never_matches_even_with_lines(store: Store) -> None:
    e1 = add_event(store, ["x.txt"])
    raw = '[{"path":"x.txt","kind":"modified","redacted":true,"note":null,"lines":["+leak"]}]'
    store._db.execute(
        "INSERT INTO event_reports VALUES (?, 'main', ?, 1, 0, ?, 20, 1)", (e1, raw, len(raw))
    )
    assert found(store, "leak") == []


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "7",  # valid JSON, but not a list
        '"+needle"',
        '{"path": "x.txt", "lines": ["+needle"]}',  # an object, not a list
        '["needle", 7, null]',  # entries that are not objects
        '[{"path": 7, "lines": ["+needle"]}]',  # a path that is not text
        '[{"lines": ["+needle"]}]',  # no path
        '[{"path": "x.txt", "lines": "+needle"}]',  # lines not an array
        '[{"path": "x.txt", "lines": [["+needle"], {"a": "+needle"}]}]',  # not text items
    ],
)
def test_odd_stored_json_never_matches_and_never_errors(store: Store, raw: str) -> None:
    e1 = add_event(store, ["x.txt"])
    store._db.execute(
        "INSERT INTO event_reports VALUES (?, 'main', ?, 0, 0, ?, 20, 1)", (e1, raw, len(raw))
    )
    good = add_event(store, ["y.txt"])
    report(store, good, diff("y.txt", "+needle"))
    assert found(store, "needle") == [(good, True)]


def test_total_and_paging_use_the_same_condition(store: Store) -> None:
    ids = []
    for i in range(5):
        event_id = add_event(store, [f"f{i}.txt"], now=float(i))
        report(store, event_id, diff(f"f{i}.txt", "+needle" if i % 2 == 0 else "+hay"))
        ids.append(event_id)
    rows, total = store.query_events(q="needle", redact=REDACT, limit=2)
    assert total == 3 and [r.event.id for r in rows] == [ids[4], ids[2]]
    rows, total = store.query_events(q="needle", redact=REDACT, limit=2, before_id=ids[2])
    assert total == 3 and [r.event.id for r in rows] == [ids[0]]


def test_a_diff_search_combines_with_the_project_status_and_time_filters(
    store: Store,
) -> None:
    # Every filter adds a placeholder before the search's two: a reordering of the
    # parameters would bind the wrong value somewhere and change these results.
    in_p = add_event(store, ["a.txt"], now=1.0)
    report(store, in_p, diff("a.txt", "+needle"))
    in_q = add_event(store, ["b.txt"], now=2.0, project="q")
    report(store, in_q, diff("b.txt", "+needle"))
    by_path = add_event(store, ["needle.txt"], now=3.0)
    failed = add_event(store, ["c.txt"], now=4.0)
    report(store, failed, diff("c.txt", "+needle"))
    store.mark_failed(failed, "boom", kind="bad_request", at=0.0)
    hay = add_event(store, ["d.txt"], now=5.0)
    report(store, hay, diff("d.txt", "+hay"))

    def search(**filters: Any) -> tuple[list[tuple[int, bool]], int]:
        rows, total = store.query_events(q="needle", redact=REDACT, **filters)
        return [(r.event.id, r.q_in_diff) for r in rows], total

    assert search(project="p") == ([(failed, True), (by_path, False), (in_p, True)], 3)
    assert search(project="q") == ([(in_q, True)], 1)
    assert search(project="p", status="pending") == ([(by_path, False), (in_p, True)], 2)
    assert search(project="p", status="failed") == ([(failed, True)], 1)
    assert search(project="p", status="pending", since=2.0, until=5.0) == (
        [(by_path, False)],
        1,
    )
    assert search(project="p", limit=1, before_id=failed) == ([(by_path, False)], 3)


def test_more_than_999_matching_events_pass_as_one_parameter(store: Store) -> None:
    for i in range(1200):
        event_id = add_event(store, [f"f{i}.txt"], now=float(i))
        report(store, event_id, diff(f"f{i}.txt", "+needle"))
    rows, total = store.query_events(q="needle", redact=REDACT, limit=5)
    assert total == 1200 and all(r.q_in_diff for r in rows)


# -- the epoch triggers and the memo ----------------------------------------------------


def test_every_report_write_bumps_the_epoch(store: Store) -> None:
    assert epoch(store) == 0
    e1 = add_event(store, ["a.txt"], now=1.0)
    report(store, e1, diff("a.txt", "+1"))
    assert epoch(store) == 1
    report(store, e1, diff("a.txt", "+2"))  # a retry: INSERT OR REPLACE, same event_id
    assert epoch(store) == 2
    store.set_report_telegram(e1, lines=1, files=1)  # an update bumps it too (harmless)
    assert epoch(store) == 3
    store.mark_sent(e1, 1)
    store.prune(older_than=2.0)  # deletes the report
    assert epoch(store) == 4
    assert store.get_event_report(e1) is None


def test_a_cascade_from_events_bumps_the_epoch(store: Store) -> None:
    e1 = add_event(store, ["a.txt"])
    report(store, e1, diff("a.txt", "+1"))
    before = epoch(store)
    store._db.execute("DELETE FROM events WHERE id = ?", (e1,))  # ON DELETE CASCADE
    assert store.get_event_report(e1) is None
    assert epoch(store) == before + 1


def test_the_memo_reuses_the_ids_until_the_epoch_changes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans = counting(store, monkeypatch)
    e1 = add_event(store, ["a.txt"])
    report(store, e1, diff("a.txt", "+needle"))
    assert found(store, "needle") == [(e1, True)]
    assert found(store, "needle") == [(e1, True)]  # the next poll
    assert scans == ["needle"]
    e2 = add_event(store, ["b.txt"])
    report(store, e2, diff("b.txt", "+needle"))  # a new report
    assert found(store, "needle") == [(e2, True), (e1, True)]
    assert scans == ["needle", "needle"]


def test_a_retry_rewriting_the_report_drops_the_stale_flag(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry rebuilds the lines from HEAD under the same event_id:
    (MAX(event_id), COUNT(*)) would not change, the epoch does."""
    scans = counting(store, monkeypatch)
    e1 = add_event(store, ["a.txt"])
    report(store, e1, diff("a.txt", "+needle"))
    assert found(store, "needle") == [(e1, True)]
    report(store, e1, diff("a.txt", "+other"))
    assert found(store, "needle") == []
    assert len(scans) == 2


def test_a_prune_and_a_cascade_rescan(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    scans = counting(store, monkeypatch)
    old = add_event(store, ["a.txt"], now=1.0)
    report(store, old, diff("a.txt", "+needle"))
    young = add_event(store, ["b.txt"], now=100.0)
    report(store, young, diff("b.txt", "+needle"))
    assert [i for i, _ in found(store, "needle")] == [young, old]
    store.mark_sent(old, 1)
    store.prune(older_than=50.0)
    assert [i for i, _ in found(store, "needle")] == [young]
    store._db.execute("DELETE FROM events WHERE id = ?", (young,))
    assert found(store, "needle") == []
    assert len(scans) == 3


def test_a_different_redaction_list_rescans(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    scans = counting(store, monkeypatch)
    e1 = add_event(store, ["a.txt"])
    report(store, e1, diff("a.txt", "+needle"))
    found(store, "needle")
    found(store, "needle", redact=(*REDACT, "*.txt"))
    found(store, "needle", redact=(*REDACT, "*.txt"))
    assert scans == ["needle", "needle"]


def test_the_memo_keeps_the_16_most_recent_searches(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans = counting(store, monkeypatch)
    e1 = add_event(store, ["a.txt"])
    report(store, e1, diff("a.txt", "+needle"))
    queries = [f"q{i:02}" for i in range(DIFF_SEARCH_MEMO)]
    for q in queries:
        found(store, q)
    found(store, queries[0])  # used again: now the most recent
    found(store, "one more")  # evicts the least recently used, queries[1]
    assert len(scans) == DIFF_SEARCH_MEMO + 1
    found(store, queries[0])
    assert len(scans) == DIFF_SEARCH_MEMO + 1  # still remembered
    found(store, queries[1])
    assert scans[-1] == queries[1]  # forgotten: scanned again


def test_an_existing_v5_database_gains_the_triggers(tmp_dir: Path) -> None:
    """A database without the triggers (as main's code before them wrote it) gets
    them at the next open; user_version is unchanged and a report written then bumps
    the epoch."""
    path = tmp_dir / "state.db"
    Store(path).close()
    db = sqlite3.connect(path)
    for name in TRIGGERS:
        db.execute(f"DROP TRIGGER {name}")
    db.commit()
    db.close()
    store = Store(path)
    names = store._db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    ).fetchall()
    assert [n for (n,) in names] == TRIGGERS
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    sync_names(store, "p")
    e1 = add_event(store, ["a.txt"])
    report(store, e1, diff("a.txt", "+1"))
    assert epoch(store) == 1
