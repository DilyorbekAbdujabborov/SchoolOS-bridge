"""`schoolos-bridge check`: is this bridge ready to run?

Each check is one row of a PASS / WARN / FAIL table. Nothing here changes a
terminal; the only cloud call is an empty heartbeat (it proves the token and
marks the bridge "seen" on the Terminallar page). Secrets are never printed.
"""

import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .cloud import CloudClient, CloudError
from .config import Config, load_config
from .drivers import DriverError, build_driver

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
#: What the terminal must report: UTC+05:00 (POSIX inverts the sign).
SCHOOL_POSIX_TZ = "CST-5:00:00"
MAX_DRIFT = timedelta(minutes=2)
PLACEHOLDERS = {"", "PASTE-BRIDGE-TOKEN-FROM-ADMIN", "CHANGE-ME"}


@dataclass(frozen=True)
class Result:
    status: str
    name: str
    detail: str


def _check_permissions(path: Path) -> Result:
    name = "Fayl huquqlari / File permissions"
    if sys.platform == "win32":
        return Result(PASS, name, "Windows: ACL o'rnatuvchi tomonidan cheklangan")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return Result(FAIL, name, f"{oct(mode)} — boshqalar o'qiy oladi. Tuzatish: sudo chmod 600 {path}")
    return Result(PASS, name, oct(mode))


def _check_cloud_url(url: str) -> Result:
    name = "Bulut manzili / Cloud URL"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return Result(FAIL, name, f"noto'g'ri manzil: {url!r} (masalan https://maktab.example.uz)")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        return Result(WARN, name, f"{url} — HTTPS emas, token ochiq yuboriladi")
    return Result(PASS, name, url)


def check_cloud(config: Config, client=None) -> Result:
    name = "Bulut + token / Cloud + token"
    if config.token in PLACEHOLDERS:
        return Result(FAIL, name, "token kiritilmagan: Terminallar sahifasidan oling")
    client = client or CloudClient(config.cloud_url, config.token, timeout=10)
    try:
        client.heartbeat([])
    except CloudError as exc:
        text = str(exc)
        if "HTTP 401" in text or "HTTP 403" in text:
            return Result(FAIL, name, "token yaroqsiz yoki bridge faolsiz (401). Yangi token oling")
        if "HTTP" in text:
            return Result(FAIL, name, f"bulut xato qaytardi: {text}")
        return Result(FAIL, name, "bulutga ulanib bo'lmadi (internet / DNS / proxy?)")
    return Result(PASS, name, "token qabul qilindi")


def check_listen_port(config: Config) -> Result:
    name = f"Tinglash porti / Listen port {config.listen_port}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((config.listen_host, config.listen_port))
    except OSError:
        return Result(WARN, name, "band — bridge xizmati allaqachon ishlayotgan bo'lsa, bu normal")
    finally:
        sock.close()
    return Result(PASS, name, "bo'sh")


def check_device(device, driver=None, now=None) -> list[Result]:
    label = f"Terminal {device.ip}"
    if device.password in PLACEHOLDERS:
        return [Result(FAIL, label, "terminal paroli kiritilmagan")]
    driver = driver or build_driver(device)
    try:
        probe = driver.probe()
    except DriverError as exc:
        text = str(exc)
        if "HTTP 401" in text:
            return [Result(FAIL, label, "login yoki parol noto'g'ri (401)")]
        if "HTTP" in text:
            return [Result(FAIL, label, f"ISAPI xatosi: {text}")]
        return [Result(FAIL, label, "javob bermadi: IP, kabel va bir tarmoqda ekanini tekshiring")]

    results = []
    about = " · ".join(x for x in (probe.model, probe.firmware) if x)
    if probe.serial and probe.serial != device.serial:
        results.append(Result(
            WARN, label,
            f"ulandi ({about}), lekin seriya {probe.serial!r} — sozlamada {device.serial!r}",
        ))
    else:
        results.append(Result(PASS, label, f"ulandi{f' ({about})' if about else ''}"))

    zone_name = f"Vaqt zonasi / Time zone {device.ip}"
    if probe.time_zone != SCHOOL_POSIX_TZ:
        results.append(Result(
            FAIL, zone_name,
            f"{probe.time_zone or '?'} — UTC+05:00 kerak. Bridge ishga tushganda o'zi tuzatadi "
            "yoki terminalda: Configuration → System → Time → UTC+05:00",
        ))
        return results
    try:
        local = datetime.fromisoformat(probe.local_time)
        drift = abs(local - (now or datetime.now(local.tzinfo)))
    except (ValueError, TypeError):
        results.append(Result(PASS, zone_name, "UTC+05:00"))
        return results
    if drift > MAX_DRIFT:
        results.append(Result(WARN, zone_name, f"UTC+05:00, lekin soat {int(drift.total_seconds())} s farq qiladi (NTP?)"))
    else:
        results.append(Result(PASS, zone_name, "UTC+05:00, soat to'g'ri"))
    return results


def run_checks(path, cloud_client=None, drivers=None) -> list[Result]:
    """Every check for the config at `path`; `cloud_client` / `drivers`
    (serial → driver) are injected by tests."""
    path = Path(path)
    if not path.exists():
        return [Result(FAIL, "Sozlama / Config", f"{path} topilmadi. Avval: schoolos-bridge init")]
    try:
        config = load_config(path)
    except (OSError, ValueError) as exc:
        return [Result(FAIL, "Sozlama / Config", f"{path}: {exc}")]
    results = [Result(PASS, "Sozlama / Config", str(path)), _check_permissions(path)]
    url = _check_cloud_url(config.cloud_url)
    results.append(url)
    results.append(check_cloud(config, cloud_client) if url.status != FAIL else
                   Result(FAIL, "Bulut + token / Cloud + token", "manzil noto'g'ri"))
    results.append(check_listen_port(config))
    if not config.devices:
        results.append(Result(WARN, "Terminallar / Terminals", "sozlamada terminal yo'q: schoolos-bridge init"))
    for device in config.devices:
        results.extend(check_device(device, (drivers or {}).get(device.serial)))
    return results


def render(results: list[Result], color: bool | None = None) -> str:
    if color is None:
        color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and sys.platform != "win32"
    paint = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
    width = max(len(r.name) for r in results)
    lines = []
    for r in results:
        status = f"{paint[r.status]}{r.status}\033[0m" if color else r.status
        lines.append(f"  {status}  {r.name.ljust(width)}  {r.detail}")
    failed = sum(r.status == FAIL for r in results)
    warned = sum(r.status == WARN for r in results)
    if failed:
        lines.append(f"\nNATIJA / RESULT: FAIL — {failed} ta xato, {warned} ta ogohlantirish. Yuqoridagi FAIL qatorlarini tuzating.")
    else:
        lines.append(f"\nNATIJA / RESULT: PASS — bridge ishga tayyor ({warned} ta ogohlantirish).")
    return "\n".join(lines)
