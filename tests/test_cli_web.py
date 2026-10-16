"""`diffbell web` with fakes: no real daemon, browser or HTTP server."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from diffbell import cli
from diffbell.cli import main
from diffbell.config import DEFAULT_CONFIG_TEXT, load_config
from diffbell.lock import singleton_lock
from diffbell.store import Store
from diffbell.web import auth, config_edit
from tests.test_store_migration import make_v5


@pytest.fixture(autouse=True)
def isolated(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIFFBELL_STATE_DIR", str(tmp_dir / "state"))
    monkeypatch.setenv("DIFFBELL_CONFIG", str(tmp_dir / "cfg" / "config.yaml"))
    monkeypatch.setenv("DIFFBELL_LOG_DIR", str(tmp_dir / "logs"))


class Browser:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, url: str) -> bool:
        self.opened.append(url)
        return True


OURS = bytes(range(32))  # the secret our daemon published in status.json


class Health:
    """Fake /healthz. Once `up`, it answers a challenge with an HMAC under `secret`
    (None: a server that knows no secret). `replay` answers every challenge with
    one fixed proof, as a squatter replaying an earlier answer would."""

    def __init__(
        self,
        up: bool = False,
        up_after: int | None = None,
        secret: bytes | None = OURS,
        replay: str | None = None,
    ) -> None:
        self.up = up
        self.up_after = up_after
        self.secret = secret
        self.replay = replay
        self.probes = 0
        self.challenges: list[str] = []

    def __call__(
        self, base: str, timeout: float, challenge: str | None = None
    ) -> dict[str, object] | None:
        self.probes += 1
        if self.up_after is not None and self.probes > self.up_after:
            self.up = True
        if not self.up:
            return None
        if challenge is None:
            return {"ok": True}
        self.challenges.append(challenge)
        if self.replay is not None:
            return {"ok": True, "proof": self.replay}
        if self.secret is None:
            return {"ok": True}
        return {"ok": True, "proof": auth.prove(self.secret, challenge)}


def publish(tmp_dir: Path, secret: bytes) -> None:
    state = tmp_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "status.json").write_text(json.dumps({"web": {"secret": secret.hex()}}))


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> Browser:
    fake = Browser()
    monkeypatch.setattr(cli, "_open_url", fake)
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(cli, "_web_sleep", sleep)
    monkeypatch.setattr(cli, "_web_clock", lambda: now[0])
    return now


def write_config(tmp_dir: Path, *, enabled: bool, port: int = 8799) -> Path:
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text(f"# keep me\nweb:\n  enabled: {str(enabled).lower()}\n  port: {port}\n")
    return cfg


@pytest.fixture
def daemon_lock(tmp_dir: Path) -> Iterator[None]:
    """A running daemon: it holds the lock and published its web secret."""
    publish(tmp_dir, OURS)
    with singleton_lock(tmp_dir / "state" / "daemon.lock"):
        yield


def login_rows(tmp_dir: Path) -> int:
    store = Store(tmp_dir / "state" / "state.db")
    try:
        return int(store._db.execute("SELECT COUNT(*) FROM web_logins").fetchone()[0])
    finally:
        store.close()


def restart_rows(tmp_dir: Path) -> int:
    store = Store(tmp_dir / "state" / "state.db")
    try:
        return int(store._db.execute("SELECT COUNT(*) FROM web_requests").fetchone()[0])
    finally:
        store.close()


def test_web_without_a_daemon_exits_2_and_leaves_the_config_alone(
    tmp_dir: Path, browser: Browser, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_dir, enabled=False)
    before = cfg.read_bytes()
    assert main(["web"]) == 2
    assert "diffbell install" in capsys.readouterr().out
    assert cfg.read_bytes() == before and browser.opened == []


def test_web_against_a_daemon_that_predates_the_web_ui_exits_2(
    tmp_dir: Path,
    browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live snapshot without a `web` block: that daemon would never start the
    server, so don't enable it in the config and wait 15 s for nothing."""
    cfg = write_config(tmp_dir, enabled=False)
    before = cfg.read_bytes()
    state = tmp_dir / "state"
    state.mkdir()
    snapshot = {"running": True, "pid": os.getpid(), "updated_at": time.time(), "projects": []}
    (state / "status.json").write_text(json.dumps(snapshot))
    health = Health(up=True)
    monkeypatch.setattr(cli, "_healthz", health)
    with singleton_lock(state / "daemon.lock"):
        assert main(["web"]) == 2
    out = capsys.readouterr().out
    assert "predates the web UI" in out and "diffbell install" in out
    assert cfg.read_bytes() == before and browser.opened == [] and health.probes == 0


@pytest.mark.usefixtures("daemon_lock")
def test_web_enables_the_ui_waits_and_opens_a_login_link(
    tmp_dir: Path,
    browser: Browser,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = write_config(tmp_dir, enabled=False)
    monkeypatch.setattr(cli, "_healthz", Health(up_after=3))
    assert main(["web"]) == 0
    assert load_config(cfg).web.enabled is True
    assert cfg.read_text().startswith("# keep me\n")  # comments preserved
    (url,) = browser.opened
    assert url.startswith("http://127.0.0.1:8799/login?t=")
    token = url.split("t=", 1)[1]
    store = Store(tmp_dir / "state" / "state.db")
    try:
        # Bound to the secret the CLI just verified: only that server redeems it.
        (bound,) = store._db.execute("SELECT secret_hash FROM web_logins").fetchone()
        assert bound == hashlib.sha256(OURS).hexdigest()
        assert auth.redeem_login(store, token, now=time.time(), secret=OURS) is not None
    finally:
        store.close()
    out = capsys.readouterr().out
    assert token not in out and "http://127.0.0.1:8799/" in out
    assert restart_rows(tmp_dir) == 0  # enabling is enough: the daemon reloads


@pytest.mark.usefixtures("daemon_lock")
def test_web_already_up_just_opens(
    tmp_dir: Path, browser: Browser, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up=True))
    assert main(["web"]) == 0
    assert len(browser.opened) == 1
    assert restart_rows(tmp_dir) == 0


@pytest.mark.usefixtures("daemon_lock")
def test_web_enabled_but_unresponsive_requests_a_restart(
    tmp_dir: Path, browser: Browser, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up_after=2))
    assert main(["web"]) == 0
    assert restart_rows(tmp_dir) == 1
    assert len(browser.opened) == 1


@pytest.mark.usefixtures("daemon_lock")
def test_web_times_out_after_15_seconds(
    tmp_dir: Path,
    browser: Browser,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health())
    assert main(["web"]) == 1
    assert clock[0] >= 15.0
    out = capsys.readouterr().out
    assert "did not answer" in out and "diffbell.log" in out
    assert browser.opened == []


@pytest.mark.parametrize(
    "squatter",
    [
        Health(up=True, secret=None),  # answers healthz, knows no secret
        Health(up=True, secret=bytes(32)),  # a different secret
        # Replays a proof that was valid for another nonce.
        Health(up=True, replay=auth.prove(OURS, "an-earlier-nonce-0123")),
    ],
)
@pytest.mark.usefixtures("daemon_lock")
def test_web_refuses_a_port_served_by_another_process(
    tmp_dir: Path,
    browser: Browser,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    squatter: Health,
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", squatter)
    assert main(["web"]) == 1
    assert (
        "Port 8799 is served by another process; refusing to send a login link."
        in capsys.readouterr().out
    )
    assert browser.opened == []
    assert login_rows(tmp_dir) == 0  # no token was ever written


@pytest.mark.usefixtures("daemon_lock")
def test_web_uses_a_fresh_nonce_per_probe(
    tmp_dir: Path, browser: Browser, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_dir, enabled=True)
    health = Health(up=True, secret=bytes(32))
    monkeypatch.setattr(cli, "_healthz", health)
    assert main(["web"]) == 1
    assert len(health.challenges) > 1
    assert len(set(health.challenges)) == len(health.challenges)
    assert all(auth.valid_nonce(c) for c in health.challenges)


@pytest.mark.usefixtures("daemon_lock")
def test_web_waits_for_the_secret_to_be_published(
    tmp_dir: Path, browser: Browser, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restarted server answers before status.json holds its new secret: wait."""
    write_config(tmp_dir, enabled=True)
    new = bytes(range(1, 33))
    monkeypatch.setattr(cli, "_healthz", Health(up=True, secret=new))

    def publish_later(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= 1.0:
            publish(tmp_dir, new)

    monkeypatch.setattr(cli, "_web_sleep", publish_later)
    assert main(["web"]) == 0
    assert len(browser.opened) == 1 and login_rows(tmp_dir) == 1


def test_web_disable_turns_it_off(tmp_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_config(tmp_dir, enabled=True)
    assert main(["web", "--disable"]) == 0
    assert load_config(cfg).web.enabled is False
    assert main(["web", "--disable"]) == 0
    assert "already disabled" in capsys.readouterr().out


def test_web_disable_on_a_fresh_config_states_the_key(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_dir / "cfg" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(DEFAULT_CONFIG_TEXT)
    assert main(["web", "--disable"]) == 0
    assert "already disabled" in capsys.readouterr().out
    assert config_edit.web_enabled_set(cfg) is True  # install won't turn it on now
    assert load_config(cfg).web.enabled is False


def test_web_logout_deletes_every_session(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["web", "--logout"]) == 0  # no database yet: nothing to do
    store = Store(tmp_dir / "state" / "state.db")
    store.add_session("a", "c", now=0.0, expires_at=1e12)
    store.add_session("b", "c", now=0.0, expires_at=1e12)
    # Pending login links die too: a link minted just before --logout must not
    # create a fresh session afterwards.
    auth.issue_login(store, now=time.time(), secret=OURS)
    store.close()
    assert main(["web", "--logout"]) == 0
    assert "Logged out 2 web session(s)." in capsys.readouterr().out
    store = Store(tmp_dir / "state" / "state.db")
    assert store.get_session("a", now=1.0) is None
    store.close()
    assert login_rows(tmp_dir) == 0


def test_web_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        main(["web", "--disable", "--logout"])


def test_web_disable_reports_an_unreadable_config(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_dir, enabled=True)
    cfg.chmod(0)
    try:
        assert main(["web", "--disable"]) == 2
    finally:
        cfg.chmod(0o644)
    assert "Permission denied" in capsys.readouterr().out
    assert load_config(cfg).web.enabled is True


# -- opening the browser: the token never reaches argv --------------------------------


class Runner:
    """Fake subprocess.run: records argv and stdin; `fail` is a return code or an
    exception to raise."""

    def __init__(self, fail: int | BaseException | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        if isinstance(self.fail, BaseException):
            raise self.fail
        return subprocess.CompletedProcess(argv, self.fail or 0, "", "")


URL = "http://127.0.0.1:8799/login?t=Ab_9-zZ"


def test_open_url_passes_the_url_on_stdin_never_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Runner()
    monkeypatch.setattr(subprocess, "run", runner)
    assert cli._open_url(URL) is True
    ((argv, kwargs),) = runner.calls
    assert argv == ["/usr/bin/osascript", "-"]
    assert kwargs["input"] == f'open location "{URL}"'
    assert kwargs["timeout"] == 10 and kwargs["check"] is False
    assert kwargs["text"] is True and kwargs["capture_output"] is True


def test_open_url_accepts_the_telegram_target(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Runner()
    monkeypatch.setattr(subprocess, "run", runner)
    assert cli._open_url(f"{URL}&next=telegram") is True
    ((argv, kwargs),) = runner.calls
    assert argv == ["/usr/bin/osascript", "-"]
    assert kwargs["input"] == f'open location "{URL}&next=telegram"'


@pytest.mark.parametrize(
    "url",
    [
        'http://127.0.0.1:8799/login?t=a"b',
        "http://127.0.0.1:8799/login?t=a\\b",
        'http://127.0.0.1:8799/login?t=x" & (do shell script "id") & "',
        "http://127.0.0.1:8799/login?t=x\n",
        "http://127.0.0.1:8799/login?t=",
        "https://127.0.0.1:8799/login?t=abc",
        "http://evil.example:8799/login?t=abc",
        "http://127.0.0.1:8799/api?t=abc",
        "http://127.0.0.1:\uff18\uff17\uff19\uff19/login?t=abc",  # fullwidth digits
        "http://127.0.0.1:\u0668\u0667\u0669\u0669/login?t=abc",  # Arabic-Indic digits
        "http://127.0.0.1:8799/login?t=abc&next=other",
        "http://127.0.0.1:8799/login?t=abc&next=telegram&x=1",
        "http://127.0.0.1:8799/login?t=abc&next=",
        "http://127.0.0.1:8799/login?t=abc#section",
        "http://127.0.0.1:8799/login?t=abc&next=telegram#x",
        "http://127.0.0.1:8799/login?t=abc&next=telegram\n",
    ],
)
def test_open_url_refuses_any_other_shape(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    runner = Runner()
    monkeypatch.setattr(subprocess, "run", runner)
    assert cli._open_url(url) is False
    assert runner.calls == []


@pytest.mark.usefixtures("daemon_lock")
def test_web_opens_the_login_link_without_putting_the_token_in_argv(
    tmp_dir: Path,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up=True))
    runner = Runner()
    monkeypatch.setattr(subprocess, "run", runner)
    assert main(["web"]) == 0
    ((argv, kwargs),) = runner.calls
    assert argv == ["/usr/bin/osascript", "-"]
    assert not any("t=" in arg for arg in argv)
    script = kwargs["input"]
    assert script.startswith('open location "http://127.0.0.1:8799/login?t=')
    token = script.split("t=", 1)[1].rstrip('"')
    assert token and token not in capsys.readouterr().out
    assert login_rows(tmp_dir) == 1  # the browser has it: the token stays to be redeemed


@pytest.mark.parametrize(
    "fail",
    [1, subprocess.TimeoutExpired(["/usr/bin/osascript", "-"], 10), OSError("no osascript")],
)
@pytest.mark.usefixtures("daemon_lock")
def test_web_reports_a_browser_that_cannot_be_opened(
    tmp_dir: Path,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail: int | BaseException,
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up=True))
    runner = Runner(fail)
    monkeypatch.setattr(subprocess, "run", runner)
    assert main(["web"]) == 1
    out = capsys.readouterr().out
    assert "Could not open the browser." in out
    assert "t=" not in out and "/login" not in out
    assert login_rows(tmp_dir) == 0  # the unused token was deleted again
    # Never a fallback that would put the URL in argv (`open URL`).
    assert [argv for argv, _ in runner.calls] == [["/usr/bin/osascript", "-"]]


# -- _open_page: shared by `web` and `install` ----------------------------------------


@pytest.mark.parametrize(("next_", "suffix"), [("telegram", "&next=telegram"), (None, "")])
@pytest.mark.usefixtures("daemon_lock")
def test_open_page_logs_in_to_the_requested_target(
    tmp_dir: Path,
    browser: Browser,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    next_: str | None,
    suffix: str,
) -> None:
    cfg = write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up_after=2))
    assert cli._open_page(cfg, next_=next_) is None
    (url,) = browser.opened
    shape = rf"http://127\.0\.0\.1:8799/login\?t=[A-Za-z0-9_-]+{re.escape(suffix)}"
    assert re.fullmatch(shape, url)
    assert cli._LOGIN_URL.fullmatch(url) is not None
    token = url.split("t=", 1)[1].removesuffix(suffix)
    store = Store(tmp_dir / "state" / "state.db")
    try:
        assert auth.redeem_login(store, token, now=time.time(), secret=OURS) is not None
    finally:
        store.close()


@pytest.mark.usefixtures("daemon_lock")
def test_open_page_refuses_a_squatter_and_mints_nothing(
    tmp_dir: Path, browser: Browser, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up=True, secret=bytes(32)))
    assert cli._open_page(cfg, next_="telegram") == (
        "Port 8799 is served by another process; refusing to send a login link."
    )
    assert browser.opened == [] and login_rows(tmp_dir) == 0


@pytest.mark.usefixtures("daemon_lock")
def test_open_page_times_out_without_a_server(
    tmp_dir: Path, browser: Browser, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health())
    error = cli._open_page(cfg, next_="telegram")
    assert error is not None and "did not answer" in error and "diffbell.log" in error
    assert browser.opened == [] and clock[0] >= cli.WEB_WAIT


@pytest.mark.usefixtures("daemon_lock")
def test_open_page_deletes_the_token_when_the_browser_fails(
    tmp_dir: Path, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up=True))
    tried: list[str] = []

    def fails(url: str) -> bool:
        tried.append(url)
        return False

    monkeypatch.setattr(cli, "_open_url", fails)
    error = cli._open_page(cfg, next_="telegram")
    assert error == "Could not open the browser."
    assert len(tried) == 1 and tried[0].endswith("&next=telegram")
    assert login_rows(tmp_dir) == 0


@pytest.mark.usefixtures("daemon_lock")
def test_web_reports_a_database_error_while_minting_the_link(
    tmp_dir: Path,
    browser: Browser,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up=True))

    def locked(*args: Any) -> str:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(auth, "issue_login", locked)
    assert main(["web"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (
        "Could not create a login link (OperationalError)."
    )
    assert browser.opened == []


# -- an unreadable daemon.lock --------------------------------------------------------


def test_web_with_an_unreadable_lock_exits_2_cleanly(
    tmp_dir: Path, browser: Browser, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_dir, enabled=False)
    lock = tmp_dir / "state" / "daemon.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("123")
    lock.chmod(0)
    try:
        assert main(["web"]) == 2
    finally:
        lock.chmod(0o600)
    out = capsys.readouterr().out
    assert "daemon.lock" in out and "Permission denied" in out
    assert load_config(cfg).web.enabled is False and browser.opened == []


# -- the /healthz probe against real (fake) servers on ephemeral ports ----------------


Handler = Callable[[BaseHTTPRequestHandler, str | None], None]


@pytest.fixture
def http_server() -> Iterator[Callable[[Handler], int]]:
    """Start fake HTTP servers on 127.0.0.1:0; returns each one's port."""
    servers: list[ThreadingHTTPServer] = []

    def start(handle: Handler) -> int:
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                query = parse_qs(urlparse(self.path).query)
                handle(self, query.get("challenge", [None])[0])

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return int(server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def reply(h: BaseHTTPRequestHandler, code: int, body: bytes, *headers: tuple[str, str]) -> None:
    h.send_response(code)
    for name, value in headers:
        h.send_header(name, value)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def proving(h: BaseHTTPRequestHandler, challenge: str | None) -> None:
    body = {"ok": True} if challenge is None else {"ok": True, "proof": auth.prove(OURS, challenge)}
    reply(h, 200, json.dumps(body).encode(), ("Content-Type", "application/json"))


def raw_server(serve: Callable[[socket.socket], None]) -> int:
    """A raw TCP server: `serve` gets each connection after the request head."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def handle(conn: socket.socket) -> None:
        with conn:
            head = b""
            while b"\r\n\r\n" not in head:
                data = conn.recv(4096)
                if not data:
                    return
                head += data
            with contextlib.suppress(OSError):
                serve(conn)

    def loop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return int(listener.getsockname()[1])


def test_probe_never_goes_through_a_proxy(
    http_server: Callable[[Handler], int], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def proxy(h: BaseHTTPRequestHandler, challenge: str | None) -> None:
        seen.append(h.path)
        reply(h, 200, b'{"ok": true}', ("Content-Type", "application/json"))

    proxy_url = f"http://127.0.0.1:{http_server(proxy)}"
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    port = http_server(proving)
    nonce = "a-fresh-nonce-0123456789"
    body = cli._healthz(f"http://127.0.0.1:{port}", 2.0, challenge=nonce)
    assert body is not None and auth.proof_ok(OURS, nonce, body.get("proof"))
    assert seen == []


def test_probe_gives_up_on_a_slow_loris_body() -> None:
    def trickle(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n")
        conn.sendall(b"Content-Length: 100000\r\n\r\n")
        for _ in range(200):  # ~10 s of one byte every 50 ms, never a read timeout
            conn.sendall(b" ")
            time.sleep(0.05)

    port = raw_server(trickle)
    started = time.monotonic()
    body = cli._healthz(f"http://127.0.0.1:{port}", 0.5, challenge="n" * 20)
    assert time.monotonic() - started < 2.0
    assert body == {}  # it answered HTTP, without a proof: foreign


def test_probe_gives_up_on_slow_loris_headers() -> None:
    def trickle(conn: socket.socket) -> None:
        for byte in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 200:
            conn.sendall(bytes([byte]))
            time.sleep(0.05)

    port = raw_server(trickle)
    started = time.monotonic()
    body = cli._healthz(f"http://127.0.0.1:{port}", 0.5, challenge="n" * 20)
    assert time.monotonic() - started < 2.0
    assert body == {}  # bytes arrived: something answered, so it is foreign


def test_probe_counts_a_garbage_reply_as_an_answer() -> None:
    """Any byte from the port means something is listening: foreign, not down."""

    def garbage(conn: socket.socket) -> None:
        conn.sendall(b"SSH-2.0-OpenSSH_9.9\r\n\r\n")

    port = raw_server(garbage)
    assert cli._healthz(f"http://127.0.0.1:{port}", 1.0, challenge="n" * 20) == {}


def test_probe_counts_an_unexpected_error_after_a_byte_as_an_answer(
    http_server: Callable[[Handler], int],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception the probe doesn't expect, raised after bytes arrived, must not
    kill the worker and pass for "nothing listening"."""
    original = cli._AnswerStream.read

    def failing_read(
        self: cli._AnswerStream, max_bytes: int, timeout: float | None = None
    ) -> bytes:
        original(self, max_bytes, timeout)  # the first byte arrives (answered is set)
        raise ValueError("unexpected parser failure")

    monkeypatch.setattr(cli._AnswerStream, "read", failing_read)
    port = http_server(proving)
    with caplog.at_level(logging.DEBUG, logger="diffbell"):
        assert cli._healthz(f"http://127.0.0.1:{port}", 1.0, challenge="n" * 20) == {}
    (record,) = [r for r in caplog.records if r.getMessage() == "healthz probe failed"]
    assert record.levelno == logging.DEBUG and record.exc_info is not None
    assert "127.0.0.1" not in record.getMessage()  # never the URL (it may carry a nonce)


def test_probe_counts_a_silent_close_as_no_answer() -> None:
    port = raw_server(lambda conn: None)  # accepts, reads the request, closes: no byte
    assert cli._healthz(f"http://127.0.0.1:{port}", 1.0, challenge="n" * 20) is None


def test_probe_survives_a_deeply_nested_json_body(http_server: Callable[[Handler], int]) -> None:
    def nested(h: BaseHTTPRequestHandler, challenge: str | None) -> None:
        reply(h, 200, b"[" * 60_000, ("Content-Type", "application/json"))

    port = http_server(nested)
    assert cli._healthz(f"http://127.0.0.1:{port}", 2.0, challenge="n" * 20) == {}


def test_probe_caps_the_body(http_server: Callable[[Handler], int]) -> None:
    """A valid proof padded past 64 KiB is still refused: the body is capped."""

    def padded(h: BaseHTTPRequestHandler, challenge: str | None) -> None:
        proof = auth.prove(OURS, challenge or "")
        body = json.dumps({"ok": True, "proof": proof, "pad": " " * cli.HEALTHZ_MAX_BODY})
        reply(h, 200, body.encode(), ("Content-Type", "application/json"))

    port = http_server(padded)
    assert cli._healthz(f"http://127.0.0.1:{port}", 2.0, challenge="n" * 20) == {}


def test_probe_stops_reading_an_endless_body() -> None:
    sent = [0]

    def endless(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
        chunk = b" " * (1 << 16)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            conn.sendall(chunk)
            sent[0] += len(chunk)

    port = raw_server(endless)
    started = time.monotonic()
    assert cli._healthz(f"http://127.0.0.1:{port}", 2.0, challenge="n" * 20) == {}
    assert time.monotonic() - started < 3.0


def json_reply(code: int, body: bytes, *headers: tuple[str, str]) -> Handler:
    return lambda h, challenge: reply(h, code, body, *headers)


@pytest.mark.parametrize(
    "handle",
    [
        json_reply(500, b"{}"),
        json_reply(404, b'{"detail": "Not Found"}'),
        json_reply(302, b"", ("Location", "http://example.invalid/healthz")),
        json_reply(200, b"hello", ("Content-Type", "text/plain")),
        json_reply(200, b"[1, 2]"),
        json_reply(200, b'{"ok": true}'),
    ],
    ids=["500", "404", "redirect", "non-JSON", "JSON array", "no proof"],
)
@pytest.mark.usefixtures("daemon_lock")
def test_any_answer_without_a_proof_is_foreign(
    tmp_dir: Path,
    browser: Browser,
    http_server: Callable[[Handler], int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    handle: Handler,
    clock: list[float],
) -> None:
    port = http_server(handle)
    write_config(tmp_dir, enabled=True, port=port)
    # Fake sleeps, real probes: the probes get budgets of 1.0/0.75/0.5/0.25 s, so
    # the last one (which decides) never times out on a slow machine.
    monkeypatch.setattr(cli, "WEB_WAIT", 1.0)
    monkeypatch.setattr(cli, "WEB_PROBE", 2.0)
    assert main(["web"]) == 1
    assert (
        f"Port {port} is served by another process; refusing to send a login link."
        in capsys.readouterr().out
    )
    assert browser.opened == [] and login_rows(tmp_dir) == 0


@pytest.mark.usefixtures("daemon_lock")
def test_nothing_listening_did_not_answer(
    tmp_dir: Path,
    browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: list[float],
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])  # closed again: nothing listens there
    write_config(tmp_dir, enabled=True, port=port)
    # Fake sleeps, real probes (as above).
    monkeypatch.setattr(cli, "WEB_WAIT", 1.0)
    monkeypatch.setattr(cli, "WEB_PROBE", 2.0)
    assert main(["web"]) == 1
    assert "did not answer" in capsys.readouterr().out
    assert browser.opened == []


@pytest.mark.usefixtures("daemon_lock")
def test_web_logs_in_against_a_real_proving_server(
    tmp_dir: Path,
    browser: Browser,
    http_server: Callable[[Handler], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = http_server(proving)
    write_config(tmp_dir, enabled=True, port=port)
    monkeypatch.setattr(cli, "WEB_WAIT", 2.0)
    assert main(["web"]) == 0
    (url,) = browser.opened
    assert url.startswith(f"http://127.0.0.1:{port}/login?t=")


@pytest.mark.parametrize("argv", [["web"], ["web", "--logout"]])
def test_web_never_migrates_under_a_running_daemon(
    tmp_dir: Path,
    browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """A pre-v6 database while the (older) daemon runs: opening the Store would
    migrate it under that daemon. Refused, the config left alone."""
    cfg = write_config(tmp_dir, enabled=False)
    before = cfg.read_bytes()
    state = tmp_dir / "state"
    state.mkdir()
    make_v5(state / "state.db")
    health = Health(up=True)
    monkeypatch.setattr(cli, "_healthz", health)

    def no_store(path: Path) -> Store:
        raise AssertionError("web must not open the Store")

    monkeypatch.setattr(cli, "Store", no_store)
    with singleton_lock(state / "daemon.lock"):
        assert main(argv) == 2
    out = capsys.readouterr().out
    assert "the database needs migrating: restart the daemon (`diffbell install`)" in out
    assert cfg.read_bytes() == before and browser.opened == [] and health.probes == 0
    assert not list(state.glob("state-pre-v6-*.db"))


@pytest.mark.usefixtures("daemon_lock")
def test_web_proceeds_when_the_sync_finds_the_database_locked(
    tmp_dir: Path,
    browser: Browser,
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_dir, enabled=True)
    monkeypatch.setattr(cli, "_healthz", Health(up=True))

    def locked(self: Store, projects: Any, now: float) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "sync_projects", locked)
    assert main(["web"]) == 0
    assert len(browser.opened) == 1
