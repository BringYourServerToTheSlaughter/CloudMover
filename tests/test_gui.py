
from pathlib import Path

def test_gui_is_qt_app():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "PySide6" in s
    assert "QTreeWidget" in s
    assert "Pause Migration" in s
    assert "Resume Migration" in s

def test_selection_is_locked_while_running():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.tree.setEnabled(False)" in s
    assert "Pause CloudMover before changing the migration selection" in s

def test_progress_is_selection_aware():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "selected_files" in s
    assert "selected_bytes" in s
    assert "/ {s[\"selected_files\"]:,} selected files" in s
