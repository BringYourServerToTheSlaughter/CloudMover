import pytest
from pathlib import Path
from test_move_log_export import paused_window
from test_first_run_safety import qt_classes
from src.cloudmover import state


@pytest.mark.parametrize("repeats",[1,6])
def test_expanded_details_do_not_compress_status(paused_window,repeats):
    w,app,scope,log=paused_window
    w.engine_events.failure=lambda mid:'Destination identity changed. Stopped without writing to it. ' * repeats
    w.refresh();app.processEvents()
    before=w.progress_card.height()
    w.safety_toggle.click();app.processEvents()
    assert w.safety_details.isVisible()
    assert w.progress_card.height()>=before
    for label in (w.stage,w.current,w.attention,w.safety_details):
        assert label.height()>=max(label.minimumSizeHint().height(),label.heightForWidth(label.width()))
    assert w.main_scroll.widget().height()>=w.main_scroll.viewport().height()
    w.safety_toggle.click();app.processEvents()
    assert not w.safety_details.isVisible()
    assert w.resume_btn.isVisible() and w.new_btn.isVisible()
    assert w.resume_btn.mapTo(w,w.resume_btn.rect().bottomLeft()).y()<w.height()
    assert w.progress_card.height()<=before+20


@pytest.mark.parametrize('accept',[False,True])
def test_new_migration_confirmation_preserves_history(paused_window,monkeypatch,accept):
    w,app,scope,log=paused_window
    c=state.connect(w.db_path)
    before=list(c.iterdump());c.close()
    old=w.setup_page
    def confirm(dialog):
        assert dialog.text()=='Start a new migration?'
        assert 'existing iCloud and Proton files untouched' in dialog.informativeText()
        buttons={b.text():b for b in dialog.buttons()}
        assert set(buttons)=={'Cancel','Start New Migration'}
        assert dialog.defaultButton()==buttons['Cancel']
        buttons['Start New Migration' if accept else 'Cancel'].click()
        return 0
    monkeypatch.setattr(scope['QMessageBox'],'exec',confirm)
    assert w.new_btn.isVisible() and w.new_btn.text()=='New Migration…'
    assert w.resume_btn.isVisible() and w.resume_btn.text()=='Resume Migration'
    w.new_btn.click();app.processEvents()
    assert (w.setup_page is not old)==accept
    if accept:
        assert w.root_stack.currentWidget()==w.setup_page
        assert not w.setup_page.consent.isChecked()
        assert w.setup_page.scan_summary is None
    c=state.connect(w.db_path);assert list(c.iterdump())==before;c.close()


def test_missing_source_and_stale_setup_cannot_trap_new_migration(paused_window,monkeypatch,tmp_path):
    w,app,scope,log=paused_window
    c=state.connect(w.db_path);old_source=Path(c.execute('SELECT source FROM migrations').fetchone()[0]);before=list(c.iterdump());c.close()
    # Model externally removed source, then forbid any remote lookup or mutation.
    for file in old_source.iterdir():file.unlink()
    old_source.rmdir()
    scope['preflight_destination']=lambda *a:pytest.fail('New Migration must not access Proton')
    fresh=tmp_path/'fresh-default';fresh.mkdir()
    scope['default_icloud_path']=lambda:fresh
    w.setup_page.source=old_source
    w.setup_page.destination_error='stale collision'
    w.setup_page.dest_edit.setText('/my-files/Stale')
    w.setup_page.consent.setChecked(True)
    w.engine_events.failure=lambda mid:'The Proton destination identity changed.'
    w.refresh();app.processEvents()
    def confirm(dialog):
        next(b for b in dialog.buttons() if b.text()=='Start New Migration').click()
        return 0
    monkeypatch.setattr(scope['QMessageBox'],'exec',confirm)
    w.new_btn.click();app.processEvents();w.refresh();app.processEvents()
    assert w.root_stack.currentWidget()==w.setup_page
    assert w.setup_page.pages.currentIndex()==0
    assert w.setup_page.source==fresh and w.setup_page.destination_error==''
    assert 'Stale' not in w.setup_page.dest_edit.text()
    assert not w.setup_page.consent.isChecked() and w.setup_page.scan_summary is None
    c=state.connect(w.db_path);assert list(c.iterdump())==before;c.close()
    assert not old_source.exists()
