"""Shared fixtures. All temp paths are realpath'd: on macOS /var → /private/var,
and Watchman reports realpaths, so comparing un-resolved paths would fail.

Integration tests run on a private Watchman (``test_watchman``, ADR 0014), never
the user's: every ``integration``-marked test gets the fixture, and every fixture
command goes through pywatchman on the private socket."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import pywatchman

from tests.helpers import HAS_WATCHMAN
from tests.private_watchman import PrivateWatchman, TestWatchmanError, private_session


def pytest_configure(config: pytest.Config) -> None:
    """The belt before any test, fixture or import-time code runs: no real switchover
    (rename spec §3). The autouse fixture below re-asserts it per test."""
    from diffbell import rename_install

    rename_install.REAL_SWITCHOVER_ALLOWED = False


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every integration test runs on the private Watchman; a unit-only run never
    starts one."""
    for item in items:
        if item.get_closest_marker("integration") is None:
            continue
        names = getattr(item, "fixturenames", None)
        if names is not None and "test_watchman" not in names:
            names.insert(0, "test_watchman")


@pytest.fixture(scope="session")
def test_watchman() -> Iterator[PrivateWatchman]:
    """A private Watchman for the session, with ``WATCHMAN_SOCK`` pointing at it.

    Guard 2: the user's watch list (CLI, default socket, ``--no-spawn --no-local``) is read
    before the start and after the stop; any difference fails the session. If the
    private instance does not start, every integration test fails quoting its
    output: there is no fallback to the user's Watchman. ``WATCHMAN_SOCK`` keeps
    pointing at the stopped instance afterwards (``private_session``)."""
    if not HAS_WATCHMAN:
        pytest.skip("watchman not installed")
    try:
        with private_session() as private:
            yield private
    except TestWatchmanError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture(autouse=True)
def no_real_switchover(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test ever moves the real dev-watchman install (rename spec §3)."""  # old-name
    from diffbell import rename_install

    monkeypatch.setattr(rename_install, "REAL_SWITCHOVER_ALLOWED", False)


@pytest.fixture(autouse=True)
def no_real_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test gets the real launchctl and Keychain runners from Tools.real(); a test
    that needs it patches its own fake over this."""
    from diffbell import rename_install

    def refuse() -> rename_install.Tools:
        pytest.fail("a test reached the real launchctl/Keychain runners")

    monkeypatch.setattr(rename_install.Tools, "real", refuse)


@pytest.fixture(autouse=True)
def watchman_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test sees a fake Watchman binary, on the agent's PATH and the daemon's,
    so no test depends on whether this Mac has one or reads the real disk for it.
    The "missing" tests override one of these attributes with None."""
    from diffbell import launchd, watcher

    monkeypatch.setattr(launchd, "find_watchman", lambda: Path("/opt/test/bin/watchman"))
    monkeypatch.setattr(watcher, "find_binary", lambda: "/opt/test/bin/watchman")


@pytest.fixture(autouse=True)
def never_under_launchd(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test's daemon believes launchd runs it, so none stops itself for a "newer
    install" (ADR 0022), whatever the shell's XPC_SERVICE_NAME. Tests of the guard
    inject `managed=` or call `under_launchd` with an explicit environment."""
    from diffbell import selfupdate

    monkeypatch.setattr(selfupdate, "under_launchd", lambda environ=None: False)


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return Path(os.path.realpath(tmp_path))


@pytest.fixture
def tmp_project(tmp_dir: Path) -> Path:
    project = tmp_dir / "proj"
    project.mkdir()
    return project


@pytest.fixture
def watchman_cleanup(test_watchman: PrivateWatchman) -> Iterator[list[Path]]:
    """Yield a list; every path appended is `watch-del`'d (on the private
    Watchman) after the test.

    Only for the few tests that need a watch root of their own: every root costs
    the Watchman server an FSEvents stream. Prefer ``shared_dir``.
    """
    roots: list[Path] = []
    yield roots
    for root in roots:
        with contextlib.suppress(pywatchman.WatchmanError):
            test_watchman.query("watch-del", str(root))


@pytest.fixture(scope="session")
def shared_root(
    test_watchman: PrivateWatchman, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """ONE Watchman root for the whole session (a `.watchmanconfig` marks it).

    Integration tests work in subdirectories of it (``shared_dir``), so
    `watch-project` on a test's project returns this root plus a relative_path
    instead of creating a new root per test. Removed with `watch-del` at the end.
    """
    root = Path(os.path.realpath(tmp_path_factory.mktemp("watched")))
    (root / ".watchmanconfig").write_text("{}")
    try:
        watched = test_watchman.query("watch-project", str(root)).get("watch")
    except pywatchman.WatchmanError as exc:
        pytest.fail(f"watch-project {root} failed on the test Watchman: {exc}")
    if watched != str(root):
        pytest.fail(f"watch-project {root} returned {watched!r}, not the shared root")
    try:
        yield root
    finally:
        with contextlib.suppress(pywatchman.WatchmanError):
            test_watchman.query("watch-del", str(root))


@pytest.fixture
def shared_dir(shared_root: Path, request: pytest.FixtureRequest) -> Path:
    """A fresh, uniquely named directory directly under the shared watch root."""
    prefix = "".join(c if c.isalnum() else "_" for c in request.node.name)[:40] + "-"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=shared_root))


@pytest.fixture
def utc() -> Iterator[None]:
    """The process's local time zone is UTC for the test (messages print local
    times), restored afterwards."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
