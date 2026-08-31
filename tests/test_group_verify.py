
from pathlib import Path
from src.cloudmover.providers import proton

def test_verify_many_one_listing(tmp_path, monkeypatch):
    a=tmp_path/"a.txt"; b=tmp_path/"b.txt"
    a.write_bytes(b"a"); b.write_bytes(b"bb")
    calls={"n":0}
    def fake_list(_):
        calls["n"]+=1
        return [
            proton.RemoteFile("a.txt",1,proton.sha1_file(a)),
            proton.RemoteFile("b.txt",2,proton.sha1_file(b)),
        ]
    monkeypatch.setattr(proton,"list_files",fake_list)
    proton.verify_many_in_parent([a,b],"/x")
    assert calls["n"]==1
