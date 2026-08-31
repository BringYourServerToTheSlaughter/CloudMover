
from src.cloudmover.engine import run_engine
def test_engine_imports():
    assert callable(run_engine)
