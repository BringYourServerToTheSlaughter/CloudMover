
from pathlib import Path
from src.cloudmover.start import engine_pid_path

def test_engine_pid_paths_do_not_collide(tmp_path):
    a=engine_pid_path(tmp_path/"migration.db")
    b=engine_pid_path(tmp_path/"onboarding-test.db")
    assert a != b
    assert a.name == "migration.engine.pid"
    assert b.name == "onboarding-test.engine.pid"

def test_gui_uses_database_specific_pid():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'self.db_path.with_name(self.db_path.stem + ".engine.pid")' in s
