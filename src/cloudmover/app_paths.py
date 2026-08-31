
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "CloudMover"

def app_support_dir() -> Path:
    """Per-user runtime directory for the packaged macOS app."""
    override = os.environ.get("CLOUDMOVER_APP_SUPPORT")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME

def default_db_path() -> Path:
    return app_support_dir() / "migration.db"

def default_log_path() -> Path:
    return app_support_dir() / "cloudmover.jsonl"

def ensure_app_support() -> Path:
    root = app_support_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root
