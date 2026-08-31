
from pathlib import Path
from src.cloudmover import bulk_pipeline
def test_verify_workers_default_bounded():
    assert bulk_pipeline.DEFAULT_VERIFY_WORKERS == 1
