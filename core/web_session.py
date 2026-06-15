"""
core/web_session.py — stateless signed session tokens for hatmaster.tv
======================================================================

Implements the session cookie described in WEBSITE_TRADING_DESIGN.md
§3.2. A session is a signed claim, not a database row:

    token   = base64url(canonical-json payload) + "." + hex(HMAC-SHA256(body, secret))
    payload = {"uid": ..., "login": ..., "name": ..., "img": ...,
               "iat": <unix>, "exp": <unix>}

Design notes:
  * Stdlib only (hmac, hashlib, base64, json, secrets, time). No
    aiohttp imports — the webserver owns cookie/HTTP concerns, this
    module owns crypto + encoding, so it unit-tests in isolation
    (tools/test_web_session.py).
  * No server-side session store. Rotating WEB_SESSION_SECRET in
    config_local.py invalidates every outstanding session — that is
    the global "log everyone out" lever.
  * Canonical JSON (sorted keys, no whitespace) so the signed bytes
    are deterministic.
  * verify() is the ONLY entry point for untrusted input. It never
    raises on garbage — any malformed, tampered, or expired token
    returns None, which callers treat as "logged out".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

# Cookie names used by core/public_webserver.py. Defined here so the
# tests and the webserver agree on a single source of truth.
SESSION_COOKIE = "hatmas_session"
OAUTH_STATE_COOKIE = "hatmas_oauth_state"

DEFAULT_MAX_AGE = 30 * 24 * 3600  # 30 days


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _mac(body: str, secret) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()


def sign(payload: dict, secret) -> str:
    """Serialize + sign a payload dict into a token string. The
    payload must be JSON-serializable. Caller is responsible for
    including iat/exp (or use issue())."""
    if not secret:
        raise ValueError("empty session secret")
    body = _b64e(json.dumps(
        payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_mac(body, secret)}"


def verify(token: Optional[str], secret, now: Optional[float] = None
           ) -> Optional[dict]:
    """Verify a token and return its payload, or None.

    None for: missing/empty token, wrong shape, bad signature,
    non-JSON body, missing exp, or expired. Never raises on
    untrusted input.
    """
    if not token or not secret or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    body, mac = parts
    try:
        expected = _mac(body, secret)
    except Exception:
        return None
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    if (now if now is not None else time.time()) >= exp:
        return None
    return payload


def issue(uid: str, login: str, display_name: str = "",
          profile_image: str = "", secret=None,
          max_age: int = DEFAULT_MAX_AGE) -> str:
    """Build + sign a session token for a Twitch identity. login is
    stored lowercase — it's the key MixItUp balances and portfolios
    rows use, and Helix logins are lowercase already."""
    now = int(time.time())
    return sign({
        "uid": str(uid),
        "login": (login or "").lower(),
        "name": display_name or login,
        "img": profile_image or "",
        "iat": now,
        "exp": now + int(max_age),
    }, secret)


def make_state() -> str:
    """Random nonce for the OAuth state parameter (CSRF guard on the
    login flow). 32 bytes of urlsafe entropy."""
    return secrets.token_urlsafe(32)
