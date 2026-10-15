# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: the bridge as two standalone executables.

    cd bridge
    pyinstaller --noconfirm --clean installer/schoolos-bridge.spec

Produces, in ``dist/`` (``.exe`` on Windows):

* ``schoolos-bridge``        - the long-running service;
* ``schoolos-bridge-setup``  - the LAN setup wizard.

Each is a single file that needs no Python installed. The wizard's web assets
are bundled under ``schoolos_bridge/setup/static`` so ``server.STATIC_DIR``
resolves inside the unpacked ``_MEIPASS`` directory at runtime. On Python < 3.11
the ``tomli`` TOML reader is added explicitly: the source imports it only inside
a ``try/except``, which PyInstaller's static analysis cannot see through.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # the bridge/ directory
STATIC = ROOT / "schoolos_bridge" / "setup" / "static"

datas = [(str(STATIC), "schoolos_bridge/setup/static")]
hiddenimports = ["tomli"] if sys.version_info < (3, 11) else []

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
build("installer/entry_setup.py", "schoolos-bridge-setup")
