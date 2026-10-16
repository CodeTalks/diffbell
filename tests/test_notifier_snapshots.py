"""Content snapshots through the notifier (content snapshots spec §2.2, §2.3, §3.3):
contents, note and branch captured at creation; the chain; delivery and catch-up
built from stored contents, per path."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from diffbell import snapshots as snapshots_mod
from diffbell import store as store_mod
from diffbell.config import BUILTIN_REDACT, NOTIFY_KINDS, ResolvedProject
from diffbell.diffing import SNAPSHOT_MAX_BYTES, read_capturable
from diffbell.fingerprint import Observation
from diffbell.models import Change, ChangeKind
from diffbell.notifier import Notifier, NotifierSettings
from diffbell.snapshots import Snapshotter
from diffbell.store import ClockState, Store
from diffbell.telegram import ErrorKind, TelegramError
from tests.fakes import FakeSender
from tests.helpers import HAS_GIT, git, init_repo, sync_names

pytestmark = pytest.mark.skipif(not HAS_GIT, reason="git not installed")

STATE = ClockState("/w", "", "c:1", "h")
M, C, D = ChangeKind.MODIFIED, ChangeKind.CREATED, ChangeKind.DELETED


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    store = Store(tmp_dir / "state" / "state.db")
    sync_names(store, "proj")
    return store


@pytest.fixture
def snap(tmp_dir: Path, store: Store) -> Snapshotter:
    return Snapshotter(tmp_dir / "state")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(tmp_project: Path) -> Path:
    init_repo(tmp_project)
    (tmp_project / "package.json").write_text('{\n  "v": 1\n}\n')
    git(tmp_project, "add", ".")
    git(tmp_project, "commit", "-q", "-m", "init")
    return tmp_project


def journal(store: Store, *paths: str, kind: ChangeKind = M) -> list[Change]:
    obs = Observation(changes=[(p, kind) for p in paths])
    return store.record_observation("proj", STATE, obs, now=1.0)


def make(
    store: Store,
    snap: Snapshotter | None,
    path: Path,
    clock: Clock,
    *,
    sender: FakeSender | None = None,
    notify_on: tuple[str, ...] = NOTIFY_KINDS,
) -> tuple[Notifier, FakeSender]:
    sender = sender or FakeSender()
    project = ResolvedProject("proj", path, ("*",), (), notify_on)
    notifier = Notifier(
        store,
        sender,
        projects=lambda name: project if name == "proj" else None,
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=clock,
        snapshots=snap,
    )
    return notifier, sender


async def flush(notifier: Notifier, store: Store, *paths: str, kind: ChangeKind = M) -> int:
    event_id = await notifier.flush("proj", journal(store, *paths, kind=kind))
    assert event_id is not None
    return event_id


# -- capture at creation ---------------------------------------------------------------------


async def test_creation_freezes_the_after_the_note_and_the_branch(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    notifier, _ = make(store, snap, repo, clock)
    (repo / "package.json").write_text('{\n  "v": 2\n}\n')
    event_id = await flush(notifier, store, "package.json")
    row = store.event_contents(event_id)["package.json"]
    assert (row.before, row.after, row.after_state, row.note) == (
        None,
        b'{\n  "v": 2\n}\n',
        "content",
        "uncommitted",
    )
    meta = store.event_meta(event_id)
    assert meta is not None and meta.branch == "main"
    assert store.get_snapshots("proj") == {"package.json": b'{\n  "v": 2\n}\n'}


async def test_the_chain_a_save_during_the_read_is_this_events_after(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save landing between the end of the burst and the read becomes part of this
    event's "after", and the next event's "before" is that same content."""
    target = tmp_project / "package.json"
    target.write_text("v1\n")
    notifier, _ = make(store, snap, tmp_project, clock)
    saves = iter(["v2 saved during the read\n"])

    def reader(root: Path, path: str, redact: Any, *args: Any) -> bytes | None:
        late = next(saves, None)
        if late is not None:
            target.write_text(late)
        return read_capturable(root, path, redact, *args)

    monkeypatch.setattr(snapshots_mod, "read_capturable", reader)
    e1 = await flush(notifier, store, "package.json")
    target.write_text("v3\n")
    e2 = await flush(notifier, store, "package.json")
    first, second = store.event_contents(e1), store.event_contents(e2)
    assert first["package.json"].after == b"v2 saved during the read\n"
    assert second["package.json"].before == first["package.json"].after
    assert second["package.json"].after == b"v3\n"


async def test_skipped_and_held_events_advance_the_snapshots(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    target = tmp_project / "Makefile"
    target.write_text("v1\n")
    skipping, _ = make(store, snap, tmp_project, clock, notify_on=("created",))
    e1 = await flush(skipping, store, "Makefile")
    assert store.get_event(e1).status == "skipped"
    assert store.get_snapshots("proj") == {"Makefile": b"v1\n"}
    store.set_snooze("proj", clock.now, None)
    target.write_text("v2\n")
    notifier, sender = make(store, snap, tmp_project, clock)
    e2 = await flush(notifier, store, "Makefile")
    assert store.get_event(e2).status == "held" and sender.sent == []
    assert store.event_contents(e2)["Makefile"].before == b"v1\n"
    assert store.get_snapshots("proj") == {"Makefile": b"v2\n"}


async def test_creation_never_opens_or_passes_a_redacted_file(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".env.local").write_text("API_KEY=SECRET\n")
    (repo / "package.json").write_text('{\n  "v": 2\n}\n')
    notifier, sender = make(store, snap, repo, clock)
    touched: list[str] = []
    real_open, real_run = os.open, subprocess.run

    def spy_open(path: Any, *args: Any, **kw: Any) -> int:
        touched.append(os.fspath(path))
        return real_open(path, *args, **kw)

    def spy_run(args: list[str], **kw: Any) -> Any:
        touched.extend(str(a) for a in args)
        return real_run(args, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(subprocess, "run", spy_run)
    event_id = await flush(notifier, store, ".env.local", "package.json")
    assert not any(".env" in t for t in touched)
    assert set(store.event_contents(event_id)) == {"package.json"}
    assert "SECRET" not in sender.sent[0][0]


async def test_a_redaction_added_during_the_capture_stores_nothing_of_the_file(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture ran with the old list; a reload added a covering pattern before it
    returned (content snapshots spec §2.5): the file is stored as never captured."""
    seed(store, {"tokens.json": b"old\n", "package.json": b"p1\n"})
    (tmp_project / "tokens.json").write_text("TOKEN=SECRET\n")
    (tmp_project / "package.json").write_text("p2\n")
    redact = [BUILTIN_REDACT]
    project = ResolvedProject("proj", tmp_project, ("*",), (), NOTIFY_KINDS)
    sender = FakeSender()
    notifier = Notifier(
        store,
        sender,
        projects=lambda name: project if name == "proj" else None,
        settings=lambda: NotifierSettings(redact=redact[0], max_lines=20, web_enabled=False),
        clock=clock,
        snapshots=snap,
    )
    real = snap.capture

    def capture_then_reload(*args: Any) -> Any:
        captured = real(*args)
        assert "tokens.json" in {c.path for c in captured.contents}  # read: old list
        redact[0] = (*BUILTIN_REDACT, "tokens*")  # the reload lands mid-capture
        return captured

    monkeypatch.setattr(snap, "capture", capture_then_reload)
    event_id = await flush(notifier, store, "tokens.json", "package.json")
    assert set(store.event_contents(event_id)) == {"package.json"}
    assert store.get_snapshots("proj") == {"package.json": b"p2\n"}
    blob = b"".join(
        bytes(row[0] or b"") + bytes(row[1] or b"")
        for row in store._db.execute("SELECT before, after FROM event_contents")
    )
    assert b"SECRET" not in blob
    assert "SECRET" not in sender.sent[0][0]


async def test_an_inactive_projects_event_reads_nothing(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journaled for folder A as "proj"; A is removed and folder B configured as
    "proj": A's event never reads B's folder."""
    (tmp_project / "package.json").write_text("{}\n")
    changes = journal(store, "package.json")
    store.sync_projects([("proj", "/df-test/b")], 0.0)
    read: list[str] = []

    def reader(root: Path, path: str, *args: Any) -> bytes | None:
        read.append(path)
        return None

    monkeypatch.setattr(snapshots_mod, "read_capturable", reader)
    notifier, _ = make(store, snap, tmp_project, clock)
    event_id = await notifier.flush("proj", changes)
    assert event_id is not None
    assert read == []
    assert store.event_contents(event_id) == {}
    meta = store.event_meta(event_id)
    assert meta is not None and meta.branch is None


async def test_uncapturable_drops_the_snapshot_and_the_next_event_has_no_before(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    target = tmp_project / "data.json"
    target.write_text("text\n")
    notifier, _ = make(store, snap, tmp_project, clock)
    await flush(notifier, store, "data.json")
    target.write_bytes(b"turned\0binary")
    e2 = await flush(notifier, store, "data.json")
    assert store.event_contents(e2)["data.json"].after_state == "uncapturable"
    assert store.get_snapshots("proj") == {}
    target.write_text("text again\n")
    e3 = await flush(notifier, store, "data.json")
    assert store.event_contents(e3)["data.json"].before is None  # never a stale before


async def test_a_capture_failure_still_creates_the_event(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_project / "a.json").write_text("1\n")
    notifier, _ = make(store, snap, tmp_project, clock)
    await flush(notifier, store, "a.json")

    def broken(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(Snapshotter, "capture", broken)
    e2 = await flush(notifier, store, "a.json")
    assert store.get_event(e2).status == "sent"
    assert store.event_contents(e2) == {}
    assert store.get_snapshots("proj") == {}


async def test_the_event_budget_is_a_pre_filter_in_path_order(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshots_mod, "EVENT_CONTENT_BUDGET", 8)
    (tmp_project / "a.json").write_text("aaaa\n")
    (tmp_project / "b.json").write_text("bbbb\n")
    capture = snap.capture(tmp_project, journal(store, "b.json", "a.json"), BUILTIN_REDACT)
    assert [(c.path, c.after_state, c.over_budget) for c in capture.contents] == [
        ("a.json", "content", False),
        ("b.json", "uncapturable", True),  # past the budget: create_event keeps no side
    ]


async def test_past_the_budget_create_event_keeps_no_before_but_binary_does(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    """Through the notifier: a path past the event budget is stored with both sides
    NULL (before, the pre-filter's past-budget path kept its "before"); a file that
    turned binary within the budget keeps its "before" for a catch-up."""
    seed(store, {"0.bin": b"z1\n", "a.json": b"a1\n", "b.json": b"b1\n"})
    (tmp_project / "0.bin").write_bytes(b"\0binary")  # first in path order: full budget
    (tmp_project / "a.json").write_text("a" * (SNAPSHOT_MAX_BYTES - 8) + "\n")
    (tmp_project / "b.json").write_text("b" * (SNAPSHOT_MAX_BYTES - 8) + "\n")
    notifier, _ = make(store, snap, tmp_project, clock)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snapshots_mod, "EVENT_CONTENT_BUDGET", SNAPSHOT_MAX_BYTES + 64)
        event_id = await flush(notifier, store, "0.bin", "a.json", "b.json")
    rows = store.event_contents(event_id)
    assert (rows["0.bin"].before, rows["0.bin"].after_state) == (b"z1\n", "uncapturable")
    assert rows["a.json"].after_state == "content"
    assert (rows["b.json"].before, rows["b.json"].after_state) == (None, "uncapturable")


def test_reads_are_bounded_by_what_is_left_of_the_budget(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each read gets min(SNAPSHOT_MAX_BYTES, what is left), and a spent budget
    opens no further file: a burst of big files can't be read whole past it."""
    monkeypatch.setattr(snapshots_mod, "EVENT_CONTENT_BUDGET", 10)
    files = {"a.json": "aaaa\n", "b.json": "bbbbb\n", "c.json": "cccc\n", "d.json": "d\n"}
    for name, data in files.items():
        (tmp_project / name).write_text(data)
    bounds: list[tuple[str, int]] = []

    def reader(root: Path, path: str, redact: Any, max_bytes: int) -> bytes | None:
        bounds.append((path, max_bytes))
        return read_capturable(root, path, redact, max_bytes)

    monkeypatch.setattr(snapshots_mod, "read_capturable", reader)
    capture = snap.capture(tmp_project, journal(store, *files), BUILTIN_REDACT)
    assert bounds == [("a.json", 10), ("b.json", 5), ("c.json", 5)]  # d.json: spent
    assert [(c.path, c.after_state, c.over_budget) for c in capture.contents] == [
        ("a.json", "content", False),
        ("b.json", "uncapturable", True),  # 6 bytes > the 5 left
        ("c.json", "content", False),  # 5 bytes: exactly what is left
        ("d.json", "uncapturable", True),  # never opened
    ]


# -- delivery from stored contents -------------------------------------------------------------


def seed(store: Store, files: dict[str, bytes]) -> None:
    """A baseline with these snapshots (what the watcher's REBASELINE stores)."""
    hashes = {p: hashlib.sha1(d).hexdigest() for p, d in files.items()}
    store.replace_baseline("proj", STATE, hashes, 1.0, snapshots=files)


async def test_a_project_without_git_shows_real_lines(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    seed(store, {"package.json": b'{\n  "v": 1\n}\n'})
    (tmp_project / "package.json").write_text('{\n  "v": 2\n}\n')
    notifier, sender = make(store, snap, tmp_project, clock)
    event_id = await flush(notifier, store, "package.json")
    text = sender.sent[0][0]
    assert "-  &quot;v&quot;: 1" in text and "+  &quot;v&quot;: 2" in text
    stored = store.get_event_report(event_id)
    assert stored is not None and stored.reports[0]["note"] is None  # no git: no note


async def test_a_change_committed_before_the_event_shows_its_lines(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    """The `+0 −0` case: a merge or pull committed the change before the event."""
    seed(store, {"package.json": b'{\n  "v": 1\n}\n'})
    (repo / "package.json").write_text('{\n  "v": 2\n}\n')
    git(repo, "commit", "-q", "-am", "pulled")
    notifier, sender = make(store, snap, repo, clock)
    event_id = await flush(notifier, store, "package.json")
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert stored.reports[0]["lines"] == ['-  "v": 1', '+  "v": 2']
    assert stored.reports[0]["note"] == "committed"
    assert "+1 −1" in sender.sent[0][0]


async def test_a_brand_new_untracked_file_is_plus_lines_untracked(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    (repo / "Makefile").write_text("all:\n\ttrue\n")
    notifier, _ = make(store, snap, repo, clock)
    event_id = await flush(notifier, store, "Makefile", kind=C)
    stored = store.get_event_report(event_id)
    assert stored is not None
    assert (stored.reports[0]["lines"], stored.reports[0]["note"]) == (
        ["+all:", "+\ttrue"],
        "untracked",
    )


async def test_a_message_sent_later_is_frozen_at_creation(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    """File, branch and HEAD all change (a checkout) before the retry: the message
    still shows the creation-time lines, note and branch, and the stored report
    keeps the same branch (one branch)."""
    seed(store, {"package.json": b'{\n  "v": 1\n}\n'})
    (repo / "package.json").write_text('{\n  "v": 2\n}\n')
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "502")])
    notifier, _ = make(store, snap, repo, clock, sender=sender)
    event_id = await flush(notifier, store, "package.json")
    assert store.get_event(event_id).status == "pending"
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "package.json").write_text('{\n  "v": 9\n}\n')
    git(repo, "commit", "-q", "-am", "elsewhere")
    clock.now += 3600
    await notifier.retry_due()
    assert store.get_event(event_id).status == "sent"
    text = sender.sent[-1][0]
    assert text.startswith("<b>🔑 proj</b> · <code>main</code>")
    assert "+  &quot;v&quot;: 2" in text and "9" not in text.split("\n", 1)[1]
    stored = store.get_event_report(event_id)
    assert stored is not None and stored.branch == "main"
    assert stored.reports[0]["note"] == "uncommitted"


async def test_an_event_whose_contents_are_gone_falls_back_per_path(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    seed(store, {"package.json": b'{\n  "v": 1\n}\n', "Makefile": b"a:\n"})
    (repo / "package.json").write_text('{\n  "v": 2\n}\n')
    (repo / "Makefile").write_text("b:\n")
    sender = FakeSender([TelegramError(ErrorKind.RETRYABLE, "502")])
    notifier, _ = make(store, snap, repo, clock, sender=sender)
    event_id = await flush(notifier, store, "package.json", "Makefile")
    store._db.execute(
        "DELETE FROM event_contents WHERE event_id = ? AND path = 'package.json'", (event_id,)
    )
    clock.now += 3600
    await notifier.retry_due()
    stored = store.get_event_report(event_id)
    assert stored is not None
    notes = {r["path"]: r["note"] for r in stored.reports}
    assert notes == {"package.json": "vs HEAD", "Makefile": "untracked"}


# -- the catch-up ------------------------------------------------------------------------------


async def held_events(
    notifier: Notifier, store: Store, target: Path, versions: list[str], clock: Clock
) -> list[int]:
    store.set_snooze("proj", clock.now, None)
    ids = []
    for version in versions:
        target.write_text(version)
        ids.append(await flush(notifier, store, target.name))
        clock.now += 60
    assert {store.get_event(i).status for i in ids} == {"held"}
    return ids


async def test_a_catch_up_is_the_first_before_vs_the_last_after(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    seed(store, {"package.json": b'{\n  "v": 1\n}\n'})
    notifier, sender = make(store, snap, repo, clock)
    versions = ['{\n  "v": 2\n}\n', '{\n  "v": 3\n}\n', '{\n  "v": 4\n}\n']
    await held_events(notifier, store, repo / "package.json", versions, clock)
    (repo / "package.json").write_text('{\n  "v": 5\n}\n')  # after the hold: not shown
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    ((text, _),) = sender.sent
    assert text.startswith("<b>🔕 Catch-up · proj</b> · <code>main</code> · held ")
    assert "-  &quot;v&quot;: 1" in text and "+  &quot;v&quot;: 4" in text
    assert "&quot;v&quot;: 2" not in text and "&quot;v&quot;: 5" not in text


async def test_a_catch_up_shows_the_last_events_branch(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    seed(store, {"package.json": b'{\n  "v": 1\n}\n'})
    notifier, sender = make(store, snap, repo, clock)
    await held_events(notifier, store, repo / "package.json", ['{\n  "v": 2\n}\n'], clock)
    git(repo, "checkout", "-q", "-b", "feat")
    await held_events(notifier, store, repo / "package.json", ['{\n  "v": 3\n}\n'], clock)
    git(repo, "checkout", "-q", "main")
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    assert sender.sent[0][0].startswith("<b>🔕 Catch-up · proj</b> · <code>feat</code>")


async def test_a_catch_up_path_without_its_opening_before_falls_back_alone(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    """No later event's "before" is ever substituted: it would show only part of
    the hold's change. The other path stays content-based."""
    seed(store, {"a.json": b"a1\n", "b.json": b"b1\n"})
    notifier, _ = make(store, snap, tmp_project, clock)
    store.set_snooze("proj", clock.now, None)
    (tmp_project / "a.json").write_text("a2\n")
    (tmp_project / "b.json").write_text("b2\n")
    first = await flush(notifier, store, "a.json", "b.json")
    (tmp_project / "a.json").write_text("a3\n")
    (tmp_project / "b.json").write_text("b3\n")
    await flush(notifier, store, "a.json", "b.json")
    store._db.execute("DELETE FROM event_contents WHERE event_id = ? AND path = 'a.json'", (first,))
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    stored = store.get_batch_report(first)
    assert stored is not None
    lines = {r["path"]: r["lines"] for r in stored.reports}
    assert lines == {"a.json": [], "b.json": ["-b1", "+b3"]}  # never "-a2", "+a3"


async def test_a_churning_lockfile_in_a_hold_keeps_its_opening_before(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contents cap evicts already-sent history, not the first held event whose
    "before" the catch-up needs."""
    monkeypatch.setattr(store_mod, "PROJECT_EVENT_CONTENT_BUDGET", 60)
    notifier, _ = make(store, snap, tmp_project, clock)
    for name in ("s1.json", "s2.json", "s3.json"):
        (tmp_project / name).write_text("sent....\n")  # 9 bytes each, settled
        await flush(notifier, store, name, kind=C)
    seed_lock = b"lock v0..\n"
    store._db.execute(
        "INSERT INTO snapshots (project_id, path, content, captured_at) "
        "SELECT id, 'yarn.lock', ?, 1.0 FROM projects WHERE name = 'proj'",
        (seed_lock,),
    )
    versions = [f"lock v{i}..\n" for i in (1, 2, 3)]  # 20 bytes per held event
    first, *_ = await held_events(notifier, store, tmp_project / "yarn.lock", versions, clock)
    assert store.event_contents(first)["yarn.lock"].before == seed_lock
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    stored = store.get_batch_report(first)
    assert stored is not None and stored.reports[0]["lines"] == ["-lock v0..", "+lock v3.."]


# -- review follow-ups -------------------------------------------------------------------------


def make_redacting(
    store: Store, snap: Snapshotter, path: Path, clock: Clock, redact: list[tuple[str, ...]]
) -> tuple[Notifier, FakeSender]:
    """As make, with a redaction list the test can change (a reload)."""
    sender = FakeSender()
    project = ResolvedProject("proj", path, ("*",), (), NOTIFY_KINDS)
    notifier = Notifier(
        store,
        sender,
        projects=lambda name: project if name == "proj" else None,
        settings=lambda: NotifierSettings(redact=redact[0], max_lines=20, web_enabled=False),
        clock=clock,
        snapshots=snap,
    )
    return notifier, sender


async def test_an_engine_exception_falls_back_for_that_path_only(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception out of the engine is that path's fallback, logged by type only:
    before, it failed the whole delivery, which retried forever with nothing sent."""
    seed(store, {"a.json": b"a1\n", "b.json": b"b1\n"})
    (tmp_project / "a.json").write_text("a2\n")
    (tmp_project / "b.json").write_text("b2\n")
    real = Snapshotter.run_engine

    def flaky(self: Snapshotter, before: bytes, after: bytes) -> Any:
        if before == b"a1\n":
            raise RuntimeError("boom a1 secret")
        return real(self, before, after)

    monkeypatch.setattr(Snapshotter, "run_engine", flaky)
    notifier, sender = make(store, snap, tmp_project, clock)
    event_id = await flush(notifier, store, "a.json", "b.json")
    assert store.get_event(event_id).status == "sent" and len(sender.sent) == 1
    stored = store.get_event_report(event_id)
    assert stored is not None
    lines = {r["path"]: r["lines"] for r in stored.reports}
    assert lines == {"a.json": [], "b.json": ["-b1", "+b2"]}  # no git: name and kind
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text


async def test_an_invalid_capture_is_uncapturable_never_a_stuck_event(
    store: Store,
    snap: Snapshotter,
    tmp_project: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture bug ("content" without bytes) is caught when the Captured is built:
    the event is created without contents and sent with the fallback. Before, it
    reached create_event as an IntegrityError (snapshots.content NOT NULL), and the
    journal rows stayed unlinked, retried at every start."""
    (tmp_project / "a.json").write_text("a\n")

    def buggy(*args: Any) -> snapshots_mod.EventCapture:
        return snapshots_mod.EventCapture((snapshots_mod.Captured("a.json", "content"),), None)

    monkeypatch.setattr(snap, "capture", buggy)
    notifier, sender = make(store, snap, tmp_project, clock)
    event_id = await flush(notifier, store, "a.json")
    assert store.get_event(event_id).status == "sent" and len(sender.sent) == 1
    assert store.event_contents(event_id) == {}
    assert store.get_snapshots("proj") == {}


async def test_a_deletion_is_frozen_with_its_before_and_note(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    """A DELETED change through the notifier: "deleted" with the snapshot as its
    before, the snapshot removed, all "-" lines, and the note of a file still in
    HEAD ("uncommitted") or never committed ("committed")."""
    (repo / "scratch.json").write_text("s\n")
    seed(store, {"package.json": b'{\n  "v": 1\n}\n', "scratch.json": b"s\n"})
    (repo / "package.json").unlink()
    (repo / "scratch.json").unlink()
    notifier, _ = make(store, snap, repo, clock)
    event_id = await flush(notifier, store, "package.json", "scratch.json", kind=D)
    rows = store.event_contents(event_id)
    pkg, scratch = rows["package.json"], rows["scratch.json"]
    assert (pkg.before, pkg.after, pkg.after_state, pkg.note) == (
        b'{\n  "v": 1\n}\n',
        None,
        "deleted",
        "uncommitted",
    )
    assert (scratch.after_state, scratch.note) == ("deleted", "committed")
    assert store.get_snapshots("proj") == {}
    stored = store.get_event_report(event_id)
    assert stored is not None
    lines = {r["path"]: r["lines"] for r in stored.reports}
    assert lines == {"package.json": ["-{", '-  "v": 1', "-}"], "scratch.json": ["-s"]}


@pytest.mark.parametrize("failure", ["missing", "timeout"])
async def test_a_capture_without_a_working_git_keeps_the_contents(
    store: Store,
    snap: Snapshotter,
    repo: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """git missing or timing out for the note and the branch: the after is still
    stored, with no note and no branch (never a guessed one)."""
    real = subprocess.run

    def no_git(args: list[str], **kw: Any) -> Any:
        if args[0] == "git":  # the note and branch calls (the engine uses its own path)
            if failure == "missing":
                raise FileNotFoundError("git")
            raise subprocess.TimeoutExpired(args, 10.0)
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", no_git)
    (repo / "package.json").write_text('{\n  "v": 2\n}\n')
    notifier, _ = make(store, snap, repo, clock)
    event_id = await flush(notifier, store, "package.json")
    row = store.event_contents(event_id)["package.json"]
    assert (row.after, row.after_state, row.note) == (b'{\n  "v": 2\n}\n', "content", None)
    meta = store.event_meta(event_id)
    assert meta is not None and meta.branch is None


async def test_a_stale_snapshot_of_a_redacted_file_is_dropped_by_create_event(
    store: Store, snap: Snapshotter, repo: Path, clock: Clock
) -> None:
    """A snapshot row for `.env.local` (stored before it was redacted): the event
    stores none of it and deletes the row, so no later diff can read it."""
    store._db.execute(
        "INSERT INTO snapshots (project_id, path, content, captured_at) "
        "SELECT id, '.env.local', ?, 1.0 FROM projects WHERE name = 'proj'",
        (b"API_KEY=OLD\n",),
    )
    (repo / ".env.local").write_text("API_KEY=SECRET\n")
    (repo / "package.json").write_text('{\n  "v": 2\n}\n')
    notifier, sender = make(store, snap, repo, clock)
    event_id = await flush(notifier, store, ".env.local", "package.json")
    assert set(store.event_contents(event_id)) == {"package.json"}
    assert ".env.local" not in store.get_snapshots("proj")
    assert "OLD" not in sender.sent[0][0] and "SECRET" not in sender.sent[0][0]


async def test_a_catch_up_path_without_its_closing_after_falls_back_alone(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    """The last held event's row is gone: the path falls back, never to an earlier
    event's "after" (it would show only part of the hold's change)."""
    seed(store, {"a.json": b"a1\n", "b.json": b"b1\n"})
    notifier, _ = make(store, snap, tmp_project, clock)
    store.set_snooze("proj", clock.now, None)
    (tmp_project / "a.json").write_text("a2\n")
    (tmp_project / "b.json").write_text("b2\n")
    first = await flush(notifier, store, "a.json", "b.json")
    (tmp_project / "a.json").write_text("a3\n")
    (tmp_project / "b.json").write_text("b3\n")
    last = await flush(notifier, store, "a.json", "b.json")
    store._db.execute("DELETE FROM event_contents WHERE event_id = ? AND path = 'a.json'", (last,))
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    stored = store.get_batch_report(first)
    assert stored is not None
    lines = {r["path"]: r["lines"] for r in stored.reports}
    assert lines == {"a.json": [], "b.json": ["-b1", "+b3"]}  # never "-a1", "+a2"


async def test_a_file_created_mid_hold_stays_content_based_without_its_first_row(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    """Created then modified in a hold reads created: it needs no opening "before",
    so the first event's row being gone changes nothing."""
    notifier, _ = make(store, snap, tmp_project, clock)
    store.set_snooze("proj", clock.now, None)
    (tmp_project / "new.json").write_text("n1\n")
    first = await flush(notifier, store, "new.json", kind=C)
    (tmp_project / "new.json").write_text("n2\n")
    await flush(notifier, store, "new.json")
    store._db.execute("DELETE FROM event_contents WHERE event_id = ?", (first,))
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    stored = store.get_batch_report(first)
    assert stored is not None
    assert [(r["kind"], r["lines"]) for r in stored.reports] == [("created", ["+n2"])]


async def test_deleted_then_recreated_in_a_hold_is_the_opening_before_vs_the_closing_after(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    seed(store, {"a.json": b"a1\n"})
    notifier, _ = make(store, snap, tmp_project, clock)
    store.set_snooze("proj", clock.now, None)
    first = await flush(notifier, store, "a.json", kind=D)
    (tmp_project / "a.json").write_text("a3\n")
    await flush(notifier, store, "a.json", kind=C)
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    stored = store.get_batch_report(first)
    assert stored is not None
    assert [(r["kind"], r["lines"]) for r in stored.reports] == [("modified", ["-a1", "+a3"])]


async def test_a_catch_up_hides_a_path_the_current_redaction_list_covers(
    store: Store, snap: Snapshotter, tmp_project: Path, clock: Clock
) -> None:
    """Stored while not redacted; the list grew before the catch-up (and the daemon's
    forget_redacted hasn't run): the send-time check still shows no line."""
    seed(store, {"tokens.json": b"TOKEN=OLD\n"})
    redact = [BUILTIN_REDACT]
    notifier, sender = make_redacting(store, snap, tmp_project, clock, redact)
    store.set_snooze("proj", clock.now, None)
    (tmp_project / "tokens.json").write_text("TOKEN=SECRET\n")
    first = await flush(notifier, store, "tokens.json")
    assert store.event_contents(first)["tokens.json"].after == b"TOKEN=SECRET\n"
    redact[0] = (*BUILTIN_REDACT, "tokens*")
    store.delete_snooze("proj", clock.now)
    await notifier.catch_up_due()
    ((text, _),) = sender.sent
    assert "SECRET" not in text and "OLD" not in text
    stored = store.get_batch_report(first)
    assert stored is not None and stored.reports[0]["lines"] == []
