
from src.cloudmover.state import connect, create_schema
from src.cloudmover.gui import build_tree_index, set_selection_prefix

def seed(db,tmp_path):
    c=connect(db)
    try:
        create_schema(c)
        now="2026-01-01T00:00:00+00:00"
        c.execute("""INSERT INTO migrations(
            id,source,destination,source_provider,destination_provider,status,strategy,
            total_files,total_logical_bytes,staging_bytes,reserve_bytes,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("m",str(tmp_path),None,"icloud","proton","PAUSED","SMART_STAGING",3,60,0,0,now,now))
        data=[("Photos/a.jpg",10,"PENDING"),("Photos/b.jpg",20,"COMPLETE"),("Docs/c.pdf",30,"PENDING")]
        for rel,size,state in data:
            c.execute("""INSERT INTO files(
                migration_id,relative_path,source_path,logical_bytes,allocated_bytes,
                hydration_state,transfer_state,created_at,updated_at,selected
            ) VALUES (?,?,?,?,?,?,?,?,?,1)""",
            ("m",rel,str(tmp_path/rel),size,0,"CLOUD_ONLY",state,now,now))
        c.commit()
    finally:c.close()

def test_folder_selection_preserves_complete_history(tmp_path):
    db=tmp_path/"m.db"; seed(db,tmp_path)
    changed=set_selection_prefix(db,"Photos",False)
    assert changed==1
    c=connect(db)
    try:
        rows={r["relative_path"]:r["selected"] for r in c.execute("SELECT relative_path,selected FROM files")}
    finally:c.close()
    assert rows["Photos/a.jpg"]==0
    assert rows["Photos/b.jpg"]==1

def test_tree_index_aggregates(tmp_path):
    rows=[
        {"relative_path":"Photos/a.jpg","logical_bytes":10,"selected":1,"transfer_state":"PENDING"},
        {"relative_path":"Docs/c.pdf","logical_bytes":30,"selected":0,"transfer_state":"PENDING"},
    ]
    tree=build_tree_index(rows)
    assert tree["total"]==2
    assert tree["selected"]==1
    assert tree["bytes"]==40
