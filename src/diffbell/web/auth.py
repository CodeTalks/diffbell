"""Login tokens, sessions, CSRF and request checks for the web UI (spec §5).

Framework-free on purpose: the CLI (`diffbell web`) imports it to mint a
login token without loading FastAPI. Only SHA-256 hashes of login tokens and
session ids are stored in ``state.db``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from diffbell.store import Store, WebSession

LOGIN_TTL = 60.0
SESSION_TTL = 30 * 24 * 3600.0
SESSION_COOKIE = "diffbell_session"
# The page's theme override (not a secret, authorises nothing): only these values
# count; no cookie, or any other value, means "follow the system".
THEME_COOKIE = "diffbell_theme"
FORCED_THEMES = ("light", "dark")
CSRF_HEADER = "x-diffbell-token"

CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


# A /healthz challenge: URL-safe base64 characters, 16-64 long.
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")


def new_token() -> str:
    """32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def secret_hash(secret: bytes) -> str:
    """What a login token is bound to: the SHA-256 of one server start's secret."""
    return hashlib.sha256(secret).hexdigest()


def issue_login(store: Store, now: float, secret: bytes) -> str:
    """Store a single-use login token's hash (valid LOGIN_TTL), bound to the server
    whose per-start ``secret`` the CLI just verified; return the token."""
    token = new_token()
    store.add_login(hash_token(token), now + LOGIN_TTL, secret_hash(secret))
    return token


def redeem_login(store: Store, token: str, now: float, secret: bytes) -> tuple[str, str] | None:
    """Consume a login token. Returns (session id, csrf token) for a new 30-day
    session, or None if the token is unknown, used, expired or was minted for
    another server start (a different ``secret``)."""
    if not token or not store.consume_login(hash_token(token), now, secret_hash(secret)):
        return None
    session_id, csrf = new_token(), new_token()
    store.add_session(hash_token(session_id), csrf, now, now + SESSION_TTL)
    return session_id, csrf


def session_for(store: Store, session_id: str | None, now: float) -> WebSession | None:
    """The live session for a cookie value (absolute expiry), touching last_used_at."""
    if not session_id:
        return None
    digest = hash_token(session_id)
    session = store.get_session(digest, now)
    if session is not None:
        store.touch_session(digest, now)
    return session


def end_session(store: Store, session_id: str | None) -> None:
    if session_id:
        store.delete_sessions(hash_token(session_id))


def host_ok(host: str | None, port: int) -> bool:
    """DNS-rebinding defence: only the exact loopback names with our port."""
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def origin_ok(origin: str | None, port: int) -> bool:
    """An absent Origin is fine (same-origin GET/HEAD); a present one must be ours."""
    return origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def _equal(given: str, expected: str) -> bool:
    """Constant-time compare as bytes: hmac.compare_digest raises TypeError on a
    non-ASCII str, and both values here can come from a hostile request. The expected
    value is always ASCII; any encoding problem is a mismatch."""
    try:
        return hmac.compare_digest(given.encode("utf-8", "surrogatepass"), expected.encode("ascii"))
    except UnicodeError:
        return False


def csrf_ok(session: WebSession, header: str | None) -> bool:
    return header is not None and _equal(header, session.csrf)


def valid_nonce(nonce: str) -> bool:
    return _NONCE.fullmatch(nonce) is not None


def prove(secret: bytes, nonce: str) -> str:
    """The /healthz proof: HMAC-SHA256 of the challenge under the server's per-start
    secret. Only a process that can read status.json (0600) can compute it."""
    return hmac.new(secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()


def proof_ok(secret: bytes, nonce: str, proof: object) -> bool:
    if not isinstance(proof, str):
        return False
    try:
        expected = prove(secret, nonce)
    except UnicodeError:  # a non-ASCII nonce cannot have been challenged
        return False
    return _equal(proof, expected)
