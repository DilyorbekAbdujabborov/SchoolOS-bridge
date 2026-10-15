"""Frozen entry point for the bridge service (PyInstaller).

A PyInstaller entry script runs as top-level ``__main__`` with no package
context, so it cannot use the relative imports inside ``schoolos_bridge``.
Importing the package's own ``__main__`` module is fine, though, and keeps the
CLI exactly the same as ``python -m schoolos_bridge``.
"""

import multiprocessing

from schoolos_bridge.__main__ import main

if __name__ == "__main__":
    # Harmless on Linux/macOS; required on Windows so a frozen child process
    # (spawned by a library, not by us today) does not re-run the whole app.
    multiprocessing.freeze_support()
    main()
