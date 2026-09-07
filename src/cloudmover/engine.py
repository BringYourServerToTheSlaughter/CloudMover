
from __future__ import annotations
from .disk_space import RESERVE_BYTES
from .version import VERSION
import argparse, shutil, time
from pathlib import Path
from .bulk_pipeline import run_bulk
from .release import release_specific, human_bytes
from .state import connect, create_schema, get_latest_migration_id

def completed_unreleased(db:Path, migration_id:str) -> list[tuple[str,str]]:
    c=connect(db)
    try:
        create_schema(c)
        rows=c.execute(
            """SELECT migration_id,relative_path FROM files
               WHERE migration_id=? AND transfer_state='COMPLETE' AND verified=1
                 AND COALESCE(local_released,0)=0 AND release_error IS NULL
                 AND relative_path != '.DS_Store' AND substr(relative_path,-10) != '/.DS_Store'
               ORDER BY updated_at ASC""",(migration_id,)
        ).fetchall()
        return [(str(r["migration_id"]),str(r["relative_path"])) for r in rows]
    finally:c.close()

def migration_id(db:Path)->str:
    c=connect(db)
    try:
        create_schema(c); return get_latest_migration_id(c)
    finally:c.close()

def free_bytes(db:Path)->int:
    c=connect(db)
    try:
        create_schema(c); mid=get_latest_migration_id(c)
        row=c.execute("SELECT source FROM migrations WHERE id=?",(mid,)).fetchone()
        if not row:
            raise LookupError(f"migration not found: {mid}")
        return shutil.disk_usage(str(row["source"])).free
    finally:c.close()

def run_engine(db:Path,cycles:int,batch:int,min_bytes:int,max_bytes:int,
               destination:str,reserve_bytes:int,verify_workers:int=1)->None:
    mid=migration_id(db)
    for cycle in range(1,cycles+1):
        free=free_bytes(db)
        print(f"\n=== SMART_STAGING cycle {cycle}/{cycles} ===")
        print(f"Free disk:   {human_bytes(free)}")
        print(f"Reserve:     {human_bytes(reserve_bytes)}")
        if free <= reserve_bytes:
            raise RuntimeError("protected free-space reserve reached; refusing to hydrate more data")

        before=set(completed_unreleased(db,mid))
        run_bulk(db,count=batch,min_bytes=min_bytes,max_bytes=max_bytes,
                 destination=destination,verify_workers=verify_workers,reserve_bytes=reserve_bytes)
        after=completed_unreleased(db,mid)

        # Release only rows that are COMPLETE+verified. Include older eligible rows too:
        # draining them first only increases safety margin.
        reclaimed=0
        released=0
        for m,rp in after:
            try:
                _,n=release_specific(db,m,rp)
                reclaimed+=n; released+=1
                print(f"[release {released}] {rp} ({human_bytes(n)} reclaimed)")
            except LookupError:
                continue
            except Exception as exc:
                print(f"Local cache cleanup warning: {exc}")

        print(f"Cycle {cycle}: released {released} verified file(s), reclaimed {human_bytes(reclaimed)}")
        print(f"Free disk:   {human_bytes(free_bytes(db))}")

def main():
    ap=argparse.ArgumentParser(description="CloudMover rolling SMART_STAGING engine")
    ap.add_argument("--db",type=Path,default=Path(".cloudmover/migration.db"))
    ap.add_argument("--cycles",type=int,default=2)
    ap.add_argument("--batch",type=int,default=10)
    ap.add_argument("--min-bytes",type=int,default=100_000)
    ap.add_argument("--max-bytes",type=int,default=5_000_000)
    ap.add_argument("--destination",default="/my-files/CloudMover-Engine-Test")
    ap.add_argument("--reserve-gb",type=float,default=RESERVE_BYTES/1_000_000_000)
    ap.add_argument("--verify-workers",type=int,default=1)
    a=ap.parse_args()
    print(f"CloudMover {VERSION}")
    print("Rolling SMART_STAGING engine")
    print("Safety: verified Proton copy required before local iCloud cache release")
    try:
        run_engine(a.db,a.cycles,a.batch,a.min_bytes,a.max_bytes,a.destination,
                   int(a.reserve_gb*1_000_000_000),a.verify_workers)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Durable state preserved; rerun to resume.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nEngine stopped safely: {exc}")
        raise SystemExit(1)
    print("\nEngine trial complete. No iCloud source-cloud deletion was performed.")

if __name__=="__main__": main()
