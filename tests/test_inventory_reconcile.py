
from src.cloudmover.state import initialize_migration, connect, reconcile_migration_inventory

def test_reconcile_adds_new_file_unselected_and_removes_missing_unfinished(tmp_path):
    source=tmp_path/"src"; source.mkdir()
    old=source/"old.txt"; old.write_text("old")
    db=tmp_path/"m.db"
    initialize_migration(source,db,"/my-files")

    old.unlink()
    (source/"new.txt").write_text("new")
    changes=reconcile_migration_inventory(db)
    assert changes["added"]==1
    assert changes["removed"]==1

    c=connect(db)
    rows=c.execute("SELECT relative_path,selected FROM files").fetchall()
    c.close()
    assert [(r["relative_path"],r["selected"]) for r in rows]==[("new.txt",0)]

def test_reconcile_preserves_completed_history(tmp_path):
    source=tmp_path/"src"; source.mkdir()
    f=source/"done.txt"; f.write_text("done")
    db=tmp_path/"m.db"
    initialize_migration(source,db,"/my-files")
    c=connect(db)
    c.execute("UPDATE files SET transfer_state='COMPLETE',verified=1")
    c.commit(); c.close()
    f.unlink()
    reconcile_migration_inventory(db)
    c=connect(db)
    assert c.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]==1
    c.close()
