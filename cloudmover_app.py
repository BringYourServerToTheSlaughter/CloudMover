
"""PyInstaller entry point for CloudMover.app.

Normal launch opens the GUI. A private --engine-worker mode runs the migration
engine inside the same bundled executable so the packaged app does not require
an external Python interpreter.
"""
from __future__ import annotations

import sys

def main() -> None:
    if "--engine-worker" in sys.argv:
        sys.argv.remove("--engine-worker")
        from src.cloudmover.start import main as engine_main
        engine_main()
        return

    from src.cloudmover.gui import main as gui_main
    gui_main()

if __name__ == "__main__":
    main()
