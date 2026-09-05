
from pathlib import Path
import pytest
from src.cloudmover.state import connect, initialize_migration
from src.cloudmover.bulk_pipeline import run_bulk, stage_batch
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

def test_bulk_fresh_destination_marks_verified_complete(tmp_path, monkeypatch):
    src=tmp_path/"src"; src.mkdir()
    (src/"a.txt").write_bytes(b"abc")
    db=tmp_path/"m.db"
    initialize_migration(src,db,"/my-files/CloudMover/run")
    c=connect(db)
    mid=c.execute("SELECT id FROM migrations LIMIT 1").fetchone()["id"]
    c.execute("UPDATE files SET transfer_state='READY',hydration_state='LOCAL',selected=1")
    c.commit(); c.close()

    calls={"uploaded":False,"checked":[]}
    from src.cloudmover.bulk_pipeline import proton
    monkeypatch.setattr(proton,"ensure_folder",lambda parent,name: parent.rstrip("/")+"/"+name)
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda path: calls["checked"].append(path))
    monkeypatch.setattr(proton,"safe_upload_promote",lambda local,parent,name: calls.__setitem__("uploaded",True))
    monkeypatch.setattr(proton,"verify_tree",lambda local,remote,rels: None)

    assert run_bulk(db,count=1,destination="/my-files/CloudMover/run")==1
    assert calls["uploaded"] is True
    assert calls["checked"]==["/my-files/CloudMover/run/a.txt"]
    c=connect(db)
    row=c.execute("SELECT transfer_state,verified,destination_path FROM files").fetchone()
    c.close()
    assert row["transfer_state"]=="COMPLETE"
    assert row["verified"]==1
    assert row["destination_path"]=="/my-files/CloudMover/run/a.txt"

def test_bulk_collision_refuses_before_upload_or_complete(tmp_path, monkeypatch):
    src=tmp_path/"src"; src.mkdir()
    (src/"a.txt").write_bytes(b"abc")
    db=tmp_path/"m.db"
    initialize_migration(src,db,"/my-files/CloudMover/run")
    c=connect(db)
    c.execute("UPDATE files SET transfer_state='READY',hydration_state='LOCAL',selected=1")
    c.commit(); c.close()

    from src.cloudmover.bulk_pipeline import proton
    monkeypatch.setattr(proton,"ensure_folder",lambda parent,name: parent.rstrip("/")+"/"+name)
    def collision(_):
        raise RuntimeError("remote destination already contains this path")
    monkeypatch.setattr(proton,"assert_no_remote_file",collision)
    monkeypatch.setattr(proton,"safe_upload_promote",lambda local,parent,name: pytest.fail("upload should not start"))

    with pytest.raises(RuntimeError,match="already contains this path"):
        run_bulk(db,count=1,destination="/my-files/CloudMover/run")
    c=connect(db)
    row=c.execute("SELECT transfer_state,verified,destination_path FROM files").fetchone()
    c.close()
    assert row["transfer_state"]=="READY"
    assert row["verified"]==0
    assert row["destination_path"] is None

@pytest.mark.parametrize(
    "error",
    [
        "remote SHA-1 digest mismatch",
        "remote claimed size mismatch",
        "remote SHA-1 digest unavailable",
    ],
)
def test_bulk_does_not_fallback_from_tree_integrity_failure(tmp_path, monkeypatch, error):
    src=tmp_path/"src"; src.mkdir()
    (src/"a.txt").write_bytes(b"abc")
    db=tmp_path/"m.db"
    initialize_migration(src,db,"/my-files/CloudMover/run")
    c=connect(db)
    c.execute("UPDATE files SET transfer_state='READY',hydration_state='LOCAL',selected=1")
    c.commit(); c.close()

    from src.cloudmover.bulk_pipeline import proton
    monkeypatch.setattr(proton,"ensure_folder",lambda parent,name: parent.rstrip("/")+"/"+name)
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda path: None)
    monkeypatch.setattr(proton,"safe_upload_promote",lambda local,parent,name: None)
    monkeypatch.setattr(proton,"verify_tree",lambda local,remote,rels: (_ for _ in ()).throw(RuntimeError(error)))
    monkeypatch.setattr(proton,"verify_many_in_parent",lambda locals_,parent: pytest.fail("digest failures must not fall back"))

    with pytest.raises(RuntimeError,match=error):
        run_bulk(db,count=1,destination="/my-files/CloudMover/run")
    c=connect(db)
    row=c.execute("SELECT transfer_state,verified,destination_path FROM files").fetchone()
    c.close()
    assert row["transfer_state"]=="VERIFYING"
    assert row["verified"]==0
    assert row["destination_path"] is None

def test_bulk_falls_back_when_tree_listing_is_incomplete(tmp_path, monkeypatch):
    src=tmp_path/"src"; src.mkdir()
    (src/"a.txt").write_bytes(b"abc")
    db=tmp_path/"m.db"
    initialize_migration(src,db,"/my-files/CloudMover/run")
    c=connect(db)
    c.execute("UPDATE files SET transfer_state='READY',hydration_state='LOCAL',selected=1")
    c.commit(); c.close()

    from src.cloudmover.bulk_pipeline import proton
    calls={"fallback":0}
    monkeypatch.setattr(proton,"ensure_folder",lambda parent,name: parent.rstrip("/")+"/"+name)
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda path: None)
    monkeypatch.setattr(proton,"safe_upload_promote",lambda local,parent,name: None)
    monkeypatch.setattr(proton,"verify_tree",lambda local,remote,rels: (_ for _ in ()).throw(proton.TreeVerificationUnavailable("missing")))
    def fallback(locals_,parent):
        calls["fallback"]+=1
        return {}
    monkeypatch.setattr(proton,"verify_many_in_parent",fallback)

    assert run_bulk(db,count=1,destination="/my-files/CloudMover/run")==1
    assert calls["fallback"]==1
