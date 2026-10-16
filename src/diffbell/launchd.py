"""launchd LaunchAgent install/uninstall (spec §9)."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

LABEL = "io.688.diffbell"
# Must cover Homebrew (watchman) and the system dirs (git, security).
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[..., str | None]


def watchman_on_agent_path(which: Which = shutil.which) -> Path | None:
    """The `watchman` the agent would run (its plist's PATH is LAUNCHD_PATH), or None."""
    found = which("watchman", path=LAUNCHD_PATH)
    return Path(found) if found else None


def watchman_missing_message(then: str) -> str:
    """The one sentence `install` and `upgrade` print when the agent can't find Watchman."""
    folders = ", ".join(LAUNCHD_PATH.split(":"))
    return (
        f"Watchman isn't installed where the agent looks ({folders}): "
        f"`brew install watchman`, then {then}."
    )


# The name every caller uses (cli: install, doctor); tests replace this attribute.
find_watchman: Callable[[], Path | None] = watchman_on_agent_path


class LaunchdError(Exception):
    pass


def plist_path() -> Path:
    return Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()


def find_executable() -> Path:
    found = shutil.which("diffbell")
    if found is None:
        raise LaunchdError("diffbell is not on PATH; run `uv tool install .` first")
    return Path(found).resolve()


def build_plist(executable: Path, log_dir: Path) -> dict[str, Any]:
    if not executable.is_absolute():
        raise ValueError(f"launchd needs an absolute executable path, got {executable}")
    crash_log = str(log_dir / "launchd.err.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(executable),
            "run",
            "--log-file",
            str(log_dir / "diffbell.log"),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Standard",
        "ThrottleInterval": 10,
        "EnvironmentVariables": {"PATH": LAUNCHD_PATH},
        "StandardOutPath": crash_log,
        "StandardErrorPath": crash_log,
    }


def _prepare_crash_log(log_dir: Path) -> None:
    """Create or empty ``launchd.err.log``, owner-only. Never follows a symlink and
    never truncates a file that is not a regular file of ours. Every OSError becomes a
    LaunchdError (no traceback in `install`). If launchd ever recreates the file
    wider, the next daemon start tightens it (paths.tighten_install)."""
    path = log_dir / "launchd.err.log"
    try:
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise LaunchdError(f"cannot prepare {path}: not a regular file of ours")
            os.ftruncate(fd, 0)
            os.fchmod(fd, stat.S_IMODE(st.st_mode) & 0o600)
        finally:
            os.close(fd)
    except OSError as exc:
        raise LaunchdError(f"cannot prepare {path}: {exc.strerror}") from None


def bootout(label: str = LABEL, *, uid: int | None = None, runner: Runner = subprocess.run) -> bool:
    """Ask launchd to stop and unload ``label``. True if launchctl said yes. Its exit
    status is never an error: "no such service" is the normal answer for an agent that
    isn't loaded, and whether one is gone is decided from `launchctl print`
    (``agent_status``). Asynchronous: the agent may still be running on return."""
    uid = os.getuid() if uid is None else uid
    result = runner(
        ["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, check=False
    )
    return result.returncode == 0


def bootstrap(plist: Path, *, uid: int | None = None, runner: Runner = subprocess.run) -> None:
    """Load the agent from ``plist``, retrying (bootout is asynchronous; bootstrap can
    race it). Raises LaunchdError with launchctl's last message."""
    uid = os.getuid() if uid is None else uid
    last_error = ""
    attempts = 3
    for attempt in range(attempts):
        result = runner(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = (result.stderr or "").strip()
        if attempt < attempts - 1:
            time.sleep(1 + attempt)
    raise LaunchdError(f"launchctl bootstrap failed: {last_error}")


def install(
    executable: Path,
    *,
    log_dir: Path,
    plist: Path | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> Path:
    target = plist or plist_path()
    uid = os.getuid() if uid is None else uid
    _prepare_crash_log(log_dir)
    bootout(LABEL, uid=uid, runner=runner)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as fh:
        plistlib.dump(build_plist(executable, log_dir), fh)
    bootstrap(target, uid=uid, runner=runner)
    return target


_PID_LINE = re.compile(r"^\s*pid = (\d+)\s*$", re.MULTILINE)


def agent_status(
    *,
    label: str = LABEL,
    uid: int | None = None,
    runner: Runner = subprocess.run,
    timeout: float | None = None,
) -> tuple[bool, int | None]:
    """(loaded, pid) of the LaunchAgent from `launchctl print` (read-only): pid None
    when it is not running. (False, None) if launchctl cannot be run."""
    uid = os.getuid() if uid is None else uid
    try:
        result = runner(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if result.returncode != 0:
        return False, None
    match = _PID_LINE.search(result.stdout or "")
    return True, int(match.group(1)) if match else None


def agent_pid(*, uid: int | None = None, runner: Runner = subprocess.run) -> int | None:
    """The pid of the running LaunchAgent (`launchctl print`), or None if it is not
    loaded, not running, or launchctl cannot be run."""
    return agent_status(uid=uid, runner=runner)[1]


def uninstall(
    *, plist: Path | None = None, uid: int | None = None, runner: Runner = subprocess.run
) -> bool:
    """Boot the agent out and delete its plist. True if anything was installed."""
    target = plist or plist_path()
    booted = bootout(LABEL, uid=uid, runner=runner)
    existed = target.exists()
    target.unlink(missing_ok=True)
    return booted or existed
