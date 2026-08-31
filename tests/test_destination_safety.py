
import pytest
from src.cloudmover.state import initialize_migration, connect
from src.cloudmover.start import migration_destination

def test_destination_is_read_from_migration_plan(tmp_path):
    source=tmp_path/"src"; source.mkdir()
    (source/"a.txt").write_text("x")
    db=tmp_path/"m.db"
    initialize_migration(source,db,"/my-files")
    assert migration_destination(db)=="/my-files"

def test_missing_destination_is_fatal(tmp_path):
    source=tmp_path/"src"; source.mkdir()
    (source/"a.txt").write_text("x")
    db=tmp_path/"m.db"
    initialize_migration(source,db,None)
    with pytest.raises(RuntimeError,match="no Proton destination"):
        migration_destination(db)

def test_start_has_no_hidden_cloudmover_test_destination():
    from pathlib import Path
    s=Path("src/cloudmover/start.py").read_text()
    assert 'default="/my-files/CloudMover-013-Test"' not in s
    assert "stored_destination=migration_destination(db)" in s
