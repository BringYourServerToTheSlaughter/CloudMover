
from __future__ import annotations
from .disk_space import RESERVE_BYTES, require_batch_space
from .version import VERSION
from .destination import prepare_destination
import argparse, os, shutil, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter
from .ui import Activity

from .hydrate import hydrate_one, select_batch, recover_incomplete, human_bytes
from .providers import proton
from .state import TransferState, transition_file, connect, create_schema, get_latest_migration_id, set_activity_progress
from .upload import ensure_remote_tree, set_destination

DEFAULT_COUNT=10
DEFAULT_MIN=100_000
DEFAULT_MAX=5_000_000
DEFAULT_DEST="/my-files/CloudMover-Bulk-Test"
DEFAULT_VERIFY_WORKERS=1



def select_ready_batch(db: Path, count: int) -> list:
    """Return READY rows first so interrupted work resumes before new hydration."""
    c=connect(db)
    try:
        create_schema(c)
        migration_id=get_latest_migration_id(c)
        rows=c.execute(
            """
            SELECT migration_id, relative_path, source_path, logical_bytes,
                   hydration_state, transfer_state
            FROM files
            WHERE migration_id=? AND transfer_state='READY' AND selected=1 AND logical_bytes>=0
            ORDER BY updated_at ASC, relative_path ASC
            LIMIT ?
            """,
            (migration_id,count),
        ).fetchall()
        from .hydrate import ManifestFile
        return [
            ManifestFile(
                migration_id=str(r["migration_id"]),
                relative_path=str(r["relative_path"]),
                source_path=str(r["source_path"]),
                logical_bytes=int(r["logical_bytes"]),
                hydration_state=str(r["hydration_state"]),
                transfer_state=str(r["transfer_state"]),
            )
            for r in rows
        ]
    finally:
        c.close()


def recover_verifying(db: Path) -> int:
    """Return rows stranded in VERIFYING to READY so verification can be retried safely."""
    from .state import connect, create_schema, get_latest_migration_id
    c=connect(db)
    try:
        create_schema(c)
        migration_id=get_latest_migration_id(c)
        rows=c.execute(
            "SELECT relative_path FROM files WHERE migration_id=? AND transfer_state='VERIFYING'",
            (migration_id,)
        ).fetchall()
    finally:
        c.close()
    count=0
    for row in rows:
        transition_file(
            db, migration_id, str(row["relative_path"]),
            TransferState.READY,
            error="Recovered after interrupted/failed verification"
        )
        count+=1
    return count


def stage_batch(items, stage_root: Path) -> list[Path]:
    """Mirror relative paths using hard links when possible, copying only as fallback."""
    top_names=set()
    for item in items:
        src=Path(item.source_path)
        dst=stage_root / item.relative_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        top_names.add(Path(item.relative_path).parts[0])
    return [stage_root/name for name in sorted(top_names)]


def select_byte_target_batch(
    db: Path,
    *,
    target_bytes: int,
    max_files: int,
    min_bytes: int,
    max_bytes: int,
) -> list:
    """Select every selected READY/PENDING file regardless of size; target bytes only shapes each batch."""
    c=connect(db)
    try:
        create_schema(c)
        migration_id=get_latest_migration_id(c)
        from .hydrate import ManifestFile

        selected=[]
        total=0

        ready=c.execute(
            """
            SELECT migration_id, relative_path, source_path, logical_bytes,
                   hydration_state, transfer_state
            FROM files
            WHERE migration_id=? AND transfer_state='READY' AND selected=1 AND logical_bytes>=0
            ORDER BY updated_at ASC, relative_path ASC
            """,(migration_id,)
        ).fetchall()
        for r in ready:
            size=int(r["logical_bytes"])
            if selected and total+size>target_bytes:
                continue
            selected.append(ManifestFile(
                str(r["migration_id"]),str(r["relative_path"]),str(r["source_path"]),
                size,str(r["hydration_state"]),str(r["transfer_state"])
            ))
            total+=size
            if len(selected)>=max_files or total>=target_bytes:
                return selected

        pending=c.execute(
            """
            SELECT migration_id, relative_path, source_path, logical_bytes,
                   hydration_state, transfer_state
            FROM files
            WHERE migration_id=?
              AND selected=1
              AND transfer_state='PENDING'
              AND logical_bytes>=0
            ORDER BY logical_bytes DESC, relative_path ASC
            """,(migration_id,)
        ).fetchall()
        for r in pending:
            size=int(r["logical_bytes"])
            if selected and total+size>target_bytes:
                continue
            selected.append(ManifestFile(
                str(r["migration_id"]),str(r["relative_path"]),str(r["source_path"]),
                size,str(r["hydration_state"]),str(r["transfer_state"])
            ))
            total+=size
            if len(selected)>=max_files or total>=target_bytes:
                break
        return selected
    finally:
        c.close()



def hydrate_selected_parallel(
    db: Path,
    items: list,
    *,
    timeout: float,
    min_bytes: int,
    max_bytes: int,
    workers: int,
) -> tuple[list, float]:
    """Hydrate selected items concurrently; READY work passes through unchanged.

    Each worker operates on a different manifest row. SQLite connections are opened
    per state operation by the existing state helpers, so no connection is shared
    across threads. Any failure aborts the cycle safely before upload.
    """
    started=perf_counter()
    hydrated=[None]*len(items)

    def one(index, item):
        if item.transfer_state == TransferState.READY.value:
            return index,item,"resume"
        hydrate_one(
            db,
            migration_id=item.migration_id,
            relative_path=item.relative_path,
            timeout_seconds=timeout,
            max_bytes=max_bytes,
            min_bytes=min_bytes,
        )
        return index,item,"hydrate"

    max_workers=max(1,min(workers,len(items)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures={pool.submit(one,i,item):(i,item) for i,item in enumerate(items)}
        for fut in as_completed(futures):
            i,item=futures[fut]
            try:
                index,done,kind=fut.result()
            except Exception:
                # Other workers may finish, but upload never starts after a hydration
                # failure. Durable row states make the next run resume safely.
                for other in futures:
                    other.cancel()
                raise
            hydrated[index]=done
            label="READY" if kind=="resume" else "READY"
            print(f"[hydrate {index+1}/{len(items)}] {done.relative_path} -> {label}")

    return [x for x in hydrated if x is not None], perf_counter()-started


def run_bulk(db:Path,count:int=DEFAULT_COUNT,min_bytes:int=DEFAULT_MIN,
             max_bytes:int=DEFAULT_MAX,destination:str=DEFAULT_DEST,timeout:float=300,
             verify_workers:int=DEFAULT_VERIFY_WORKERS,target_bytes:int|None=None,
             hydrate_workers:int=1,verbose:bool=False,reserve_bytes:int=RESERVE_BYTES,event_callback=None):
    def emit(event, **data):
        if event_callback:
            event_callback(event, **data)

    recovered=recover_incomplete(db)
    recovered_verify=recover_verifying(db)

    if target_bytes is not None:
        items=select_byte_target_batch(
            db,target_bytes=target_bytes,max_files=count,
            min_bytes=min_bytes,max_bytes=max_bytes
        )
    else:
        ready_items=select_ready_batch(db,count)
        remaining=max(0,count-len(ready_items))
        pending_items=select_batch(
            db,count=remaining,min_bytes=min_bytes,max_bytes=max_bytes
        ) if remaining else []
        items=ready_items+pending_items
    if not items:
        raise LookupError("no READY or eligible cloud-only PENDING files for bulk trial")

    base=prepare_destination(db,destination,items[0].migration_id)
    require_batch_space(items, reserve_bytes)

    print(f"Recovered:   {recovered} interrupted hydration file(s)")
    print(f"Recovered:   {recovered_verify} interrupted verification file(s)")
    print(f"Batch size:  {len(items)}")
    payload_bytes=sum(i.logical_bytes for i in items)
    print(f"Payload:     {human_bytes(payload_bytes)}")
    print()

    emit("hydration_start",files=len(items),logical_bytes=payload_bytes)
    timings={"hydrate":0.0,"stage":0.0,"upload":0.0,"verify":0.0}
    set_activity_progress(db,stage="hydrate",completed=0,total=len(items),detail="Preparing selected files")
    if hydrate_workers > 1:
        print(f"Hydration:   up to {hydrate_workers} concurrent file(s)")
        hydrated,t_hydrate=hydrate_selected_parallel(
            db,items,timeout=timeout,min_bytes=min_bytes,max_bytes=max_bytes,
            workers=hydrate_workers
        )
        timings["hydrate"]=t_hydrate
        set_activity_progress(db,stage="hydrate",completed=len(hydrated),total=len(items),detail="Hydration complete")
    else:
        hydrated=[]
        for i,item in enumerate(items,1):
            if item.transfer_state == TransferState.READY.value:
                if verbose: print(f"[resume  {i}/{len(items)}] READY {item.relative_path}")
                hydrated.append(item)
                continue
            if verbose: print(f"[hydrate {i}/{len(items)}] {item.relative_path}")
            t=perf_counter()
            hydrate_one(db,migration_id=item.migration_id,relative_path=item.relative_path,
                        timeout_seconds=timeout,max_bytes=max_bytes,min_bytes=min_bytes)
            timings["hydrate"] += perf_counter()-t
            hydrated.append(item)
            set_activity_progress(db,stage="hydrate",completed=len(hydrated),total=len(items),detail=item.relative_path)

    emit("hydration_complete",files=len(hydrated))
    with tempfile.TemporaryDirectory(prefix="cloudmover-stage-") as td:
        stage=Path(td)
        t=perf_counter()
        top_paths=stage_batch(hydrated,stage)
        timings["stage"]=perf_counter()-t

        # The exact destination root was claimed before hydration.
        remote_cache={base}
        for item in hydrated:
            ensure_remote_tree(base,item.relative_path,remote_cache)

        for item in hydrated:
            transition_file(db,item.migration_id,item.relative_path,
                            TransferState.UPLOADING,increment_attempts=True)

        try:
            t=perf_counter()
            payload_detail=f"{payload_bytes/1024**2:,.1f} MB" if payload_bytes else ""
            set_activity_progress(db,stage="upload",completed=0,total=0,detail=f"{len(hydrated):,} files • {human_bytes(payload_bytes)}")
            emit("upload_start",files=len(hydrated),logical_bytes=payload_bytes)
            with Activity("Uploading",payload_detail,enabled=not verbose):
                entries=[]
                indexed={}
                for file_index,item in enumerate(hydrated,1):
                    local=stage/item.relative_path
                    parent=base.rstrip("/")
                    if local.parent != stage:
                        parent += "/" + str(Path(item.relative_path).parent)
                    entries.append((local,parent,local.name))
                    indexed[local]=(file_index,item)
                completed=0
                for remote_parent,files in proton.transport_groups(entries):
                    def observe_file(local,event,**data):
                        nonlocal completed
                        file_index,item=indexed[local]
                        if data.get("phase")=="safe_upload_total" and data.get("status")=="completed":
                            completed+=1
                        emit(event,relative_path=item.relative_path,logical_bytes=item.logical_bytes,**data)
                        set_activity_progress(db,stage="upload",completed=completed,total=len(hydrated),
                                              detail=item.relative_path,migration_id=item.migration_id,
                                              relative_path=item.relative_path,logical_bytes=item.logical_bytes,
                                              file_index=file_index,file_total=len(hydrated),
                                              phase=data["phase"],phase_status=data["status"])
                    def observe_group(event,**data):
                        emit(event,files=len(files),**data)
                        set_activity_progress(db,stage="upload",completed=completed,total=len(hydrated),
                                              detail=f"{len(files)} files",migration_id=hydrated[0].migration_id,
                                              relative_path="",file_index=0,file_total=len(hydrated),
                                              phase=data["phase"],phase_status=data["status"])
                    with proton.observe_safe_upload(observe_group):
                        proton.safe_upload_group(files,remote_parent,observe_file=observe_file)
            timings["upload"]=perf_counter()-t
            emit("upload_complete",files=len(hydrated),logical_bytes=payload_bytes,duration_ms=round(timings["upload"]*1000))
            set_activity_progress(db,stage="upload",completed=1,total=1,detail="Upload complete")
        except Exception as exc:
            for item in hydrated:
                try:
                    transition_file(db,item.migration_id,item.relative_path,
                                    TransferState.READY,error=str(exc))
                except Exception:
                    pass
            raise

        emit("verification_start",files=len(hydrated))
        for item in hydrated:
            transition_file(db,item.migration_id,item.relative_path,TransferState.VERIFYING)

        # v0.12.1 first attempts one tree snapshot per top-level folder. If Proton
        # CLI 0.8.x does not expose descendants in that listing shape, fall back to
        # the already-proven exact-parent grouped verification.
        t=perf_counter()
        top_groups={}
        for item in hydrated:
            top=Path(item.relative_path).parts[0]
            top_groups.setdefault(top,[]).append(item)

        verified_count=0
        tree_ok=True
        set_activity_progress(db,stage="verify",completed=0,total=max(1,len(top_groups)),detail="Checking Proton destination")
        try:
            for group_index,(top,group) in enumerate(top_groups.items(),1):
                subtree=base.rstrip("/")+"/"+top
                rels=[]
                for item in group:
                    parts=Path(item.relative_path).parts
                    rels.append("/".join(parts[1:]) if len(parts)>1 else parts[0])
                proton.verify_tree(stage/top,subtree,rels)
                set_activity_progress(db,stage="verify",completed=group_index,total=len(top_groups),detail=f"Verified {top}")
        except proton.TreeVerificationUnavailable:
            tree_ok=False

        if not tree_ok:
            groups={}
            for item in hydrated:
                parts=Path(item.relative_path).parts
                remote_parent=base.rstrip("/")
                if len(parts)>1:
                    remote_parent += "/" + "/".join(parts[:-1])
                groups.setdefault(remote_parent, []).append(item)
            set_activity_progress(db,stage="verify",completed=0,total=max(1,len(groups)),detail="Checking Proton folders")
            for group_index,(remote_parent,group) in enumerate(groups.items(),1):
                locals_=[Path(item.source_path) for item in group]
                proton.verify_many_in_parent(locals_,remote_parent)
                set_activity_progress(db,stage="verify",completed=group_index,total=len(groups),detail=remote_parent)

        for item in hydrated:
            remote_file=base.rstrip("/")+"/"+item.relative_path
            set_destination(db,item,remote_file)
            transition_file(db,item.migration_id,item.relative_path,
                            TransferState.COMPLETE,verified=True)
            verified_count+=1
            print(f"[verify  {verified_count}/{len(items)}] COMPLETE {item.relative_path}")
        emit("verification_complete",files=verified_count)
        timings["verify"]=perf_counter()-t
        set_activity_progress(db,stage="complete",completed=1,total=1,detail="Batch verified")

    total=sum(timings.values())
    print("\nTiming totals")
    print(f"  hydrate+SHA  {timings['hydrate']:8.2f}s")
    print(f"  local stage  {timings['stage']:8.2f}s")
    print(f"  bulk upload  {timings['upload']:8.2f}s")
    print(f"  verification {timings['verify']:8.2f}s")
    print(f"  measured     {total:8.2f}s")
    return len(hydrated)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",type=Path,default=Path(".cloudmover/migration.db"))
    ap.add_argument("--batch",type=int,default=DEFAULT_COUNT)
    ap.add_argument("--min-bytes",type=int,default=DEFAULT_MIN)
    ap.add_argument("--max-bytes",type=int,default=DEFAULT_MAX)
    ap.add_argument("--destination",default=DEFAULT_DEST)
    ap.add_argument("--timeout",type=float,default=300)
    ap.add_argument("--verify-workers",type=int,default=DEFAULT_VERIFY_WORKERS)
    ap.add_argument("--hydrate-workers",type=int,default=1)
    ap.add_argument("--verbose",action="store_true")
    a=ap.parse_args()
    print(f"CloudMover {VERSION}\n")
    print("Bulk end-to-end performance trial")
    print(f"Destination: {a.destination}")
    print("Strategy:    hydrate batch -> hard-link tree -> bounded same-parent temp uploads -> exact verify\n")
    try:
        done=run_bulk(a.db,a.batch,a.min_bytes,a.max_bytes,a.destination,a.timeout,a.verify_workers,hydrate_workers=a.hydrate_workers,verbose=getattr(a,"verbose",False))
    except KeyboardInterrupt:
        print("\nInterrupted. Durable state preserved.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nBulk pipeline failed safely: {exc}")
        raise SystemExit(1)
    print(f"\nCompleted:   {done}")
    print("Eviction/deletion remains disabled.")

if __name__=="__main__":
    main()
