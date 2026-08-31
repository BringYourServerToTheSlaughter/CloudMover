
from src.cloudmover.state import initialize_migration, connect
from src.cloudmover.bulk_pipeline import select_byte_target_batch
from src.cloudmover.start import eligible_releases

def make_db(tmp_path):
    source=tmp_path/"source"; source.mkdir()
    (source/"Wills").mkdir(); (source/"Photos").mkdir()
    (source/"Wills"/"a.bin").write_bytes(b"a"*50)
    (source/"Photos"/"wrong.mov").write_bytes(b"b"*90)
    (source/"Photos"/"old.mov").write_bytes(b"c"*80)
    db=tmp_path/"x.db"
    initialize_migration(source,db,"/my-files")
    c=connect(db)
    mid=c.execute("SELECT id FROM migrations LIMIT 1").fetchone()["id"]
    c.execute("UPDATE files SET hydration_state='CLOUD_ONLY', transfer_state='PENDING', selected=0 WHERE migration_id=?",(mid,))
    c.execute("UPDATE files SET selected=1 WHERE migration_id=? AND relative_path='Wills/a.bin'",(mid,))
    c.execute("""UPDATE files SET transfer_state='COMPLETE',verified=1,selected=0,local_released=0
                 WHERE migration_id=? AND relative_path='Photos/old.mov'""",(mid,))
    c.commit(); c.close()
    return db

def test_byte_target_never_selects_deselected_pending(tmp_path):
    db=make_db(tmp_path)
    items=select_byte_target_batch(db,target_bytes=250_000_000,max_files=250,min_bytes=1,max_bytes=100_000_000)
    assert [x.relative_path for x in items] == ["Wills/a.bin"]

def test_release_backlog_never_releases_deselected(tmp_path):
    db=make_db(tmp_path)
    assert eligible_releases(db) == []
