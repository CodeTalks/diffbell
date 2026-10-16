"""`diffbell upgrade` (Plan 6 spec §4): every step with fakes and a scratch state
directory, never the real uv, git checkout, launchctl or live state."""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from diffbell import cli, doctor, launchd, liveness, rename_install, upgrade
from diffbell.install_source import Source, read_source, write_source
from diffbell.liveness import write_status_file
from diffbell.store import Store
from diffbell.upgrade import Options, Runners, run_upgrade
from tests.test_doctor import scene as doctor_scene
from tests.test_store_migration import make_v5

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DOCTOR_OK = [{"name": n, "status": "ok", "message": "fine", "fix": None} for n in ("A", "B")]


class World:
    """Fake git, uv and the new tool. `diffbell install` "restarts" the daemon by
    writing status.json the way a fresh start does."""

    def __init__(self, tmp_dir: Path) -> None:
        self.repo = tmp_dir / "repo"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text('[project]\nname = "diffbell"\n')
        self.state = tmp_dir / "state"
        self.state.mkdir(mode=0o700)
        Store(self.state / "state.db").close()
        self.bin = tmp_dir / "uv-bin"
        self.clock = NOW
        self.dirty = ""
        self.fail: dict[str, int | Exception] = {}  # step name -> exit code or exception
        self.doctor: list[dict[str, Any]] = DOCTOR_OK
        self.daemon_starts = True
        self.connected = True  # the started daemon reaches Watchman
        self.calls: list[list[str]] = []
        self.envs: dict[str, dict[str, str]] = {}
        self.printed: list[str] = []
        self.previous = tmp_dir / "previous-checkout"  # an earlier install's source
        self.previous.mkdir()
        self.agent_runs = str(self.bin / "diffbell")  # the plist's ProgramArguments[0]
        self.branch = "feature/ops-speed"
        self.old_doctor = False  # a checkout from before Plan 7: no --pending-source
        # The doctor's answers in order (exit code, stdout, stderr), overriding the above.
        self.doctor_replies: list[tuple[int, str, str]] = []

    def now(self) -> float:
        return self.clock

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def _step(self, argv: list[str]) -> str:
        if argv[0] == "git":
            return "git " + argv[3]
        if argv[:3] == ["uv", "tool", "dir"]:
            return "uv dir"
        if argv[:3] == ["uv", "tool", "install"]:
            return "uv install"
        return argv[1]  # the new tool: install / doctor

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        step = self._step(argv)
        if "env" in kwargs:
            self.envs[step] = kwargs["env"]
        failure = self.fail.get(step)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return subprocess.CompletedProcess(argv, failure, "", "boom")
        out = ""
        if step == "git status":
            out = self.dirty
        elif step == "git rev-parse":
            out = f"{self.branch}\n" if "--abbrev-ref" in argv else COMMIT + "\n"
        elif step == "uv dir":
            out = f"{self.bin}\n"
        elif step == "install":
            self.clock += 2
            if self.daemon_starts:
                write_status_file(
                    self.state,
                    {
                        "running": True,
                        "updated_at": self.clock,
                        "last_started_at": self.clock,
                        "connected": self.connected,
                        "projects": [{"name": "a", "status": "subscribed"}],
                    },
                )
        elif step == "doctor":
            if self.doctor_replies:
                code, stdout, stderr = self.doctor_replies.pop(0)
                return subprocess.CompletedProcess(argv, code, stdout, stderr)
            if self.old_doctor and "--pending-source" in argv:
                usage = "diffbell: error: unrecognized arguments: --pending-source"
                return subprocess.CompletedProcess(argv, 2, "", usage)
            out = json.dumps(self.doctor)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def runners(self) -> Runners:
        return Runners(
            run=self.run,
            now=self.now,
            monotonic=self.monotonic,
            sleep=self.sleep,
            out=self.printed.append,
            plist=lambda: {"ProgramArguments": [self.agent_runs, "run", "--log-file", "x"]},
            env={"PATH": "/repo/.venv/bin:/usr/bin", "VIRTUAL_ENV": "/repo/.venv"},
        )

    def upgrade(self, **options: Any) -> int:
        options.setdefault("from_path", self.repo)
        return run_upgrade(Options(state_dir=self.state, **options), self.runners())

    @property
    def text(self) -> str:
        return "\n".join(self.printed)

    def backups(self) -> list[str]:
        return sorted(p.name for p in self.state.glob("state-upgrade-*"))


@pytest.fixture
def world(tmp_dir: Path) -> World:
    return World(tmp_dir)


@pytest.fixture(autouse=True)
def no_old_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """The upgrade guard never looks at the real old install here."""
    monkeypatch.setattr(rename_install, "for_install", lambda: None)


def old_install_left(monkeypatch: pytest.MonkeyPatch, left: list[str]) -> list[Any]:
    old, tools = object(), object()
    calls: list[Any] = []

    def leftovers(o: object, t: object) -> list[str]:
        calls.append((o, t))
        return left

    monkeypatch.setattr(rename_install, "for_install", lambda: (old, True))
    monkeypatch.setattr(rename_install.Tools, "real", lambda: tools)
    monkeypatch.setattr(rename_install, "leftovers", leftovers)
    calls.append((old, tools))  # the expected arguments, first
    return calls


def test_an_old_install_still_here_refuses_the_upgrade(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = old_install_left(monkeypatch, ["the agent", "the config folder /x"])
    assert world.upgrade() == 2
    assert calls[1] == calls[0]
    assert world.printed == [
        "Upgrade failed: an old dev-watchman install is still here (the agent, "  # old-name
        "the config folder /x): run `diffbell install` first, which moves it"
    ]
    assert world.calls == []  # no git, no uv, no backup
    assert world.backups() == [] and read_source(world.state) is None


def test_no_watchman_for_the_agent_refuses_the_upgrade(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launchd, "find_watchman", lambda: None)
    assert world.upgrade() == 2
    folders = ", ".join(launchd.LAUNCHD_PATH.split(":"))
    assert world.printed == [
        f"Upgrade failed: Watchman isn't installed where the agent looks ({folders}): "
        "`brew install watchman`, then run the upgrade again."
    ]
    assert world.calls == []  # no git, no uv
    assert world.backups() == [] and read_source(world.state) is None


def test_an_unchecked_keychain_alone_does_not_block_the_upgrade(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like install: a slow or locked Keychain with nothing else left is no old install."""
    old_install_left(monkeypatch, [rename_install.KEYCHAIN_UNCHECKED])
    assert world.upgrade() == 0
    assert not any("still here" in line for line in world.printed)


def test_nothing_left_of_an_old_install_upgrades(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = old_install_left(monkeypatch, [])
    assert world.upgrade() == 0
    assert calls[1] == calls[0]


def test_a_successful_upgrade(world: World) -> None:
    assert world.upgrade() == 0
    assert world.calls == [
        ["git", "-C", str(world.repo), "status", "--porcelain"],
        ["git", "-C", str(world.repo), "rev-parse", "HEAD"],
        ["git", "-C", str(world.repo), "rev-parse", "--abbrev-ref", "HEAD"],
        ["uv", "tool", "dir", "--bin"],
        ["uv", "tool", "install", "--force", "--reinstall", str(world.repo)],
        [str(world.bin / "diffbell"), "install"],
        [
            str(world.bin / "diffbell"),
            "doctor",
            "--json",
            "--pending-source",
            json.dumps({"path": str(world.repo), "commit": COMMIT, "branch": "feature/ops-speed"}),
        ],
    ]
    assert world.printed[0] == f"Installing feature/ops-speed @ 0123456 from {world.repo}"
    assert world.printed[-1] == "Upgraded to feature/ops-speed @ 0123456 · 2 ok"
    assert read_source(world.state) == Source(
        str(world.repo), COMMIT, "feature/ops-speed", world.clock
    )
    assert stat.S_IMODE(os.stat(world.state / "install-source.json").st_mode) == 0o600


def test_the_new_tool_runs_with_its_own_bin_dir_first(world: World) -> None:
    assert world.upgrade() == 0
    for step in ("uv install", "install", "doctor"):
        env = world.envs[step]
        assert env["PATH"] == f"{world.bin}:/repo/.venv/bin:/usr/bin", step
        assert "VIRTUAL_ENV" not in env, step


def test_git_is_only_read(world: World) -> None:
    world.upgrade()
    git = [argv[3:] for argv in world.calls if argv[0] == "git"]
    assert git == [
        ["status", "--porcelain"],
        ["rev-parse", "HEAD"],
        ["rev-parse", "--abbrev-ref", "HEAD"],
    ]
    for step in ("git status", "git rev-parse"):  # not even an index refresh
        assert world.envs[step]["GIT_OPTIONAL_LOCKS"] == "0", step


def test_uncommitted_changes_are_refused_unless_allowed(world: World) -> None:
    world.dirty = " M src/diffbell/cli.py\n"
    assert world.upgrade() == 2
    assert world.printed == [
        f"Upgrade failed: {world.repo} has uncommitted changes; commit them, or pass "
        "--allow-dirty to install them anyway"
    ]
    assert not any(argv[0] == "uv" for argv in world.calls)
    assert world.upgrade(allow_dirty=True) == 0


def test_a_dry_run_installs_nothing(world: World) -> None:
    assert world.upgrade(dry_run=True) == 0
    assert [argv[0] for argv in world.calls] == ["git", "git", "git"]
    assert world.backups() == [] and read_source(world.state) is None
    assert world.printed[1].startswith("Dry run:")


def test_without_from_the_recorded_source_is_used(world: World) -> None:
    assert world.upgrade(from_path=None) == 2
    assert world.printed == [
        "Upgrade failed: no install source recorded yet: pass --from PATH (a diffbell checkout)"
    ]
    write_source(world.state, Source(str(world.repo), "old", "main", NOW - 99))
    assert world.upgrade(from_path=None) == 0


def test_the_source_must_be_a_diffbell_checkout(world: World, tmp_dir: Path) -> None:
    other = tmp_dir / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\nname = "something-else"\n')
    assert world.upgrade(from_path=other) == 2
    assert "is not a diffbell checkout" in world.text
    assert world.calls == []


def test_the_backup_is_0600_from_creation_and_a_real_copy(world: World) -> None:
    modes: list[int] = []

    def spying(source: Path, target: Path) -> None:
        modes.append(stat.S_IMODE(os.stat(target).st_mode))  # before a byte is copied
        upgrade._backup_db(source, target)

    runners = world.runners()
    runners.backup = spying
    assert run_upgrade(Options(state_dir=world.state, from_path=world.repo), runners) == 0
    [name] = world.backups()
    assert name == "state-upgrade-20270115T080000Z.db"
    assert modes == [0o600]
    backup = world.state / name
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    conn = sqlite3.connect(f"{backup.as_uri()}?mode=ro", uri=True)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    conn.close()


def test_only_the_three_newest_backups_are_kept_and_nothing_else_is_touched(
    world: World,
) -> None:
    for stamp in ("20260101T000000Z", "20260201T000000Z", "20260301T000000Z", "20260401T000000Z"):
        (world.state / f"state-upgrade-{stamp}.db").write_text("old")
    (world.state / "state-upgrade-notes.txt").write_text("mine")
    (world.state / "state-upgrade-manual.db").write_text("mine")  # a .db, not ours
    (world.state / "state-5b-backup.db").write_text("mine")
    assert world.upgrade() == 0
    assert world.backups() == [
        "state-upgrade-20260301T000000Z.db",
        "state-upgrade-20260401T000000Z.db",
        "state-upgrade-20270115T080000Z.db",
        "state-upgrade-manual.db",
        "state-upgrade-notes.txt",
    ]
    assert (world.state / "state-5b-backup.db").exists()


def test_no_backup_skips_it(world: World) -> None:
    assert world.upgrade(no_backup=True) == 0
    assert world.backups() == []


@pytest.mark.parametrize(
    ("step", "failure", "reason"),
    [
        ("uv install", 1, "uv tool install failed (exit 1)"),
        (
            "uv install",
            subprocess.TimeoutExpired(["uv"], 300),
            "uv tool install timed out after 300 s",
        ),
        ("install", 2, "diffbell install failed (exit 2)"),
        (
            "install",
            subprocess.TimeoutExpired(["dbell"], 60),
            "diffbell install timed out after 60 s",
        ),
        (
            "doctor",
            subprocess.TimeoutExpired(["dbell"], 60),
            "diffbell doctor timed out after 60 s",
        ),
        ("uv dir", 2, "uv tool dir --bin failed: boom"),
        ("doctor", FileNotFoundError("dbell"), "cannot run {bin}/diffbell (FileNotFoundError)"),
        ("doctor", PermissionError("dbell"), "cannot run {bin}/diffbell (PermissionError)"),
    ],
)
def test_a_failed_step_prints_the_rollback_and_records_nothing(
    world: World, step: str, failure: int | Exception, reason: str
) -> None:
    previous = Source(str(world.previous), "old", "main", NOW - 99)
    write_source(world.state, previous)
    world.fail[step] = failure
    reason = reason.format(bin=world.bin)
    assert world.upgrade() == 2
    [backup] = world.backups()
    db = world.state / "state.db"
    assert world.printed[world.printed.index(f"Upgrade failed: {reason}") :] == [
        f"Upgrade failed: {reason}",
        "To roll back:",
        f"  uv tool install --force --reinstall {world.previous}",
        "  diffbell install",
        f"Database backup: {world.state / backup}",
        "To restore it (loses the events since the backup):",
        "  diffbell uninstall",
        f"  cp {world.state / backup} {db}",
        f"  rm -f {db}-wal {db}-shm",
        "  then reinstall as above",
    ]
    assert read_source(world.state) == previous


def migrating_world(world: World) -> Path:
    """A v5 database the new daemon migrates when `diffbell install` starts it
    (writing its own state-pre-v6 copy); then the doctor fails."""
    db = world.state / "state.db"
    db.unlink()
    make_v5(db)
    real = world.run

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = real(argv, **kwargs)
        if world._step(argv) == "install":
            Store(db).close()  # the new daemon opens it: v5 -> v6
        return result

    world.run = run  # type: ignore[method-assign]
    world.doctor = [{"name": "Database", "status": "fail", "message": "x", "fix": None}]
    write_source(world.state, Source(str(world.previous), "old", "main", NOW - 99))
    return db


def test_a_rollback_below_v6_requires_restoring_a_backup(world: World) -> None:
    """The previous code reads a v6 database without error, then every per-project
    query fails: restoring is required, not optional (ADR 0016)."""
    db = migrating_world(world)
    assert world.upgrade() == 2
    [backup] = world.backups()
    [pre_v6] = world.state.glob("state-pre-v6-*.db")
    text = world.printed[world.printed.index("To roll back:") :]
    assert text == [
        "To roll back:",
        f"  uv tool install --force --reinstall {world.previous}",
        "  diffbell install",
        "The upgrade migrated the database from schema v5 to v7: the previous code cannot use it.",
        f"Database backup: {world.state / backup}",
        f"Pre-v6 backup: {pre_v6}",
        "Restoring one is REQUIRED before rolling back (loses the events since the backup):",
        "  diffbell uninstall",
        f"  cp {world.state / backup} {db}",
        f"  rm -f {db}-wal {db}-shm",
        "  then reinstall as above",
    ]


def test_without_an_upgrade_backup_the_pre_v6_copy_is_the_one_to_restore(
    world: World,
) -> None:
    db = migrating_world(world)
    assert world.upgrade(no_backup=True) == 2
    assert world.backups() == []
    [pre_v6] = world.state.glob("state-pre-v6-*.db")
    text = world.printed[world.printed.index("To roll back:") + 3 :]
    assert text == [
        "The upgrade migrated the database from schema v5 to v7: the previous code cannot use it.",
        f"Pre-v6 backup: {pre_v6}",
        "Restoring one is REQUIRED before rolling back (loses the events since the backup):",
        "  diffbell uninstall",
        f"  cp {pre_v6} {db}",
        f"  rm -f {db}-wal {db}-shm",
        "  then reinstall as above",
    ]


def test_a_failure_before_the_migration_keeps_the_restore_optional(world: World) -> None:
    db = migrating_world(world)
    world.fail["uv install"] = 1  # the new daemon never starts: still v5
    assert world.upgrade() == 2
    assert "REQUIRED" not in world.text
    assert "To restore it (loses the events since the backup):" in world.printed
    assert not list(world.state.glob("state-pre-v6-*.db"))
    assert db.exists()


@pytest.mark.parametrize("step", ["git status", "git rev-parse"])
def test_a_missing_git_is_a_failure_not_a_traceback(world: World, step: str) -> None:
    world.fail[step] = FileNotFoundError("git")
    assert world.upgrade() == 2
    assert world.printed == ["Upgrade failed: cannot run git (FileNotFoundError)"]
    assert world.backups() == []
    assert read_source(world.state) is None


def test_a_relative_state_dir_is_backed_up(
    world: World, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_dir)
    options = Options(state_dir=Path("state"), from_path=world.repo)
    assert run_upgrade(options, world.runners()) == 0
    [backup] = world.backups()
    conn = sqlite3.connect(world.state / backup)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        conn.close()


def test_a_previous_source_that_is_gone_is_named_not_reinstalled(world: World) -> None:
    gone = world.previous / "moved-away"
    write_source(world.state, Source(str(gone), "old", "main", NOW - 99))
    world.fail["install"] = 1
    assert world.upgrade(no_backup=True) == 2
    assert (
        f"  previous source gone ({gone}): reinstall from the checkout you used before (uv tool install --force --reinstall PATH), then diffbell install"
        in world.printed
    )
    assert not any(line.startswith("  uv tool install") for line in world.printed)


def test_an_agent_that_runs_another_executable_is_a_failure(world: World) -> None:
    world.agent_runs = "/repo/.worktrees/x/.venv/bin/diffbell"
    assert world.upgrade(no_backup=True) == 2
    assert (
        f"Upgrade failed: the agent runs {world.agent_runs}, "
        f"not the uv tool's {world.bin / 'diffbell'}"
    ) in world.printed
    assert "To roll back:" in world.printed
    assert read_source(world.state) is None
    assert not any(argv[1:] == ["doctor", "--json"] for argv in world.calls)


def test_an_unreadable_plist_is_a_failure(world: World) -> None:
    runners = world.runners()

    def missing() -> dict[str, Any]:
        raise FileNotFoundError("io.688.diffbell.plist")

    runners.plist = missing
    options = Options(state_dir=world.state, from_path=world.repo, no_backup=True)
    assert run_upgrade(options, runners) == 2
    assert "Upgrade failed: cannot read the agent's plist (FileNotFoundError)" in world.printed


def test_a_daemon_that_never_restarts_is_a_failure(world: World) -> None:
    world.daemon_starts = False
    assert world.upgrade(no_backup=True) == 2
    assert "Upgrade failed: the daemon did not report a new start within 30 s" in world.printed
    assert "previous source unknown" in world.text
    assert "No database backup was made." in world.printed
    assert read_source(world.state) is None


def test_a_daemon_that_starts_but_is_not_ready_is_a_failure(world: World) -> None:
    world.connected = False
    assert world.upgrade(no_backup=True) == 2
    assert (
        "Upgrade failed: the daemon did not report ready within 30 s: "
        "not connected to Watchman (is it running?)"
    ) in world.printed
    assert read_source(world.state) is None
    assert not any(argv[1:2] == ["doctor"] for argv in world.calls)


def test_the_wait_is_the_shared_readiness_wait() -> None:
    assert upgrade.WAIT_SECONDS == liveness.READY_WAIT_SECONDS


def test_a_doctor_failure_is_an_upgrade_failure(world: World) -> None:
    world.doctor = [
        *DOCTOR_OK,
        {"name": "Watchman", "status": "fail", "message": "the server is not running", "fix": "x"},
        {"name": "Log", "status": "warn", "message": "1 error line(s)", "fix": "y"},
    ]
    assert world.upgrade() == 2
    assert "Doctor: 2 ok · 1 warning(s) · 1 failure(s)" in world.printed
    assert "  ✗ Watchman: the server is not running" in world.printed
    assert "Upgrade failed: doctor reports Watchman: the server is not running" in world.printed
    assert read_source(world.state) is None


def test_warnings_alone_still_upgrade(world: World) -> None:
    world.doctor = [*DOCTOR_OK, {"name": "Log", "status": "warn", "message": "m", "fix": None}]
    assert world.upgrade() == 0


def test_an_unconfigured_install_upgrades(world: World, tmp_dir: Path) -> None:
    """The real doctor's rows for an install with no bot: warnings only, so the
    upgrade passes (bot onboarding spec D7)."""
    s = doctor_scene(tmp_dir / "doctor")
    for account in s.secrets:
        s.secrets[account] = None
    rows = json.loads(doctor.to_json(doctor.run_checks(s.env)))
    assert {r["name"]: r["status"] for r in rows if r["status"] != "ok"} == {
        "Keychain": "warn",
        "Telegram": "warn",
    }
    world.doctor = rows
    assert world.upgrade() == 0
    assert "Doctor: 11 ok · 2 warning(s) · 0 failure(s)" in world.printed
    assert "  ! Keychain: not set up" in world.printed
    assert "  ! Telegram: not set up, nothing to check" in world.printed


def test_the_command_line(monkeypatch: pytest.MonkeyPatch, tmp_dir: Path) -> None:
    seen: list[Options] = []
    monkeypatch.setenv("DIFFBELL_STATE_DIR", str(tmp_dir / "s"))

    def fake(options: Options) -> int:
        seen.append(options)
        return 0

    monkeypatch.setattr(upgrade, "run_upgrade", fake)
    assert (
        cli.main(["upgrade", "--from", str(tmp_dir), "--allow-dirty", "--dry-run", "--no-backup"])
        == 0
    )
    assert seen == [
        Options(
            state_dir=tmp_dir / "s",
            from_path=tmp_dir,
            allow_dirty=True,
            dry_run=True,
            no_backup=True,
        )
    ]


# -- Plan 7 follow-ups (spec §4) --------------------------------------------------------


def test_the_doctor_judges_the_source_being_installed(world: World, tmp_dir: Path) -> None:
    """One JSON argument: an "@" (or anything) in the path or the branch can't mis-split it."""
    odd = tmp_dir / "a@b dir" / "repo"
    odd.mkdir(parents=True)
    (odd / "pyproject.toml").write_text('[project]\nname = "diffbell"\n')
    world.branch = "fix/a@b"
    assert world.upgrade(from_path=odd) == 0
    [doctor] = [argv for argv in world.calls if argv[1:2] == ["doctor"]]
    assert doctor[2:4] == ["--json", "--pending-source"] and len(doctor) == 5
    assert json.loads(doctor[4]) == {"path": str(odd), "commit": COMMIT, "branch": "fix/a@b"}


def test_an_older_doctor_is_asked_again_without_the_pending_source(world: World) -> None:
    """Upgrading to a checkout from before Plan 7: its doctor rejects the option."""
    world.old_doctor = True
    assert world.upgrade() == 0
    doctors = [argv[1:] for argv in world.calls if argv[1:2] == ["doctor"]]
    assert [d[:3] for d in doctors] == [
        ["doctor", "--json", "--pending-source"],
        ["doctor", "--json"],
    ]
    assert len(doctors[1]) == 2
    assert world.printed[-1] == "Upgraded to feature/ops-speed @ 0123456 · 2 ok"


def test_any_other_doctor_failure_is_not_retried(world: World) -> None:
    world.fail["doctor"] = 2  # exit 2 without "unrecognized arguments"
    assert world.upgrade() == 2
    assert len([argv for argv in world.calls if argv[1:2] == ["doctor"]]) == 1
    assert "Upgrade failed: diffbell doctor printed no readable result" in world.printed


def test_an_unrecognized_other_argument_is_not_retried(world: World) -> None:
    """Only a doctor that rejects --pending-source itself is asked again."""
    world.doctor_replies = [(2, "", "diffbell: error: unrecognized arguments: --json")]
    assert world.upgrade() == 2
    assert len([argv for argv in world.calls if argv[1:2] == ["doctor"]]) == 1


def test_a_real_doctor_failure_with_exit_2_is_reported_not_retried(world: World) -> None:
    gone = {"name": "Install", "status": "fail", "message": "the source being installed /x is gone"}
    world.doctor_replies = [(2, json.dumps([*DOCTOR_OK, {**gone, "fix": None}]), "")]
    assert world.upgrade() == 2
    assert len([argv for argv in world.calls if argv[1:2] == ["doctor"]]) == 1
    assert "  ✗ Install: the source being installed /x is gone" in world.printed
    assert "Upgrade failed: doctor reports Install: the source being installed /x is gone" in (
        world.printed
    )


def test_a_retry_that_fails_too_is_reported_once(world: World) -> None:
    usage = "diffbell: error: unrecognized arguments: --pending-source {}"
    world.doctor_replies = [(2, "", usage), (1, "Traceback (most recent call last)", "")]
    assert world.upgrade() == 2
    assert len([argv for argv in world.calls if argv[1:2] == ["doctor"]]) == 2
    assert "Upgrade failed: diffbell doctor printed no readable result" in world.printed


def test_every_line_is_flushed_before_the_next_subprocess() -> None:
    """Printed lines reach a pipe before uv's own output (spec §4, "Output order")."""
    program = (
        "import subprocess, sys\n"
        "from diffbell.upgrade import Runners\n"
        "say = Runners().out\n"
        "say('Installing x')\n"
        "subprocess.run([sys.executable, '-c', 'print(\"uv output\", flush=True)'], check=True)\n"
        "say('Doctor: 12 ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.splitlines() == ["Installing x", "uv output", "Doctor: 12 ok"]


def test_the_default_printer_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: seen.append(kwargs))
    Runners().out("line")
    assert seen == [{"flush": True}]


def test_upgrade_imports_nothing_after_it_starts() -> None:
    """Step 4 replaces the installed tool's files under the running process: every
    import must already have happened at module level."""
    tree = ast.parse(Path(upgrade.__file__).read_text())
    nested = [
        node.lineno
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef)
        for node in ast.walk(func)
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert nested == []
