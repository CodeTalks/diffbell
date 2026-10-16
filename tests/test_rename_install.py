"""rename_install (rename spec §3): moving a dev-watchman install to Diffbell. Every
test uses temp folders and fake runners: never the real launchctl or Keychain."""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from diffbell import rename_install
from diffbell.install_source import Source, read_source, write_source
from diffbell.rename_install import MoveIncomplete, New, OldInstall, RenameError, Tools
from diffbell.store import Store

SCRATCH = "io.688.diffbell-scratch"
OLD_SERVICE = "io.688.dev-watchman-scratch"


class Keychain:
    """A fake /usr/bin/security over {(service, account): value}."""

    def __init__(self, items: dict[tuple[str, str], str] | None = None) -> None:
        self.items = dict(items or {})

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        verb = args[1]
        key = (args[args.index("-s") + 1], args[args.index("-a") + 1])
        if verb == "find-generic-password":
            if key not in self.items:
                return subprocess.CompletedProcess(args, 44, "", "")
            out = self.items[key] + "\n" if "-w" in args else ""
            return subprocess.CompletedProcess(args, 0, out, "")
        if verb == "add-generic-password":
            self.items[key] = kwargs["input"].split("\n")[0]
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "delete-generic-password":
            code = 0 if self.items.pop(key, None) is not None else 44
            return subprocess.CompletedProcess(args, code, "", "")
        raise AssertionError(args)


class Launchctl:
    """A fake launchctl: `loaded` labels answer `print`; bootout unloads one after
    ``lag`` prints (bootout is asynchronous)."""

    def __init__(self, loaded: set[str] | None = None, lag: int = 0) -> None:
        self.loaded = set(loaded or ())
        self.lag = lag
        self.calls: list[list[str]] = []
        self.pending: dict[str, int] = {}

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        verb, target = args[1], args[-1]
        label = target.rsplit("/", 1)[-1]
        if verb == "bootout":
            if label in self.loaded:
                self.pending[label] = self.lag
            return subprocess.CompletedProcess(args, 0 if label in self.loaded else 113, "", "")
        if verb == "print":
            if label in self.pending:
                if self.pending[label] <= 0:
                    self.loaded.discard(label)
                    del self.pending[label]
                else:
                    self.pending[label] -= 1
            code = 0 if label in self.loaded else 113
            return subprocess.CompletedProcess(args, code, "pid = 7\n" if code == 0 else "", "")
        if verb == "bootstrap":
            self.loaded.add(Path(target).stem)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


@pytest.fixture(autouse=True)
def scratch_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIFFBELL_KEYCHAIN_SERVICE", SCRATCH)


@pytest.fixture(autouse=True)
def home(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake login home with the real-looking tree: no test reads the real one."""
    fake = tmp_dir / "home"
    for folder in (
        ".config/dev-watchman",
        ".local/share/dev-watchman",
        "Library/Logs/dev-watchman",
        "Library/LaunchAgents",
        ".config/diffbell",
        ".local/share/diffbell",
        "Library/Logs/diffbell",
    ):
        (fake / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rename_install, "_login_home", lambda: fake)
    return fake


@pytest.fixture
def no_overrides(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The switchover's environment: no scratch override, HOME is the login home."""
    for name in rename_install.OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))


def marked(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / rename_install.SCRATCH_MARKER).write_text("")
    return root


def old_install(root: Path) -> OldInstall:
    return OldInstall.scratch(marked(root / "old"))


def new_install(root: Path) -> New:
    return New(
        config_path=root / "new" / "config" / "diffbell" / "config.yaml",
        state_dir=root / "new" / "state",
        log_dir=root / "new" / "logs",
    )


def tools(launchctl: Launchctl | None = None, keychain: Keychain | None = None) -> Tools:
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    said: list[str] = []
    return Tools(
        launchctl=launchctl or Launchctl(),
        security=keychain or Keychain(),
        uid=501,
        clock=lambda: clock["t"],
        sleep=sleep,
        say=said.append,
    )


def build_old(root: Path, keychain: Keychain, *, marker: bool = False) -> OldInstall:
    old = old_install(root)
    for folder in (old.config_dir, old.state_dir, old.log_dir):
        folder.mkdir(parents=True, mode=0o700)
    (old.config_dir / "config.yaml").write_text("projects: []\n")
    (old.log_dir / "dev-watchman.log").write_text("old log\n")
    old.plist.parent.mkdir(parents=True)
    old.plist.write_text("<plist/>")
    keychain.items[(OLD_SERVICE, "bot-token")] = "123:tok"
    keychain.items[(OLD_SERVICE, "chat-id")] = "42"
    if marker:
        keychain.items[(OLD_SERVICE, "save-pending")] = "1.0"
    return old


@contextmanager
def held_without_pid(path: Path) -> Iterator[None]:
    """The lock as singleton_lock holds it at its start and shutdown: an empty file."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_leftovers_names_each_part(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    t = tools(keychain=keychain)
    assert rename_install.leftovers(old, t) == [
        "the agent",
        f"the config folder {old.config_dir}",
        f"the state folder {old.state_dir}",
        f"the log folder {old.log_dir}",
        "Keychain items bot-token, chat-id",
    ]
    assert rename_install.leftovers(old_install(tmp_dir / "none"), tools()) == []


def test_the_real_install_is_refused_without_switchover(tmp_dir: Path) -> None:
    with pytest.raises(RenameError, match="real dev-watchman install"):
        rename_install.check_guard(OldInstall.real(), switchover=False)
    with pytest.raises(RenameError, match="real dev-watchman install"):
        rename_install.check_guard(
            replace(old_install(tmp_dir), label="io.688.dev-watchman"), switchover=False
        )


def test_switchover_is_refused_while_a_scratch_override_is_set() -> None:
    with pytest.raises(RenameError, match="DIFFBELL_KEYCHAIN_SERVICE"):
        rename_install.check_guard(OldInstall.real(), switchover=True)


@pytest.mark.usefixtures("no_overrides")
def test_switchover_is_disabled_in_tests() -> None:
    with pytest.raises(RenameError, match="disabled"):
        rename_install.check_guard(OldInstall.real(), switchover=True)


def test_a_scratch_move_needs_a_scratch_service(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIFFBELL_KEYCHAIN_SERVICE", "io.688.diffbell")
    with pytest.raises(RenameError, match="-scratch"):
        rename_install.check_guard(old_install(tmp_dir), switchover=False)


def test_for_install_skips_the_real_install_while_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert rename_install.for_install() is None  # DIFFBELL_KEYCHAIN_SERVICE is set


def test_a_marked_scratch_root_passes(tmp_dir: Path) -> None:
    rename_install.check_guard(old_install(tmp_dir), switchover=False)


def test_a_root_without_the_marker_is_refused(tmp_dir: Path) -> None:
    root = tmp_dir / "bare"
    root.mkdir()
    with pytest.raises(RenameError, match=rename_install.SCRATCH_MARKER):
        rename_install.check_guard(OldInstall.scratch(root), switchover=False)
    with pytest.raises(RenameError, match="not a scratch root"):
        rename_install.check_guard(replace(old_install(tmp_dir), root=None), switchover=False)


def test_the_real_library_is_refused_even_marked(home: Path) -> None:
    """scratch(~/Library) puts its logs at ~/Library/logs: the real ~/Library/Logs
    on a case-insensitive disk."""
    with pytest.raises(RenameError, match="real dev-watchman install"):
        rename_install.check_guard(OldInstall.scratch(marked(home / "Library")), switchover=False)


def test_a_case_variant_of_a_real_folder_is_refused(home: Path) -> None:
    root = marked(home / ".CONFIG")
    old = replace(
        OldInstall.scratch(root),
        config_dir=root / "dev-watchman",
        state_dir=root / "state",
        log_dir=root / "logs",
        plist=root / "x.plist",
    )
    with pytest.raises(RenameError, match="real dev-watchman install"):
        rename_install.check_guard(old, switchover=False)


def test_a_root_around_the_home_is_refused(home: Path) -> None:
    with pytest.raises(RenameError, match="contains your home folder"):
        rename_install.check_guard(OldInstall.scratch(marked(home.parent)), switchover=False)
    with pytest.raises(RenameError, match="contains your home folder"):
        rename_install.check_guard(OldInstall.scratch(marked(home)), switchover=False)


def test_the_real_plist_with_a_scratch_label_is_refused(tmp_dir: Path) -> None:
    old = replace(old_install(tmp_dir), plist=OldInstall.real().plist)
    with pytest.raises(RenameError, match="real dev-watchman install"):
        rename_install.check_guard(old, switchover=False)


def test_a_path_outside_the_root_is_refused(tmp_dir: Path) -> None:
    old = replace(old_install(tmp_dir), state_dir=tmp_dir / "elsewhere")
    with pytest.raises(RenameError, match="outside the scratch root"):
        rename_install.check_guard(old, switchover=False)


def test_a_bad_keychain_service_is_a_rename_error(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIFFBELL_KEYCHAIN_SERVICE", "bogus")
    with pytest.raises(RenameError, match="DIFFBELL_KEYCHAIN_SERVICE"):
        rename_install.check_guard(old_install(tmp_dir), switchover=False)


@pytest.mark.usefixtures("no_overrides")
def test_an_empty_override_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIFFBELL_STATE_DIR", "")
    with pytest.raises(RenameError, match="DIFFBELL_STATE_DIR"):
        rename_install.check_guard(OldInstall.real(), switchover=True)
    assert rename_install.for_install() is None


@pytest.mark.usefixtures("no_overrides")
def test_a_foreign_home_refuses_the_switchover(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_dir / "other"))
    with pytest.raises(RenameError, match="HOME is not your login home"):
        rename_install.check_home()
    monkeypatch.setattr(rename_install, "REAL_SWITCHOVER_ALLOWED", True)
    rename_install.check_guard(OldInstall.real(), switchover=True)  # HOME is checked later


def foreign_switchover(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> tuple[Launchctl, Keychain, list[str], list[int]]:
    """The switchover allowed (as outside tests), under a HOME that isn't the login
    home, on the fake login home's tree, with fake runners."""
    monkeypatch.setattr(rename_install, "REAL_SWITCHOVER_ALLOWED", True)
    monkeypatch.setenv("HOME", str(tmp_dir / "other"))
    launchctl, keychain = Launchctl(), Keychain()
    t, said = said_tools(launchctl, keychain)
    installed: list[int] = []

    def install() -> int:
        installed.append(1)
        return 0

    code = rename_install.run(
        OldInstall.real(), switchover=True, install=install, tools=t, new=new_install(tmp_dir)
    )
    installed.insert(0, code)
    return launchctl, keychain, said, installed


@pytest.mark.usefixtures("no_overrides")
def test_a_foreign_home_with_nothing_left_installs_normally(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    for folder in OldInstall.real().folders():
        folder.rmdir()
    launchctl, _, said, installed = foreign_switchover(tmp_dir, monkeypatch, home)
    assert installed == [0, 1]
    assert launchctl.calls == [] and said == []


@pytest.mark.usefixtures("no_overrides")
def test_a_foreign_home_with_an_old_install_is_refused_before_any_runner(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    launchctl, _, said, installed = foreign_switchover(tmp_dir, monkeypatch, home)
    assert installed == [1]  # the exit code only: install never ran
    assert launchctl.calls == []
    assert len(said) == 1 and said[0].startswith("Move failed: ")
    assert "HOME is not your login home" in said[0]
    assert all(folder.is_dir() for folder in OldInstall.real().folders())


@pytest.mark.usefixtures("no_overrides")
def test_the_recheck_still_refuses_a_foreign_home(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    monkeypatch.setattr(rename_install, "REAL_SWITCHOVER_ALLOWED", True)
    old, new = OldInstall.real(), new_install(tmp_dir)
    rename_install._recheck(old, new, switchover=True)  # the login home passes
    monkeypatch.setenv("HOME", str(tmp_dir / "other"))
    with pytest.raises(RenameError, match="HOME is not your login home"):
        rename_install._recheck(old, new, switchover=True)


def test_real_builds_from_the_login_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/somewhere/else")
    assert OldInstall.real().config_dir == home / ".config" / "dev-watchman"


def test_a_scratch_destination_must_avoid_the_real_folders(tmp_dir: Path, home: Path) -> None:
    rename_install.check_destination(new_install(tmp_dir))
    with pytest.raises(RenameError, match="scratch destination"):
        rename_install.check_destination(
            replace(new_install(tmp_dir), config_path=home / ".config" / "diffbell" / "config.yaml")
        )
    with pytest.raises(RenameError, match="scratch destination"):
        rename_install.check_destination(replace(new_install(tmp_dir), log_dir=home / "Library"))


def test_a_destination_on_the_real_old_folders_is_refused(tmp_dir: Path, home: Path) -> None:
    real = OldInstall.real()
    with pytest.raises(RenameError, match="scratch destination"):
        rename_install.check_destination(
            replace(new_install(tmp_dir), config_path=real.config_dir / "config.yaml")
        )
    with pytest.raises(RenameError, match="scratch destination"):
        rename_install.check_destination(replace(new_install(tmp_dir), state_dir=real.state_dir))


def test_an_nfd_spelling_of_the_home_is_refused(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APFS treats NFC and NFD names as one folder; the guard must too."""
    nfc = tmp_dir / unicodedata.normalize("NFC", "Café")
    for folder in (".config/dev-watchman", "Library/Logs/dev-watchman", ".config/diffbell"):
        (nfc / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rename_install, "_login_home", lambda: nfc)
    nfd = Path(unicodedata.normalize("NFD", str(nfc)))
    assert str(nfd) != str(nfc)
    with pytest.raises(RenameError):
        rename_install.check_guard(OldInstall.scratch(marked(nfd / "Library")), switchover=False)
    new = New(nfd / ".config" / "diffbell" / "config.yaml", tmp_dir / "s", tmp_dir / "l")
    with pytest.raises(RenameError, match="scratch destination"):
        rename_install.check_destination(new)


def test_a_relative_scratch_root_is_refused() -> None:
    with pytest.raises(RenameError, match="absolute path"):
        OldInstall.scratch(Path("scratch"))


# -- steps 1-5 ---------------------------------------------------------------------


def test_stop_waits_for_the_old_agent(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    launchctl = Launchctl({old.label}, lag=3)
    rename_install.stop_old_agent(old, tools(launchctl, keychain))
    assert old.label not in launchctl.loaded
    assert ["launchctl", "bootout", f"gui/501/{old.label}"] in launchctl.calls
    assert not old.plist.exists() and old.plist_aside.read_text() == "<plist/>"
    assert rename_install.leftovers(old, tools(keychain=keychain))[0] == "the agent"
    rename_install.stop_old_agent(old, tools(Launchctl(), keychain))  # a rerun passes
    assert old.plist_aside.exists()


def test_an_old_agent_that_never_stops_is_started_again(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    launchctl = Launchctl({old.label}, lag=10_000)
    with pytest.raises(RenameError, match="didn't stop"):
        rename_install.stop_old_agent(old, tools(launchctl, keychain))
    assert ["launchctl", "bootstrap", "gui/501", str(old.plist)] in launchctl.calls


def test_a_held_lock_without_a_pid_is_waited_for(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    launchctl = Launchctl()  # not loaded: nothing to start again
    with (
        held_without_pid(old.state_dir / "daemon.lock"),
        pytest.raises(RenameError, match=r"didn't stop.*wasn't loaded; nothing to start again"),
    ):
        rename_install.stop_old_agent(old, tools(launchctl, keychain))
    assert not [c for c in launchctl.calls if c[1] == "bootstrap"]
    assert old.plist.exists()


def test_a_timeout_restores_the_plist_before_bootstrapping(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    os.rename(old.plist, old.plist_aside)  # an earlier run moved it aside
    launchctl = Launchctl({old.label}, lag=10_000)
    with pytest.raises(RenameError, match="started again"):
        rename_install.stop_old_agent(old, tools(launchctl, keychain))
    assert old.plist.exists() and not old.plist_aside.exists()
    assert ["launchctl", "bootstrap", "gui/501", str(old.plist)] in launchctl.calls


def test_an_unreadable_old_lock_restarts_the_old_agent(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    os.rename(old.plist, old.plist_aside)
    lock = old.state_dir / "daemon.lock"
    lock.write_text("")
    lock.chmod(0)
    launchctl = Launchctl({old.label})
    try:
        with pytest.raises(RenameError, match=r"cannot read.*started again"):
            rename_install.stop_old_agent(old, tools(launchctl, keychain))
    finally:
        lock.chmod(0o600)
    assert old.plist.exists() and not old.plist_aside.exists()
    assert ["launchctl", "bootstrap", "gui/501", str(old.plist)] in launchctl.calls


class BrokenBootout(Launchctl):
    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[1] == "bootout":
            raise OSError(errno.EIO, "launchctl broke")
        return super().__call__(args, **kwargs)


def test_a_failing_launchctl_is_a_rename_error(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    launchctl = BrokenBootout({old.label})
    with pytest.raises(RenameError, match=r"stopping the old agent.*started again"):
        rename_install.stop_old_agent(old, tools(launchctl, keychain))
    assert ["launchctl", "bootstrap", "gui/501", str(old.plist)] in launchctl.calls


def test_with_no_old_agent_stopping_passes_at_once(tmp_dir: Path) -> None:
    rename_install.stop_old_agent(old_install(tmp_dir), tools())


def test_folders_move_with_their_mode(tmp_dir: Path) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    assert rename_install.move_folders(old, new) == 3
    assert not old.config_dir.exists() and new.config_path.exists()
    assert new.state_dir.is_dir() and (new.log_dir / "dev-watchman.log").exists()
    assert new.state_dir.stat().st_mode & 0o777 == 0o700
    assert rename_install.move_folders(old, new) == 0  # a rerun: nothing left to move


def test_a_non_empty_new_folder_is_never_merged(tmp_dir: Path) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    new.state_dir.mkdir(parents=True)
    (new.state_dir / "state.db").write_text("x")
    with pytest.raises(MoveIncomplete, match="both exist") as caught:
        rename_install.move_folders(old, new)
    assert caught.value.moved == 0
    assert all(folder.is_dir() for folder in old.folders())  # the config didn't move first
    assert not new.config_dir.exists()


def test_another_volume_is_refused_before_anything_moves(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    monkeypatch.setattr(rename_install, "_same_device", lambda a, b: a != old.log_dir)
    with pytest.raises(MoveIncomplete, match="another volume") as caught:
        rename_install.move_folders(old, new)
    assert caught.value.moved == 0
    assert all(folder.is_dir() for folder in old.folders())


def test_same_device_compares_the_nearest_existing_folder(tmp_dir: Path) -> None:
    (tmp_dir / "a").mkdir()
    assert rename_install._same_device(tmp_dir / "a", tmp_dir / "not" / "yet" / "there")


def test_a_failure_partway_says_how_many_moved(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    real_rename = os.rename
    calls: list[str] = []

    def rename(src: Any, dst: Any) -> None:
        calls.append(str(src))
        if len(calls) == 2:
            raise OSError(errno.EXDEV, "Cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(MoveIncomplete, match="Cross-device link") as caught:
        rename_install.move_folders(old, new)
    assert caught.value.moved == 1
    assert new.config_dir.is_dir() and old.state_dir.is_dir()


def test_an_empty_new_folder_is_replaced(tmp_dir: Path) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    new.config_dir.mkdir(parents=True)
    rename_install.move_folders(old, new)
    assert new.config_path.exists()


def test_a_symlinked_old_folder_is_refused(tmp_dir: Path) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    target = tmp_dir / "elsewhere"
    target.mkdir()
    old.state_dir.rmdir()
    old.state_dir.symlink_to(target)
    with pytest.raises(RenameError, match="symlink"):
        rename_install.move_folders(old, new)
    assert old.state_dir.is_symlink() and not os.path.lexists(new.state_dir)
    assert old.config_dir.is_dir() and not new.config_dir.exists()  # preflight


def test_a_symlinked_new_folder_is_refused(tmp_dir: Path) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    new.config_dir.parent.mkdir(parents=True)
    new.config_dir.symlink_to(tmp_dir / "dangling")
    with pytest.raises(RenameError, match="symlink"):
        rename_install.move_folders(old, new)
    assert old.config_dir.is_dir() and new.config_dir.is_symlink()


def test_recheck_runs_before_each_move(tmp_dir: Path) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)
    calls: list[int] = []
    rename_install.move_folders(old, new, recheck=lambda: calls.append(1))
    assert len(calls) == 3


def test_a_failing_recheck_stops_before_any_rename(tmp_dir: Path) -> None:
    old = build_old(tmp_dir, Keychain())
    new = new_install(tmp_dir)

    def refuse() -> None:
        raise RenameError("guard says no")

    with pytest.raises(MoveIncomplete, match="guard says no") as caught:
        rename_install.move_folders(old, new, recheck=refuse)
    assert caught.value.moved == 0
    assert all(folder.is_dir() for folder in old.folders())
    assert not any(folder.exists() for folder in new.folders())


def test_log_files_take_the_new_name_unless_it_exists(tmp_dir: Path) -> None:
    new = new_install(tmp_dir)
    new.log_dir.mkdir(parents=True)
    (new.log_dir / "dev-watchman.log").write_text("old")
    (new.log_dir / "dev-watchman.log.1").write_text("old1")
    (new.log_dir / "diffbell.log.1").write_text("new1")
    rename_install.rename_logs(new)
    assert (new.log_dir / "diffbell.log").read_text() == "old"
    assert (new.log_dir / "diffbell.log.1").read_text() == "new1"
    assert (new.log_dir / "dev-watchman.log.1").exists()


def test_a_symlinked_log_file_is_refused(tmp_dir: Path) -> None:
    new = new_install(tmp_dir)
    new.log_dir.mkdir(parents=True)
    secret = tmp_dir / "secret.txt"
    secret.write_text("x")
    (new.log_dir / "dev-watchman.log").symlink_to(secret)
    with pytest.raises(RenameError, match="symlink"):
        rename_install.rename_logs(new)
    assert not os.path.lexists(new.log_dir / "diffbell.log")
    (new.log_dir / "dev-watchman.log").unlink()
    (new.log_dir / "dev-watchman.log.2").write_text("old2")
    (new.log_dir / "diffbell.log.2").symlink_to(tmp_dir / "dangling")
    with pytest.raises(RenameError, match="symlink"):
        rename_install.rename_logs(new)
    assert (new.log_dir / "dev-watchman.log.2").read_text() == "old2"


def test_credentials_are_copied_and_the_old_ones_kept(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    t = tools(keychain=keychain)
    assert rename_install.copy_credentials(old, t) == "copied the bot token and chat id"
    assert keychain.items[(SCRATCH, "bot-token")] == "123:tok"
    assert keychain.items[(SCRATCH, "chat-id")] == "42"
    assert (SCRATCH, "save-pending") not in keychain.items
    assert (OLD_SERVICE, "bot-token") in keychain.items  # deleted only in step 7
    assert rename_install.copy_credentials(old, t) == "already copied"


def test_other_diffbell_credentials_are_kept(tmp_dir: Path) -> None:
    keychain = Keychain({(SCRATCH, "bot-token"): "9:new", (SCRATCH, "chat-id"): "7"})
    old = build_old(tmp_dir, keychain)
    said = rename_install.copy_credentials(old, tools(keychain=keychain))
    assert said == (
        "kept the Diffbell credentials already set up "
        "(the old dev-watchman ones are deleted at the end)"
    )
    assert "9:new" not in said and "123:tok" not in said
    assert keychain.items[(SCRATCH, "bot-token")] == "9:new"
    assert keychain.items[(SCRATCH, "chat-id")] == "7"


def test_a_malformed_old_token_is_a_rename_error(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    keychain.items[(OLD_SERVICE, "bot-token")] = "123\n:tok"
    with pytest.raises(RenameError, match="copying the Telegram credentials") as caught:
        rename_install.copy_credentials(old, tools(keychain=keychain))
    assert "tok" not in str(caught.value)


def test_an_unfinished_old_save_carries_its_marker(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain, marker=True)
    rename_install.copy_credentials(old, tools(keychain=keychain))
    assert (SCRATCH, "save-pending") in keychain.items
    assert (SCRATCH, "bot-token") not in keychain.items


def test_one_old_item_copies_nothing(tmp_dir: Path) -> None:
    keychain = Keychain({(OLD_SERVICE, "chat-id"): "42"})
    assert rename_install.copy_credentials(old_install(tmp_dir), tools(keychain=keychain)) == (
        "nothing to copy (Telegram wasn't set up)"
    )
    assert not [k for k in keychain.items if k[0] == SCRATCH]


def checkout(root: Path, name: str, package: str) -> Path:
    folder = root / "projects" / name
    folder.mkdir(parents=True)
    (folder / "pyproject.toml").write_text(f'[project]\nname = "{package}"\n')
    return folder


def moved_scene(tmp_dir: Path) -> tuple[New, Path, Path, int]:
    new = new_install(tmp_dir)
    for folder in new.folders():
        folder.mkdir(parents=True)
    # Canonical, as config.resolve_projects makes them (macOS: /var is /private/var).
    old_checkout = Path(os.path.realpath(tmp_dir)) / "projects" / "dev-watchman"
    new_checkout = Path(os.path.realpath(checkout(tmp_dir, "diffbell", "diffbell")))
    write_source(new.state_dir, Source(str(old_checkout), "abc1234", "main", 1.0))
    new.config_path.write_text(f"projects:\n  - path: {old_checkout}\n")
    store = Store(new.state_dir / "state.db")
    store.sync_projects([("dev-watchman", str(old_checkout))], 1.0)
    pid = store.project_token("dev-watchman")
    store.close()
    return new, old_checkout, new_checkout, pid


def test_a_moved_checkout_is_followed_everywhere(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new, _old, new_checkout, pid = moved_scene(tmp_dir)
    monkeypatch.setenv("DIFFBELL_CONFIG", str(tmp_dir / "not-this.yaml"))  # New's, not this
    assert rename_install.follow_checkout(new).startswith("moved project")
    store = Store(new.state_dir / "state.db")
    row = store._db.execute(
        "SELECT id FROM projects WHERE path = ?", (str(new_checkout),)
    ).fetchone()
    store.close()
    assert row[0] == pid
    assert str(new_checkout) in new.config_path.read_text()
    source = read_source(new.state_dir)
    assert source is not None and source.path == str(new_checkout)
    assert source.commit == "abc1234"
    assert rename_install.follow_checkout(new) == "checkout: nothing to follow"  # rerun


def test_a_rerun_after_the_config_edit_failed_converges(tmp_dir: Path) -> None:
    new, old_checkout, new_checkout, _pid = moved_scene(tmp_dir)
    store = Store(new.state_dir / "state.db")
    store.move_project_path(str(old_checkout), str(new_checkout))  # the DB edit happened
    store.close()
    assert rename_install.follow_checkout(new).startswith("moved project")
    assert str(new_checkout) in new.config_path.read_text()


def test_a_project_already_at_the_new_folder_is_refused(tmp_dir: Path) -> None:
    new, old_checkout, new_checkout, _pid = moved_scene(tmp_dir)
    store = Store(new.state_dir / "state.db")
    store.sync_projects([("dev-watchman", str(old_checkout)), ("other", str(new_checkout))], 2.0)
    store.close()
    with pytest.raises(RenameError, match="already watches"):
        rename_install.follow_checkout(new)


def test_an_unreadable_database_is_a_rename_error(tmp_dir: Path) -> None:
    new, _old, _new, _pid = moved_scene(tmp_dir)
    (new.state_dir / "state.db").write_bytes(b"not a database" * 512)
    for leftover in new.state_dir.glob("state.db-*"):
        leftover.unlink()
    with pytest.raises(RenameError, match="following the checkout: the database"):
        rename_install.follow_checkout(new)


def test_a_checkout_that_did_not_move_is_left_alone(tmp_dir: Path) -> None:
    new = new_install(tmp_dir)
    new.state_dir.mkdir(parents=True)
    kept = checkout(tmp_dir, "dev-watchman", "dev-watchman")
    write_source(new.state_dir, Source(str(kept), "abc1234", "main", 1.0))
    assert rename_install.follow_checkout(new) == "checkout: nothing to follow"


def test_a_new_folder_that_is_not_a_diffbell_checkout_is_left_alone(tmp_dir: Path) -> None:
    new = new_install(tmp_dir)
    new.state_dir.mkdir(parents=True)
    checkout(tmp_dir, "diffbell", "something-else")
    gone = tmp_dir / "projects" / "dev-watchman"
    write_source(new.state_dir, Source(str(gone), "abc1234", "main", 1.0))
    assert rename_install.follow_checkout(new) == "checkout: nothing to follow"


# -- the aside plist, step 1's refusals --------------------------------------------


def test_the_aside_plist_sits_outside_launch_agents(tmp_dir: Path, home: Path) -> None:
    """launchd may load any file in LaunchAgents at login: the aside is one level up."""
    old = old_install(tmp_dir)
    assert old.root is not None
    assert old.plist_aside == old.root / f"{old.label}.plist.moving"
    assert OldInstall.real().plist_aside == home / "Library" / "io.688.dev-watchman.plist.moving"
    rename_install.check_guard(old, switchover=False)


def test_an_aside_plist_outside_the_root_is_refused(tmp_dir: Path) -> None:
    root = marked(tmp_dir / "old")
    old = replace(OldInstall.scratch(root), plist=root / "x.plist")  # aside: tmp_dir/x…
    with pytest.raises(RenameError, match="outside the scratch root"):
        rename_install.check_guard(old, switchover=False)


def test_stop_says_whether_the_old_agent_was_loaded(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    assert rename_install.stop_old_agent(old, tools(Launchctl({old.label}), keychain)) is True
    assert rename_install.stop_old_agent(old, tools(Launchctl(), keychain)) is False


def test_both_plists_are_refused_and_neither_overwritten(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    old.plist_aside.write_text("<aside/>")
    launchctl = Launchctl({old.label})
    with pytest.raises(RenameError, match=r"both .* and .* exist; remove one") as caught:
        rename_install.stop_old_agent(old, tools(launchctl, keychain))
    assert "run `diffbell install` again" in str(caught.value)
    assert old.plist.read_text() == "<plist/>" and old.plist_aside.read_text() == "<aside/>"
    assert not [c for c in launchctl.calls if c[1] in ("bootout", "bootstrap")]  # no stop
    assert old.label in launchctl.loaded


def test_a_stop_failure_after_a_folder_moved_does_not_restart(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    os.rename(old.config_dir, tmp_dir / "moved-config")  # an earlier run moved it
    launchctl = Launchctl({old.label}, lag=10_000)
    with pytest.raises(RenameError, match="run `diffbell install` again") as caught:
        rename_install.stop_old_agent(old, tools(launchctl, keychain))
    assert f"an old folder is missing ({old.config_dir})" in str(caught.value)
    assert not [c for c in launchctl.calls if c[1] == "bootstrap"]


# -- run() and step 7 ----------------------------------------------------------------


def run_move(
    tmp_dir: Path, old: OldInstall, t: Tools, install_code: int = 0, new: New | None = None
) -> int:
    return rename_install.run(
        old,
        switchover=False,
        install=lambda: install_code,
        tools=t,
        new=new or new_install(tmp_dir),
    )


def said_tools(launchctl: Launchctl | None = None, keychain: Keychain | None = None) -> Any:
    said: list[str] = []
    return replace(tools(launchctl, keychain), say=said.append), said


def test_a_full_move_deletes_the_old_last(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    launchctl = Launchctl({old.label}, lag=1)
    t, said = said_tools(launchctl, keychain)
    assert run_move(tmp_dir, old, t) == 0
    assert not old.plist.exists() and not old.plist_aside.exists()
    assert not [k for k in keychain.items if k[0] == OLD_SERVICE]
    assert keychain.items[(SCRATCH, "bot-token")] == "123:tok"
    assert (new_install(tmp_dir).log_dir / "diffbell.log").exists()
    assert not any(folder.exists() for folder in old.folders())
    assert said[-1] == "Moved the scratch install."
    assert "  moved the config, state and log folders" in said
    assert not any("123:tok" in line or "42" in line.split() for line in said)
    assert rename_install.leftovers(old, tools(keychain=keychain)) == []


def test_a_rerun_with_the_folders_already_moved_says_so(tmp_dir: Path) -> None:
    import shutil

    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    for folder in old.folders():
        shutil.rmtree(folder, ignore_errors=True)
    t, said = said_tools(Launchctl({old.label}), keychain)
    assert run_move(tmp_dir, old, t) == 0
    assert "  the folders were already moved" in said
    assert "  moved the config, state and log folders" not in said


def test_a_switchover_names_the_old_tool_to_remove(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rename_install, "check_guard", lambda *a, **k: None)
    monkeypatch.setattr(rename_install, "check_home", lambda *a, **k: None)
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    t, said = said_tools(Launchctl({old.label}), keychain)
    code = rename_install.run(
        old, switchover=True, install=lambda: 0, tools=t, new=new_install(tmp_dir)
    )
    assert code == 0
    assert said[-1] == (
        "Moved from dev-watchman. Remove the old tool: `uv tool uninstall dev-watchman`"  # old-name
    )


def test_a_failed_install_keeps_the_old_items_and_a_rerun_finishes(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    t, said = said_tools(Launchctl({old.label}), keychain)
    assert run_move(tmp_dir, old, t, install_code=1) == 1
    assert old.plist_aside.exists() and (OLD_SERVICE, "bot-token") in keychain.items
    assert "run `diffbell install` again" in said[-1]
    assert run_move(tmp_dir, old, tools(keychain=keychain)) == 0  # the rerun finishes
    assert not old.plist.exists() and not old.plist_aside.exists()
    assert not [k for k in keychain.items if k[0] == OLD_SERVICE]


def refused_first_move(tmp_dir: Path) -> New:
    new = new_install(tmp_dir)
    new.config_dir.mkdir(parents=True)
    (new.config_dir / "config.yaml").write_text("x")
    return new


def test_a_refused_first_move_puts_a_loaded_old_agent_back(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    new = refused_first_move(tmp_dir)
    launchctl = Launchctl({old.label})
    t, said = said_tools(launchctl, keychain)
    assert run_move(tmp_dir, old, t, new=new) == 1
    assert ["launchctl", "bootstrap", "gui/501", str(old.plist)] in launchctl.calls
    assert old.plist.exists() and not old.plist_aside.exists()
    assert said[-1].startswith("Move failed:") and "started again" in said[-1]
    assert (OLD_SERVICE, "bot-token") in keychain.items


def test_a_refused_first_move_leaves_an_unloaded_old_agent_stopped(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    new = refused_first_move(tmp_dir)
    launchctl = Launchctl()
    t, said = said_tools(launchctl, keychain)
    assert run_move(tmp_dir, old, t, new=new) == 1
    assert not [c for c in launchctl.calls if c[1] == "bootstrap"]
    assert "wasn't loaded" in said[-1]
    assert old.plist.read_text() == "<plist/>" and not old.plist_aside.exists()  # put back


def test_an_aside_plist_that_cannot_go_back_is_named(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    new = refused_first_move(tmp_dir)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if str(src) == str(old.plist_aside):
            raise OSError(errno.EACCES, "Permission denied")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    t, said = said_tools(Launchctl(), keychain)
    assert run_move(tmp_dir, old, t, new=new) == 1
    assert str(old.plist_aside) in said[-1] and old.plist_aside.exists()


def test_a_later_folder_failure_says_run_install_again(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    real_rename = os.rename
    folders = {str(f) for f in old.folders()}
    renamed: list[str] = []

    def rename(src: Any, dst: Any) -> None:
        if str(src) in folders:
            renamed.append(str(src))
            if len(renamed) == 2:  # config moves, state fails
                raise OSError(errno.EIO, "Input/output error")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    launchctl = Launchctl({old.label})
    t, said = said_tools(launchctl, keychain)
    assert run_move(tmp_dir, old, t) == 1
    assert not any(c[1] == "bootstrap" for c in launchctl.calls)
    assert said[-1].startswith("Move failed:")
    assert "run `diffbell install` again to finish moving" in said[-1]
    assert old.plist_aside.exists() and (OLD_SERVICE, "bot-token") in keychain.items


def test_nothing_to_move_just_installs(tmp_dir: Path) -> None:
    t, said = said_tools()
    calls: list[int] = []

    def install() -> int:
        calls.append(1)
        return 0

    old = old_install(tmp_dir / "none")
    code = rename_install.run(
        old, switchover=False, install=install, tools=t, new=new_install(tmp_dir)
    )
    assert code == 0 and calls == [1]
    assert said == []


class Recorder:
    """Fakes that record every runner call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 113, "", "")


@pytest.mark.parametrize("refusal", ["real", "unmarked", "destination"])
def test_the_guard_runs_before_any_runner_or_stat(
    refusal: str, tmp_dir: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = Keychain()
    built = build_old(tmp_dir, keychain)
    new = new_install(tmp_dir)
    if refusal == "real":
        old = OldInstall.real()
    elif refusal == "unmarked":
        (tmp_dir / "old" / rename_install.SCRATCH_MARKER).unlink()
        old = built
    else:
        old = built
        new = replace(new, state_dir=home / ".local" / "share" / "diffbell")
    watched = {str(p) for p in (old.plist, old.plist_aside, *old.folders(), *new.folders())}
    stats: list[str] = []
    recording = [True]
    real_stat, real_lstat = os.stat, os.lstat

    def record(fn: Any) -> Any:
        def wrapped(path: Any, *args: Any, **kwargs: Any) -> Any:
            if recording[0] and isinstance(path, (str, os.PathLike)) and os.fspath(path) in watched:
                stats.append(os.fspath(path))
            return fn(path, *args, **kwargs)

        return wrapped

    launchctl, security = Recorder(), Recorder()
    left: list[int] = []

    def found(*args: Any) -> list[str]:
        left.append(1)
        return ["x"]

    monkeypatch.setattr(rename_install, "leftovers", found)
    said: list[str] = []
    t = Tools(launchctl=launchctl, security=security, uid=501, say=said.append)
    monkeypatch.setattr(os, "stat", record(real_stat))
    monkeypatch.setattr(os, "lstat", record(real_lstat))
    code = rename_install.run(old, switchover=False, install=lambda: 0, tools=t, new=new)
    recording[0] = False
    assert code == 1
    assert said[-1].startswith("Move failed:")
    assert launchctl.calls == [] and security.calls == [] and left == []
    if refusal != "destination":  # the scratch guard itself resolves the old paths
        assert stats == []
    assert all(folder.is_dir() for folder in built.folders())


def test_clean_up_reports_without_raising(tmp_dir: Path) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain, marker=True)
    old.plist_aside.write_text("<aside/>")

    def broken(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "")

    problems = rename_install.clean_up(old, replace(tools(), security=broken))
    assert len(problems) == 3 and all("keychain delete failed" in p for p in problems)
    assert not old.plist.exists() and not old.plist_aside.exists()
    assert rename_install.clean_up(old, tools(keychain=keychain)) == []
    assert not [k for k in keychain.items if k[0] == OLD_SERVICE]


def test_a_store_migration_error_is_a_rename_error(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from diffbell.store import MigrationError

    new, _old, _new, _pid = moved_scene(tmp_dir)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise MigrationError("migration v9 failed")

    monkeypatch.setattr(rename_install, "Store", refuse)
    with pytest.raises(RenameError, match="following the checkout: the database"):
        rename_install.follow_checkout(new)


def test_a_backstop_error_says_move_failed(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)

    def broken(new: New) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(rename_install, "rename_logs", broken)
    t, said = said_tools(Launchctl(), keychain)
    assert run_move(tmp_dir, old, t) == 1
    assert said[-1].startswith("Move failed:") and "Input/output error" in said[-1]
    assert (OLD_SERVICE, "bot-token") in keychain.items


# -- python -m diffbell.rename_install -----------------------------------------------


def test_the_scratch_main_moves_the_scratch_install(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = Keychain()
    old = build_old(tmp_dir, keychain)
    new = new_install(tmp_dir)
    monkeypatch.setenv("DIFFBELL_RENAME_FROM", str(old.root))
    monkeypatch.setenv("DIFFBELL_CONFIG", str(new.config_path))
    monkeypatch.setenv("DIFFBELL_STATE_DIR", str(new.state_dir))
    monkeypatch.setenv("DIFFBELL_LOG_DIR", str(new.log_dir))
    launchctl = Launchctl()
    fake = tools(launchctl, keychain)
    monkeypatch.setattr(rename_install.Tools, "real", lambda: fake)
    assert rename_install._scratch_main() == 0
    assert new.config_path.exists() and not old.config_dir.exists()
    assert not [k for k in keychain.items if k[0] == OLD_SERVICE]
    assert keychain.items[(SCRATCH, "chat-id")] == "42"


def test_the_scratch_main_needs_an_absolute_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_real() -> Tools:
        raise AssertionError("no real runner")

    monkeypatch.setattr(rename_install.Tools, "real", no_real)
    monkeypatch.delenv("DIFFBELL_RENAME_FROM", raising=False)
    assert rename_install._scratch_main() == 2
    assert "DIFFBELL_RENAME_FROM" in capsys.readouterr().out
    monkeypatch.setenv("DIFFBELL_RENAME_FROM", "relative/root")
    assert rename_install._scratch_main() == 2
    assert capsys.readouterr().out.startswith("Refused: ")


class FailingFinds(Keychain):
    """Every value read exits 36 (as a locked Keychain can); existence checks work."""

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[1] == "find-generic-password" and "-w" in args:
            return subprocess.CompletedProcess(args, 36, "", "")
        return super().__call__(args, **kwargs)


def test_a_failure_after_the_folders_moved_says_run_install_again(tmp_dir: Path) -> None:
    keychain = FailingFinds()
    old = build_old(tmp_dir, keychain)
    t, said = said_tools(Launchctl({old.label}), keychain)
    assert run_move(tmp_dir, old, t) == 1
    assert not any(folder.exists() for folder in old.folders())
    assert said[-1].startswith("Move failed: copying the Telegram credentials")
    assert said[-1].count(rename_install.AGAIN) == 1


def test_an_unchecked_keychain_alone_skips_the_move(tmp_dir: Path) -> None:
    def broken(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 36, "", "")

    launchctl = Launchctl()
    t, said = said_tools(launchctl)
    t = replace(t, security=broken)
    calls: list[int] = []

    def install() -> int:
        calls.append(1)
        return 0

    old = old_install(tmp_dir / "none")
    code = rename_install.run(
        old, switchover=False, install=install, tools=t, new=new_install(tmp_dir)
    )
    assert code == 0 and calls == [1] and launchctl.calls == []
    assert said == [
        "Couldn't check the Keychain for an old dev-watchman install; skipping the move"
    ]
