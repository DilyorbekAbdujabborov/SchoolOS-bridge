# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: the bridge as one standalone executable.

    cd bridge
    pyinstaller --noconfirm --clean installer/schoolos-bridge.spec

Produces ``dist/schoolos-bridge`` (``.exe`` on Windows): the relay, the
terminal listener and the setup page in one file that needs no Python installed
(double-clicked on Windows it installs itself — see ``schoolos_bridge.winsvc``). The wizard's web assets
are bundled under ``schoolos_bridge/setup/static`` so ``server.STATIC_DIR``
resolves inside the unpacked ``_MEIPASS`` directory at runtime. On Python < 3.11
the ``tomli`` TOML reader is added explicitly: the source imports it only inside
a ``try/except``, which PyInstaller's static analysis cannot see through.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # the bridge/ directory
STATIC = ROOT / "schoolos_bridge" / "setup" / "static"

datas = [(str(STATIC), "schoolos_bridge/setup/static")]
hiddenimports = ["tomli"] if sys.version_info < (3, 11) else []

# Windows has no system IANA time-zone database, so the frozen bridge resolves
# `zoneinfo.ZoneInfo("Asia/Tashkent")` (drivers/hikvision.py) through the pip
# `tzdata` package that requirements-build.txt installs there. Pull its data in
# explicitly instead of trusting hooks-contrib's `hook-zoneinfo` to notice.
# Linux/macOS read the OS database and skip this.
if importlib.util.find_spec("tzdata") is not None:
    from PyInstaller.utils.hooks import collect_data_files

    datas += collect_data_files("tzdata")
    hiddenimports += ["tzdata"]

# Small console tools: drop the GUI and doc/test scaffolding they never use.
excludes = ["tkinter", "pydoc", "doctest", "test"]


def build(entry: str, name: str):
    analysis = Analysis(
        [str(ROOT / entry)],
        pathex=[str(ROOT)],
        binaries=[],
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
        optimize=0,
    )
    pyz = PYZ(analysis.pure)
    # No COLLECT: passing the binaries straight to EXE makes a onefile build.
    return EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,  # UPX is missing on most build machines and breaks macOS signing
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )


build("installer/entry_service.py", "schoolos-bridge")
