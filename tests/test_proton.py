
import pytest
from pathlib import Path
from src.cloudmover.providers import proton
from src.cloudmover.providers.proton import RemoteFile
def test_sha1(tmp_path):
    p=tmp_path/"x";p.write_bytes(b"abc")
    assert proton.sha1_file(p)=="a9993e364706816aba3e25717850c26c9cd0d89d"

def test_verify_requires_remote_digest(tmp_path, monkeypatch):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("x.txt",3,None)])
    with pytest.raises(RuntimeError,match="digest unavailable"):
        proton.verify(p,"/my-files/CloudMover/run")

def test_verify_rejects_wrong_digest(tmp_path, monkeypatch):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("x.txt",3,"bad")])
    with pytest.raises(RuntimeError,match="digest mismatch"):
        proton.verify(p,"/my-files/CloudMover/run")

def test_verify_accepts_matching_digest(tmp_path, monkeypatch):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("x.txt",3,proton.sha1_file(p))])
    assert proton.verify(p,"/my-files/CloudMover/run").name=="x.txt"

def test_preflight_refuses_existing_remote_file_without_digest(monkeypatch):
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("x.txt",3,None)])
    with pytest.raises(RuntimeError,match="already contains this path"):
        proton.assert_no_remote_file("/my-files/CloudMover/run/x.txt")

def test_preflight_refuses_existing_remote_file_with_wrong_digest(monkeypatch):
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("x.txt",3,"bad")])
    with pytest.raises(RuntimeError,match="already contains this path"):
        proton.assert_no_remote_file("/my-files/CloudMover/run/x.txt")

def test_preflight_refuses_existing_remote_file_with_same_digest(tmp_path, monkeypatch):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("x.txt",3,proton.sha1_file(p))])
    with pytest.raises(RuntimeError,match="already contains this path"):
        proton.assert_no_remote_file("/my-files/CloudMover/run/x.txt")

def test_preflight_allows_fresh_remote_path(monkeypatch):
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("other.txt",3,"abc")])
    proton.assert_no_remote_file("/my-files/CloudMover/run/x.txt")

def test_verify_tree_requires_digest(tmp_path, monkeypatch):
    root=tmp_path/"root"; root.mkdir()
    p=root/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton,"list_tree",lambda _: {"x.txt": RemoteFile("x.txt",3,None)})
    with pytest.raises(RuntimeError,match="digest unavailable"):
        proton.verify_tree(root,"/my-files/CloudMover/run",["x.txt"])

def test_upload_uses_rename_conflict_strategy_not_skip(tmp_path, monkeypatch):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    seen=[]
    monkeypatch.setattr(proton.shutil,"which",lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(
        proton,
        "info_file",
        lambda path: RemoteFile(path.rsplit("/",1)[1],3,proton.sha1_file(p),"node-1"),
    )
    def fake_run(cmd, timeout=None):
        seen.append(cmd)
        return "Uploaded x.txt","",0
    monkeypatch.setattr(proton,"_run_cli_process",fake_run)
    proton.upload(p,"/my-files/CloudMover/run")
    upload_cmd=next(cmd for cmd in seen if cmd[1:3]==["filesystem","upload"])
    assert "-f" in upload_cmd
    assert "rename" in upload_cmd
    assert "skip" not in upload_cmd

def test_upload_many_uses_rename_conflict_strategy_not_skip(tmp_path, monkeypatch):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    seen=[]
    monkeypatch.setattr(proton.shutil,"which",lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(
        proton,
        "info_file",
        lambda path: RemoteFile(path.rsplit("/",1)[1],3,proton.sha1_file(p),"node-1"),
    )
    def fake_run(cmd, timeout=None):
        seen.append(cmd)
        return "Uploaded x.txt","",0
    monkeypatch.setattr(proton,"_run_cli_process",fake_run)
    proton.upload_many([p],"/my-files/CloudMover/run")
    upload_cmd=next(cmd for cmd in seen if cmd[1:3]==["filesystem","upload"])
    assert "-f" in upload_cmd
    assert "rename" in upload_cmd
    assert "skip" not in upload_cmd

def test_upload_fails_closed_when_temp_path_missing_after_silent_rename(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton.shutil,"which",lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(proton,"info_file",lambda path: (_ for _ in ()).throw(RuntimeError("not found")))
    monkeypatch.setattr(proton,"_run_cli_process",lambda cmd, timeout=None: ("","",0))
    with pytest.raises(RuntimeError,match="not found"):
        proton.upload(p,"/my-files/CloudMover/run")

def test_upload_fails_closed_when_temp_path_missing_after_silent_skip(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton.shutil,"which",lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(proton,"info_file",lambda path: (_ for _ in ()).throw(RuntimeError("not found")))
    monkeypatch.setattr(proton,"_run_cli_process",lambda cmd, timeout=None: ("","",0))
    with pytest.raises(RuntimeError,match="not found"):
        proton.upload(p,"/my-files/CloudMover/run")

def test_safe_upload_refuses_pre_existing_final_path_even_with_matching_digest(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton,"list_files",lambda _: [RemoteFile("x.txt",3,proton.sha1_file(p),"old-node")])
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: pytest.fail("upload should not start"))
    with pytest.raises(RuntimeError,match="already contains this path"):
        proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")

def test_safe_upload_requires_temp_digest(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: "")
    monkeypatch.setattr(proton,"info_file",lambda path: RemoteFile(path.rsplit("/",1)[1],3,None,"node-1"))
    with pytest.raises(RuntimeError,match="digest unavailable"):
        proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")

def test_safe_upload_requires_node_identity(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: "")
    monkeypatch.setattr(proton,"info_file",lambda path: RemoteFile(path.rsplit("/",1)[1],3,proton.sha1_file(p),None))
    with pytest.raises(RuntimeError,match="node identity unavailable"):
        proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")

def test_info_file_only_uses_top_level_uid_for_node_identity(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    payload = {
        "id": "generic-id",
        "nodeId": "generic-node-id",
        "parentUid": "parent-1",
        "name": {"ok": True, "value": "x.txt"},
        "activeRevision": {
            "uid": "revision-uid",
            "claimedSize": 3,
            "claimedDigests": {"sha1": proton.sha1_file(p)},
        },
    }
    monkeypatch.setattr(proton,"run",lambda args, json_output=False, timeout=120.0: proton.json.dumps(payload))
    with pytest.raises(RuntimeError,match="node identity unavailable"):
        proton.verify_remote_exact(p,"/my-files/CloudMover/run/x.txt","x.txt")

def test_safe_upload_rejects_node_identity_mismatch(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton.shutil,"which",lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: "")
    def fake_info(path):
        name=path.rsplit("/",1)[1]
        node="node-1" if name.startswith(".cloudmover-upload-") else "node-2"
        return RemoteFile(name,3,proton.sha1_file(p),node)
    monkeypatch.setattr(proton,"info_file",fake_info)
    monkeypatch.setattr(proton,"run",lambda args, timeout=120.0, json_output=False: "")
    with pytest.raises(RuntimeError,match="identity changed"):
        proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")

def test_safe_upload_promotes_temp_to_final_with_same_node(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    calls={"rename":[],"upload":[]}
    monkeypatch.setattr(proton.shutil,"which",lambda _: "/usr/local/bin/proton-drive")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: calls["upload"].append(args) or "")
    monkeypatch.setattr(
        proton,
        "info_file",
        lambda path: RemoteFile(path.rsplit("/",1)[1],3,proton.sha1_file(p),"node-1"),
    )
    monkeypatch.setattr(proton,"run",lambda args, timeout=120.0, json_output=False: calls["rename"].append(args) or "")
    remote=proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")
    assert remote.name=="x.txt"
    assert calls["upload"][0][:4]==["filesystem","upload","-f","rename"]
    assert calls["rename"]==[["filesystem","rename","/my-files/CloudMover/run/.cloudmover-upload-abc123.tmp","x.txt"]]

def test_safe_upload_trashes_verified_temp_by_uid_after_post_upload_failure(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    checks=[]
    trash=[]
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    def assert_no_remote_file(path):
        checks.append(path)
        if len(checks) == 3:
            raise RuntimeError("remote destination already contains this path")
    monkeypatch.setattr(proton,"assert_no_remote_file",assert_no_remote_file)
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: "")
    def info(path):
        assert path in (
            "/my-files/CloudMover/run/.cloudmover-upload-abc123.tmp",
            "/my-files/temp-node",
        )
        return RemoteFile(".cloudmover-upload-abc123.tmp",3,proton.sha1_file(p),"temp-node","parent-1")
    monkeypatch.setattr(proton,"info_file",info)
    monkeypatch.setattr(proton,"run",lambda args, timeout=120.0, json_output=False: trash.append(args) or "")

    with pytest.raises(RuntimeError,match="already contains"):
        proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")

    assert trash == [["filesystem","trash","/my-files/temp-node"]]

def test_safe_upload_reports_cleanup_failure_without_hiding_upload_failure(monkeypatch, tmp_path, caplog):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    monkeypatch.setattr(proton,"assert_no_remote_file",lambda _: None)
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: "")
    def info(path):
        if path.endswith("x.txt"):
            return RemoteFile("x.txt",3,proton.sha1_file(p),"different-node","parent-1")
        if path == "/my-files/temp-node":
            raise RuntimeError("cleanup lookup failed")
        return RemoteFile(".cloudmover-upload-abc123.tmp",3,proton.sha1_file(p),"temp-node","parent-1")
    monkeypatch.setattr(proton,"info_file",info)
    monkeypatch.setattr(proton,"run",lambda args, timeout=120.0, json_output=False: "")

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError,match="identity changed"):
            proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")

    assert "failed to cleanup Proton temp upload temp-node" in caplog.text

def test_safe_upload_cleanup_refuses_to_trash_unverified_uid(monkeypatch, tmp_path):
    p=tmp_path/"x.txt"; p.write_bytes(b"abc")
    checks=[]
    trash=[]
    monkeypatch.setattr(proton.uuid,"uuid4",lambda: type("U",(),{"hex":"abc123"})())
    def assert_no_remote_file(path):
        checks.append(path)
        if len(checks) == 3:
            raise RuntimeError("blocked")
    monkeypatch.setattr(proton,"assert_no_remote_file",assert_no_remote_file)
    monkeypatch.setattr(proton,"_run_interruptible_upload",lambda args: "")
    def info(path):
        if path == "/my-files/temp-node":
            return RemoteFile(".cloudmover-upload-abc123.tmp",3,"wrong","temp-node","parent-1")
        return RemoteFile(".cloudmover-upload-abc123.tmp",3,proton.sha1_file(p),"temp-node","parent-1")
    monkeypatch.setattr(proton,"info_file",info)
    monkeypatch.setattr(proton,"run",lambda args, timeout=120.0, json_output=False: trash.append(args) or "")

    with pytest.raises(RuntimeError,match="blocked"):
        proton.safe_upload_promote(p,"/my-files/CloudMover/run","x.txt")

    assert trash == []

def test_upload_many_rejects_directory_inputs(tmp_path, monkeypatch):
    d=tmp_path/"tree"; d.mkdir()
    monkeypatch.setattr(proton,"safe_upload_promote",lambda local,parent,name: pytest.fail("directory upload should be explicit"))
    with pytest.raises(RuntimeError,match="no longer supports directory inputs"):
        proton.upload_many([d],"/my-files/CloudMover/run")
