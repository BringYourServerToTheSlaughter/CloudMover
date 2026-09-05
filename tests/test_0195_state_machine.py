
from pathlib import Path

from src.cloudmover.state import (
    connect, initialize_migration, recover_stale_runtime_state,
)

def test_stale_running_is_paused_and_transients_requeued(tmp_path):
    source=tmp_path/"source"; source.mkdir()
    for name in ("h.txt","u.txt","v.txt","c.txt"):
        (source/name).write_text(name)
    db=tmp_path/"migration.db"
    initialize_migration(source,db,"/my-files")

    c=connect(db)
    mid=c.execute("SELECT id FROM migrations ORDER BY created_at DESC LIMIT 1").fetchone()["id"]
    c.execute("UPDATE migrations SET status='RUNNING' WHERE id=?",(mid,))
    for name,state in [
        ("h.txt","HYDRATING"),("u.txt","UPLOADING"),
        ("v.txt","VERIFYING"),("c.txt","COMPLETE"),
    ]:
        c.execute(
            "UPDATE files SET transfer_state=? WHERE migration_id=? AND relative_path=?",
            (state,mid,name),
        )
    c.commit(); c.close()

    recovered=recover_stale_runtime_state(db)
    assert recovered=={"HYDRATING":1,"UPLOADING":1,"VERIFYING":1}

    c=connect(db)
    assert c.execute("SELECT status FROM migrations WHERE id=?",(mid,)).fetchone()["status"]=="PAUSED"
    states={r["relative_path"]:r["transfer_state"] for r in c.execute(
        "SELECT relative_path,transfer_state FROM files WHERE migration_id=?",(mid,)
    )}
    c.close()
    assert states["h.txt"]=="PENDING"
    assert states["u.txt"]=="READY"
    assert states["v.txt"]=="READY"
    assert states["c.txt"]=="COMPLETE"

def test_recovery_is_idempotent(tmp_path):
    source=tmp_path/"source"; source.mkdir()
    (source/"x.txt").write_text("x")
    db=tmp_path/"migration.db"
    initialize_migration(source,db,"/my-files")
    assert recover_stale_runtime_state(db)=={"HYDRATING":0,"UPLOADING":0,"VERIFYING":0}
    assert recover_stale_runtime_state(db)=={"HYDRATING":0,"UPLOADING":0,"VERIFYING":0}

def test_gui_repairs_before_reconcile_and_launch():
    s=Path("src/cloudmover/gui.py").read_text()
    resume=s[s.index("def resume_migration"):s.index("def load_selection_tree")]
    assert resume.index("repair_stale_runtime_if_needed") < resume.index("reconcile_migration_inventory")
    assert resume.index("reconcile_migration_inventory") < resume.index("subprocess.Popen")

def test_gui_runtime_truth_uses_live_pid():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'status="RUNNING" if live else db_status' in s
    assert 'if not live and db_status=="RUNNING"' in s

def test_proton_inherits_worker_process_group():
    s=Path("src/cloudmover/providers/proton.py").read_text()
    assert "start_new_session=True" not in s
    assert "def _terminate_child" in s
    assert "proc.send_signal(first_signal)" in s

def test_worker_claims_pid_before_running_status():
    s=Path("src/cloudmover/start.py").read_text()
    block=s[s.index("recovered_startup=recover_stale_runtime_state"):s.index("log_event(",s.index("recovered_startup=recover_stale_runtime_state"))]
    assert block.index("write_engine_pid(db)") < block.index('set_migration_status(db,"RUNNING")')
