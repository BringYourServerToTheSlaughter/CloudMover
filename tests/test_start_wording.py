
from pathlib import Path
def test_first_start_and_resume_wording_both_exist():
    s=Path("src/cloudmover/gui.py").read_text()
    assert '"Start Migration"' in s
    assert '"Starting migration…"' in s
    assert '"Resume Migration"' in s
    assert '"Resuming…"' in s
    assert "migration_has_started" in s
