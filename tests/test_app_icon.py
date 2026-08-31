
from pathlib import Path

def test_app_icon_assets_exist():
    assert Path("assets/CloudMover.png").exists()
    assert Path("assets/CloudMover.icns").exists()

def test_pyinstaller_uses_cloudmover_icon():
    s=Path("CloudMover.spec").read_text()
    assert 'assets" / "CloudMover.icns"' in s
    assert "icon=None" not in s
