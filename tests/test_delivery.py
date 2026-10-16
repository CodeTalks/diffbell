"""The shared classification and backoff (health spec §4.3, §8 "Health delivery")."""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

import diffbell
from diffbell.config import BUILTIN_REDACT, ResolvedProject
from diffbell.delivery import Failure, backoff, classify
from diffbell.fingerprint import Observation
from diffbell.models import ChangeKind
from diffbell.notifier import Notifier, NotifierSettings
from diffbell.store import ClockState, Store
from diffbell.telegram import ErrorKind, TelegramError
from tests.fakes import FakeSender
from tests.helpers import sync_names

SRC = Path(diffbell.__file__).parent


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(0, 30.0), (1, 60.0), (2, 120.0), (5, 960.0), (6, 1800.0), (40, 1800.0)],
)
def test_backoff_doubles_from_30_seconds_up_to_30_minutes(attempts: int, seconds: float) -> None:
    assert backoff(attempts) == seconds


def test_a_429_waits_its_retry_after() -> None:
    exc = TelegramError(ErrorKind.RETRYABLE, "429", retry_after=7)
    assert classify(exc, 3) == Failure(ErrorKind.RETRYABLE, 7.0)


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_every_other_failure_waits_the_backoff(kind: ErrorKind) -> None:
    assert classify(TelegramError(kind, "boom"), 2) == Failure(kind, 120.0)


def imports(module: str) -> set[str]:
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_delivery_depends_on_telegram_only() -> None:
    ours = {n for n in imports("delivery") if n.startswith("diffbell")}
    assert ours == {"diffbell.telegram"}


# -- the notifier uses it: same classification, same delays ----------------------


@pytest.mark.parametrize(
    "exc",
    [
        TelegramError(ErrorKind.RETRYABLE, "sendMessage: 502"),
        TelegramError(ErrorKind.RETRYABLE, "sendMessage: 429", retry_after=42),
    ],
)
async def test_the_notifier_defers_by_the_shared_delay(tmp_dir: Path, exc: TelegramError) -> None:
    store = Store(tmp_dir / "state.db")
    sync_names(store, "proj")
    (tmp_dir / "proj").mkdir()
    project = ResolvedProject("proj", tmp_dir / "proj", ("*",), ())
    notifier = Notifier(
        store,
        FakeSender([exc]),
        projects=lambda name: project,
        settings=lambda: NotifierSettings(redact=BUILTIN_REDACT, max_lines=20, web_enabled=False),
        clock=lambda: 1000.0,
    )
    obs = Observation(changes=[("package.json", ChangeKind.MODIFIED)])
    changes = store.record_observation("proj", ClockState("/w", "", "c:1", "h"), obs, now=1.0)
    event_id = await notifier.flush("proj", changes)
    assert event_id is not None
    event = store.get_event(event_id)
    assert (event.status, event.attempts) == ("pending", 1)
    assert event.next_attempt_at == 1000.0 + classify(exc, 0).delay


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_non_finite_retry_after_waits_the_backoff(value: float) -> None:
    exc = TelegramError(ErrorKind.RETRYABLE, "429", retry_after=value)
    assert classify(exc, 1) == Failure(ErrorKind.RETRYABLE, 60.0)
