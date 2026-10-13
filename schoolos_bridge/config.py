"""Bridge configuration (TOML). Terminal passwords live only here."""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (Ubuntu 22.04): the same parser, from PyPI
    import tomli as tomllib

VENDORS = ("hikvision",)
DIRECTIONS = ("IN", "OUT", "BOTH")

#: Where installers put the config; `--config` / `SCHOOLOS_BRIDGE_CONFIG` override it.
ENV_CONFIG = "SCHOOLOS_BRIDGE_CONFIG"
#: Pre-installer location (the first hand-written README used it).
LEGACY_POSIX_CONFIG = Path("/etc/schoolos-bridge.toml")


def _windows_home() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SchoolOS Bridge"


def default_config_path() -> Path:
    if os.environ.get(ENV_CONFIG):
        return Path(os.environ[ENV_CONFIG])
    if sys.platform == "win32":
        return _windows_home() / "config.toml"
    path = Path("/etc/schoolos-bridge/config.toml")
    if not path.exists() and LEGACY_POSIX_CONFIG.exists():
        return LEGACY_POSIX_CONFIG
    return path


def default_state_path() -> Path:
    if sys.platform == "win32":
        return _windows_home() / "state.json"
    return Path("/var/lib/schoolos-bridge/state.json")


@dataclass(frozen=True)
class DeviceConfig:
    serial: str
    vendor: str
    ip: str
    username: str
    password: str
    direction: str = "IN"


@dataclass(frozen=True)
class Config:
    cloud_url: str
    token: str
    poll_seconds: float = 2
    state_file: Path = Path("/var/lib/schoolos-bridge/state.json")
    listen_host: str = "0.0.0.0"
    listen_port: int = 8099
    devices: list[DeviceConfig] = field(default_factory=list)

    def secrets(self) -> list[str]:
        """Every value that must never reach a log line."""
        return [s for s in [self.token, *(d.password for d in self.devices)] if s]


def load_config(path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for key in ("cloud_url", "token"):
        if not data.get(key):
            raise ValueError(f"config: '{key}' is required")
    devices = []
    for raw in data.get("devices", []):
        try:
            device = DeviceConfig(**raw)
        except TypeError as exc:
            raise ValueError(f"config: bad [[devices]] entry ({exc})") from exc
        if device.vendor not in VENDORS:
            raise ValueError(f"config: unknown vendor '{device.vendor}' (known: {', '.join(VENDORS)})")
        if device.direction not in DIRECTIONS:
            raise ValueError(f"config: direction must be one of {DIRECTIONS}")
        devices.append(device)
    port = int(data.get("listen_port", 8099))
    if not 1 <= port <= 65535:
        raise ValueError("config: listen_port must be 1..65535")
    return Config(
        cloud_url=data["cloud_url"],
        token=data["token"],
        poll_seconds=float(data.get("poll_seconds", 2)),
        state_file=Path(data.get("state_file", default_state_path())),
        listen_host=data.get("listen_host", "0.0.0.0"),
        listen_port=port,
        devices=devices,
    )
