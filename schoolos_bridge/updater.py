"""Self-update: fetch a newer signed build, verify it, swap it in, restart.

Trust model: the cloud only *relays* the release description; the bridge runs as
SYSTEM, so it accepts a build only if an Ed25519 signature made with the
release key (private half lives in CI only) verifies against PUBLIC_KEY below.
The signature covers version, platform and SHA-256, so the server cannot swap
the file, relabel an old build as new, or push a build to another platform.

Only a frozen (PyInstaller) build updates itself. The new binary must pass
`--self-test` before it replaces the running one; the old one stays as `.bak`.
After a swap the process exits with RESTART_EXIT_CODE and the service manager
(systemd, launchd, the Windows scheduled task) starts the new build.
"""

import base64
import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__

log = logging.getLogger("schoolos_bridge")

PUBLIC_KEY = "/LZGbAZYiqBFTrOcYzbTtQT2mAKMVsqU+Ihk9PauBWY="
CHECK_EVERY = 6 * 3600
RESTART_EXIT_CODE = 75
MAX_BINARY_BYTES = 200 * 1024 * 1024


class UpdateError(Exception):
    pass


def platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def parse_version(text: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(\.\d+){0,3}", text or ""):
        raise UpdateError(f"bad version {text!r}")
    return tuple(int(part) for part in text.split("."))


def signed_message(version: str, platform: str, sha256: str) -> bytes:
    return f"schoolos-bridge\n{version}\n{platform}\n{sha256}".encode()


def verify_release(release: dict, platform: str, public_key: str = PUBLIC_KEY) -> None:
    """Raise UpdateError unless `release` is signed by the release key."""
    try:
        version, sha256, signature = release["version"], release["sha256"], release["signature"]
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(
            base64.b64decode(signature), signed_message(version, platform, sha256)
        )
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise UpdateError("release signature is invalid") from exc


def newer_release(release: dict | None, platform: str, current: str = __version__):
    """The verified release if it is strictly newer than `current`, else None."""
    if not release:
        return None
    verify_release(release, platform)
    if parse_version(release["version"]) <= parse_version(current):
        return None  # never downgrade, never reinstall the same build
    if not str(release.get("url", "")).startswith("https://"):
        raise UpdateError("release url must be https")
    return release


def download(url: str, dest: Path, sha256: str, session=None) -> None:
    session = session or requests.Session()
    digest = hashlib.sha256()
    size = 0
    try:
        with session.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with open(dest, "wb") as out:
                for chunk in response.iter_content(1 << 20):
                    size += len(chunk)
                    if size > MAX_BINARY_BYTES:
                        raise UpdateError("download too large")
                    digest.update(chunk)
                    out.write(chunk)
    except (requests.RequestException, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {exc}") from exc
    if digest.hexdigest() != sha256.lower():
        dest.unlink(missing_ok=True)
        raise UpdateError("downloaded file does not match its sha256")


def install(new_path: Path, current_exe: Path, version: str) -> None:
    """Self-test `new_path`, then swap it over `current_exe` (keeping `.bak`)."""
    os.chmod(new_path, 0o755)
    try:
        result = subprocess.run(
            [str(new_path), "--self-test"], capture_output=True, text=True, timeout=90
        )
        shown = subprocess.run(
            [str(new_path), "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        new_path.unlink(missing_ok=True)
        raise UpdateError(f"new build did not start: {exc}") from exc
    if result.returncode != 0 or version not in shown.stdout:
        new_path.unlink(missing_ok=True)
        raise UpdateError(f"new build failed its self-test: {(result.stderr or result.stdout).strip()[:300]}")
    backup = current_exe.with_name(current_exe.name + ".bak")
    # Renaming a running executable is allowed everywhere (Windows included);
    # overwriting it is not.
    os.replace(current_exe, backup)
    try:
        os.replace(new_path, current_exe)
    except OSError:
        os.replace(backup, current_exe)
        raise


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def maybe_update(cloud, session=None) -> bool:
    """Check the cloud for a newer signed build and install it. True = installed,
    the caller should exit so the service manager restarts the new build."""
    if not is_frozen():
        return False
    platform = platform_name()
    try:
        release = newer_release(cloud.fetch_update(platform), platform)
        if release is None:
            return False
        current_exe = Path(sys.executable)
        new_path = current_exe.with_name(current_exe.name + ".new")
        log.warning("updating bridge %s -> %s", __version__, release["version"])
        download(release["url"], new_path, release["sha256"], session)
        install(new_path, current_exe, release["version"])
    except UpdateError as exc:
        log.warning("bridge update skipped: %s", exc)
        return False
    except Exception as exc:  # network/cloud trouble must never stop the relay
        log.warning("bridge update check failed: %s", exc)
        return False
    log.warning("bridge updated to %s; restarting", release["version"])
    return True
