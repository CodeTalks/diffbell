from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from diffbell.launchd import (
    LABEL,
    LAUNCHD_PATH,
    LaunchdError,
    agent_pid,
    agent_status,
    bootout,
    bootstrap,
    build_plist,
    find_executable,
    homebrew_prefix,
    install,
    running_from_homebrew,
    stable_executable,
    uninstall,
    watchman_on_agent_path,
)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")


def test_build_plist(tmp_dir: Path) -> None:
    plist = build_plist(Path("/u/bin/diffbell"), tmp_dir)
    assert plist["Label"] == LABEL == "io.688.diffbell"
    assert plist["ProgramArguments"] == [
        "/u/bin/diffbell",
        "run",
        "--log-file",
        str(tmp_dir / "diffbell.log"),
    ]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["ProcessType"] == "Standard"
    assert plist["EnvironmentVariables"]["PATH"] == LAUNCHD_PATH
    # Only PATH: the agent never inherits DIFFBELL_KEYCHAIN_SERVICE (scratch only).
    assert set(plist["EnvironmentVariables"]) == {"PATH"}
    for needed in ("/opt/homebrew/bin", "/usr/bin", "/bin"):
        assert needed in LAUNCHD_PATH.split(":")
    assert plist["StandardErrorPath"] == str(tmp_dir / "launchd.err.log")


def test_install_writes_plist_and_bootstraps(tmp_dir: Path) -> None:
    runner = Recorder()
    target = tmp_dir / "agents" / f"{LABEL}.plist"
    logs = tmp_dir / "logs"
    (logs).mkdir()
    (logs / "launchd.err.log").write_text("old crash")
    install(Path("/u/bin/diffbell"), plist=target, log_dir=logs, uid=501, runner=runner)
    assert plistlib.loads(target.read_bytes())["Label"] == LABEL
    assert runner.calls[0] == ["launchctl", "bootout", f"gui/501/{LABEL}"]
    assert runner.calls[-1] == ["launchctl", "bootstrap", "gui/501", str(target)]
    assert (logs / "launchd.err.log").read_text() == ""


def test_uninstall_boots_out_and_removes(tmp_dir: Path) -> None:
    runner = Recorder()
    target = tmp_dir / f"{LABEL}.plist"
    target.write_text("x")
    assert uninstall(plist=target, uid=501, runner=runner) is True
    assert runner.calls == [["launchctl", "bootout", f"gui/501/{LABEL}"]]
    assert not target.exists()


def test_uninstall_when_nothing_installed(tmp_dir: Path) -> None:
    def failing(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 3, "", "No such process")

    assert uninstall(plist=tmp_dir / "none.plist", uid=501, runner=failing) is False


def test_uninstall_true_when_only_bootout_succeeds(tmp_dir: Path) -> None:
    assert uninstall(plist=tmp_dir / "none.plist", uid=501, runner=Recorder()) is True


def test_uninstall_true_when_only_plist_existed(tmp_dir: Path) -> None:
    target = tmp_dir / "p.plist"
    target.write_text("x")

    def failing(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 3, "", "")

    assert uninstall(plist=target, uid=501, runner=failing) is True
    assert not target.exists()


def test_build_plist_rejects_relative_executable(tmp_dir: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        build_plist(Path("bin/diffbell"), tmp_dir)


class FlakyBootstrap(Recorder):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[1] == "bootstrap" and self.failures > 0:
            self.failures -= 1
            return subprocess.CompletedProcess(args, 5, "", "Input/output error")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_install_retries_bootstrap(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    runner = FlakyBootstrap(failures=2)
    target = tmp_dir / f"{LABEL}.plist"
    assert install(Path("/u/bin/dw"), plist=target, log_dir=tmp_dir, uid=1, runner=runner) == target
    assert sum(c[1] == "bootstrap" for c in runner.calls) == 3
    assert len(sleeps) == 2


def test_install_gives_up_without_a_final_sleep(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    runner = FlakyBootstrap(failures=3)
    with pytest.raises(LaunchdError, match="Input/output error"):
        install(Path("/u/bin/dw"), plist=tmp_dir / "x.plist", log_dir=tmp_dir, uid=1, runner=runner)
    assert sum(c[1] == "bootstrap" for c in runner.calls) == 3
    assert len(sleeps) == 2


def answering(returncode: int, stdout: str) -> Any:
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_agent_pid_parses_launchctl_print() -> None:
    out = f"gui/501/{LABEL} = {{\n\tactive count = 1\n\tstate = running\n\tpid = 4242\n}}\n"
    runner = answering(0, out)
    assert agent_pid(uid=501, runner=runner) == 4242
    assert runner.calls == [["launchctl", "print", f"gui/501/{LABEL}"]]


def test_agent_pid_none_when_not_running_or_not_loaded() -> None:
    assert agent_pid(uid=501, runner=answering(0, "\tstate = not running\n")) is None
    assert agent_pid(uid=501, runner=answering(113, "Could not find service")) is None


def test_agent_pid_none_when_launchctl_cannot_run() -> None:
    def broken(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("launchctl")

    assert agent_pid(uid=501, runner=broken) is None


def test_agent_status_tells_not_loaded_from_not_running() -> None:
    assert agent_status(uid=501, runner=answering(0, "\tstate = running\n\tpid = 7\n")) == (True, 7)
    assert agent_status(uid=501, runner=answering(0, "\tstate = not running\n")) == (True, None)
    assert agent_status(uid=501, runner=answering(113, "Could not find service")) == (False, None)


def test_agent_status_passes_its_timeout_and_survives_one() -> None:
    seen: list[Any] = []

    def slow(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    assert agent_status(uid=501, runner=slow, timeout=5.0) == (False, None)
    assert seen == [5.0]


class StatusRunner(Recorder):
    def __init__(self, codes: list[int]) -> None:
        super().__init__()
        self.codes = codes

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.codes.pop(0), "", "boom")


def test_bootout_names_the_label_and_ignores_the_exit() -> None:
    runner = StatusRunner([113])  # "no such service": never an error
    assert bootout("io.688.other", uid=501, runner=runner) is False
    assert runner.calls == [["launchctl", "bootout", "gui/501/io.688.other"]]


def test_bootstrap_retries_then_raises(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda s: None)
    runner = StatusRunner([5, 5, 5])
    with pytest.raises(LaunchdError, match="bootstrap failed: boom"):
        bootstrap(tmp_dir / "x.plist", uid=501, runner=runner)
    assert len(runner.calls) == 3


def test_agent_status_for_another_label() -> None:
    runner = Recorder()
    agent_status(label="io.688.other", uid=501, runner=runner)
    assert runner.calls == [["launchctl", "print", "gui/501/io.688.other"]]


def test_watchman_is_looked_up_on_the_agents_path() -> None:
    seen: list[tuple[str, str | None]] = []

    def which(cmd: str, path: str | None = None) -> str | None:
        seen.append((cmd, path))
        return "/opt/homebrew/bin/watchman"

    assert watchman_on_agent_path(which=which) == Path("/opt/homebrew/bin/watchman")
    assert seen == [("watchman", LAUNCHD_PATH)]
    assert watchman_on_agent_path(which=lambda cmd, path=None: None) is None


# -- Homebrew installs (ADR 0021) ----------------------------------------------------


def brew_layout(root: Path, version: str = "0.1.1") -> Path:
    """A Homebrew prefix with diffbell `version` in its Cellar, `opt/diffbell` pointing
    at it and `bin/diffbell` linked the way Homebrew links a virtualenv formula."""
    prefix = root.resolve() / "homebrew"
    script = prefix / "Cellar" / "diffbell" / version / "libexec" / "bin" / "diffbell"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    (prefix / "opt").mkdir()
    (prefix / "opt" / "diffbell").symlink_to(prefix / "Cellar" / "diffbell" / version)
    (prefix / "bin").mkdir()
    (prefix / "bin" / "diffbell").symlink_to(script)
    return prefix


def test_a_cellar_executable_becomes_the_stable_opt_path(tmp_dir: Path) -> None:
    prefix = brew_layout(tmp_dir)
    resolved = prefix / "Cellar" / "diffbell" / "0.1.1" / "libexec" / "bin" / "diffbell"
    assert (
        stable_executable(resolved) == prefix / "opt" / "diffbell" / "libexec" / "bin" / "diffbell"
    )
    assert homebrew_prefix(resolved) == prefix


def test_other_executables_are_kept(tmp_dir: Path) -> None:
    uv_tool = (
        tmp_dir.resolve() / ".local" / "share" / "uv" / "tools" / "diffbell" / "bin" / "diffbell"
    )
    assert stable_executable(uv_tool) == uv_tool
    assert homebrew_prefix(uv_tool) is None
    # Another formula's Cellar is not ours.
    other = tmp_dir.resolve() / "homebrew" / "Cellar" / "other" / "1.0" / "bin" / "diffbell"
    assert stable_executable(other) == other and homebrew_prefix(other) is None


def test_a_cellar_path_without_its_opt_link_is_kept(tmp_dir: Path) -> None:
    prefix = brew_layout(tmp_dir)
    (prefix / "opt" / "diffbell").unlink()
    resolved = prefix / "Cellar" / "diffbell" / "0.1.1" / "libexec" / "bin" / "diffbell"
    assert stable_executable(resolved) == resolved


def test_find_executable_gives_launchd_the_opt_path(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`brew upgrade` deletes the old Cellar folder: the plist must not name it."""
    prefix = brew_layout(tmp_dir)
    monkeypatch.setattr(shutil, "which", lambda name: str(prefix / "bin" / name))
    assert find_executable() == prefix / "opt" / "diffbell" / "libexec" / "bin" / "diffbell"


def test_find_executable_resolves_a_uv_tool_link(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_dir.resolve() / "tools" / "diffbell" / "bin" / "diffbell"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    link = tmp_dir.resolve() / "bin" / "diffbell"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(shutil, "which", lambda name: str(link))
    assert find_executable() == real


def test_running_from_homebrew_reads_the_interpreter_prefix(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = brew_layout(tmp_dir)
    monkeypatch.setattr(sys, "prefix", str(prefix / "opt" / "diffbell" / "libexec"))
    assert running_from_homebrew() == prefix
    monkeypatch.setattr(sys, "prefix", str(tmp_dir.resolve() / "venv"))
    assert running_from_homebrew() is None
