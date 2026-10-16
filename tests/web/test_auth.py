from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from diffbell.store import Store
from diffbell.web import auth

SECRET = bytes(range(32))  # the running server's per-start secret


@pytest.fixture
def store(tmp_dir: Path) -> Store:
    return Store(tmp_dir / "state.db")


def test_tokens_are_random_and_only_hashes_are_stored(store: Store) -> None:
    token = auth.issue_login(store, now=0.0, secret=SECRET)
    assert len(token) >= 43 and token != auth.issue_login(store, now=0.0, secret=SECRET)
    stored = [r[0] for r in store._db.execute("SELECT token_hash FROM web_logins")]
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored


def test_login_token_is_bound_to_the_server_secret(store: Store) -> None:
    """A token minted for one server start is useless after a restart (new secret)."""
    token = auth.issue_login(store, now=0.0, secret=SECRET)
    (bound,) = store._db.execute("SELECT secret_hash FROM web_logins").fetchone()
    assert bound == hashlib.sha256(SECRET).hexdigest()
    assert auth.redeem_login(store, token, now=1.0, secret=bytes(32)) is None
    assert auth.redeem_login(store, token, now=1.0, secret=SECRET) is None  # and gone


def test_login_token_is_single_use(store: Store) -> None:
    token = auth.issue_login(store, now=0.0, secret=SECRET)
    first = auth.redeem_login(store, token, now=1.0, secret=SECRET)
    assert first is not None
    assert auth.redeem_login(store, token, now=1.0, secret=SECRET) is None


def test_login_token_expires_after_60_seconds(store: Store) -> None:
    token = auth.issue_login(store, now=0.0, secret=SECRET)
    assert auth.redeem_login(store, token, now=60.0, secret=SECRET) is None
    assert auth.redeem_login(store, "", now=0.0, secret=SECRET) is None


def test_session_lasts_30_days_absolute(store: Store) -> None:
    token = auth.issue_login(store, now=0.0, secret=SECRET)
    redeemed = auth.redeem_login(store, token, now=0.0, secret=SECRET)
    assert redeemed is not None
    session_id, csrf = redeemed
    stored = [r[0] for r in store._db.execute("SELECT token_hash FROM web_sessions")]
    assert stored == [auth.hash_token(session_id)]  # the cookie value itself is never stored
    day = 24 * 3600.0
    session = auth.session_for(store, session_id, now=29 * day)
    assert session is not None and session.csrf == csrf
    assert auth.session_for(store, session_id, now=30 * day) is None  # used daily or not
    assert auth.session_for(store, None, now=0.0) is None
    assert auth.session_for(store, "forged", now=0.0) is None


def test_end_session(store: Store) -> None:
    token = auth.issue_login(store, now=0.0, secret=SECRET)
    redeemed = auth.redeem_login(store, token, now=0.0, secret=SECRET)
    assert redeemed is not None
    auth.end_session(store, redeemed[0])
    assert auth.session_for(store, redeemed[0], now=1.0) is None


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1:8765", True),
        ("localhost:8765", True),
        ("127.0.0.1:8766", False),
        ("evil.example:8765", False),
        ("127.0.0.1", False),
        (None, False),
    ],
)
def test_host_check(host: str | None, ok: bool) -> None:
    assert auth.host_ok(host, 8765) is ok


@pytest.mark.parametrize(
    ("origin", "ok"),
    [
        (None, True),
        ("http://127.0.0.1:8765", True),
        ("http://localhost:8765", True),
        ("https://127.0.0.1:8765", False),
        ("http://evil.example", False),
        ("null", False),
    ],
)
def test_origin_check(origin: str | None, ok: bool) -> None:
    assert auth.origin_ok(origin, 8765) is ok


def test_csrf_check(store: Store) -> None:
    token = auth.issue_login(store, now=0.0, secret=SECRET)
    redeemed = auth.redeem_login(store, token, now=0.0, secret=SECRET)
    assert redeemed is not None
    session = auth.session_for(store, redeemed[0], now=1.0)
    assert session is not None
    assert auth.csrf_ok(session, redeemed[1]) is True
    assert auth.csrf_ok(session, "wrong") is False
    assert auth.csrf_ok(session, None) is False
    # hmac.compare_digest refuses non-ASCII str: a hostile header must not raise
    assert auth.csrf_ok(session, "\u00e9" * len(redeemed[1])) is False
    assert auth.csrf_ok(session, "\ud800") is False


def test_security_headers() -> None:
    assert auth.SECURITY_HEADERS == {
        "Content-Security-Policy": (
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def test_challenge_proof() -> None:
    secret = bytes(range(32))
    nonce = "0123456789abcdef-_XY"
    proof = auth.prove(secret, nonce)
    assert len(proof) == 64 and secret.hex() not in proof
    assert auth.proof_ok(secret, nonce, proof) is True
    assert auth.proof_ok(secret, nonce + "Z", proof) is False  # bound to the nonce
    assert auth.proof_ok(bytes(32), nonce, proof) is False  # bound to the secret
    assert auth.proof_ok(secret, nonce, None) is False
    assert auth.proof_ok(secret, nonce, "\u00e9" * 64) is False  # non-ASCII proof
    assert auth.proof_ok(secret, nonce, "\udcff") is False
    assert auth.proof_ok(secret, "\u00e9" * 20, proof) is False  # non-ASCII nonce


@pytest.mark.parametrize(
    ("nonce", "ok"),
    [
        ("a" * 16, True),
        ("A-_9" * 16, True),
        ("a" * 15, False),
        ("a" * 65, False),
        ("a b" * 8, False),
        ("é" * 16, False),
    ],
)
def test_valid_nonce(nonce: str, ok: bool) -> None:
    assert auth.valid_nonce(nonce) is ok
