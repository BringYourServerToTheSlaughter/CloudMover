
from __future__ import annotations
import argparse, json, os, shutil, signal, time
from datetime import timedelta
from pathlib import Path

from .bulk_pipeline import run_bulk
from .engine import migration_id, free_bytes
from .release import release_specific, human_bytes
from .state import connect, create_schema, get_latest_migration_id, utc_now
from .ui import dashboard

VERSION="0.18.5"

def engine_pid_path(db:Path)->Path:
    return db.with_name(db.stem + ".engine.pid")

def write_engine_pid(db:Path):
    p=engine_pid_path(db); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(str(os.getpid()))

def clear_engine_pid(db:Path):
    p=engine_pid_path(db)
    try:
        if p.exists() and p.read_text().strip()==str(os.getpid()): p.unlink()
    except OSError:
        pass

def snapshot(db:Path)->dict:
    c=connect(db)
    try:
        create_schema(c)
        mid=get_latest_migration_id(c)
        m=c.execute("SELECT * FROM migrations WHERE id=?",(mid,)).fetchone()
        rows=c.execute(
            """SELECT transfer_state,COUNT(*) n,COALESCE(SUM(logical_bytes),0) bytes
               FROM files WHERE migration_id=? AND selected=1 GROUP BY transfer_state""",(mid,)
        ).fetchall()
        states={str(r["transfer_state"]):{"files":int(r["n"]),"bytes":int(r["bytes"])} for r in rows}
        totals=c.execute(
            """SELECT COUNT(*) n,COALESCE(SUM(logical_bytes),0) bytes
               FROM files WHERE migration_id=? AND selected=1""",(mid,)
        ).fetchone()
        total=int(totals["n"])
        total_bytes=int(totals["bytes"])
        complete=states.get("COMPLETE",{"files":0,"bytes":0})
        return {"id":mid,"source":str(m["source"]),"status":str(m["status"]),
                "total_files":total,"total_bytes":total_bytes,
                "complete_files":complete["files"],"complete_bytes":complete["bytes"],
                "states":states}
    finally:c.close()

def migration_destination(db:Path)->str:
    """Return the destination committed to the migration plan; never invent one."""
    c=connect(db)
    try:
        create_schema(c); mid=get_latest_migration_id(c)
        row=c.execute("SELECT destination FROM migrations WHERE id=?",(mid,)).fetchone()
        destination=str(row["destination"]).strip() if row and row["destination"] else ""
        if not destination:
            raise RuntimeError("migration has no Proton destination; refusing to start")
        return destination
    finally:
        c.close()

def eligible_releases(db:Path,limit:int=1000):
    c=connect(db)
    try:
        create_schema(c); mid=get_latest_migration_id(c)
        rows=c.execute(
            """SELECT migration_id,relative_path FROM files
               WHERE migration_id=? AND selected=1
                 AND transfer_state='COMPLETE' AND verified=1
                 AND COALESCE(local_released,0)=0
               ORDER BY updated_at ASC LIMIT ?""",(mid,limit)
        ).fetchall()
        return [(str(r["migration_id"]),str(r["relative_path"])) for r in rows]
    finally:c.close()

def set_migration_status(db:Path,status:str):
    c=connect(db)
    try:
        create_schema(c); mid=get_latest_migration_id(c)
        c.execute("UPDATE migrations SET status=?,updated_at=? WHERE id=?",(status,utc_now(),mid))
        c.commit()
    finally:c.close()

def normalize_transient_states_for_pause(db:Path)->dict[str,int]:
    """Normalize transient rows to retry-safe states when the engine pauses."""
    c=connect(db)
    try:
        create_schema(c); mid=get_latest_migration_id(c)
        counts={}
        for state in ("HYDRATING","UPLOADING","VERIFYING"):
            counts[state]=int(c.execute(
                "SELECT COUNT(*) n FROM files WHERE migration_id=? AND transfer_state=?",
                (mid,state)).fetchone()["n"])

        c.execute(
            """UPDATE files
               SET transfer_state='PENDING',
                   last_error='Recovered after pause during hydration',
                   updated_at=?
               WHERE migration_id=? AND transfer_state='HYDRATING'""",
            (utc_now(),mid))
        c.execute(
            """UPDATE files
               SET transfer_state='READY',
                   last_error='Recovered after pause during upload',
                   updated_at=?
               WHERE migration_id=? AND transfer_state='UPLOADING'""",
            (utc_now(),mid))
        c.execute(
            """UPDATE files
               SET transfer_state='READY',
                   last_error='Recovered after pause during verification',
                   updated_at=?
               WHERE migration_id=? AND transfer_state='VERIFYING'""",
            (utc_now(),mid))
        c.commit()
        return counts
    finally:
        c.close()

def fmt_duration(seconds:float)->str:
    return str(timedelta(seconds=int(max(0,seconds))))

def progress_line(s:dict,started:float,baseline_files:int=0,baseline_bytes:int=0)->str:
    pct=(100*s["complete_files"]/s["total_files"]) if s["total_files"] else 100
    elapsed=time.monotonic()-started
    session_files=max(0,s["complete_files"]-baseline_files)
    session_bytes=max(0,s["complete_bytes"]-baseline_bytes)
    remaining_files=max(0,s["total_files"]-s["complete_files"])
    remaining_bytes=max(0,s["total_bytes"]-s["complete_bytes"])
    if elapsed>0 and session_bytes>0:
        rate=session_bytes/elapsed
        eta=remaining_bytes/rate
    elif elapsed>0 and session_files>0:
        rate=session_files/elapsed
        eta=remaining_files/rate
    else:
        eta=None
    eta_text=fmt_duration(eta) if eta is not None else "calculating..."
    return (f'Overall: {s["complete_files"]:,}/{s["total_files"]:,} files '
            f'({pct:.2f}%) | {human_bytes(s["complete_bytes"])} / '
            f'{human_bytes(s["total_bytes"])} | elapsed {fmt_duration(elapsed)} | ETA {eta_text}')

def choose_batch(free:int,reserve:int,user_batch:int)->int:
    # Conservative v0.12 adaptive cap: scale count down near reserve.
    headroom=max(0,free-reserve)
    if headroom < 1_000_000_000: return min(user_batch,5)
    if headroom < 5_000_000_000: return min(user_batch,10)
    return user_batch

def log_event(log_path:Path,event:str,**data):
    log_path.parent.mkdir(parents=True,exist_ok=True)
    rec={"ts":utc_now(),"event":event,**data}
    with log_path.open("a",encoding="utf-8") as f:
        f.write(json.dumps(rec,ensure_ascii=False)+"\n")

def drain_releases(db:Path,log_path:Path)->tuple[int,int]:
    released=reclaimed=0
    for mid,rp in eligible_releases(db):
        try:
            _,n=release_specific(db,mid,rp)
            released+=1; reclaimed+=n
            log_event(log_path,"release_complete",relative_path=rp,reclaimed_bytes=n)
        except LookupError:
            continue
    return released,reclaimed

def dry_run(db:Path,reserve:int,batch:int,target_mb:float):
    s=snapshot(db); free=free_bytes(db); chosen=choose_batch(free,reserve,batch)
    print(f"Migration:  {s['id']}")
    print(f"Source:     {s['source']}")
    print(f"Progress:   {s['complete_files']:,}/{s['total_files']:,} files")
    print(f"Data:       {human_bytes(s['complete_bytes'])} / {human_bytes(s['total_bytes'])}")
    print(f"Free disk:  {human_bytes(free)}")
    print(f"Keep free:    {human_bytes(reserve)}")
    print(f"Next batch: target {target_mb:.0f} MB, up to {chosen} files")
    print("Action:     resume READY work first, then hydrate; upload; verify; release")
    print("\nDry run only. No files or migration state were changed.")

def run(db:Path,batch:int,reserve:int,destination:str|None,max_cycles:int|None,
        log_path:Path,min_bytes:int,max_bytes:int,target_mb:float,hydrate_workers:int,verbose:bool=False):
    stored_destination=migration_destination(db)
    if destination is not None and destination.rstrip("/") != stored_destination.rstrip("/"):
        raise RuntimeError(
            f"destination override does not match migration plan: {destination} != {stored_destination}"
        )
    destination=stored_destination
    started=time.monotonic(); cycle=0
    baseline=snapshot(db)
    baseline_files=baseline["complete_files"]
    baseline_bytes=baseline["complete_bytes"]
    set_migration_status(db,"RUNNING")
    write_engine_pid(db)
    log_event(log_path,"start",migration_id=migration_id(db))
    try:
        while True:
            s=snapshot(db)
            print("\n"+progress_line(s,started,baseline_files,baseline_bytes))
            if s["complete_files"] >= s["total_files"]:
                set_migration_status(db,"COMPLETE")
                log_event(log_path,"migration_complete",files=s["total_files"],bytes=s["total_bytes"])
                print("\nMigration COMPLETE.")
                return

            if max_cycles is not None and cycle>=max_cycles:
                normalize_transient_states_for_pause(db)
                set_migration_status(db,"PAUSED")
                log_event(log_path,"cycle_limit",cycles=cycle)
                print("\nCycle limit reached. Migration paused safely.")
                return

            free=free_bytes(db)
            if free <= reserve:
                normalize_transient_states_for_pause(db)
                set_migration_status(db,"PAUSED")
                log_event(log_path,"reserve_stop",free_bytes=free,reserve_bytes=reserve)
                print("\nProtected disk reserve reached. Migration paused safely.")
                return

            # Drain old verified local cache before asking for more staging space.
            released,reclaimed=drain_releases(db,log_path)
            if released:
                print(f"Released backlog: {released} file(s), {human_bytes(reclaimed)} reclaimed")

            chosen=choose_batch(free_bytes(db),reserve,batch)
            cycle+=1
            print(f"\n=== cycle {cycle} | batch up to {chosen} | free {human_bytes(free_bytes(db))} ===")
            log_event(log_path,"cycle_start",cycle=cycle,batch=chosen)

            try:
                target_bytes=int(target_mb*1_000_000)
                run_bulk(db,count=chosen,min_bytes=min_bytes,max_bytes=max_bytes,
                         destination=destination,verify_workers=1,
                         target_bytes=target_bytes,hydrate_workers=hydrate_workers,verbose=verbose)
            except LookupError:
                # No eligible PENDING/READY work: if everything isn't complete, surface state.
                s=snapshot(db)
                if s["complete_files"]>=s["total_files"]:
                    continue
                set_migration_status(db,"PAUSED")
                log_event(log_path,"no_eligible_work",states=s["states"])
                print("\nNo eligible READY/PENDING work remains for current size policy.")
                print("Migration paused; inspect state before changing policy.")
                return

            released,reclaimed=drain_releases(db,log_path)
            print(f"Released: {released} verified file(s), {human_bytes(reclaimed)} reclaimed")
            log_event(log_path,"cycle_complete",cycle=cycle,released=released,reclaimed_bytes=reclaimed)
    except KeyboardInterrupt:
        recovered=normalize_transient_states_for_pause(db)
        set_migration_status(db,"PAUSED")
        log_event(log_path,"user_pause",cycle=cycle,recovered=recovered)
        print("\nPaused safely. Run the same command to resume.")
        return
    except Exception as exc:
        recovered=normalize_transient_states_for_pause(db)
        set_migration_status(db,"PAUSED")
        log_event(log_path,"error",cycle=cycle,error=str(exc),recovered=recovered)
        print(f"\nStopped safely: {exc}")
        raise SystemExit(1)
    finally:
        clear_engine_pid(db)

def main():
    ap=argparse.ArgumentParser(description="CloudMover production-engine trial")
    ap.add_argument("--db",type=Path,default=Path(".cloudmover/migration.db"))
    ap.add_argument("--batch",type=int,default=250)
    ap.add_argument("--reserve-gb",type=float,default=20.0)
    ap.add_argument("--destination",default=None, help="optional safety assertion; must match destination stored in migration plan")
    ap.add_argument("--max-cycles",type=int,default=None)
    ap.add_argument("--target-mb",type=float,default=250.0)
    ap.add_argument("--hydrate-workers",type=int,default=1)
    ap.add_argument("--verbose",action="store_true",
                    help="show detailed per-file migration output")
    ap.add_argument("--min-bytes",type=int,default=1_000_000)
    ap.add_argument("--max-bytes",type=int,default=100_000_000)
    ap.add_argument("--log",type=Path,default=Path(".cloudmover/cloudmover.jsonl"))
    ap.add_argument("--dry-run",action="store_true")
    a=ap.parse_args()
    reserve=int(a.reserve_gb*1_000_000_000)
    print(f"CloudMover {VERSION}")
    print("Continuous SMART_STAGING engine")
    print("Ctrl+C safely pauses; rerun the same command to resume.\n")
    if a.dry_run:
        dry_run(a.db,reserve,a.batch,a.target_mb)
    else:
        run(a.db,a.batch,reserve,a.destination,a.max_cycles,a.log,a.min_bytes,a.max_bytes,a.target_mb,a.hydrate_workers,a.verbose)

if __name__=="__main__": main()
