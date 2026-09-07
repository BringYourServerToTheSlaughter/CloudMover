import json
from test_first_run_safety import qt_classes, dashboard_window
from src.cloudmover.event_log import MigrationEventReader, friendly_engine_failure
from src.cloudmover.start import log_event

ERROR='Proton CLI failed (exit 1) running: ["filesystem", "create-folder", "/my-files/CloudMover/iCloud Migration - Sep 06, 2026 at 22-36-40", "86fb19fd-7bca-416b-9e33-659699f6dc4d"] | stderr: Node not found: iCloud Migration - Sep 06, 2026 at 22-36-40'


def test_fatal_engine_error_is_visible_without_failed_files(dashboard_window,tmp_path,monkeypatch):
    w,s=dashboard_window
    path=tmp_path/'events.jsonl';s['m']['id']='migration-a';s['m']['status']='PAUSED'
    log_event(path,'start',migration_id='migration-a')
    log_event(path,'error',migration_id='migration-a',error=ERROR)
    w.engine_events=MigrationEventReader(path)
    monkeypatch.setattr(type(w),'live_engine_pid',lambda self:None)
    w.refresh()
    assert 'Migration stopped' in w.status.text()
    assert w.stage.text()=='Stopped due to an error'
    assert w.filebar.isHidden() and w.fileprogress.isHidden()
    assert not w.attention_title.isHidden() and not w.error_details.isHidden()
    assert 'parent folder' in w.current.text()
    assert 'filesystem' not in w.current.text() and '86fb19fd' not in w.current.text()
    assert w.engine_failure_details==ERROR
    assert w.resume_btn.isEnabled()
    log_event(path,'start',migration_id='migration-a')
    monkeypatch.setattr(type(w),'live_engine_pid',lambda self:123)
    w.refresh()
    assert 'Hydrating' in w.stage.text()
    assert w.attention_title.isHidden() and w.error_details.isHidden()


def test_error_reader_scopes_legacy_events_partial_lines_and_retry(tmp_path):
    path=tmp_path/'log.jsonl';reader=MigrationEventReader(path)
    path.write_text(json.dumps({'event':'start','migration_id':'a'})+'\n'+json.dumps({'event':'error','error':ERROR})+'\n')
    assert reader.failure('a')==ERROR and reader.failure('b') is None
    with path.open('a') as f:f.write('{"event":"start","migration_id":"a"}')
    assert reader.failure('a')==ERROR
    with path.open('a') as f:f.write('\n')
    assert reader.failure('a') is None
    log_event(path,'error',migration_id='b',error='Other migration failure')
    assert reader.failure('a') is None
    assert reader.failure('b')=='Other migration failure'


def test_friendly_error_has_actual_reason_without_raw_command():
    assert 'required parent folder' in friendly_engine_failure(ERROR)
    assert '86fb19fd' not in friendly_engine_failure(ERROR)
    assert 'Permission denied' in friendly_engine_failure('Proton CLI failed running: giant argv | stderr: Permission denied')
