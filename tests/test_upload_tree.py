
from src.cloudmover import upload

def test_ensure_remote_tree(monkeypatch):
    calls=[]
    def fake(parent,name):
        calls.append((parent,name))
        return parent.rstrip("/")+"/"+name
    monkeypatch.setattr(upload.proton,"ensure_folder",fake)
    parent,full=upload.ensure_remote_tree(
        "/my-files/CloudMover-Tree-Test",
        "Personal/Mortgage Application 2020/file.pdf",
    )
    assert calls == [
        ("/my-files/CloudMover-Tree-Test","Personal"),
        ("/my-files/CloudMover-Tree-Test/Personal","Mortgage Application 2020"),
    ]
    assert parent == "/my-files/CloudMover-Tree-Test/Personal/Mortgage Application 2020"
    assert full == "/my-files/CloudMover-Tree-Test/Personal/Mortgage Application 2020/file.pdf"
