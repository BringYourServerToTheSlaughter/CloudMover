
from pathlib import Path

from src.cloudmover.state import connect, initialize_migration

def test_gui_is_qt_app():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "PySide6" in s
    assert "QTreeWidget" in s
    assert "Pause Migration" in s
    assert "Resume Migration" in s

def test_selection_is_locked_while_running():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.tree.setEnabled(False)" in s
    assert "Pause CloudMover before changing the migration selection" in s

def test_progress_is_selection_aware():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "selected_files" in s
    assert "selected_bytes" in s
    assert "/ {s[\"selected_files\"]:,} files verified" in s

def test_errors_are_first_class_and_actionable():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Needs attention" in s
    assert "failed_rows" in s
    assert "last_error" in s
    assert "Source iCloud files remain safe and are not deleted" in s
    assert "start or resume CloudMover to retry safe pending work" in s

def test_errors_use_existing_state_without_retry_button():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "transfer_state='FAILED'" in s
    assert "QPushButton(\"Retry" not in s
    assert "def retry" not in s

def test_setup_exposes_editable_proton_destination():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Proton destination" in s
    assert "dest_edit=QLineEdit(self.destination)" in s
    assert "destination_changed" in s
    assert "validate_proton_destination(self.destination)" in s

def test_system_check_prominently_confirms_selected_source():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.source_title=QLabel()" in s
    assert "self.source_path=QLabel()" in s
    assert "self.source_note=QLabel()" in s
    assert "Selected source:" in s
    assert "Full path:" in s
    assert "Custom source selected" in s
    assert "Default iCloud Drive root selected." in s

def test_setup_buttons_use_consistent_primary_secondary_styles():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "QPushButton#primaryButton" in s
    assert "QPushButton#secondaryButton" in s
    assert "QPushButton#newMigrationButton" in s
    assert 'choose.setObjectName("secondaryButton")' in s
    assert 'self.back.setObjectName("secondaryButton")' in s
    assert 'self.next.setObjectName("primaryButton")' in s

def test_window_startup_loads_selection_from_sqlite_only():
    s=Path("src/cloudmover/gui.py").read_text()
    init=s[s.index("        def __init__(self,db_path):"):s.index("        def pid_path")]
    assert "self.load_selection_tree(reconcile_live=False, include_live_directories=False)" in init
    assert "reconcile_migration_inventory" not in init
    assert "source_directories" not in init
    assert "walk_entries" not in init

def test_selection_tree_live_scans_are_opt_in():
    s=Path("src/cloudmover/gui.py").read_text()
    load=s[s.index("        def load_selection_tree"):s.index("        def apply_check")]
    assert "def load_selection_tree(self, reconcile_live=False, include_live_directories=False):" in load
    assert "if reconcile_live and status not in" in load
    assert "source_directories(Path(str(m[\"source\"]))) if include_live_directories else []" in load

def test_paused_migration_shows_separate_new_migration_action():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'self.new_btn=QPushButton("New Migration")' in s
    assert "self.new_btn.clicked.connect(self.new_migration)" in s
    assert 'self.resume_btn.setText("Resume Migration" if migration_has_started(self.db_path) else "Start Migration")' in s
    assert "self.new_btn.setVisible(True)" in s

def test_completed_migration_has_stable_completion_state():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.completion_title.show()" in s
    assert "files copied and verified" in s
    assert 'self.resume_btn.hide()' in s
    assert 'self.pause_btn.setText("Done")' in s
    assert "self.new_btn.setEnabled(True)" in s
    assert "No further action is required" in s

def test_new_migration_confirms_paused_history_is_preserved():
    s=Path("src/cloudmover/gui.py").read_text()
    block=s[s.index("        def new_migration"):s.index("        def _setup_finished")]
    assert "dialog=QMessageBox(self)" in block
    assert "existing iCloud and Proton files untouched." in block
    assert 'dialog.addButton("Start New Migration",QMessageBox.AcceptRole)' in block
    assert "self.setup_page=SetupDialog(self.db_path)" in block
    assert "DELETE FROM migrations" not in block
    assert "UPDATE migrations SET status='COMPLETE'" not in block

def test_new_migration_append_preserves_paused_history_and_becomes_active(tmp_path):
    db=tmp_path/"migration.db"
    first_source=tmp_path/"first"
    second_source=tmp_path/"second"
    first_source.mkdir()
    second_source.mkdir()
    (first_source/"old.txt").write_text("old")
    (second_source/"new.txt").write_text("new")

    first=initialize_migration(first_source,db,"/my-files/CloudMover/old")
    conn=connect(db)
    try:
        conn.execute(
            "UPDATE migrations SET status='PAUSED',created_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (first.migration_id,),
        )
        conn.commit()
    finally:
        conn.close()

    second=initialize_migration(second_source,db,"/my-files/CloudMover/new")

    conn=connect(db)
    try:
        first_row=conn.execute(
            "SELECT status,source,destination FROM migrations WHERE id=?",
            (first.migration_id,),
        ).fetchone()
        latest=conn.execute(
            "SELECT id,status,source,destination FROM migrations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        count=conn.execute("SELECT COUNT(*) n FROM migrations").fetchone()["n"]
    finally:
        conn.close()

    assert count==2
    assert first_row["status"]=="PAUSED"
    assert first_row["source"]==str(first_source.resolve())
    assert first_row["destination"]=="/my-files/CloudMover/old"
    assert latest["id"]==second.migration_id
    assert latest["status"]=="PLANNED"
    assert latest["destination"]=="/my-files/CloudMover/new"

def test_setup_scan_dispatches_initialize_migration_off_gui_thread():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "class MigrationScanWorker(QObject):" in s
    assert "progress=Signal(object)" in s
    assert "finished=Signal(object,object)" in s
    assert "failed=Signal(str)" in s
    assert "@Slot()" in s
    assert 'if not payload.get("final") and now-self.last_progress < 0.125:' in s
    assert "self.progress.emit(payload)" in s
    assert "summary=initialize_migration(self.source,self.db_path,self.destination,report,cancel_check=self.cancel_event.is_set)" in s
    assert "self.finished.emit(summary,self.latest_progress)" in s
    assert "self.scan_thread=QThread(self)" in s
    assert "self.scan_worker.moveToThread(self.scan_thread)" in s
    assert "self.scan_thread.started.connect(self.scan_worker.run)" in s
    assert "self.scan_worker.progress.connect(self.scan_progress)" in s

def test_setup_scan_disables_controls_and_prevents_duplicate_scan():
    s=Path("src/cloudmover/gui.py").read_text()
    scan=s[s.index("            if i==2:"):s.index("            self.accept()")]
    assert "if self.scan_running():" in scan
    assert "return" in scan
    assert "self.next.setEnabled(False); self.back.setEnabled(False)" in scan
    assert "Scanning {source_label(self.source)}" in scan
    assert "self.scan_bar.setRange(0,0)" in scan
    assert "self.scan_bar.show()" in scan
    assert "self.scan_thread.start()" in scan

def test_setup_scan_restores_controls_on_success_and_failure():
    s=Path("src/cloudmover/gui.py").read_text()
    success=s[s.index("        def scan_finished"):s.index("        def scan_failed")]
    failure=s[s.index("        def scan_failed"):s.index("    class Window")]
    assert "self.scan_thread=None" in success
    assert "self.scan_worker=None" in success
    assert "self.scan_summary={" in success
    assert "Scan complete" in success
    assert "self.scan_bar.hide()" in success
    assert "self.back.setEnabled(True)" in success
    assert "self.refresh_page()" in success
    assert "QTimer.singleShot(700,self.show_scan_review)" not in success
    review=s[s.index("        def show_scan_review"):s.index("        def scan_failed")]
    assert "self.pages.setCurrentIndex(3)" in review
    assert "QMessageBox.critical(self,\"Scan failed\",message)" in failure
    assert "self.scan_bar.hide()" in failure
    assert "self.next.setEnabled(True); self.back.setEnabled(True)" in failure

def test_setup_scan_shows_live_activity_without_known_percentage():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.scan_bar=QProgressBar()" in s
    assert "No files are being moved yet." in s
    assert "Cloud-only files are not downloaded during this scan." in s
    progress=s[s.index("        def scan_progress"):s.index("        def scan_finished")]
    assert "Now scanning:" in progress
    assert "Files:" in progress
    assert "Folders:" in progress
    assert "Logical bytes:" in progress
    assert "Cloud-only files:" in progress
    assert "Elapsed:" in progress
    assert "self.elide_path" in progress

def test_scan_complete_state_requires_review_migration_click():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'self.next.setText("Review Migration" if self.scan_summary else f"Scan {source_label(self.source)}")' in s
    scan=s[s.index("            if i==2:"):s.index("                self.next.setEnabled(False); self.back.setEnabled(False)")]
    assert "if self.scan_summary:" in scan
    assert "self.show_scan_review()" in scan
    success=s[s.index("        def scan_finished"):s.index("        def show_scan_review")]
    assert "Selected source folder:" in success
    assert "Proton destination:" in success
    assert "Files found:" in success
    assert "Folders found:" in success
    assert "Logical size:" in success
    assert "Cloud-only files found:" in success
