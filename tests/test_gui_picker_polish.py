
from pathlib import Path
from src.cloudmover import state
from test_first_run_safety import qt_classes

def test_picker_window_is_large(qt_classes,tmp_path):
    app,scope=qt_classes
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('test')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/Backup')
    w=scope['Window'](db);w.timer.stop();w.show();app.processEvents()
    try:
        assert w.width()>=1000 and w.height()>=800
        assert w.tree.height()>=320 and w.picker.isVisible()
        selection_height=w.height()
        w.review_selection();app.processEvents()
        assert w.height()<selection_height and not w.picker.isVisible()
    finally:w.close()

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
