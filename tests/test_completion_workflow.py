
from pathlib import Path

def test_complete_state_offers_new_migration_and_report():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'self.resume_btn.setText("New Migration")' in s
    assert 'self.pause_btn.setText("View Report")' in s
    assert "def new_migration" in s
    assert "def view_report" in s

def test_new_migration_reuses_setup_without_deleting_history():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.setup_page=SetupDialog(self.db_path)" in s
    assert "DELETE FROM migrations" not in s
    assert "rm -f" not in s

def test_completion_clears_stale_transition_message():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "✓ Migration completed successfully." in s
    assert "completed migration history" in s

def test_report_contains_core_safety_counts():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Files completed:" in s
    assert "Files verified:" in s
    assert "Files failed:" in s
    assert "Originals remain in iCloud." in s
