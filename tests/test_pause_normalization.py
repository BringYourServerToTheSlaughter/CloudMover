
from src.cloudmover.state import connect, create_schema
from src.cloudmover.start import normalize_transient_states_for_pause

def test_pause_normalizes_transient_states(tmp_path):
    db=tmp_path/"m.db"
    c=connect(db)
    try:
        create_schema(c)
        now="2026-01-01T00:00:00+00:00"
        c.execute("""INSERT INTO migrations(
            id,source,destination,source_provider,destination_provider,status,strategy,
            total_files,total_logical_bytes,staging_bytes,reserve_bytes,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("m",str(tmp_path),None,"icloud","proton","RUNNING","SMART_STAGING",3,3,0,0,now,now))
        for i,state in enumerate(("HYDRATING","UPLOADING","VERIFYING")):
            c.execute("""INSERT INTO files(
                migration_id,relative_path,source_path,logical_bytes,allocated_bytes,
                hydration_state,transfer_state,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("m",f"f{i}",str(tmp_path/f"f{i}"),1,0,"LOCAL",state,now,now))
        c.commit()
    finally:
        c.close()

    counts=normalize_transient_states_for_pause(db)
    assert counts=={"HYDRATING":1,"UPLOADING":1,"VERIFYING":1}

    c=connect(db)
    try:
        rows=c.execute(
            "SELECT relative_path,transfer_state FROM files WHERE migration_id='m'"
        ).fetchall()
        states={r["relative_path"]:r["transfer_state"] for r in rows}
    finally:
        c.close()

    assert states["f0"]=="PENDING"
    assert states["f1"]=="READY"
    assert states["f2"]=="READY"
