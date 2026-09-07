import json
from pathlib import Path
import pytest
from src.cloudmover import bulk_pipeline, state, gui
from src.cloudmover.providers import proton
from src.cloudmover.performance import summarize
from test_destination_regression import FakeProton, remote
from test_first_run_safety import qt_classes


def locals_for(tmp_path,count=3):
    files=[]
    for i in range(count):
        path=tmp_path/f"Family ({i}) - O'Brien 日本語.txt"
        path.write_bytes(f'disposable {i}'.encode())
        files.append((path,path.name))
    return files


def test_group_argv_count_and_independent_proof(remote,tmp_path):
    files=locals_for(tmp_path,14)
    parent='/my-files/CloudMover'
    results=proton.safe_upload_group(files,parent)
    assert len(results)==14 and len({r.node_id for r in results})==14
    assert len(remote.calls)==2*14+4
    upload=[c for c in remote.calls if c[2]=='upload']
    assert len(upload)==1
    assert upload[0][3:6]==['-f','rename','-t']
    assert len(upload[0][6:-1])==14 and upload[0][-1]==parent
    assert all(Path(p).name.startswith('.cloudmover-upload-') for p in upload[0][6:-1])
    first_rename=next(i for i,c in enumerate(remote.calls) if c[2]=='rename')
    assert sum(c[2]=='info' for c in remote.calls)==0
    assert sum(c[2]=='list' for c in remote.calls[:first_rename])==3
    for i,c in enumerate(remote.calls):
        if c[2]=='rename':
            assert remote.calls[i-1]==['proton-drive','filesystem','list','-j','-t','file',parent]
    assert remote.calls[-1]==['proton-drive','filesystem','list','-j','-t','file',parent]
    assert {r.name for r in results}=={name for _,name in files}


@pytest.mark.parametrize('failure',['missing','digest','size','uid','duplicate_uid','transport_error'])
def test_group_partial_success_fails_closed(remote,tmp_path,monkeypatch,failure):
    files=locals_for(tmp_path); original=remote.process
    def process(cmd,**kw):
        result=original(cmd,**kw)
        if cmd[2]=='upload':
            key=list(remote.files)[1]
            if failure=='missing':del remote.files[key]
            elif failure=='digest':remote.files[key]['activeRevision']['claimedDigests']['sha1']='bad'
            elif failure=='size':remote.files[key]['activeRevision']['claimedSize']=100000
            elif failure=='uid':remote.files[key].pop('uid')
            elif failure=='duplicate_uid':remote.files[key]['uid']=next(iter(remote.files.values()))['uid']
            else:return '', 'partial upload failed',1
        return result
    monkeypatch.setattr(proton,'_run_cli_process',process)
    with pytest.raises(RuntimeError):proton.safe_upload_group(files,'/my-files/CloudMover')
    assert not any(c[2]=='rename' for c in remote.calls)
    assert not any(p.endswith('/'+name) for p in remote.files for _,name in files)
    # Unknown/mismatched nodes are preserved; only previously proven temps cleaned.
    assert all(c[-1].startswith('/my-files/node-') for c in remote.calls if c[2]=='trash')


def test_collision_after_transport_is_not_cached(remote,tmp_path,monkeypatch):
    files=locals_for(tmp_path);parent='/my-files/CloudMover';original=remote.process
    def process(cmd,**kw):
        result=original(cmd,**kw)
        if cmd[2]=='upload':
            remote.files[parent+'/'+files[0][1]]={'name':files[0][1],'uid':'someone-else',
                'activeRevision':{'claimedSize':4,'claimedDigests':{'sha1':'other'}}}
        return result
    monkeypatch.setattr(proton,'_run_cli_process',process)
    with pytest.raises(RuntimeError,match='already contains'):
        proton.safe_upload_group(files,parent)
    assert remote.files[parent+'/'+files[0][1]]['uid']=='someone-else'
    assert not any(c[2]=='rename' for c in remote.calls)


@pytest.mark.parametrize('phase',['upload','rename'])
def test_group_interrupt_leaves_no_false_success(remote,tmp_path,monkeypatch,phase):
    files=locals_for(tmp_path);original=remote.process
    def process(cmd,**kw):
        if cmd[2]==phase:raise KeyboardInterrupt()
        return original(cmd,**kw)
    monkeypatch.setattr(proton,'_run_cli_process',process)
    with pytest.raises(KeyboardInterrupt):proton.safe_upload_group(files,'/my-files/CloudMover')
    assert not any(c[2]=='trash' for c in remote.calls)
    assert all('.cloudmover-upload-' in p for p in remote.files)


def test_groups_bounded_and_same_parent(tmp_path):
    entries=[(tmp_path/str(i),'/my-files/A' if i%2 else '/my-files/B',str(i)) for i in range(5000)]
    groups=list(proton.transport_groups(entries))
    assert sum(len(files) for _,files in groups)==5000
    assert all(len(files)<=32 for _,files in groups)
    assert all(int(name)%2 == (1 if parent.endswith('A') else 0) for parent,files in groups for _,name in files)
    with pytest.raises(ValueError):list(proton.transport_groups([(tmp_path/'a','x'*32000,'a')]))


def test_invalid_group_does_no_io(remote,tmp_path):
    files=locals_for(tmp_path)
    for invalid in (files*20,[(files[0][0],'../bad')],files+files,[(tmp_path,'folder')]):
        with pytest.raises(ValueError):proton.safe_upload_group(invalid,'/my-files/CloudMover')
    assert not remote.calls


@pytest.mark.parametrize('baseline,expected',[(True,121),(False,41)])
def test_fresh_14_file_migration_exact_count(remote,tmp_path,monkeypatch,baseline,expected):
    if baseline:
        def original_transport(files,parent,**kw):
            # The previous engine preflight plus the unchanged seven-call provider.
            for local,name in files:proton.assert_no_remote_file(parent+'/'+name)
            return [proton.safe_upload_promote(local,parent,name) for local,name in files]
        monkeypatch.setattr(proton,'safe_upload_group',original_transport)
    source=tmp_path/'SyncMeBro-2';source.mkdir();pics=source/'Pics';pics.mkdir()
    locals_for(pics,14)
    db=tmp_path/'state.db';destination='/my-files/SyncMeBro-2'
    state.initialize_migration(source,db,destination)
    c=state.connect(db);c.execute("UPDATE files SET transfer_state='READY',selected=1,hydration_state='LOCAL'");c.commit();c.close()
    assert bulk_pipeline.run_bulk(db,count=14,destination=destination)==14
    # fresh root: 4; new Pics folder: 4; transport/proofs:32; tree verification:1
    assert len(remote.calls)==expected
    assert sum(c[2]=='upload' for c in remote.calls)==(14 if baseline else 1)
    c=state.connect(db)
    assert c.execute("SELECT COUNT(*) FROM files WHERE transfer_state='COMPLETE' AND verified=1").fetchone()[0]==14
    c.close()
    assert not any('/my-files/CloudMover' in arg for call in remote.calls for arg in call)


def test_source_name_in_actual_window(qt_classes,tmp_path):
    app,scope=qt_classes
    source=tmp_path/'SyncMeBro-2';source.mkdir();(source/'one.txt').write_text('a')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/SyncMeBro-2')
    window=scope['Window'](db)
    try:
        assert window.route.text().startswith('SyncMeBro-2   →   Proton Drive')
        assert window.tree.topLevelItem(0).text(0)=='SyncMeBro-2'
        assert gui.source_label(gui.default_icloud_path())=='iCloud Drive'
        assert 'file 0' not in gui.current_upload_text({'phase':'cli_upload','detail':'14 files'})
    finally:window.timer.stop();window.close()


def test_process_events_and_summary(remote,tmp_path):
    files=locals_for(tmp_path);rows=[]
    def emit(event,**data):rows.append({'event':event,'migration_id':'m',**data})
    emit('upload_start',ts='2026-01-01T00:00:00+00:00',files=3,logical_bytes=36)
    with proton.observe_cli(emit),proton.observe_safe_upload(emit):
        proton.safe_upload_group(files,'/my-files/CloudMover',observe_file=lambda local,event,**data:emit(event,relative_path=local.name,**data))
    emit('upload_complete',duration_ms=1000)
    result=summarize(rows,'m')
    assert result['cli_process_count']==10
    assert result['file_attempts']==3 and result['upload_wall_seconds']==1
    assert result['average_successful_file_latency_seconds'] is not None
    assert result['cli_failures']==0
    assert all('argv' not in r and 'stdout' not in r and 'stderr' not in r for r in rows)
    assert summarize([], 'm')['cli_process_count'] is None
    rows.append({'event':'upload_start','migration_id':'m','ts':'2026-01-01T00:00:00+00:00'})
    assert summarize(rows,'m')['effective_logical_MB_per_second'] is None


@pytest.mark.parametrize('fault',['interrupt','mismatch','final_mismatch'])
def test_failed_group_database_and_recovery(remote,tmp_path,monkeypatch,fault):
    source=tmp_path/'source';source.mkdir();locals_for(source)
    db=tmp_path/'state.db';destination='/my-files/new migration'
    state.initialize_migration(source,db,destination)
    c=state.connect(db);c.execute("UPDATE files SET transfer_state='READY',selected=1,hydration_state='LOCAL'");c.commit();c.close()
    original=remote.process
    def process(cmd,**kw):
        result=original(cmd,**kw)
        if cmd[2]=='upload':
            if fault=='interrupt':raise KeyboardInterrupt()
            if fault=='mismatch':next(iter(remote.files.values()))['activeRevision']['claimedDigests']['sha1']='bad'
        if cmd[2]=='rename' and fault=='final_mismatch':
            remote.files[cmd[-2].rsplit('/',1)[0]+'/'+cmd[-1]]['activeRevision']['claimedDigests']['sha1']='bad'
        return result
    monkeypatch.setattr(proton,'_run_cli_process',process)
    with pytest.raises((RuntimeError,KeyboardInterrupt)):
        bulk_pipeline.run_bulk(db,count=3,destination=destination)
    from src.cloudmover.start import normalize_transient_states_for_pause
    normalize_transient_states_for_pause(db)
    c=state.connect(db)
    rows=c.execute('SELECT transfer_state,verified FROM files').fetchall();c.close()
    assert all(r['transfer_state']=='READY' and not r['verified'] for r in rows)
    if fault=='final_mismatch':
        # Preserve the promoted node on failed proof; resume refuses to overwrite it.
        monkeypatch.setattr(proton,'_run_cli_process',original)
        previous=dict(remote.files)
        with pytest.raises(RuntimeError,match='already contains'):
            bulk_pipeline.run_bulk(db,count=3,destination=destination)
        assert remote.files==previous


def test_group_copy_fallback_respects_reserve(remote,tmp_path,monkeypatch):
    from types import SimpleNamespace
    files=locals_for(tmp_path)
    monkeypatch.setattr(proton.os,'link',lambda *a: (_ for _ in ()).throw(OSError('no hard links')))
    monkeypatch.setattr(proton.shutil,'disk_usage',lambda p:SimpleNamespace(free=5_000_000_000))
    with pytest.raises(RuntimeError,match='reserve'):proton.safe_upload_group(files,'/my-files/CloudMover')
    assert not any(c[2]=='upload' for c in remote.calls)


def test_cli_telemetry_timeout_and_context_restart(monkeypatch):
    rows=[]
    monkeypatch.setattr(proton,'cli',lambda:'proton-drive')
    monkeypatch.setattr(proton,'_run_cli_process',lambda *a,**kw: (_ for _ in ()).throw(TimeoutError()))
    with proton.observe_cli(lambda event,**data:rows.append(data)):
        with pytest.raises(TimeoutError):proton.run(['filesystem','info','/my-files/private name'])
    assert len(rows)==1 and rows[0]['error_type']=='TimeoutError' and rows[0]['exit_status'] is None
    monkeypatch.setattr(proton,'_run_cli_process',lambda *a,**kw:('ok','',0))
    assert proton.run(['filesystem','info','/my-files/private name'])=='ok'
    assert len(rows)==1  # observer lifetime ended; no stale log sink
