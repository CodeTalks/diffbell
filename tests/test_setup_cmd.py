from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from diffbell import BRAND
from diffbell.secrets import SecretStoreError, save_credentials
from diffbell.setup_cmd import RESTART_HINT, SetupIO, run_setup
from diffbell.telegram import ErrorKind, TelegramError

TOKEN = "123:SECRET-TOKEN"


class FakeBot:
    def __init__(
        self,
        *,
        webhook: str = "",
        updates: list[dict[str, Any]] | None = None,
        bad_token: bool = False,
        update_batches: list[Any] | None = None,
        webhook_error: TelegramError | None = None,
    ) -> None:
        self.webhook_error = webhook_error
        self.update_batches = update_batches
        self.update_calls = 0
        self.webhook = webhook
        self.updates = (
            updates
            if updates is not None
            else [
                {
                    "update_id": 1,
                    "message": {"chat": {"id": 555, "type": "private", "username": "alex"}},
                }
            ]
        )
        self.bad_token = bad_token
        self.sent: list[str] = []
        self.closed = 0

    async def get_me(self) -> dict[str, Any]:
        if self.bad_token:
            raise TelegramError(ErrorKind.AUTH, "getMe: Unauthorized")
        return {"username": "my_bot"}

    async def get_webhook_info(self) -> dict[str, Any]:
        if self.webhook_error is not None:
            raise self.webhook_error
        return {"url": self.webhook}

    async def get_updates(self, *, offset: int | None = None) -> list[dict[str, Any]]:
        self.update_calls += 1
        if self.update_batches is not None:
            batch = self.update_batches.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return batch  # type: ignore[no-any-return]
        return self.updates

    async def send(self, text: str, *, parse_mode: str | None = "HTML") -> int:
        self.sent.append(text)
        return 1

    async def aclose(self) -> None:
        self.closed += 1


class Saver:
    """Fake ``save``: records the pair (as the Keychain items it would write), or
    raises ``fail``."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.saved: dict[str, str] = {}
        self.calls = 0

    def __call__(self, token: str, chat_id: str) -> None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        self.saved.update({"bot-token": token, "chat-id": chat_id})


def no_daemon() -> bool:
    return False


def scripted(answers: list[str], said: list[str]) -> SetupIO:
    replies = iter(answers)
    return SetupIO(
        ask_secret=lambda _prompt: TOKEN,
        ask=lambda _prompt: next(replies),
        say=said.append,
    )


async def test_happy_path(tmp_dir: Path) -> None:
    bot = FakeBot()
    saver = Saver()
    said: list[str] = []
    code = await run_setup(
        scripted(["y", "", ""], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "cfg" / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0
    assert saver.saved == {"bot-token": TOKEN, "chat-id": "555"} and saver.calls == 1
    assert (tmp_dir / "cfg" / "config.yaml").exists()
    assert bot.sent and "set up" in bot.sent[0]
    assert bot.closed == 2
    assert all(TOKEN not in line for line in said)
    assert RESTART_HINT not in said  # no daemon holds the lock


def test_the_default_writer_is_save_credentials() -> None:
    assert inspect.signature(run_setup).parameters["save"].default is save_credentials


async def test_setup_writes_marker_chat_token_then_clears_the_marker(tmp_dir: Path) -> None:
    """Same order as the web wizard (bot onboarding spec §2, save order)."""
    ops: list[tuple[str, str]] = []

    def save(token: str, chat_id: str) -> None:
        save_credentials(
            token,
            chat_id,
            put=lambda account, value: ops.append(("put", account)),
            delete=lambda account: ops.append(("delete", account)),
        )

    code = await run_setup(
        scripted(["y", "", ""], []),
        make_client=lambda token, chat: FakeBot(),
        config_path=tmp_dir / "config.yaml",
        save=save,
        daemon_running=no_daemon,
    )
    assert code == 0
    assert ops == [
        ("put", "save-pending"),
        ("put", "chat-id"),
        ("put", "bot-token"),
        ("delete", "save-pending"),
    ]


async def test_a_running_daemon_gets_the_restart_hint(tmp_dir: Path) -> None:
    """Unconditional: the daemon keeps its previous bot, or none, until a restart
    (first setup and replace alike: run_setup can't tell them apart)."""
    said: list[str] = []
    code = await run_setup(
        scripted(["y", "", ""], said),
        make_client=lambda token, chat: FakeBot(),
        config_path=tmp_dir / "config.yaml",
        save=Saver(),
        daemon_running=lambda: True,
    )
    assert code == 0
    assert RESTART_HINT in said
    assert (
        f"Saved; the running {BRAND} keeps its previous bot (or none) until you run "
        "`diffbell install`."
    ) == RESTART_HINT
    assert said.index(RESTART_HINT) > next(
        i for i, line in enumerate(said) if line.startswith("Saved the bot token")
    )


async def test_webhook_is_left_alone_and_manual_id_used(tmp_dir: Path) -> None:
    bot = FakeBot(webhook="https://example.com/hook")
    saver = Saver()
    said: list[str] = []
    code = await run_setup(
        scripted(["y", "777"], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0
    assert saver.saved["chat-id"] == "777"
    assert any("webhook" in line for line in said)


async def test_webhook_info_error_falls_back_to_manual(tmp_dir: Path) -> None:
    bot = FakeBot(webhook_error=TelegramError(ErrorKind.RETRYABLE, "getWebhookInfo: timeout"))
    saver = Saver()
    said: list[str] = []
    code = await run_setup(
        scripted(["y", "888"], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0 and saver.saved["chat-id"] == "888"
    assert bot.update_calls == 0
    assert any("getWebhookInfo: timeout" in x for x in said)


async def test_no_private_chat_falls_back_to_manual(tmp_dir: Path) -> None:
    bot = FakeBot(updates=[])
    saver = Saver()
    said: list[str] = []
    code = await run_setup(
        scripted(["y", "", "", "-100"], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0 and saver.saved["chat-id"] == "-100"
    assert bot.update_calls == 2
    assert any("No message found yet" in x and "@my_bot" in x for x in said)


async def test_retry_finds_the_chat_after_prompting_again(tmp_dir: Path) -> None:
    found = [{"update_id": 1, "message": {"chat": {"id": 9, "type": "private"}}}]
    bot = FakeBot(update_batches=[[], found])
    saver = Saver()
    code = await run_setup(
        scripted(["y", "", "", ""], []),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0 and saver.saved["chat-id"] == "9"
    assert bot.update_calls == 2


async def test_get_updates_error_falls_back_to_manual(tmp_dir: Path) -> None:
    bot = FakeBot(update_batches=[TelegramError(ErrorKind.RETRYABLE, "boom")])
    saver = Saver()
    said: list[str] = []
    code = await run_setup(
        scripted(["y", "", "42"], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0 and saver.saved["chat-id"] == "42"
    assert any("boom" in x for x in said)


async def test_manual_chat_id_strips_inner_whitespace(tmp_dir: Path) -> None:
    bot = FakeBot(webhook="https://example.com/hook")
    saver = Saver()
    code = await run_setup(
        scripted(["y", " -100 123 456 "], []),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0 and saver.saved["chat-id"] == "-100123456"


async def test_manual_chat_id_reprompts_then_succeeds(tmp_dir: Path) -> None:
    bot = FakeBot(webhook="https://example.com/hook")
    saver = Saver()
    said: list[str] = []
    code = await run_setup(
        scripted(["y", "abc", "@me", "321"], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 0 and saver.saved["chat-id"] == "321"
    assert sum("number" in x for x in said) == 2


async def test_non_numeric_manual_chat_id_saves_nothing(tmp_dir: Path) -> None:
    bot = FakeBot(webhook="https://example.com/hook")
    saver = Saver()
    code = await run_setup(
        scripted(["y", "abc", "def", "ghi"], []),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 1 and saver.calls == 0


async def test_declining_the_bot_saves_nothing(tmp_dir: Path) -> None:
    bot = FakeBot()
    saver = Saver()
    code = await run_setup(
        scripted(["n"], []),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 1 and saver.calls == 0


async def test_bad_token_saves_nothing(tmp_dir: Path) -> None:
    bot = FakeBot(bad_token=True)
    saver = Saver()
    said: list[str] = []
    code = await run_setup(
        scripted([], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=saver,
        daemon_running=no_daemon,
    )
    assert code == 1 and saver.calls == 0
    assert any("rejected" in line for line in said)
    assert all(TOKEN not in line for line in said)


async def test_keychain_failure_is_reported_without_the_token(tmp_dir: Path) -> None:
    bot = FakeBot()
    said: list[str] = []

    def never_asked() -> bool:
        raise AssertionError("no restart hint after a failed save")

    code = await run_setup(
        scripted(["y", "", ""], said),
        make_client=lambda token, chat: bot,
        config_path=tmp_dir / "config.yaml",
        save=Saver(SecretStoreError("keychain write failed for 'chat-id' (exit 1)")),
        daemon_running=never_asked,
    )
    assert code == 1
    assert any("Keychain" in line for line in said)
    assert all(TOKEN not in line for line in said)
    assert not bot.sent
