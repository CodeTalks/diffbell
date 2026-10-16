"""The integration tests run on a private Watchman (Plan 6 spec §2, guard 1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pywatchman

from tests.private_watchman import PrivateWatchman

pytestmark = pytest.mark.integration


def test_pywatchman_resolves_the_private_socket(test_watchman: PrivateWatchman) -> None:
    """Guard 1: the resolution WatchmanClient goes through (WATCHMAN_SOCK first)."""
    assert pywatchman.client()._resolvesockname().unix_domain == str(test_watchman.sock)


def test_every_integration_test_gets_the_private_watchman_without_asking() -> None:
    sock = Path(os.environ["WATCHMAN_SOCK"])
    assert sock.name == "sock" and sock.parent.name.startswith("dfwm-")
    assert sock.is_socket()


def test_the_shared_root_lives_on_the_private_watchman(
    test_watchman: PrivateWatchman, shared_root: Path
) -> None:
    assert str(shared_root) in test_watchman.query("watch-list")["roots"]
