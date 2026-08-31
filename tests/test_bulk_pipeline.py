
from pathlib import Path
from src.cloudmover.bulk_pipeline import stage_batch
class X:
    def __init__(self,source_path,relative_path):
        self.source_path=source_path; self.relative_path=relative_path
def test_stage_preserves_tree(tmp_path):
    src=tmp_path/"src.txt"; src.write_bytes(b"hello")
    stage=tmp_path/"stage"; stage.mkdir()
    tops=stage_batch([X(str(src),"A/B/file.txt")],stage)
    assert (stage/"A/B/file.txt").read_bytes()==b"hello"
    assert tops == [stage/"A"]

def test_bulk_pipeline_defines_payload_bytes_for_activity():
    source = Path("src/cloudmover/bulk_pipeline.py").read_text()
    assert "payload_bytes=sum(i.logical_bytes for i in items)" in source
    assert "payload_bytes/1024**2" in source
