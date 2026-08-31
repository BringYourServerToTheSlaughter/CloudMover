
from src.cloudmover.bulk_pipeline import DEFAULT_VERIFY_WORKERS
def test_safe_default_single_verifier():
    assert DEFAULT_VERIFY_WORKERS == 1
