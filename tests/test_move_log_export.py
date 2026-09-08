from pathlib import Path
import pytest
from src.cloudmover import state
from test_first_run_safety import qt_classes


@pytest.fixture
def paused_window(qt_classes,tmp_path,monkeypatch):
    app,scope=qt_classes
    source=tmp_path/'source';source.mkdir()
    for name in ('one.txt','two.txt'):(source/name).write_text('safe')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/Backup')
    c=state.connect(db);c.execute("UPDATE files SET attempts=1")
    c.execute("UPDATE files SET transfer_state='COMPLETE',verified=1 WHERE relative_path='one.txt'")
    c.execute("UPDATE migrations SET status='PAUSED'");c.commit();c.close()
    log=tmp_path/'cloudmover.jsonl';log.write_text('{"event":"test","relative_path":"one.txt"}\n')
    scope['default_log_path']=lambda:log
    monkeypatch.setattr(scope['Window'],'live_engine_pid',lambda self:None)
    w=scope['Window'](db);w.timer.stop();w.show();app.processEvents()
    yield w,app,scope,log
    monkeypatch.setattr(w,'live_engine_pid',lambda:None)
    w.close()


def test_reopened_paused_move_has_no_selection(paused_window,monkeypatch):
    w,app,scope,log=paused_window
    assert '4 · Move</b>' in w.steps.text()
    assert not w.picker.isVisible() and not w.search.isVisible() and not w.tree.isVisible()
    assert w.progress_card.isVisible() and w.stage.text()=='Paused / ready'
    assert '1 / 2 files verified' in w.overall.text()
    assert w.resume_btn.isVisible() and w.resume_btn.text()=='Resume Migration'
    assert not w.pause_btn.isVisible() and w.export_log_btn.isVisible()
    w.review_selection();w.secondary_action();w.refresh()
    assert not w.reviewing and not w.picker.isVisible()
    monkeypatch.setattr(w,'live_engine_pid',lambda:123);w.refresh()
    assert w.pause_btn.isVisible() and w.pause_btn.text()=='Pause Migration'
    assert not w.resume_btn.isVisible() and not w.picker.isVisible()


@pytest.mark.parametrize('mode',['copy','cancel','missing','unwritable','same'])
def test_export_log_copy_only(paused_window,tmp_path,monkeypatch,mode):
    w,app,scope,log=paused_window
    original=log.read_bytes();target=tmp_path/'export.jsonl';dialogs=[]
    def save(*args):
        dialogs.append(args)
        return ('' if mode=='cancel' else str(log if mode=='same' else target),'')
    monkeypatch.setattr(scope['QFileDialog'],'getSaveFileName',save)
    if mode=='missing':log.unlink()
    if mode=='unwritable':target=tmp_path/'missing-folder'/'export.jsonl'
    before=w.notice.text();w.export_log_btn.click()
    if mode=='copy':
        assert target.read_bytes()==original
        assert 'successfully' in w.notice.text() and 'filenames and paths' in w.notice.text()
        assert dialogs[0][2]=='CloudMover-0.20.0-log.jsonl'
    elif mode=='cancel':assert w.notice.text()==before and not target.exists()
    elif mode=='missing':assert not dialogs and 'No diagnostic log' in w.notice.text()
    elif mode=='unwritable':assert 'Could not export' in w.notice.text()
    else:assert 'original diagnostic log cannot be replaced' in w.notice.text()
    if mode!='missing':assert log.read_bytes()==original
    assert str(tmp_path) not in w.notice.text()
