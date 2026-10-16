"""Bridge configuration (TOML). Terminal passwords and the cloud token live only
here, sealed by `vault` — the file never holds them in clear."""

import ipaddress
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (Ubuntu 22.04): the same parser, from PyPI
    import tomli as tomllib

VENDORS = ("hikvision",)
DEFAULT_CLOUD_URL = "https://tochna.uz"
DIRECTIONS = ("IN", "OUT", "BOTH")

#: Where installers put the config; `--config` / `SCHOOLOS_BRIDGE_CONFIG` override it.
ENV_CONFIG = "SCHOOLOS_BRIDGE_CONFIG"
#: Pre-installer location (the first hand-written README used it).
LEGACY_POSIX_CONFIG = Path("/etc/schoolos-bridge.toml")


def normalize_cloud_url(url) -> str:
    """The cloud root the client appends `/api/devices/...` to.

    Technicians paste whatever the browser shows: `tochna.uz`, `http://tochna.uz/`
    or the API root `https://tochna.uz/api`. Plain http to a public host is
    upgraded — the server redirects it to https and the redirect turns the
    bridge's POST into a GET (HTTP 405). A trailing `/api` is dropped so the
    client never asks for `/api/api/devices/` (HTTP 404). Blank means the default.
    """
    url = (url or "").strip()
    if not url:
        return DEFAULT_CLOUD_URL
    if "://" not in url:
        url = f"https://{url}"
    scheme, _, rest = url.partition("://")
    host_port, _, path = rest.partition("/")
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    for suffix in ("api/devices", "api"):
        if path == suffix or path.endswith(f"/{suffix}"):
            path = path[: -len(suffix)].strip("/")
            break
    scheme = scheme.lower()
    if scheme == "http" and not _is_local_host(host_port):
        scheme = "https"
    return f"{scheme}://{host_port}" + (f"/{path}" if path else "")


def _is_local_host(host_port: str) -> bool:
    host = host_port.rsplit("@", 1)[-1]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _windows_home() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SchoolOS Bridge"


def _macos_home() -> Path:
    # A per-user location: unlike Linux (which installs under /etc as root),
    # macOS runs the bridge from a normal account, and ~/Library is writable
    # without sudo. A LaunchDaemon that sets SCHOOLOS_BRIDGE_CONFIG can point
    # somewhere else.
    return Path.home() / "Library" / "Application Support" / "SchoolOS Bridge"


def _default_dir() -> Path:
    """The per-OS directory holding config.toml and state.json by default."""
    if sys.platform == "win32":
        return _windows_home()
    if sys.platform == "darwin":
        return _macos_home()
    return Path("/var/lib/schoolos-bridge")


def default_config_path() -> Path:
    if os.environ.get(ENV_CONFIG):
        return Path(os.environ[ENV_CONFIG])
    if sys.platform == "win32":
        return _windows_home() / "config.toml"
    if sys.platform == "darwin":
        return _macos_home() / "config.toml"
    path = Path("/etc/schoolos-bridge/config.toml")
    if not path.exists() and LEGACY_POSIX_CONFIG.exists():
        return LEGACY_POSIX_CONFIG
    return path


def key_dir(config_path) -> Path:
    """Where the bridge's key files (`secret.key`, `ui.key`) live: next to the
    config, except for the legacy `/etc/schoolos-bridge.toml`, whose directory
    is `/etc` itself — those go to the state directory instead."""
    parent = Path(config_path).resolve().parent
    if parent == Path("/etc"):
        return _default_dir()
    return parent


def default_state_path() -> Path:
    return _default_dir() / "state.json"


@dataclass(frozen=True)
class DeviceConfig:
    serial: str
    vendor: str
    ip: str
    username: str
    password: str
    direction: str = "IN"
    #: Human label ("Asosiy kirish") written by the setup wizard; the bridge
    #: itself does not use it — it is for the operator reading the config.
    location_label: str = ""
    #: Terminal model as the wizard's probe read it; reported to the cloud so a
    #: terminal registered from the bridge still shows what it is.
    model: str = ""


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
    from .vault import Vault, VaultError, open_config

    with open(path, "rb") as f:
        data = tomllib.load(f)
    try:
        data = open_config(data, Vault.for_config(path))
    except VaultError as exc:
        raise ValueError(str(exc)) from exc
    if not data.get("token"):
        raise ValueError("config: 'token' is required")
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
        cloud_url=normalize_cloud_url(data.get("cloud_url")),
        token=data["token"],
        poll_seconds=float(data.get("poll_seconds", 2)),
        state_file=Path(data.get("state_file", default_state_path())),
        listen_host=data.get("listen_host", "0.0.0.0"),
        listen_port=port,
        devices=devices,
    )
