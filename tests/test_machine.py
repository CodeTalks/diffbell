"""This Mac's name in messages (machine name spec §1, ADR 0023). `scutil` is never
run: every test injects the runner. The conftest belt replaces
`machine.computer_name`, so the real function is taken at import time."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from diffbell import BRAND, machine

REAL_COMPUTER_NAME = machine.computer_name


def runner(stdout: str = "", returncode: int = 0, exc: BaseException | None = None) -> Any:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_the_computer_name_comes_from_scutil() -> None:
    run = runner("Work MBP\n")
    assert REAL_COMPUTER_NAME(run=run) == "Work MBP"
    argv, kwargs = run.calls[0]
    assert argv == ["/usr/sbin/scutil", "--get", "ComputerName"]
    assert kwargs["timeout"] == 2
    assert kwargs["check"] is False


@pytest.mark.parametrize(
    "run",
    [
        runner("", returncode=1),
        runner("   \n"),
        runner(exc=OSError("no scutil")),
        runner(exc=subprocess.TimeoutExpired("scutil", 2)),
    ],
)
def test_without_scutil_the_host_name_is_used_without_local(
    monkeypatch: pytest.MonkeyPatch, run: Any
) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "studio.local")
    assert REAL_COMPUTER_NAME(run=run) == "studio"


def test_no_name_at_all_is_none_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> str:
        raise OSError("no host name")

    monkeypatch.setattr("socket.gethostname", broken)
    assert REAL_COMPUTER_NAME(run=runner(exc=OSError())) is None
    monkeypatch.setattr("socket.gethostname", lambda: "")
    assert REAL_COMPUTER_NAME(run=runner(returncode=1)) is None
    monkeypatch.setattr("socket.gethostname", lambda: ".local")
    assert REAL_COMPUTER_NAME(run=runner(returncode=1)) is None


def test_an_unexpected_runner_error_is_still_never_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "mini")
    assert REAL_COMPUTER_NAME(run=runner(exc=RuntimeError("boom"))) == "mini"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  Work MBP  ", "Work MBP"),
        ("Work​ MBP", "Work MBP"),  # format (zero-width space)
        ("Work‮MBP", "WorkMBP"),  # bidi override
        ("Work\x07\nMBP", "Work MBP"),  # the bell dropped; the newline separates words
        ("Work \t  MBP", "Work MBP"),  # tabs are control characters: dropped
        ("Work    MBP", "Work MBP"),
        ("Work  MBP", "Work MBP"),  # other whitespace collapses too
        ("x" * 50, "x" * 40),
        ("​‎", ""),
        ("Felix’s MacBook", "Felix’s MacBook"),
    ],
)
def test_clean(text: str, expected: str) -> None:
    assert machine.clean(text) == expected


def test_max_name_is_forty() -> None:
    assert machine.MAX_NAME == 40


def test_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "computer_name", lambda: "  Felix's​ Mac ")
    assert machine.resolve(None) == "Felix's Mac"
    assert machine.resolve("") is None
    assert machine.resolve("  ") is None
    assert machine.resolve("​") is None
    assert machine.resolve(" Work  MBP ") == "Work MBP"
    monkeypatch.setattr(machine, "computer_name", lambda: None)
    assert machine.resolve(None) is None
    monkeypatch.setattr(machine, "computer_name", lambda: "​")
    assert machine.resolve(None) is None


def test_default_name_is_the_cleaned_computer_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "computer_name", lambda: " Studio​ ")
    assert machine.default_name() == "Studio"
    monkeypatch.setattr(machine, "computer_name", lambda: None)
    assert machine.default_name() is None


def test_signature() -> None:
    assert machine.signature(None) == BRAND
    assert machine.signature("Work MBP") == f"{BRAND} (Work MBP)"


def test_the_belt_names_every_test_mac() -> None:
    assert machine.computer_name() == "Test Mac"
    assert machine.resolve(None) == "Test Mac"


def test_control_whitespace_inside_a_name_becomes_a_space() -> None:
    """A tab or newline between words separates them; other control and bidi
    characters are dropped."""
    assert machine.clean("Work\tMBP\nTwo") == "Work MBP Two"
    assert machine.clean("Wo\u202erk") == "Work"
