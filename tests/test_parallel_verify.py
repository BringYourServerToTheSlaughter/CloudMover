from src.cloudmover.bulk_pipeline import DEFAULT_VERIFY_WORKERS
def test_default_verify_workers_is_bounded():
    assert 1 <= DEFAULT_VERIFY_WORKERS <= 8
