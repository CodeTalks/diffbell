"""`diffbell doctor`: one read-only health check (Plan 6 spec §3, ADR 0014).

Read-only: it never opens the ``Store`` (opening migrates the database), reads
SQLite with ``mode=ro``, never edits the config, never tightens permissions, never
sends a message and never prints a secret. It never starts Watchman: no
``WatchmanClient`` (pywatchman resolves its socket with ``watchman get-sockname``,
which starts a server), only ``watchman --no-spawn --no-local`` ``version`` and
``--no-pretty watch-list`` (``--no-local``: no server is exit 1, not the client's
own answer).
Every runner is injected (``Env``); ``cli.cmd_doctor`` builds the real one.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from diffbell import launchd, liveness, paths
from diffbell.config import ConfigError, load_and_resolve, load_config
from diffbell.diffing import split_lines
from diffbell.install_source import read_source
from diffbell.secrets import Missing, credentials
from diffbell.store import SCHEMA_VERSION
from diffbell.telegram import ErrorKind, TelegramError

Status = Literal["ok", "warn", "fail"]
Runner = Callable[..., subprocess.CompletedProcess[str]]

CHECK_TIMEOUT = 5.0  # seconds per check (they run side by side)
# getMe's total budget, left 2 s short of the check's for the Keychain read before
# it: a hung network becomes a retryable error (a warning), not a timed-out check.
TELEGRAM_TIMEOUT = CHECK_TIMEOUT - 2
STALE_AFTER = 120.0  # status.json older than this: the daemon is not ticking
LOG_ERRORS = ("Traceback", "database migration failed", "database busy")
START_LINE = "diffbell started with"
# The daemon's own start record ("<date> <time> INFO diffbell.daemon: " + START_LINE),
# not START_LINE inside another record's message (a logged path, say).
START_RECORD = re.compile(r"^\S+ \S+ INFO diffbell\.daemon: " + re.escape(START_LINE))
# --no-local: without a server the client exits 1 instead of answering itself.
VERSION = ["watchman", "--no-spawn", "--no-local", "--no-pretty", "version"]
WATCH_LIST = ["watchman", "--no-spawn", "--no-local", "--no-pretty", "watch-list"]
MARK: dict[str, str] = {"ok": "✓", "warn": "!", "fail": "✗"}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    message: str
    fix: str | None = None  # shown only when not ok


@dataclass(frozen=True, slots=True)
class Env:
    """Everything a check reads, injected (tests use fakes and scratch paths)."""

    config_path: Path
    state_dir: Path
    log_dir: Path
    run: Runner  # `watchman --no-spawn` and `launchctl print`
    secret: Callable[[str], str | None]  # Keychain read; raises SecretStoreError
    get_me: Callable[[str], None]  # Telegram getMe with this token; raises TelegramError
    healthz: Callable[[int], dict[str, Any] | None]  # /healthz on a port (cli._healthz)
    now: Callable[[], float]
    plist: Path
    uid: int
    agent_watchman: Callable[[], Path | None]  # launchd.find_watchman: the agent's PATH
    # `upgrade`'s hidden --pending-source (Plan 7 spec §4): the JSON object
    # {"path", "commit", "branch"} of the source being installed, judged instead of
    # install-source.json (which still names the previous one). None: doctor alone.
    pending_source: str | None = None
    # What is left of a dev-watchman install (rename_install.leftovers), as phrases;  # old-name
    # cli passes lambda: None (not checked) while a scratch override is set (never the
    # real Keychain).
    old_install: Callable[[], list[str] | None] = lambda: []


PENDING_INVALID = (
    'the pending install source is not a JSON object with "path", "commit" and "branch"'
)


def _pending(raw: str) -> tuple[str, str, str] | None:
    """(path, commit, branch) of a --pending-source value, or None if malformed."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    path, commit, branch = (data.get(key) for key in ("path", "commit", "branch"))
    if not all(isinstance(v, str) and v for v in (path, commit, branch)):
        return None
    return str(path), str(commit), str(branch)


def _q(path: Path) -> str:
    return shlex.quote(str(path))


# -- the checks --------------------------------------------------------------------


def check_install(env: Env) -> Check:
    fix = "diffbell upgrade --from <repo>"
    if env.pending_source is not None:
        # Run by `upgrade` before it records the new source: judge that one. A
        # malformed value fails; it never falls back to the old record.
        pending = _pending(env.pending_source)
        if pending is None:
            return Check("Install", "fail", PENDING_INVALID, fix)
        path, commit, branch = pending
        if not Path(path).is_dir():
            # fail, where a gone recorded source below is only a warn: this is the
            # checkout being installed right now, so the install itself is broken.
            return Check("Install", "fail", f"the source being installed {path} is gone", fix)
        return Check("Install", "ok", f"installing {branch} @ {commit[:7]} from {path}")
    source = read_source(env.state_dir)
    if source is None:
        return Check("Install", "warn", "install source unknown (no install-source.json)", fix)
    if not Path(source.path).is_dir():
        return Check("Install", "warn", f"the recorded source {source.path} is gone", fix)
    when = datetime.fromtimestamp(source.installed_at).strftime("%Y-%m-%d %H:%M")
    return Check(
        "Install", "ok", f"{source.branch} @ {source.commit[:7]} from {source.path} ({when})"
    )


OLD_INSTALL_NAME = "dev-watchman"  # old-name


def check_old_install(env: Env) -> Check:
    """Rename spec section 3: anything left of the old install. Existence only (no
    Keychain value is read); the fix is the move itself, never deleting by hand."""
    left = env.old_install()
    if left is None:
        return Check("Old install", "ok", "not checked (a DIFFBELL_* override is set)")
    if not left:
        return Check("Old install", "ok", f"nothing left from {OLD_INSTALL_NAME}")
    message = f"left from {OLD_INSTALL_NAME}: " + ", ".join(left)
    return Check("Old install", "warn", message, "diffbell install")


def check_config(env: Env) -> Check:
    try:
        _, projects, missing = load_and_resolve(env.config_path)
    except ConfigError as exc:
        return Check("Config", "fail", str(exc).splitlines()[0], "fix the config file, then retry")
    if missing:
        names = ", ".join(m.name for m in missing)
        return Check(
            "Config",
            "warn",
            f"project folder missing: {names}",
            "restore the folder, or remove the project in the web UI",
        )
    return Check("Config", "ok", f"{len(projects)} project(s)")


def _ask_watchman(env: Env, argv: list[str]) -> dict[str, Any] | None:
    """The server's JSON reply, or None (no server, an error or an odd reply).
    Raises FileNotFoundError (no binary) and TimeoutExpired."""
    result = env.run(
        argv,
        capture_output=True,
        text=True,
        timeout=CHECK_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        return None
    return reply if isinstance(reply, dict) and "error" not in reply else None


def check_watchman(env: Env) -> Check:
    agent = env.agent_watchman()
    if agent is None:
        folders = ", ".join(launchd.LAUNCHD_PATH.split(":"))
        return Check(
            "Watchman",
            "fail",
            f"Watchman isn't installed where the agent looks ({folders})",
            "brew install watchman",
        )
    try:
        reply = _ask_watchman(env, VERSION)
        listing = _ask_watchman(env, WATCH_LIST) if reply is not None else None
    except FileNotFoundError:
        return Check(
            "Watchman", "warn", f"the agent can run {agent}, but this shell can't: check your PATH"
        )
    except subprocess.TimeoutExpired:
        return Check("Watchman", "fail", "the server did not answer", "watchman version")
    if reply is None:
        return Check(
            "Watchman",
            "fail",
            "the server is not running (the daemon starts it when it connects)",
            "watchman version",
        )
    raw = listing.get("roots") if listing is not None else None
    if not isinstance(raw, list):
        return Check("Watchman", "fail", "the server did not list its watches", "watchman version")
    roots = [str(r) for r in raw]
    version = reply.get("version", "?")
    try:
        _, projects, _ = load_and_resolve(env.config_path)
    except ConfigError:
        projects = []
    unwatched = [
        p.name
        for p in projects
        if not any(str(p.path) == r or str(p.path).startswith(r + "/") for r in roots)
    ]
    if unwatched:
        return Check(
            "Watchman",
            "warn",
            f"{version}; not watched: {', '.join(unwatched)}",
            "diffbell install",
        )
    return Check("Watchman", "ok", f"{version}, {len(roots)} root(s)")


def check_launchd(env: Env) -> Check:
    fix = "diffbell install"
    if not env.plist.exists():
        return Check("launchd agent", "fail", f"{env.plist.name} is missing", fix)
    loaded, pid = launchd.agent_status(uid=env.uid, runner=env.run, timeout=CHECK_TIMEOUT)
    if not loaded:
        return Check("launchd agent", "fail", f"{launchd.LABEL} is not loaded", fix)
    if pid is None:
        return Check("launchd agent", "fail", f"{launchd.LABEL} is loaded but not running", fix)
    return Check("launchd agent", "ok", f"{launchd.LABEL} running (pid {pid})")


def check_liveness(env: Env) -> Check:
    log = env.log_dir / "diffbell.log"
    fix = f"tail -n 50 {_q(log)}"
    status = liveness.read_status(env.state_dir)
    if status is None:
        return Check("Daemon", "fail", "no status.json: the daemon never ran here", fix)
    if status.get("running") is not True:
        return Check("Daemon", "fail", "stopped (its last status.json says not running)", fix)
    failure = status.get("last_failure")
    if isinstance(failure, str) and failure:
        return Check("Daemon", "fail", f"last failure: {failure}", fix)
    updated = status.get("updated_at")
    age = env.now() - float(updated) if isinstance(updated, int | float) else None
    if age is None or age > STALE_AFTER:
        shown = "never" if age is None else f"{age:.0f} s ago"
        return Check("Daemon", "fail", f"status.json is stale (last update {shown})", fix)
    return Check("Daemon", "ok", f"running (pid {status.get('pid')}, updated {age:.0f} s ago)")


def _read_only(db: Path) -> sqlite3.Connection:
    """The database read-only: never created, never migrated, never written."""
    # absolute(): a relative DIFFBELL_STATE_DIR has no file URI.
    return sqlite3.connect(f"{db.absolute().as_uri()}?mode=ro", uri=True, timeout=CHECK_TIMEOUT)


DB_FIX = (
    "stop the agent (diffbell uninstall), copy a backup "
    "(state-upgrade-*.db in the state dir) over state.db, remove state.db-wal and "
    "state.db-shm, then diffbell install"
)


def check_database(env: Env) -> Check:
    db = env.state_dir / "state.db"
    if not db.exists():
        return Check("Database", "fail", "no state.db yet", "diffbell install")
    conn = _read_only(db)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if version < SCHEMA_VERSION:
        # The daemon migrates on start; a backup would be older still.
        message = f"schema v{version}: not migrated yet; restart the daemon"
        return Check("Database", "fail", message, "diffbell install")
    if version > SCHEMA_VERSION:
        return Check(
            "Database", "fail", f"schema v{version}, this code needs v{SCHEMA_VERSION}", DB_FIX
        )
    if quick != "ok":
        return Check("Database", "fail", f"quick_check: {quick}", DB_FIX)
    if orphans:
        return Check("Database", "fail", f"foreign_key_check: {len(orphans)} row(s)", DB_FIX)
    return Check("Database", "ok", f"schema v{version}, quick_check ok")


def check_problems(env: Env) -> Check:
    db = env.state_dir / "state.db"
    if not db.exists():
        return Check("Open problems", "ok", "none (no database yet)")
    conn = _read_only(db)
    try:
        rows = conn.execute(
            "SELECT severity, message, fix FROM problems "
            "WHERE severity != 'notice' AND resolved_at IS NULL ORDER BY opened_at"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return Check("Open problems", "ok", "none")
    serious = [r for r in rows if r[0] == "serious"]
    message = "; ".join(str(r[1]) for r in rows)
    fixes = "\n".join(str(r[2]) for r in rows if r[2])
    return Check("Open problems", "fail" if serious else "warn", message, fixes or None)


def check_keychain(env: Env) -> Check:
    """The same gate as `run` (``secrets.credentials``): both items present and no
    unfinished save. Not set up is a warning, never a failure (bot onboarding spec
    D7), so `upgrade` passes on an install without a bot. A read error raises: the
    check then fails with the exception's type name (``run_checks``)."""
    state = credentials(get=env.secret)
    if isinstance(state, Missing):
        return Check("Keychain", "warn", state.message, "diffbell setup")
    return Check("Keychain", "ok", "bot token and chat id present")


TELEGRAM_FIX = {
    ErrorKind.AUTH: "diffbell setup",
    ErrorKind.RETRYABLE: "check the network, then retry",
    ErrorKind.BAD_REQUEST: "diffbell setup",
    ErrorKind.NOT_SET_UP: "diffbell setup",
}


def check_telegram(env: Env) -> Check:
    state = credentials(get=env.secret)
    if isinstance(state, Missing):
        return Check("Telegram", "warn", "not set up, nothing to check", "diffbell setup")
    try:
        env.get_me(state.token)
    except TelegramError as exc:
        kind = ErrorKind(exc.kind)
        # A network blip, a timeout or a 5xx is a warning: it must not fail an
        # upgrade whose install worked. A bad token is a failure.
        status: Status = "warn" if kind is ErrorKind.RETRYABLE else "fail"
        return Check("Telegram", status, f"getMe failed ({kind.value})", TELEGRAM_FIX[kind])
    return Check("Telegram", "ok", "getMe ok (no message sent; `diffbell test` sends one)")


def check_web(env: Env) -> Check:
    try:
        config = load_config(env.config_path)
    except ConfigError:
        return Check("Web UI", "ok", "skipped (the config does not load)")
    if not config.web.enabled:
        return Check("Web UI", "ok", "disabled")
    port = config.web.port
    body = env.healthz(port)
    if body is None:
        return Check(
            "Web UI", "warn", f"enabled, but nothing answers on port {port}", "diffbell install"
        )
    if body.get("ok") is not True:
        return Check(
            "Web UI",
            "warn",
            f"port {port} answers, but not as diffbell",
            "diffbell install",
        )
    return Check("Web UI", "ok", f"answering on 127.0.0.1:{port}")


def check_permissions(env: Env) -> Check:
    loose = paths.loose_modes(env.config_path, env.state_dir, env.log_dir)
    if loose:
        shown = ", ".join(f"{p.name} {old:o}" for p, old, _ in loose)
        return Check(
            "Permissions",
            "warn",
            f"wider than 0700/0600: {shown}",
            "restart the daemon (diffbell install); it tightens on start",
        )
    return Check("Permissions", "ok", "own dirs 0700, own files 0600")


def check_log(env: Env) -> Check:
    log = env.log_dir / "diffbell.log"
    try:
        # Split on "\n" only (bytes: read_text would turn "\r" into "\n"): a logged
        # U+2028, "\x85" or "\r" must not start a line that reads as a later start.
        lines = split_lines(log.read_bytes().decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return Check("Log", "ok", "no log yet")
    starts = [i for i, line in enumerate(lines) if START_RECORD.match(line)]
    since = lines[starts[-1] :] if starts else lines
    errors = sum(1 for line in since if any(e in line for e in LOG_ERRORS))
    if errors:
        return Check(
            "Log",
            "warn",
            f"{errors} error line(s) since the last start",
            f"grep -n -E {shlex.quote('|'.join(LOG_ERRORS))} {_q(log)}",
        )
    return Check("Log", "ok", "no error lines since the last start")


CHECKS: tuple[Callable[[Env], Check], ...] = (
    check_install,
    check_old_install,
    check_config,
    check_watchman,
    check_launchd,
    check_liveness,
    check_database,
    check_problems,
    check_keychain,
    check_telegram,
    check_web,
    check_permissions,
    check_log,
)
NAMES = (
    "Install",
    "Old install",
    "Config",
    "Watchman",
    "launchd agent",
    "Daemon",
    "Database",
    "Open problems",
    "Keychain",
    "Telegram",
    "Web UI",
    "Permissions",
    "Log",
)


def run_checks(env: Env, timeout: float = CHECK_TIMEOUT) -> list[Check]:
    """Every check, side by side, each given ``timeout`` seconds. A check that
    raises or times out fails with the exception's type name only (its text may
    carry a URL or a secret); the others still run. A Telegram check that times out
    is a warning, like any retryable Telegram error (the network, most likely)."""
    results: list[Check | None] = [None] * len(CHECKS)

    def runner(index: int) -> None:
        name = NAMES[index]
        try:
            results[index] = CHECKS[index](env)
        except Exception as exc:
            results[index] = Check(name, "fail", f"the check failed ({type(exc).__name__})")

    threads = [
        threading.Thread(target=runner, args=(i,), name=f"doctor-{i}", daemon=True)
        for i in range(len(CHECKS))
    ]
    deadline = env.now() + timeout
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - env.now()))
    return [
        result if result is not None else _timed_out(NAMES[i], timeout)
        for i, result in enumerate(results)
    ]


def _timed_out(name: str, timeout: float) -> Check:
    if name == "Telegram":
        return Check(
            name,
            "warn",
            "Telegram did not answer in time (network?)",
            TELEGRAM_FIX[ErrorKind.RETRYABLE],
        )
    if name == "Old install":  # a slow Keychain lookup, like Telegram's network
        message = f"the check for a {OLD_INSTALL_NAME} install timed out after {timeout:g} s"
        return Check(name, "warn", message)
    return Check(name, "fail", f"timed out after {timeout:g} s")


def summary(checks: list[Check]) -> str:
    def count(status: Status, word: str) -> str:
        n = sum(1 for c in checks if c.status == status)
        return f"{n} {word}{'' if n == 1 or word == 'ok' else 's'}"

    return f"{count('ok', 'ok')} · {count('warn', 'warning')} · {count('fail', 'failure')}"


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    out: list[str] = []
    for c in checks:
        out.append(f"{MARK[c.status]} {c.name.ljust(width)}  {c.message}")
        if c.status != "ok" and c.fix:
            out += [f"    {line}" for line in c.fix.splitlines()]
    out.append(summary(checks))
    return "\n".join(out)


def to_json(checks: list[Check]) -> str:
    return json.dumps([asdict(c) for c in checks], indent=2, ensure_ascii=False)


def exit_code(checks: list[Check]) -> int:
    return 2 if any(c.status == "fail" for c in checks) else 0
