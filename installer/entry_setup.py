"""Frozen entry point for the local setup wizard (PyInstaller).

See ``entry_service.py`` for why this wrapper exists: a PyInstaller entry script
has no package context, so it imports the package's ``__main__`` instead of
being inside it.
"""

import multiprocessing

from schoolos_bridge.setup.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
