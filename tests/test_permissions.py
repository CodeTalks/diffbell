"""Private permissions for diffbell's own files (spec 2026-10-09, Plan 4).

Every test works under tmp paths, never the real ~/.config, ~/.local/share or
~/Library/Logs. Creation tests run under a permissive umask (022) set and restored
by the fixture, so an explicit mode is what makes them pass, not the test runner's
own umask.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from diffbell import cli, launchd, paths
from diffbell.cli import PrivateRotatingFileHandler, configure_logging, main
from diffbell.config import write_default_config
from diffbell.lock import singleton_lock
from diffbell.paths import tighten, tighten_install
from diffbell.store import Store
from diffbell.web import config_edit

SRC = Path(__file__).resolve().parents[1] / "src"


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    noisy = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield
    for handler in root.handlers:
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in noisy.items():
        logging.getLogger(name).setLevel(level)


# -- tighten -------------------------------------------------------------------------


def test_tighten_clears_group_and_other_bits(tmp_dir: Path) -> None:
    f = tmp_dir / "f"
    f.write_text("x")
    f.chmod(0o644)
    d = tmp_dir / "d"
    d.mkdir()
    d.chmod(0o755)
    assert tighten(f, kind="file") is True
    assert tighten(d, kind="dir") is True
    assert mode(f) == 0o600
    assert mode(d) == 0o700
    assert tighten(f, kind="file") is False  # already private: nothing to change


def test_tighten_never_loosens(tmp_dir: Path) -> None:
    f = tmp_dir / "f"
    f.write_text("x")
    f.chmod(0o400)
    assert tighten(f, kind="file") is False
    assert mode(f) == 0o400
    f.chmod(0o440)
    assert tighten(f, kind="file") is True
    assert mode(f) == 0o400


def test_tighten_skips_a_symlink_and_leaves_its_target(tmp_dir: Path) -> None:
    target = tmp_dir / "target"
    target.write_text("x")
    target.chmod(0o644)
    link = tmp_dir / "link"
    link.symlink_to(target)
    assert tighten(link, kind="file") is False
    assert mode(target) == 0o644
    target_dir = tmp_dir / "tdir"
    target_dir.mkdir()
    target_dir.chmod(0o755)
    dir_link = tmp_dir / "dlink"
    dir_link.symlink_to(target_dir)
    assert tighten(dir_link, kind="dir") is False
    assert mode(target_dir) == 0o755


def test_tighten_skips_a_path_owned_by_another_uid(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_dir / "f"
    f.write_text("x")
    f.chmod(0o644)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert tighten(f, kind="file") is False
    assert mode(f) == 0o644


def test_tighten_of_a_missing_path_is_a_no_op(tmp_dir: Path) -> None:
    assert tighten(tmp_dir / "nope", kind="file") is False
    assert tighten(tmp_dir / "nope", kind="dir") is False


def test_tighten_logs_the_change(tmp_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    f = tmp_dir / "f"
    f.write_text("x")
    f.chmod(0o644)
    with caplog.at_level(logging.INFO, logger="diffbell"):
        tighten(f, kind="file")
    assert any(str(f) in r.getMessage() and "644" in r.getMessage() for r in caplog.records)


# -- tighten_install -----------------------------------------------------------------


def wide_layout(root: Path) -> tuple[Path, Path, Path]:
    """A pre-Plan-4 install: dirs 755, files 644."""
    cfg = root / "diffbell" / "config.yaml"
    state = root / "state"
    logs = root / "logs"
    for d in (cfg.parent, state, logs):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o755)
    cfg.write_text("projects: []\n")
    names = ["state.db", "state.db-wal", "state.db-shm", "status.json", "daemon.lock"]
    for name in names:
        (state / name).write_text("x")
    for name in ["diffbell.log", "diffbell.log.1", "diffbell.log.3"]:
        (logs / name).write_text("x")
    (logs / "launchd.err.log").write_text("")
    for f in [cfg, *state.iterdir(), *logs.iterdir()]:
        f.chmod(0o644)
    return cfg, state, logs


def test_tighten_install_makes_a_wide_layout_private(tmp_dir: Path) -> None:
    cfg, state, logs = wide_layout(tmp_dir)
    tighten_install(cfg, state, logs)
    for d in (cfg.parent, state, logs):
        assert mode(d) == 0o700, d
    for f in [cfg, *state.iterdir(), *logs.iterdir()]:
        assert mode(f) == 0o600, f


def test_tighten_install_leaves_parents_and_unknown_files_alone(tmp_dir: Path) -> None:
    base = tmp_dir / "home"
    base.mkdir()
    base.chmod(0o755)
    cfg, state, logs = wide_layout(base)
    other = state / "someone-elses.txt"
    other.write_text("x")
    other.chmod(0o644)
    tighten_install(cfg, state, logs)
    assert mode(base) == 0o755
    assert mode(other) == 0o644


def test_tighten_install_tightens_a_symlinked_configs_target_only(tmp_dir: Path) -> None:
    dotfiles = tmp_dir / "dotfiles"
    dotfiles.mkdir()
    dotfiles.chmod(0o755)
    real = dotfiles / "diffbell.yaml"
    real.write_text("projects: []\n")
    real.chmod(0o644)
    cfg = tmp_dir / "diffbell" / "config.yaml"
    cfg.parent.mkdir()
    cfg.parent.chmod(0o755)
    cfg.symlink_to(real)
    tighten_install(cfg, tmp_dir / "state", tmp_dir / "logs")
    assert cfg.is_symlink()
    assert mode(real) == 0o600
    assert mode(dotfiles) == 0o755  # the target's parent is left alone
    assert mode(cfg.parent) == 0o700


def test_tighten_install_skips_a_symlinked_state_dir(tmp_dir: Path) -> None:
    real_state = tmp_dir / "elsewhere"
    real_state.mkdir()
    real_state.chmod(0o755)
    (real_state / "state.db").write_text("x")
    (real_state / "state.db").chmod(0o644)
    state = tmp_dir / "state"
    state.symlink_to(real_state)
    tighten_install(tmp_dir / "diffbell" / "config.yaml", state, tmp_dir / "logs")
    assert mode(real_state) == 0o755
    assert mode(real_state / "state.db") == 0o644


def test_tighten_install_leaves_a_shared_config_dir_alone(tmp_dir: Path) -> None:
    shared = tmp_dir / "dotfiles"  # DIFFBELL_CONFIG=~/dotfiles/dw.yaml, or $HOME
    shared.mkdir()
    shared.chmod(0o755)
    cfg = shared / "dw.yaml"
    cfg.write_text("projects: []\n")
    cfg.chmod(0o644)
    tighten_install(cfg, tmp_dir / "state", tmp_dir / "logs")
    assert mode(shared) == 0o755
    assert mode(cfg) == 0o600


def test_tighten_install_returns_what_it_changed(tmp_dir: Path) -> None:
    cfg, state, logs = wide_layout(tmp_dir)
    (state / "daemon.lock").chmod(0o600)  # already private: not reported
    changed = tighten_install(cfg, state, logs)
    assert (cfg, 0o644, 0o600) in changed
    assert (cfg.parent, 0o755, 0o700) in changed
    assert (state, 0o755, 0o700) in changed
    assert all(path != state / "daemon.lock" for path, _, _ in changed)
    assert tighten_install(cfg, state, logs) == []


def test_tighten_install_on_a_fresh_machine_is_a_no_op(tmp_dir: Path) -> None:
    tighten_install(tmp_dir / "diffbell" / "config.yaml", tmp_dir / "state", tmp_dir / "logs")
    assert sorted(p.name for p in tmp_dir.iterdir()) == []


def test_tighten_install_is_best_effort(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg, state, logs = wide_layout(tmp_dir)
    real_chmod = os.chmod
    refused = state / "state.db"

    def chmod(path: Any, new_mode: int, **kwargs: Any) -> None:
        if Path(path) == refused:
            raise PermissionError(1, "Operation not permitted", str(path))
        real_chmod(path, new_mode, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)
    with caplog.at_level(logging.WARNING, logger="diffbell"):
        tighten_install(cfg, state, logs)  # never raises
    assert mode(refused) == 0o644
    assert mode(state / "status.json") == 0o600  # the others are still tightened
    assert mode(logs / "diffbell.log") == 0o600
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# -- creation sites ------------------------------------------------------------------


@pytest.mark.usefixtures("permissive_umask")
def test_write_default_config_creates_private_dir_and_file(tmp_dir: Path) -> None:
    path = tmp_dir / "cfg" / "config.yaml"
    assert write_default_config(path) is True
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700


@pytest.mark.usefixtures("permissive_umask")
def test_config_edit_creates_a_new_file_private(tmp_dir: Path) -> None:
    path = tmp_dir / "new.yaml"
    config_edit._atomic_write(path, b"projects: []\n")
    assert mode(path) == 0o600


@pytest.mark.usefixtures("permissive_umask")
def test_config_edit_keeps_an_existing_files_mode(tmp_dir: Path) -> None:
    path = tmp_dir / "old.yaml"
    path.write_text("projects: []\n")
    path.chmod(0o640)
    config_edit._atomic_write(path, b"projects: []\n# edited\n")
    assert mode(path) == 0o640


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.mark.usefixtures("permissive_umask")
def test_launchd_install_creates_a_private_crash_log(tmp_dir: Path) -> None:
    logs = tmp_dir / "logs"
    launchd.install(
        Path("/u/bin/diffbell"),
        plist=tmp_dir / "agents" / "x.plist",
        log_dir=logs,
        uid=501,
        runner=Recorder(),
    )
    assert mode(logs) == 0o700
    assert mode(logs / "launchd.err.log") == 0o600
    assert mode(tmp_dir / "agents") == 0o755  # LaunchAgents is not ours to tighten


@pytest.mark.usefixtures("permissive_umask")
def test_launchd_install_truncates_and_narrows_an_old_crash_log(tmp_dir: Path) -> None:
    logs = tmp_dir / "logs"
    logs.mkdir()
    old = logs / "launchd.err.log"
    old.write_text("old crash")
    old.chmod(0o644)
    launchd.install(
        Path("/u/bin/diffbell"),
        plist=tmp_dir / "x.plist",
        log_dir=logs,
        uid=501,
        runner=Recorder(),
    )
    assert old.read_text() == ""
    assert mode(old) == 0o600


def test_launchd_install_refuses_a_symlinked_crash_log(tmp_dir: Path) -> None:
    logs = tmp_dir / "logs"
    logs.mkdir()
    elsewhere = tmp_dir / "elsewhere.log"
    elsewhere.write_text("keep me")
    elsewhere.chmod(0o644)
    (logs / "launchd.err.log").symlink_to(elsewhere)
    runner = Recorder()
    with pytest.raises(launchd.LaunchdError, match="cannot prepare"):
        launchd.install(
            Path("/u/bin/dw"), plist=tmp_dir / "x.plist", log_dir=logs, uid=501, runner=runner
        )
    assert elsewhere.read_text() == "keep me" and mode(elsewhere) == 0o644
    assert runner.calls == []


def test_launchd_install_refuses_a_crash_log_owned_by_another_uid(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_dir / "logs"
    logs.mkdir()
    crash = logs / "launchd.err.log"
    crash.write_text("someone else's")
    crash.chmod(0o644)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(launchd.LaunchdError, match="cannot prepare"):
        launchd.install(
            Path("/u/bin/dw"), plist=tmp_dir / "x.plist", log_dir=logs, uid=501, runner=Recorder()
        )
    assert crash.read_text() == "someone else's"  # refused, not truncated
    assert mode(crash) == 0o644


def test_install_reports_a_bad_crash_log_cleanly(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = isolated / "diffbell" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("projects: []\n")
    logs = isolated / "logs"
    logs.mkdir()
    (logs / "launchd.err.log").symlink_to(isolated / "elsewhere.log")
    real_install = launchd.install
    monkeypatch.setattr(
        launchd,
        "install",
        functools.partial(real_install, plist=isolated / "x.plist", uid=501, runner=Recorder()),
    )
    monkeypatch.setattr(launchd, "find_executable", lambda: Path("/u/bin/diffbell"))
    monkeypatch.setattr(launchd, "agent_pid", lambda: None)
    assert main(["install"]) == 1
    out = capsys.readouterr().out
    assert "Install failed: cannot prepare" in out and "Traceback" not in out


@pytest.mark.usefixtures("permissive_umask", "restore_logging")
def test_configure_logging_creates_a_private_dir_and_file(tmp_dir: Path) -> None:
    log_file = tmp_dir / "logs" / "diffbell.log"
    configure_logging(log_file, verbose=False)
    assert mode(log_file.parent) == 0o700
    assert mode(log_file) == 0o600


@pytest.mark.usefixtures("permissive_umask")
def test_store_and_lock_create_private_dirs(tmp_dir: Path) -> None:
    store = Store(tmp_dir / "state" / "state.db")
    store.close()
    assert mode(tmp_dir / "state") == 0o700
    with singleton_lock(tmp_dir / "lockdir" / "daemon.lock"):
        pass
    assert mode(tmp_dir / "lockdir") == 0o700


# -- log rotation --------------------------------------------------------------------


@pytest.mark.usefixtures("permissive_umask")
def test_rotating_handler_keeps_every_file_private(tmp_dir: Path) -> None:
    log_file = tmp_dir / "diffbell.log"
    handler = PrivateRotatingFileHandler(log_file, maxBytes=1024, backupCount=3, encoding="utf-8")
    try:
        assert mode(log_file) == 0o600
        handler.emit(logging.makeLogRecord({"msg": "first"}))
        handler.doRollover()
        assert mode(log_file) == 0o600
        assert mode(tmp_dir / "diffbell.log.1") == 0o600
    finally:
        handler.close()


@pytest.mark.usefixtures("restore_logging")
def test_logging_to_a_log_symlinked_to_dev_null_does_not_fail(tmp_dir: Path) -> None:
    log_file = tmp_dir / "logs" / "diffbell.log"
    log_file.parent.mkdir()
    log_file.symlink_to("/dev/null")
    configure_logging(log_file, verbose=False)
    logging.getLogger("diffbell").warning("still fine")
    assert log_file.is_symlink()


def test_rotating_handler_never_loosens_a_log(tmp_dir: Path) -> None:
    log_file = tmp_dir / "diffbell.log"
    log_file.write_text("")
    log_file.chmod(0o200)  # write-only: must not gain the read bit
    handler = PrivateRotatingFileHandler(log_file, maxBytes=1024, backupCount=3, encoding="utf-8")
    try:
        assert mode(log_file) == 0o200
    finally:
        handler.close()


@pytest.mark.usefixtures("permissive_umask")
def test_rotating_handler_narrows_an_existing_wide_log(tmp_dir: Path) -> None:
    log_file = tmp_dir / "diffbell.log"
    log_file.write_text("old\n")
    log_file.chmod(0o644)
    handler = PrivateRotatingFileHandler(log_file, maxBytes=1024, backupCount=3, encoding="utf-8")
    try:
        assert mode(log_file) == 0o600
        handler.doRollover()
        assert mode(log_file) == 0o600
        assert mode(tmp_dir / "diffbell.log.1") == 0o600
    finally:
        handler.close()


# -- daemon start and install --------------------------------------------------------


@pytest.fixture
def isolated(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("DIFFBELL_STATE_DIR", str(tmp_dir / "state"))
    monkeypatch.setenv("DIFFBELL_CONFIG", str(tmp_dir / "diffbell" / "config.yaml"))
    monkeypatch.setenv("DIFFBELL_LOG_DIR", str(tmp_dir / "logs"))
    monkeypatch.setattr(
        cli, "get_secret", lambda account, **kwargs: None if account == "save-pending" else "x"
    )
    yield tmp_dir


@pytest.mark.usefixtures("restore_logging")
def test_run_tightens_an_existing_install_before_the_daemon_starts(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, state, logs = wide_layout(isolated)
    seen: list[int] = []

    async def fake_daemon(creds: Any) -> int:
        seen.append(mode(state / "state.db"))
        return 0

    monkeypatch.setattr(cli, "_run_daemon", fake_daemon)
    assert main(["run", "--log-file", str(logs / "diffbell.log")]) == 0
    assert seen == [0o600]
    assert mode(cfg) == 0o600 and mode(cfg.parent) == 0o700
    assert mode(state) == 0o700 and mode(logs) == 0o700
    assert mode(logs / "diffbell.log.1") == 0o600


@pytest.mark.usefixtures("restore_logging")
def test_run_starts_even_if_tightening_fails(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wide_layout(isolated)

    def refuse(path: Any, new_mode: int, **kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(os, "chmod", refuse)
    started: list[bool] = []

    async def fake_daemon(state: Any) -> int:
        started.append(True)
        return 0

    monkeypatch.setattr(cli, "_run_daemon", fake_daemon)
    assert main(["run"]) == 0
    assert started == [True]


def test_install_tightens_an_existing_install(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, state, logs = wide_layout(isolated)
    monkeypatch.setattr(launchd, "find_executable", lambda: Path("/u/bin/diffbell"))
    monkeypatch.setattr(launchd, "install", lambda executable, *, log_dir: isolated / "x.plist")
    monkeypatch.setattr(launchd, "agent_pid", lambda: None)
    monkeypatch.setattr(cli, "_wait_until_ready", lambda started: 0)  # no daemon here
    assert main(["install"]) == 0
    assert mode(cfg) == 0o600 and mode(cfg.parent) == 0o700
    assert mode(state) == 0o700 and mode(state / "status.json") == 0o600
    assert mode(logs) == 0o700 and mode(logs / "launchd.err.log") == 0o600


def test_install_prints_what_it_tightened(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, state, _ = wide_layout(isolated)
    monkeypatch.setattr(launchd, "find_executable", lambda: Path("/u/bin/diffbell"))
    monkeypatch.setattr(launchd, "install", lambda executable, *, log_dir: isolated / "x.plist")
    monkeypatch.setattr(launchd, "agent_pid", lambda: None)
    monkeypatch.setattr(cli, "_wait_until_ready", lambda started: 0)  # no daemon here
    assert main(["install"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert f"tightened {cfg}: 644 -> 600" in out
    assert f"tightened {state}: 755 -> 700" in out
    assert main(["install"]) == 0
    assert "tightened" not in capsys.readouterr().out  # nothing left to change


# -- no process-wide umask -----------------------------------------------------------


def test_src_never_changes_the_umask() -> None:
    offenders = [
        f"{py.relative_to(SRC)}:{n}"
        for py in sorted(SRC.rglob("*.py"))
        for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bumask\s*\(|import\s+.*\bumask\b", line)
    ]
    assert offenders == []


def test_paths_module_is_the_tightening_home() -> None:
    assert paths.tighten is tighten and paths.tighten_install is tighten_install


def test_loose_modes_reports_what_tighten_install_would_change_and_changes_nothing(
    tmp_dir: Path,
) -> None:
    cfg, state, logs = wide_layout(tmp_dir)
    own = [cfg.parent, state, logs, cfg, *state.iterdir(), *logs.iterdir()]
    before = {p: mode(p) for p in own}
    found = paths.loose_modes(cfg, state, logs)
    assert {p: mode(p) for p in own} == before  # read-only
    assert found == tighten_install(cfg, state, logs)
    assert paths.loose_modes(cfg, state, logs) == []


def test_the_install_source_file_is_one_of_ours() -> None:
    assert "install-source.json" in paths.STATE_FILES
