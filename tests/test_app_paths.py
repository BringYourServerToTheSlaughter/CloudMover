
from pathlib import Path
from src.cloudmover.app_paths import app_support_dir, default_db_path, default_log_path

def test_default_runtime_paths_are_in_application_support(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLOUDMOVER_APP_SUPPORT", raising=False)
    expected = tmp_path / "Library" / "Application Support" / "CloudMover"
    assert app_support_dir() == expected
    assert default_db_path() == expected / "migration.db"
    assert default_log_path() == expected / "cloudmover.jsonl"

def test_runtime_path_can_be_overridden(monkeypatch, tmp_path):
    target = tmp_path / "cm-data"
    monkeypatch.setenv("CLOUDMOVER_APP_SUPPORT", str(target))
    assert app_support_dir() == target
