"""Read and write the bridge config file for the setup UI.

The file holds the terminal admin passwords (`chmod 600`), so writes are atomic
and re-tighten permissions. Reading returns the raw dict; the UI masks secrets
before anything is sent to the browser.
"""

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (Ubuntu 22.04)
    import tomli as tomllib

from .tomlwrite import DEVICE_KEYS, TOP_KEYS, dumps


def read_config(path) -> dict:
    """The config as a dict, or `{}` when the file does not exist yet."""
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    with open(file, "rb") as handle:
        return tomllib.load(handle)


def write_config(path, data: dict) -> None:
    """Write `data` with owner-only permissions, keeping the file's existing
    owner and working whether or not the directory is writable.

    Two things matter for `/etc/schoolos-bridge.toml`, which the service reads
    as the `schoolos-bridge` user: the file must stay owned by that user, and an
    installer may hand a technician an account that can write the file but not
    the directory. So we replace atomically when we can (copying the old owner
    onto the new inode) and rewrite in place when we cannot."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(data)

    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    owner = _existing_owner(file)
    if owner is not None:
        try:
            os.chown(tmp, *owner)
        except OSError:
            pass
    try:
        os.replace(tmp, file)
    except OSError:
        # Directory not writable (e.g. /etc as the bridge user): keep the inode
        # and its ownership, and write the content through it.
        _write_in_place(file, text)
        try:
            tmp.unlink()
        except OSError:
            pass


def _existing_owner(file: Path):
    try:
        stat = file.stat()
    except OSError:
        return None
    return (stat.st_uid, stat.st_gid)


def _write_in_place(file: Path, text: str) -> None:
    with open(file, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(file, 0o600)


def clean_device(raw: dict) -> dict:
    """A device entry with only known fields, in a stable order."""
    return {key: raw[key] for key in DEVICE_KEYS if raw.get(key) not in (None, "")}


def public_config(path) -> dict:
    """The config with secrets replaced by booleans, safe to send to the browser."""
    data = read_config(path)
    devices = []
    for device in data.get("devices", []):
        row = {key: device.get(key, "") for key in DEVICE_KEYS if key != "password"}
        row["password_set"] = bool(device.get("password"))
        devices.append(row)
    return {
        "config_path": str(path) if path else "",
        "config_exists": bool(data),
        "cloud_url": data.get("cloud_url", ""),
        "token_set": bool(data.get("token")),
        "poll_seconds": data.get("poll_seconds", 2),
        "state_file": str(data.get("state_file", "")),
        "listen_host": data.get("listen_host", "0.0.0.0"),
        "listen_port": data.get("listen_port", 8099),
        "devices": devices,
    }


def merge_save(existing: dict, payload: dict) -> dict:
    """Build the config dict to write from the setup request.

    A blank/missing `token` keeps the one already on disk — the UI never shows
    the token, so it must not require the technician to retype it. Same for a
    device password left blank (the terminal is already configured).
    """
    cloud_url = (payload.get("cloud_url") or "").strip()
    token = (payload.get("token") or "").strip() or existing.get("token", "")

    devices = []
    old_by_serial = {d.get("serial"): d for d in existing.get("devices", [])}
    for raw in payload.get("devices", []):
        clean = clean_device(raw)
        if not clean.get("password"):
            previous = old_by_serial.get(clean.get("serial"), {})
            if previous.get("password"):
                clean["password"] = previous["password"]
        devices.append(clean)

    result = {
        "cloud_url": cloud_url,
        "token": token,
        "poll_seconds": float(payload.get("poll_seconds") or existing.get("poll_seconds") or 2),
        "listen_host": payload.get("listen_host") or existing.get("listen_host") or "0.0.0.0",
        "listen_port": int(payload.get("listen_port") or existing.get("listen_port") or 8099),
        "devices": devices,
    }
    # An empty state_file must be omitted, not written as "": the bridge would
    # otherwise treat Path("") — the current directory — as the state file.
    state_file = payload.get("state_file") or existing.get("state_file") or ""
    if state_file:
        result["state_file"] = state_file
    return result
