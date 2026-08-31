
from src.cloudmover.state import initialize_migration, connect
from src.cloudmover.bulk_pipeline import select_byte_target_batch

def test_selected_tiny_and_large_pending_files_are_eligible(tmp_path):
    source=tmp_path/"src"; source.mkdir()
    (source/"tiny.txt").write_bytes(b"x"*9)
    (source/"medium.bin").write_bytes(b"m"*200)
    (source/"large.bin").write_bytes(b"L"*500)
    db=tmp_path/"m.db"
    initialize_migration(source,db,"/my-files")

    c=connect(db)
    mid=c.execute("SELECT id FROM migrations LIMIT 1").fetchone()["id"]
    c.execute("""UPDATE files
                 SET transfer_state='PENDING',selected=1,hydration_state='CLOUD_ONLY'
                 WHERE migration_id=?""",(mid,))
    c.commit(); c.close()

    # Deliberately pass bounds that would have excluded tiny/large files in the
    # old prototype. Product byte-target selection must ignore those exclusions.
    items=select_byte_target_batch(
        db,target_bytes=10_000,max_files=250,min_bytes=100,max_bytes=300
    )
    assert {x.relative_path for x in items}=={"tiny.txt","medium.bin","large.bin"}
