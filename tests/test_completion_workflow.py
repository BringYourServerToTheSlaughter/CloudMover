
from pathlib import Path

def test_complete_state_offers_new_migration_and_report():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'self.resume_btn.hide()' in s
    assert "self.resume_btn.setEnabled(False)" in s
    assert 'self.pause_btn.setText("Done")' in s
    assert 'self.new_btn=QPushButton("New Migration")' in s
    assert "def new_migration" in s
    assert "def view_report" in s

def test_completed_primary_action_cannot_rerun_same_migration():
    s=Path("src/cloudmover/gui.py").read_text()
    block=s[s.index("        def primary_action"):s.index("        def secondary_action")]
    assert "if status==\"COMPLETE\":" in block
    assert "self.resume_migration()" in block
    assert "self.new_migration()" not in block
    assert "This migration is complete. Choose New Migration..." in block

def test_new_migration_reuses_setup_without_deleting_history():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.setup_page=SetupDialog(self.db_path)" in s
    assert "DELETE FROM migrations" not in s
    assert "rm -f" not in s

def test_completion_clears_stale_transition_message():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Migration completed successfully. No further action is required." in s
    assert "Migration completed with {failed_files:,} failed file(s)." in s
    assert "completed migration history" in s

def test_completion_screen_contains_required_summary_and_safety_text():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.completion_title=QLabel()" in s
    assert "Migration completed successfully" in s
    assert "Completed with attention needed" in s
    assert "Source path:" in s
    assert "Proton destination:" in s
    assert "Total selected files:" in s
    assert "Completed files:" in s
    assert "Verified files:" in s
    assert "Failed files:" in s
    assert "Total logical bytes migrated:" in s
    assert "Local iCloud cache released:" in s
    assert "Migration start time:" in s
    assert "Completion time:" in s
    assert "Elapsed duration:" in s
    assert "Source files remain in iCloud." in s
    assert "Verified Proton copies were required before local cache release." in s

def test_report_contains_core_safety_counts():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "successfully copied and verified" in s
    assert "Verified files:" in s
    assert "Failed files:" in s
    assert "Originals remain safely in iCloud" in s
