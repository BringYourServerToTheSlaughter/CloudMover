
from src.cloudmover.start import engine_pid_path

def test_engine_pid_is_database_specific(tmp_path):
    assert engine_pid_path(tmp_path/"migration.db") == tmp_path/"migration.engine.pid"
    assert engine_pid_path(tmp_path/"other.db") == tmp_path/"other.engine.pid"
