from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .providers.icloud import HydrationState, inspect_file
from .state import (
    TransferState,
    connect,
    create_schema,
    get_latest_migration_id,
    transition_file,
    utc_now,
)

DEFAULT_MAX_BYTES = 10_000_000  # 10 MB safety cap for v0.5 payload trials
DEFAULT_MIN_BYTES = 1             # single-file mode
DEFAULT_BATCH_MIN_BYTES = 0             # selected files are never excluded by size
DEFAULT_BATCH_SIZE = 10


@dataclass(frozen=True)
class ManifestFile:
    migration_id: str
    relative_path: str
    source_path: str
    logical_bytes: int
    hydration_state: str
    transfer_state: str


def select_one_file(
    db_path: Path,
    migration_id: str | None = None,
    relative_path: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    min_bytes: int = DEFAULT_MIN_BYTES,
) -> ManifestFile:
    conn = connect(db_path)
    try:
        create_schema(conn)
        migration_id = migration_id or get_latest_migration_id(conn)
        if relative_path:
            row = conn.execute(
                """
                SELECT migration_id, relative_path, source_path, logical_bytes,
                       hydration_state, transfer_state
                FROM files
                WHERE migration_id = ? AND relative_path = ?
                """,
                (migration_id, relative_path),
            ).fetchone()
        else:
            # Smallest cloud-only PENDING file first: minimizes blast radius for v0.4.
            row = conn.execute(
                """
                SELECT migration_id, relative_path, source_path, logical_bytes,
                       hydration_state, transfer_state
                FROM files
                WHERE migration_id = ?
                  AND transfer_state = 'PENDING'
              AND selected = 1
                  AND hydration_state = 'CLOUD_ONLY'
                  AND logical_bytes BETWEEN ? AND ?
                ORDER BY logical_bytes ASC, relative_path ASC
                LIMIT 1
                """,
                (migration_id, min_bytes, max_bytes),
            ).fetchone()

        if row is None:
            if relative_path:
                raise LookupError(f"file not found in migration: {relative_path}")
            raise LookupError(
                f"no CLOUD_ONLY PENDING non-zero file between {min_bytes:,} and {max_bytes:,} bytes found in migration"
            )

        item = ManifestFile(
            migration_id=str(row["migration_id"]),
            relative_path=str(row["relative_path"]),
            source_path=str(row["source_path"]),
            logical_bytes=int(row["logical_bytes"]),
            hydration_state=str(row["hydration_state"]),
            transfer_state=str(row["transfer_state"]),
        )
        if item.transfer_state != TransferState.PENDING.value:
            raise ValueError(
                f"v0.4 only hydrates PENDING files; {item.relative_path} is {item.transfer_state}"
            )
        return item
    finally:
        conn.close()


def update_manifest_hydration(
    db_path: Path,
    migration_id: str,
    relative_path: str,
    hydration_state: HydrationState,
    allocated_bytes: int,
    source_sha256: str | None = None,
) -> None:
    conn = connect(db_path)
    try:
        create_schema(conn)
        conn.execute(
            """
            UPDATE files
            SET hydration_state = ?, allocated_bytes = ?, source_sha256 = COALESCE(?, source_sha256), updated_at = ?
            WHERE migration_id = ? AND relative_path = ?
            """,
            (
                hydration_state.value,
                allocated_bytes,
                source_sha256,
                utc_now(),
                migration_id,
                relative_path,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def request_icloud_download(path: Path) -> None:
    brctl = shutil.which("brctl")
    if not brctl:
        raise RuntimeError("macOS brctl command was not found")

    result = subprocess.run(
        [brctl, "download", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown brctl error").strip()
        raise RuntimeError(f"iCloud download request failed: {detail}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_local_file(path: Path, expected_size: int) -> tuple[int, str]:
    state = inspect_file(path)
    if state.hydration is not HydrationState.LOCAL:
        raise RuntimeError("file is still cloud-only after hydration")
    if state.logical_bytes != expected_size:
        raise RuntimeError(
            f"size changed during hydration: expected {expected_size:,}, got {state.logical_bytes:,} bytes"
        )

    # v0.5 reads the complete small test payload and records a SHA-256.
    # This proves the hydrated contents are readable, not merely that SF_DATALESS disappeared.
    checksum = sha256_file(path)
    return state.allocated_bytes, checksum


def hydrate_one(
    db_path: Path,
    *,
    migration_id: str | None = None,
    relative_path: str | None = None,
    timeout_seconds: float = 300,
    poll_seconds: float = 1.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    min_bytes: int = DEFAULT_MIN_BYTES,
) -> ManifestFile:
    item = select_one_file(db_path, migration_id, relative_path, max_bytes)
    path = Path(item.source_path)

    current = inspect_file(path)
    if current.hydration is HydrationState.LOCAL:
        update_manifest_hydration(
            db_path, item.migration_id, item.relative_path, current.hydration, current.allocated_bytes
        )
        transition_file(db_path, item.migration_id, item.relative_path, TransferState.READY)
        return item

    if current.hydration is HydrationState.UNKNOWN:
        raise RuntimeError(f"cannot determine iCloud hydration state for: {path}")

    transition_file(
        db_path,
        item.migration_id,
        item.relative_path,
        TransferState.HYDRATING,
        increment_attempts=True,
    )

    try:
        request_icloud_download(path)
        deadline = time.monotonic() + timeout_seconds
        while True:
            observed = inspect_file(path)
            if observed.hydration is HydrationState.LOCAL:
                allocated, checksum = verify_local_file(path, item.logical_bytes)
                update_manifest_hydration(
                    db_path,
                    item.migration_id,
                    item.relative_path,
                    HydrationState.LOCAL,
                    allocated,
                    checksum,
                )
                transition_file(
                    db_path,
                    item.migration_id,
                    item.relative_path,
                    TransferState.READY,
                    error=None,
                )
                return item

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out after {timeout_seconds:g}s waiting for iCloud hydration"
                )
            time.sleep(poll_seconds)
    except Exception as exc:
        transition_file(
            db_path,
            item.migration_id,
            item.relative_path,
            TransferState.FAILED,
            error=str(exc),
        )
        raise


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} TB"



def recover_incomplete(db_path: Path, migration_id: str | None = None) -> int:
    """Return HYDRATING files to PENDING after an interrupted prior run."""
    conn = connect(db_path)
    try:
        create_schema(conn)
        migration_id = migration_id or get_latest_migration_id(conn)
        rows = conn.execute(
            """
            SELECT relative_path
            FROM files
            WHERE migration_id = ? AND transfer_state = 'HYDRATING'
            """,
            (migration_id,),
        ).fetchall()
    finally:
        conn.close()

    recovered = 0
    for row in rows:
        transition_file(
            db_path,
            migration_id,
            str(row["relative_path"]),
            TransferState.PENDING,
            error="Recovered after interrupted hydration run",
        )
        recovered += 1
    return recovered


def select_batch(
    db_path: Path,
    *,
    migration_id: str | None = None,
    count: int = DEFAULT_BATCH_SIZE,
    min_bytes: int = DEFAULT_BATCH_MIN_BYTES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[ManifestFile]:
    conn = connect(db_path)
    try:
        create_schema(conn)
        migration_id = migration_id or get_latest_migration_id(conn)
        rows = conn.execute(
            """
            SELECT migration_id, relative_path, source_path, logical_bytes,
                   hydration_state, transfer_state
            FROM files
            WHERE migration_id = ?
              AND transfer_state = 'PENDING'
              AND selected = 1
              AND hydration_state = 'CLOUD_ONLY'
              AND logical_bytes BETWEEN ? AND ?
            ORDER BY logical_bytes ASC, relative_path ASC
            LIMIT ?
            """,
            (migration_id, min_bytes, max_bytes, count),
        ).fetchall()
        return [
            ManifestFile(
                migration_id=str(row["migration_id"]),
                relative_path=str(row["relative_path"]),
                source_path=str(row["source_path"]),
                logical_bytes=int(row["logical_bytes"]),
                hydration_state=str(row["hydration_state"]),
                transfer_state=str(row["transfer_state"]),
            )
            for row in rows
        ]
    finally:
        conn.close()


def hydrate_batch(
    db_path: Path,
    *,
    migration_id: str | None = None,
    count: int = DEFAULT_BATCH_SIZE,
    min_bytes: int = DEFAULT_BATCH_MIN_BYTES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = 300,
) -> tuple[int, int]:
    recovered = recover_incomplete(db_path, migration_id)
    items = select_batch(
        db_path,
        migration_id=migration_id,
        count=count,
        min_bytes=min_bytes,
        max_bytes=max_bytes,
    )
    if not items:
        raise LookupError("no eligible CLOUD_ONLY PENDING files found for batch trial")

    completed = 0
    failed = 0
    print(f"Recovered:   {recovered} interrupted file(s)")
    print(f"Batch size:  {len(items)}")
    print(f"Payload:     {human_bytes(sum(i.logical_bytes for i in items))}")
    print()

    for idx, item in enumerate(items, start=1):
        print(f"[{idx}/{len(items)}] {item.relative_path}")
        print(f"        {human_bytes(item.logical_bytes)}")
        try:
            hydrate_one(
                db_path,
                migration_id=item.migration_id,
                relative_path=item.relative_path,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                min_bytes=min_bytes,
            )
            print("        READY")
            completed += 1
        except KeyboardInterrupt:
            print()
            print("Interrupted by user. Current state has been preserved.")
            raise
        except Exception as exc:
            print(f"        FAILED: {exc}")
            failed += 1

    return completed, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Hydrate iCloud files for CloudMover")
    parser.add_argument("--db", type=Path, default=Path(".cloudmover/migration.db"))
    parser.add_argument("--migration-id", type=str, default=None)
    parser.add_argument("--path", dest="relative_path", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--batch", type=int, default=None, help="Hydrate up to N eligible files")
    parser.add_argument("--min-bytes", type=int, default=None)
    args = parser.parse_args()

    print("CloudMover 0.19.5")
    print()

    if args.batch:
        min_bytes = args.min_bytes if args.min_bytes is not None else DEFAULT_BATCH_MIN_BYTES
        print("Batch hydration trial")
        print(f"Candidate range: {human_bytes(min_bytes)} - {human_bytes(args.max_bytes)}")
        print("Press Ctrl+C at any time to test interruption recovery.")
        print()
        try:
            completed, failed = hydrate_batch(
                args.db,
                migration_id=args.migration_id,
                count=args.batch,
                min_bytes=min_bytes,
                max_bytes=args.max_bytes,
                timeout_seconds=args.timeout,
            )
        except KeyboardInterrupt:
            raise SystemExit(130)
        print()
        print(f"Completed:   {completed}")
        print(f"Failed:      {failed}")
        print("No upload, eviction, or deletion was performed.")
        return

    try:
        candidate = select_one_file(
            args.db, args.migration_id, args.relative_path, args.max_bytes, DEFAULT_MIN_BYTES
        )
        print("Single-file hydration trial")
        print(f"File:       {candidate.relative_path}")
        print(f"Size:       {human_bytes(candidate.logical_bytes)}")
        print("State:      PENDING -> HYDRATING")
        print()
        hydrate_one(
            args.db,
            migration_id=candidate.migration_id,
            relative_path=candidate.relative_path,
            timeout_seconds=args.timeout,
            max_bytes=args.max_bytes,
            min_bytes=DEFAULT_MIN_BYTES,
        )
        print("Hydration:  complete")
        print("State:      READY")
        print()
        print("No upload, eviction, or deletion was performed.")
    except Exception as exc:
        print(f"Hydration failed safely: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
