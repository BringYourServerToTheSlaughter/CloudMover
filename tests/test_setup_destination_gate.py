import copy
import time
from pathlib import Path
import pytest
from src.cloudmover import state
from src.cloudmover.destination import preflight_destination
from test_destination_regression import remote
from test_first_run_safety import qt_classes


@pytest.fixture
def setup(qt_classes,tmp_path,remote):
    app,scope=qt_classes
    source=tmp_path/'SyncMeBro-2';source.mkdir();(source/'one.txt').write_text('local inventory only')
    scope['preflight_destination']=preflight_destination
    scope['default_icloud_path']=lambda:source
    w=scope['SetupDialog'](tmp_path/'new.db')
    w.checks=lambda:(True,'proton-drive',100_000_000_000)
    w.readiness_message='Source is available and readable.'
    w.consent.setChecked(True);w.go_next()
    yield app,scope,source,w,remote
    if w.scan_running():
        w.request_scan_cancel()
        settle(app,w)
    w.scan_clock.stop();w.close()


def settle(app,w):
    until=time.monotonic()+3
    while w.scan_running() and time.monotonic()<until:
        app.processEvents();time.sleep(.002)
    assert not w.scan_running()


def test_collision_stays_editable_without_worker_or_inventory(setup):
    app,scope,source,w,remote=setup
    remote.folders.add(w.destination)
    remote.files[w.destination+'/keep.txt']={'name':'keep.txt','uid':'keep-uid','activeRevision':{'claimedSize':4}}
    before=copy.deepcopy(remote.files);folders=set(remote.folders)
    scope['MigrationScanWorker']=lambda *a:pytest.fail('Scan worker must not be constructed')
    scope['initialize_migration']=lambda *a,**kw:pytest.fail('Inventory must not be initialized')
    w.show();app.processEvents();w.go_next()
    assert w.pages.currentIndex()==1
    assert w.source==source and w.consent.isChecked()
    assert w.scan_worker is None and w.scan_thread is None and w.scan_summary is None
    assert not w.db_path.exists()
    assert 'That Proton folder already exists' in w.dest_help.text()
    assert w.destination in w.dest_help.text()
    assert 'Scan failed' not in w.scan_text.text()+w.dest_help.text()
    assert w.dest_edit.selectedText()==w.destination and w.dest_edit.hasFocus()
    assert not w.scan_bar.isVisible()
    assert remote.files==before and remote.folders==folders
    assert all(c[2]=='info' for c in remote.calls)


def test_second_collision_then_new_destination_starts_normally(setup):
    app,scope,source,w,remote=setup
    calls=[];scan_factory=scope['MigrationScanWorker']
    def worker(*args):calls.append(args);return scan_factory(*args)
    scope['MigrationScanWorker']=worker
    for destination in (w.destination,'/my-files/Also exists'):
        remote.folders.add(destination);w.dest_edit.setText(destination);w.go_next()
        assert w.pages.currentIndex()==1 and not calls and w.scan_summary is None
        assert destination in w.dest_help.text() and not w.db_path.exists()
    w.dest_edit.setText('/my-files/Fresh destination')
    assert not w.destination_error
    w.go_next()
    assert w.pages.currentIndex()==2 and not calls
    before=len(remote.calls)
    w.go_next()
    assert len(calls)==1 and w.scan_running()
    assert len(remote.calls)==before+2  # fresh absence + parent lookup immediately before startup
    settle(app,w)
    assert w.scan_summary is not None and w.scan_summary['source']==str(source)
    assert w.scan_summary['destination']=='/my-files/Fresh destination'
    assert w.scan_summary['files']==1
    assert '/my-files/Fresh destination' not in remote.folders  # preflight is read-only
    c=state.connect(w.db_path)
    assert c.execute('SELECT destination FROM migrations').fetchone()[0]=='/my-files/Fresh destination'
    c.close()


def test_collision_appearing_on_ready_screen_returns_to_destination(setup):
    app,scope,source,w,remote=setup
    w.go_next();assert w.pages.currentIndex()==2
    remote.folders.add(w.destination)
    scope['MigrationScanWorker']=lambda *a:pytest.fail('Late setup collision must precede worker construction')
    w.go_next()
    assert w.pages.currentIndex()==1 and w.source==source
    assert not w.db_path.exists() and w.scan_summary is None
    assert 'Scan failed' not in w.scan_text.text()
    assert w.dest_edit.selectedText()==w.destination


def test_source_preflight_failure_is_source_specific(setup):
    app,scope,source,w,remote=setup
    def checks():
        w.readiness_message='The selected iCloud source is unavailable or unreadable.'
        return False,'proton-drive',0
    w.checks=checks
    w.go_next()
    assert w.pages.currentIndex()==1 and not w.scan_running()
    assert 'source is unavailable or unreadable' in w.check_text.text()
    assert not w.destination_error and not remote.calls
    assert 'Scan failed' not in w.scan_text.text()
    assert not w.next.isEnabled()


def test_actual_inventory_failure_uses_scan_category(setup,monkeypatch):
    app,scope,source,w,remote=setup
    scope['initialize_migration']=lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('Cannot inventory damaged directory'))
    messages=[]
    monkeypatch.setattr(scope['QMessageBox'],'critical',lambda parent,title,msg:messages.append((title,msg)))
    w.go_next();w.go_next();settle(app,w)
    assert w.pages.currentIndex()==2 and w.scan_summary is None
    assert messages==[('Scan failed','Cannot inventory damaged directory')]
    assert w.scan_text.text().startswith('Scan failed')
    assert not w.destination_error and not w.db_path.exists()


def test_unavailable_destination_and_invalid_syntax_are_setup_errors(setup,monkeypatch):
    app,scope,source,w,remote=setup
    w.dest_edit.setText('invalid');w.go_next()
    assert w.pages.currentIndex()==1 and not remote.calls
    assert 'Proton destination' in w.dest_help.text()
    w.dest_edit.setText('/my-files/Unavailable parent/new')
    w.go_next()
    assert w.pages.currentIndex()==1 and not w.scan_running()
    assert 'Could not validate the Proton destination' in w.dest_help.text()
    assert w.destination in w.dest_help.text()
    assert 'Scan failed' not in w.scan_text.text()
    assert not w.db_path.exists()


def test_failed_setup_preserves_existing_inventory(setup):
    app,scope,source,w,remote=setup
    state.initialize_migration(source,w.db_path,'/my-files/Old planned destination')
    before=w.db_path.read_bytes()
    w.scan_summary={'summary':'previous successful scan'}
    remote.folders.add(w.destination)
    w.go_next()
    assert w.db_path.read_bytes()==before and w.scan_summary is None
    assert w.pages.currentIndex()==1


def test_scan_worker_has_no_remote_preflight_owner(qt_classes,tmp_path):
    app,scope=qt_classes
    scope['preflight_destination']=lambda *a:pytest.fail('Inventory worker must not perform setup preflight')
    scope['initialize_migration']=lambda *a,**kw:'inventory result'
    worker=scope['MigrationScanWorker'](tmp_path,tmp_path/'db','/my-files/new')
    results=[];worker.finished.connect(lambda summary,progress:results.append(summary))
    worker.run()
    assert results==['inventory result']


def test_preflight_disables_navigation_and_reentry(setup):
    app,scope,source,w,remote=setup
    checked=[]
    def check(path):
        checked.append(path)
        assert not w.next.isEnabled() and not w.back.isEnabled()
        w.go_next()
        assert not w.scan_running()
    scope['preflight_destination']=check
    w.go_next()
    assert checked==[w.destination] and w.pages.currentIndex()==2
