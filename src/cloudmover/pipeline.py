
from __future__ import annotations
import argparse
from time import perf_counter
from pathlib import Path
from .hydrate import DEFAULT_MAX_BYTES, hydrate_one, select_batch, recover_incomplete, human_bytes
from .upload import upload_one
from .state import connect, create_schema, get_latest_migration_id

DEFAULT_PIPELINE_MIN_BYTES=100_000
DEFAULT_PIPELINE_MAX_BYTES=5_000_000
DEFAULT_PIPELINE_COUNT=10

def counts(db:Path,migration_id:str|None=None)->dict[str,int]:
    c=connect(db)
    try:
        create_schema(c); migration_id=migration_id or get_latest_migration_id(c)
        return {str(r["transfer_state"]):int(r["n"]) for r in c.execute(
            "SELECT transfer_state,COUNT(*) n FROM files WHERE migration_id=? GROUP BY transfer_state",
            (migration_id,)).fetchall()}
    finally:c.close()

def run_pipeline(db:Path,count:int=DEFAULT_PIPELINE_COUNT,
                 min_bytes:int=DEFAULT_PIPELINE_MIN_BYTES,max_bytes:int=DEFAULT_PIPELINE_MAX_BYTES,
                 destination:str="/my-files/CloudMover-Tree-Test",timeout:float=300)->tuple[int,int]:
    recovered=recover_incomplete(db)
    items=select_batch(db,count=count,min_bytes=min_bytes,max_bytes=max_bytes)
    if not items: raise LookupError("no eligible cloud-only PENDING files for pipeline trial")
    print(f"Recovered:   {recovered} interrupted hydration file(s)")
    print(f"Batch size:  {len(items)}")
    print(f"Payload:     {human_bytes(sum(i.logical_bytes for i in items))}")
    print()
    completed=failed=0
    remote_cache=set()
    totals={"hydrate":0.0,"path_setup":0.0,"upload":0.0,"verify":0.0,"total":0.0}
    for i,item in enumerate(items,1):
        print(f"[{i}/{len(items)}] {item.relative_path}")
        print(f"        {human_bytes(item.logical_bytes)}")
        item_start=perf_counter()
        try:
            t=perf_counter()
            hydrate_one(db,migration_id=item.migration_id,relative_path=item.relative_path,
                        timeout_seconds=timeout,max_bytes=max_bytes,min_bytes=min_bytes)
            hydrate_s=perf_counter()-t
            from .upload import upload_specific
            _, ut=upload_specific(db,item.migration_id,item.relative_path,destination,remote_cache)
            total_s=perf_counter()-item_start
            totals["hydrate"]+=hydrate_s
            totals["path_setup"]+=ut["path_setup"]
            totals["upload"]+=ut["upload"]
            totals["verify"]+=ut["verify"]
            totals["total"]+=total_s
            print(f"        hydrate+SHA  {hydrate_s:7.2f}s")
            print(f"        path setup   {ut['path_setup']:7.2f}s")
            print(f"        upload       {ut['upload']:7.2f}s")
            print(f"        verify       {ut['verify']:7.2f}s")
            print(f"        total        {total_s:7.2f}s  COMPLETE")
            completed+=1
        except KeyboardInterrupt:
            print("\nInterrupted by user. Durable state has been preserved.")
            raise
        except Exception as e:
            print(f"        FAILED: {e}")
            failed+=1
    print()
    print("Timing totals")
    print(f"  hydrate+SHA  {totals['hydrate']:8.2f}s")
    print(f"  path setup   {totals['path_setup']:8.2f}s")
    print(f"  upload       {totals['upload']:8.2f}s")
    print(f"  verify       {totals['verify']:8.2f}s")
    print(f"  wall work    {totals['total']:8.2f}s")
    return completed,failed

def main():
    ap=argparse.ArgumentParser(description="CloudMover end-to-end batch pipeline")
    ap.add_argument("--db",type=Path,default=Path(".cloudmover/migration.db"))
    ap.add_argument("--batch",type=int,default=DEFAULT_PIPELINE_COUNT)
    ap.add_argument("--min-bytes",type=int,default=DEFAULT_PIPELINE_MIN_BYTES)
    ap.add_argument("--max-bytes",type=int,default=DEFAULT_PIPELINE_MAX_BYTES)
    ap.add_argument("--destination",default="/my-files/CloudMover-Tree-Test")
    ap.add_argument("--timeout",type=float,default=300)
    a=ap.parse_args()
    print("CloudMover 0.9.2\n\nEnd-to-end batch trial")
    print(f"Destination: {a.destination}")
    print("Path:        CLOUD_ONLY -> HYDRATING -> READY -> UPLOADING -> VERIFYING -> COMPLETE\n")
    try:
        done,failed=run_pipeline(a.db,a.batch,a.min_bytes,a.max_bytes,a.destination,a.timeout)
    except KeyboardInterrupt:
        raise SystemExit(130)
    print(f"\nCompleted:   {done}")
    print(f"Failed:      {failed}")
    print("Eviction/deletion remains disabled.")
if __name__=="__main__":main()
