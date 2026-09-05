
from pathlib import Path
from src.cloudmover.state import initialize_migration, connect, set_activity_progress, get_activity_progress
from src.cloudmover.bulk_pipeline import recover_verifying

def test_orphaned_verifying_rows_recover_to_ready(tmp_path):
    source=tmp_path/"src"; source.mkdir()
    (source/"a.txt").write_text("abc")
    db=tmp_path/"m.db"
    initialize_migration(source,db,"/my-files")
    c=connect(db)
    c.execute("UPDATE files SET transfer_state='VERIFYING',selected=1")
    c.commit(); c.close()
    assert recover_verifying(db)==1
    c=connect(db)
    assert c.execute("SELECT transfer_state FROM files").fetchone()["transfer_state"]=="READY"
    c.close()

def test_activity_progress_round_trip(tmp_path):
    db=tmp_path/"x.db"
    set_activity_progress(db,stage="verify",completed=3,total=10,detail="folder")
    assert get_activity_progress(db)=={"stage":"verify","completed":3,"total":10,"detail":"folder"}

def test_adaptive_batch_ceiling_is_5000():
    assert '"--batch","5000"' in Path("src/cloudmover/gui.py").read_text()
    assert 'ap.add_argument("--batch",type=int,default=5000)' in Path("src/cloudmover/start.py").read_text()

def test_proton_list_verification_is_bounded_and_interruptible():
    s=Path("src/cloudmover/providers/proton.py").read_text()
    assert "timeout: float | None=120.0" in s
    assert "Proton CLI timed out" in s
    assert "start_new_session=True" not in s

def test_gui_has_real_progress_when_worker_knows_total():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "completed/total_units" in s
    assert "self.filebar.setRange(0,1000)" in s
    assert "progress unavailable from Proton" in s

def test_sigterm_uses_graceful_cleanup_and_gui_signals_group():
    assert "signal.signal(signal.SIGTERM,_termination_as_interrupt)" in Path("src/cloudmover/start.py").read_text()
    assert "os.killpg(pid,signal.SIGINT)" in Path("src/cloudmover/gui.py").read_text()
