"""Watcher logic against a fake Watchman caller (no real Watchman): subscribe decisions,
generation-safe PDU routing, canceled/error PDUs, and the problems it records in the
health registry (Watchman down, lost watches, recrawls; health spec §4.2)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pywatchman

from diffbell.config import ResolvedProject
from diffbell.health import Health
from diffbell.models import Change, ChangeKind
from diffbell.patterns import build_expression, expression_hash
from diffbell.store import ClockState, Store
from diffbell.watcher import (
    CONFIG_SUB,
    CONFIG_TARGET_SUB,
    RECRAWL_CLEAR_AFTER,
    RETRY_ERRORS_EVERY,
    WATCHMAN_GRACE,
    Mode,
    Watcher,
    recrawl_fix,
)
from diffbell.wmclient import WatchmanUnavailable
from tests.fakes import FakeSender
from tests.helpers import sync_names

ROOT = "/w/proj"
SHA_A = "a" * 40
SHA_B = "b" * 40


def project(watched: tuple[str, ...] = ("package.json",), path: str = ROOT) -> ResolvedProject:
    return ResolvedProject("p", Path(path), watched, ())


def ehash(p: ResolvedProject) -> str:
    return expression_hash(build_expression(p))


def entry(name: str, sha: str | None) -> dict[str, Any]:
    if sha is None:
        return {"name": name, "exists": False, "type": "f"}
    return {"name": name, "exists": True, "type": "f", "content.sha1hex": sha}


class FakeCaller:
    """Answers watch-project/subscribe/unsubscribe like Watchman; records every call."""

    def __init__(self, watch: str = ROOT, relative: str | None = None) -> None:
        self.watch = watch
        self.relative = relative
        self.calls: list[tuple[Any, ...]] = []
        self.errors: dict[str, Exception] = {}  # command -> exception raised once

    async def call(self, *args: Any) -> Any:
        self.calls.append(args)
        cmd = args[0]
        if cmd in self.errors:
            raise self.errors.pop(cmd)
        if cmd == "watch":
            return {"watch": args[1]}
        if cmd == "watch-project":
            res: dict[str, Any] = {"watch": self.watch}
            if self.relative:
                res["relative_path"] = self.relative
            return res
        if cmd == "subscribe":
            return {"subscribe": args[2], "clock": "c:sub"}
        if cmd == "unsubscribe":
            return {"unsubscribe": args[2], "deleted": True}
        raise AssertionError(f"unexpected command {args!r}")

    def subscribes(self) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == "subscribe"]

    def unsubscribes(self) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == "unsubscribe"]


class Env:
    def __init__(
        self,
        tmp_dir: Path,
        caller: FakeCaller | None = None,
        *,
        sender: FakeSender | None = None,
        config_path: Path | None = None,
        on_config_change: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.now = [1000.0]
        self.store = Store(tmp_dir / "state.db")
        sync_names(self.store, "p", "q", "r")  # every project these tests configure
        self.sender = sender or FakeSender()
        self.caller = caller or FakeCaller()
        self.changes: list[Change] = []
        self.health = Health(self.store, self.sender, clock=lambda: self.now[0], tz=ZoneInfo("UTC"))
        self.watcher = Watcher(
            self.caller,
            self.store,
            self.changes.extend,
            health=self.health,
            config_path=config_path,
            on_config_change=on_config_change,
            clock=lambda: self.now[0],
        )

    async def connect(self, *projects: ResolvedProject) -> None:
        await self.watcher.reconcile(list(projects))
        await self.watcher.handle({"_diffbell": "connected"})

    def sub_name(self) -> str:
        return str(self.caller.subscribes()[-1][2])

    def state(self) -> dict[str, Any]:
        rows = {r["name"]: r for r in self.watcher.status()}
        return rows["p"]

    async def alerts(self) -> list[str]:
        """What Telegram got so far, after a maintenance tick sent what is due."""
        await self.health.tick(self.now[0])
        return [text for text, _ in self.sender.sent]

    def problems(self) -> dict[str, str]:
        return {p.key: p.message for p in self.health.problems()}

    async def pdu(
        self,
        files: list[dict[str, Any]],
        *,
        fresh: bool,
        sub: str | None = None,
        clock: str = "c:2",
    ) -> None:
        await self.watcher.handle(
            {
                "subscription": sub or self.sub_name(),
                "root": self.caller.watch,
                "clock": clock,
                "is_fresh_instance": fresh,
                "files": files,
            }
        )


@pytest.fixture
def env(tmp_dir: Path) -> Env:
    return Env(tmp_dir)


# -- subscribe decision --------------------------------------------------------


async def test_first_run_subscribes_without_since_and_baselines_silently(env: Env) -> None:
    await env.connect(project())
    (_, root, name, opts) = env.caller.subscribes()[0]
    assert (root, name) == (ROOT, "diffbell:p#1")
    assert "since" not in opts and "relative_root" not in opts
    assert opts["defer_vcs"] is True
    assert env.state()["status"] == "subscribed"
    assert env.state()["note"] == "first run: baseline recorded"

    await env.pdu([entry("package.json", SHA_A)], fresh=True)
    assert env.changes == []
    assert env.store.get_hashes("p") == {"package.json": SHA_A}
    assert env.store.get_clock("p") == ClockState(ROOT, "", "c:2", ehash(project()))


async def test_matching_stored_clock_resumes_with_since(env: Env) -> None:
    p = project()
    env.store.replace_baseline("p", ClockState(ROOT, "", "c:old", ehash(p)), {}, 0.0)
    await env.connect(p)
    opts = env.caller.subscribes()[0][3]
    assert opts["since"] == "c:old"
    assert env.state()["note"] is None


async def test_relative_root_is_passed_and_must_match_for_since(tmp_dir: Path) -> None:
    caller = FakeCaller(watch="/w", relative="proj")
    env = Env(tmp_dir, caller)
    p = project()
    # Same watch root, different relative root: not the same clock domain.
    env.store.replace_baseline("p", ClockState("/w", "", "c:old", ehash(p)), {}, 0.0)
    await env.connect(p)
    opts = caller.subscribes()[0][3]
    assert opts["relative_root"] == "proj"
    assert "since" not in opts
    assert env.state()["relative_root"] == "proj"


async def test_expression_change_rebaselines_silently(env: Env) -> None:
    old = project(("package.json",))
    env.store.replace_baseline(
        "p", ClockState(ROOT, "", "c:old", ehash(old)), {"package.json": SHA_A}, 0.0
    )
    new = project(("package.json", "Makefile"))
    await env.connect(new)
    assert "since" not in env.caller.subscribes()[0][3]
    assert env.state()["note"] == "expression changed: re-baselined"

    # Full listing differs from the stored hashes, but nothing is journaled.
    await env.pdu([entry("package.json", SHA_B), entry("Makefile", SHA_A)], fresh=True)
    assert env.changes == []
    assert env.store.get_hashes("p") == {"package.json": SHA_B, "Makefile": SHA_A}
    clock = env.store.get_clock("p")
    assert clock is not None and clock.expr_hash == ehash(new)


@pytest.mark.parametrize(
    ("prev_split", "new_watch", "new_relative"),
    [
        (("/w", "proj"), ROOT, None),  # the ancestor /w stopped being watched
        ((ROOT, ""), "/w", "proj"),  # an ancestor /w started being watched
    ],
)
async def test_same_directory_new_watch_root_catches_up_from_stored_hashes(
    tmp_dir: Path,
    prev_split: tuple[str, str],
    new_watch: str,
    new_relative: str | None,
) -> None:
    env = Env(tmp_dir, FakeCaller(watch=new_watch, relative=new_relative))
    p = project()
    env.store.replace_baseline(
        "p",
        ClockState(*prev_split, "c:old", ehash(p)),
        {"package.json": SHA_A, "gone.lock": SHA_A},
        0.0,
    )
    await env.connect(p)
    assert "since" not in env.caller.subscribes()[0][3]
    assert env.watcher._states["p"].mode is Mode.CATCHUP
    assert env.state()["note"] == "watch root changed: caught up from stored hashes"

    await env.pdu([entry("package.json", SHA_B)], fresh=True)
    assert sorted((c.path, c.kind) for c in env.changes) == [
        ("gone.lock", ChangeKind.DELETED),
        ("package.json", ChangeKind.MODIFIED),
    ]
    clock = env.store.get_clock("p")
    assert clock is not None
    assert (clock.watch_root, clock.relative_root) == (new_watch, new_relative or "")


async def test_project_path_change_rebaselines_silently(tmp_dir: Path) -> None:
    env = Env(tmp_dir, FakeCaller(watch="/w/other"))
    old, new = project(path=ROOT), project(path="/w/other")  # same name, new directory
    env.store.replace_baseline(
        "p",
        ClockState(ROOT, "", "c:old", ehash(old)),
        {"package.json": SHA_A, ".env": SHA_A},
        0.0,
    )
    await env.connect(new)
    assert "since" not in env.caller.subscribes()[0][3]
    assert env.watcher._states["p"].mode is Mode.REBASELINE
    assert env.state()["note"] == "project path changed: re-baselined"

    await env.pdu([entry("package.json", SHA_B)], fresh=True)
    assert env.changes == []  # the old directory's hashes are not diffed against
    assert env.store.get_hashes("p") == {"package.json": SHA_B}
    clock = env.store.get_clock("p")
    assert clock is not None and clock.watch_root == "/w/other"


async def test_fresh_instance_with_empty_stored_baseline_reports_created(env: Env) -> None:
    # A baseline was recorded with zero matching files; then Watchman lost the clock
    # (reboot/recrawl), so the resumed subscription delivers a fresh instance.
    p = project()
    env.store.replace_baseline("p", ClockState(ROOT, "", "c:old", ehash(p)), {}, 0.0)
    await env.connect(p)
    assert env.caller.subscribes()[0][3]["since"] == "c:old"
    await env.pdu([entry("package.json", SHA_A)], fresh=True)
    assert [(c.path, c.kind) for c in env.changes] == [("package.json", ChangeKind.CREATED)]
    assert [(c.path, c.kind) for c in env.store.unflushed_changes()] == [
        ("package.json", ChangeKind.CREATED)
    ]
    assert env.store.get_hashes("p") == {"package.json": SHA_A}


async def test_incremental_pdu_journals_changes(env: Env) -> None:
    await env.connect(project())
    await env.pdu([], fresh=True, clock="c:1")
    await env.pdu([entry("package.json", SHA_A)], fresh=False)
    assert [(c.path, c.kind) for c in env.changes] == [("package.json", ChangeKind.CREATED)]
    assert "last_change_at" not in env.state()  # the dashboard reads it from the store


# -- generations ---------------------------------------------------------------


async def test_late_pdu_from_replaced_subscription_is_ignored(env: Env) -> None:
    await env.connect(project(("package.json",)))
    old_sub = env.sub_name()
    await env.pdu([entry("package.json", SHA_A)], fresh=True, sub=old_sub, clock="c:1")

    await env.watcher.reconcile([project(("package.json", "Makefile"))])
    new_sub = env.sub_name()
    assert (old_sub, new_sub) == ("diffbell:p#1", "diffbell:p#2")
    assert env.caller.unsubscribes() == [("unsubscribe", ROOT, old_sub)]
    before = env.store.get_clock("p")

    # A PDU for the old generation arrives after the switch: it must not be applied.
    await env.pdu([entry("package.json", SHA_B)], fresh=False, sub=old_sub, clock="c:late")
    assert env.changes == []
    assert env.store.get_clock("p") == before
    assert env.store.get_hashes("p") == {"package.json": SHA_A}

    # The new generation's fresh instance re-baselines silently.
    await env.pdu([entry("package.json", SHA_B)], fresh=True, sub=new_sub, clock="c:3")
    assert env.changes == []
    assert env.store.get_hashes("p") == {"package.json": SHA_B}


async def test_pdu_for_a_name_renamed_before_the_reconcile_is_dropped_then_replayed(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    # The daemon syncs the renamed config first; the watcher re-subscribes only in
    # the reconcile that follows. A PDU in between names a project the Store no
    # longer maps (stable project ids plan, Task 2b A).
    await env.connect(project())
    old_sub = env.sub_name()
    await env.pdu([entry("package.json", SHA_A)], fresh=True, sub=old_sub, clock="c:1")
    before = env.store.get_clock("p")
    assert before is not None and before.clock == "c:1"

    env.store.sync_projects([("p2", "/df-test/p")], 0.0)  # p renamed p2: same folder
    with caplog.at_level(logging.DEBUG, logger="diffbell.watcher"):
        await env.pdu([entry("package.json", SHA_B)], fresh=False, sub=old_sub, clock="c:2")
        await env.pdu([entry("package.json", SHA_A)], fresh=False, sub=old_sub, clock="c:3")
    assert env.changes == []
    assert env.store.get_clock("p2") == before  # same id, clock not advanced
    assert env.store.get_hashes("p2") == {"package.json": SHA_A}
    assert env.store.unflushed_changes() == []
    # The first drop per name (until the next reconcile) is a warning, then debug.
    drops = [r.levelno for r in caplog.records if "dropped a PDU for 'p'" in r.getMessage()]
    assert drops == [logging.WARNING, logging.DEBUG], caplog.text

    # The reconcile re-subscribes under the new name from the stored clock, and
    # Watchman replays the change since then: reported once, under the new name.
    renamed = ResolvedProject("p2", Path(ROOT), ("package.json",), ())
    await env.watcher.reconcile([renamed])
    assert env.caller.subscribes()[-1][3]["since"] == "c:1"
    await env.pdu([entry("package.json", SHA_B)], fresh=False, clock="c:3")
    assert [(c.project, c.path, c.kind) for c in env.changes] == [
        ("p2", "package.json", ChangeKind.MODIFIED)
    ]
    assert [(c.project, c.path) for c in env.store.unflushed_changes()] == [("p2", "package.json")]


class RootCaller(FakeCaller):
    """Watches every folder as its own root."""

    async def call(self, *args: Any) -> Any:
        if args[0] == "watch-project":
            self.calls.append(args)
            return {"watch": args[1]}
        return await super().call(*args)


async def test_a_pdu_racing_a_name_swap_is_dropped_and_each_folder_keeps_its_own_id(
    tmp_dir: Path,
) -> None:
    """Folders A ("p") and B ("q") swap names in the config. The sync runs first; a
    PDU from A's old subscription ("p") arrives before the reconcile. "p" now names
    B's id: the write must not land there. Dropped; after the reconcile each folder
    resumes from its own clock and its change lands under its new name, once (Task
    2b follow-up)."""
    env = Env(tmp_dir, RootCaller())
    env.store.sync_projects([("p", "/df-test/a"), ("q", "/df-test/b")], 0.0)
    await env.connect(
        project(path="/w/a"), ResolvedProject("q", Path("/w/b"), ("package.json",), ())
    )
    sub_a, sub_b = (str(c[2]) for c in env.caller.subscribes())
    await env.pdu([entry("package.json", SHA_A)], fresh=True, sub=sub_a, clock="c:a1")
    await env.pdu([entry("package.json", SHA_B)], fresh=True, sub=sub_b, clock="c:b1")
    clock_a, clock_b = env.store.get_clock("p"), env.store.get_clock("q")
    assert clock_a is not None and clock_b is not None

    env.store.sync_projects([("q", "/df-test/a"), ("p", "/df-test/b")], 0.0)  # swapped
    await env.pdu([entry("package.json", SHA_B)], fresh=False, sub=sub_a, clock="c:a2")
    assert env.changes == []
    assert env.store.get_clock("q") == clock_a and env.store.get_clock("p") == clock_b
    assert env.store.get_hashes("q") == {"package.json": SHA_A}  # folder A, untouched
    assert env.store.get_hashes("p") == {"package.json": SHA_B}  # folder B, untouched
    assert env.store.unflushed_changes() == []

    await env.watcher.reconcile(
        [ResolvedProject("q", Path("/w/a"), ("package.json",), ()), project(path="/w/b")]
    )
    subs = {c[1]: c for c in env.caller.subscribes()[2:]}
    assert (subs["/w/a"][3]["since"], subs["/w/b"][3]["since"]) == ("c:a1", "c:b1")
    # Watchman replays A's change since c:a1 (and B has one of its own).
    await env.pdu([entry("package.json", SHA_B)], fresh=False, sub=subs["/w/a"][2], clock="c:a3")
    await env.pdu([entry("package.json", SHA_A)], fresh=False, sub=subs["/w/b"][2], clock="c:b3")
    assert sorted((c.project, c.path) for c in env.changes) == [
        ("p", "package.json"),
        ("q", "package.json"),
    ]
    assert len(env.store.unflushed_changes()) == 2
    assert env.store.get_hashes("q") == {"package.json": SHA_B}  # folder A
    assert env.store.get_hashes("p") == {"package.json": SHA_A}  # folder B


async def test_a_drop_warns_again_after_the_next_reconcile(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    await env.connect(project())
    sub = env.sub_name()
    env.store.sync_projects([("other", "/df-test/p")], 0.0)
    with caplog.at_level(logging.DEBUG, logger="diffbell.watcher"):
        await env.pdu([], fresh=False, sub=sub, clock="c:1")
        await env.watcher.reconcile([project()])  # a reconcile that didn't fix it
        await env.pdu([], fresh=False, sub=env.sub_name(), clock="c:2")
    drops = [r.levelno for r in caplog.records if "dropped a PDU" in r.getMessage()]
    assert drops == [logging.WARNING, logging.WARNING], caplog.text


async def test_a_subscribed_state_without_a_token_is_an_error(env: Env) -> None:
    await env.connect(project())
    env.watcher._states["p"].token = None
    with pytest.raises(RuntimeError, match="token"):
        await env.pdu([entry("package.json", SHA_A)], fresh=True)
    assert env.store.get_clock("p") is None


async def test_a_sync_during_watch_project_leaves_the_subscription_to_the_reconcile(
    tmp_dir: Path,
) -> None:
    """Folders A ("p") and B ("q") swap names while the watcher awaits watch-project
    (the daemon syncs, then reconciles, with no await between). Nothing subscribes or
    baselines from the stale config; the reconcile subscribes each folder from its
    own id's clock (Task 2b follow-up, review item 1)."""
    env = Env(tmp_dir, RootCaller())
    env.store.sync_projects([("p", "/df-test/a"), ("q", "/df-test/b")], 0.0)
    pa = project(path="/w/a")
    qb = ResolvedProject("q", Path("/w/b"), ("package.json",), ())
    env.store.replace_baseline("p", ClockState("/w/a", "", "c:a0", ehash(pa)), {"x": SHA_A}, 0.0)
    env.store.replace_baseline("q", ClockState("/w/b", "", "c:b0", ehash(qb)), {"x": SHA_B}, 0.0)
    qa = ResolvedProject("q", Path("/w/a"), ("package.json",), ())
    pb = project(path="/w/b")
    swapped: list[asyncio.Task[None]] = []
    real = env.caller.call

    async def call(*args: Any) -> Any:
        res = await real(*args)
        if args[0] == "watch-project" and not swapped:
            env.store.sync_projects([("q", "/df-test/a"), ("p", "/df-test/b")], 0.0)
            swapped.append(asyncio.ensure_future(env.watcher.reconcile([qa, pb])))
            await asyncio.sleep(0)  # the reconcile records the new config, then waits
        return res

    env.caller.call = call  # type: ignore[method-assign]
    await env.connect(pa, qb)
    await swapped[0]
    subs = env.caller.subscribes()
    assert [(c[1], c[3].get("since")) for c in subs] == [("/w/a", "c:a0"), ("/w/b", "c:b0")]
    assert [str(c[2]).split("#")[0] for c in subs] == ["diffbell:q", "diffbell:p"]
    assert env.store.get_hashes("q") == {"x": SHA_A} and env.store.get_hashes("p") == {"x": SHA_B}
    assert {r["name"]: r["status"] for r in env.watcher.status()} == {
        "q": "subscribed",
        "p": "subscribed",
    }


async def test_a_name_whose_folder_changed_behind_an_equal_project_is_resubscribed(
    env: Env,
) -> None:
    """A symlinked project path retargeted: the config entry (and so the
    ResolvedProject) is unchanged, but the sync gave the name a new folder id. The
    old subscription's token is stale and would drop every PDU: re-subscribe."""
    await env.connect(project())
    old_sub = env.sub_name()
    await env.pdu([entry("package.json", SHA_A)], fresh=True, sub=old_sub, clock="c:1")
    env.store.sync_projects([("p", "/df-test/p-retargeted")], 0.0)  # a new id
    await env.watcher.reconcile([project()])  # equal to the subscribed project
    assert env.caller.unsubscribes() == [("unsubscribe", ROOT, old_sub)]
    new_sub = env.sub_name()
    assert new_sub != old_sub
    assert env.state()["status"] == "subscribed"
    assert env.watcher._states["p"].token == env.store.project_token("p")
    # The new id has no clock: a first run, baselined silently, then journaled.
    await env.pdu([entry("package.json", SHA_A)], fresh=True, sub=new_sub, clock="c:2")
    await env.pdu([entry("package.json", SHA_B)], fresh=False, sub=new_sub, clock="c:3")
    assert [(c.project, c.kind) for c in env.changes] == [("p", ChangeKind.MODIFIED)]


async def test_an_unchanged_reconcile_keeps_the_subscription(env: Env) -> None:
    await env.connect(project())
    sync_names(env.store, "p", "q", "r")  # the same folders: the same ids
    await env.watcher.reconcile([project()])
    assert len(env.caller.subscribes()) == 1 and env.caller.unsubscribes() == []


async def test_a_subscribe_left_pending_by_a_retarget_is_subscribed_by_the_reconcile(
    env: Env,
) -> None:
    """The retarget's sync lands while the subscribe awaits watch-project: it stays
    pending. The reconcile that follows (same ResolvedProject) must subscribe it."""
    real = env.caller.call
    synced: list[asyncio.Task[None]] = []

    async def call(*args: Any) -> Any:
        res = await real(*args)
        if args[0] == "watch-project" and not synced:
            env.store.sync_projects([("p", "/df-test/p-retargeted")], 0.0)
            synced.append(asyncio.ensure_future(env.watcher.reconcile([project()])))
            await asyncio.sleep(0)
        return res

    env.caller.call = call  # type: ignore[method-assign]
    await env.connect(project())
    await synced[0]
    assert len(env.caller.subscribes()) == 1
    assert env.state()["status"] == "subscribed"
    assert env.watcher._states["p"].token == env.store.project_token("p")


async def test_retrying_a_project_the_sync_just_removed_opens_no_problem(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Review item 2: a stale name is not a watch failure: no exception logged, no
    problem, no alert. The reconcile that follows the sync removes it."""
    env = Env(tmp_dir)
    (tmp_dir / "proj").mkdir()
    await env.connect(project(path=str(tmp_dir / "proj")))
    await cancel(env)
    assert env.state()["status"] == "error"
    sync_names(env.store, "q")  # p removed; its reconcile is still to come
    env.health.forget_projects_except(["q"])  # as the daemon's apply does
    sent = await env.alerts()
    env.now[0] += RETRY_ERRORS_EVERY
    with caplog.at_level(logging.DEBUG, logger="diffbell.watcher"):
        await env.watcher.tick()
    assert env.problems() == {}
    assert await env.alerts() == sent
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
    assert env.state()["status"] == "pending"


async def test_removed_project_is_unsubscribed_and_its_pdus_ignored(env: Env) -> None:
    await env.connect(project())
    sub = env.sub_name()
    await env.watcher.reconcile([])
    assert env.caller.unsubscribes() == [("unsubscribe", ROOT, sub)]
    assert env.watcher.status() == []
    await env.pdu([entry("package.json", SHA_A)], fresh=False, sub=sub)
    assert env.changes == [] and env.store.get_clock("p") is None


# -- canceled / error PDUs -----------------------------------------------------


async def test_canceled_pdu_marks_error_and_records_a_lost_watch(env: Env) -> None:
    await env.connect(project())
    await env.watcher.handle(
        {"subscription": env.sub_name(), "root": ROOT, "canceled": True, "unilateral": True}
    )
    assert env.state()["status"] == "error"
    assert "canceled" in str(env.state()["error"])
    lost = "project p: watch lost: watch canceled by Watchman (folder removed?)"
    assert env.problems() == {"project:p": lost}
    assert await env.alerts() == [f"⚠️ Diffbell: {lost}"]
    assert await env.alerts() == [f"⚠️ Diffbell: {lost}"]  # once


async def test_error_pdu_for_live_subscription_alerts_and_resubscribes(env: Env) -> None:
    p = project()
    await env.connect(p)
    old_sub = env.sub_name()
    await env.pdu([], fresh=True, clock="c:1")

    await env.watcher.handle(
        {
            "_diffbell": "error",
            "pdu": {"error": "boom", "subscription": old_sub, "root": ROOT, "unilateral": True},
        }
    )
    # Re-subscribed at once: the problem is already resolved, and the one message
    # says what it was.
    assert env.problems() == {}
    assert await env.alerts() == [
        "✅ Diffbell: project p: watching again "
        "(was down 00:16–00:16: project p: Watchman error: boom)"
    ]
    assert env.caller.unsubscribes() == [("unsubscribe", ROOT, old_sub)]
    new_sub = env.sub_name()
    assert new_sub == "diffbell:p#2"
    assert env.caller.subscribes()[-1][3]["since"] == "c:1"  # resumes, nothing lost
    assert env.state()["status"] == "subscribed"
    assert "boom" in str(env.state()["note"])

    # Old generation is dead; the new one is live.
    await env.pdu([entry("package.json", SHA_A)], fresh=False, sub=old_sub)
    assert env.changes == []
    await env.pdu([entry("package.json", SHA_A)], fresh=False, sub=new_sub)
    assert [c.kind for c in env.changes] == [ChangeKind.CREATED]


async def test_error_pdu_resubscribe_failure_leaves_project_in_error(env: Env) -> None:
    await env.connect(project())
    env.caller.errors["subscribe"] = pywatchman.CommandError("still broken")
    await env.watcher.handle(
        {
            "_diffbell": "error",
            "pdu": {"error": "boom", "subscription": env.sub_name(), "root": ROOT},
        }
    )
    assert env.state()["status"] == "error"
    assert "still broken" in str(env.state()["error"])
    assert env.problems()["project:p"].startswith("project p: cannot watch /w/proj: ")


async def test_error_without_known_subscription_only_logs(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    await env.connect(project())
    calls = len(env.caller.calls)
    with caplog.at_level(logging.WARNING, logger="diffbell.watcher"):
        await env.watcher.handle(
            {"_diffbell": "error", "pdu": {"error": "root gone", "root": ROOT}}
        )
        await env.watcher.handle(
            {"_diffbell": "error", "pdu": {"error": "stale", "subscription": "diffbell:p#99"}}
        )
        await env.watcher.handle({"_diffbell": "error", "message": "socket hiccup"})
    assert len(env.caller.calls) == calls
    assert env.problems() == {}
    assert await env.alerts() == []
    assert env.state()["status"] == "subscribed"
    text = caplog.text
    assert "root gone" in text and "stale" in text and "socket hiccup" in text


async def test_command_error_on_watch_marks_error_and_records_it(env: Env) -> None:
    env.caller.errors["watch-project"] = pywatchman.CommandError("no such dir")
    await env.connect(project())
    assert env.state()["status"] == "error"
    [alert] = await env.alerts()
    assert alert.startswith(f"⚠️ Diffbell: project p: cannot watch {ROOT}: ")
    assert alert.endswith("no such dir")
    [problem] = env.health.problems()
    assert (problem.key, problem.severity, problem.project) == ("project:p", "serious", "p")


async def test_unavailable_during_subscribe_marks_disconnected(env: Env) -> None:
    env.caller.errors["subscribe"] = WatchmanUnavailable("lost")
    await env.connect(project())
    assert env.state()["status"] == "disconnected"
    assert env.problems() == {}
    assert await env.alerts() == []


# -- connection state: Watchman down --------------------------------------------


async def test_not_connected_reconcile_is_pending_without_calls(env: Env) -> None:
    await env.watcher.reconcile([project()])
    assert env.caller.calls == []
    assert env.state()["status"] == "pending"


async def test_watchman_down_alerts_after_five_minutes_then_resolves_once(env: Env) -> None:
    start = env.now[0]  # the watcher starts "down" (not yet connected)
    [problem] = env.health.problems()
    assert (problem.key, problem.severity, problem.fix) == (
        "watchman",
        "serious",
        "watchman version",  # runnable: the page copies it into a terminal
    )
    env.now[0] = start + WATCHMAN_GRACE - 1
    await env.watcher.tick()
    assert await env.alerts() == []
    env.now[0] = start + WATCHMAN_GRACE
    assert await env.alerts() == [
        "⚠️ Diffbell: Watchman is unreachable; key files are not being watched. "
        "Check that Watchman is installed and running."
    ]
    env.now[0] += 30
    await env.connect(project(path="/"))  # a folder that exists: tick checks it
    assert env.problems() == {}
    alerts = await env.alerts()
    assert alerts[-1] == "✅ Diffbell: Watchman is reachable again (down 00:16–00:22)"
    assert len(alerts) == 2
    await env.watcher.tick()
    assert len(await env.alerts()) == 2


async def test_short_outage_neither_alerts_nor_recovers(env: Env) -> None:
    await env.connect(project())
    await env.watcher.handle({"_diffbell": "disconnected"})
    assert env.state()["status"] == "disconnected"
    assert list(env.problems()) == ["watchman"]  # on the page at once
    env.now[0] += 60
    await env.watcher.tick()
    await env.watcher.handle({"_diffbell": "connected"})
    assert await env.alerts() == []
    assert env.problems() == {}
    assert env.state()["status"] == "subscribed"
    assert env.sub_name() == "diffbell:p#2"  # fresh generation after reconnect


async def test_watchman_down_across_a_restart_resolves_with_the_first_down_time(
    tmp_dir: Path,
) -> None:
    env = Env(tmp_dir)
    env.now[0] += WATCHMAN_GRACE
    assert len(await env.alerts()) == 1
    # The daemon restarts while Watchman is still down: the open problem is kept.
    again = Watcher(
        env.caller, env.store, env.changes.extend, health=env.health, clock=env.watcher._clock
    )
    env.watcher = again
    env.now[0] += 600
    await env.connect(project())
    assert (await env.alerts())[-1] == (
        "✅ Diffbell: Watchman is reachable again (down 00:16–00:31)"
    )


async def test_a_lost_watch_healthy_after_a_restart_is_resolved_with_its_message(
    tmp_dir: Path,
) -> None:
    """A `project:p` saved (and sent) before a restart is resolved by the new
    watcher's first good subscribe: the ⚠️ gets its ✅."""
    env = Env(tmp_dir)
    env.health.open("project:p", "serious", "project p: watch lost: gone", project="p")
    assert await env.alerts() == ["⚠️ Diffbell: project p: watch lost: gone"]
    # The restart: a new registry and watcher on the same database.
    env.health = Health(env.store, env.sender, clock=lambda: env.now[0], tz=ZoneInfo("UTC"))
    env.watcher = Watcher(
        env.caller, env.store, env.changes.extend, health=env.health, clock=env.watcher._clock
    )
    env.now[0] += 60
    await env.connect(project())
    assert env.problems() == {}
    assert (await env.alerts())[1:] == ["✅ Diffbell: project p: watching again (down 00:16–00:17)"]


async def test_reconnect_resubscribes_from_stored_clock(env: Env) -> None:
    await env.connect(project())
    await env.pdu([], fresh=True, clock="c:7")
    await env.watcher.handle({"_diffbell": "disconnected"})
    await env.watcher.handle({"_diffbell": "connected"})
    assert env.caller.subscribes()[-1][3]["since"] == "c:7"


# -- config reload, robustness ------------------------------------------------------


async def test_reconnect_reloads_config_once_outside_the_lock(tmp_dir: Path) -> None:
    reloads: list[int] = []
    env: Env

    async def on_config_change() -> None:
        reloads.append(1)
        await env.watcher.reconcile([project()])  # takes the lock: must not deadlock

    env = Env(tmp_dir, on_config_change=on_config_change)
    await env.connect(project())
    assert reloads == []  # the first connect already reads the config
    await env.watcher.handle({"_diffbell": "disconnected"})
    await asyncio.wait_for(env.watcher.handle({"_diffbell": "connected"}), timeout=2)
    assert reloads == [1]
    assert env.state()["status"] == "subscribed"


async def test_config_subscription_matches_the_config_file_name_by_wholename(
    tmp_dir: Path,
) -> None:
    env = Env(tmp_dir, config_path=tmp_dir / "custom.yaml")
    await env.connect()
    [config_sub] = [c for c in env.caller.subscribes() if c[2] == CONFIG_SUB]
    assert config_sub[3]["expression"] == ["name", "custom.yaml", "wholename"]


async def test_config_dir_is_watched_as_its_own_root(tmp_dir: Path) -> None:
    # `watch`, not `watch-project`: a .git in an ancestor (e.g. ~/.config/.git or
    # ~/.git) must not widen the root to that ancestor.
    env = Env(tmp_dir, config_path=tmp_dir / "config.yaml")
    await env.connect()
    assert ("watch", str(tmp_dir)) in env.caller.calls
    assert ("watch-project", str(tmp_dir)) not in env.caller.calls
    [config_sub] = [c for c in env.caller.subscribes() if c[2] == CONFIG_SUB]
    assert config_sub[1] == str(tmp_dir)
    assert "relative_root" not in config_sub[3]


def config_subs(env: Env) -> list[tuple[Any, ...]]:
    return [c for c in env.caller.subscribes() if c[2] in (CONFIG_SUB, CONFIG_TARGET_SUB)]


def symlinked_config(tmp_dir: Path, target_dir: str = "dotfiles") -> tuple[Path, Path]:
    """config.yaml in conf/ symlinked to <target_dir>/real.yaml (a dotfiles setup)."""
    (tmp_dir / "conf").mkdir()
    (tmp_dir / target_dir).mkdir()
    real = tmp_dir / target_dir / "real.yaml"
    real.write_text("projects: []\n")
    link = tmp_dir / "conf" / "config.yaml"
    link.symlink_to(real)
    return link, real


async def test_plain_config_has_one_config_subscription(tmp_dir: Path) -> None:
    env = Env(tmp_dir, config_path=tmp_dir / "config.yaml")
    await env.connect()
    assert [(c[1], c[2]) for c in config_subs(env)] == [(str(tmp_dir), CONFIG_SUB)]


async def test_symlinked_config_also_watches_the_resolved_target(tmp_dir: Path) -> None:
    link, real = symlinked_config(tmp_dir)
    env = Env(tmp_dir, config_path=link)
    await env.connect()
    subs = {c[2]: c for c in config_subs(env)}
    assert set(subs) == {CONFIG_SUB, CONFIG_TARGET_SUB}
    # the link location (the user may retarget the symlink) ...
    assert subs[CONFIG_SUB][1] == str(tmp_dir / "conf")
    assert subs[CONFIG_SUB][3]["expression"] == ["name", "config.yaml", "wholename"]
    # ... and the file a web save actually rewrites, each its own `watch` root
    assert subs[CONFIG_TARGET_SUB][1] == str(real.parent)
    assert subs[CONFIG_TARGET_SUB][3]["expression"] == ["name", "real.yaml", "wholename"]
    assert ("watch", str(real.parent)) in env.caller.calls
    assert ("watch-project", str(real.parent)) not in env.caller.calls


async def test_change_to_the_symlink_target_reloads_the_config(tmp_dir: Path) -> None:
    reloads: list[int] = []

    async def on_config_change() -> None:
        reloads.append(1)

    link, real = symlinked_config(tmp_dir)
    env = Env(tmp_dir, config_path=link, on_config_change=on_config_change)
    await env.connect()
    base = {"root": str(real.parent), "clock": "c:1", "subscription": CONFIG_TARGET_SUB}
    await env.watcher.handle({**base, "is_fresh_instance": True, "files": []})
    assert reloads == []  # the initial fresh instance is not an edit
    files = [{"name": "real.yaml", "exists": True}]
    await env.watcher.handle({**base, "is_fresh_instance": False, "files": files})
    assert reloads == [1]


async def test_canceled_target_subscription_is_retried(tmp_dir: Path) -> None:
    reloads: list[int] = []

    async def on_config_change() -> None:
        reloads.append(1)

    link, real = symlinked_config(tmp_dir)
    env = Env(tmp_dir, config_path=link, on_config_change=on_config_change)
    await env.connect()
    await env.watcher.handle(
        {"subscription": CONFIG_TARGET_SUB, "root": str(real.parent), "canceled": True}
    )
    assert reloads == []
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    targets = [c for c in config_subs(env) if c[2] == CONFIG_TARGET_SUB]
    assert len(targets) == 2 and targets[-1][1] == str(real.parent)
    assert reloads == [1]


async def test_failed_target_watch_is_retried(tmp_dir: Path) -> None:
    link, real = symlinked_config(tmp_dir)
    caller = FakeCaller()
    env = Env(tmp_dir, caller, config_path=link)
    original = caller.call

    async def call(*args: Any) -> Any:
        if args[:2] == ("watch", str(real.parent)) and not env.now[0] > 1000.0:
            raise pywatchman.CommandError("too many watches")
        return await original(*args)

    caller.call = call  # type: ignore[method-assign]
    await env.connect()
    assert [c[2] for c in config_subs(env)] == [CONFIG_SUB]
    assert any("config edits will not be picked up" in a for a in await env.alerts())
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert [c[2] for c in config_subs(env)].count(CONFIG_TARGET_SUB) == 1


async def test_retargeted_symlink_moves_the_target_subscription(tmp_dir: Path) -> None:
    reloads: list[int] = []

    async def on_config_change() -> None:
        reloads.append(1)

    link, real = symlinked_config(tmp_dir)
    env = Env(tmp_dir, config_path=link, on_config_change=on_config_change)
    await env.connect()
    (tmp_dir / "elsewhere").mkdir()
    other = tmp_dir / "elsewhere" / "other.yaml"
    other.write_text("projects: []\n")
    link.unlink()
    link.symlink_to(other)
    await env.watcher.handle(
        {
            "subscription": CONFIG_SUB,
            "root": str(link.parent),
            "clock": "c:2",
            "is_fresh_instance": False,
            "files": [{"name": "config.yaml", "exists": True}],
        }
    )
    assert reloads == [1]
    last = [c for c in config_subs(env) if c[2] == CONFIG_TARGET_SUB][-1]
    assert (last[1], last[3]["expression"]) == (
        str(other.parent),
        ["name", "other.yaml", "wholename"],
    )
    assert ("unsubscribe", str(real.parent), CONFIG_TARGET_SUB) in env.caller.calls


async def test_missing_config_dir_warns(tmp_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    env = Env(tmp_dir, config_path=tmp_dir / "absent" / "config.yaml")
    with caplog.at_level(logging.WARNING, logger="diffbell.watcher"):
        await env.connect(project())
    assert str(tmp_dir / "absent") in caplog.text
    assert [c for c in env.caller.subscribes() if c[2] == CONFIG_SUB] == []


async def test_unexpected_subscribe_exception_marks_error_and_continues(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.caller.errors["watch-project"] = RuntimeError("kaput")
    other = ResolvedProject("q", Path("/w/q"), ("package.json",), ())
    with caplog.at_level(logging.ERROR, logger="diffbell.watcher"):
        await env.connect(project(), other)
    rows = {r["name"]: r for r in env.watcher.status()}
    assert rows["p"]["status"] == "error" and "kaput" in str(rows["p"]["error"])
    assert rows["q"]["status"] == "subscribed"
    assert "kaput" in caplog.text


class NeverSender(FakeSender):
    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        raise AssertionError("the watcher must never send")


async def test_the_watcher_only_records_problems_and_never_sends(tmp_dir: Path) -> None:
    """Problems found while the lock is held are recorded, never sent from here: the
    daemon's maintenance tick sends them, so Telegram never stalls subscriptions."""
    env = Env(tmp_dir, sender=NeverSender())
    q = ResolvedProject("q", Path("/w/q"), ("package.json",), ())
    r = ResolvedProject("r", Path("/w/r"), ("package.json",), ())
    env.caller.errors["watch-project"] = pywatchman.CommandError("no p")
    await env.connect(project(), q)  # a problem raised while subscribing on connect
    env.caller.errors["watch-project"] = pywatchman.CommandError("no r")
    await env.watcher.reconcile([project(), q, r])  # one raised by a reconcile
    q_sub = next(c[2] for c in env.caller.subscribes() if str(c[2]).startswith("diffbell:q#"))
    env.caller.errors["subscribe"] = pywatchman.CommandError("still broken")
    await env.watcher.handle(  # an error PDU, then a failing re-subscribe
        {"_diffbell": "error", "pdu": {"error": "boom", "subscription": q_sub, "root": ROOT}}
    )
    assert sorted(env.problems()) == ["project:p", "project:q", "project:r"]


async def test_unexpected_subscribe_exception_is_recorded(env: Env) -> None:
    env.caller.errors["watch-project"] = RuntimeError("kaput")
    await env.connect(project())
    [alert] = await env.alerts()
    assert alert == "⚠️ Diffbell: project p: cannot watch: RuntimeError: kaput"


# -- retrying failed / canceled watches -----------------------------------------


async def cancel(env: Env, sub: str | None = None) -> None:
    await env.watcher.handle(
        {"subscription": sub or env.sub_name(), "root": ROOT, "canceled": True, "unilateral": True}
    )


async def test_canceled_project_is_retried_once_its_directory_exists(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    p = project(path=str(tmp_dir / "proj"))
    (tmp_dir / "proj").mkdir()
    await env.connect(p)
    old_sub = env.sub_name()
    await cancel(env)
    assert env.state()["status"] == "error"

    env.now[0] += RETRY_ERRORS_EVERY - 1
    await env.watcher.tick()
    assert env.sub_name() == old_sub  # not yet due

    env.now[0] += 1
    await env.watcher.tick()
    assert ("unsubscribe", ROOT, old_sub) in env.caller.unsubscribes()
    assert env.sub_name() == "diffbell:p#2"
    assert env.state()["status"] == "subscribed"
    assert env.state()["note"] == "watching again after error"
    assert env.problems() == {}
    assert (await env.alerts())[-1] == (
        "✅ Diffbell: project p: watching again "
        "(was down 00:16–00:17: project p: watch lost: watch canceled by Watchman (folder removed?))"
    )

    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert env.sub_name() == "diffbell:p#2"  # healthy projects are left alone


async def test_errored_project_whose_directory_is_gone_is_not_retried(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    await env.connect(project(path=str(tmp_dir / "absent")))
    await cancel(env)
    calls = len(env.caller.calls)
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert len(env.caller.calls) == calls
    assert env.state()["status"] == "error"


async def test_retry_that_fails_again_stays_in_error_without_recovered_alert(
    tmp_dir: Path,
) -> None:
    env = Env(tmp_dir)
    await env.connect(project(path=str(tmp_dir)))
    await cancel(env)
    env.caller.errors["watch-project"] = pywatchman.CommandError("still gone")
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert env.state()["status"] == "error"
    assert "still gone" in str(env.state()["error"])
    assert "project:p" in env.problems()
    assert not any("watching again" in a for a in await env.alerts())


async def test_retry_runs_even_after_the_folder_and_recrawl_checks(tmp_dir: Path) -> None:
    """tick()'s other checks must not return before the retry pass."""
    env = Env(tmp_dir)
    await env.connect(project(path=str(tmp_dir)))
    await cancel(env)
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert env.state()["status"] == "subscribed"


async def test_no_retry_while_disconnected(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    await env.connect(project(path=str(tmp_dir)))
    await cancel(env)
    await env.watcher.handle({"_diffbell": "disconnected"})
    calls = len(env.caller.calls)
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert len(env.caller.calls) == calls


async def test_a_retry_resolves_without_sending_from_the_watcher(tmp_dir: Path) -> None:
    env = Env(tmp_dir, sender=NeverSender())
    await env.connect(project(path=str(tmp_dir)))
    await cancel(env)
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert env.state()["status"] == "subscribed" and env.problems() == {}


async def test_canceled_config_subscription_is_resubscribed_and_reloaded(tmp_dir: Path) -> None:
    reloads: list[int] = []

    async def on_config_change() -> None:
        reloads.append(1)

    env = Env(tmp_dir, config_path=tmp_dir / "config.yaml", on_config_change=on_config_change)
    await env.connect()
    assert len([c for c in env.caller.subscribes() if c[2] == CONFIG_SUB]) == 1
    await env.watcher.handle({"subscription": CONFIG_SUB, "root": str(tmp_dir), "canceled": True})
    assert reloads == []  # a canceled PDU is not a config edit

    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert len([c for c in env.caller.subscribes() if c[2] == CONFIG_SUB]) == 2
    assert reloads == [1]  # edits made while unwatched are picked up

    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert len([c for c in env.caller.subscribes() if c[2] == CONFIG_SUB]) == 2


async def test_failed_config_watch_is_recorded_retried_and_resolved(tmp_dir: Path) -> None:
    caller = FakeCaller()
    caller.errors["watch"] = pywatchman.CommandError("too many watches")
    env = Env(tmp_dir, caller, config_path=tmp_dir / "config.yaml")
    await env.connect()
    [alert] = await env.alerts()
    assert alert.startswith("⚠️ Diffbell: config edits will not be picked up: ")
    assert alert.endswith("too many watches")
    assert list(env.problems()) == ["config-watch"]
    assert [c for c in env.caller.subscribes() if c[2] == CONFIG_SUB] == []

    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert len([c for c in env.caller.subscribes() if c[2] == CONFIG_SUB]) == 1
    assert env.problems() == {}
    assert (await env.alerts())[-1] == (
        "✅ Diffbell: config edits are picked up again (down 00:16–00:17)"
    )


# -- a removed folder, recrawl warnings (health spec §4.2) ------------------------------


async def test_a_removed_project_folder_is_a_lost_watch_until_it_is_back(tmp_dir: Path) -> None:
    """Watchman does not cancel a watch below its root when the folder goes: the
    tick notices, and the retry picks the folder up again once it is back."""
    env = Env(tmp_dir)
    folder = tmp_dir / "proj"
    folder.mkdir()
    await env.connect(project(path=str(folder)))
    await env.watcher.tick()
    assert env.problems() == {}
    folder.rmdir()
    await env.watcher.tick()
    await env.watcher.tick()
    assert env.state()["status"] == "error"
    assert env.problems() == {"project:p": "project p: watch lost: project folder removed"}
    assert await env.alerts() == ["⚠️ Diffbell: project p: watch lost: project folder removed"]
    folder.mkdir()
    env.now[0] += RETRY_ERRORS_EVERY
    await env.watcher.tick()
    assert env.state()["status"] == "subscribed"
    assert await env.alerts() == [
        "⚠️ Diffbell: project p: watch lost: project folder removed",
        "✅ Diffbell: project p: watching again (down 00:16–00:17)",
    ]


async def test_a_removed_folder_then_a_cancel_pdu_is_one_alert(tmp_dir: Path) -> None:
    """Both notice the same lost watch: the cancel only updates the open problem."""
    for alert_first in (True, False):
        env = Env(tmp_dir / str(alert_first))
        folder = tmp_dir / f"proj-{alert_first}"
        folder.mkdir(parents=True)
        await env.connect(project(path=str(folder)))
        folder.rmdir()
        await env.watcher.tick()
        await env.watcher.tick()
        if alert_first:
            assert len(await env.alerts()) == 1
        await cancel(env)
        assert list(env.problems()) == ["project:p"]
        alerts = await env.alerts()
        env.now[0] += 3600
        assert alerts == await env.alerts()
        assert len(alerts) == 1 and alerts[0].startswith("⚠️ Diffbell: project p: watch lost")


async def test_a_folder_missing_at_one_tick_only_is_not_a_lost_watch(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    folder = tmp_dir / "proj"
    folder.mkdir()
    await env.connect(project(path=str(folder)))
    for _ in range(3):  # moved away and back between ticks, three times
        folder.rename(tmp_dir / "away")
        await env.watcher.tick()
        (tmp_dir / "away").rename(folder)
        await env.watcher.tick()
    assert env.state()["status"] == "subscribed"
    assert env.problems() == {}
    folder.rmdir()
    await env.watcher.tick()
    assert env.problems() == {}  # one tick: not yet
    await env.watcher.tick()
    assert list(env.problems()) == ["project:p"]  # two in a row: lost


def test_the_recrawl_fix_quotes_the_root() -> None:
    root = "/Users/me/my proj;rm -rf $HOME"
    assert recrawl_fix(root) == (
        "watchman watch-del '/Users/me/my proj;rm -rf $HOME' ; "
        "watchman watch-project '/Users/me/my proj;rm -rf $HOME'"
    )


async def recrawl_warning(env: Env) -> None:
    await env.watcher.handle(
        {
            "subscription": env.sub_name(),
            "root": env.caller.watch,
            "clock": "c:3",
            "is_fresh_instance": False,
            "files": [],
            "warning": "Recrawled this watch 1 time, most recently because:\n"
            "/w/a b: kFSEventStreamEventFlagUserDropped",
        }
    )


async def test_a_recrawl_warning_is_a_page_only_problem_with_a_quoted_fix(tmp_dir: Path) -> None:
    env = Env(tmp_dir, FakeCaller(watch="/w/a b;$x"))
    await env.connect(project())
    await recrawl_warning(env)
    [problem] = env.health.problems()
    assert (problem.key, problem.severity, problem.project) == ("recrawl:p", "warning", "p")
    assert problem.message.startswith("project p: Watchman had to recrawl the folder")
    assert problem.fix == "watchman watch-del '/w/a b;$x' ; watchman watch-project '/w/a b;$x'"
    env.now[0] += WATCHMAN_GRACE
    assert await env.alerts() == []  # never sent


async def test_other_watchman_warnings_are_only_logged(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    await env.connect(project())
    with caplog.at_level(logging.WARNING, logger="diffbell.watcher"):
        await env.watcher.handle(
            {
                "subscription": env.sub_name(),
                "root": ROOT,
                "clock": "c:3",
                "files": [],
                "is_fresh_instance": False,
                "warning": "something else",
            }
        )
    assert "something else" in caplog.text
    assert env.problems() == {}


async def test_a_recrawl_clears_on_the_next_fresh_subscription(env: Env) -> None:
    await env.connect(project())
    await recrawl_warning(env)
    await env.watcher.handle({"_diffbell": "disconnected"})
    await env.watcher.handle({"_diffbell": "connected"})  # subscribes afresh
    assert env.problems() == {}


async def test_a_recrawl_clears_after_a_day_without_a_new_warning(env: Env) -> None:
    await env.connect(project(path="/"))
    await recrawl_warning(env)
    env.now[0] += RECRAWL_CLEAR_AFTER - 60
    await recrawl_warning(env)  # a new warning starts the day again
    env.now[0] += RECRAWL_CLEAR_AFTER - 1
    await env.watcher.tick()
    assert list(env.problems()) == ["recrawl:p"]
    env.now[0] += 1
    await env.watcher.tick()
    assert env.problems() == {}


async def test_a_missing_watchman_binary_says_so_with_the_brew_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_dir: Path
) -> None:
    from diffbell import watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "find_binary", lambda: None)
    env = Env(tmp_dir)
    [problem] = env.health.problems()
    assert (problem.key, problem.severity, problem.message, problem.fix) == (
        "watchman",
        "serious",
        watcher_mod.WATCHMAN_MISSING,
        "brew install watchman",
    )
    assert watcher_mod.WATCHMAN_MISSING == (
        "Watchman isn't installed: no `watchman` on the PATH it searches; "
        "key files are not being watched."
    )
