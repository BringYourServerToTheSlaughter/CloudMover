
from pathlib import Path
from src.cloudmover.state import connect, create_schema
from src.cloudmover.engine import free_bytes

def test_engine_free_bytes_uses_real_migration_schema(tmp_path):
    db=tmp_path/"m.db"
    source=tmp_path/"src"
    source.mkdir()
    c=connect(db)
    try:
        create_schema(c)
        c.execute(
            """INSERT INTO migrations(
                id,source,destination,source_provider,destination_provider,status,strategy,
                total_files,total_logical_bytes,staging_bytes,reserve_bytes,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("m1",str(source),None,"icloud",None,"PLANNED","SMART_STAGING",
             0,0,0,0,"2026-01-01T00:00:00+00:00","2026-01-01T00:00:00+00:00")
        )
        c.commit()
    finally:
        c.close()
    assert free_bytes(db) >= 0
