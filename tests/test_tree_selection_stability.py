import json
import time
import pytest
from PySide6.QtCore import Qt,QTimer,QPoint
from PySide6.QtTest import QTest,QSignalSpy
from src.cloudmover import state
from test_first_run_safety import qt_classes


def manifest(tmp_path,folders=3,files=4):
    source=tmp_path/'Source';source.mkdir();db=tmp_path/'selection.db'
    summary=state.initialize_migration(source,db,'/my-files/new selection')
    now=state.utc_now();c=state.connect(db)
    c.executemany('''INSERT INTO files(migration_id,relative_path,source_path,logical_bytes,
        allocated_bytes,hydration_state,transfer_state,selected,created_at,updated_at)
        VALUES(?,?,?,?,?,'LOCAL','PENDING',0,?,?)''',
        [(summary.migration_id,f'Folder {d}/file {i}',str(source/f'Folder {d}/file {i}'),1,1,now,now)
         for d in range(folders) for i in range(files)])
    c.commit();c.close();return db


def count(db):
    c=state.connect(db)
    try:return c.execute('SELECT SUM(selected) FROM files').fetchone()[0]
    finally:c.close()


def flush(app,w):
    for _ in range(10):
        app.processEvents()
        if not w._selection_timer.isActive():return
    pytest.fail('selection timer did not settle')


def logs(db):return [json.loads(line) for line in db.with_suffix('.selection.jsonl').read_text().splitlines()]


@pytest.fixture
def window(qt_classes,tmp_path):
    app,scope=qt_classes;db=manifest(tmp_path);w=scope['Window'](db);w.timer.stop()
    yield app,scope,db,w
    w._selection_timer.stop();w.timer.stop();w.close();w.deleteLater();app.processEvents()


def test_root_then_child_and_parent_partial_states(window):
    app,_,db,w=window;root=w.tree.topLevelItem(0);folder=root.child(0)
    generation=w._tree_generation
    root.setCheckState(0,Qt.Checked)
    assert count(db)==12  # persistence happens inside the action, before UI reconciliation
    assert w._selection_timer.isActive()
    assert w.tree.topLevelItem(0) is root
    flush(app,w)
    assert all(root.child(d).child(i).checkState(0)==Qt.Checked for d in range(3) for i in range(4))
    folder.setCheckState(0,Qt.Unchecked);flush(app,w)
    assert count(db)==8 and root.checkState(0)==Qt.PartiallyChecked
    assert all(folder.child(i).checkState(0)==Qt.Unchecked for i in range(4))
    folder.child(0).setCheckState(0,Qt.Checked);flush(app,w)
    assert count(db)==9 and folder.checkState(0)==Qt.PartiallyChecked
    folder.setCheckState(0,Qt.Checked);flush(app,w)
    assert root.checkState(0)==Qt.Checked and count(db)==12
    root.setCheckState(0,Qt.Unchecked);flush(app,w)
    assert root.checkState(0)==Qt.Unchecked and count(db)==0
    assert w._tree_generation==generation and w.tree.topLevelItem(0) is root


def test_siblings_rapid_toggles_coalesce_without_reentry(window):
    app,scope,db,w=window;root=w.tree.topLevelItem(0)
    root.setExpanded(True);root.child(0).setExpanded(True)
    root.setCheckState(0,Qt.Checked);flush(app,w)
    spy=QSignalSpy(w.tree.itemChanged)
    for i in range(3):root.child(i).setCheckState(0,Qt.Unchecked)
    for i in range(40):root.child(0).setCheckState(0,Qt.Checked if i%2==0 else Qt.Unchecked)
    assert spy.count()==43
    before=len([r for r in logs(db) if r['event']=='tree_selection_change_start'])
    flush(app,w)
    assert spy.count()==43  # descendant/parent reconciliation emits no itemChanged signals
    assert len([r for r in logs(db) if r['event']=='tree_selection_change_start'])==before
    assert count(db)==0 and root.checkState(0)==Qt.Unchecked
    assert root.isExpanded() and root.child(0).isExpanded()
    assert not w._selection_handling and not w._selection_loading


def test_no_sync_rebuild_even_from_another_change_slot(window):
    app,_,db,w=window;root=w.tree.topLevelItem(0);generation=w._tree_generation
    observations=[]
    def another_slot(item,column):
        w.refresh()  # periodic refresh also never destroys tree items
        w.load_selection_tree()
        observations.append(w._tree_generation)
        assert item.treeWidget() is w.tree
    w.tree.itemChanged.connect(another_slot)
    root.setCheckState(0,Qt.Checked)
    assert observations==[generation] and w.tree.topLevelItem(0) is root
    del root  # never retain/use item wrappers across the explicit deferred rebuild
    flush(app,w)
    assert w._tree_generation==generation+1 and count(db)==12
    assert w.tree.topLevelItem(0).checkState(0)==Qt.Checked
    assert any(r['event']=='tree_refresh_deferred' for r in logs(db))


def test_timer_refresh_near_checkbox_and_restart_persistence(window):
    app,scope,db,w=window;root=w.tree.topLevelItem(0);generation=w._tree_generation
    QTimer.singleShot(0,w.refresh)
    root.setCheckState(0,Qt.Checked);flush(app,w)
    root.child(1).setCheckState(0,Qt.Unchecked)
    QTimer.singleShot(0,w.refresh);flush(app,w)
    assert count(db)==8 and w._tree_generation==generation
    restarted=scope['Window'](db)
    try:
        r=restarted.tree.topLevelItem(0)
        assert r.checkState(0)==Qt.PartiallyChecked and r.child(1).checkState(0)==Qt.Unchecked
        assert restarted.pick_summary.text().startswith('8 files selected')
    finally:restarted.timer.stop();restarted.close()


def test_persistence_failure_restores_existing_items_deferred(window):
    app,scope,db,w=window;root=w.tree.topLevelItem(0)
    original=scope['set_selection_prefix']
    scope['set_selection_prefix']=lambda *a:(_ for _ in ()).throw(RuntimeError('test database locked'))
    try:
        root.setCheckState(0,Qt.Checked)
        assert root.checkState(0)==Qt.Checked and count(db)==0
        flush(app,w)
        assert root.checkState(0)==Qt.Unchecked and w.tree.topLevelItem(0) is root
        assert 'test database locked' in w.notice.text()
    finally:scope['set_selection_prefix']=original


def test_completed_selection_unchanged(window):
    app,_,db,w=window
    c=state.connect(db);c.execute("UPDATE files SET selected=1,verified=1,transfer_state='COMPLETE' WHERE id=(SELECT MIN(id) FROM files)");c.commit();c.close()
    w.load_selection_tree();root=w.tree.topLevelItem(0)
    root.setCheckState(0,Qt.Checked);flush(app,w)
    root.setCheckState(0,Qt.Unchecked);flush(app,w)
    assert count(db)==1 and root.checkState(0)==Qt.PartiallyChecked


def test_native_checkbox_mouse_release_path(window):
    app,_,db,w=window;w.show();app.processEvents();root=w.tree.topLevelItem(0)
    def click(item):
        w.tree.scrollToItem(item);app.processEvents()
        rect=w.tree.visualItemRect(item)
        # In this tree the checkbox is the first indicator in the item rectangle.
        QTest.mouseClick(w.tree.viewport(),Qt.LeftButton,Qt.NoModifier,QPoint(rect.left()+8,rect.center().y()))
        flush(app,w)
    click(root)
    assert count(db)==12
    for _ in range(12):
        click(root.child(0))
    assert count(db)==12 and w.tree.topLevelItem(0) is root
    click(root.child(1));assert count(db)==8


def test_large_tree_and_bounded_diagnostics(qt_classes,tmp_path):
    app,scope=qt_classes;db=manifest(tmp_path,folders=100,files=100)
    began=time.perf_counter();w=scope['Window'](db);w.timer.stop()
    build=time.perf_counter()-began
    try:
        root=w.tree.topLevelItem(0);generation=w._tree_generation
        began=time.perf_counter();root.setCheckState(0,Qt.Checked);flush(app,w)
        select=time.perf_counter()-began
        began=time.perf_counter()
        for d in (0,20,40,60,80):root.child(d).setCheckState(0,Qt.Unchecked)
        flush(app,w);deselect=time.perf_counter()-began
        assert count(db)==9500 and root.checkState(0)==Qt.PartiallyChecked
        assert w._tree_generation==generation and w.tree.topLevelItem(0) is root
        records=logs(db)
        assert len(records)<30
        starts=[r for r in records if r['event']=='tree_selection_change_start']
        assert len(starts)==6 and starts[0]['descendant_count']==10000
        assert all(r['tree_generation']==generation and 'migration_id' in r for r in records)
        assert all(r['persistence_duration_ms']>=0 for r in records if r['event']=='tree_selection_change_complete')
        print(f'10,000 files: build={build:.3f}s root select={select:.3f}s five deselections={deselect:.3f}s')
        # Generous guard against accidental per-descendant DB writes/quadratic rebuilding.
        assert select<10 and deselect<10
    finally:w._selection_timer.stop();w.timer.stop();w.close()
