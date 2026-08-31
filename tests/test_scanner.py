from pathlib import Path

from src.cloudmover.scanner import choose_strategy, scan


def test_scan_counts(tmp_path: Path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "folder" / "b.bin").write_bytes(b"123456")

    result = scan(tmp_path)

    assert result.files == 2
    assert result.directories == 2
    assert result.logical_bytes == 11


def test_full_local_staging():
    strategy, staging, reserve = choose_strategy(10_000, 100_000_000_000)
    assert strategy == "FULL_LOCAL_STAGING"
    assert staging == 10_000
    assert reserve > 0


def test_smart_staging():
    strategy, staging, reserve = choose_strategy(100_000_000_000, 30_000_000_000)
    assert strategy == "SMART_STAGING"
    assert staging >= 0
    assert reserve > 0
