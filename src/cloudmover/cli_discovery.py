"""Shared discovery for GUI checks and packaged/terminal migration workers.

Finder usually omits third-party installation directories from PATH. Prefer the
caller's PATH, then check executable files in standard macOS install locations.
Do not change PATH or start a shell to discover an executable.
"""
import os
from pathlib import Path
import shutil


def find_proton_drive_cli() -> str | None:
    found=shutil.which("proton-drive")
    if found:
        return found
    for candidate in (
        "/usr/local/bin/proton-drive",
        "/opt/homebrew/bin/proton-drive",
    ):
        path=Path(candidate)
        if path.is_file() and os.access(path,os.X_OK):
            return str(path)
    return None

