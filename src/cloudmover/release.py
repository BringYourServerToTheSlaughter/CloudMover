
from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .providers.icloud import HydrationState, inspect_file
from .state import connect, create_schema, get_latest_migration_id, utc_now

@dataclass(frozen=True)
class ReleaseItem:
    migration_id: str
    relative_path: str
    source_path: str
    logical_bytes: int
    allocated_bytes: int

def select_one(db: Path, migration_id: str | None = None) -> ReleaseItem:
    c=connect(db)
    try:
        create_schema(c)
        migration_id=migration_id or get_latest_migration_id(c)
        row=c.execute(
            """
            SELECT migration_id, relative_path, source_path, logical_bytes, allocated_bytes
            FROM files
            WHERE migration_id=?
              AND transfer_state='COMPLETE'
              AND verified=1
              AND hydration_state='LOCAL'
              AND COALESCE(local_released,0)=0
            ORDER BY allocated_bytes ASC, relative_path ASC
            LIMIT 1
            """,
            (migration_id,),
        ).fetchone()
        if not row:
            raise LookupError("no verified COMPLETE local file is eligible for release")
        return ReleaseItem(
            str(row["migration_id"]), str(row["relative_path"]), str(row["source_path"]),
            int(row["logical_bytes"]), int(row["allocated_bytes"])
        )
    finally:
        c.close()

def request_evict(path: Path) -> None:
    brctl=shutil.which("brctl")
    if not brctl:
        raise RuntimeError("macOS brctl command was not found")
    p=subprocess.run([brctl,"evict",str(path)],text=True,capture_output=True)
    if p.returncode != 0:
        detail=(p.stderr or p.stdout or "unknown brctl error").strip()
        raise RuntimeError(f"iCloud eviction request failed: {detail}")

def mark_released(db:Path,item:ReleaseItem,allocated_after:int)->None:
    c=connect(db)
    try:
        create_schema(c)
        c.execute(
            """
            UPDATE files
            SET hydration_state='CLOUD_ONLY',
                allocated_bytes=?,
                local_released=1,
                released_at=?,
                updated_at=?
            WHERE migration_id=? AND relative_path=?
            """,
            (allocated_after,utc_now(),utc_now(),item.migration_id,item.relative_path)
        )
        c.commit()
    finally:c.close()


def get_specific_release_item(db: Path, migration_id: str, relative_path: str) -> ReleaseItem:
    c=connect(db)
    try:
        create_schema(c)
        row=c.execute(
            """
            SELECT migration_id, relative_path, source_path, logical_bytes, allocated_bytes
            FROM files
            WHERE migration_id=? AND relative_path=?
              AND transfer_state='COMPLETE' AND verified=1
              AND COALESCE(local_released,0)=0
            """,
            (migration_id, relative_path),
        ).fetchone()
        if not row:
            raise LookupError(f"file is not eligible for release: {relative_path}")
        return ReleaseItem(
            str(row["migration_id"]), str(row["relative_path"]), str(row["source_path"]),
            int(row["logical_bytes"]), int(row["allocated_bytes"])
        )
    finally:
        c.close()

def release_specific(db: Path, migration_id: str, relative_path: str,
                     timeout: float=120, poll: float=1.0) -> tuple[ReleaseItem,int]:
    item=get_specific_release_item(db,migration_id,relative_path)
    path=Path(item.source_path)
    before=inspect_file(path)
    if before.hydration is HydrationState.CLOUD_ONLY:
        mark_released(db,item,before.allocated_bytes)
        return item,max(0,item.allocated_bytes-before.allocated_bytes)
    if before.hydration is not HydrationState.LOCAL:
        raise RuntimeError(f"cannot safely release unknown hydration state: {path}")
    request_evict(path)
    deadline=time.monotonic()+timeout
    while True:
        after=inspect_file(path)
        if after.hydration is HydrationState.CLOUD_ONLY:
            reclaimed=max(0,before.allocated_bytes-after.allocated_bytes)
            mark_released(db,item,after.allocated_bytes)
            return item,reclaimed
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout:g}s waiting for iCloud eviction")
        time.sleep(poll)


def release_one(db:Path,timeout:float=120,poll:float=1.0)->tuple[ReleaseItem,int]:
    item=select_one(db)
    path=Path(item.source_path)
    before=inspect_file(path)
    if before.hydration is HydrationState.CLOUD_ONLY:
        mark_released(db,item,before.allocated_bytes)
        return item, max(0,item.allocated_bytes-before.allocated_bytes)
    if before.hydration is not HydrationState.LOCAL:
        raise RuntimeError(f"cannot safely release unknown hydration state: {path}")

    request_evict(path)
    deadline=time.monotonic()+timeout
    while True:
        after=inspect_file(path)
        if after.hydration is HydrationState.CLOUD_ONLY:
            reclaimed=max(0,before.allocated_bytes-after.allocated_bytes)
            mark_released(db,item,after.allocated_bytes)
            return item,reclaimed
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout:g}s waiting for iCloud eviction")
        time.sleep(poll)

def human_bytes(n:int)->str:
    v=float(n)
    for u in ("B","KB","MB","GB","TB"):
        if v<1000 or u=="TB": return f"{v:.2f} {u}"
        v/=1000
    return f"{v:.2f} TB"

def main():
    ap=argparse.ArgumentParser(description="Release one verified CloudMover local staging file")
    ap.add_argument("--db",type=Path,default=Path(".cloudmover/migration.db"))
    ap.add_argument("--timeout",type=float,default=120)
    a=ap.parse_args()

    print("CloudMover 0.19.5\n")
    print("Single-file SMART_STAGING release trial")
    item=select_one(a.db)
    print(f"File:       {item.relative_path}")
    print(f"Allocated:  {human_bytes(item.allocated_bytes)}")
    print("Safety:     Proton COMPLETE + verified; iCloud source retained")
    print()
    try:
        _,reclaimed=release_one(a.db,a.timeout)
    except Exception as exc:
        print(f"Release failed safely: {exc}")
        raise SystemExit(1)
    print("Local copy: released")
    print(f"Reclaimed:  {human_bytes(reclaimed)}")
    print("iCloud:     source retained as cloud-only")
    print("Proton:     verified copy retained")
    print("\nNo source-cloud deletion was performed.")

if __name__=="__main__":
    main()
