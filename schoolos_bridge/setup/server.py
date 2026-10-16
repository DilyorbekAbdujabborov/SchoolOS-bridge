"""The setup wizard's HTTP server: static assets + a small JSON API.

Runs inside the bridge process, on its own port (never the terminal-facing
listen port), on localhost unless told otherwise. Every `/api/*` call must carry
a session token (`Authorization: Bearer`), obtained by trading the one-time
sign-in link the bridge opens in the browser (see `auth`). Nobody types a PIN.
"""

import json
import logging
import mimetypes
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..checks import run_checks
from ..vault import VaultError
from ..cloud import CloudClient, CloudError, explain
from ..config import DIRECTIONS, VENDORS, normalize_cloud_url
from . import discovery, store
from .auth import AuthError, Sessions

log = logging.getLogger("schoolos_bridge.setup")


def _static_dir() -> Path:
    """Where the wizard's web files live.

    From source that is the package directory. In a PyInstaller build the code
    lives inside the temporary ``_MEIPASS`` directory and the files are bundled
    next to it, so prefer that copy when present.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        frozen = Path(bundle) / "schoolos_bridge" / "setup" / "static"
        if frozen.is_dir():
            return frozen
    return Path(__file__).resolve().parent / "static"


STATIC_DIR = _static_dir()
MAX_BODY = 1 << 20  # 1 MiB is ample for the wizard's payloads
#: A browser that opens a socket and stalls must not pin a worker thread.
SOCKET_TIMEOUT = 30
#: Bad-token attempts allowed from one address before it is answered 429.
AUTH_MAX_FAILURES = 10
AUTH_LOCKOUT_SECONDS = 60

#: Sent with every response. The wizard ships no inline script/style and never
#: talks to any host but itself, so a strict policy costs nothing and turns a
#: would-be XSS into a non-event. `nosniff` keeps a mistyped content type from
#: being re-interpreted as script.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'",
    ),
)


def register_devices(cloud_url, token, devices, client=None):
    """Tell the cloud about the configured terminals so it registers them.

    The cloud creates a terminal its school has never seen (the bridge knows the
    LAN address the director cannot) and runs the school's full sync, so
    configuring a terminal here is enough — nobody adds it by hand. Best-effort:
    if this is skipped, the service's next heartbeat registers it anyway."""
    url = normalize_cloud_url(cloud_url)
    token = (token or "").strip()
    items = [
        {
            "serial": (device.get("serial") or "").strip(),
            "ip": device.get("ip") or None,
            "vendor": device.get("vendor") or "hikvision",
            "model": device.get("model") or "",
            "direction": device.get("direction") or "BOTH",
            "location_label": device.get("location_label") or "",
        }
        for device in devices
        if (device.get("serial") or "").strip()
    ]
    if not token:
        return {"ok": False, "error": "not_configured", "message": "Bridge tokeni kerak."}
    if not items:
        return {"ok": False, "error": "no_devices", "message": "Sozlamada terminal yo'q."}
    client = client or CloudClient(url, token, timeout=10)
    try:
        client.heartbeat(items)
    except CloudError as exc:
        code, message = explain(exc, normalize_cloud_url(url))
        return {"ok": False, "error": code, "message": message}
    return {"ok": True, "count": len(items)}


def host_platform() -> str:
    """The OS the bridge runs on: "windows", "macos" or "linux"."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        config_path,
        auth=True,
        scan_timeout=discovery.DEFAULT_SWEEP_TIMEOUT,
        status=None,
    ):
        super().__init__(address, _Handler)
        self.config_path = str(config_path) if config_path else ""
        #: None = --no-auth (tests / a trusted dev box).
        self.sessions = Sessions(self.config_path) if auth else None
        self.scan_timeout = scan_timeout
        #: Callable returning the running bridge's status dict (or None).
        self.status = status
        #: client address -> recent bad-token timestamps (monotonic seconds).
        self._failures: dict[str, list[float]] = {}
        self._failures_lock = threading.Lock()

    def _prune(self, host: str, now: float) -> list[float]:
        cutoff = now - AUTH_LOCKOUT_SECONDS
        recent = [t for t in self._failures.get(host, ()) if t > cutoff]
        self._failures[host] = recent
        return recent

    def locked(self, host: str) -> bool:
        """True when `host` has burned its bad-token budget recently."""
        if self.sessions is None:
            return False
        with self._failures_lock:
            return len(self._prune(host, time.monotonic())) >= AUTH_MAX_FAILURES

    def record_failure(self, host: str) -> None:
        with self._failures_lock:
            self._prune(host, time.monotonic()).append(time.monotonic())


class _Handler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT

    server_version = "SchoolOSBridgeSetup/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------------
    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _security_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message, code="error"):
        self._json(status, {"ok": False, "error": code, "message": message})

    def _body(self) -> dict:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY:
            # Refuse the oversized payload and hang up: the unread remainder
            # would otherwise be parsed as the next request on this connection.
            self.close_connection = True
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _bearer(self) -> str:
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    def _authorized(self) -> bool:
        sessions = self.server.sessions
        if sessions is None:
            return True
        try:
            sessions.check(self._bearer())
        except AuthError:
            return False
        return True

    def _static(self, rel: str):
        rel = unquote(rel)
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            self._error(404, "topilmadi", "not_found")
            return
        if not target.is_file():
            self._error(404, "topilmadi", "not_found")
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    # -- routes ---------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        self._serve(self._route_get)

    def do_POST(self):  # noqa: N802
        self._serve(self._route_post)

    def _serve(self, route):
        """A client that hangs up mid-response is normal (the browser moved on);
        do not let it print a traceback."""
        try:
            route()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("setup request failed: %s", exc)
            try:
                self._error(500, "server xatosi", "server_error")
            except Exception:
                pass

    def _route_get(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._static("index.html")
            return
        if path.startswith("/assets/"):
            self._static(path[len("/assets/"):])
            return
        if path == "/api/state" and self._guard():
            self._state()
            return
        self._error(404, "topilmadi", "not_found")

    def _route_post(self):
        path = urlparse(self.path).path
        # Authenticate before looking up the route: an unauthorised peer must not
        # be able to map which endpoints exist by reading 401 vs 404.
        if path == "/api/session":
            self._session()
            return
        if not self._guard():
            return
        routes = {
            "/api/logout": self._logout,
            "/api/device-delete": self._device_delete,
            "/api/scan": self._scan,
            "/api/probe": self._probe,
            "/api/cloud-test": self._cloud_test,
            "/api/register": self._register,
            "/api/save": self._save,
            "/api/check": self._check,
        }
        handler = routes.get(path)
        if handler is None:
            self._error(404, "topilmadi", "not_found")
            return
        handler()

    def _guard(self) -> bool:
        host = self.client_address[0]
        if self.server.locked(host):
            self._error(429, "Juda ko'p urinish. Birozdan so'ng qayta urinib ko'ring.", "too_many_requests")
            return False
        if not self._authorized():
            self.server.record_failure(host)
            self._error(401, "Sessiya tugagan. Bridge'ni qayta oching.", "unauthorized")
            return False
        return True

    # -- handlers -------------------------------------------------------------
    def _session(self):
        """Trade a one-time sign-in link for a session token."""
        host = self.client_address[0]
        if self.server.sessions is None:
            self._json(200, {"ok": True, "token": "", "expires": 0})
            return
        if self.server.locked(host):
            self._error(429, "Juda ko'p urinish. Birozdan so'ng qayta urinib ko'ring.", "too_many_requests")
            return
        link = (self._body().get("link") or "").strip()
        try:
            token, expires = self.server.sessions.exchange(link)
        except AuthError:
            self.server.record_failure(host)
            self._error(401, "Kirish havolasi eskirgan yoki ishlatilgan. Bridge'ni qayta oching.", "bad_link")
            return
        self._json(200, {"ok": True, "token": token, "expires": expires})

    def _logout(self):
        if self.server.sessions is not None:
            self.server.sessions.revoke(self._bearer())
        self._json(200, {"ok": True})

    def _device_delete(self):
        """Remove one terminal from the config right away (no separate save)."""
        payload = self._body()
        serial = (payload.get("serial") or "").strip()
        ip = (payload.get("ip") or "").strip()
        if not serial and not ip:
            self._error(400, "Terminal ko'rsatilmagan", "bad_request")
            return
        data = self._config()
        if data is None:
            return
        devices = data.get("devices", [])
        kept = [
            d for d in devices
            if not ((serial and d.get("serial") == serial) or (not serial and ip and d.get("ip") == ip))
        ]
        if len(kept) == len(devices):
            self._json(200, {"ok": True, "deleted": 0})
            return
        data["devices"] = kept
        store.write_config(self.server.config_path, data)
        self._json(200, {"ok": True, "deleted": len(devices) - len(kept)})

    def _state(self):
        public = store.public_config(self.server.config_path)
        public.update(
            {
                "hostname": socket.gethostname(),
                "default_subnet": discovery.default_subnet(),
                "primary_ip": discovery.primary_ip(),
                "default_username": "admin",
                "config_path": self.server.config_path,
                "platform": host_platform(),
                "service": self.server.status() if self.server.status else None,
            }
        )
        self._json(200, public)

    def _scan(self):
        payload = self._body()
        target = (payload.get("target") or "").strip()
        try:
            found = discovery.scan(target, timeout=self.server.scan_timeout)
        except discovery.ScanError as exc:
            self._error(400, str(exc), "scan_too_large")
            return
        except Exception as exc:  # never 500 the wizard on a sweep hiccup
            log.exception("scan failed: %s", exc)
            self._error(500, "skaner xatosi", "scan_failed")
            return
        known = {d.get("ip") for d in store.read_raw(self.server.config_path).get("devices", [])}
        for row in found:
            row["configured"] = row["ip"] in known or f"{row['ip']}:{row['port']}" in known
        self._json(
            200,
            {"ok": True, "target": target or discovery.default_subnet(), "found": found},
        )

    def _probe(self):
        payload = self._body()
        ip = (payload.get("ip") or "").strip()
        port = int(payload.get("port") or 80)
        username = (payload.get("username") or "admin").strip()
        password = payload.get("password") or ""
        if not ip:
            self._error(400, "IP kerak", "bad_request")
            return
        if not password:
            password = self._stored_password(ip, port)
        if not password:
            self._error(400, "Parol kerak", "password_required")
            return
        result = discovery.probe(ip, username, password, port)
        self._json(200, result)

    def _cloud_test(self):
        payload = self._body()
        url = normalize_cloud_url((payload.get("cloud_url") or "").strip() or self._stored("cloud_url"))
        token = (payload.get("token") or "").strip() or self._stored("token")
        if not token:
            self._error(400, "Token kerak", "token_required")
            return
        try:
            CloudClient(url, token, timeout=10).heartbeat([])
        except CloudError as exc:
            code, message = explain(exc, url)
            self._json(200, {"ok": False, "error": code, "message": message})
            return
        self._json(200, {"ok": True, "message": "Token qabul qilindi.", "cloud_url": url})

    def _save(self):
        payload = self._body()
        existing = self._config(strict=False)
        try:
            data = store.merge_save(existing, payload)
        except (TypeError, ValueError) as exc:
            self._error(400, f"Sozlama xatosi: {exc}", "bad_request")
            return
        error = self._validate(data)
        if error:
            self._error(400, error, "validation")
            return
        if not self.server.config_path:
            self._error(400, "Config fayl manzili noma'lum", "no_path")
            return
        store.write_config(self.server.config_path, data)
        public = store.public_config(self.server.config_path)
        public["saved"] = True
        # Register the terminals with the cloud right away so they exist (and
        # start syncing) even before the service is running. A failure here must
        # not fail the save — the service's heartbeat registers them anyway.
        public["registration"] = register_devices(
            data.get("cloud_url"), data.get("token"), data.get("devices", [])
        )
        self._json(200, public)

    def _register(self):
        """Publish the saved terminals to the cloud without writing the config —
        the wizard's explicit "cloudga qo'shish" step."""
        payload = self._body()
        saved = self._config()
        if saved is None:
            return
        url = (payload.get("cloud_url") or saved.get("cloud_url") or "").strip()
        token = (payload.get("token") or saved.get("token") or "").strip()
        self._json(200, register_devices(url, token, saved.get("devices", [])))

    def _check(self):
        if not self.server.config_path:
            self._error(400, "Config fayl manzili noma'lum", "no_path")
            return
        try:
            results = run_checks(self.server.config_path)
        except Exception as exc:
            log.exception("checks failed: %s", exc)
            self._error(500, "tekshiruv xatosi", "check_failed")
            return
        self._json(
            200,
            {
                "ok": True,
                "results": [
                    {"status": r.status, "name": r.name, "detail": r.detail} for r in results
                ],
            },
        )

    # -- helpers --------------------------------------------------------------
    def _validate(self, data) -> str | None:
        if not data.get("token"):
            return "Bridge tokeni kiritilmagan."
        for index, device in enumerate(data.get("devices", []), start=1):
            label = f"{index}-terminal"
            if not device.get("serial"):
                return f"{label}: seriya raqami yo'q."
            if device.get("vendor") not in VENDORS:
                return f"{label}: vendor noto'g'ri."
            if not device.get("ip"):
                return f"{label}: IP manzil yo'q."
            if not device.get("password"):
                return f"{label}: parol yo'q."
            if device.get("direction") not in DIRECTIONS:
                return f"{label}: yo'nalish noto'g'ri."
        return None

    def _config(self, strict=True):
        """The saved config with secrets opened. A key that cannot open them
        (config copied from another PC) answers 409 when `strict`, or reads as
        empty secrets so a fresh save can replace them."""
        try:
            return store.read_config(self.server.config_path)
        except VaultError as exc:
            if not strict:
                raw = store.read_raw(self.server.config_path)
                raw.pop("token", None)
                for device in raw.get("devices", []):
                    device.pop("password", None)
                return raw
            self._error(409, str(exc), "vault")
            return None

    def _stored(self, key):
        return (self._config(strict=False) or {}).get(key, "")

    def _stored_password(self, ip, port):
        host = f"{ip}:{port}" if port and port != 80 else ip
        for device in self._config(strict=False).get("devices", []):
            if device.get("ip") in (ip, host):
                return device.get("password", "")
        return ""


def make_server(
    config_path, host, port, auth=True, scan_timeout=discovery.DEFAULT_SWEEP_TIMEOUT, status=None
) -> SetupServer:
    return SetupServer((host, port), config_path, auth=auth, scan_timeout=scan_timeout, status=status)


__all__ = ["SetupServer", "make_server"]
