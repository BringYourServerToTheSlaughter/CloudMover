
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from .providers import proton
from .state import TransferState, connect, create_schema, get_latest_migration_id, transition_file, utc_now

@dataclass(frozen=True)
class Item:
    migration_id:str
    relative_path:str
    source_path:str
    logical_bytes:int

def pick(db:Path,migration_id:str|None=None)->Item:
    c=connect(db)
    try:
        create_schema(c); migration_id=migration_id or get_latest_migration_id(c)
        r=c.execute("""SELECT migration_id,relative_path,source_path,logical_bytes FROM files
        WHERE migration_id=? AND transfer_state='READY' AND logical_bytes>0
        ORDER BY logical_bytes ASC,relative_path ASC LIMIT 1""",(migration_id,)).fetchone()
        if not r: raise LookupError("no non-zero READY file available")
        return Item(str(r["migration_id"]),str(r["relative_path"]),str(r["source_path"]),int(r["logical_bytes"]))
    finally:c.close()

def set_destination(db:Path,item:Item,dest:str)->None:
    c=connect(db)
    try:
        create_schema(c)
        c.execute("UPDATE files SET destination_path=?,updated_at=? WHERE migration_id=? AND relative_path=?",
                  (dest,utc_now(),item.migration_id,item.relative_path)); c.commit()
    finally:c.close()



def ensure_remote_tree(base: str, relative_path: str, cache: set[str] | None = None) -> tuple[str, str]:
    """Create relative parent folders, skipping paths already proven in this run."""
    rel = Path(relative_path)
    current = base.rstrip("/")
    if cache is not None:
        cache.add(current)
    for part in rel.parts[:-1]:
        target = current.rstrip("/") + "/" + part
        if cache is not None and target in cache:
            current = target
            continue
        current = proton.ensure_folder(current, part)
        if cache is not None:
            cache.add(current)
    return current, current.rstrip("/") + "/" + rel.name


def get_specific(db: Path, migration_id: str, relative_path: str) -> Item:
    c=connect(db)
    try:
        create_schema(c)
        r=c.execute("""SELECT migration_id,relative_path,source_path,logical_bytes FROM files
        WHERE migration_id=? AND relative_path=? AND transfer_state='READY'""",
        (migration_id,relative_path)).fetchone()
        if not r:
            raise LookupError(f"file is not READY: {relative_path}")
        return Item(str(r["migration_id"]),str(r["relative_path"]),str(r["source_path"]),int(r["logical_bytes"]))
    finally:c.close()

def upload_specific(db: Path, migration_id: str, relative_path: str,
                    dest_parent: str="/my-files/CloudMover-Tree-Test",
                    remote_cache: set[str] | None = None) -> tuple[Item, dict[str, float]]:
    item=get_specific(db,migration_id,relative_path)
    local=Path(item.source_path)
    if not local.exists(): raise FileNotFoundError(local)
    timings={}
    t=perf_counter()
    parent,name=dest_parent.rsplit("/",1)
    base_path=dest_parent.rstrip("/")
    if remote_cache is not None and base_path in remote_cache:
        base=base_path
    else:
        base = proton.ensure_folder(parent or "/",name)
        if remote_cache is not None: remote_cache.add(base)
    remote_parent, remote_file = ensure_remote_tree(base, item.relative_path, remote_cache)
    timings["path_setup"]=perf_counter()-t
    transition_file(db,item.migration_id,item.relative_path,TransferState.UPLOADING,increment_attempts=True)
    try:
        t=perf_counter()
        proton.upload(local,remote_parent)
        timings["upload"]=perf_counter()-t
        transition_file(db,item.migration_id,item.relative_path,TransferState.VERIFYING)
        t=perf_counter()
        proton.verify(local,remote_parent)
        timings["verify"]=perf_counter()-t
        set_destination(db,item,remote_file)
        transition_file(db,item.migration_id,item.relative_path,TransferState.COMPLETE,verified=True)
        return item,timings
    except Exception as e:
        try: transition_file(db,item.migration_id,item.relative_path,TransferState.READY,error=str(e))
        except Exception: pass
        raise


def upload_one(db:Path,dest_parent:str="/my-files/CloudMover-Test")->Item:
    item=pick(db); local=Path(item.source_path)
    if not local.exists(): raise FileNotFoundError(local)
    parent,name=dest_parent.rsplit("/",1)
    base = proton.ensure_folder(parent or "/",name)
    remote_parent, remote_file = ensure_remote_tree(base, item.relative_path)
    transition_file(db,item.migration_id,item.relative_path,TransferState.UPLOADING,increment_attempts=True)
    try:
        proton.upload(local,remote_parent)
        transition_file(db,item.migration_id,item.relative_path,TransferState.VERIFYING)
        proton.verify(local,remote_parent)
        set_destination(db,item,remote_file)
        transition_file(db,item.migration_id,item.relative_path,TransferState.COMPLETE,verified=True)
        return item
    except Exception as e:
        # Return to READY because local source remains intact and can be retried.
        try: transition_file(db,item.migration_id,item.relative_path,TransferState.READY,error=str(e))
        except Exception: pass
        raise

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",type=Path,default=Path(".cloudmover/migration.db"))
    ap.add_argument("--destination",default="/my-files/CloudMover-Test")
    a=ap.parse_args()
    print("CloudMover 0.18.5\n\nSingle-file Proton upload trial")
    item=pick(a.db)
    print(f"File:        {item.relative_path}")
    print(f"Destination: {a.destination}")
    print("State:       READY -> UPLOADING -> VERIFYING -> COMPLETE\n")
    try:
        upload_one(a.db,a.destination)
        print("Upload:      complete")
        print("Verification: passed")
        print("State:       COMPLETE")
        print("\nNo source deletion or iCloud eviction was performed.")
    except Exception as e:
        print(f"Upload failed safely: {e}")
        raise SystemExit(1)

if __name__=="__main__": main()
