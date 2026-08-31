from pathlib import Path
from unittest.mock import patch

import pytest

from src.cloudmover.hydrate import hydrate_one, select_one_file
from src.cloudmover.providers.icloud import FileState, HydrationState
from src.cloudmover.state import connect, create_schema, get_summary, utc_now


def make_db(tmp_path: Path, source_file: Path, hydration='CLOUD_ONLY') -> Path:
    db = tmp_path / 'migration.db'
    conn = connect(db)
    create_schema(conn)
    now = utc_now()
    conn.execute(
        """INSERT INTO migrations(id,source,destination,source_provider,destination_provider,status,strategy,total_files,total_logical_bytes,staging_bytes,reserve_bytes,created_at,updated_at)
           VALUES('m1', ?, NULL, 'icloud', NULL, 'PLANNED', 'SMART_STAGING', 1, ?, 1, 1, ?, ?)""",
        (str(source_file.parent), source_file.stat().st_size, now, now),
    )
    conn.execute(
        """INSERT INTO files(migration_id,relative_path,source_path,logical_bytes,allocated_bytes,hydration_state,transfer_state,created_at,updated_at)
           VALUES('m1', ?, ?, ?, 0, ?, 'PENDING', ?, ?)""",
        (source_file.name, str(source_file), source_file.stat().st_size, hydration, now, now),
    )
    conn.commit(); conn.close()
    return db


def test_selects_cloud_only_pending_file(tmp_path):
    f = tmp_path / 'a.txt'; f.write_text('hello')
    db = make_db(tmp_path, f)
    item = select_one_file(db)
    assert item.relative_path == 'a.txt'


def test_hydrate_transitions_to_ready(tmp_path):
    f = tmp_path / 'a.txt'; f.write_text('hello')
    db = make_db(tmp_path, f)
    states = [
        FileState(HydrationState.CLOUD_ONLY, 5, 0),
        FileState(HydrationState.CLOUD_ONLY, 5, 0),
        FileState(HydrationState.LOCAL, 5, 4096),
        FileState(HydrationState.LOCAL, 5, 4096),
    ]
    with patch('src.cloudmover.hydrate.inspect_file', side_effect=states), \
         patch('src.cloudmover.hydrate.request_icloud_download'):
        hydrate_one(db, timeout_seconds=2, poll_seconds=0)
    summary = get_summary(db)
    assert summary.states['READY'] == 1


def test_timeout_marks_failed(tmp_path):
    f = tmp_path / 'a.txt'; f.write_text('hello')
    db = make_db(tmp_path, f)
    cloud = FileState(HydrationState.CLOUD_ONLY, 5, 0)
    with patch('src.cloudmover.hydrate.inspect_file', return_value=cloud), \
         patch('src.cloudmover.hydrate.request_icloud_download'), \
         patch('src.cloudmover.hydrate.time.monotonic', side_effect=[0, 10]):
        with pytest.raises(TimeoutError):
            hydrate_one(db, timeout_seconds=1, poll_seconds=0)
    summary = get_summary(db)
    assert summary.states['FAILED'] == 1
