import json
import pytest
from test_destination_regression import remote, FakeProton
from src.cloudmover import gui, state, bulk_pipeline
from src.cloudmover.destination import prepare_destination, preflight_destination


def plan(tmp_path,path):
    source=tmp_path/'source';source.mkdir(exist_ok=True)
    (source/'Pics').mkdir(exist_ok=True)
    (source/'Pics'/'file.txt').write_text('disposable content')
    db=tmp_path/'state.db'
    mid=state.initialize_migration(source,db,path).migration_id
    return db,mid


def test_default_root_leaves_user_cloudmover_completely_untouched(remote,tmp_path,monkeypatch):
    path=gui.new_destination(tmp_path/"SyncMeBro-2")
    db,mid=plan(tmp_path,path)
    remote.files['/my-files/CloudMover/build.app']={'name':'build.app','uid':'user-owned'}
    before=dict(remote.files)
    assert preflight_destination(path) is None
    assert prepare_destination(db,path,mid)==path
    assert remote.files==before
    assert not any('/my-files/CloudMover' in arg for cmd in remote.calls for arg in cmd)
    assert gui.friendly_destination(path)=='Proton Drive → '+path.rsplit('/',1)[1]
    assert path.count('/')==2
    assert all(mid not in folder for folder in remote.folders)
    # Per-file relative hierarchy is placed directly in the exact displayed root.
    from src.cloudmover.upload import ensure_remote_tree
    parent,file=ensure_remote_tree(path,'Pics/file.txt')
    assert file==path+'/Pics/file.txt'


def test_existing_folder_even_empty_is_refused(remote,tmp_path):
    path='/my-files/CloudMover'
    db,mid=plan(tmp_path,path)
    with pytest.raises(RuntimeError,match='already exists'):
        prepare_destination(db,path,mid)
    assert all(cmd[2]=='info' for cmd in remote.calls)
    assert remote.folders=={'/my-files','/my-files/CloudMover'}


def test_explicit_custom_destination_is_exact(remote,tmp_path):
    path='/my-files/My iCloud Backup'
    db,mid=plan(tmp_path,path)
    assert prepare_destination(db,path,mid)==path
    assert ['proton-drive','filesystem','create-folder','/my-files','My iCloud Backup','-j'] in remote.calls
    assert gui.friendly_destination(path)=='Proton Drive → My iCloud Backup'


def test_resume_requires_recorded_same_node(remote,tmp_path):
    path='/my-files/My iCloud Backup'
    db,mid=plan(tmp_path,path)
    prepare_destination(db,path,mid)
    remote.calls.clear()
    assert prepare_destination(db,path,mid)==path
    assert all(cmd[2]=='info' for cmd in remote.calls)
    remote.folder_ids[path]='replacement-node'
    with pytest.raises(RuntimeError,match='identity changed'):
        prepare_destination(db,path,mid)
    assert all(cmd[2]=='info' for cmd in remote.calls)


def test_collision_between_preflight_and_create_does_not_adopt(remote,tmp_path,monkeypatch):
    path='/my-files/My iCloud Backup';db,mid=plan(tmp_path,path)
    original=remote.process
    def race(cmd,**kwargs):
        if cmd[2]=='create-folder':remote.folders.add(path)
        return original(cmd,**kwargs)
    monkeypatch.setattr('src.cloudmover.providers.proton._run_cli_process',race)
    with pytest.raises(RuntimeError,match='Already exists'):
        prepare_destination(db,path,mid)
    c=state.connect(db)
    assert c.execute('SELECT value FROM metadata WHERE key=?',('destination_owner:'+mid,)).fetchone() is None
    c.close()


def test_unowned_legacy_destination_is_not_adopted_or_rewritten(remote,tmp_path):
    path='/my-files/CloudMover/old migration/old-uuid'
    remote.folders.add(path)
    db,mid=plan(tmp_path,path)
    with pytest.raises(RuntimeError,match='already exists'):
        prepare_destination(db,path,mid)
    c=state.connect(db)
    assert c.execute('SELECT destination FROM migrations WHERE id=?',(mid,)).fetchone()['destination']==path
    c.close()
    assert path in remote.folders
    assert all(cmd[2]=='info' for cmd in remote.calls)


def test_changed_stored_destination_cannot_reuse_ownership(remote,tmp_path):
    path='/my-files/My iCloud Backup';db,mid=plan(tmp_path,path)
    prepare_destination(db,path,mid)
    with pytest.raises(RuntimeError,match='does not match'):
        prepare_destination(db,'/my-files/CloudMover',mid)


def test_wizard_collision_prevents_inventory(tmp_path,monkeypatch):
    import time
    from test_first_run_safety import qt_classes
    app,scope=qt_classes.__wrapped__(monkeypatch)
    scope['default_icloud_path']=lambda:tmp_path
    scope['icloud_readiness']=lambda p:(True,'Available')
    scope['find_proton_drive_cli']=lambda:'proton-drive'
    def collision(path):raise RuntimeError('Destination already exists; choose a new name.')
    scope['preflight_destination']=collision
    scope['initialize_migration']=lambda *a,**kw:pytest.fail('Inventory must not start after destination collision')
    monkeypatch.setattr(scope['QMessageBox'],'critical',lambda *a:None)
    w=scope['SetupDialog'](tmp_path/'db')
    w.consent.setChecked(True);w.go_next();w.go_next();w.go_next()
    deadline=time.monotonic()+3
    while w.scan_running() and time.monotonic()<deadline:
        app.processEvents();time.sleep(.005)
    assert w.scan_summary is None and not w.scan_running()
    assert not (tmp_path/'db').exists()
    w.go_back();assert w.pages.currentIndex()==1
    w.dest_edit.setText('/my-files/My iCloud Backup')
    assert w.destination=='/my-files/My iCloud Backup'
    assert w.dest_name.text()=='Proton Drive → My iCloud Backup'
    w.close()
