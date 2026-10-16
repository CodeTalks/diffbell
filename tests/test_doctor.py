"""`diffbell doctor` (Plan 6 spec §3): every check with fakes and scratch paths,
never the real Keychain, launchctl, Telegram or Watchman."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from diffbell import __version__, cli, doctor, launchd
from diffbell.doctor import Check, Env, run_checks
from diffbell.install_source import Source, write_source
from diffbell.liveness import write_status_file
from diffbell.secrets import (
    ACCOUNT_BOT_TOKEN,
    ACCOUNT_CHAT_ID,
    ACCOUNT_SAVE_PENDING,
    SecretStoreError,
)
from diffbell.store import Problem, Store
from diffbell.telegram import ErrorKind, TelegramError

NOW = 1_800_000_000.0
TOKEN = "123456789:FAKE-TOKEN-never-printed-AAAAAAAAAA"
CHAT = "424242424"


class FakeRun:
    """`watchman --no-spawn --no-local` and `launchctl print`; records every argv.
    ``watchman`` overrides every Watchman call, ``watch_list`` only the listing."""

    def __init__(self, roots: list[str]) -> None:
        self.roots = roots
        self.watchman: subprocess.CompletedProcess[str] | Exception | None = None
        self.watch_list: subprocess.CompletedProcess[str] | None = None
        self.launchctl = subprocess.CompletedProcess([], 0, "state = running\n\tpid = 4242\n", "")
        self.argvs: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        self.kwargs.append(kwargs)
        if argv[0] == "watchman":
            if isinstance(self.watchman, Exception):
                raise self.watchman
            if self.watchman is not None:
                return self.watchman
            reply: dict[str, Any] = {"version": "2026.10.05.00"}
            if argv[-1] == "watch-list":
                if self.watch_list is not None:
                    return self.watch_list
                reply["roots"] = self.roots
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")
        assert argv[:2] == ["launchctl", "print"], argv
        return self.launchctl


@dataclasses.dataclass
class Scene:
    env: Env
    run: FakeRun
    secrets: dict[str, str | None]
    home: Path
    project: Path


def scene(tmp_dir: Path) -> Scene:
    """An install where every check is ok."""
    home = tmp_dir / "home"
    config_dir, state, logs = home / "diffbell", home / "state", home / "logs"
    project = home / "projects" / "shop"
    for d in (config_dir, state, logs, project):
        d.mkdir(mode=0o700, parents=True)
    config = config_dir / "config.yaml"
    config.write_text(f"web:\n  enabled: true\n  port: 8765\nprojects:\n  - path: {project}\n")
    config.chmod(0o600)
    Store(state / "state.db").close()
    for name in ("state.db", "state.db-wal", "state.db-shm"):
        if (state / name).exists():
            (state / name).chmod(0o600)
    write_status_file(state, {"pid": 4242, "running": True, "updated_at": NOW - 5})
    write_source(state, Source(str(tmp_dir), "abc1234def", "main", NOW - 3600))
    log = logs / "diffbell.log"
    log.write_text(
        "2026-10-10 09:00:00 ERROR x: Traceback (an old one)\n"
        "2026-10-10 10:00:00 INFO diffbell.daemon: diffbell started with 1 project(s), 0 missing\n"
    )
    log.chmod(0o600)
    plist = home / "io.688.diffbell.plist"
    plist.write_text("<plist/>")
    run = FakeRun([str(project.parent)])
    secrets: dict[str, str | None] = {
        ACCOUNT_BOT_TOKEN: TOKEN,
        ACCOUNT_CHAT_ID: CHAT,
        ACCOUNT_SAVE_PENDING: None,
    }

    def secret(account: str) -> str | None:
        value = secrets[account]
        if value == "raise":
            raise SecretStoreError("keychain command 'find-generic-password' timed out after 5s")
        return value

    env = Env(
        config_path=config,
        state_dir=state,
        log_dir=logs,
        run=run,
        secret=secret,
        get_me=lambda token: None,
        healthz=lambda port: {"ok": True},
        now=lambda: NOW,
        plist=plist,
        uid=501,
        old_install=lambda: [],
        agent_watchman=lambda: Path("/opt/homebrew/bin/watchman"),
    )
    return Scene(env, run, secrets, home, project)


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_a_healthy_install_is_all_ok(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    checks = run_checks(s.env)
    assert [c.name for c in checks] == list(doctor.NAMES)
    assert [c for c in checks if c.status != "ok"] == []
    text = doctor.render(checks)
    assert text.splitlines()[-1] == "13 ok · 0 warnings · 0 failures"
    assert text.splitlines()[0].startswith("✓ Install")
    assert doctor.exit_code(checks) == 0


def test_no_secret_is_ever_printed(tmp_dir: Path) -> None:
    s = scene(tmp_dir)

    def failing(token: str) -> None:
        raise TelegramError(ErrorKind.AUTH, f"getMe: Unauthorized for bot{token}")

    s.env = dataclasses.replace(s.env, get_me=failing)
    for checks in (run_checks(s.env), run_checks(scene(tmp_dir / "b").env)):
        for text in (doctor.render(checks), doctor.to_json(checks)):
            assert TOKEN not in text and "FAKE-TOKEN" not in text and CHAT not in text


def test_the_watchman_check_never_spawns_a_server(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    run_checks(s.env)
    watchman = [
        (a, k) for a, k in zip(s.run.argvs, s.run.kwargs, strict=True) if a[0] == "watchman"
    ]
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": 5.0,
        "check": False,
        "stdin": subprocess.DEVNULL,
    }
    assert watchman == [
        (["watchman", "--no-spawn", "--no-local", "--no-pretty", "version"], kwargs),
        (["watchman", "--no-spawn", "--no-local", "--no-pretty", "watch-list"], kwargs),
    ]


def test_no_watchman_for_the_agent_fails_naming_where_it_looks(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    env = dataclasses.replace(s.env, agent_watchman=lambda: None)
    folders = ", ".join(launchd.LAUNCHD_PATH.split(":"))
    assert doctor.check_watchman(env) == Check(
        "Watchman",
        "fail",
        f"Watchman isn't installed where the agent looks ({folders})",
        "brew install watchman",
    )
    assert s.run.argvs == []  # the shell's watchman isn't even tried


def test_only_the_shell_missing_watchman_is_a_warning(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    s.run.watchman = FileNotFoundError("watchman")
    assert doctor.check_watchman(s.env) == Check(
        "Watchman",
        "warn",
        "the agent can run /opt/homebrew/bin/watchman, but this shell can't: check your PATH",
        None,
    )


@pytest.mark.parametrize(
    ("result", "status", "message", "fix"),
    [
        (  # --no-local without a server: exit 1, no output
            subprocess.CompletedProcess([], 1, "", ""),
            "fail",
            "the server is not running (the daemon starts it when it connects)",
            "watchman version",
        ),
        (
            subprocess.CompletedProcess(
                [], 1, '{"error":"... command watch-list not available in this mode"}', ""
            ),
            "fail",
            "the server is not running (the daemon starts it when it connects)",
            "watchman version",
        ),
        (
            subprocess.TimeoutExpired(["watchman"], 5),
            "fail",
            "the server did not answer",
            "watchman version",
        ),
        (
            subprocess.CompletedProcess([], 0, "garbage", ""),
            "fail",
            "the server is not running (the daemon starts it when it connects)",
            "watchman version",
        ),
    ],
)
def test_watchman_down(tmp_dir: Path, result: Any, status: str, message: str, fix: str) -> None:
    s = scene(tmp_dir)
    s.run.watchman = result
    assert doctor.check_watchman(s.env) == Check("Watchman", status, message, fix)  # type: ignore[arg-type]


def test_a_server_that_answers_version_but_not_watch_list_fails(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    s.run.watch_list = subprocess.CompletedProcess([], 0, '{"error": "oops"}', "")
    assert doctor.check_watchman(s.env) == Check(
        "Watchman", "fail", "the server did not list its watches", "watchman version"
    )


def test_a_project_watchman_does_not_watch_is_a_warning(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    s.run.roots = ["/elsewhere", str(s.project) + "-not"]
    assert doctor.check_watchman(s.env) == Check(
        "Watchman", "warn", "2026.10.05.00; not watched: shop", "diffbell install"
    )
    s.run.roots = [str(s.project)]
    assert doctor.check_watchman(s.env).status == "ok"


def test_install_source(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    ok = doctor.check_install(s.env)
    assert ok.status == "ok" and ok.message.startswith(f"main @ abc1234 from {tmp_dir} (")
    write_source(s.env.state_dir, Source(str(tmp_dir / "gone"), "abc", "main", NOW))
    assert doctor.check_install(s.env) == Check(
        "Install",
        "warn",
        f"the recorded source {tmp_dir / 'gone'} is gone",
        "diffbell upgrade --from <repo>",
    )
    (s.env.state_dir / "install-source.json").unlink()
    assert doctor.check_install(s.env).message == "install source unknown (no install-source.json)"


def test_a_homebrew_install_is_its_own_source(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0021: Homebrew records the version; install-source.json doesn't apply."""
    s = scene(tmp_dir)
    (s.env.state_dir / "install-source.json").unlink()
    monkeypatch.setattr(launchd, "running_from_homebrew", lambda: Path("/opt/homebrew"))
    assert doctor.check_install(s.env) == Check(
        "Install", "ok", f"Homebrew (/opt/homebrew), diffbell {__version__}"
    )


# -- the pending source (Plan 7 spec §4) ------------------------------------------------


def pending(tmp_dir: Path) -> tuple[str, Path]:
    """A pending source whose folder exists (and has an "@" in its path)."""
    repo = tmp_dir / "w" / "a@b" / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"path": str(repo), "commit": "0123456789abcdef", "branch": "fix/a@b"})
    return raw, repo


def test_a_pending_source_is_judged_instead_of_the_record(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    raw, repo = pending(tmp_dir)
    env = dataclasses.replace(s.env, pending_source=raw)
    ok = Check("Install", "ok", f"installing fix/a@b @ 0123456 from {repo}")
    (s.env.state_dir / "install-source.json").unlink()  # the first upgrade: nothing recorded
    assert doctor.check_install(env) == ok
    write_source(s.env.state_dir, Source(str(tmp_dir / "gone"), "abc", "main", NOW))
    assert doctor.check_install(env) == ok  # the previous source is gone: not judged


def test_a_pending_source_whose_folder_is_gone_fails(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    gone = tmp_dir / "gone"
    raw = json.dumps({"path": str(gone), "commit": "0123456789abcdef", "branch": "main"})
    assert doctor.check_install(dataclasses.replace(s.env, pending_source=raw)) == Check(
        "Install",
        "fail",
        f"the source being installed {gone} is gone",
        "diffbell upgrade --from <repo>",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"a string"',
        '{"path": "/x", "commit": "c"}',
        '{"path": "", "commit": "c", "branch": "b"}',
        '{"path": "/x", "commit": 1, "branch": "b"}',
        '{"path": "/x", "commit": "c", "branch": null}',
    ],
)
def test_a_malformed_pending_source_fails_and_never_falls_back(tmp_dir: Path, raw: str) -> None:
    s = scene(tmp_dir)  # a valid install-source.json is there: it must not be used instead
    check = doctor.check_install(dataclasses.replace(s.env, pending_source=raw))
    assert check == Check(
        "Install",
        "fail",
        'the pending install source is not a JSON object with "path", "commit" and "branch"',
        "diffbell upgrade --from <repo>",
    )


def test_the_command_passes_the_pending_source_and_hides_the_option(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = scene(tmp_dir)
    raw, repo = pending(tmp_dir)
    monkeypatch.setattr(cli, "doctor_env", lambda: s.env)
    assert cli.main(["doctor", "--json", "--pending-source", raw]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["Install"]["message"] == f"installing fix/a@b @ 0123456 from {repo}"
    assert cli.main(["doctor", "--json", "--pending-source", "{"]) == 2
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["Install"]["status"] == "fail"
    with pytest.raises(SystemExit):
        cli.main(["doctor", "--help"])
    assert "pending" not in capsys.readouterr().out


def test_config(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    s.project.rmdir()
    assert doctor.check_config(s.env) == Check(
        "Config",
        "warn",
        "project folder missing: shop",
        "restore the folder, or remove the project in the web UI",
    )
    s.env.config_path.write_text("projects: [1, 2\n")
    bad = doctor.check_config(s.env)
    assert (bad.status, bad.fix) == ("fail", "fix the config file, then retry")
    assert "invalid YAML" in bad.message


def test_launchd(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    assert doctor.check_launchd(s.env).message == f"{launchd.LABEL} running (pid 4242)"
    assert ["launchctl", "print", f"gui/501/{launchd.LABEL}"] in s.run.argvs
    s.run.launchctl = subprocess.CompletedProcess([], 0, "state = waiting\n", "")
    assert doctor.check_launchd(s.env).message == f"{launchd.LABEL} is loaded but not running"
    s.run.launchctl = subprocess.CompletedProcess([], 113, "", "Could not find service")
    assert doctor.check_launchd(s.env).message == f"{launchd.LABEL} is not loaded"
    s.env.plist.unlink()
    assert doctor.check_launchd(s.env) == Check(
        "launchd agent", "fail", "io.688.diffbell.plist is missing", "diffbell install"
    )


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (None, "no status.json: the daemon never ran here"),
        (
            {"running": False, "clean_shutdown": True, "updated_at": NOW},
            "stopped (its last status.json says not running)",
        ),
        (
            {"running": True, "updated_at": NOW, "last_failure": "keychain read failed"},
            "last failure: keychain read failed",
        ),
        (
            {"running": True, "updated_at": NOW - 121},
            "status.json is stale (last update 121 s ago)",
        ),
        ({"running": True}, "status.json is stale (last update never)"),
    ],
)
def test_daemon_liveness(tmp_dir: Path, status: dict[str, Any] | None, message: str) -> None:
    s = scene(tmp_dir)
    (s.env.state_dir / "status.json").unlink()
    if status is not None:
        write_status_file(s.env.state_dir, status)
    check = doctor.check_liveness(s.env)
    log = s.env.log_dir / "diffbell.log"
    assert check == Check("Daemon", "fail", message, f"tail -n 50 {log}")


def test_the_database_is_read_only_and_unchanged(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    db = s.env.state_dir / "state.db"
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    assert doctor.check_database(s.env) == Check("Database", "ok", "schema v7, quick_check ok")
    assert doctor.check_problems(s.env).status == "ok"
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_a_relative_state_dir_still_opens_the_database(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = scene(tmp_dir)
    monkeypatch.chdir(s.env.state_dir.parent)
    env = dataclasses.replace(s.env, state_dir=Path(s.env.state_dir.name))
    assert doctor.check_database(env) == Check("Database", "ok", "schema v7, quick_check ok")


def test_an_older_database_is_not_migrated_yet_and_needs_a_restart(tmp_dir: Path) -> None:
    """Not yet opened by the v6 daemon: a restart migrates it. Restoring a backup
    would be wrong advice (it is older still)."""
    s = scene(tmp_dir)
    conn = sqlite3.connect(s.env.state_dir / "state.db")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()
    check = doctor.check_database(s.env)
    assert check == Check(
        "Database",
        "fail",
        "schema v5: not migrated yet; restart the daemon",
        "diffbell install",
    )


def test_a_newer_database_needs_a_backup_restored(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    conn = sqlite3.connect(s.env.state_dir / "state.db")
    conn.execute("PRAGMA user_version = 8")  # written by newer code
    conn.commit()
    conn.close()
    check = doctor.check_database(s.env)
    assert (check.status, check.message) == ("fail", "schema v8, this code needs v7")
    assert check.fix == doctor.DB_FIX


def test_no_database_is_a_failure_and_is_not_created(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    for name in ("state.db", "state.db-wal", "state.db-shm"):
        (s.env.state_dir / name).unlink(missing_ok=True)
    assert doctor.check_database(s.env).message == "no state.db yet"
    assert not (s.env.state_dir / "state.db").exists()


def test_open_problems(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    store = Store(s.env.state_dir / "state.db")
    store.put_problem(Problem("recrawl:shop", "warning", "shop", "shop was recrawled", None, 1.0))
    store.put_problem(Problem("restart", "notice", None, "restarted", None, 2.0))
    store.close()
    assert doctor.check_problems(s.env) == Check("Open problems", "warn", "shop was recrawled")
    store = Store(s.env.state_dir / "state.db")
    store.put_problem(
        Problem("watchman", "serious", None, "Watchman is unreachable", "watchman version", 3.0)
    )
    store.close()
    assert doctor.check_problems(s.env) == Check(
        "Open problems",
        "fail",
        "shop was recrawled; Watchman is unreachable",
        "watchman version",
    )


@pytest.mark.parametrize(
    ("absent", "marker", "message"),
    [
        ((ACCOUNT_BOT_TOKEN, ACCOUNT_CHAT_ID), None, "not set up"),
        ((ACCOUNT_CHAT_ID,), None, "not set up: the chat id is missing"),
        ((ACCOUNT_BOT_TOKEN,), None, "not set up: the bot token is missing"),
        ((), "1800000000.0", "not set up: an earlier save didn't finish"),
    ],
)
def test_keychain_not_set_up_is_a_warning(
    tmp_dir: Path, absent: tuple[str, ...], marker: str | None, message: str
) -> None:
    s = scene(tmp_dir)
    for account in absent:
        s.secrets[account] = None
    s.secrets[ACCOUNT_SAVE_PENDING] = marker
    expected = Check("Keychain", "warn", message, "diffbell setup")
    assert doctor.check_keychain(s.env) == expected


def test_an_unreadable_keychain_still_fails_the_check(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    s.secrets[ACCOUNT_BOT_TOKEN] = "raise"
    failed = by_name(run_checks(s.env))["Keychain"]
    assert failed == Check("Keychain", "fail", "the check failed (SecretStoreError)")


def test_telegram_without_a_token_is_a_warning_and_calls_nothing(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    s.secrets[ACCOUNT_BOT_TOKEN] = None
    called: list[str] = []
    s.env = dataclasses.replace(s.env, get_me=called.append)
    assert doctor.check_telegram(s.env) == Check(
        "Telegram", "warn", "not set up, nothing to check", "diffbell setup"
    )
    assert called == []


def test_telegram_with_a_save_pending_marker_calls_nothing(tmp_dir: Path) -> None:
    """The marker reads as not set up everywhere: no getMe on an untrusted token."""
    s = scene(tmp_dir)
    s.secrets[ACCOUNT_SAVE_PENDING] = "1800000000.0"
    called: list[str] = []
    s.env = dataclasses.replace(s.env, get_me=called.append)
    assert doctor.check_telegram(s.env) == Check(
        "Telegram", "warn", "not set up, nothing to check", "diffbell setup"
    )
    assert called == []


def test_an_unconfigured_install_only_warns(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not set up is a warning, never a failure (bot onboarding spec D7)."""
    s = scene(tmp_dir)
    for account in s.secrets:
        s.secrets[account] = None
    checks = by_name(run_checks(s.env))
    assert checks["Keychain"] == Check("Keychain", "warn", "not set up", "diffbell setup")
    assert checks["Telegram"] == Check(
        "Telegram", "warn", "not set up, nothing to check", "diffbell setup"
    )
    assert [c.name for c in checks.values() if c.status == "fail"] == []
    monkeypatch.setattr(cli, "doctor_env", lambda: s.env)
    assert cli.main(["doctor"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("11 ok · 2 warnings · 0 failures")


@pytest.mark.parametrize(
    ("kind", "status", "fix"),
    [
        (ErrorKind.AUTH, "fail", "diffbell setup"),
        (ErrorKind.BAD_REQUEST, "fail", "diffbell setup"),
        # A network blip must not fail an upgrade whose install worked.
        (ErrorKind.RETRYABLE, "warn", "check the network, then retry"),
    ],
)
def test_telegram_reports_the_kind_only(
    tmp_dir: Path, kind: ErrorKind, status: str, fix: str
) -> None:
    s = scene(tmp_dir)
    seen: list[str] = []

    def get_me(token: str) -> None:
        seen.append(token)
        raise TelegramError(kind, "getMe: Unauthorized")

    s.env = dataclasses.replace(s.env, get_me=get_me)
    assert doctor.check_telegram(s.env) == Check(
        "Telegram",
        status,  # type: ignore[arg-type]
        f"getMe failed ({kind})",
        fix,
    )
    assert seen == [TOKEN]


def test_web(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    s.env = dataclasses.replace(s.env, healthz=lambda port: None)
    assert doctor.check_web(s.env) == Check(
        "Web UI", "warn", "enabled, but nothing answers on port 8765", "diffbell install"
    )
    s.env = dataclasses.replace(s.env, healthz=lambda port: {})
    assert doctor.check_web(s.env).message == "port 8765 answers, but not as diffbell"
    s.env.config_path.write_text("web:\n  enabled: false\n")
    assert doctor.check_web(s.env) == Check("Web UI", "ok", "disabled")


def test_permissions_are_reported_never_changed(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    status = s.env.state_dir / "status.json"
    status.chmod(0o644)
    s.env.log_dir.chmod(0o755)
    check = doctor.check_permissions(s.env)
    assert check.status == "warn"
    assert check.message == "wider than 0700/0600: status.json 644, logs 755"
    assert (status.stat().st_mode & 0o777, s.env.log_dir.stat().st_mode & 0o777) == (0o644, 0o755)


def test_log_errors_since_the_last_start(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    log = s.env.log_dir / "diffbell.log"
    with log.open("a") as fh:
        fh.write("2026-10-10 10:01:00 ERROR x: Traceback (most recent call last):\n")
        fh.write("2026-10-10 10:02:00 ERROR x: database busy (another ...); retry\n")
    check = doctor.check_log(s.env)
    assert (check.status, check.message) == ("warn", "2 error line(s) since the last start")
    assert check.fix == (f"grep -n -E 'Traceback|database migration failed|database busy' {log}")


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\r", "\x0b", "\x0c", "\x1c"])
def test_a_logged_line_separator_cannot_fake_a_start(tmp_dir: Path, sep: str) -> None:
    """The log is split on "\\n" only: a path holding U+2028 (or "\\x85", "\\r", ...)
    logged inside an error must not read as a later start that hides the errors."""
    s = scene(tmp_dir)
    log = s.env.log_dir / "diffbell.log"
    fake = "2026-10-10 10:03:00 INFO diffbell.daemon: diffbell started with 1 project(s)"
    with log.open("a", encoding="utf-8", newline="") as fh:
        fh.write("2026-10-10 10:01:00 ERROR x: Traceback (most recent call last):\n")
        fh.write(f"2026-10-10 10:02:00 ERROR x: database busy on a{sep}{fake}\n")
    check = doctor.check_log(s.env)
    assert (check.status, check.message) == ("warn", "2 error line(s) since the last start")


def test_only_the_daemons_own_start_record_counts_as_a_start(tmp_dir: Path) -> None:
    """START_LINE inside another record's message (a logged path, say) is no start."""
    s = scene(tmp_dir)
    log = s.env.log_dir / "diffbell.log"
    with log.open("a") as fh:
        fh.write("2026-10-10 10:01:00 ERROR x: Traceback (most recent call last):\n")
        fh.write("2026-10-10 10:02:00 WARNING x: skipped diffbell started with 1\n")
    check = doctor.check_log(s.env)
    assert (check.status, check.message) == ("warn", "1 error line(s) since the last start")


def test_a_check_that_raises_or_hangs_fails_alone(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = scene(tmp_dir)
    release = threading.Event()

    def hangs(env: Env) -> Check:
        release.wait(10)
        return Check("Log", "ok", "late")

    def raises(env: Env) -> Check:
        raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/getMe")

    checks = list(doctor.CHECKS)
    checks[doctor.NAMES.index("Log")] = hangs
    checks[doctor.NAMES.index("Telegram")] = raises
    monkeypatch.setattr(doctor, "CHECKS", tuple(checks))
    s.env = dataclasses.replace(s.env, now=time.monotonic)
    try:
        result = by_name(run_checks(s.env, timeout=0.3))
    finally:
        release.set()
    assert result["Log"] == Check("Log", "fail", "timed out after 0.3 s")
    assert result["Telegram"] == Check("Telegram", "fail", "the check failed (RuntimeError)")
    assert result["Config"].status == "ok"


def test_a_telegram_check_that_runs_out_of_time_is_a_warning(tmp_dir: Path) -> None:
    """A black-holed network must not fail an upgrade whose install worked."""
    s = scene(tmp_dir)
    release = threading.Event()

    def slow(token: str) -> None:
        release.wait(10)

    s.env = dataclasses.replace(s.env, get_me=slow, now=time.monotonic)
    try:
        result = by_name(run_checks(s.env, timeout=0.3))
    finally:
        release.set()
    assert result["Telegram"] == Check(
        "Telegram",
        "warn",
        "Telegram did not answer in time (network?)",
        doctor.TELEGRAM_FIX[ErrorKind.RETRYABLE],
    )


def test_an_old_install_check_that_runs_out_of_time_is_a_warning(tmp_dir: Path) -> None:
    """A slow Keychain lookup for the old install must not fail an upgrade."""
    s = scene(tmp_dir)
    release = threading.Event()

    def slow() -> list[str]:
        release.wait(10)
        return []

    s.env = dataclasses.replace(s.env, old_install=slow, now=time.monotonic)
    try:
        result = by_name(run_checks(s.env, timeout=0.3))
    finally:
        release.set()
    check = result["Old install"]
    assert check.status == "warn" and check.fix is None
    assert check.message == "the check for a dev-watchman install timed out after 0.3 s"  # old-name


def test_a_telegram_auth_error_is_still_a_failure(tmp_dir: Path) -> None:
    s = scene(tmp_dir)

    def unauthorized(token: str) -> None:
        raise TelegramError(ErrorKind.AUTH, "getMe: Unauthorized")

    s.env = dataclasses.replace(s.env, get_me=unauthorized)
    assert by_name(run_checks(s.env))["Telegram"] == Check(
        "Telegram", "fail", "getMe failed (auth)", "diffbell setup"
    )


def test_get_me_gives_up_within_its_own_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The getMe budget is a total, shorter than the check's: a hung connection
    becomes a retryable error before run_checks gives up on the thread."""
    assert doctor.TELEGRAM_TIMEOUT <= doctor.CHECK_TIMEOUT - 2
    made: list[float] = []
    closed: list[bool] = []

    class Hangs:
        def __init__(self, token: str, chat: str | None, *, timeout: float) -> None:
            made.append(timeout)

        async def get_me(self) -> None:
            await asyncio.sleep(3600)

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(cli, "TelegramClient", Hangs)
    monkeypatch.setattr(doctor, "TELEGRAM_TIMEOUT", 0.2)
    started = time.monotonic()
    with pytest.raises(TelegramError) as caught:
        cli._get_me(TOKEN)
    assert time.monotonic() - started < 2
    assert caught.value.kind is ErrorKind.RETRYABLE
    assert TOKEN not in str(caught.value)
    assert made == [0.2] and closed == [True]


def test_the_command_prints_json_and_exits_2_on_a_failure(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = scene(tmp_dir)
    monkeypatch.setattr(cli, "doctor_env", lambda: s.env)
    assert cli.main(["doctor", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == list(doctor.NAMES)
    assert set(rows[0]) == {"name", "status", "message", "fix"}
    no_watchman = dataclasses.replace(s.env, agent_watchman=lambda: None)
    monkeypatch.setattr(cli, "doctor_env", lambda: no_watchman)
    assert cli.main(["doctor"]) == 2
    out = capsys.readouterr().out
    assert "✗ Watchman" in out and "    brew install watchman" in out
    assert out.rstrip().endswith("12 ok · 0 warnings · 1 failure")


def test_a_shell_only_missing_watchman_is_a_warning_in_both_outputs(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = scene(tmp_dir)
    s.run.watchman = FileNotFoundError("watchman")
    monkeypatch.setattr(cli, "doctor_env", lambda: s.env)
    message = "the agent can run /opt/homebrew/bin/watchman, but this shell can't: check your PATH"
    assert cli.main(["doctor"]) == 0
    lines = capsys.readouterr().out.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("! Watchman"))
    assert message in lines[at]
    assert not lines[at + 1].startswith("    ")  # no fix line
    assert cli.main(["doctor", "--json"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["Watchman"]["status"] == "warn" and rows["Watchman"]["fix"] is None


def test_the_real_environment_points_at_the_scratch_paths(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIFFBELL_STATE_DIR", str(tmp_dir / "s"))
    monkeypatch.setenv("DIFFBELL_CONFIG", str(tmp_dir / "c.yaml"))
    monkeypatch.setenv("DIFFBELL_LOG_DIR", str(tmp_dir / "l"))
    env = cli.doctor_env()
    assert (env.state_dir, env.config_path, env.log_dir) == (
        tmp_dir / "s",
        tmp_dir / "c.yaml",
        tmp_dir / "l",
    )
    assert env.uid == os.getuid() and env.run is subprocess.run


def test_an_old_install_left_behind_is_a_warning(tmp_dir: Path) -> None:
    s = scene(tmp_dir)
    env = dataclasses.replace(s.env, old_install=lambda: ["the agent", "Keychain items bot-token"])
    check = doctor.check_old_install(env)
    assert (check.status, check.fix) == ("warn", "diffbell install")
    left = "left from dev-watchman: the agent, Keychain items bot-token"  # old-name
    assert check.message == left
    assert "rm " not in (check.fix or "")  # never deleting by hand
    ok = doctor.check_old_install(s.env)
    assert (ok.status, ok.message) == ("ok", "nothing left from dev-watchman")  # old-name
    names = list(doctor.NAMES)
    assert names[names.index("Install") + 1] == "Old install"
    assert doctor.CHECKS[names.index("Old install")] is doctor.check_old_install


def test_doctor_under_a_scratch_override_never_looks_for_the_old_install(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from diffbell import rename_install

    monkeypatch.setenv("DIFFBELL_STATE_DIR", str(tmp_dir / "s"))
    monkeypatch.setattr(rename_install, "leftovers", lambda *a: pytest.fail("looked"))
    monkeypatch.setattr(rename_install.Tools, "real", lambda: pytest.fail("real tools"))
    seen: list[list[str] | None] = []

    def fake_checks(env: Env) -> list[Check]:
        seen.append(env.old_install())
        return [Check("Old install", "ok", "x")]

    monkeypatch.setattr(doctor, "run_checks", fake_checks)
    assert cli.main(["doctor"]) == 0
    assert seen == [None]
    none = dataclasses.replace(scene(tmp_dir).env, old_install=lambda: None)
    check = doctor.check_old_install(none)
    assert (check.status, check.message) == (
        "ok",
        "not checked (a DIFFBELL_* override is set)",
    )


def test_doctor_without_an_override_asks_leftovers_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    from diffbell import rename_install

    old, tools = object(), object()
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(rename_install, "for_install", lambda: (old, True))
    monkeypatch.setattr(rename_install.OldInstall, "real", lambda: old)
    monkeypatch.setattr(rename_install.Tools, "real", lambda: tools)

    def fake_leftovers(o: object, t: object) -> list[str]:
        calls.append((o, t))
        return ["the agent"]

    monkeypatch.setattr(rename_install, "leftovers", fake_leftovers)
    env = cli.doctor_env()
    assert calls == []  # building the Env looks at nothing
    assert env.old_install() == ["the agent"]
    assert calls == [(old, tools)]


def test_the_telegram_fix_map_covers_every_error_kind() -> None:
    assert set(doctor.TELEGRAM_FIX) == set(ErrorKind)
    assert doctor.TELEGRAM_FIX[ErrorKind.NOT_SET_UP] == "diffbell setup"
