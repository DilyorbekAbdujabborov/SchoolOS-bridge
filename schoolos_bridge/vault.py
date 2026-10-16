"""Secrets at rest: the cloud token and terminal passwords are never written to
config.toml in clear.

Each secret is stored as ``enc:v1:<Fernet token>``. The Fernet key lives in
``secret.key`` next to the config:

* **Windows** — the key itself is sealed with DPAPI (machine scope), so a copied
  config + key file is useless on any other PC; the data folder's ACL (set by
  ``schoolos-bridge install``) keeps it to Administrators and SYSTEM.
* **Linux / macOS** — the key file is owner-only (``0600``), owned like the
  config, so the service user can read it and nobody else can.

A plain value (an older config, or one typed by hand) still loads; the bridge
re-saves it encrypted the next time it starts (`migrate`).
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
KEY_FILE = "secret.key"
_DPAPI_MARK = b"dpapi:"

WINDOWS = sys.platform == "win32"


class VaultError(Exception):
    """A secret could not be sealed or opened (missing or foreign key)."""


# -- protected key files -------------------------------------------------------
def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData, machine scope, no UI."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    ui_forbidden, local_machine = 0x1, 0x4
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source), None, None, None, None, ui_forbidden | local_machine, ctypes.byref(result)
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, ui_forbidden, ctypes.byref(result)
        )
    if not ok:
        raise VaultError(f"DPAPI: {ctypes.WinError()}")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def read_protected(path: Path) -> bytes | None:
    """The bytes a `write_protected` call stored, or None when the file is absent."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VaultError(f"{path}: o'qib bo'lmadi ({exc})") from exc
    if raw.startswith(_DPAPI_MARK):
        if not WINDOWS:
            raise VaultError(f"{path}: bu kalit Windows'da yaratilgan")
        return _dpapi(base64.b64decode(raw[len(_DPAPI_MARK):]), protect=False)
    return raw.strip()


def write_protected(path: Path, data: bytes, owner_of: Path | None = None) -> None:
    """Store `data` so only this machine's service/admins can read it back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _DPAPI_MARK + base64.b64encode(_dpapi(data, protect=True)) if WINDOWS else data
    tmp = path.with_name(path.name + ".tmp")
    # Create owner-only from the start: no window where the key is world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    if not WINDOWS and owner_of is not None:
        try:
            stat = owner_of.stat()
            os.chown(tmp, stat.st_uid, stat.st_gid)
        except OSError:
            pass
    os.replace(tmp, path)


# -- the vault -----------------------------------------------------------------
class Vault:
    def __init__(self, key_path: Path, owner_of: Path | None = None):
        self.key_path = Path(key_path)
        #: The key file takes this file's owner (the service user on Linux).
        self.owner_of = owner_of
        self._fernet: Fernet | None = None

    @classmethod
    def for_config(cls, config_path) -> Vault:
        from .config import key_dir

        config = Path(config_path).resolve()
        return cls(key_dir(config) / KEY_FILE, owner_of=config if config.exists() else None)

    def _cipher(self, create: bool) -> Fernet | None:
        if self._fernet is None:
            key = read_protected(self.key_path)
            if key is None:
                if not create:
                    return None
                key = Fernet.generate_key()
                write_protected(self.key_path, key, owner_of=self.owner_of)
            try:
                self._fernet = Fernet(key)
            except ValueError as exc:
                raise VaultError(f"{self.key_path}: kalit buzilgan") from exc
        return self._fernet

    @staticmethod
    def is_sealed(value) -> bool:
        return isinstance(value, str) and value.startswith(PREFIX)

    def seal(self, value: str) -> str:
        if not value or self.is_sealed(value):
            return value
        token = self._cipher(create=True).encrypt(value.encode("utf-8"))
        return PREFIX + token.decode("ascii")

    def open(self, value: str) -> str:
        if not self.is_sealed(value):
            return value or ""
        cipher = self._cipher(create=False)
        if cipher is None:
            raise VaultError(
                f"Shifr kaliti topilmadi ({self.key_path}). Parol va tokenni sozlash sahifasida qayta kiriting."
            )
        try:
            return cipher.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise VaultError(
                f"Shifrni ochib bo'lmadi ({self.key_path} boshqa kalit). Parol va tokenni qayta kiriting."
            ) from exc


# -- config dict helpers ---------------------------------------------------------
def _walk(data: dict, fn) -> dict:
    """A copy of a raw config dict with `fn` applied to every secret field."""
    out = dict(data)
    if out.get("token"):
        out["token"] = fn(out["token"])
    devices = []
    for device in out.get("devices", []) or []:
        device = dict(device)
        if device.get("password"):
            device["password"] = fn(device["password"])
        devices.append(device)
    if "devices" in out:
        out["devices"] = devices
    return out


def open_config(data: dict, vault: Vault) -> dict:
    return _walk(data, vault.open)


def seal_config(data: dict, vault: Vault) -> dict:
    return _walk(data, vault.seal)


def has_plaintext(data: dict) -> bool:
    found = []
    _walk(data, lambda v: found.append(not Vault.is_sealed(v)) or v)
    return any(found)


def self_test() -> str:
    """Seal and open a value with a throwaway key (proves DPAPI/Fernet work in a
    frozen build). Returns a short description of what was exercised."""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        vault = Vault(Path(folder) / KEY_FILE)
        sealed = vault.seal("self-test")
        if not vault.is_sealed(sealed) or Vault(vault.key_path).open(sealed) != "self-test":
            raise VaultError("round-trip failed")
    return "DPAPI+Fernet" if WINDOWS else "Fernet"
