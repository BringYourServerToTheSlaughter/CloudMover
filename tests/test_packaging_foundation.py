
from pathlib import Path

def test_pyinstaller_entrypoint_exists():
    assert Path("cloudmover_app.py").exists()
    assert Path("CloudMover.spec").exists()
    assert Path("scripts/build_macos_app.sh").exists()

def test_gui_uses_application_support_by_default():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "default_db_path()" in s
    assert 'ap.add_argument("--db",default=None)' in s

def test_start_uses_application_support_defaults():
    s=Path("src/cloudmover/start.py").read_text()
    assert "default_db_path()" in s
    assert "default_log_path()" in s

def test_spec_is_gui_app():
    s=Path("CloudMover.spec").read_text()
    assert 'console=False' in s
    assert 'name="CloudMover.app"' in s
    assert 'bundle_identifier="com.cloudmover.app"' in s
