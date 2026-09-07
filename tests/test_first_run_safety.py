import ast
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.cloudmover import gui, state, disk_space
from src.cloudmover.scanner import choose_strategy
from src.cloudmover.version import VERSION


def test_destination_is_readable_and_isolated():
    a=gui.new_destination()
    assert a == '/my-files/iCloud Drive'
    assert a.count('/')==2
    assert 'CloudMover' not in a
    assert gui.friendly_destination(a)=='Proton Drive → '+a.rsplit('/',1)[1]
    assert gui.validate_proton_destination(a)[0]


def test_readiness_unavailable_missing_outside_unreadable(tmp_path, monkeypatch):
    root=tmp_path/'iCloud'; monkeypatch.setattr(gui,'default_icloud_path',lambda: root)
    assert not gui.icloud_readiness(root)[0]
    root.mkdir()
    assert gui.icloud_readiness(root)[0]
    assert 'could not be confirmed' in gui.icloud_readiness(root)[1]
    assert not gui.icloud_readiness(root/'missing')[0]
    assert not gui.icloud_readiness(tmp_path)[0]
    monkeypatch.setattr(gui.os,'access',lambda *a: False)
    assert not gui.icloud_readiness(root)[0]


@pytest.mark.parametrize('free,allowed',[(5_000_000_000,False),(5_499_999_999,False),(5_500_000_000,True)])
def test_batch_headroom(free,allowed,monkeypatch):
    monkeypatch.setattr(disk_space.shutil,'disk_usage',lambda p: SimpleNamespace(free=free))
    items=[SimpleNamespace(source_path='/source/file',logical_bytes=250_000_000)]
    if allowed: disk_space.require_batch_space(items)
    else:
        with pytest.raises(RuntimeError): disk_space.require_batch_space(items)


def test_staging_volume_and_oversize_file(monkeypatch):
    items=[SimpleNamespace(source_path='/source/file',logical_bytes=3_000_000_000)]
    monkeypatch.setattr(disk_space.shutil,'disk_usage',lambda p: SimpleNamespace(free=10_000_000_000))
    with pytest.raises(RuntimeError): disk_space.require_batch_space(items)
    items[0].logical_bytes=250_000_000
    monkeypatch.setattr(disk_space.shutil,'disk_usage',lambda p: SimpleNamespace(free=6_000_000_000 if p.startswith('/source') else 5_000_000_000))
    with pytest.raises(RuntimeError): disk_space.require_batch_space(items)


def test_strategy_bounded():
    for free in [0,4_000_000_000,5_100_000_000,100_000_000_000]:
        _,staging,reserve=choose_strategy(50_000_000_000,free)
        assert reserve==5_000_000_000
        assert 0 <= staging <= min(250_000_000,max(0,free-reserve))


@pytest.mark.parametrize('during_commit',[False,True])
def test_cancel_leaves_no_partial_migration(tmp_path,during_commit,monkeypatch):
    source=tmp_path/'source';source.mkdir();(source/'file').write_text('a')
    db=tmp_path/'state.db'; cancelled=False
    def progress(p):
        nonlocal cancelled
        if not during_commit: cancelled=True
    original_connect=state.connect
    def connect(path):
        c=original_connect(path)
        def trace(sql):
            nonlocal cancelled
            if during_commit and 'INSERT INTO files' in sql:
                cancelled=True
        c.set_trace_callback(trace)
        return c
    monkeypatch.setattr(state,'connect',connect)
    def check():
        return cancelled
    with pytest.raises(RuntimeError,match='Scan cancelled'):
        state.initialize_migration(source,db,'/my-files/test',progress,cancel_check=check)
    c=state.connect(db);state.create_schema(c)
    assert c.execute('SELECT COUNT(*) FROM migrations').fetchone()[0]==0
    assert c.execute('SELECT COUNT(*) FROM files').fetchone()[0]==0
    c.close()


@pytest.fixture
def qt_classes(monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6 import QtCore, QtWidgets
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    scope=dict(vars(gui))
    scope["preflight_destination"]=lambda path:None
    for module in (QtCore,QtWidgets):
        scope.update({name:getattr(module,name) for name in dir(module) if name.startswith("Q") or name in ("Signal","Slot")})
    tree=ast.parse(Path(gui.__file__).read_text())
    main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    classes=[n for n in main.body if isinstance(n,ast.ClassDef) and n.name in ('MigrationScanWorker','SetupDialog','CompletionDialog','Window')]
    exec(compile(ast.Module(body=classes,type_ignores=[]),gui.__file__,'exec'),scope)
    return app,scope


def test_consent_scan_refresh_cancel_and_retry(qt_classes,tmp_path,monkeypatch):
    app,scope=qt_classes
    scope['default_icloud_path']=lambda: tmp_path
    scope['icloud_readiness']=lambda p:(True,'Available')
    scope['find_proton_drive_cli']=lambda:'proton-drive'
    import threading,time
    entered=threading.Event()
    def scan(source,db,destination,report,cancel_check):
        entered.set()
        while not cancel_check(): time.sleep(.005)
        raise RuntimeError('Scan cancelled')
    scope['initialize_migration']=scan
    dialog=scope['SetupDialog'](tmp_path/'db')
    assert not dialog.next.isEnabled()
    dialog.go_next();assert dialog.pages.currentIndex()==0
    dialog.consent.setChecked(True)
    assert dialog.next.isEnabled()
    dialog.consent.setChecked(False)
    assert not dialog.next.isEnabled()
    dialog.consent.setChecked(True);dialog.go_next();dialog.go_next()
    dialog.go_next();worker=dialog.scan_worker
    assert dialog.scan_running()
    dialog.refresh_page();dialog.go_next();dialog.go_back()
    assert dialog.scan_worker is worker
    assert not dialog.next.isEnabled() and not dialog.back.isEnabled()
    assert entered.wait(2)
    dialog.request_scan_cancel()
    assert dialog.scan_text.text() == "Cancelling scan…"
    assert not dialog.cancel_scan.isEnabled()
    dialog.refresh_page();dialog.tick_scan()
    assert not dialog.next.isEnabled() and not dialog.back.isEnabled()
    assert "Estimating" not in dialog.scan_activity.text()
    deadline=time.monotonic()+3
    while dialog.scan_running() and time.monotonic()<deadline:
        app.processEvents();time.sleep(.005)
    assert not dialog.scan_running()
    assert dialog.scan_summary is None
    assert dialog.next.isEnabled() and dialog.back.isEnabled()
    dialog.close()


def test_version_sources():
    import tomllib
    assert VERSION=='0.19.6'
    data=tomllib.loads(Path('pyproject.toml').read_text())
    assert data['tool']['setuptools']['dynamic']['version']['attr']=='src.cloudmover.version.VERSION'
    assert 'version=VERSION' in Path('CloudMover.spec').read_text()


def test_disabled_buttons_render_disabled(qt_classes,tmp_path):
    app,scope=qt_classes
    w=scope['SetupDialog'](tmp_path/'db')
    w.show();app.processEvents()
    def background(button):
        app.processEvents()
        return button.grab().toImage().pixelColor(15,button.height()//2).name()
    assert not w.next.isEnabled()
    assert background(w.next)=='#183047'
    w.consent.setChecked(True)
    assert w.next.isEnabled() and background(w.next)=='#2563eb'
    w.consent.setChecked(False)
    assert not w.next.isEnabled() and background(w.next)=='#183047'
    w.pages.setCurrentIndex(2);w.refresh_page()
    w.back.setEnabled(False)
    assert background(w.back)=='#183047'
    w.close()


def test_cancel_before_resuming_walk_does_not_call_provider(tmp_path,monkeypatch):
    from src.cloudmover import scanner
    cancelled=False
    calls=[]
    class Entry:
        name="file"
        def is_dir(self,**kwargs):
            calls.append('is_dir')
            return False
    class Entries:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def __next__(self):
            calls.append('next')
            return Entry()
    monkeypatch.setattr(scanner.os,'scandir',lambda p:Entries())
    def check():
        if cancelled: raise RuntimeError('Scan cancelled')
    walk=scanner.walk_entries(tmp_path,checkpoint=check)
    next(walk);cancelled=True
    with pytest.raises(RuntimeError,match='Scan cancelled'):next(walk)
    assert calls==['next']


def test_cancel_after_stat_stops_before_inspection(tmp_path,monkeypatch):
    cancelled=False
    class Entry:
        path=str(tmp_path/'file')
        def is_dir(self,**kwargs):return False
        def is_file(self,**kwargs):return True
        def stat(self,**kwargs):
            nonlocal cancelled
            cancelled=True
            return None
    monkeypatch.setattr(state,'walk_entries',lambda *a,**kw:iter([Entry()]))
    monkeypatch.setattr(state,'inspect_file',lambda *a,**kw:pytest.fail('inspection after cancel'))
    with pytest.raises(RuntimeError,match='Scan cancelled'):
        state.initialize_migration(tmp_path,tmp_path/'db',cancel_check=lambda:cancelled)
    assert not (tmp_path/'db').exists()


@pytest.fixture
def dashboard_window(qt_classes,tmp_path,monkeypatch):
    app,scope=qt_classes
    snapshot={
        'm':{'destination':'/my-files/CloudMover/test','status':'RUNNING','source':str(tmp_path),'reserve_bytes':5_000_000_000},
        'done':{'n':0,'b':0},'selected_files':6,'selected_bytes':600,
        'active':{'transfer_state':'HYDRATING','relative_path':'file','source_path':str(tmp_path/'file'),'logical_bytes':100},
        'active_count':1,'active_bytes':100,'activity':{'stage':'hydrate','completed':0,'total':6},
        'failed_files':0,'failed_rows':[],'released':{'n':0},'verified_files':0,
    }
    cls=scope['Window']
    monkeypatch.setattr(cls,'repair_stale_runtime_if_needed',lambda self:{})
    monkeypatch.setattr(cls,'load_selection_tree',lambda *a,**kw:None)
    monkeypatch.setattr(cls,'live_engine_pid',lambda self:123)
    scope['snapshot']=lambda db:snapshot
    scope['inspect_file']=lambda p:SimpleNamespace(allocated_bytes=50,hydration=gui.HydrationState.CLOUD_ONLY)
    w=cls(tmp_path/'db');w.timer.stop()
    yield w,snapshot
    # Do not invoke app-exit engine controls in the mocked dashboard.
    w.deleteLater();app.processEvents()


def test_dashboard_refresh_replaces_starting_and_matches_activity(dashboard_window):
    w,s=dashboard_window
    w.notice.setText('Starting migration…');w.refresh()
    assert not w.notice.text()
    assert 'Hydrating file 1 of 6' in w.stage.text()
    assert 'downloading from iCloud locally' in w.stage.text()
    assert w.bar.value()==0 and w.filebar.value()==500
    assert 'Overall migration progress' in w.status.text()
    assert 'Current file' in w.fileprogress.text()
    s['active']['transfer_state']='UPLOADING'
    # Previous hydration counters must not become upload percentages.
    w.refresh()
    assert w.stage.text()=='Uploading batch to Proton Drive…'
    assert w.filebar.maximum()==0
    s['active']['transfer_state']='VERIFYING'
    s['activity']={'stage':'verify','completed':1,'total':2}
    w.refresh()
    assert w.filebar.maximum()==1000 and w.filebar.value()==500
    assert 'folder groups' in w.fileprogress.text()
    assert 'Verifying' in w.stage.text()
    s['active']=None;s['done']['n']=6
    w.refresh()
    assert w.stage.text()=='Finalizing migration…'
    assert w.completion_title.isHidden()
    s['done']['n']=0;w.refresh()
    assert w.stage.text()=='Preparing migration…'


def test_attention_only_visible_for_failures(dashboard_window):
    w,s=dashboard_window
    assert w.attention_title.isHidden() and w.attention.isHidden()
    s['failed_files']=1
    s['failed_rows']=[{'relative_path':'bad','last_error':'Network unavailable'}]
    w.refresh()
    assert not w.attention_title.isHidden() and not w.attention.isHidden()
    assert 'Network unavailable' in w.attention.text()
    s['failed_files']=0;s['failed_rows']=[];w.refresh()
    assert w.attention_title.isHidden() and w.attention.isHidden()


def test_cancel_waits_for_inflight_filesystem_call(qt_classes,tmp_path,monkeypatch):
    import threading,time
    app,scope=qt_classes
    scope['default_icloud_path']=lambda:tmp_path
    scope['icloud_readiness']=lambda p:(True,'Available')
    scope['find_proton_drive_cli']=lambda:'proton-drive'
    entered=threading.Event();release=threading.Event()
    class Entry:
        path=str(tmp_path/'file')
        def is_dir(self,**kwargs):return False
        def is_file(self,**kwargs):return True
        def stat(self,**kwargs):
            entered.set()
            assert release.wait(3)
            return None
    monkeypatch.setattr(state,'walk_entries',lambda *a,**kw:iter([Entry()]))
    w=scope['SetupDialog'](tmp_path/'db')
    w.consent.setChecked(True);w.go_next();w.go_next();w.go_next()
    try:
        assert entered.wait(2)
        w.request_scan_cancel();app.processEvents();w.refresh_page()
        assert w.scan_running()
        assert not w.cancel_scan.isEnabled() and not w.back.isEnabled() and not w.next.isEnabled()
        assert w.scan_text.text()=='Cancelling scan…'
    finally:
        release.set()
        deadline=time.monotonic()+3
        while w.scan_running() and time.monotonic()<deadline:
            app.processEvents();time.sleep(.005)
    assert not w.scan_running() and w.scan_summary is None
    assert not (tmp_path/'db').exists()
    assert w.next.isEnabled() and w.back.isEnabled()
    w.close()


def test_successful_scan_restores_review_navigation(qt_classes,tmp_path):
    import time
    app,scope=qt_classes
    scope['default_icloud_path']=lambda:tmp_path
    scope['icloud_readiness']=lambda p:(True,'Available')
    scope['find_proton_drive_cli']=lambda:'proton-drive'
    (tmp_path/'file').write_text('content')
    w=scope['SetupDialog'](tmp_path/'inventory.db')
    w.consent.setChecked(True);w.go_next();w.go_next();w.go_next()
    deadline=time.monotonic()+3
    while w.scan_running() and time.monotonic()<deadline:
        app.processEvents();time.sleep(.005)
    assert not w.scan_running() and w.scan_summary is not None
    assert w.next.isEnabled() and w.back.isEnabled()
    assert w.next.text()=='Review Migration'
    w.go_next();assert w.pages.currentIndex()==3
    w.close()
