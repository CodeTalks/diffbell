"""`diffbell upgrade` (Plan 6 spec §4, ADR 0014): install from a checkout, back
up, restart, check.

Builds exactly what is on disk: git is only read (never pull, check out or commit).
It never opens the ``Store``; the backup reads the database with ``mode=ro``. A
failure is reported with the rollback commands and the backup, never rolled back
automatically. Every runner is injected (``Runners``).

Every import is at module level: step 4 replaces the installed tool's environment
while this process runs, so nothing may be imported from it afterwards.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import sqlite3
import subprocess
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from diffbell import launchd, liveness, rename_install
from diffbell.install_source import Source, read_source, write_source

GIT_TIMEOUT = 10.0
UV_TIMEOUT = 300.0
INSTALL_TIMEOUT = 60.0
DOCTOR_TIMEOUT = 60.0
WAIT_SECONDS = liveness.READY_WAIT_SECONDS
BACKUP_PREFIX = "state-upgrade-"
# Only files named exactly like the backups this command makes are ever pruned.
BACKUP_NAME = re.compile(r"state-upgrade-\d{8}T\d{6}Z\.db")
KEEP_BACKUPS = 3

Runner = Callable[..., subprocess.CompletedProcess[str]]


class UpgradeError(Exception):
    """A step failed. The message is user-facing."""


def _backup_db(source: Path, target: Path) -> None:
    """Copy ``source`` (opened read-only) into the existing empty ``target`` with
    SQLite's backup API: a consistent snapshot, even while the daemon writes."""
    # absolute(): a relative DIFFBELL_STATE_DIR has no file URI.
    src = sqlite3.connect(f"{source.absolute().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _say(line: str) -> None:
    """Print one line at once: the subprocesses (uv, install, doctor) write to the
    same stdout, so a buffered line would show up after their output (Plan 7 spec §4)."""
    print(line, flush=True)


def _read_plist() -> dict[str, Any]:
    with open(launchd.plist_path(), "rb") as fh:
        data = plistlib.load(fh)
    return data if isinstance(data, dict) else {}


@dataclass
class Runners:
    """The outside world, injected; tests pass fakes."""

    run: Runner = subprocess.run
    backup: Callable[[Path, Path], None] = _backup_db
    now: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    out: Callable[[str], None] = _say
    plist: Callable[[], dict[str, Any]] = _read_plist
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))


@dataclass(frozen=True, slots=True)
class Options:
    state_dir: Path
    from_path: Path | None = None
    allow_dirty: bool = False
    dry_run: bool = False
    no_backup: bool = False


@dataclass(frozen=True, slots=True)
class Checkout:
    path: Path
    branch: str
    commit: str

    @property
    def short(self) -> str:
        return self.commit[:7]


# -- the steps ----------------------------------------------------------------------


def resolve_source(options: Options, recorded: Source | None) -> Path:
    """Step 1: ``--from``, else the recorded source; it must be a diffbell checkout."""
    if options.from_path is not None:
        path = options.from_path.expanduser()
    elif recorded is not None:
        path = Path(recorded.path)
    else:
        raise UpgradeError("no install source recorded yet: pass --from PATH (a diffbell checkout)")
    path = Path(os.path.realpath(path))
    try:
        with open(path / "pyproject.toml", "rb") as fh:
            name = tomllib.load(fh).get("project", {}).get("name")
    except (OSError, tomllib.TOMLDecodeError):
        name = None
    if name != "diffbell":
        raise UpgradeError(f"{path} is not a diffbell checkout (no pyproject.toml naming it)")
    return path


def _git(runners: Runners, path: Path, *args: str) -> str:
    argv = ["git", "-C", str(path), *args]
    # GIT_OPTIONAL_LOCKS=0: `git status` must not even refresh the index file.
    env = {**runners.env, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = runners.run(
            argv, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False, env=env
        )
    except subprocess.TimeoutExpired:
        raise UpgradeError(f"git {args[0]} timed out after {GIT_TIMEOUT:g} s") from None
    except OSError as exc:  # no git binary, or not executable
        raise UpgradeError(f"cannot run git ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise UpgradeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def read_checkout(runners: Runners, path: Path, allow_dirty: bool) -> Checkout:
    """Step 2, read-only: the branch, the commit, and no uncommitted changes."""
    dirty = _git(runners, path, "status", "--porcelain").strip()
    commit = _git(runners, path, "rev-parse", "HEAD").strip()
    branch = _git(runners, path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if dirty and not allow_dirty:
        raise UpgradeError(
            f"{path} has uncommitted changes; commit them, or pass --allow-dirty "
            "to install them anyway"
        )
    return Checkout(path, branch, commit)


def backup(runners: Runners, state_dir: Path) -> Path | None:
    """Step 3: snapshot state.db into a new 0600 ``state-upgrade-<UTC>.db``, then
    keep the KEEP_BACKUPS newest (only that pattern is ever deleted). None if there
    is no database yet."""
    db = state_dir / "state.db"
    if not db.exists():
        return None
    stamp = datetime.fromtimestamp(runners.now(), UTC).strftime("%Y%m%dT%H%M%SZ")
    target = state_dir / f"{BACKUP_PREFIX}{stamp}.db"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise UpgradeError(f"cannot create the backup {target}: {exc.strerror}") from None
    os.close(fd)
    try:
        runners.backup(db, target)
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise UpgradeError(f"the backup failed: {exc}") from None
    backups = sorted(p for p in state_dir.iterdir() if BACKUP_NAME.fullmatch(p.name))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def _tool_bin(runners: Runners) -> Path:
    """Where `uv tool install` puts the diffbell executable."""
    try:
        result = runners.run(
            ["uv", "tool", "dir", "--bin"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeError(f"cannot run uv ({type(exc).__name__})") from None
    if result.returncode != 0 or not result.stdout.strip():
        raise UpgradeError(f"uv tool dir --bin failed: {result.stderr.strip()}")
    return Path(result.stdout.strip())


def install(runners: Runners, checkout: Checkout, env: dict[str, str]) -> None:
    """Step 4: `uv tool install --force --reinstall`, its output shown."""
    argv = ["uv", "tool", "install", "--force", "--reinstall", str(checkout.path)]
    try:
        result = runners.run(argv, timeout=UV_TIMEOUT, check=False, env=env)
    except subprocess.TimeoutExpired:
        raise UpgradeError(f"uv tool install timed out after {UV_TIMEOUT:g} s") from None
    except OSError as exc:
        raise UpgradeError(f"cannot run uv ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise UpgradeError(f"uv tool install failed (exit {result.returncode})")


def _tool_env(runners: Runners, bin_dir: Path) -> dict[str, str]:
    """The environment for the new tool: its bin dir first on PATH, so `install`
    finds (and launchd runs) the uv tool, never a worktree's .venv."""
    env = dict(runners.env)
    env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
    env.pop("VIRTUAL_ENV", None)
    return env


def restart(runners: Runners, executable: Path, env: dict[str, str]) -> float:
    """Step 5: the newly installed `diffbell install`. Returns when it started."""
    started = runners.now()
    try:
        result = runners.run(
            [str(executable), "install"], timeout=INSTALL_TIMEOUT, check=False, env=env
        )
    except subprocess.TimeoutExpired:
        raise UpgradeError(f"diffbell install timed out after {INSTALL_TIMEOUT:g} s") from None
    except OSError as exc:
        raise UpgradeError(f"cannot run {executable} ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise UpgradeError(f"diffbell install failed (exit {result.returncode})")
    return started


def check_plist(runners: Runners, executable: Path) -> None:
    """After step 5: the agent must run the uv tool's diffbell, never a
    worktree's .venv (``install`` uses whatever ``diffbell`` is first on PATH)."""
    try:
        arguments = runners.plist().get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise UpgradeError(f"cannot read the agent's plist ({type(exc).__name__})") from None
    first = arguments[0] if isinstance(arguments, list) and arguments else None
    if not isinstance(first, str) or os.path.realpath(first) != os.path.realpath(executable):
        raise UpgradeError(f"the agent runs {first}, not the uv tool's {executable}")


def wait_for_start(runners: Runners, state_dir: Path, started: float) -> None:
    """Step 6: status.json says the daemon started after step 5 and is ready (the
    shared `liveness.wait_until_ready`). The new `install` already waited for that,
    so this normally passes at once; it still guards a checkout whose `install`
    does not wait."""
    result = liveness.wait_until_ready(
        state_dir, started, monotonic=runners.monotonic, sleep=runners.sleep
    )
    if result.ready:
        return
    if not liveness.started_after(result.snapshot, started):
        raise UpgradeError(f"the daemon did not report a new start within {WAIT_SECONDS:g} s")
    raise UpgradeError(
        f"the daemon did not report ready within {WAIT_SECONDS:g} s: " + "; ".join(result.missing)
    )


def pending_source(checkout: Checkout) -> str:
    """The source being installed, for the new doctor's Install check: one JSON
    argument, so no character of a path or branch (an "@" included) can mis-split it."""
    return json.dumps(
        {"path": str(checkout.path), "commit": checkout.commit, "branch": checkout.branch}
    )


def _doctor(
    runners: Runners, argv: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return runners.run(
            argv, capture_output=True, text=True, timeout=DOCTOR_TIMEOUT, check=False, env=env
        )
    except subprocess.TimeoutExpired:
        raise UpgradeError(f"diffbell doctor timed out after {DOCTOR_TIMEOUT:g} s") from None
    except OSError as exc:
        raise UpgradeError(f"cannot run {argv[0]} ({type(exc).__name__})") from None


def check(
    runners: Runners, executable: Path, env: dict[str, str], checkout: Checkout
) -> tuple[int, list[str]]:
    """Step 7: the newly installed `diffbell doctor --json`, judging ``checkout``
    as the install source (install-source.json still names the previous one). A
    checkout from before Plan 7 has a doctor without --pending-source (argparse:
    exit 2, "unrecognized arguments: --pending-source"): it is run once more without
    the option. Any other failure is reported as it is. Returns (ok count, the
    failed checks as "name: message"); prints its summary."""
    plain = [str(executable), "doctor", "--json"]
    result = _doctor(runners, [*plain, "--pending-source", pending_source(checkout)], env)
    stderr = result.stderr or ""
    if (
        result.returncode == 2
        and "unrecognized arguments" in stderr
        and "--pending-source" in stderr
    ):
        result = _doctor(runners, plain, env)
    try:
        rows = json.loads(result.stdout)
        statuses = [(str(r["name"]), str(r["status"]), str(r["message"])) for r in rows]
    except (ValueError, KeyError, TypeError):
        raise UpgradeError("diffbell doctor printed no readable result") from None
    ok = sum(1 for _, s, _ in statuses if s == "ok")
    warn = sum(1 for _, s, _ in statuses if s == "warn")
    failed = [f"{n}: {m}" for n, s, m in statuses if s == "fail"]
    runners.out(f"Doctor: {ok} ok · {warn} warning(s) · {len(failed)} failure(s)")
    for name, status, message in statuses:
        if status != "ok":
            runners.out(f"  {'!' if status == 'warn' else '✗'} {name}: {message}")
    return ok, failed


def schema_version(db: Path) -> int | None:
    """``PRAGMA user_version`` of ``db``, read with ``mode=ro`` (never the Store:
    opening it migrates). None if there is no database or it can't be read."""
    if not db.exists():
        return None
    try:
        # absolute(): a relative DIFFBELL_STATE_DIR has no file URI.
        conn = sqlite3.connect(f"{db.absolute().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


# Schema versions whose migration only adds nullable columns (v7: events.last_error_kind
# and last_error_at). Code from before such a bump still runs on the result: it never
# lowers user_version, and it names every column it reads.
ADDITIVE_VERSIONS = frozenset({7})


def breaking_bump(before: int, now: int) -> bool:
    """True when going from schema ``before`` to ``now`` passed a version the previous
    code can't use (any version not in ADDITIVE_VERSIONS)."""
    return any(v not in ADDITIVE_VERSIONS for v in range(before + 1, now + 1))


def rollback_text(
    state_dir: Path,
    previous: Source | None,
    backup_path: Path | None,
    version_before: int | None = None,
) -> list[str]:
    """What to run to go back (never run automatically). ``version_before`` is the
    database's schema version before this upgrade: when the new daemon migrated it
    to a newer one, the previous code can't use it (an old daemon opens a v6
    database without error, then every per-project query fails), so restoring a
    backup is required, not optional (ADR 0016). A bump through ADDITIVE_VERSIONS
    only is said as such and keeps the restore optional."""
    lines = ["To roll back:"]
    generic = (
        "reinstall from the checkout you used before "
        "(uv tool install --force --reinstall PATH), then diffbell install"
    )
    if previous is not None and not Path(previous.path).is_dir():
        lines.append(f"  previous source gone ({previous.path}): {generic}")
    elif previous is not None:
        lines += [
            f"  uv tool install --force --reinstall {shlex.quote(previous.path)}",
            "  diffbell install",
        ]
    else:
        lines.append(f"  previous source unknown: {generic}")
    db = state_dir / "state.db"
    version_now = schema_version(db)
    bumped = version_before is not None and version_now is not None and version_before < version_now
    migrated = (
        bumped
        and version_before is not None
        and version_now is not None
        and breaking_bump(version_before, version_now)
    )

    def restore(source: Path) -> list[str]:
        return [
            "  diffbell uninstall",
            f"  cp {shlex.quote(str(source))} {shlex.quote(str(db))}",
            f"  rm -f {shlex.quote(str(db))}-wal {shlex.quote(str(db))}-shm",
            "  then reinstall as above",
        ]

    if migrated:
        # The store's own copy from before the v6 rewrite (Store._backup_before_v6).
        pre_v6 = sorted(state_dir.glob("state-pre-v6-*.db"), reverse=True)
        candidates = ([backup_path] if backup_path is not None else []) + pre_v6
        lines.append(
            f"The upgrade migrated the database from schema v{version_before} to "
            f"v{version_now}: the previous code cannot use it."
        )
        if backup_path is not None:
            lines.append(f"Database backup: {backup_path}")
        lines += [f"Pre-v6 backup: {path}" for path in pre_v6]
        if not candidates:
            lines.append(
                f"No backup of the schema v{version_before} database exists: "
                "the previous code cannot run on this database."
            )
            return lines
        lines.append(
            "Restoring one is REQUIRED before rolling back (loses the events since the backup):"
        )
        return lines + restore(candidates[0])
    if bumped:
        lines.append(
            f"The upgrade moved the database from schema v{version_before} to "
            f"v{version_now}, which only adds columns: the previous code still runs "
            "on it (its doctor reports the newer schema)."
        )
    if backup_path is None:
        lines.append("No database backup was made.")
        return lines
    lines += [
        f"Database backup: {backup_path}",
        "To restore it (loses the events since the backup):",
        *restore(backup_path),
    ]
    return lines


def refuse_an_old_install() -> None:
    """Rename spec section 3: an upgrade never runs over an old install that is still
    here (only `install` moves it). Existence only; never under a scratch override."""
    move = rename_install.for_install()
    if move is None:
        return
    # Like install: an unchecked Keychain (slow or locked) is not an old install.
    found = rename_install.leftovers(move[0], rename_install.Tools.real())
    left = [what for what in found if what != rename_install.KEYCHAIN_UNCHECKED]
    if left:
        what = ", ".join(left)
        raise UpgradeError(
            f"an old dev-watchman install is still here ({what}): "  # old-name
            "run `diffbell install` first, which moves it"
        )


def run_upgrade(options: Options, runners: Runners | None = None) -> int:
    """Steps 1-8. Exit code 0 on success (or a dry run), 2 on any failure."""
    runners = runners or Runners()
    out = runners.out
    try:
        refuse_an_old_install()
        if launchd.find_watchman() is None:
            raise UpgradeError(launchd.watchman_missing_message("run the upgrade again"))
    except UpgradeError as exc:
        out(f"Upgrade failed: {exc}")
        return 2
    previous = read_source(options.state_dir)
    try:
        path = resolve_source(options, previous)
        checkout = read_checkout(runners, path, options.allow_dirty)
    except UpgradeError as exc:
        out(f"Upgrade failed: {exc}")
        return 2
    out(f"Installing {checkout.branch} @ {checkout.short} from {checkout.path}")
    if options.dry_run:
        out("Dry run: would back up state.db, run uv tool install --force --reinstall,")
        out("restart the agent (diffbell install) and run diffbell doctor.")
        return 0
    backup_path: Path | None = None
    # Before anything runs: the new daemon may migrate the database (rollback_text).
    version_before = schema_version(options.state_dir / "state.db")
    try:
        if not options.no_backup:
            backup_path = backup(runners, options.state_dir)
            if backup_path is not None:
                out(f"Backed up the database to {backup_path}")
        bin_dir = _tool_bin(runners)
        env = _tool_env(runners, bin_dir)
        install(runners, checkout, env)
        executable = bin_dir / "diffbell"
        started = restart(runners, executable, env)
        check_plist(runners, executable)
        wait_for_start(runners, options.state_dir, started)
        ok, failed = check(runners, executable, env, checkout)
        if failed:
            raise UpgradeError("doctor reports " + "; ".join(failed))
    except UpgradeError as exc:
        out(f"Upgrade failed: {exc}")
        for line in rollback_text(options.state_dir, previous, backup_path, version_before):
            out(line)
        return 2
    write_source(
        options.state_dir,
        Source(str(checkout.path), checkout.commit, checkout.branch, runners.now()),
    )
    out(f"Upgraded to {checkout.branch} @ {checkout.short} · {ok} ok")
    return 0
