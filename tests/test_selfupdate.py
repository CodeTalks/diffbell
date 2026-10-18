"""Noticing a newer install under the running daemon, and the update notice (ADR 0022)."""

from __future__ import annotations

import logging
import plistlib
from pathlib import Path

import pytest

from diffbell import selfupdate
from diffbell.selfupdate import Install, SelfUpdate, replaced
from diffbell.store import Store

BREW_0 = "/opt/homebrew/Cellar/diffbell/0.1.2"
BREW_1 = "/opt/homebrew/Cellar/diffbell/0.1.3"


# -- what counts as replaced ----------------------------------------------------------


def test_homebrew_is_judged_by_its_opt_link() -> None:
    start = Install("0.1.2", BREW_0)
    assert replaced(start, Install("0.1.2", BREW_1)) == "0.1.3"  # opt moved on
    assert replaced(start, Install("0.1.2", BREW_0)) is None
    # Uninstalled (no opt link): there is no new version to restart into.
    assert replaced(start, Install(None, None)) is None
    # The old Cellar's metadata still says 0.1.2; only the link matters.
    assert replaced(start, Install("0.1.9", BREW_0)) is None


def test_other_installs_are_judged_by_the_installed_version() -> None:
    start = Install("0.1.2", None)
    assert replaced(start, Install("0.1.3", None)) == "0.1.3"
    assert replaced(start, Install("0.1.2", None)) is None
    assert replaced(start, Install(None, None)) is None  # unreadable: never a restart
    assert replaced(Install(None, None), Install("0.1.3", None)) is None


def test_brew_target_resolves_the_opt_link(tmp_dir: Path) -> None:
    prefix = tmp_dir.resolve()
    cellar = prefix / "Cellar" / "diffbell" / "0.1.3"
    cellar.mkdir(parents=True)
    (prefix / "opt").mkdir()
    (prefix / "opt" / "diffbell").symlink_to(cellar)
    assert selfupdate.brew_target(prefix) == str(cellar)
    assert selfupdate.brew_target(None) is None
    assert selfupdate.brew_target(prefix / "elsewhere") is None


def test_the_installed_version_is_this_packages() -> None:
    from diffbell import __version__

    assert selfupdate.installed_version() == __version__


# -- the guards and the pace --------------------------------------------------------------


class Disk:
    def __init__(self, install: Install) -> None:
        self.install = install
        self.reads = 0

    def __call__(self) -> Install:
        self.reads += 1
        return self.install


def updater(
    disk: Disk, *, managed: bool = True, program: Path | None = Path("/opt/homebrew/opt/x")
) -> SelfUpdate:
    return SelfUpdate(
        Install("0.1.2", BREW_0),
        "0.1.2",
        current=disk,
        managed=lambda: managed,
        program=lambda: program,
    )


def test_a_new_install_under_launchd_asks_for_a_restart(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="diffbell.selfupdate")
    disk = Disk(Install("0.1.2", BREW_0))
    check = updater(disk)
    assert check.check(1000.0) is None
    disk.install = Install("0.1.2", BREW_1)
    assert check.check(1030.0) is None  # within SELF_CHECK_EVERY: not even looked at
    assert disk.reads == 1
    assert check.check(1000.0 + selfupdate.SELF_CHECK_EVERY) == "0.1.3"
    assert "Diffbell 0.1.3 is installed (running 0.1.2): restarting" in caplog.text


def test_a_fresh_start_never_sees_a_change() -> None:
    """The start snapshot is taken from the same disk: no restart loop."""
    disk = Disk(Install("0.1.3", BREW_1))
    fresh = SelfUpdate(
        disk(), "0.1.3", current=disk, managed=lambda: True, program=lambda: Path("/x")
    )
    assert [fresh.check(t * 60.0) for t in range(5)] == [None] * 5


@pytest.mark.parametrize(("managed", "program"), [(False, Path("/x")), (True, None)])
def test_outside_launchd_or_without_a_program_it_only_warns_once(
    managed: bool, program: Path | None, caplog: pytest.LogCaptureFixture
) -> None:
    disk = Disk(Install("0.1.2", BREW_1))
    check = updater(disk, managed=managed, program=program)
    assert check.check(0.0) is None and check.check(60.0) is None
    assert caplog.text.count("restart it to run the new version") == 1


def test_under_launchd_means_launchd_is_the_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the conftest belt replaces it for every other test
    assert selfupdate.under_launchd(lambda: 1)  # launchd's child
    assert not selfupdate.under_launchd(lambda: 4242)  # a shell's


def test_plist_program_needs_an_executable(tmp_dir: Path) -> None:
    program = tmp_dir / "diffbell"
    plist = tmp_dir / "agent.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(program), "run"]}))
    assert selfupdate.plist_program(plist) is None  # not there yet
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o700)
    assert selfupdate.plist_program(plist) == program
    assert selfupdate.plist_program(tmp_dir / "missing.plist") is None


# -- the update notice --------------------------------------------------------------------


class Notices:
    def __init__(self) -> None:
        self.queued: list[tuple[str, str]] = []

    def notice(self, key: str, text: str) -> None:
        self.queued.append((key, text))


def test_the_notice_names_both_versions_once(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state.db")
    health = Notices()
    selfupdate.queue_update_notice(store, health, "0.1.2")  # type: ignore[arg-type]
    assert health.queued == []  # a first start says nothing
    selfupdate.queue_update_notice(store, health, "0.1.2")  # type: ignore[arg-type]
    assert health.queued == []  # same version: a plain restart
    selfupdate.queue_update_notice(store, health, "0.1.3")  # type: ignore[arg-type]
    assert health.queued == [("update", "⬆️ Diffbell updated: 0.1.2 → 0.1.3")]
    assert store.kv_get(selfupdate.RUNNING_VERSION_KEY) == "0.1.3"


def test_a_downgrade_is_not_called_an_update() -> None:
    assert selfupdate.update_text("0.1.3", "0.1.2") == "Diffbell changed version: 0.1.3 → 0.1.2"
    assert selfupdate.update_text("0.1.9", "0.1.10") == "⬆️ Diffbell updated: 0.1.9 → 0.1.10"
