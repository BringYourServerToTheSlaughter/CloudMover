import copy
import json
from pathlib import Path
import pytest
from src.cloudmover import gui,state,bulk_pipeline
from src.cloudmover.providers import proton
from src.cloudmover.performance import summarize
from test_destination_regression import remote
from test_transport_performance import locals_for
from test_first_run_safety import qt_classes


@pytest.mark.parametrize('fault',['missing','digest','size','uid','parent','duplicate','missing_digest'])
def test_final_group_proves_each_file_and_identifies_failure(remote,tmp_path,monkeypatch,fault):
    files=locals_for(tmp_path);parent='/my-files/CloudMover';original=remote.process
    bad_path=parent+'/'+files[1][1]
    def process(cmd,**kw):
        result=original(cmd,**kw)
        if cmd[2]=='rename' and cmd[-1]==files[-1][1]:
            bad=remote.files[bad_path]
            if fault=='missing':del remote.files[bad_path]
            elif fault=='digest':bad['activeRevision']['claimedDigests']['sha1']='bad'
            elif fault=='size':bad['activeRevision']['claimedSize']+=1
            elif fault=='uid':bad['uid']='other-uid'
            elif fault=='parent':bad['parentUid']='other-parent'
            elif fault=='missing_digest':bad['activeRevision']['claimedDigests'].pop('sha1')
            else:
                remote.files[parent+'/duplicate-entry']={**bad}
        return result
    monkeypatch.setattr(proton,'_run_cli_process',process)
    with pytest.raises(RuntimeError,match=__import__('re').escape(files[1][1])):
        proton.safe_upload_group(files,parent)
    assert sum(c[2]=='rename' for c in remote.calls)==3
    assert not any(c[2]=='trash' for c in remote.calls)  # never trash a promoted final


def test_listing_missing_fields_falls_back_to_exact_info(remote,tmp_path,monkeypatch):
    files=locals_for(tmp_path);original=remote.process
    def process(cmd,**kw):
        result=original(cmd,**kw)
        if cmd[2]=='list' and result[2]==0:
            rows=json.loads(result[0])
            for r in rows:r.pop('uid',None);r['activeRevision']['claimedDigests']={}
            return json.dumps(rows),'',0
        return result
    monkeypatch.setattr(proton,'_run_cli_process',process)
    result=proton.safe_upload_group(files,'/my-files/CloudMover')
    assert len(result)==3 and all(r.node_id and r.sha1 for r in result)
    assert sum(c[2]=='info' for c in remote.calls)==6


def test_collision_between_promotions_is_checked_again(remote,tmp_path,monkeypatch):
    files=locals_for(tmp_path);original=remote.process;parent='/my-files/CloudMover'
    intruder={'name':files[1][1],'uid':'intruder','activeRevision':{'claimedSize':1,'claimedDigests':{'sha1':'other'}}}
    def process(cmd,**kw):
        result=original(cmd,**kw)
        if cmd[2]=='rename':remote.files[parent+'/'+files[1][1]]=copy.deepcopy(intruder)
        return result
    monkeypatch.setattr(proton,'_run_cli_process',process)
    with pytest.raises(RuntimeError,match='already contains'):proton.safe_upload_group(files,parent)
    assert sum(c[2]=='rename' for c in remote.calls)==1
    assert remote.files[parent+'/'+files[1][1]]==intruder


def test_collision_after_last_check_rename_rejection_is_fatal(remote,tmp_path,monkeypatch):
    files=locals_for(tmp_path);original=remote.process;parent='/my-files/CloudMover'
    intruder={'name':files[0][1],'uid':'intruder','activeRevision':{'claimedSize':1,'claimedDigests':{'sha1':'other'}}}
    def process(cmd,**kw):
        if cmd[2]=='rename':remote.files[parent+'/'+files[0][1]]=copy.deepcopy(intruder)
        return original(cmd,**kw)  # SDK rename contract rejects occupied names.
    monkeypatch.setattr(proton,'_run_cli_process',process)
    with pytest.raises(proton.ProtonCLIError,match='already exists'):proton.safe_upload_group(files,parent)
    assert remote.files[parent+'/'+files[0][1]]==intruder
    assert sum(c[2]=='rename' for c in remote.calls)==1


def test_group_final_uid_proof_before_complete(remote,tmp_path,monkeypatch):
    source=tmp_path/'SyncMeBro-2';source.mkdir();pics=source/'Pics';pics.mkdir();files=locals_for(pics,13)
    db=tmp_path/'state.db';destination='/my-files/SyncMeBro-2'
    state.initialize_migration(source,db,destination)
    c=state.connect(db);c.execute("UPDATE files SET selected=1,hydration_state='LOCAL',transfer_state='READY'");c.commit();c.close()
    original=remote.process; observed=False
    def process(cmd,**kw):
        nonlocal observed
        if cmd[2]=='list' and sum(c[2]=='rename' for c in remote.calls)==13 and not observed:
            c=state.connect(db)
            assert c.execute('SELECT COUNT(*) FROM files WHERE verified=1 OR local_released=1').fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM files WHERE transfer_state='UPLOADING'").fetchone()[0]==13
            c.close();observed=True
        return original(cmd,**kw)
    monkeypatch.setattr(proton,'_run_cli_process',process)
    assert bulk_pipeline.run_bulk(db,count=13,destination=destination)==13
    assert observed and len(remote.calls)==39
    c=state.connect(db)
    assert c.execute("SELECT COUNT(*) FROM files WHERE verified=1 AND transfer_state='COMPLETE'").fetchone()[0]==13
    c.close()


def report_data(warnings=0):
    return dict(m={'status':'COMPLETE','destination':'/my-files/SyncMeBro-2'},
                done={'n':13,'b':50_828_655},selected_files=13,verified_files=13,
                failed_files=0,cleanup_warnings=warnings)


@pytest.mark.parametrize('warnings',[0,2])
def test_completion_dialog_actual_widgets(qt_classes,warnings):
    app,scope=qt_classes
    d=scope['CompletionDialog'](report_data(warnings));d.show();app.processEvents()
    try:
        assert d.heading.text()==('Completed with warnings' if warnings else 'Migration complete')
        assert d.icon.text()==('!' if warnings else '✓')
        assert d.warning.isVisible()==bool(warnings)
        assert d.done_button.text()=='Done'
        assert d.summary.text()=='13 files successfully copied and verified'
        assert d.transferred.text()=='50.83 MB transferred'
        assert d.destination.text()=='Proton Drive → SyncMeBro-2'
        assert '\\n' not in '\n'.join(w.text() for w in (d.heading,d.summary,d.destination,d.warning,d.safety))
        assert d.done_button.geometry().bottom() < d.height()
        assert d.grab().save('/tmp/cloudmover-completion-'+('warnings' if warnings else 'success')+'.png')
        d.done_button.click();assert d.result()==d.DialogCode.Accepted
    finally:d.close()


def test_report_does_not_claim_unfinished_migration_complete():
    for key,value in [('verified_files',12),('failed_files',1)]:
        s=report_data();s[key]=value
        assert gui.completion_dialog_content(s)['title']=='Migration progress'
    s=report_data();s['m']['status']='RUNNING'
    assert gui.completion_dialog_content(s)['title']=='Migration progress'


def test_actual_selection_verification_and_scan_labels(qt_classes,tmp_path):
    app,scope=qt_classes
    source=tmp_path/'SyncMeBro-2';source.mkdir();locals_for(source,13)
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/SyncMeBro-2')
    c=state.connect(db);c.execute('UPDATE files SET selected=1');c.commit();c.close()
    w=scope['Window'](db)
    try:
        assert w.pick_summary.text().startswith('13 files selected')
        assert w.overall.text().startswith('0 / 13 files verified')
        assert w.tree.topLevelItem(0).text(0)=='SyncMeBro-2'
        assert w.route.text().startswith('SyncMeBro-2')
        c=state.connect(db);c.execute("UPDATE files SET transfer_state='COMPLETE',verified=1 WHERE rowid IN (SELECT rowid FROM files LIMIT 5)");c.commit();c.close();w.refresh()
        assert w.overall.text().startswith('5 / 13 files verified')
        assert w.pick_summary.text().startswith('13 files selected')
    finally:w.timer.stop();w.close()
    d=scope['SetupDialog'](tmp_path/'new.db');d.source=source;d.pages.setCurrentIndex(2);d.refresh_page()
    try:
        assert d.scan_title.text()=='Scan SyncMeBro-2'
        assert d.next.text()=='Scan SyncMeBro-2'
    finally:d.close()


def test_command_summary_scopes_and_unknown_startup():
    rows=[{'event':'cli_process','command':'filesystem info','duration_ms':9000},
          {'event':'upload_start','files':13,'logical_bytes':50_828_655,'ts':'2026-01-01T00:00:00Z'},
          {'event':'cli_process','command':'filesystem upload','duration_ms':3000},
          {'event':'cli_process','command':'filesystem list','duration_ms':1000},
          {'event':'cli_process','command':'filesystem list','duration_ms':2000},
          {'event':'upload_complete','duration_ms':7000},
          {'event':'verification_start'},
          {'event':'cli_process','command':'filesystem list','duration_ms':2000},
          {'event':'verification_complete'}]
    result=summarize([dict(r,migration_id='m') for r in rows],'m')
    assert result['cli_process_count']==5
    assert result['upload_commands_per_file']==3/13
    assert result['commands_upload_pipeline']['filesystem list']['calls']==2
    assert result['commands_upload_pipeline']['filesystem list']['average_seconds']==1.5
    assert result['commands_upload_pipeline']['filesystem list']['percent_upload_wall']==pytest.approx(300/7)
    assert result['commands_batch_verification']['filesystem list']['total_seconds']==2
    assert result['outside_cli_seconds']==1
    assert result['process_startup_seconds'] is None and result['api_only_seconds'] is None
