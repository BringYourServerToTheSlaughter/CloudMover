from pathlib import Path
import pytest
from src.cloudmover import gui, state
from src.cloudmover.destination import preflight_destination, prepare_destination
from src.cloudmover.providers import proton
from test_destination_regression import remote
from test_first_run_safety import qt_classes


@pytest.mark.parametrize('path', ['/my-files', '/my-files/', '/my-files/New Backup'])
def test_root_and_subfolder_validation(path):
    assert gui.validate_proton_destination(path)[0]


@pytest.mark.parametrize('path', ['/my-files', '/my-files/'])
def test_root_preflight_and_identity_without_creation(remote,tmp_path,path):
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('safe')
    db=tmp_path/'state.db';mid=state.initialize_migration(source,db,path).migration_id
    preflight_destination(path)
    assert prepare_destination(db,path,mid)=='/my-files'
    assert prepare_destination(db,path,mid)=='/my-files'
    assert all(call[2] in ('info','list') for call in remote.calls)
    remote.folder_ids['/my-files']='replacement'
    with pytest.raises(RuntimeError,match='identity changed'):
        prepare_destination(db,path,mid)


def test_root_preserves_existing_file_collision_guard(remote,tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('safe')
    db=tmp_path/'state.db';mid=state.initialize_migration(source,db,'/my-files/').migration_id
    prepare_destination(db,'/my-files/',mid)
    remote.files['/my-files/one.txt']={'name':'one.txt','uid':'precious'}
    with pytest.raises(RuntimeError):
        proton.assert_no_remote_file('/my-files/one.txt')
    assert remote.files['/my-files/one.txt']['uid']=='precious'
    assert all(call[2] in ('info','list') for call in remote.calls)


def test_stages_and_contextual_controls(qt_classes,tmp_path,monkeypatch):
    app,scope=qt_classes
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('safe')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/')
    monkeypatch.setattr(scope['Window'],'live_engine_pid',lambda self:None)
    w=scope['Window'](db);w.show();app.processEvents()
    assert '2 · Select</b>' in w.steps.text()
    assert w.picker.isVisible() and not w.bar.isVisible()
    assert not w.resume_btn.isVisible() and w.pause_btn.text()=='Back'
    w.review_selection()
    assert '3 · Review</b>' in w.steps.text()
    assert w.status.text()=='Ready to move'
    assert w.resume_btn.isVisible() and w.resume_btn.text()=='Start Migration'
    assert not w.picker.isVisible() and not w.bar.isVisible()
    w.secondary_action()
    assert w.picker.isVisible()
    monkeypatch.setattr(w,'live_engine_pid',lambda:123)
    w.refresh()
    assert '4 · Move</b>' in w.steps.text()
    assert not w.resume_btn.isVisible() and w.pause_btn.text()=='Pause Migration'
    assert not w.picker.isVisible() and w.bar.isVisible()
    state.set_activity_progress(db,stage='release',completed=0,total=1,detail='one.txt')
    w.refresh()
    assert '4 · Move</b>' in w.steps.text()
    assert 'Release' in w.phases.text()
    monkeypatch.setattr(w,'live_engine_pid',lambda:None)
    c=state.connect(db);c.execute("UPDATE migrations SET status='COMPLETE'");c.execute("UPDATE files SET transfer_state='COMPLETE',verified=1");c.commit();c.close()
    w.refresh()
    assert w.pause_btn.text()=='Done' and w.new_btn.isVisible()
    assert not w.resume_btn.isVisible()
    w.timer.stop();w.close()


def test_main_flow_has_no_engineering_footer():
    text=Path(gui.__file__).read_text()
    assert 'refreshes every second' not in text
    assert 'Engine:' not in text
    assert 'setText("Paused")' not in text


@pytest.mark.parametrize('existing', ['folder', 'file'])
def test_root_refuses_existing_selected_folder_without_remote_mutation(remote,tmp_path,existing):
    source=tmp_path/'source';(source/'CloudMover').mkdir(parents=True)
    (source/'CloudMover'/'build.zip').write_text('test')
    db=tmp_path/'state.db';mid=state.initialize_migration(source,db,'/my-files/').migration_id
    if existing=='file':
        remote.folders.remove('/my-files/CloudMover')
        remote.files['/my-files/CloudMover']={'name':'CloudMover','uid':'precious'}
    folders=set(remote.folders);files=dict(remote.files)
    with pytest.raises(RuntimeError,match='already exists'):
        prepare_destination(db,'/my-files/',mid)
    assert folders==remote.folders and files==remote.files
    assert all(call[2]=='info' for call in remote.calls)
    c=state.connect(db)
    assert c.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE 'destination%'").fetchone()[0]==0
    c.close()


def test_root_owns_new_nested_folders_and_rejects_changed_identity(remote,tmp_path):
    source=tmp_path/'source';(source/'Photos'/'Summer').mkdir(parents=True)
    (source/'Photos'/'Summer'/'one.txt').write_text('safe')
    db=tmp_path/'state.db';mid=state.initialize_migration(source,db,'/my-files/').migration_id
    prepare_destination(db,'/my-files/',mid)
    assert '/my-files/Photos/Summer' in remote.folders
    assert all('/my-files/CloudMover' not in call for call in remote.calls)
    remote.calls.clear();prepare_destination(db,'/my-files/',mid)
    assert all(call[2]=='info' for call in remote.calls)
    remote.folder_ids['/my-files/Photos/Summer']='replacement'
    with pytest.raises(RuntimeError,match='identity changed'):
        prepare_destination(db,'/my-files/',mid)


def test_root_creation_crash_does_not_adopt_orphan(remote,tmp_path,monkeypatch):
    source=tmp_path/'source';(source/'Photos').mkdir(parents=True)
    (source/'Photos'/'one.txt').write_text('safe')
    db=tmp_path/'state.db';mid=state.initialize_migration(source,db,'/my-files/').migration_id
    original=proton.info_file
    def interrupted(path):
        if path=='/my-files/Photos':raise KeyboardInterrupt()
        return original(path)
    with monkeypatch.context() as patch:
        patch.setattr(proton,'info_file',interrupted)
        with pytest.raises(KeyboardInterrupt):prepare_destination(db,'/my-files/',mid)
    with pytest.raises(RuntimeError,match='already exists'):
        prepare_destination(db,'/my-files/',mid)
    assert '/my-files/Photos' in remote.folders


def test_review_start_source_labels_pause_and_completion(qt_classes,tmp_path,monkeypatch):
    app,scope=qt_classes
    source=tmp_path/'SyncMeBro-2';source.mkdir();(source/'one.txt').write_text('safe')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/Backup')
    monkeypatch.setattr(scope['Window'],'live_engine_pid',lambda self:None)
    w=scope['Window'](db);w.timer.stop();w.show();app.processEvents()
    assert source.name in w.route.text() and w.tree.topLevelItem(0).text(0)==source.name
    w.review_btn.click();assert w.review_detail.isVisible()
    assert str(source) in w.review_detail.text() and 'Proton Drive → Backup' in w.review_detail.text()
    assert w.pause_btn.text()=='Back to Selection'
    starts=[];monkeypatch.setattr(w,'resume_migration',lambda:starts.append(True))
    w.resume_btn.click();assert starts==[True]
    c=state.connect(db);c.execute("UPDATE files SET attempts=1");c.commit();c.close()
    w.refresh();assert w.resume_btn.text()=='Resume Migration'
    c=state.connect(db)
    c.execute("UPDATE files SET transfer_state='COMPLETE',verified=1,release_error='macOS refused cleanup'")
    c.execute("UPDATE migrations SET status='COMPLETE'");c.commit();c.close()
    w.refresh()
    assert '5 · Complete</b>' in w.steps.text()
    assert w.cleanup_notice.isVisible() and not w.attention.isVisible()
    assert not w.resume_btn.isVisible() and w.pause_btn.text()=='Done'
    w.pause_btn.click();assert not w.isVisible()


@pytest.mark.parametrize('node', ['file', 'folder'])
def test_root_direct_file_cannot_replace_existing_node(remote,tmp_path,node):
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('safe')
    db=tmp_path/'state.db';mid=state.initialize_migration(source,db,'/my-files/').migration_id
    if node=='file':remote.files['/my-files/one.txt']={'name':'one.txt','uid':'precious'}
    else:remote.folders.add('/my-files/one.txt')
    with pytest.raises(RuntimeError,match='already exists'):prepare_destination(db,'/my-files/',mid)
    assert all(call[2]=='info' for call in remote.calls)


def test_unverified_completion_is_not_presented_as_success(qt_classes,tmp_path,monkeypatch):
    app,scope=qt_classes
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('safe')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/Backup')
    c=state.connect(db);c.execute("UPDATE migrations SET status='COMPLETE'");c.commit();c.close()
    monkeypatch.setattr(scope['Window'],'live_engine_pid',lambda self:None)
    w=scope['Window'](db);w.timer.stop();w.show();app.processEvents()
    assert 'needs attention' in w.status.text()
    assert w.completion_title.isHidden() and not w.resume_btn.isEnabled()
    assert w.attention.isVisible() and 'verified file counts' in w.attention.text()
    w.close()


@pytest.mark.parametrize('destination,friendly',[('/my-files/','Proton Drive root'),('/my-files/Sync3','Proton Drive → Sync3')])
def test_friendly_destination_safety_and_active_spacing(qt_classes,tmp_path,monkeypatch,destination,friendly):
    app,scope=qt_classes
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('safe')
    db=tmp_path/'state.db';state.initialize_migration(source,db,destination)
    monkeypatch.setattr(scope['Window'],'live_engine_pid',lambda self:None)
    w=scope['Window'](db);w.timer.stop();w.show();app.processEvents()
    try:
        assert w.destination_detail.text()==friendly
        w.review_selection()
        assert friendly in w.review_detail.text() and '/my-files' not in w.review_detail.text()
        assert 'before freeing temporary local storage' in w.safety.text()
        monkeypatch.setattr(w,'live_engine_pid',lambda:123)
        w.refresh()
        assert w.progress_card.layout().contentsMargins().top()==24
        assert state.connect(db).execute('SELECT destination FROM migrations').fetchone()[0]==destination
    finally:
        monkeypatch.setattr(w,'live_engine_pid',lambda:None)
        w.close()
