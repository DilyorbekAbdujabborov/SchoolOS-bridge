"""Find access-control terminals on the local network.

The bridge sits on the school LAN, so discovery happens here — not in the cloud.
Strategy:

1. Work out the local subnet (the /24 around the machine's primary address), or
   accept an explicit IP / CIDR / range from the operator.
2. Probe every host concurrently on the HTTP ports Hikvision terminals use.
3. Keep the hosts that answer ISAPI (`/ISAPI/System/deviceInfo`): a Digest
   challenge or a `<DeviceInfo>` body means it is a terminal, not a printer.
4. Once the operator supplies a password, `probe()` reads the serial, model,
   firmware and clock through the real driver — the same code the bridge runs.
"""

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from ..config import DeviceConfig
from ..drivers import DriverError, build_driver

ISAPI_INFO = "/ISAPI/System/deviceInfo"
#: Hikvision web/ISAPI ports. 80 is the default; some installs move it to 8000.
HTTP_PORTS = (80, 8000)
#: Substrings that mark a Hikvision web server (or its ISAPI SDK bridge).
HIKVISION_MARKERS = ("hikvision", "webs", "app-webs", "dnp", "hiksdk")
#: How many hosts to probe at once — a /24 finishes in a few seconds.
DEFAULT_WORKERS = 64
#: Per-host connect/read budget while sweeping (unresponsive IPs are common).
DEFAULT_SWEEP_TIMEOUT = 0.4
#: A confirmed terminal gets a little more time to answer the identity request.
DEFAULT_PROBE_TIMEOUT = 6.0
#: Refuse to sweep anything larger than this: a /8 would take hours and is
#: never what a technician means to type.
MAX_HOSTS = 1024


class ScanError(Exception):
    """The requested range is too large to sweep."""


def primary_ip() -> str:
    """This machine's outbound address (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def default_subnet() -> str:
    """The /24 around the primary address, as a CIDR string."""
    try:
        return str(ipaddress.ip_network(f"{primary_ip()}/24", strict=False))
    except ValueError:
        return ""


def _hosts(target: str):
    """Turn an operator's text into a list of host addresses.

    Accepts a CIDR (`192.168.1.0/24`), a single IP, or a range
    (`192.168.1.10-40`, where the trailing number is the final last octet).
    """
    target = (target or "").strip()
    if not target:
        return []
    try:
        if "/" in target:
            network = ipaddress.ip_network(target, strict=False)
            if network.num_addresses > MAX_HOSTS + 2:
                raise ScanError(f"{network} ichida {network.num_addresses} manzil bor (maksimum {MAX_HOSTS})")
            return [str(ip) for ip in network.hosts()]
        if "-" in target:
            start, _, end = target.partition("-")
            first = ipaddress.ip_address(start.strip())
            end_text = end.strip()
            if end_text.isdigit() and first.version == 4 and int(end_text) <= 255:
                # `10.0.0.10-12`: the last octet runs from .10 through .12.
                last = ipaddress.IPv4Address((int(first) & ~0xFF) | int(end_text))
            else:  # `10.0.0.10-10.0.0.40`: a full end address
                last = ipaddress.ip_address(end_text)
            return _expand_range(first, last)
        return [str(ipaddress.ip_address(target))]
    except ScanError:
        raise
    except ValueError:
        return []


def _expand_range(first, last):
    if int(last) < int(first):
        first, last = last, first
    count = int(last) - int(first) + 1
    if count > MAX_HOSTS:
        raise ScanError(f"oraliqda {count} manzil bor (maksimum {MAX_HOSTS})")
    return [str(first.__class__(value)) for value in range(int(first), int(last) + 1)]


def looks_like_terminal(response) -> bool:
    """Does this HTTP response come from an ISAPI terminal?"""
    if response.status_code == 401:
        auth = (response.headers.get("WWW-Authenticate") or "").lower()
        server = (response.headers.get("Server") or "").lower()
        return "digest" in auth or any(marker in server for marker in HIKVISION_MARKERS)
    if response.status_code == 200:
        text = response.text[:4000].lower()
        return any(marker in text for marker in ("serialnumber", "devicename", "hikvision"))
    return False


def _url(ip: str, port: int) -> str:
    host = f"{ip}:{port}" if port != 80 else ip
    return f"http://{host}{ISAPI_INFO}"


def detect(ip: str, timeout: float = DEFAULT_SWEEP_TIMEOUT, http=requests):
    """Probe one host; return a candidate dict when it is a terminal, else None."""
    for port in HTTP_PORTS:
        try:
            response = http.get(_url(ip, port), timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            continue
        if looks_like_terminal(response):
            return {
                "ip": ip,
                "port": port,
                "server": response.headers.get("Server") or "",
                "status": response.status_code,
            }
    return None


def scan(target: str = "", timeout: float = DEFAULT_SWEEP_TIMEOUT, workers: int = DEFAULT_WORKERS, http=requests):
    """Sweep `target` (CIDR / IP / range) and return the terminals that answer.

    An empty `target` uses the local /24. `http` is injected by tests."""
    hosts = _hosts(target) or _hosts(default_subnet())
    found = []
    if not hosts:
        return found
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(detect, ip, timeout, http): ip for ip in hosts}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:  # a bug in one probe must not sink the sweep
                result = None
            if result:
                found.append(result)
    found.sort(key=lambda row: ipaddress.ip_address(row["ip"]))
    return found


def probe(ip: str, username: str, password: str, port: int = 80, timeout: float = DEFAULT_PROBE_TIMEOUT, http=requests):
    """Read a terminal's identity with the given credentials.

    Returns a dict on success, or `{"error": "..."}` when it is unreachable or
    the login was refused. Uses the real `HikvisionDriver`, so a success here
    means the bridge will be able to drive the terminal."""

    host = f"{ip}:{port}" if port and port != 80 else ip
    config = DeviceConfig(serial="", vendor="hikvision", ip=host, username=username, password=password)
    driver = build_driver(config)
    driver.timeout = timeout
    driver.http = http
    try:
        info = driver.probe()
    except DriverError as exc:
        text = str(exc)
        if "HTTP 401" in text:
            return {"ip": ip, "port": port, "ok": False, "error": "login_required"}
        if "HTTP" in text:
            return {"ip": ip, "port": port, "ok": False, "error": "isapi", "detail": text}
        return {"ip": ip, "port": port, "ok": False, "error": "unreachable", "detail": text}
    if not info.serial:
        return {"ip": ip, "port": port, "ok": False, "error": "not_a_terminal"}
    return {
        "ip": ip,
        "port": port,
        "ok": True,
        "serial": info.serial,
        "model": info.model,
        "firmware": info.firmware,
        "time_zone": info.time_zone,
        "local_time": info.local_time,
    }
