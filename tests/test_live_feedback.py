import json
from pathlib import Path
import pytest
from test_first_run_safety import qt_classes, dashboard_window
from test_destination_regression import remote
from src.cloudmover import gui, state, scanner, release, start
from src.cloudmover.providers import proton
from src.cloudmover.providers.icloud import FileState, HydrationState
from src.cloudmover.diagnostics import safe_diagnostic


def make_plan(tmp_path,names=('one.txt','two.txt')):
    source=tmp_path/'SyncMeBro-2';source.mkdir()
    for name in names:
        file=source/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('content')
    db=tmp_path/'db'
    mid=state.initialize_migration(source,db,gui.new_destination(source)).migration_id
    return source,db,mid


def test_source_name_destination_and_custom_choice(qt_classes,tmp_path,monkeypatch):
    app,scope=qt_classes
    w=scope['SetupDialog'](tmp_path/'db')
    monkeypatch.setattr(scope['QFileDialog'],'getExistingDirectory',lambda *a:str(tmp_path/'SyncMeBro-2'))
    w.choose_source()
    assert w.destination=='/my-files/SyncMeBro-2'
    assert w.dest_edit.text()==w.destination
    assert gui.friendly_destination(w.destination)=='Proton Drive → SyncMeBro-2'
    w.dest_edit.setText('/my-files/My Backup')
    monkeypatch.setattr(scope['QFileDialog'],'getExistingDirectory',lambda *a:str(tmp_path/'Other'))
    w.choose_source()
    assert w.destination=='/my-files/My Backup'
    w.close()


def test_ds_store_excluded_from_inventory_and_counts(tmp_path):
    source,db,mid=make_plan(tmp_path,('.DS_Store','Pics/.DS_Store','Pics/photo.txt','.keep'))
    result=scanner.scan(source)
    assert result.files==2 and result.logical_bytes==14
    c=state.connect(db)
    assert {r[0] for r in c.execute('SELECT relative_path FROM files')}=={'Pics/photo.txt','.keep'}
    assert c.execute('SELECT COUNT(*) FROM files WHERE selected=1').fetchone()[0]==2
    c.close()
    (source/'New').mkdir();(source/'New'/'.DS_Store').write_text('metadata')
    assert state.reconcile_migration_inventory(db)['added']==0


def test_release_failure_is_recorded_continues_and_completes(tmp_path,monkeypatch):
    source,db,mid=make_plan(tmp_path)
    c=state.connect(db)
    c.execute("UPDATE files SET transfer_state='COMPLETE',verified=1,hydration_state='LOCAL',selected=1")
    c.commit();c.close()
    evicted=set();attempts=[]
    def inspect(path):return FileState(HydrationState.CLOUD_ONLY if Path(path).name in evicted else HydrationState.LOCAL,7,0 if Path(path).name in evicted else 512)
    def evict(path):
        attempts.append(path.name)
        if path.name=='one.txt':raise RuntimeError('NSFileProviderErrorDomain Code=-2008 cannot evict')
        evicted.add(path.name)
    monkeypatch.setattr(release,'inspect_file',inspect)
    monkeypatch.setattr(release,'request_evict',evict)
    log=tmp_path/'log.jsonl'
    start.run(db,10,5_000_000_000,None,1,log,1,100_000,250,1)
    assert set(attempts)=={'one.txt','two.txt'}
    c=state.connect(db);rows={r['relative_path']:r for r in c.execute('SELECT * FROM files')}
    assert all(r['transfer_state']=='COMPLETE' and r['verified']==1 and r['last_error'] is None for r in rows.values())
    assert rows['one.txt']['local_released']==0 and '-2008' in rows['one.txt']['release_error']
    assert rows['two.txt']['local_released']==1 and rows['two.txt']['release_error'] is None
    assert c.execute('SELECT status FROM migrations').fetchone()[0]=='COMPLETE'
    c.close()
    events=[json.loads(x) for x in log.read_text().splitlines()]
    assert all(r['migration_id']==mid for r in events)
    assert 'error' not in {r['event'] for r in events}
    assert 'release_warning' in {r['event'] for r in events}
    assert events[-1]['event']=='migration_complete' and events[-1]['cleanup_warnings']==1
    snapshot=gui.snapshot(db)
    assert snapshot['cleanup_warnings']==1 and snapshot['verified_files']==2 and snapshot['failed_files']==0


def test_release_never_evicts_unverified_or_legacy_ds_store(tmp_path,monkeypatch):
    source,db,mid=make_plan(tmp_path,('one.txt','legacy.txt'))
    c=state.connect(db)
    c.execute("UPDATE files SET transfer_state='COMPLETE',verified=0")
    c.execute("UPDATE files SET relative_path='.DS_Store',verified=1 WHERE relative_path='legacy.txt'")
    c.commit();c.close()
    monkeypatch.setattr(release,'request_evict',lambda p:pytest.fail('must not evict'))
    for name in ('one.txt','.DS_Store'):
        with pytest.raises(LookupError):release.release_specific(db,mid,name)
    assert start.eligible_releases(db)==[]
    c=state.connect(db)
    assert c.execute("SELECT COUNT(*) FROM files WHERE relative_path='.DS_Store'").fetchone()[0]==1
    c.close()


def test_schema_upgrade_adds_separate_nullable_release_error(tmp_path):
    source,db,mid=make_plan(tmp_path)
    c=state.connect(db);c.execute('ALTER TABLE files DROP COLUMN release_error');c.commit()
    state.create_schema(c)
    assert 'release_error' in {r['name'] for r in c.execute('PRAGMA table_info(files)')}
    assert c.execute('SELECT COUNT(*) FROM files WHERE release_error IS NULL').fetchone()[0]==2
    assert c.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]=='5'
    c.close()


def test_upload_timing_is_bounded_and_preserves_verification(remote,tmp_path,monkeypatch):
    import itertools
    ticks=itertools.count(step=0.05)
    monkeypatch.setattr(proton,"perf_counter",lambda:next(ticks))
    local=tmp_path/'file.txt';local.write_text('content');records=[]
    with proton.observe_safe_upload(lambda event,**data:records.append({'event':event,**data})):
        proton.safe_upload_promote(local,'/my-files/CloudMover',local.name)
    phases={'safe_upload_total','destination_preflight','cli_upload','temp_verification','rename_promote','final_verification'}
    assert len(records)==12
    assert {r['phase'] for r in records}==phases
    assert all(r['duration_ms']>=0 for r in records)
    assert next(r for r in records if r['phase']=='cli_upload' and r['status']=='completed')['duration_ms']==50
    assert all(r['exit_status']==0 for r in records if r['status']=='completed')
    assert all('stdout' not in r and 'stderr' not in r for r in records)
    assert '/my-files/CloudMover/file.txt' in remote.files


def test_upload_error_timing_keeps_failure(remote,tmp_path):
    local=tmp_path/'file.txt';local.write_text('content');records=[];remote.fail_rename=True
    with pytest.raises(proton.ProtonCLIError):
        with proton.observe_safe_upload(lambda event,**data:records.append(data)):
            proton.safe_upload_promote(local,'/my-files/CloudMover',local.name)
    assert any(r['phase']=='rename_promote' and r['status']=='error' and r['exit_status']==1 for r in records)
    assert records[-1]['phase']=='safe_upload_total' and records[-1]['status']=='error'
    assert not remote.files


def test_diagnostics_redacted_and_bounded():
    assert 'json-secret' not in safe_diagnostic('{"access_token": "json-secret"}')
    text=safe_diagnostic('Authorization: Bearer secret123 token=secret456 password=secret789 '+ 'x'*2000)
    assert len(text)<=1000
    assert not any(secret in text for secret in ('secret123','secret456','secret789'))


def test_current_file_progress_uses_activity_not_batch_row(dashboard_window):
    w,s=dashboard_window
    s['active']['transfer_state']='UPLOADING';s['active']['relative_path']='wrong-batch-row.txt'
    s['activity']={'stage':'upload','completed':2,'total':14,'file_index':3,'file_total':14,
                   'relative_path':'IMG_0423.JPG','logical_bytes':3_380_000,'phase':'cli_upload','phase_status':'started'}
    s['selected_files']=14;s['verified_files']=0
    w.refresh()
    assert 'Uploading file 3 of 14' in w.stage.text()
    assert 'IMG_0423.JPG' in w.current.text() and 'wrong-batch' not in w.current.text()
    assert '0 / 14 files verified' in w.overall.text()
    assert '14 files selected' in w.pick_summary.text()
    assert w.filebar.maximum()==0
    for phase,word in [('temp_verification','Verifying'),('rename_promote','Promoting'),('final_verification','Verifying')]:
        s['activity']['phase']=phase;w.refresh();assert word in w.stage.text()
    assert w.bar.value()==0


def test_cleanup_warning_is_not_transfer_failure(dashboard_window,monkeypatch):
    w,s=dashboard_window
    s['m'].update(status='COMPLETE',created_at='2026-09-07T00:00:00+00:00',updated_at='2026-09-07T00:01:00+00:00')
    s.update(cleanup_warnings=1,verified_files=6,failed_files=0,active=None)
    s['done']={'n':6,'b':600};s['released']={'n':5,'b':500}
    monkeypatch.setattr(type(w),'live_engine_pid',lambda self:None)
    w.refresh()
    assert 'Migration complete' in w.status.text()
    assert 'local cleanup warnings' in w.completion_title.text()
    assert not w.cleanup_notice.isHidden() and w.attention_title.isHidden()
    assert '5 / 6 local cache files freed' in w.completion_detail.text()
