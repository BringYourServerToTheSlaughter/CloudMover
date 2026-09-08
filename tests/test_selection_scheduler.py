
from pathlib import Path

def test_bulk_scheduler_honors_selected_flag():
    s=Path("src/cloudmover/bulk_pipeline.py").read_text()
    assert "transfer_state='READY' AND selected=1" in s
    assert "AND selected=1" in s

def test_state_schema_has_selected_flag():
    s=Path("src/cloudmover/state.py").read_text()
    assert "selected INTEGER NOT NULL DEFAULT 1" in s
    assert 'if "selected" not in columns' in s

def test_gui_has_stateful_controls():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'self.resume_btn.hide()' in s
    assert 'self.pause_btn.setText("Back to Selection" if self.reviewing else "Back")' in s
    assert 'setText("Pausing…")' in s
    assert 'setText("Resuming…")' in s
