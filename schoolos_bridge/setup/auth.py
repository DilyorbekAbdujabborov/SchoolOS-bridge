"""Sign-in for the setup page: a one-time link, then a session token.

Nobody types a PIN. Starting the bridge (or ``schoolos-bridge open``) opens the
browser at ``/#t=<link token>``:

* the **link token** is a short-lived (2 min), single-use JWT signed with
  ``ui.key`` — a key file next to the config that only the service account and
  administrators can read (DPAPI-sealed on Windows). Minting a link therefore
  needs the same rights as reading the config itself;
* the page trades it once for a **session token** — a JWT signed with a secret
  that exists only in this process's memory. It lives in the browser tab's
  ``sessionStorage``: closing the tab, signing out, or restarting the bridge
  ends it.

The token travels in the URL fragment, which browsers never send to a server or
put in a Referer, and the page drops it from the address bar at once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from ..vault import read_protected, write_protected

UI_KEY_FILE = "ui.key"
LINK_TTL = 120
SESSION_TTL = 8 * 3600


class AuthError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


_HEADER = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())


def encode(claims: dict, key: bytes) -> str:
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{_HEADER}.{body}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{_HEADER}.{body}.{_b64(signature)}"


def decode(token: str, key: bytes, typ: str, now: float | None = None) -> dict:
    """Verify signature, type and expiry; return the claims."""
    try:
        header, body, signature = token.split(".")
    except (AttributeError, ValueError) as exc:
        raise AuthError("bad token") from exc
    if header != _HEADER:
        raise AuthError("bad header")  # pins alg=HS256: no "none", no alg swap
    expected = hmac.new(key, f"{header}.{body}".encode("ascii"), hashlib.sha256).digest()
    try:
        given = _unb64(signature)
        claims = json.loads(_unb64(body))
    except (ValueError, TypeError) as exc:
        raise AuthError("bad token") from exc
    if not hmac.compare_digest(expected, given):
        raise AuthError("bad signature")
    if not isinstance(claims, dict) or claims.get("typ") != typ:
        raise AuthError("wrong token type")
    if float(claims.get("exp", 0)) < (time.time() if now is None else now):
        raise AuthError("expired")
    return claims


def ui_key_path(config_path) -> Path:
    from ..config import key_dir

    return key_dir(config_path) / UI_KEY_FILE


def load_ui_key(config_path, create: bool = True) -> bytes:
    """The link-signing key; created (owner-only / DPAPI) on first use."""
    path = ui_key_path(config_path)
    key = read_protected(path)
    if key is None:
        if not create:
            raise AuthError(f"{path} yo'q — bridge hali ishga tushmagan")
        key = _b64(secrets.token_bytes(32)).encode("ascii")
        write_protected(path, key, owner_of=Path(config_path).resolve() if Path(config_path).exists() else None)
    return key


def issue_link(config_path, ttl: int = LINK_TTL) -> str:
    """A single-use sign-in token for the setup page (see module docstring)."""
    now = time.time()
    claims = {"typ": "link", "jti": secrets.token_urlsafe(12), "iat": int(now), "exp": int(now + ttl)}
    return encode(claims, load_ui_key(config_path))


class Sessions:
    """Trades link tokens for in-memory session tokens and checks them."""

    def __init__(self, config_path, session_ttl: int = SESSION_TTL):
        self.config_path = config_path
        self.session_ttl = session_ttl
        self._secret = secrets.token_bytes(32)  # never written anywhere
        self._used: dict[str, float] = {}  # link jti -> its expiry
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    def exchange(self, link: str) -> tuple[str, int]:
        claims = decode(link, load_ui_key(self.config_path), "link")
        now = time.time()
        with self._lock:
            self._used = {jti: exp for jti, exp in self._used.items() if exp > now}
            jti = str(claims.get("jti") or "")
            if not jti or jti in self._used:
                raise AuthError("link already used")
            self._used[jti] = float(claims["exp"])
        exp = int(now + self.session_ttl)
        token = encode({"typ": "session", "sid": secrets.token_urlsafe(12), "exp": exp}, self._secret)
        return token, exp

    def check(self, token: str) -> dict:
        claims = decode(token, self._secret, "session")
        if claims.get("sid") in self._revoked:
            raise AuthError("signed out")
        return claims

    def revoke(self, token: str) -> None:
        try:
            claims = decode(token, self._secret, "session")
        except AuthError:
            return
        with self._lock:
            self._revoked.add(claims.get("sid"))
