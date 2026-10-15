#!/usr/bin/env python3
"""Build the bridge's standalone executables with PyInstaller.

    cd bridge
    python installer/build.py

Runs identically on Linux, macOS and Windows - but PyInstaller cannot
cross-compile, so run it once on each OS you ship to (CI does exactly that, on
ubuntu/macos/windows runners, and uploads the three artifacts).

The result is written to ``dist/``:

* ``schoolos-bridge``       - the service (run it, or install it as a service);
* ``schoolos-bridge-setup`` - the setup wizard.

Neither needs Python on the target machine.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = HERE / "schoolos-bridge.spec"
DIST = ROOT / "dist"
WORK = ROOT / "build"

BINARIES = ("schoolos-bridge", "schoolos-bridge-setup")


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller topilmadi / not found.\n"
            "  pip install -r requirements-build.txt",
            file=sys.stderr,
        )
        return 2

    if args.clean:
        for folder in (WORK, DIST):
            shutil.rmtree(folder, ignore_errors=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        str(SPEC),
    ]
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    print("\nTayyor / Built:", flush=True)
    for name in BINARIES:
        path = DIST / _exe(name)
        marker = "OK " if path.exists() else "YO'Q / MISSING "
        print(f"  {marker}{path}", flush=True)
    return 0 if all((DIST / _exe(n)).exists() for n in BINARIES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
