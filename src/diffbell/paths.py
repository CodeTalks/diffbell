"""Filesystem locations. Environment overrides exist for tests and side-by-side runs."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)


def config_path() -> Path:
    raw = os.environ.get("DIFFBELL_CONFIG", "~/.config/diffbell/config.yaml")
    return Path(raw).expanduser()


def state_dir() -> Path:
    return Path(os.environ.get("DIFFBELL_STATE_DIR", "~/.local/share/diffbell")).expanduser()


def log_dir() -> Path:
    return Path(os.environ.get("DIFFBELL_LOG_DIR", "~/Library/Logs/diffbell")).expanduser()


# -- private permissions (ADR 0007, "Local file permissions") ------------------------
#
# diffbell's own directories are 0700 and its own files 0600. Creation sites set
# the mode explicitly; existing installs are narrowed here. Never a process-wide
# mask: child processes (git) would inherit it and write into the user's repos.

_ALLOWED = {"dir": 0o700, "file": 0o600}

STATE_FILES = (
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "status.json",
    "daemon.lock",
    "install-source.json",
)
LOG_FILES = (
    "diffbell.log",
    "diffbell.log.1",
    "diffbell.log.2",
    "diffbell.log.3",
    "launchd.err.log",
)


OWN_CONFIG_DIR = "diffbell"

Change = tuple[Path, int, int]  # (path, old mode, new mode)


def _loose(path: Path, kind: Literal["dir", "file"]) -> Change | None:
    """(path, mode, narrowed mode) if ``path`` is ours and wider than allowed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    is_kind = stat.S_ISDIR(st.st_mode) if kind == "dir" else stat.S_ISREG(st.st_mode)
    if not is_kind or st.st_uid != os.getuid():
        return None
    old = stat.S_IMODE(st.st_mode)
    new = old & _ALLOWED[kind]
    return None if new == old else (path, old, new)


def _tighten(path: Path, kind: Literal["dir", "file"]) -> Change | None:
    change = _loose(path, kind)
    if change is None:
        return None
    _, old, new = change
    os.chmod(path, new, follow_symlinks=False)
    log.info("tightened %s: %o -> %o", path, old, new)
    return (path, old, new)


def tighten(path: Path, *, kind: Literal["dir", "file"]) -> bool:
    """Clear the mode bits outside 0o700 (dir) or 0o600 (file). Tighten only.

    Never follows a symlink: a missing path, a symlink, a path of another type than
    ``kind`` or one not owned by this uid is skipped. Returns whether the mode
    changed. Raises OSError from ``lstat``/``chmod`` (other than a missing path)."""
    return _tighten(path, kind) is not None


def _best_effort(path: Path, kind: Literal["dir", "file"], changed: list[Change]) -> None:
    try:
        change = _tighten(path, kind)
    except OSError as exc:
        log.warning("could not tighten %s: %s", path, exc.strerror or type(exc).__name__)
        return
    if change is not None:
        changed.append(change)


def tighten_install(config_path: Path, state_dir: Path, log_dir: Path) -> list[Change]:
    """Narrow an existing install's own dirs to 0700 and files to 0600 (best effort).

    Returns the ``(path, old, new)`` modes it changed.

    Only diffbell's own paths, never a parent directory. The config dir is
    tightened only when it is ours (named ``diffbell``, like the default
    ``~/.config/diffbell``): a config placed in a shared dir (``~/dotfiles``,
    ``$HOME``) leaves that dir alone. A symlinked directory is skipped together with
    its files. The one deliberate exception is the config file: if it is a symlink
    (dotfiles), its resolved target file is tightened, because the content is
    diffbell's own; the target's directory is left alone. Never raises OSError:
    a failure is logged and the rest is still tightened."""
    changed: list[Change] = []
    for path, kind in own_paths(config_path, state_dir, log_dir):
        _best_effort(path, kind, changed)
    return changed


def own_paths(
    config_path: Path, state_dir: Path, log_dir: Path
) -> list[tuple[Path, Literal["dir", "file"]]]:
    """diffbell's own paths, in the order ``tighten_install`` narrows them: the
    config dir (only when named ``diffbell``), the config file's target, then
    each state and log dir that is not a symlink, with its files."""
    paths: list[tuple[Path, Literal["dir", "file"]]] = []
    if config_path.parent.name == OWN_CONFIG_DIR:
        paths.append((config_path.parent, "dir"))
    paths.append((Path(os.path.realpath(config_path)), "file"))
    for directory, names in ((state_dir, STATE_FILES), (log_dir, LOG_FILES)):
        if os.path.islink(directory):
            continue
        paths.append((directory, "dir"))
        paths += [(directory / name, "file") for name in names]
    return paths


def loose_modes(config_path: Path, state_dir: Path, log_dir: Path) -> list[Change]:
    """The own paths wider than 0700/0600, as ``(path, mode, allowed mode)``.
    Read-only (``doctor``): nothing is changed. Never raises OSError: a path that
    cannot be read is left out."""
    found: list[Change] = []
    for path, kind in own_paths(config_path, state_dir, log_dir):
        try:
            change = _loose(path, kind)
        except OSError:
            continue
        if change is not None:
            found.append(change)
    return found
