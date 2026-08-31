
from src.cloudmover.release import human_bytes
def test_release_human_bytes():
    assert human_bytes(1000) == "1.00 KB"
