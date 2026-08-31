from __future__ import annotations

import argparse
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator

from .providers.icloud import inspect_file
from .scanner import choose_strategy, human_bytes, walk_entries

SCHEMA_VERSION = 4


class TransferState(str, Enum):
    PENDING = "PENDING"
    HYDRATING = "HYDRATING"
    READY = "READY"
    UPLOADING = "UPLOADING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


# Keep transitions explicit. Recovery paths may move work back to a safe state.
ALLOWED_TRANSITIONS: dict[TransferState, set[TransferState]] = {
    TransferState.PENDING: {TransferState.HYDRATING, TransferState.READY, TransferState.FAILED},
    TransferState.HYDRATING: {TransferState.READY, TransferState.PENDING, TransferState.FAILED},
    TransferState.READY: {TransferState.UPLOADING, TransferState.PENDING, TransferState.FAILED},
    TransferState.UPLOADING: {TransferState.VERIFYING, TransferState.READY, TransferState.FAILED},
    TransferState.VERIFYING: {TransferState.COMPLETE, TransferState.READY, TransferState.FAILED},
    TransferState.COMPLETE: set(),
    TransferState.FAILED: {TransferState.PENDING, TransferState.READY},
}


@dataclass(frozen=True)
class MigrationSummary:
    migration_id: str
    source: str
    status: str
    files: int
    logical_bytes: int
    strategy: str
    staging_bytes: int
    reserve_bytes: int
    states: dict[str, int]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS migrations (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            destination TEXT,
            source_provider TEXT NOT NULL,
            destination_provider TEXT,
            status TEXT NOT NULL,
            strategy TEXT NOT NULL,
            total_files INTEGER NOT NULL,
            total_logical_bytes INTEGER NOT NULL,
            staging_bytes INTEGER NOT NULL,
            reserve_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            migration_id TEXT NOT NULL REFERENCES migrations(id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            destination_path TEXT,
            logical_bytes INTEGER NOT NULL,
            allocated_bytes INTEGER NOT NULL,
            hydration_state TEXT NOT NULL,
            transfer_state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            verified INTEGER NOT NULL DEFAULT 0,
            selected INTEGER NOT NULL DEFAULT 1,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(migration_id, relative_path)
        );

        CREATE INDEX IF NOT EXISTS idx_files_migration_state
        ON files(migration_id, transfer_state);
        """
    )
    # Forward-compatible v0.5 migration for databases created by 0.3/0.4.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)")}
    if "source_sha256" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN source_sha256 TEXT")
    if "local_released" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN local_released INTEGER NOT NULL DEFAULT 0")
    if "released_at" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN released_at TEXT")
    if "selected" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN selected INTEGER NOT NULL DEFAULT 1")

    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def initialize_migration(source: Path, db_path: Path, destination: str | None = None) -> MigrationSummary:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"source does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"source is not a directory: {source}")

    usage = os.statvfs(source)
    free_bytes = usage.f_bavail * usage.f_frsize

    # Inventory in memory only at row granularity, then persist in batches. This avoids
    # opening file contents and therefore avoids hydrating iCloud placeholders.
    file_rows: list[tuple[str, str, int, int, str]] = []
    total_bytes = 0
    for entry in walk_entries(source):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            state = inspect_file(entry.path, stat_result=st)
            path = Path(entry.path)
            relative = str(path.relative_to(source))
            file_rows.append(
                (
                    relative,
                    str(path),
                    state.logical_bytes,
                    state.allocated_bytes,
                    state.hydration.value,
                )
            )
            total_bytes += state.logical_bytes
        except (PermissionError, FileNotFoundError, OSError) as exc:
            print(f"warning: could not inventory {entry.path}: {exc}")

    strategy, staging_bytes, reserve_bytes = choose_strategy(total_bytes, free_bytes)
    migration_id = str(uuid.uuid4())
    now = utc_now()

    conn = connect(db_path)
    try:
        create_schema(conn)
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO migrations(
                    id, source, destination, source_provider, destination_provider,
                    status, strategy, total_files, total_logical_bytes,
                    staging_bytes, reserve_bytes, created_at, updated_at
                ) VALUES (?, ?, ?, 'icloud', ?, 'PLANNED', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    str(source),
                    destination,
                    "proton" if destination else None,
                    strategy,
                    len(file_rows),
                    total_bytes,
                    staging_bytes,
                    reserve_bytes,
                    now,
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO files(
                    migration_id, relative_path, source_path, logical_bytes,
                    allocated_bytes, hydration_state, transfer_state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                [
                    (migration_id, relative, path, logical, allocated, hydration, now, now)
                    for relative, path, logical, allocated, hydration in file_rows
                ],
            )
    finally:
        conn.close()

    return get_summary(db_path, migration_id)


def get_latest_migration_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM migrations ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        raise LookupError("no migrations found in database")
    return str(row["id"])


def get_summary(db_path: Path, migration_id: str | None = None) -> MigrationSummary:
    conn = connect(db_path)
    try:
        create_schema(conn)
        migration_id = migration_id or get_latest_migration_id(conn)
        migration = conn.execute("SELECT * FROM migrations WHERE id = ?", (migration_id,)).fetchone()
        if migration is None:
            raise LookupError(f"migration not found: {migration_id}")
        rows = conn.execute(
            "SELECT transfer_state, COUNT(*) AS count FROM files WHERE migration_id = ? GROUP BY transfer_state",
            (migration_id,),
        ).fetchall()
        states = {str(row["transfer_state"]): int(row["count"]) for row in rows}
        return MigrationSummary(
            migration_id=migration_id,
            source=str(migration["source"]),
            status=str(migration["status"]),
            files=int(migration["total_files"]),
            logical_bytes=int(migration["total_logical_bytes"]),
            strategy=str(migration["strategy"]),
            staging_bytes=int(migration["staging_bytes"]),
            reserve_bytes=int(migration["reserve_bytes"]),
            states=states,
        )
    finally:
        conn.close()


def transition_file(
    db_path: Path,
    migration_id: str,
    relative_path: str,
    new_state: TransferState,
    *,
    error: str | None = None,
    verified: bool | None = None,
    increment_attempts: bool = False,
) -> None:
    conn = connect(db_path)
    try:
        create_schema(conn)
        row = conn.execute(
            "SELECT transfer_state FROM files WHERE migration_id = ? AND relative_path = ?",
            (migration_id, relative_path),
        ).fetchone()
        if row is None:
            raise LookupError(f"file not found in migration: {relative_path}")
        old_state = TransferState(str(row["transfer_state"]))
        if new_state != old_state and new_state not in ALLOWED_TRANSITIONS[old_state]:
            raise ValueError(f"invalid state transition: {old_state.value} -> {new_state.value}")

        fields = ["transfer_state = ?", "updated_at = ?", "last_error = ?"]
        values: list[object] = [new_state.value, utc_now(), error]
        if verified is not None:
            fields.append("verified = ?")
            values.append(1 if verified else 0)
        if increment_attempts:
            fields.append("attempts = attempts + 1")
        values.extend([migration_id, relative_path])
        conn.execute(
            f"UPDATE files SET {', '.join(fields)} WHERE migration_id = ? AND relative_path = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def print_summary(summary: MigrationSummary, db_path: Path) -> None:
    print("CloudMover 0.18.5")
    print()
    print(f"Migration:  {summary.migration_id}")
    print(f"Database:   {db_path.expanduser()}")
    print(f"Source:     {summary.source}")
    print(f"Status:     {summary.status}")
    print()
    print(f"Files:      {summary.files:,}")
    print(f"Data:       {human_bytes(summary.logical_bytes)}")
    print(f"Strategy:   {summary.strategy}")
    print(f"Staging:    {human_bytes(summary.staging_bytes)}")
    print(f"Reserve:    {human_bytes(summary.reserve_bytes)}")
    print()
    print("Transfer states:")
    for state in TransferState:
        count = summary.states.get(state.value, 0)
        if count or state is TransferState.PENDING:
            print(f"  {state.value:<10} {count:>8,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CloudMover durable migration state")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Create a migration database and file manifest")
    init_parser.add_argument("source", type=Path)
    init_parser.add_argument("--db", type=Path, default=Path(".cloudmover/migration.db"))
    init_parser.add_argument("--destination", type=str, default=None)

    status_parser = sub.add_parser("status", help="Show status of a migration database")
    status_parser.add_argument("--db", type=Path, default=Path(".cloudmover/migration.db"))
    status_parser.add_argument("--migration-id", type=str, default=None)

    args = parser.parse_args()
    if args.command == "init":
        summary = initialize_migration(args.source, args.db, args.destination)
        print_summary(summary, args.db)
    elif args.command == "status":
        summary = get_summary(args.db, args.migration_id)
        print_summary(summary, args.db)


if __name__ == "__main__":
    main()
