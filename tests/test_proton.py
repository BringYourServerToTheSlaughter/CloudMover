
from pathlib import Path
from src.cloudmover.providers.proton import sha1_file
def test_sha1(tmp_path):
    p=tmp_path/"x";p.write_bytes(b"abc")
    assert sha1_file(p)=="a9993e364706816aba3e25717850c26c9cd0d89d"
