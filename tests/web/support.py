"""Shared helpers for the web tests: an in-process app over a temp home."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from diffbell.config import load_and_resolve
from diffbell.secrets import SecretStoreError, save_credentials
from diffbell.store import Store
from diffbell.web import auth
from diffbell.web.app import WebContext
from diffbell.web.preview import Previewer
from tests.fakes import FakeBot
from tests.helpers import sync_names

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "SECRET-MARKER-5f2c"
WEB_SECRET = bytes(range(100, 132))  # the server's per-start secret


@dataclass
class Env:
    home: Path
    proj: Path
    config: Path
    store: Store
    snapshot: dict[str, Any]
    sent: list[str] = field(default_factory=list)
    send_error: Exception | None = None
    now: float = 1_000_000.0
    # Settings → Telegram: the slot, the bot behind make_probe, and a dict for the
    # Keychain (its writes and deletes logged in order; `keychain_fail` names the
    # account whose write fails).
    configured: bool = True
    bot_username: str | None = "my_bot"
    bot: FakeBot = field(default_factory=FakeBot)
    setups: list[tuple[str, str, str | None]] = field(default_factory=list)
    keychain: dict[str, str] = field(default_factory=dict)
    keychain_log: list[str] = field(default_factory=list)
    keychain_fail: str | None = None
    # The daemon's cached setup reason (None: "both" while not set up, as the daemon
    # reads it); set by note_unfinished_save, cleared by complete_setup.
    setup_reason: str | None = None


def make_env(tmp_dir: Path) -> Env:
    home = tmp_dir / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    for rel in ("package.json", ".env.local", "node_modules/package.json"):
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(f"{SECRET}\n")
    config = tmp_dir / "cfg" / "config.yaml"
    config.parent.mkdir()
    config.write_text(f"# my config\nprojects:\n  - path: {proj}\n")
    snapshot: dict[str, Any] = {
        "pid": 4242,
        "running": True,
        "updated_at": 0.0,
        "connected": True,
        "watchman_version": "2026.10.05.00",
        "config_version": "v-applied",
        "config_rejected_version": None,
        "projects": [
            {
                "name": "proj",
                "path": str(proj),
                "status": "subscribed",
                "error": None,
                "note": None,
            },
            {"name": "gone", "path": "/nowhere", "status": "missing"},
        ],
        "counts": {"pending": 0, "sent": 0, "failed": 0},
        "web": {
            "enabled": True,
            "running": True,
            "port": PORT,
            "last_error": None,
            "secret": WEB_SECRET.hex(),
        },
    }
    store = Store(tmp_dir / "state.db")
    # As the daemon's sync leaves it: the configured project, plus the other names
    # these tests store events under (stable project ids spec §2).
    sync_names(store, "proj", "other", "removed", "gone")
    return Env(home, proj, config, store, snapshot)


def make_context(env: Env) -> WebContext:
    async def send_test() -> None:
        if env.send_error is not None:
            raise env.send_error
        env.sent.append("sample")

    async def complete_setup(token: str, chat_id: str, bot: str | None) -> None:
        env.setups.append((token, chat_id, bot))
        env.configured = True
        env.bot_username = bot
        env.setup_reason = None

    def put(account: str, value: str) -> None:
        env.keychain_log.append(f"put {account}")
        if account == env.keychain_fail:
            raise SecretStoreError(f"keychain write failed for {account!r} (exit 1)")
        env.keychain[account] = value

    def delete(account: str) -> None:
        env.keychain_log.append(f"delete {account}")
        env.keychain.pop(account, None)

    def save(token: str, chat_id: str) -> None:
        save_credentials(token, chat_id, put=put, delete=delete)

    def setup_reason() -> str | None:
        return None if env.configured else (env.setup_reason or "both")

    def note_unfinished_save() -> None:
        env.setup_reason = "unfinished save"

    return WebContext(
        store=env.store,
        config_path=env.config,
        port=PORT,
        secret=WEB_SECRET,
        snapshot=lambda: env.snapshot,
        current=lambda: load_and_resolve(env.config),
        send_test=send_test,
        previewer=Previewer(home=env.home),
        telegram_status=lambda: {"configured": env.configured, "bot": env.bot_username},
        configured=lambda: env.configured,
        complete_setup=complete_setup,
        make_probe=env.bot.client,
        save_credentials=save,
        setup_reason=setup_reason,
        note_unfinished_save=note_unfinished_save,
        clock=lambda: env.now,
    )


async def log_in(env: Env, client: httpx.AsyncClient) -> str:
    """Log the client in; return the CSRF token (also set as a default header)."""
    token = auth.issue_login(env.store, env.now, WEB_SECRET)
    response = await client.get(f"/login?t={token}")
    assert response.status_code == 303
    csrf = str((await client.get("/api/session")).json()["csrf"])
    client.headers[auth.CSRF_HEADER] = csrf
    return csrf
