
from pathlib import Path

def test_first_run_is_icloud_to_proton_only():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Move from iCloud Drive to Proton Drive" in s
    assert "SetupDialog" in s
    assert "default_icloud_path" in s
    assert 'self.destination="/my-files"' in s

def test_first_run_checks_source_and_proton():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'shutil.which("proton-drive")' in s
    assert "Free disk space" in s
    assert "Proton Drive command-line service" in s

def test_first_run_scans_and_creates_plan():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "initialize_migration(self.source,self.db_path,self.destination)" in s
    assert "Nothing transfers until you press Start Migration" in s

def test_existing_migration_skips_wizard():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "if not has_migration(db_path):" in s
