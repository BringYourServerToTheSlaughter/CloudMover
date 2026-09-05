import sqlite3
from pathlib import Path

import pytest

from src.cloudmover.providers.icloud import FileState, HydrationState
from src.cloudmover.state import (
    TransferState,
    get_summary,
    initialize_migration,
    transition_file,
)


def test_initialize_migration_persists_manifest(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    (source / "nested").mkdir()
    (source / "nested" / "b.txt").write_text("world")
    db = tmp_path / "state.db"

    summary = initialize_migration(source, db)

    assert db.exists()
    assert summary.files == 2
    assert summary.states["PENDING"] == 2

    with sqlite3.connect(db) as conn:
        paths = [row[0] for row in conn.execute("SELECT relative_path FROM files ORDER BY relative_path")]
    assert paths == ["a.txt", "nested/b.txt"]


def test_initialize_migration_reports_optional_scan_progress(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    (source / "nested").mkdir()
    (source / "nested" / "b.txt").write_text("world")
    db = tmp_path / "state.db"
    progress = []

    def fake_inspect(path, *, stat_result=None):
        return FileState(HydrationState.CLOUD_ONLY, stat_result.st_size, 0)

    monkeypatch.setattr("src.cloudmover.state.inspect_file", fake_inspect)

    summary = initialize_migration(source, db, progress_callback=progress.append)

    assert summary.files == 2
    assert progress
    assert progress[-1]["files"] == 2
    assert progress[-1]["folders"] == 1
    assert progress[-1]["logical_bytes"] == 10
    assert progress[-1]["cloud_only"] == 2
    assert progress[-1]["relative_path"] == ""
    assert progress[-1]["final"] is True


def test_status_survives_reopen(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    db = tmp_path / "state.db"

    created = initialize_migration(source, db)
    reopened = get_summary(db, created.migration_id)

    assert reopened.migration_id == created.migration_id
    assert reopened.states["PENDING"] == 1


def test_valid_state_transitions_persist(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    db = tmp_path / "state.db"
    summary = initialize_migration(source, db)

    transition_file(db, summary.migration_id, "a.txt", TransferState.READY)
    transition_file(db, summary.migration_id, "a.txt", TransferState.UPLOADING, increment_attempts=True)
    transition_file(db, summary.migration_id, "a.txt", TransferState.VERIFYING)
    transition_file(db, summary.migration_id, "a.txt", TransferState.COMPLETE, verified=True)

    final = get_summary(db, summary.migration_id)
    assert final.states["COMPLETE"] == 1

    with sqlite3.connect(db) as conn:
        attempts, verified = conn.execute("SELECT attempts, verified FROM files").fetchone()
    assert attempts == 1
    assert verified == 1


def test_invalid_state_transition_is_rejected(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    db = tmp_path / "state.db"
    summary = initialize_migration(source, db)

    with pytest.raises(ValueError, match="invalid state transition"):
        transition_file(db, summary.migration_id, "a.txt", TransferState.COMPLETE)
