
from pathlib import Path

from src.cloudmover.gui import find_proton_drive_cli, validate_proton_destination

def test_first_run_is_icloud_to_proton_only():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Move from iCloud Drive to Proton Drive" in s
    assert "SetupDialog" in s
    assert "default_icloud_path" in s
    assert 'self.destination=new_destination(self.source)' in s

def test_first_run_checks_source_and_proton():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "find_proton_drive_cli()" in s
    assert "Free disk space" in s
    assert "Proton Drive command-line service" in s
    assert "Proton destination" in s
    assert "dest_edit=QLineEdit(self.destination)" in s
    assert "validate_proton_destination(self.destination)" in s

def test_first_run_scans_and_creates_plan():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "initialize_migration(self.source,self.db_path,self.destination,report,cancel_check=self.cancel_event.is_set)" in s
    assert "Nothing transfers until you press Start Migration" in s

def test_existing_migration_skips_wizard():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "if not has_migration(db_path):" in s

def test_first_run_destination_explains_collision_safety():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Existing destinations are refused" in s

def test_proton_destination_validation():
    assert validate_proton_destination("/my-files/CloudMover/run")[0] is True
    assert validate_proton_destination("/my-files")[0] is True
    assert validate_proton_destination("/shared-with-me/CloudMover/run")[0] is False
    assert validate_proton_destination("/my-files/CloudMover/../run")[0] is False
    assert validate_proton_destination("/my-files/CloudMover//run")[0] is False
    assert validate_proton_destination("/my-files/CloudMover/run/")[0] is False

def test_packaged_app_cli_discovery_checks_common_macos_paths_outside_path(monkeypatch):
    checked=[]
    def fake_which(name):
        assert name=="proton-drive"
        return None
    def fake_is_file(self):
        checked.append(str(self))
        return str(self)=="/usr/local/bin/proton-drive"
    def fake_access(path,mode):
        return str(path)=="/usr/local/bin/proton-drive"

    monkeypatch.setattr("src.cloudmover.gui.shutil.which",fake_which)
    monkeypatch.setattr("src.cloudmover.gui.Path.is_file",fake_is_file)
    monkeypatch.setattr("src.cloudmover.gui.os.access",fake_access)

    assert find_proton_drive_cli()=="/usr/local/bin/proton-drive"
    assert "/usr/local/bin/proton-drive" in checked

def test_packaged_app_cli_discovery_still_fails_closed(monkeypatch):
    monkeypatch.setattr("src.cloudmover.gui.shutil.which",lambda name: None)
    monkeypatch.setattr("src.cloudmover.gui.Path.is_file",lambda self: False)
    monkeypatch.setattr("src.cloudmover.gui.os.access",lambda path,mode: False)

    assert find_proton_drive_cli() is None
