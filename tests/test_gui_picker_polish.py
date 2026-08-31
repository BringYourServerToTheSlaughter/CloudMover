
from pathlib import Path

def test_picker_window_is_large():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.resize(1260,930)" in s
    assert "self.setMinimumSize(1000,760)" in s

def test_picker_has_search_and_expand_collapse():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Search files and folders" in s
    assert "Expand All" in s
    assert "Collapse All" in s
    assert "filter_tree" in s
    assert "expandAll()" in s
    assert "collapseAll()" in s

def test_picker_is_primary_workspace_area():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "lay.addWidget(picker,1)" in s
    assert "# 1) Picker first" in s
