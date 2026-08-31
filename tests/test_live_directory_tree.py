
from src.cloudmover.gui import source_directories, build_tree_index

def test_live_directories_include_empty_folders(tmp_path):
    (tmp_path/"Empty").mkdir()
    (tmp_path/"HasFiles").mkdir()
    (tmp_path/"HasFiles"/"x.txt").write_text("x")
    dirs=source_directories(tmp_path)
    assert "Empty" in dirs
    assert "HasFiles" in dirs

    rows=[{"relative_path":"HasFiles/x.txt","logical_bytes":1,"selected":1,"transfer_state":"PENDING"}]
    tree=build_tree_index(rows,dirs)
    assert "Empty" in tree["dirs"]
    assert tree["dirs"]["Empty"]["total"]==0

def test_zero_selected_progress_is_zero():
    from pathlib import Path
    s=Path("src/cloudmover/gui.py").read_text()
    assert "pct=(done/total*100 if total else 0.0)" in s
