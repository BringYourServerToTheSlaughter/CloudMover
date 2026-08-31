
from pathlib import Path
from unittest.mock import patch
from src.cloudmover.bulk_pipeline import hydrate_selected_parallel
from src.cloudmover.hydrate import ManifestFile

def item(n):
    return ManifestFile("m",f"f{n}",f"/tmp/f{n}",1_000_000,"CLOUD_ONLY","PENDING")

def test_parallel_hydrate_returns_original_order(tmp_path):
    items=[item(1),item(2),item(3)]
    with patch("src.cloudmover.bulk_pipeline.hydrate_one") as h:
        out,elapsed=hydrate_selected_parallel(
            tmp_path/"x.db",items,timeout=1,min_bytes=1,max_bytes=2_000_000,workers=3
        )
    assert [x.relative_path for x in out]==["f1","f2","f3"]
    assert h.call_count==3
    assert elapsed>=0
