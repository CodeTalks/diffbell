"""Moving an existing dev-watchman install to Diffbell (rename spec §3).

`diffbell install` calls run() before its usual steps: it stops the old agent, moves
the three folders, renames the log files, copies the Telegram credentials to the new
Keychain service, follows a moved checkout, then installs and only after the new
agent runs deletes the old plist and Keychain items. The only code that names the
old install; it acts on the real one only with switchover=True, which only `install`
passes and only without a scratch override (rename spec §3).
"""

from __future__ import annotations

import os
import pwd
import sqlite3
import subprocess
import time
import tomllib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from diffbell import launchd, paths, secrets
from diffbell.install_source import Source, read_source, write_source
from diffbell.lock import LockUnreadable, is_held
from diffbell.secrets import (
    ACCOUNT_BOT_TOKEN,
    ACCOUNT_CHAT_ID,
    ACCOUNT_SAVE_PENDING,
    Runner,
    SecretStoreError,
)
from diffbell.store import MigrationError, ProjectPathTaken, Store
from diffbell.web import config_edit

OLD_NAME = "dev-watchman"
OLD_LABEL = "io.688.dev-watchman"
OLD_SERVICE = OLD_LABEL
SCRATCH_SUFFIX = "-scratch"
SCRATCH_LABEL = OLD_LABEL + SCRATCH_SUFFIX
# A scratch root must hold this file (scripts/live/rename-check.sh writes it).
SCRATCH_MARKER = ".diffbell-rename-scratch"
NEW_NAME = "diffbell"
OVERRIDES = ("DIFFBELL_CONFIG", "DIFFBELL_STATE_DIR", "DIFFBELL_LOG_DIR", secrets.SERVICE_ENV)
RENAME_FROM_ENV = "DIFFBELL_RENAME_FROM"
ACCOUNTS = (ACCOUNT_BOT_TOKEN, ACCOUNT_CHAT_ID, ACCOUNT_SAVE_PENDING)
STOP_TIMEOUT = 10.0
POLL = 0.25
# tests/conftest.py sets it False: no test ever reaches the real move.
REAL_SWITCHOVER_ALLOWED = True


class RenameError(Exception):
    """A step failed; the message names the step and what to do."""


def _login_home() -> Path:
    """The login user's home from the password database: $HOME can't steer it."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


@dataclass(frozen=True, slots=True)
class OldInstall:
    label: str
    service: str
    plist: Path
    config_dir: Path
    state_dir: Path
    log_dir: Path
    root: Path | None = None  # set only for a scratch install (scratch())

    @classmethod
    def real(cls) -> OldInstall:
        home = _login_home()
        return cls(
            label=OLD_LABEL,
            service=OLD_SERVICE,
            plist=home / "Library" / "LaunchAgents" / f"{OLD_LABEL}.plist",
            config_dir=home / ".config" / OLD_NAME,
            state_dir=home / ".local" / "share" / OLD_NAME,
            log_dir=home / "Library" / "Logs" / OLD_NAME,
        )

    @classmethod
    def scratch(cls, root: Path) -> OldInstall:
        """The old-named scratch install `scripts/live/rename-check.sh` builds."""
        if not root.is_absolute():
            raise RenameError("the scratch root must be an absolute path")
        label = SCRATCH_LABEL
        return cls(
            label=label,
            service=label,
            plist=root / "LaunchAgents" / f"{label}.plist",
            config_dir=root / "config" / OLD_NAME,
            state_dir=root / "state",
            log_dir=root / "logs",
            root=root,
        )

    @property
    def plist_aside(self) -> Path:
        """Where step 1 puts the stopped agent's plist, so a re-login can't start a
        half-moved old install (which would recreate its folders). One level above
        LaunchAgents (launchd may load any file there at login), on the same volume:
        ~/Library/<label>.plist.moving, or <root>/<label>.plist.moving for a scratch
        install. _restart_old puts it back; step 7 (clean_up) deletes it."""
        return self.plist.parent.parent / (self.plist.name + ".moving")

    def folders(self) -> tuple[Path, Path, Path]:
        return self.config_dir, self.state_dir, self.log_dir


@dataclass(frozen=True, slots=True)
class New:
    config_path: Path
    state_dir: Path
    log_dir: Path

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    @classmethod
    def current(cls) -> New:
        return cls(paths.config_path(), paths.state_dir(), paths.log_dir())

    def folders(self) -> tuple[Path, Path, Path]:
        return self.config_dir, self.state_dir, self.log_dir


@dataclass(frozen=True)
class Tools:
    """The runners are required: only Tools.real() (cli) uses the real ones."""

    launchctl: Runner
    security: Runner
    uid: int = field(default_factory=os.getuid)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = print

    @classmethod
    def real(cls) -> Tools:
        return cls(launchctl=subprocess.run, security=subprocess.run)


FOLDER_NAMES = ("config folder", "state folder", "log folder")
KEYCHAIN_UNCHECKED = "Keychain items (couldn't check)"


def leftovers(old: OldInstall, tools: Tools) -> list[str]:
    """What is left of the old install, as phrases (doctor's row, detection). Reads no
    Keychain value: `secrets.has_item` only checks existence."""
    agent = old.plist.exists() or old.plist_aside.exists()
    found = ["the agent"] if agent else []
    for what, folder in zip(FOLDER_NAMES, old.folders(), strict=True):
        if folder.exists():
            found.append(f"the {what} {folder}")
    try:
        items = [
            a for a in ACCOUNTS if secrets.has_item(a, service=old.service, runner=tools.security)
        ]
    except SecretStoreError:
        found.append(KEYCHAIN_UNCHECKED)
    else:
        if items:
            found.append("Keychain items " + ", ".join(items))
    return found


def _key(path: Path) -> str:
    """A path as the disk compares it: resolved, case-insensitive (APFS default) and
    normalization-insensitive (APFS treats the NFC and NFD spellings as one name)."""
    return unicodedata.normalize(
        "NFD", unicodedata.normalize("NFD", os.path.realpath(path)).casefold()
    )


def _parts(path: Path) -> list[str]:
    return [part for part in _key(path).split(os.sep) if part]


def _inside(path: Path, folder: Path) -> bool:
    """Whether ``path`` is ``folder`` or inside it, by whole components."""
    inner, outer = _parts(path), _parts(folder)
    return inner[: len(outer)] == outer


def _related(a: Path, b: Path) -> bool:
    return _inside(a, b) or _inside(b, a)


def _real_new() -> tuple[Path, Path, Path]:
    """The real Diffbell defaults, from the login home (never the overrides)."""
    home = _login_home()
    return (
        home / ".config" / NEW_NAME,
        home / ".local" / "share" / NEW_NAME,
        home / "Library" / "Logs" / NEW_NAME,
    )


def _real_paths() -> list[Path]:
    real = OldInstall.real()
    plist = _login_home() / "Library" / "LaunchAgents" / f"{launchd.LABEL}.plist"
    return [*real.folders(), real.plist, real.plist_aside, *_real_new(), plist]


REAL = "refusing to touch the real dev-watchman install outside the switchover"


def _check_switchover() -> None:
    overridden = [name for name in OVERRIDES if name in os.environ]
    if overridden:
        raise RenameError(f"refusing to move the real install while {', '.join(overridden)} is set")
    if not REAL_SWITCHOVER_ALLOWED:
        raise RenameError("moving the real install is disabled here (tests)")


def check_home() -> None:
    """The switchover only: refuse a $HOME that isn't the login home. run() checks it
    once an old install was found (so a foreign HOME with nothing to move installs
    normally), and again before each folder rename."""
    home = os.environ.get("HOME")
    if home is not None and os.path.realpath(home) != os.path.realpath(_login_home()):
        raise RenameError(
            f"refusing to move the real install: HOME is not your login home ({home})"
        )


def check_guard(old: OldInstall, *, switchover: bool) -> None:
    """Refuse anything that could touch the real old install unless this is the
    switchover; refuse the switchover under a scratch override or in tests (a foreign
    $HOME is check_home's, once there is something to move). A scratch move must be a marked scratch root that holds every path, holds
    no home, and touches no real old or new path (compared resolved and casefolded)."""
    if switchover:
        _check_switchover()
        return
    if old.label == OLD_LABEL or old.service == OLD_SERVICE:
        raise RenameError(f"{REAL} (label {old.label}, service {old.service})")
    root = old.root
    if root is None:
        raise RenameError(f"{REAL} (not a scratch root)")
    if not (root / SCRATCH_MARKER).is_file():
        raise RenameError(f"{root} has no {SCRATCH_MARKER}: not a scratch root")
    if old.label != SCRATCH_LABEL or old.service != SCRATCH_LABEL:
        raise RenameError(f"a scratch move needs the label and service {SCRATCH_LABEL}")
    if _inside(_login_home(), root):
        raise RenameError(f"the scratch root {root} contains your home folder")
    for path in (root, *old.folders(), old.plist, old.plist_aside):
        for real in _real_paths():
            if _related(path, real):
                raise RenameError(f"{REAL} ({path} overlaps {real})")
    for path in (*old.folders(), old.plist, old.plist_aside):
        if not _inside(path, root):
            raise RenameError(f"{path} is outside the scratch root {root}")
    try:
        service = secrets.service()
    except SecretStoreError as exc:
        raise RenameError(str(exc)) from None
    if not service.endswith(SCRATCH_SUFFIX):
        raise RenameError(
            "a scratch move needs a -scratch Keychain service (DIFFBELL_KEYCHAIN_SERVICE)"
        )


def check_destination(new: New) -> None:
    """A scratch move's destination must not be (in, or around) a real old or new
    folder or plist. Scratch runs only: the switchover moves to the real new ones."""
    for path in new.folders():
        for real in _real_paths():
            if _related(path, real):
                raise RenameError(f"the scratch destination {path} overlaps the real {real}")


def for_install() -> tuple[OldInstall, bool] | None:
    """What `install` moves: nothing while a scratch override is set (tests and scratch
    runs never look at the real install), else the real old install, as the switchover."""
    if any(name in os.environ for name in OVERRIDES):
        return None
    return OldInstall.real(), True


# -- steps 1-5 (rename spec section 3) -------------------------------------------------


def _restart_old(old: OldInstall, tools: Tools) -> str:
    """Put the old agent back (the rollback rule: only while no folder has moved):
    its plist first, if step 1 moved it aside, then bootstrap."""
    try:
        if os.path.lexists(old.plist_aside) and not os.path.lexists(old.plist):
            os.rename(old.plist_aside, old.plist)
    except OSError as exc:
        return f"putting the old agent's plist back failed too: {exc.strerror}"
    if not old.plist.exists():
        return "there was no old agent to start again"
    try:
        launchd.bootstrap(old.plist, uid=tools.uid, runner=tools.launchctl)
    except (launchd.LaunchdError, OSError) as exc:
        return f"starting the old agent again failed too: {exc}"
    return "the old agent was started again"


AGAIN = "run `diffbell install` again to finish moving"
NOT_LOADED = "the old agent wasn't loaded; nothing to start again"


def _missing_folder(old: OldInstall) -> Path | None:
    """The first missing old folder: an earlier run may have moved it, and the old
    agent would then start without its config or state (the rollback rule)."""
    return next((folder for folder in old.folders() if not os.path.lexists(folder)), None)


def _put_plist_back(old: OldInstall) -> str:
    """An agent that wasn't loaded: its plist back in place, no bootstrap."""
    try:
        if os.path.lexists(old.plist_aside) and not os.path.lexists(old.plist):
            os.rename(old.plist_aside, old.plist)
    except OSError as exc:
        return f"{NOT_LOADED}; its plist stays at {old.plist_aside} ({exc.strerror})"
    return NOT_LOADED


def _roll_back(old: OldInstall, tools: Tools, *, was_loaded: bool) -> str:
    """The rollback rule: only while no old folder is missing, the old install goes
    back as it was: a loaded agent is started again, an unloaded one gets its plist
    back without a bootstrap. Otherwise the agent stays stopped, its plist aside."""
    missing = _missing_folder(old)
    if missing is not None:
        return f"an old folder is missing ({missing}), so the old agent stays stopped; {AGAIN}"
    if not was_loaded:
        return _put_plist_back(old)
    return _restart_old(old, tools)


def stop_old_agent(old: OldInstall, tools: Tools) -> bool:
    """Step 1: refuse if both the plist and its aside copy exist (before any stop:
    neither is ever overwritten). Then bootout, and wait until launchd no longer lists
    the label and nobody holds the old state lock (bootout is asynchronous; the lock
    counts while held, even with an empty pid file). Then the plist goes aside
    (OldInstall.plist_aside). A failure follows the rollback rule (_roll_back). A
    rerun with only the aside plist finds nothing loaded and passes. Returns whether
    the old agent was loaded before the stop."""
    both = (
        f"both {old.plist} and {old.plist_aside} exist; remove one, "
        "then run `diffbell install` again"
    )
    if os.path.lexists(old.plist) and os.path.lexists(old.plist_aside):
        raise RenameError(f"stopping the old agent: {both}")  # nothing stopped yet
    was_loaded = launchd.agent_status(label=old.label, uid=tools.uid, runner=tools.launchctl)[0]

    def failed(why: str) -> RenameError:
        return RenameError(
            f"stopping the old agent: {why}; {_roll_back(old, tools, was_loaded=was_loaded)}"
        )

    try:
        launchd.bootout(old.label, uid=tools.uid, runner=tools.launchctl)
        deadline = tools.clock() + STOP_TIMEOUT
        while True:
            loaded, _ = launchd.agent_status(label=old.label, uid=tools.uid, runner=tools.launchctl)
            if not loaded and not is_held(old.state_dir / "daemon.lock"):
                break
            if tools.clock() >= deadline:
                raise failed(f"it didn't stop within {STOP_TIMEOUT:g} s")
            tools.sleep(POLL)
        if os.path.lexists(old.plist):
            if os.path.lexists(old.plist_aside):  # appeared while stopping
                raise failed(both)
            os.rename(old.plist, old.plist_aside)
    except LockUnreadable as exc:
        raise failed(str(exc)) from None
    except OSError as exc:
        raise failed(exc.strerror or type(exc).__name__) from None
    return was_loaded


class MoveIncomplete(RenameError):
    """move_folders stopped; ``moved`` folders had moved by then (0: nothing did, so
    the old agent may be started again)."""

    def __init__(self, message: str, moved: int) -> None:
        super().__init__(message)
        self.moved = moved


MOVING = "moving the folders"


def _refuse_links(step: str, *candidates: Path) -> None:
    for path in candidates:
        if os.path.islink(path):
            raise RenameError(f"{step}: {path} is a symlink; refusing to follow it")


def _nearest_existing(path: Path) -> Path:
    while not os.path.lexists(path) and path != path.parent:
        path = path.parent
    return path


def _same_device(a: Path, b: Path) -> bool:
    """Whether ``a`` and the nearest existing folder of ``b`` share a volume, so an
    os.rename between them can't fail with EXDEV."""
    return os.stat(a).st_dev == os.stat(_nearest_existing(b)).st_dev


def preflight_folders(old: OldInstall, new: New) -> None:
    """Every refusal move_folders can predict, before any rename: a symlink, a
    non-empty new folder, another volume."""
    for o, n in zip(old.folders(), new.folders(), strict=True):
        _refuse_links(MOVING, o, n)
        if not o.exists():
            continue
        try:
            if n.exists() and (not n.is_dir() or any(n.iterdir())):
                raise RenameError(f"{MOVING}: {o} and {n} both exist; move or remove one")
            if not _same_device(o, n):
                raise RenameError(f"{MOVING}: {n} is on another volume than {o}")
        except OSError as exc:
            raise RenameError(f"{MOVING}: {o} → {n}: {exc.strerror}") from None


def move_folders(old: OldInstall, new: New, *, recheck: Callable[[], None] = lambda: None) -> int:
    """Step 2: rename each old folder to the new path (same volume: mode and owner
    kept) and return how many moved. preflight_folders runs first, so every
    predictable refusal comes while nothing has moved. Never merges, never follows a
    symlink; ``recheck`` (the guard) runs immediately before each rename. Any failure
    raises MoveIncomplete with the number that had moved."""
    moved = 0
    try:
        preflight_folders(old, new)
        for o, n in zip(old.folders(), new.folders(), strict=True):
            _refuse_links(MOVING, o, n)
            if not o.exists():
                continue
            try:
                if n.exists():
                    if any(n.iterdir()):
                        raise RenameError(f"{MOVING}: {o} and {n} both exist; move or remove one")
                    n.rmdir()
                n.parent.mkdir(parents=True, exist_ok=True)
                recheck()
                _refuse_links(MOVING, o, n)
                os.rename(o, n)
            except OSError as exc:
                raise RenameError(f"{MOVING}: {o} → {n}: {exc.strerror}") from None
            moved += 1
    except MoveIncomplete:
        raise
    except RenameError as exc:
        raise MoveIncomplete(str(exc), moved) from None
    return moved


def rename_logs(new: New) -> None:
    """Step 3: dev-watchman.log{,.1,.2,.3} → diffbell.log{…}; a new name that exists
    wins. A symlinked log file (old or new name) is refused, never followed."""
    step = "renaming the log files"
    for suffix in ("", ".1", ".2", ".3"):
        o = new.log_dir / f"{OLD_NAME}.log{suffix}"
        n = new.log_dir / f"{NEW_NAME}.log{suffix}"
        _refuse_links(step, o, n)
        if o.exists() and not n.exists():
            try:
                os.rename(o, n)
            except OSError as exc:
                raise RenameError(f"{step}: {o} → {n}: {exc.strerror}") from None


def copy_credentials(old: OldInstall, tools: Tools) -> str:
    """Step 4: the old pair to the new (default) service through save_credentials;
    an old unfinished save carries its marker. A pair Diffbell already has is kept.
    The old items stay (step 7). Never says a value."""
    run = tools.security
    try:
        current = secrets.credentials(get=partial(secrets.get_secret, runner=run))
        state = secrets.credentials(
            get=partial(secrets.get_secret, service=old.service, runner=run)
        )
        if isinstance(current, secrets.Credentials):
            if current == state:
                return "already copied"
            if isinstance(state, secrets.Credentials):
                return (
                    "kept the Diffbell credentials already set up "
                    "(the old dev-watchman ones are deleted at the end)"
                )
            return "Diffbell's credentials are already set up"
        if isinstance(state, secrets.Credentials):
            secrets.save_credentials(
                state.token,
                state.chat_id,
                put=partial(secrets.set_secret, runner=run),
                delete=partial(secrets.delete_secret, runner=run),
            )
            return "copied the bot token and chat id"
        if state.what == "unfinished save":
            secrets.set_secret(ACCOUNT_SAVE_PENDING, str(time.time()), runner=run)
            return "copied the unfinished-save marker (Telegram needs setting up again)"
    except SecretStoreError as exc:
        raise RenameError(f"copying the Telegram credentials: {exc}") from None
    except ValueError:  # its message names no value; a value is never put in one
        raise RenameError(
            "copying the Telegram credentials: an old item isn't a single line; "
            "set Telegram up again with `diffbell setup`"
        ) from None
    return "nothing to copy (Telegram wasn't set up)"


def _is_checkout(folder: Path) -> bool:
    """Whether ``folder`` is a diffbell checkout (its pyproject.toml names it)."""
    try:
        with open(folder / "pyproject.toml", "rb") as fh:
            project = tomllib.load(fh).get("project", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(project, dict) and project.get("name") == NEW_NAME


def follow_checkout(new: New) -> str:
    """Step 5: the recorded dev-watchman checkout is gone and a diffbell checkout sits
    beside it under the new name (plan decision 1): the database first (idempotent),
    then the config (``new.config_path``), then install-source.json. The moved project
    keeps its id and history; its clock is dropped, so it re-baselines."""
    source = read_source(new.state_dir)
    if source is None:
        return "checkout: nothing to follow"
    old_path = Path(source.path)
    new_path = old_path.with_name(NEW_NAME)
    if os.path.lexists(old_path) or old_path.name != OLD_NAME or not _is_checkout(new_path):
        return "checkout: nothing to follow"
    old_c, new_c = os.path.realpath(old_path), os.path.realpath(new_path)
    step = "following the checkout"
    db = new.state_dir / "state.db"
    if db.exists():
        try:
            store = Store(db)
        except (sqlite3.Error, MigrationError) as exc:
            raise RenameError(f"{step}: the database: {exc}") from None
        try:
            store.move_project_path(old_c, new_c)
        except ProjectPathTaken:
            raise RenameError(
                f"{step}: a project already watches {new_c}; "
                "remove it from the config, then run `diffbell install` again"
            ) from None
        except (sqlite3.Error, MigrationError) as exc:
            raise RenameError(f"{step}: the database: {exc}") from None
        finally:
            store.close()
    try:
        config_edit.move_project_path(new.config_path, old_c, new_c)
    except (
        config_edit.ConfigInvalid,
        config_edit.ConfigUnreadable,
        config_edit.ConfigConflict,
    ) as exc:
        raise RenameError(f"{step}: the config: {exc}") from None
    except OSError as exc:
        raise RenameError(f"{step}: the config: {exc.strerror}") from None
    try:
        write_source(
            new.state_dir, Source(str(new_path), source.commit, source.branch, source.installed_at)
        )
    except OSError as exc:
        raise RenameError(f"{step}: install-source.json: {exc.strerror}") from None
    return f"moved project {old_c} → {new_c} (its history stays)"


# -- step 7, run() and the scratch entry point ------------------------------------------


def clean_up(old: OldInstall, tools: Tools) -> list[str]:
    """Step 7, only after the new agent runs: the old plist (in place or aside) and
    the old Keychain items. A failure is reported, never raised (doctor names what is
    left; a rerun removes it). Never says a value."""
    problems = []
    for plist in (old.plist, old.plist_aside):
        try:
            plist.unlink(missing_ok=True)
        except OSError as exc:
            problems.append(f"{plist}: {exc.strerror}")
    for account in ACCOUNTS:
        try:
            secrets.delete_secret(account, service=old.service, runner=tools.security)
        except SecretStoreError as exc:
            problems.append(str(exc))
    return problems


def _recheck(old: OldInstall, new: New, *, switchover: bool) -> None:
    """The guard again, immediately before each folder rename."""
    check_guard(old, switchover=switchover)
    if switchover:
        check_home()
    else:
        check_destination(new)


def _move(old: OldInstall, new: New, *, switchover: bool, tools: Tools) -> None:
    """Steps 1-5. Raises RenameError naming the step and what to do."""
    say = tools.say
    say("Moving the dev-watchman install to Diffbell:")
    was_loaded = stop_old_agent(old, tools)
    say("  stopped the old agent")
    try:
        moved = move_folders(old, new, recheck=partial(_recheck, old, new, switchover=switchover))
    except MoveIncomplete as exc:
        if exc.moved == 0:
            raise RenameError(f"{exc}; {_roll_back(old, tools, was_loaded=was_loaded)}") from None
        raise RenameError(f"{exc}; {AGAIN}") from None
    say(
        "  moved the config, state and log folders"
        if moved > 0
        else "  the folders were already moved"
    )
    try:  # both agents are stopped by now: a rerun finishes the move
        rename_logs(new)
        say("  " + copy_credentials(old, tools))
        say("  " + follow_checkout(new))
    except RenameError as exc:
        message = str(exc)
        raise RenameError(message if AGAIN in message else f"{message}; {AGAIN}") from None


def _describe(exc: Exception) -> str:
    """A backstop error without a secret: an OSError's reason and file, a database
    error's message, only the type of anything else."""
    if isinstance(exc, OSError):
        where = f" ({exc.filename})" if exc.filename else ""
        return f"{exc.strerror or type(exc).__name__}{where}; {AGAIN}"
    if isinstance(exc, sqlite3.Error):
        return f"the database: {exc}; {AGAIN}"
    return f"{type(exc).__name__}; {AGAIN}"


def run(
    old: OldInstall,
    *,
    switchover: bool,
    install: Callable[[], int],
    tools: Tools,
    new: New | None = None,
) -> int:
    """`diffbell install` with the move first (rename spec section 3); returns the exit
    code. The guard runs first, before any runner call or stat of the old install
    (then a scratch run's destination check); only then is the old install looked
    for, and the switchover's HOME checked once one was found. Nothing old is deleted until ``install`` (step 6) returned 0."""
    say = tools.say
    try:
        check_guard(old, switchover=switchover)
        new = new or New.current()
        if not switchover:
            check_destination(new)
        left = leftovers(old, tools)
        if left == [KEYCHAIN_UNCHECKED]:
            say("Couldn't check the Keychain for an old dev-watchman install; skipping the move")
            left = []
        found = bool(left)
        if found:
            if switchover:
                check_home()
            _move(old, new, switchover=switchover, tools=tools)
    except RenameError as exc:
        say(f"Move failed: {exc}")
        return 1
    except (OSError, sqlite3.Error, ValueError) as exc:
        say(f"Move failed: {_describe(exc)}")
        return 1
    code = install()
    if not found:
        return code
    if code != 0:
        say(
            "The old install's plist and Keychain items are kept until the new agent "
            "runs: run `diffbell install` again."
        )
        return code
    for problem in clean_up(old, tools):
        say(f"Couldn't remove part of the old install: {problem} (`diffbell doctor` lists it)")
    if switchover:
        say("Moved from dev-watchman. Remove the old tool: `uv tool uninstall dev-watchman`")
    else:
        say("Moved the scratch install.")
    return 0


def _scratch_main() -> int:
    """`python -m diffbell.rename_install` (live check R): the old-named scratch install
    under DIFFBELL_RENAME_FROM, moved to the current (scratch) Diffbell paths with no
    agent (step 6 is a no-op that returns 0)."""
    root = os.environ.get(RENAME_FROM_ENV)
    if not root:
        print(f"{RENAME_FROM_ENV} is not set")
        return 2
    try:
        old = OldInstall.scratch(Path(root))
    except RenameError as exc:
        print(f"Refused: {exc}")
        return 2
    return run(old, switchover=False, install=lambda: 0, tools=Tools.real(), new=New.current())


if __name__ == "__main__":
    raise SystemExit(_scratch_main())
