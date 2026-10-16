"""Kept for old scripts: ``python -m schoolos_bridge.setup`` now runs the one
bridge program (relay + setup page) and opens the setup page.

Use ``python -m schoolos_bridge`` (or the ``schoolos-bridge`` executable)."""

import sys

from ..__main__ import main

if __name__ == "__main__":
    main(["run", *[a for a in sys.argv[1:] if a not in ("--no-auth",)]])
