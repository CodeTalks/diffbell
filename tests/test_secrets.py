from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from diffbell import secrets as secrets_mod
from diffbell.secrets import (
    ACCOUNT_BOT_TOKEN,
    ACCOUNT_CHAT_ID,
    ACCOUNT_SAVE_PENDING,
    DEFAULT_SERVICE,
    Credentials,
    Missing,
    SecretStoreError,
    credentials,
    delete_secret,
    get_secret,
    has_item,
    save_credentials,
    service,
    set_secret,
)

OVERRIDE = "DIFFBELL_KEYCHAIN_SERVICE"
SRC = Path(__file__).parents[1] / "src" / "diffbell"


@pytest.fixture(autouse=True)
def no_service_override(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A scratch override in the developer's shell must not leak into these tests."""
    monkeypatch.delenv(OVERRIDE, raising=False)
    yield


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


def test_get_secret_reads_password() -> None:
    runner = FakeRunner(stdout="tok123\n")
    assert get_secret("bot-token", runner=runner) == "tok123"
    args, _ = runner.calls[0]
    assert args[:2] == ["/usr/bin/security", "find-generic-password"]
    assert args[2:4] == ["-s", DEFAULT_SERVICE]


def test_get_secret_missing_returns_none() -> None:
    assert get_secret("bot-token", runner=FakeRunner(returncode=44)) is None


def test_get_secret_other_error_raises() -> None:
    with pytest.raises(SecretStoreError):
        get_secret("bot-token", runner=FakeRunner(returncode=51))


def test_set_secret_never_puts_value_in_argv() -> None:
    runner = FakeRunner()
    set_secret("bot-token", "s3cr3t", runner=runner)
    args, kwargs = runner.calls[0]
    assert "s3cr3t" not in " ".join(args)
    assert args[-1] == "-w"
    assert kwargs["input"] == "s3cr3t\ns3cr3t\n"


@pytest.mark.parametrize("bad", ["", "a\nb"])
def test_set_secret_rejects_unsafe_values(bad: str) -> None:
    with pytest.raises(ValueError):
        set_secret("bot-token", bad, runner=FakeRunner())


def test_set_secret_failure_raises_without_value() -> None:
    with pytest.raises(SecretStoreError) as exc:
        set_secret("bot-token", "s3cr3t", runner=FakeRunner(returncode=1))
    assert "s3cr3t" not in str(exc.value)


class TimeoutRunner(FakeRunner):
    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout") or 0)


def test_keychain_calls_are_bounded_by_a_timeout() -> None:
    runner = FakeRunner(stdout="tok\n")
    get_secret("bot-token", runner=runner)
    set_secret("bot-token", "s3cr3t", runner=runner)
    assert all(kwargs.get("timeout") == 10 for _, kwargs in runner.calls)


def test_get_secret_timeout_raises_secret_store_error() -> None:
    with pytest.raises(SecretStoreError, match="timed out"):
        get_secret("bot-token", runner=TimeoutRunner())


def test_set_secret_timeout_raises_without_value() -> None:
    with pytest.raises(SecretStoreError) as exc:
        set_secret("bot-token", "s3cr3t", runner=TimeoutRunner())
    assert "s3cr3t" not in str(exc.value)
    assert exc.value.__context__ is None and exc.value.__cause__ is None


def test_security_runs_in_a_new_session() -> None:
    # No controlling terminal: `security -w` cannot open /dev/tty and reads stdin.
    runner = FakeRunner(stdout="tok\n")
    get_secret("bot-token", runner=runner)
    set_secret("bot-token", "s3cr3t", runner=runner)
    assert [kwargs.get("start_new_session") for _, kwargs in runner.calls] == [True, True]


def test_get_secret_accepts_a_longer_timeout() -> None:
    runner = FakeRunner(stdout="tok\n")
    get_secret("bot-token", runner=runner, timeout=30)
    assert runner.calls[0][1]["timeout"] == 30


def test_get_secret_timeout_message_names_the_timeout() -> None:
    with pytest.raises(SecretStoreError, match="timed out after 30s"):
        get_secret("bot-token", runner=TimeoutRunner(), timeout=30)


# -- the service and its scratch override (bot onboarding spec §4) ------------------


def test_the_service_constant_is_gone() -> None:
    assert not hasattr(secrets_mod, "SERVICE")


def test_service_defaults_to_the_real_one() -> None:
    assert service() == DEFAULT_SERVICE == "io.688.diffbell"


def test_a_scratch_override_is_used_by_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERRIDE, "io.688.diffbell-scratch")
    runner = FakeRunner(stdout="tok\n")
    get_secret("bot-token", runner=runner)
    set_secret("bot-token", "s3cr3t", runner=runner)
    delete_secret("save-pending", runner=runner)
    assert [args[args.index("-s") + 1] for args, _ in runner.calls] == [
        "io.688.diffbell-scratch"
    ] * 3


@pytest.mark.parametrize(
    "bad", ["", "com.example.other", "io.688.dev-watchmen", "io.688.diffbellfoo"]
)
def test_an_override_outside_the_prefix_is_refused(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv(OVERRIDE, bad)
    runner = FakeRunner()
    with pytest.raises(SecretStoreError, match=OVERRIDE):
        get_secret("bot-token", runner=runner)
    with pytest.raises(SecretStoreError, match=OVERRIDE):
        set_secret("bot-token", "s3cr3t", runner=runner)
    with pytest.raises(SecretStoreError, match=OVERRIDE):
        delete_secret("save-pending", runner=runner)
    assert runner.calls == []


# -- delete_secret ------------------------------------------------------------------


def test_delete_secret_runs_delete_generic_password() -> None:
    runner = FakeRunner()
    delete_secret("save-pending", runner=runner)
    args, kwargs = runner.calls[0]
    assert args == [
        "/usr/bin/security",
        "delete-generic-password",
        "-s",
        DEFAULT_SERVICE,
        "-a",
        "save-pending",
    ]
    assert kwargs["timeout"] == 10 and kwargs["start_new_session"] is True


def test_deleting_a_missing_item_counts_as_success() -> None:
    delete_secret("save-pending", runner=FakeRunner(returncode=44))


def test_a_failed_delete_raises() -> None:
    with pytest.raises(SecretStoreError, match="delete failed"):
        delete_secret("save-pending", runner=FakeRunner(returncode=51))


def test_a_delete_timeout_raises_secret_store_error() -> None:
    with pytest.raises(SecretStoreError, match="timed out"):
        delete_secret("save-pending", runner=TimeoutRunner())


# -- credentials(): the startup table (bot onboarding spec §1) ----------------------


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ({ACCOUNT_BOT_TOKEN: "t", ACCOUNT_CHAT_ID: "42"}, Credentials("t", "42")),
        ({}, Missing("both")),
        ({ACCOUNT_CHAT_ID: "42"}, Missing("bot token")),
        ({ACCOUNT_BOT_TOKEN: "t"}, Missing("chat id")),
        (
            {ACCOUNT_SAVE_PENDING: "1.5", ACCOUNT_BOT_TOKEN: "t", ACCOUNT_CHAT_ID: "42"},
            Missing("unfinished save"),
        ),
    ],
)
def test_credentials_follow_the_startup_table(
    items: dict[str, str], expected: Credentials | Missing
) -> None:
    assert credentials(get=lambda account: items.get(account)) == expected


def test_the_marker_is_read_first() -> None:
    seen: list[str] = []

    def get(account: str) -> str | None:
        seen.append(account)
        return None

    credentials(get=get)
    assert seen == [ACCOUNT_SAVE_PENDING, ACCOUNT_BOT_TOKEN, ACCOUNT_CHAT_ID]


def test_a_keychain_error_is_never_taken_for_not_set_up() -> None:
    def broken(account: str) -> str | None:
        raise SecretStoreError(f"keychain read failed for {account!r} (exit 1)")

    with pytest.raises(SecretStoreError):
        credentials(get=broken)


def test_missing_messages() -> None:
    assert Missing("both").message == "not set up"
    assert Missing("bot token").message == "not set up: the bot token is missing"
    assert Missing("chat id").message == "not set up: the chat id is missing"
    assert Missing("unfinished save").message == ("not set up: an earlier save didn't finish")


# -- save_credentials(): one order for both writers (bot onboarding spec §2) ---------


class KeychainRecorder:
    """put/delete stand-ins; the step numbered ``fail_at`` (0-based) raises."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.ops: list[tuple[str, str]] = []
        self.values: dict[str, str] = {}
        self.fail_at = fail_at

    def _step(self, op: tuple[str, str]) -> None:
        if len(self.ops) == self.fail_at:
            raise SecretStoreError(f"keychain write failed for {op[1]!r} (exit 1)")
        self.ops.append(op)

    def put(self, account: str, value: str) -> None:
        self._step(("put", account))
        self.values[account] = value

    def delete(self, account: str) -> None:
        self._step(("delete", account))


def test_save_writes_marker_chat_token_then_deletes_the_marker() -> None:
    keychain = KeychainRecorder()
    save_credentials("123:tok", "42", put=keychain.put, delete=keychain.delete)
    assert keychain.ops == [
        ("put", ACCOUNT_SAVE_PENDING),
        ("put", ACCOUNT_CHAT_ID),
        ("put", ACCOUNT_BOT_TOKEN),
        ("delete", ACCOUNT_SAVE_PENDING),
    ]
    assert keychain.values[ACCOUNT_CHAT_ID] == "42"
    assert keychain.values[ACCOUNT_BOT_TOKEN] == "123:tok"
    float(keychain.values[ACCOUNT_SAVE_PENDING])  # the marker holds the current time


@pytest.mark.parametrize("fail_at", [0, 1, 2, 3])
def test_a_failure_at_any_step_stops_the_save_and_keeps_the_marker(fail_at: int) -> None:
    keychain = KeychainRecorder(fail_at)
    with pytest.raises(SecretStoreError):
        save_credentials("123:tok", "42", put=keychain.put, delete=keychain.delete)
    assert len(keychain.ops) == fail_at  # nothing after the failing step
    assert ("delete", ACCOUNT_SAVE_PENDING) not in keychain.ops


@pytest.mark.parametrize(("token", "chat_id"), [("", "42"), ("123:tok", "4\n2"), ("123:tok", "")])
def test_save_validates_before_writing_anything(token: str, chat_id: str) -> None:
    keychain = KeychainRecorder()
    with pytest.raises(ValueError):
        save_credentials(token, chat_id, put=keychain.put, delete=keychain.delete)
    assert keychain.ops == []


def test_an_explicit_service_reaches_that_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERRIDE, "io.688.diffbell-scratch")
    runner = FakeRunner(stdout="v\n")
    get_secret("bot-token", service="io.688.other", runner=runner)
    delete_secret("bot-token", service="io.688.other", runner=runner)
    assert [args[3] for args, _ in runner.calls] == ["io.688.other", "io.688.other"]


def test_without_service_the_guard_applies_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OVERRIDE, "io.688.other")
    with pytest.raises(SecretStoreError):
        get_secret("bot-token", runner=FakeRunner())


def test_has_item_never_reads_the_value() -> None:
    runner = FakeRunner()
    assert has_item("chat-id", service="io.688.other", runner=runner) is True
    args, _ = runner.calls[0]
    assert args == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "io.688.other",
        "-a",
        "chat-id",
    ]
    assert has_item("chat-id", runner=FakeRunner(returncode=44)) is False
    with pytest.raises(SecretStoreError):
        has_item("chat-id", runner=FakeRunner(returncode=51))


def test_only_the_move_passes_an_explicit_service() -> None:
    """The one exception to service()'s guard (rename spec section 3): call sites only,
    so a docstring or comment can't trip it."""
    callers = set()
    for file in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and any(k.arg == "service" for k in node.keywords):
                callers.add(file.relative_to(SRC).as_posix())
    assert (SRC / "secrets.py").is_file()
    assert callers <= {"secrets.py", "rename_install.py"}
