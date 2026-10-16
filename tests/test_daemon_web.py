"""The daemon owns the web server's lifecycle; a web failure never stops watching."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from diffbell.config import Config
from diffbell.daemon import Daemon
from diffbell.secrets import save_credentials
from diffbell.sender_slot import SenderSlot
from diffbell.store import Store
from diffbell.telegram import TelegramClient
from diffbell.web import auth, config_edit
from diffbell.web import server as server_mod
from diffbell.web.server import WebServer
from tests.fakes import FakeSender, fake_slot
from tests.helpers import free_port, port_is_free
from tests.test_daemon import no_watchman, wait_until


def write_config(path: Path, proj: Path, *, enabled: bool, port: int) -> None:
    path.write_text(
        f"web:\n  enabled: {str(enabled).lower()}\n  port: {port}\nprojects:\n  - path: {proj}\n"
    )


class Env:
    def __init__(
        self,
        tmp_dir: Path,
        *,
        enabled: bool = True,
        sender: FakeSender | None = None,
        maintenance_interval: float = 0.02,
    ) -> None:
        self.proj = tmp_dir / "proj"
        self.proj.mkdir()
        self.config = tmp_dir / "cfg" / "config.yaml"
        self.config.parent.mkdir()
        self.port = free_port()
        write_config(self.config, self.proj, enabled=enabled, port=self.port)
        self.state = tmp_dir / "state"
        self.store = Store(self.state / "state.db")
        self.sender = sender or FakeSender()
        self.daemon = Daemon(
            self.config,
            self.store,
            fake_slot(self.sender),
            state_dir=self.state,
            wm_factory=no_watchman,
            recv_timeout=0.05,
            maintenance_interval=maintenance_interval,
            preview_home=tmp_dir,
        )
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.daemon.run(self.stop))

    async def close(self) -> None:
        self.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout=10)

    async def reload(self) -> None:
        """A self-watch reload, then the web sync it started in the background."""
        await self.daemon._reload()
        while pending := list(self.daemon._background):
            await asyncio.gather(*pending, return_exceptions=True)

    def web(self) -> dict[str, Any]:
        return dict(self.daemon.status_snapshot()["web"])

    def status_file(self) -> dict[str, Any]:
        """status.json, or {} before its first write."""
        try:
            return dict(json.loads((self.state / "status.json").read_text()))
        except (OSError, ValueError):
            return {}

    def alerts(self) -> list[str]:
        return [text for text, _ in self.sender.sent if "web UI" in text]


async def healthz(port: int) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def test_web_starts_when_enabled_and_stops_with_the_daemon(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    assert env.daemon._previewer._home == tmp_dir  # previews stay under preview_home
    env.start()
    await wait_until(lambda: env.web()["running"])
    assert await healthz(env.port)
    await wait_until(lambda: env.status_file().get("web", {}).get("running") is True)
    secret = bytes.fromhex(env.status_file()["web"]["secret"])
    assert len(secret) == 32
    assert (env.state / "status.json").stat().st_mode & 0o777 == 0o600
    nonce = "fresh-nonce-0123456789"
    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{env.port}/healthz?challenge={nonce}")
    assert auth.proof_ok(secret, nonce, response.json()["proof"])
    assert secret.hex() not in response.text
    await env.close()
    assert env.status_file()["web"]["secret"] is None
    assert port_is_free(env.port)
    assert env.status_file()["web"]["running"] is False


async def test_web_stays_off_when_disabled(tmp_dir: Path) -> None:
    env = Env(tmp_dir, enabled=False)
    env.start()
    await wait_until(lambda: (env.state / "status.json").exists())
    assert env.web() == {
        "enabled": False,
        "running": False,
        "port": env.port,
        "last_error": None,
        "secret": None,
    }
    assert port_is_free(env.port)
    await env.close()


async def test_port_in_use_alerts_and_watching_continues(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", env.port))
        busy.listen()
        env.start()
        await wait_until(lambda: bool(env.alerts()))
        assert env.alerts()[0].startswith(
            f"⚠️ Diffbell: web UI could not start on 127.0.0.1:{env.port}: "
        )
        web = env.web()
        assert web["running"] is False and "in use" in str(web["last_error"])
        assert env.task is not None and not env.task.done()  # the daemon keeps running
    await env.close()


async def test_crash_is_alerted_and_visible_in_the_snapshot(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(self: Any) -> None:
        await asyncio.sleep(0.1)
        raise RuntimeError("boom")

    monkeypatch.setattr(server_mod._Server, "main_loop", crash)
    env = Env(tmp_dir)
    env.start()
    await wait_until(lambda: any("web UI stopped: RuntimeError: boom" in a for a in env.alerts()))
    await wait_until(lambda: env.status_file().get("web", {}).get("last_error") is not None)
    snapshot = env.status_file()
    assert snapshot["web"]["running"] is False
    assert snapshot["web"]["last_error"] == "RuntimeError: boom"
    assert snapshot["running"] is True  # watching goes on
    assert env.task is not None and not env.task.done()
    await env.close()


async def test_reload_moves_the_server_to_a_new_port_and_disables_it(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    env.start()
    await wait_until(lambda: env.web()["running"])
    new_port = free_port()
    write_config(env.config, env.proj, enabled=True, port=new_port)
    await env.reload()
    assert env.web()["port"] == new_port and await healthz(new_port)
    assert port_is_free(env.port)
    write_config(env.config, env.proj, enabled=False, port=new_port)
    await env.reload()
    assert env.web()["running"] is False and port_is_free(new_port)
    await env.close()


async def test_restart_request_restarts_the_server(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts: list[int] = []
    # The snapshot the moment each server is up, before the daemon resumes: under
    # load a status read can land there, so it must already show the new secret.
    at_start: list[dict[str, Any]] = []
    real_start = WebServer.start

    async def counting_start(self: WebServer, app: Any, port: int) -> asyncio.Task[None]:
        starts.append(port)
        task = await real_start(self, app, port)
        at_start.append(env.web())
        return task

    monkeypatch.setattr(WebServer, "start", counting_start)
    env = Env(tmp_dir)
    env.start()
    await wait_until(lambda: len(at_start) == 1)
    assert len(starts) == 1
    first = env.web()["secret"]
    assert first is not None and at_start[0]["secret"] == first
    env.store.add_web_request("restart", env.daemon._clock())
    await wait_until(lambda: len(at_start) == 2)
    assert len(starts) == 2 and env.web()["running"]
    assert await healthz(env.port)
    second = env.web()["secret"]
    assert second not in (None, first)  # a fresh secret per start
    assert at_start[1]["running"] and at_start[1]["secret"] == second
    await env.close()


async def test_restart_request_recovers_a_crashed_server(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crashes: list[int] = []
    real_main_loop = server_mod._Server.main_loop

    async def crash_once(self: Any) -> None:
        if not crashes:
            crashes.append(1)
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")
        await real_main_loop(self)

    monkeypatch.setattr(server_mod._Server, "main_loop", crash_once)
    env = Env(tmp_dir)
    env.start()
    await wait_until(lambda: env.web()["last_error"] is not None)
    env.store.add_web_request("restart", env.daemon._clock())
    await wait_until(lambda: env.web()["running"] and env.web()["last_error"] is None)
    assert await healthz(env.port)
    await env.close()


async def test_old_restart_request_is_ignored(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Env(tmp_dir, enabled=False)
    env.store.add_web_request("restart", env.daemon._clock() - 120)  # written while down
    env.start()
    await wait_until(
        lambda: env.store._db.execute("SELECT COUNT(*) FROM web_requests").fetchone()[0] == 0
    )
    assert env.web()["running"] is False
    await env.close()


async def test_web_task_is_not_supervised(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead web task must never take the daemon down (W8)."""

    async def crash(self: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(server_mod._Server, "main_loop", crash)
    env = Env(tmp_dir)
    env.start()
    await wait_until(lambda: env.web()["last_error"] is not None)
    await asyncio.sleep(0.2)
    assert env.task is not None and not env.task.done()
    await env.close()  # a clean stop: no RuntimeError("background task … stopped")


async def test_shutdown_during_a_restart_never_orphans_the_server(tmp_dir: Path) -> None:
    """SIGTERM while maintenance restarts the server cancels the caller of stop():
    the server must still go away, and its end is a requested stop, not a crash."""
    env = Env(tmp_dir)
    env.start()
    await wait_until(lambda: env.web()["running"])
    # A keep-alive connection keeps uvicorn's graceful shutdown busy.
    reader, writer = await asyncio.open_connection("127.0.0.1", env.port)
    writer.write(f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{env.port}\r\n\r\n".encode())
    await writer.drain()
    await reader.read(200)
    try:
        env.store.add_web_request("restart", env.daemon._clock())
        await wait_until(lambda: env.daemon._web.stopping, timeout=5)
        await env.close()  # stop.set() while the restart's stop() is in progress
        # The server closed the keep-alive connection first, so the port sits in
        # TIME_WAIT: check that nothing listens, the way the next start binds it.
        server_mod._bind(env.port).close()
        await asyncio.wait_for(reader.read(), 1)  # EOF: the connection was not leaked
        await asyncio.sleep(0.3)  # any late done-callback would alert by now
        assert env.alerts() == []
        assert env.daemon._web._task is None
        assert env.status_file()["web"]["running"] is False
    finally:
        writer.close()


async def test_a_second_cancel_during_the_web_stop_still_stops_watchman(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ignores_exit(self: Any, sockets: Any = None) -> None:
        self.started = True
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod._Server, "serve", ignores_exit)
    env = Env(tmp_dir)
    env.start()
    # Written once start() returned: run() is past startup, in its main wait.
    await wait_until(lambda: env.status_file().get("web", {}).get("running") is True)
    wm = env.daemon._wm
    assert wm is not None
    stopped: list[bool] = []
    real_stop = wm.stop

    def recording_stop(timeout: float = 5.0) -> None:
        stopped.append(True)
        real_stop(timeout)

    monkeypatch.setattr(wm, "stop", recording_stop)
    assert env.task is not None
    env.task.cancel()  # first cancel: run() enters its cleanup, web.stop() waits
    await wait_until(lambda: env.daemon._web.stopping)
    env.task.cancel()  # a second cancel lands inside `await self._web.stop()`
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(env.task, 5)
    assert stopped == [True]
    assert port_is_free(env.port)
    assert env.status_file()["running"] is False


class SlowSender(FakeSender):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(
        self, text: str, *, parse_mode: str | None = "HTML", silent: bool = False
    ) -> int:
        self.entered.set()
        await self.release.wait()
        return await super().send(text, parse_mode=parse_mode, silent=silent)


async def test_the_start_failure_alert_is_sent_outside_the_web_lock(tmp_dir: Path) -> None:
    sender = SlowSender()
    env = Env(tmp_dir, sender=sender)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", env.port))
        busy.listen()
        env.start()
        await asyncio.wait_for(sender.entered.wait(), 5)
        # Telegram is still sending: the pump/maintenance must not wait on it.
        assert not env.daemon._web_lock.locked()
        await asyncio.wait_for(env.daemon._sync_web(), 1)
        sender.release.set()
        await wait_until(lambda: bool(env.alerts()))
    await env.close()


async def test_status_json_drops_the_secret_as_soon_as_the_server_goes(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No maintenance tick in between (interval 60 s): status.json is rewritten when
    the server is stopped, disabled or crashes."""
    env = Env(tmp_dir, maintenance_interval=60)
    env.start()
    await wait_until(lambda: env.status_file().get("web", {}).get("running") is True)
    # Disabled by a reload.
    write_config(env.config, env.proj, enabled=False, port=env.port)
    await env.reload()
    web = env.status_file()["web"]
    assert web["running"] is False and web["secret"] is None
    # Re-enabled, then stopped directly (the done-callback path).
    write_config(env.config, env.proj, enabled=True, port=env.port)
    await env.reload()
    assert env.status_file()["web"]["running"] is True
    await env.daemon._web.stop()
    web = env.status_file()["web"]
    assert web["running"] is False and web["secret"] is None
    await env.close()


async def test_status_json_is_rewritten_when_the_server_crashes(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(self: Any) -> None:
        await asyncio.sleep(0.3)
        raise RuntimeError("boom")

    monkeypatch.setattr(server_mod._Server, "main_loop", crash)
    env = Env(tmp_dir, maintenance_interval=60)
    env.start()
    await wait_until(lambda: env.status_file().get("web", {}).get("running") is True)
    await wait_until(lambda: env.status_file()["web"]["running"] is False, timeout=2)
    web = env.status_file()["web"]
    assert web["secret"] is None and web["last_error"] == "RuntimeError: boom"
    await env.close()


async def test_a_cancel_while_the_web_server_starts_still_stops_watchman(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never_starts(self: Any, sockets: Any = None) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(server_mod._Server, "serve", never_starts)
    env = Env(tmp_dir)
    env.daemon._web = WebServer(stop_timeout=0.1)
    env.start()
    await wait_until(lambda: env.daemon._web._task is not None)  # inside start()
    wm = env.daemon._wm
    assert wm is not None
    stopped: list[bool] = []
    real_wm_stop = wm.stop

    def recording_stop(timeout: float = 5.0) -> None:
        stopped.append(True)
        real_wm_stop(timeout)

    monkeypatch.setattr(wm, "stop", recording_stop)
    assert env.task is not None
    env.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(env.task, 5)
    assert stopped == [True]
    assert port_is_free(env.port)
    assert env.status_file()["running"] is False


async def test_a_reload_never_waits_for_the_web_server(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pump runs reloads: a slow web start/stop must not hold up file watching.
    The applied config_version is recorded before the web sync even starts."""
    entered, gate = asyncio.Event(), asyncio.Event()
    real_start = WebServer.start

    async def gated_start(self: WebServer, app: Any, port: int) -> asyncio.Task[None]:
        entered.set()
        await gate.wait()
        return await real_start(self, app, port)

    monkeypatch.setattr(WebServer, "start", gated_start)
    env = Env(tmp_dir, enabled=False)
    env.start()
    await wait_until(lambda: env.status_file().get("running") is True)
    write_config(env.config, env.proj, enabled=True, port=env.port)
    await asyncio.wait_for(env.daemon._reload(), 1)
    assert env.daemon.status_snapshot()["config_version"] == config_edit.config_version(env.config)
    await asyncio.wait_for(entered.wait(), 1)
    assert env.web()["running"] is False  # still starting, in the background
    gate.set()
    await wait_until(lambda: env.web()["running"])
    assert await healthz(env.port)
    await env.close()
    assert port_is_free(env.port)


async def test_shutdown_during_a_background_web_start_leaves_no_server(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled between the bind and uvicorn's `started`: the port is released."""
    entered = asyncio.Event()
    real_startup = server_mod._Server.startup

    async def stuck_startup(self: Any, sockets: Any = None) -> None:
        entered.set()
        await asyncio.Event().wait()  # never finishes starting
        await real_startup(self, sockets)

    monkeypatch.setattr(server_mod._Server, "startup", stuck_startup)
    env = Env(tmp_dir, enabled=False)
    env.start()
    await wait_until(lambda: env.status_file().get("running") is True)
    write_config(env.config, env.proj, enabled=True, port=env.port)
    await env.daemon._reload()
    await asyncio.wait_for(entered.wait(), 1)
    assert not port_is_free(env.port)  # bound, not started yet
    await env.close()
    assert port_is_free(env.port) and env.daemon._web.running is False
    assert env.status_file()["web"]["running"] is False


async def test_disabling_while_a_start_is_in_flight_leaves_the_server_off(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reload #2 runs while reload #1's start is still running: the server must end
    up matching the latest config (off), not the one #1 read."""
    entered, gate = asyncio.Event(), asyncio.Event()
    real_start = WebServer.start

    async def gated_start(self: WebServer, app: Any, port: int) -> asyncio.Task[None]:
        entered.set()
        await gate.wait()
        return await real_start(self, app, port)

    monkeypatch.setattr(WebServer, "start", gated_start)
    env = Env(tmp_dir, enabled=False)
    env.start()
    await wait_until(lambda: env.status_file().get("running") is True)
    write_config(env.config, env.proj, enabled=True, port=env.port)
    await env.daemon._reload()
    await asyncio.wait_for(entered.wait(), 1)
    write_config(env.config, env.proj, enabled=False, port=env.port)
    await env.daemon._reload()
    gate.set()
    while pending := list(env.daemon._background):
        await asyncio.gather(*pending, return_exceptions=True)
    assert env.daemon._web.running is False and port_is_free(env.port)
    assert env.web()["running"] is False
    await env.close()


async def test_a_port_flipped_back_while_the_server_stops_stays_on_the_first_port(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A -> B is still inside stop() when a reload sets A again: it ends on A."""
    entered, gate = asyncio.Event(), asyncio.Event()
    real_stop = WebServer.stop

    async def gated_stop(self: WebServer) -> None:
        entered.set()
        await gate.wait()
        await real_stop(self)

    env = Env(tmp_dir)
    env.start()
    await wait_until(lambda: env.web()["running"])
    monkeypatch.setattr(WebServer, "stop", gated_stop)
    other = free_port()
    write_config(env.config, env.proj, enabled=True, port=other)
    await env.daemon._reload()
    await asyncio.wait_for(entered.wait(), 1)
    write_config(env.config, env.proj, enabled=True, port=env.port)
    await env.daemon._reload()
    gate.set()
    while pending := list(env.daemon._background):
        await asyncio.gather(*pending, return_exceptions=True)
    assert env.web()["running"] is True and env.web()["port"] == env.port
    assert await healthz(env.port) and port_is_free(other)
    await env.close()


async def test_turning_the_web_ui_off_forgets_its_problem_without_a_message(
    tmp_dir: Path,
) -> None:
    env = Env(tmp_dir, enabled=False)
    health = env.daemon._health
    health.open("web", "serious", f"web UI could not start on 127.0.0.1:{env.port}: in use")
    await health.tick(env.daemon._clock())
    assert len(env.alerts()) == 1
    await env.daemon._sync_web()
    assert env.store.all_problems() == []
    await health.tick(env.daemon._clock() + 3600)
    assert len(env.alerts()) == 1  # forgotten: no ✅ for a server the user turned off


async def test_a_web_start_resolves_its_problem(tmp_dir: Path) -> None:
    env = Env(tmp_dir)
    # Not running: hand the daemon the config its start would have loaded.
    env.daemon._config = Config.model_validate({"web": {"enabled": True, "port": env.port}})
    health = env.daemon._health
    health.open("web", "serious", f"web UI could not start on 127.0.0.1:{env.port}: in use")
    await health.tick(env.daemon._clock())
    try:
        await env.daemon._sync_web()
        assert env.daemon._web.running
        assert health.problems() == []
        await health.tick(env.daemon._clock())
    finally:
        await env.daemon._web.stop()
    alerts = env.alerts()
    assert len(alerts) == 2
    assert alerts[1].startswith("✅ Diffbell: the web UI is running again (down ")


async def test_the_web_context_wires_telegram_setup(tmp_dir: Path) -> None:
    """Settings → Telegram gets the slot's state, the daemon's complete_setup, the
    one Keychain writer and a separate client per draft (bot onboarding spec §2)."""
    proj = tmp_dir / "proj"
    proj.mkdir()
    config = tmp_dir / "config.yaml"
    write_config(config, proj, enabled=False, port=free_port())
    store = Store(tmp_dir / "state" / "state.db")
    slot = SenderSlot(None, make_client=TelegramClient)
    daemon = Daemon(config, store, slot, state_dir=tmp_dir / "state", wm_factory=no_watchman)
    ctx = daemon._web_context(free_port(), b"s" * 32)
    assert ctx.configured() is False
    assert ctx.telegram_status() == {"configured": False, "bot": None}
    assert ctx.complete_setup == daemon.complete_setup
    assert ctx.setup_reason() == "both"
    assert ctx.note_unfinished_save == daemon.note_unfinished_save
    assert ctx.save_credentials is save_credentials
    probe = ctx.make_probe("1:" + "A" * 35, None)
    assert isinstance(probe, TelegramClient)
    await probe.aclose()
