from __future__ import annotations
from .disk_space import RESERVE_BYTES, BATCH_BYTES
import threading
import argparse, os, signal, subprocess, sys, shutil, time
from datetime import datetime
from pathlib import Path

from .scanner import human_bytes, walk_entries
from .state import connect, create_schema, initialize_migration, reconcile_migration_inventory, get_activity_progress, recover_stale_runtime_state
from .providers.icloud import inspect_file, HydrationState
from .app_paths import default_db_path, default_log_path, ensure_app_support
from .destination import preflight_destination
from .event_log import MigrationEventReader, friendly_engine_failure

from .version import VERSION

def snapshot(db_path: Path) -> dict:
    conn=connect(db_path)
    try:
        create_schema(conn)
        m=conn.execute("SELECT * FROM migrations ORDER BY created_at DESC LIMIT 1").fetchone()
        if not m:
            raise RuntimeError("No migration found in database.")
        mid=m["id"]

        totals=conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(logical_bytes),0) b
               FROM files WHERE migration_id=? AND selected=1""",(mid,)).fetchone()
        done=conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(logical_bytes),0) b
               FROM files WHERE migration_id=? AND selected=1 AND transfer_state='COMPLETE'""",
            (mid,)).fetchone()
        all_totals=conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(logical_bytes),0) b
               FROM files WHERE migration_id=?""",(mid,)).fetchone()
        active=conn.execute(
            """SELECT relative_path,source_path,transfer_state,logical_bytes
               FROM files
               WHERE migration_id=? AND selected=1
                 AND transfer_state IN ('HYDRATING','UPLOADING','VERIFYING')
               ORDER BY updated_at DESC LIMIT 1""",(mid,)).fetchone()
        active_count=int(conn.execute(
            """SELECT COUNT(*) n FROM files
               WHERE migration_id=? AND selected=1
                 AND transfer_state IN ('HYDRATING','UPLOADING','VERIFYING')""",
            (mid,)).fetchone()["n"])
        active_bytes=int(conn.execute(
            """SELECT COALESCE(SUM(logical_bytes),0) b FROM files
               WHERE migration_id=? AND selected=1
                 AND transfer_state IN ('HYDRATING','UPLOADING','VERIFYING')""",
            (mid,)).fetchone()["b"])
        released=conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(logical_bytes),0) b FROM files
               WHERE migration_id=? AND selected=1 AND local_released=1""",(mid,)).fetchone()
        failed=conn.execute(
            """SELECT COUNT(*) n FROM files
               WHERE migration_id=? AND selected=1 AND transfer_state='FAILED'""",(mid,)).fetchone()
        failed_rows=conn.execute(
            """SELECT relative_path,last_error
               FROM files
               WHERE migration_id=? AND selected=1 AND transfer_state='FAILED'
               ORDER BY updated_at DESC, relative_path ASC
               LIMIT 6""",(mid,)).fetchall()
        verified=conn.execute(
            """SELECT COUNT(*) n FROM files
               WHERE migration_id=? AND selected=1 AND verified=1""",(mid,)).fetchone()
        cleanup_warnings=int(conn.execute(
            "SELECT COUNT(*) FROM files WHERE migration_id=? AND selected=1 AND transfer_state='COMPLETE' AND verified=1 AND release_error IS NOT NULL AND COALESCE(local_released,0)=0",(mid,)).fetchone()[0])
        activity=get_activity_progress(db_path)
        if activity.get("migration_id") and activity["migration_id"] != mid:
            activity={}

        return dict(
            m=m, done=done, active=active, active_count=active_count,
            active_bytes=active_bytes, released=released,
            selected_files=int(totals["n"]), selected_bytes=int(totals["b"]),
            all_files=int(all_totals["n"]), all_bytes=int(all_totals["b"]),
            failed_files=int(failed["n"]), verified_files=int(verified["n"]),
            failed_rows=failed_rows, activity=activity, cleanup_warnings=cleanup_warnings
        )
    finally:
        conn.close()

def selection_rows(db_path: Path):
    conn=connect(db_path)
    try:
        create_schema(conn)
        m=conn.execute("SELECT id,status,source FROM migrations ORDER BY created_at DESC LIMIT 1").fetchone()
        if not m:
            return None,[]
        rows=conn.execute(
            """SELECT relative_path,logical_bytes,selected,transfer_state
               FROM files WHERE migration_id=? ORDER BY relative_path""",(m["id"],)).fetchall()
        return m,rows
    finally:
        conn.close()

def set_selection_prefix(db_path: Path, prefix: str, selected: bool) -> int:
    """Select/deselect unfinished rows at one path or below one folder."""
    conn=connect(db_path)
    try:
        create_schema(conn)
        m=conn.execute("SELECT id,status,source FROM migrations ORDER BY created_at DESC LIMIT 1").fetchone()
        if not m:
            raise RuntimeError("No migration found.")
        if str(m["status"]).upper()=="RUNNING":
            raise RuntimeError("Pause CloudMover before changing the migration selection.")
        value=1 if selected else 0
        if not prefix:
            cur=conn.execute(
                """UPDATE files SET selected=?
                   WHERE migration_id=? AND transfer_state!='COMPLETE'""",(value,m["id"]))
        else:
            escaped=prefix.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
            cur=conn.execute(
                """UPDATE files SET selected=?
                   WHERE migration_id=? AND transfer_state!='COMPLETE'
                     AND (relative_path=? OR relative_path LIKE ? ESCAPE '\\')""",
                (value,m["id"],prefix,escaped+"/%"))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

def migration_has_started(db_path: Path) -> bool:
    conn=connect(db_path)
    try:
        create_schema(conn)
        m=conn.execute("SELECT id FROM migrations ORDER BY created_at DESC LIMIT 1").fetchone()
        if not m: return False
        return conn.execute(
            """SELECT 1 FROM files WHERE migration_id=?
               AND (attempts>0 OR transfer_state!='PENDING') LIMIT 1""",(m["id"],)
        ).fetchone() is not None
    finally:
        conn.close()

def elapsed_between(start, end) -> str:
    try:
        started=datetime.fromisoformat(str(start))
        finished=datetime.fromisoformat(str(end))
    except (TypeError,ValueError):
        return "Unknown"
    seconds=max(0,int((finished-started).total_seconds()))
    hours,seconds=divmod(seconds,3600)
    minutes,seconds=divmod(seconds,60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def source_directories(source: Path) -> list[str]:
    """Return the live iCloud directory hierarchy, including empty folders."""
    source=Path(source).expanduser()
    result=[]
    if not source.exists():
        return result
    for entry in walk_entries(source):
        try:
            if entry.is_dir(follow_symlinks=False):
                result.append(str(Path(entry.path).relative_to(source)))
        except (PermissionError,FileNotFoundError,OSError,ValueError):
            continue
    return result

def build_tree_index(rows, directories=None):
    root={"dirs":{},"files":[],"selected":0,"total":0,"bytes":0,"selected_bytes":0}
    for relative in directories or []:
        node=root
        for part in Path(str(relative)).parts:
            node=node["dirs"].setdefault(
                part,{"dirs":{},"files":[],"selected":0,"total":0,"bytes":0,"selected_bytes":0}
            )
    for r in rows:
        parts=Path(str(r["relative_path"])).parts
        node=root
        is_selected=bool(r["selected"])
        size=int(r["logical_bytes"])
        node["total"]+=1; node["bytes"]+=size
        if is_selected:
            node["selected"]+=1; node["selected_bytes"]+=size
        for part in parts[:-1]:
            node=node["dirs"].setdefault(part,{"dirs":{},"files":[],"selected":0,"total":0,"bytes":0,"selected_bytes":0})
            node["total"]+=1; node["bytes"]+=size
            if is_selected:
                node["selected"]+=1; node["selected_bytes"]+=size
        if parts:
            node["files"].append((parts[-1],str(r["relative_path"]),size,is_selected,str(r["transfer_state"])))
    return root

def has_migration(db_path: Path) -> bool:
    conn=connect(db_path)
    try:
        create_schema(conn)
        return conn.execute("SELECT 1 FROM migrations LIMIT 1").fetchone() is not None
    finally:
        conn.close()

def default_icloud_path() -> Path:
    return Path.home()/"Library/Mobile Documents/com~apple~CloudDocs"

def source_label(source):
    source=Path(source)
    return "iCloud Drive" if source == default_icloud_path() else (source.name or str(source))


def completion_dialog_content(s):
    """Presentation only: never manufacture completion from a report-button click."""
    complete=(str(s['m']['status']).upper()=='COMPLETE' and
              int(s['done']['n'])==s['selected_files']==s['verified_files'] and
              not s['failed_files'])
    warnings=int(s.get('cleanup_warnings',0))
    if not complete:
        title='Migration progress'
        summary=f"{s['verified_files']:,} / {s['selected_files']:,} files verified"
        icon='•'
    else:
        title='Completed with warnings' if warnings else 'Migration complete'
        summary=f"{s['verified_files']:,} files successfully copied and verified"
        icon='!' if warnings else '✓'
    return dict(title=title,summary=summary,icon=icon,warning=complete and warnings>0,
                transferred=f"{int(s['done']['b'])/1_000_000:,.2f} MB transferred",
                destination=friendly_destination(str(s['m']['destination'] or 'Not set')),
                detail=(f"{warnings:,} local {'copy could' if warnings==1 else 'copies could'} not be released. "
                        "The Proton copies are verified; you can review local cleanup later.") if complete and warnings else '',
                safety='Originals remain safely in iCloud')


def new_destination(source=None):
    source=Path(source) if source is not None else default_icloud_path()
    name=source_label(source)
    return f"/my-files/{name}"


def friendly_destination(path):
    parts = path.strip("/").split("/")
    if parts and parts[0] == "my-files":
        parts[0] = "Proton Drive"
    return " → ".join(parts)


def icloud_readiness(source):
    root = default_icloud_path().expanduser().resolve()
    source = Path(source).expanduser().resolve()
    if not root.is_dir():
        return False, "iCloud Drive is unavailable on this Mac. Enable iCloud Drive in System Settings and wait for its folder to appear. Apple account state could not be confirmed."
    if source != root and root not in source.parents:
        return False, "Choose a folder inside the available iCloud Drive location."
    if not source.is_dir():
        return False, "The selected iCloud folder is unavailable. Choose an existing folder."
    try:
        if not os.access(source, os.R_OK | os.X_OK):
            raise PermissionError()
        with os.scandir(source):
            pass  # Open the directory only; never read file contents.
    except OSError:
        return False, "CloudMover cannot read this iCloud folder. Check folder permissions and iCloud Drive availability."
    return True, "iCloud Drive folder is available and readable. Apple account / iCloud service state could not be confirmed."


def find_proton_drive_cli() -> str | None:
    found=shutil.which("proton-drive")
    if found:
        return found
    for candidate in (
        "/usr/local/bin/proton-drive",
        "/opt/homebrew/bin/proton-drive",
    ):
        path=Path(candidate)
        if path.is_file() and os.access(path,os.X_OK):
            return str(path)
    return None

def validate_proton_destination(path: str) -> tuple[bool, str]:
    path=(path or "").strip()
    if not path:
        return False,"Enter a Proton destination."
    if not path.startswith("/my-files/"):
        return False,"Destination must start with /my-files/."
    if path.rstrip("/") == "/my-files":
        return False,"Choose a folder inside /my-files, not /my-files itself."
    if path.endswith("/"):
        return False,"Remove the trailing slash from the destination."
    parts=path.split("/")
    if "" in parts[1:]:
        return False,"Destination cannot contain empty path segments."
    if any(part in (".","..") for part in parts):
        return False,"Destination cannot contain . or .. path segments."
    return True,"This exact folder will be created. Existing destinations are refused; choose a new name if it already exists."

def current_upload_text(activity):
    index=int(activity.get("file_index",0)); total=int(activity.get("file_total",0))
    phase=activity.get("phase","")
    label={"safe_upload_total":"Preparing upload", "destination_preflight":"Checking destination",
           "cli_upload":"Uploading", "temp_verification":"Verifying uploaded temporary copy",
           "rename_promote":"Promoting verified upload", "final_verification":"Verifying final Proton copy"}.get(phase,"Processing")
    if phase=="safe_upload_total" and activity.get("phase_status")=="completed":
        label="Upload verified; awaiting batch verification for"
    if not index:
        return f"{label} · {activity.get('detail','upload group')}…"
    return f"{label} file {index:,} of {total:,}…"


def active_stage_text(snapshot):
    """Describe running work using file state and matching measured batch activity.

    Verification activity counts folder groups, not files. Never label those counts
    as files, and never use an old phase's activity as the current phase's progress.
    """
    active = snapshot.get("active")
    if not active:
        if int(snapshot["done"]["n"]) >= int(snapshot["selected_files"]):
            return "Finalizing migration…"
        return "Preparing migration…"
    state = str(active["transfer_state"])
    activity = snapshot.get("activity") or {}
    expected = {"HYDRATING": "hydrate", "UPLOADING": "upload", "VERIFYING": "verify"}.get(state)
    total = int(activity.get("total", 0))
    completed = int(activity.get("completed", 0))
    if state == "HYDRATING":
        if activity.get("stage") == expected and total > 0 and 0 <= completed < total:
            return f"Hydrating file {completed + 1:,} of {total:,} — downloading from iCloud locally for transfer…"
        return "Hydrating — downloading from iCloud locally for transfer…"
    if state == "UPLOADING" and activity.get("stage")=="upload" and activity.get("phase"):
        return current_upload_text(activity)
    if state == "UPLOADING":
        return "Uploading batch to Proton Drive…"
    if state == "VERIFYING":
        return "Verifying Proton copies…"
    return "Preparing migration…"


def main():
    try:
        from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
        from PySide6.QtWidgets import (
            QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QProgressBar,
            QFrame,QPushButton,QTreeWidget,QTreeWidgetItem,QSplitter,QLineEdit,
            QDialog,QStackedWidget,QFileDialog,QMessageBox,QCheckBox,QSizePolicy
        )
    except ImportError:
        raise SystemExit("CloudMover GUI requires PySide6. Install with: python3 -m pip install PySide6")

    class MigrationScanWorker(QObject):
        progress=Signal(object)
        finished=Signal(object,object)
        failed=Signal(str)

        def __init__(self,source,db_path,destination):
            super().__init__()
            self.source=source
            self.db_path=db_path
            self.destination=destination
            self.started_at=0.0
            self.last_progress=0.0
            self.latest_progress={}
            self.cancel_event=threading.Event()

        @Slot()
        def run(self):
            try:
                self.started_at=time.monotonic()
                self.last_progress=0.0
                def report(progress):
                    now=time.monotonic()
                    payload=dict(progress)
                    payload["elapsed"]=now-self.started_at
                    self.latest_progress=payload
                    if not payload.get("final") and now-self.last_progress < 0.125:
                        return
                    self.last_progress=now
                    self.progress.emit(payload)
                if self.cancel_event.is_set():
                    raise RuntimeError("Scan cancelled")
                preflight_destination(self.destination)
                if self.cancel_event.is_set():
                    raise RuntimeError("Scan cancelled")
                summary=initialize_migration(self.source,self.db_path,self.destination,report,cancel_check=self.cancel_event.is_set)
                self.finished.emit(summary,self.latest_progress)
            except Exception as exc:
                self.failed.emit(str(exc))

    class SetupDialog(QDialog):
        """First-run wizard for the v1 product scope: iCloud Drive → Proton Drive."""
        def __init__(self,db_path):
            super().__init__()
            self.db_path=db_path
            self.source=default_icloud_path()
            self.destination=new_destination(self.source)
            self.destination_custom=False
            self.scan_thread=None
            self.scan_worker=None
            self.scan_summary=None
            self.setWindowTitle(f"CloudMover {VERSION} — Set Up Migration")
            self.resize(760,640)
            self.setMinimumSize(680,600)
            self.setStyleSheet("""
              QDialog,QWidget { background:#0b1c31; color:#f3f4f6; font-size:14px; }
              QLabel { background:transparent; }
              QCheckBox::indicator { width:20px; height:20px; border:1px solid #94a3b8; border-radius:3px; }
              QCheckBox::indicator:checked { background:#2563eb; }
              QLabel#hero { font-size:28px; font-weight:700; color:#f8fafc; }
              QLabel#step { font-size:20px; font-weight:650; color:#f8fafc; }
              QLabel#muted { color:#cbd5e1; }
              QLabel#sourceTitle { font-size:16px; font-weight:700; color:#f8fafc; }
              QLabel#sourceChanged { color:#fde68a; font-weight:650; }
              QPushButton { border-radius:8px; padding:10px 18px; font-weight:700;
                            color:#f8fafc; background:#17324c; border:1px solid #47617b; }
              QPushButton#primaryButton { color:white; background:#2563eb; border:1px solid #60a5fa; }
              QPushButton#secondaryButton { color:#e5edf6; background:#12273d; border:1px solid #3d536b; }
              QPushButton:disabled, QPushButton#primaryButton:disabled, QPushButton#secondaryButton:disabled,
              QPushButton#resumeButton:disabled, QPushButton#pauseButton:disabled, QPushButton#newMigrationButton:disabled { color:#8190a5; background:#183047; border:1px solid #334a60; }
              QFrame#card { background:#10283f; border:1px solid #334a60; border-radius:10px; }
              QLineEdit { background:#10283f; border:1px solid #405269; border-radius:8px;
                          padding:9px 12px; color:#f8fafc; }
            """)
            lay=QVBoxLayout(self); lay.setContentsMargins(36,30,36,30); lay.setSpacing(14)
            self.pages=QStackedWidget(); lay.addWidget(self.pages,1)

            # Welcome
            p=QWidget(); l=QVBoxLayout(p)
            hero=QLabel("Move from iCloud Drive to Proton Drive"); hero.setObjectName("hero"); hero.setWordWrap(True); l.addWidget(hero)
            sub=QLabel("CloudMover safely migrates your files in small verified batches, even when your Mac does not have enough free space to download your entire iCloud Drive at once.")
            sub.setObjectName("muted"); sub.setWordWrap(True); l.addWidget(sub)
            l.addSpacing(20)
            for txt in ["✓ Originals remain in iCloud","✓ Proton copies are verified before local space is released","✓ Pause and resume whenever you need to"]:
                q=QLabel(txt); q.setObjectName("muted"); l.addWidget(q)
            self.consent=QCheckBox()
            consent_text=QLabel("I understand that CloudMover will copy my selected files to Proton Drive, verify the Proton copies before releasing local iCloud cache space, and leave the original files in iCloud.")
            consent_text.setWordWrap(True)
            consent_row=QHBoxLayout(); consent_row.addWidget(self.consent); consent_row.addWidget(consent_text,1)
            l.addLayout(consent_row)
            self.consent.toggled.connect(self.refresh_page)
            l.addStretch(); self.pages.addWidget(p)

            # System check
            p=QWidget(); l=QVBoxLayout(p)
            t=QLabel("System check"); t.setObjectName("step"); l.addWidget(t)
            system_layout=l
            source_card=QFrame(); source_card.setObjectName("card"); system_layout.addWidget(source_card)
            l=QVBoxLayout(source_card)
            self.source_title=QLabel(); self.source_title.setObjectName("sourceTitle"); self.source_title.setWordWrap(True); l.addWidget(self.source_title)
            self.source_path=QLabel(); self.source_path.setObjectName("muted"); self.source_path.setWordWrap(True); l.addWidget(self.source_path)
            self.source_note=QLabel(); self.source_note.setObjectName("sourceChanged"); self.source_note.setWordWrap(True); l.addWidget(self.source_note)
            self.check_text=QLabel(); self.check_text.setMinimumHeight(175); self.check_text.setObjectName("muted"); self.check_text.setWordWrap(True); l.addWidget(self.check_text)
            choose=QPushButton("Choose a Different iCloud Folder"); choose.setObjectName("secondaryButton"); choose.clicked.connect(self.choose_source); l.addWidget(choose)
            destination_card=QFrame(); destination_card.setObjectName("card"); system_layout.addWidget(destination_card)
            l=QVBoxLayout(destination_card)
            dest_label=QLabel("Proton destination"); dest_label.setObjectName("muted"); l.addWidget(dest_label)
            self.dest_name=QLabel(friendly_destination(self.destination)); self.dest_name.setWordWrap(True); l.addWidget(self.dest_name)
            self.dest_edit=QLineEdit(self.destination)
            self.dest_edit.setPlaceholderText("/my-files/My iCloud Backup")
            self.dest_edit.textChanged.connect(self.destination_changed)
            l.addWidget(self.dest_edit)
            self.dest_help=QLabel(); self.dest_help.setObjectName("muted"); self.dest_help.setWordWrap(True); l.addWidget(self.dest_help)
            self.dest_edit.setStyleSheet("font-size:11px; color:#94a3b8")
            self.pages.addWidget(p)

            # Scan/review
            p=QWidget(); l=QVBoxLayout(p)
            self.scan_title=QLabel(f"Scan {source_label(self.source)}"); self.scan_title.setObjectName("step"); l.addWidget(self.scan_title)
            q=QLabel("CloudMover will inventory file names, sizes, and iCloud hydration state without intentionally downloading cloud-only file contents.")
            q.setObjectName("muted"); q.setWordWrap(True); l.addWidget(q)
            self.scan_text=QLabel("Ready to scan."); self.scan_text.setObjectName("muted"); self.scan_text.setWordWrap(True); l.addWidget(self.scan_text)
            self.scan_bar=QProgressBar(); self.scan_bar.setTextVisible(False); self.scan_bar.hide(); l.addWidget(self.scan_bar)
            self.scan_activity=QLabel(""); self.scan_activity.setObjectName("muted"); self.scan_activity.setWordWrap(True); l.addWidget(self.scan_activity)
            self.scan_reassurance=QLabel("No files are being moved yet.\nCloud-only files are not downloaded during this scan.")
            self.scan_reassurance.setObjectName("muted"); self.scan_reassurance.setWordWrap(True); l.addWidget(self.scan_reassurance)
            l.addStretch(); self.pages.addWidget(p)

            # Review
            p=QWidget(); l=QVBoxLayout(p)
            t=QLabel("Ready to choose what moves"); t.setObjectName("step"); l.addWidget(t)
            self.review=QLabel(); self.review.setObjectName("muted"); self.review.setWordWrap(True); l.addWidget(self.review)
            l.addSpacing(12)
            q=QLabel("After setup, the migration screen opens with the file/folder picker. Nothing transfers until you press Start Migration.")
            q.setObjectName("muted"); q.setWordWrap(True); l.addWidget(q)
            l.addStretch(); self.pages.addWidget(p)

            nav=QHBoxLayout()
            self.back=QPushButton("Back"); self.back.setObjectName("secondaryButton"); self.back.clicked.connect(self.go_back)
            self.next=QPushButton("Get Started"); self.next.setObjectName("primaryButton"); self.next.clicked.connect(self.go_next)
            self.cancel_scan=QPushButton("Cancel Scan"); self.cancel_scan.hide()
            self.cancel_scan.clicked.connect(self.request_scan_cancel)
            nav.addWidget(self.cancel_scan)
            self.scan_clock=QTimer(self); self.scan_clock.setInterval(1000)
            self.scan_clock.timeout.connect(self.tick_scan)
            self.latest_scan_progress={}
            nav.addWidget(self.back); nav.addStretch(); nav.addWidget(self.next); lay.addLayout(nav)
            self.refresh_page()

        def choose_source(self):
            chosen=QFileDialog.getExistingDirectory(self,"Choose iCloud Drive",str(self.source))
            if chosen:
                self.source=Path(chosen)
                if not self.destination_custom:
                    self.dest_edit.blockSignals(True)
                    self.destination=new_destination(self.source)
                    self.dest_edit.setText(self.destination)
                    self.dest_edit.blockSignals(False)
                self.scan_summary=None
                self.refresh_page()

        def destination_changed(self,text):
            self.destination_custom=True
            self.destination=str(text).strip()
            self.scan_summary=None
            if self.pages.currentIndex()==1:
                self.refresh_page()

        def checks(self):
            source_ok,self.readiness_message=icloud_readiness(self.source)
            proton=find_proton_drive_cli()
            free=0
            try:
                if source_ok:
                    u=os.statvfs(self.source); free=u.f_bavail*u.f_frsize
            except OSError: pass
            return source_ok,proton,free

        def source_changed_from_default(self):
            try:
                return self.source.expanduser().resolve() != default_icloud_path().expanduser().resolve()
            except OSError:
                return Path(self.source).expanduser() != default_icloud_path().expanduser()

        def source_folder_name(self):
            return "iCloud Drive" if not self.source_changed_from_default() else (self.source.name or str(self.source))

        def refresh_page(self):
            i=self.pages.currentIndex()
            self.back.setVisible(i>0)
            self.back.setEnabled(not self.scan_running())
            if self.scan_running():
                self.next.setEnabled(False)
                self.next.setText("Cancelling scan…" if self.scan_worker.cancel_event.is_set() else "Scanning…")
                return
            if i==0:
                self.next.setText("Get Started")
                self.next.setEnabled(self.consent.isChecked())
            elif i==1:
                source_ok,proton,free=self.checks()
                destination_ok,destination_message=validate_proton_destination(self.destination)
                self.source_title.setText(f"Selected source: {self.source_folder_name()}")
                self.source_path.setText(f"Full path: {self.source}")
                self.source_note.setText(
                    "Custom source selected — this is different from the default iCloud Drive root."
                    if self.source_changed_from_default() else
                    "Default iCloud Drive root selected."
                )
                space_ok=free >= RESERVE_BYTES + 2*BATCH_BYTES
                self.check_text.setText(
                    f"{'✓' if source_ok else '✕'} {self.readiness_message}\n\n"
                    f"{'✓' if proton else '✕'} Proton Drive command-line service: {'available' if proton else 'not found'}\n\n"
                    f"{'✓' if space_ok else '✕'} Free disk space: {human_bytes(free)} free. Keep 5 GB free, plus up to 500 MB for a typical 250 MB batch. Larger files require more space."
                )
                self.check_text.setStyleSheet("background:#142c42; border-radius:8px; padding:12px; color:" + ("#cbd5e1" if source_ok and proton and space_ok else "#fda4af"))
                self.dest_name.setText(friendly_destination(self.destination))
                self.dest_help.setText(
                    f"{'✓' if destination_ok else '✕'} {destination_message}"
                )
                self.next.setText("Continue")
                self.next.setEnabled(bool(source_ok and proton and destination_ok and space_ok))
            elif i==2:
                self.scan_title.setText(f"Scan {source_label(self.source)}")
                self.next.setText("Review Migration" if self.scan_summary else f"Scan {source_label(self.source)}")
                self.next.setEnabled(bool(self.scan_summary) or not self.scan_running())
            else:
                self.next.setText("Choose Files")
                self.next.setEnabled(True)

        def scan_running(self):
            return self.scan_thread is not None

        def elide_path(self,path,limit=84):
            path=str(path or "")
            if len(path)<=limit:
                return path
            keep=max(10,(limit-3)//2)
            return f"{path[:keep]}...{path[-keep:]}"

        def go_back(self):
            if self.scan_running():
                return
            if self.pages.currentIndex()>0:
                self.pages.setCurrentIndex(self.pages.currentIndex()-1)
                self.refresh_page()

        def go_next(self):
            i=self.pages.currentIndex()
            if self.scan_running():
                return
            if i==0 and not self.consent.isChecked():
                return
            if i in (1,2) and not self.scan_summary:
                source_ok,proton,free=self.checks()
                if not (source_ok and proton and free >= RESERVE_BYTES + 2*BATCH_BYTES):
                    self.pages.setCurrentIndex(1); self.refresh_page(); return
            if i<2:
                if i==1:
                    ok,msg=validate_proton_destination(self.destination)
                    if not ok:
                        QMessageBox.warning(self,"Check Proton destination",msg)
                        self.refresh_page()
                        return
                self.pages.setCurrentIndex(i+1); self.refresh_page(); return
            if i==2:
                if self.scan_running():
                    return
                if self.scan_summary:
                    self.show_scan_review()
                    return
                self.next.setEnabled(False); self.back.setEnabled(False)
                self.scan_text.setText(f"Scanning {source_label(self.source)}… this can take a few minutes for a large library.")
                self.scan_activity.setText("Preparing scan…")
                self.scan_reassurance.setText("No files are being moved yet.\nCloud-only files are not downloaded during this scan.")
                self.scan_bar.setRange(0,0)
                self.scan_bar.show()
                self.scan_summary=None
                self.destination=self.destination.strip()
                self.scan_thread=QThread(self)
                self.scan_worker=MigrationScanWorker(self.source,self.db_path,self.destination)
                self.scan_worker.moveToThread(self.scan_thread)
                self.scan_thread.started.connect(self.scan_worker.run)
                self.scan_worker.progress.connect(self.scan_progress)
                self.scan_worker.finished.connect(self.scan_finished)
                self.scan_worker.failed.connect(self.scan_failed)
                self.scan_worker.finished.connect(self.scan_thread.quit)
                self.scan_worker.failed.connect(self.scan_thread.quit)
                self.scan_worker.finished.connect(self.scan_worker.deleteLater)
                self.scan_worker.failed.connect(self.scan_worker.deleteLater)
                self.scan_thread.finished.connect(self.scan_thread.deleteLater)
                self.scan_started=time.monotonic()
                self.latest_scan_progress={}
                self.cancel_scan.setEnabled(True); self.cancel_scan.show()
                self.scan_clock.start()
                self.scan_thread.start()
                return
            self.accept()

        def reject(self):
            if self.scan_running():
                self.request_scan_cancel()
                return
            super().reject()

        def closeEvent(self,event):
            if self.scan_running():
                self.request_scan_cancel(); event.ignore(); return
            super().closeEvent(event)

        def request_scan_cancel(self):
            if self.scan_worker is not None:
                self.scan_worker.cancel_event.set()
                self.cancel_scan.setEnabled(False)
                self.next.setEnabled(False); self.back.setEnabled(False)
                self.next.setText("Cancelling scan…")
                self.scan_text.setText("Cancelling scan…")
                self.scan_reassurance.setText("Waiting for the current macOS/iCloud filesystem call to return, then stopping safely. No incomplete inventory will be available to continue.")
                self.tick_scan()

        def tick_scan(self):
            progress=dict(self.latest_scan_progress)
            progress["elapsed"]=time.monotonic()-self.scan_started
            self.scan_progress(progress)

        def scan_progress(self,progress):
            self.latest_scan_progress=dict(progress)
            self.scan_activity.setText(
                f"Now scanning: {self.elide_path(progress.get('relative_path',''))}\n"
                f"Files: {int(progress.get('files',0)):,} • "
                f"Folders: {int(progress.get('folders',0)):,} • "
                f"Logical bytes: {human_bytes(int(progress.get('logical_bytes',0)))} • "
                f"Cloud-only files: {int(progress.get('cloud_only',0)):,} • "
                f"Elapsed: {int(progress.get('elapsed',0))}s\n"
                + ("Cancelling scan… waiting for safe shutdown." if self.scan_worker and self.scan_worker.cancel_event.is_set()
                   else "Estimating time remaining… Total folder contents are not yet known.")
            )

        def scan_finished(self,summary,progress):
            self.finish_scan_thread()
            self.scan_thread=None
            self.scan_worker=None
            files=int(progress.get("files",summary.files)) if progress else summary.files
            folders=int(progress.get("folders",0)) if progress else 0
            logical_bytes=int(progress.get("logical_bytes",summary.logical_bytes)) if progress else summary.logical_bytes
            cloud_only=int(progress.get("cloud_only",0)) if progress else 0
            self.scan_summary={
                "summary":summary,
                "source_folder":self.source_folder_name(),
                "source":str(self.source),
                "destination":self.destination,
                "files":files,
                "folders":folders,
                "logical_bytes":logical_bytes,
                "cloud_only":cloud_only,
            }
            self.scan_text.setText(
                f"Scan complete — {files:,} files • {human_bytes(logical_bytes)}"
            )
            self.scan_bar.hide()
            self.scan_activity.setText(
                f"Selected source folder: {self.scan_summary['source_folder']} ({self.scan_summary['source']})\n"
                f"Proton destination: {friendly_destination(self.scan_summary['destination'])}\n"
                f"Files found: {files:,} • Folders found: {folders:,} • "
                f"Logical size: {human_bytes(logical_bytes)} • Cloud-only files found: {cloud_only:,}"
            )
            self.review.setText(
                f"{summary.files:,} files found\n"
                f"{human_bytes(summary.logical_bytes)} total\n"
                f"Strategy: {summary.strategy.replace('_',' ').title()}\n"
                f"Protected disk reserve: {human_bytes(summary.reserve_bytes)}\n\n"
                "Next, choose exactly which files and folders you want to migrate."
            )
            self.back.setEnabled(True)
            self.refresh_page()

        def show_scan_review(self):
            self.pages.setCurrentIndex(3)
            self.back.setEnabled(False)
            self.refresh_page()

        def finish_scan_thread(self):
            self.scan_clock.stop(); self.cancel_scan.hide()
            self.scan_reassurance.setText("No files are being moved yet.\nCloud-only files are not downloaded during this scan.")
            self.scan_thread.quit()
            self.scan_thread.wait()

        def scan_failed(self,message):
            self.finish_scan_thread()
            self.scan_summary=None
            self.scan_text.setText("Scan cancelled. You can scan again." if message == "Scan cancelled" else "Scan failed. Check the source and try again.")
            self.scan_thread=None
            self.scan_worker=None
            self.scan_bar.hide()
            if message != "Scan cancelled":
                QMessageBox.critical(self,"Scan failed",message)
            self.next.setEnabled(True); self.back.setEnabled(True)
            self.refresh_page()

    class CompletionDialog(QDialog):
        def __init__(self,s,parent=None):
            super().__init__(parent)
            content=completion_dialog_content(s)
            self.setWindowTitle(content['title'])
            self.setMinimumWidth(520)
            self.setStyleSheet("""
                QDialog { background:#0b1c31; color:#f3f4f6; }
                QLabel { color:#e2e8f0; font-size:15px; background:transparent; }
                QLabel#completionHeading { color:#f8fafc; font-size:26px; font-weight:700; }
                QLabel#completionIcon { font-size:32px; font-weight:700; }
                QLabel#completionDestination { background:#142c42; border-radius:8px; padding:14px; }
                QLabel#completionCaption { color:#cbd5e1; font-size:13px; }
                QPushButton { background:#2563eb; color:white; border:0; border-radius:7px; padding:10px 28px; font-size:15px; }
                QPushButton:hover { background:#1d4ed8; }
                QPushButton:focus { border:2px solid #bfdbfe; }
            """)
            layout=QVBoxLayout(self); layout.setContentsMargins(28,24,28,24); layout.setSpacing(14)
            heading=QHBoxLayout(); heading.setSpacing(14)
            self.icon=QLabel(content['icon']); self.icon.setObjectName('completionIcon')
            self.icon.setAccessibleName('Completed with warnings' if content['warning'] else content['title'])
            self.icon.setStyleSheet('color:#fcd34d' if content['warning'] else 'color:#86efac')
            self.heading=QLabel(content['title']); self.heading.setObjectName('completionHeading')
            heading.addWidget(self.icon); heading.addWidget(self.heading,1); layout.addLayout(heading)
            self.summary=QLabel(content['summary']); layout.addWidget(self.summary)
            self.transferred=QLabel(content['transferred']); self.transferred.setObjectName('completionCaption'); layout.addWidget(self.transferred)
            layout.addSpacing(6)
            caption=QLabel('Destination'); caption.setObjectName('completionCaption'); layout.addWidget(caption)
            self.destination=QLabel(content['destination']); self.destination.setObjectName('completionDestination'); layout.addWidget(self.destination)
            self.warning=QLabel(content['detail']); self.warning.setStyleSheet('color:#fcd34d'); self.warning.setVisible(bool(content['detail'])); layout.addWidget(self.warning)
            self.safety=QLabel(content['safety']); layout.addWidget(self.safety)
            for label in (self.heading,self.summary,self.transferred,self.destination,self.warning,self.safety):
                label.setTextFormat(Qt.PlainText); label.setWordWrap(True)
            buttons=QHBoxLayout(); buttons.addStretch()
            self.done_button=QPushButton('Done'); self.done_button.setDefault(True); self.done_button.clicked.connect(self.accept)
            buttons.addWidget(self.done_button); layout.addLayout(buttons)

    class Window(QWidget):
        def __init__(self,db_path):
            super().__init__()
            self.db_path=db_path
            self.engine_events=MigrationEventReader(default_log_path())
            self.engine_failure_details=""
            self._selection_loading=False
            self.setWindowTitle(f"CloudMover {VERSION}")
            self.resize(1260,930)
            self.setMinimumSize(1000,760)
            self.setStyleSheet("""
              QWidget { background:#0b1c31; color:#f3f4f6; font-size:14px; }
              QLabel#title { color:#f8fafc; font-size:30px; font-weight:700; }
              QLabel#route { color:#cbd5e1; font-size:16px; }
              QLabel#status { color:#f1f5f9; font-size:20px; font-weight:600; }
              QLabel#completionTitle { color:#bbf7d0; font-size:24px; font-weight:800; }
              QLabel#completionDetail { color:#e2e8f0; line-height:150%; }
              QLabel#muted { color:#cbd5e1; }
              QLabel#safety { color:#dbeafe; }
              QProgressBar { height:18px; border:0; border-radius:9px; background:#405269; }
              QProgressBar::chunk { border-radius:9px; background:#22d3a6; }
              QFrame { color:#52657b; }
              QPushButton { border-radius:8px; padding:10px 18px; font-weight:700; color:white; }
              QPushButton#resumeButton { background:#0f9f7f; border:1px solid #22d3a6; }
              QPushButton#resumeButton:hover { background:#10b98f; }
              QPushButton#newMigrationButton { background:#0f9f7f; border:1px solid #22d3a6; }
              QPushButton#newMigrationButton:hover { background:#10b98f; }
              QPushButton#pauseButton { background:#b76518; border:1px solid #f59e0b; }
              QPushButton#pauseButton:hover { background:#d1781e; }
              QPushButton:disabled, QPushButton#primaryButton:disabled, QPushButton#secondaryButton:disabled,
              QPushButton#resumeButton:disabled, QPushButton#pauseButton:disabled, QPushButton#newMigrationButton:disabled { color:#8190a5; background:#183047; border:1px solid #334a60; }
              QTreeWidget { background:#0f2741; alternate-background-color:#102c49;
                            border:1px solid #405269; border-radius:8px; padding:6px; }
              QTreeWidget::item { padding:4px; }
              QTreeWidget::item:selected { background:#17496c; }
              QLineEdit { background:#10283f; border:1px solid #405269; border-radius:8px;
                          padding:9px 12px; color:#f8fafc; }
              QLineEdit:focus { border:1px solid #22d3a6; }
            """)

            root_layout=QVBoxLayout(self); root_layout.setContentsMargins(0,0,0,0)
            self.root_stack=QStackedWidget()
            root_layout.addWidget(self.root_stack)

            # Main migration page
            main_page=QWidget()
            lay=QVBoxLayout(main_page); lay.setContentsMargins(34,28,34,28); lay.setSpacing(10)
            title=QLabel("CloudMover"); title.setObjectName("title"); lay.addWidget(title)
            self.route=QLabel("Source   →   Proton Drive"); self.route.setObjectName("route"); lay.addWidget(self.route)

            # 1) Picker first
            picker=QWidget(); pl=QVBoxLayout(picker); pl.setContentsMargins(0,12,0,0)
            pick_head=QHBoxLayout()
            pick_title=QLabel("Choose what to move"); pick_title.setObjectName("status")
            self.pick_summary=QLabel(""); self.pick_summary.setObjectName("muted")
            pick_head.addWidget(pick_title); pick_head.addStretch(); pick_head.addWidget(self.pick_summary)
            pl.addLayout(pick_head)

            tools=QHBoxLayout()
            self.search=QLineEdit()
            self.search.setPlaceholderText("Search files and folders…")
            self.search.textChanged.connect(self.filter_tree)
            self.expand_btn=QPushButton("Expand All")
            self.collapse_btn=QPushButton("Collapse All")
            self.expand_btn.clicked.connect(self.tree_expand_all)
            self.collapse_btn.clicked.connect(self.tree_collapse_all)
            tools.addWidget(self.search,1)
            tools.addWidget(self.expand_btn)
            tools.addWidget(self.collapse_btn)
            pl.addLayout(tools)

            self.pick_help=QLabel("Available while paused. Completed transfers stay in the migration history.")
            self.pick_help.setObjectName("muted"); pl.addWidget(self.pick_help)

            self.tree=QTreeWidget()
            self.tree.setHeaderLabels(["File / Folder","Size","Status"])
            self.tree.setColumnWidth(0,560); self.tree.setColumnWidth(1,110)
            self.tree.itemChanged.connect(self.selection_changed)
            pl.addWidget(self.tree)
            lay.addWidget(picker,1)

            # 2) Migration status second
            sep=QFrame(); sep.setFrameShape(QFrame.HLine); lay.addWidget(sep)
            self.status=QLabel("Migration"); self.status.setObjectName("status"); lay.addWidget(self.status)
            self.bar=QProgressBar(); self.bar.setRange(0,10000); self.bar.setTextVisible(False); lay.addWidget(self.bar)
            self.overall=QLabel(); self.overall.setObjectName("muted"); lay.addWidget(self.overall)

            speed=QLabel("Migration speed depends on your internet connection, especially upload speed, file size, file count, and provider/API overhead. Many small files may take longer than fewer large files of the same total size.")
            speed.setWordWrap(True); speed.setObjectName("muted"); lay.addWidget(speed)
            self.cleanup_notice=QLabel(); self.cleanup_notice.setWordWrap(True); self.cleanup_notice.setStyleSheet("color:#fde68a"); self.cleanup_notice.hide(); lay.addWidget(self.cleanup_notice)
            self.stage=QLabel(); self.stage.setObjectName("status"); lay.addWidget(self.stage)
            self.current=QLabel(); self.current.setWordWrap(True); self.current.setObjectName("muted"); lay.addWidget(self.current)
            self.completion_title=QLabel(); self.completion_title.setObjectName("completionTitle"); self.completion_title.hide(); lay.addWidget(self.completion_title)
            self.completion_detail=QLabel(); self.completion_detail.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Minimum); self.completion_detail.setObjectName("completionDetail"); self.completion_detail.setWordWrap(True); self.completion_detail.hide(); lay.addWidget(self.completion_detail)
            self.filebar=QProgressBar(); self.filebar.setTextVisible(False); self.filebar.hide(); lay.addWidget(self.filebar)
            self.fileprogress=QLabel(); self.fileprogress.setObjectName("muted"); self.fileprogress.hide(); lay.addWidget(self.fileprogress)

            self.attention_title=QLabel("Needs attention"); self.attention_title.setObjectName("status"); lay.addWidget(self.attention_title)
            self.attention=QLabel(); self.attention.setObjectName("muted"); self.attention.setWordWrap(True); lay.addWidget(self.attention)

            safety_title=QLabel("Migration safety"); safety_title.setObjectName("status"); lay.addWidget(safety_title)
            self.error_details=QPushButton("Show error details"); self.error_details.hide(); self.error_details.clicked.connect(self.show_engine_error); lay.addWidget(self.error_details)
            self.safety=QLabel(); self.safety.setObjectName("safety"); self.safety.setWordWrap(True); lay.addWidget(self.safety)

            # 3) Controls last
            controls=QHBoxLayout()
            self.resume_btn=QPushButton("Start Migration"); self.resume_btn.setObjectName("resumeButton")
            self.pause_btn=QPushButton("Paused"); self.pause_btn.setObjectName("pauseButton")
            self.new_btn=QPushButton("New Migration..."); self.new_btn.setObjectName("newMigrationButton")
            self.resume_btn.clicked.connect(self.primary_action)
            self.pause_btn.clicked.connect(self.secondary_action)
            self.new_btn.clicked.connect(self.new_migration)
            controls.addWidget(self.resume_btn); controls.addWidget(self.pause_btn); controls.addWidget(self.new_btn); controls.addStretch()
            lay.addLayout(controls)
            self.notice=QLabel(""); self.notice.setObjectName("muted"); self.notice.setWordWrap(True); lay.addWidget(self.notice)

            self.foot=QLabel("Migration controls  •  refreshes every second"); self.foot.setObjectName("muted"); lay.addWidget(self.foot)
            self.root_stack.addWidget(main_page)

            # Setup page lives inside the same window.
            self.setup_page=SetupDialog(self.db_path)
            self.setup_page.setWindowFlags(Qt.Widget)
            self.setup_page.accepted.connect(self._setup_finished)
            self.root_stack.addWidget(self.setup_page)

            self.timer=QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(1000)
            try:
                recovered=self.repair_stale_runtime_if_needed()
                if any(recovered.values()):
                    self.notice.setText(
                        "Recovered interrupted work • "
                        f"{recovered['HYDRATING']} hydrating • "
                        f"{recovered['UPLOADING']} uploading • "
                        f"{recovered['VERIFYING']} verifying."
                    )
            except Exception as exc:
                self.notice.setText(f"Recovery check failed: {exc}")
            self.load_selection_tree(reconcile_live=False, include_live_directories=False)
            self.refresh()

        def show_engine_error(self):
            message=QMessageBox(self)
            message.setWindowTitle("Migration stopped")
            message.setIcon(QMessageBox.Critical)
            message.setText(friendly_engine_failure(self.engine_failure_details))
            message.setDetailedText(self.engine_failure_details)
            message.exec()

        def pid_path(self): return self.db_path.with_name(self.db_path.stem + ".engine.pid")

        def live_engine_pid(self):
            path=self.pid_path()
            try:
                pid=int(path.read_text().strip())
                os.kill(pid,0)
                return pid
            except (OSError,ValueError):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None

        def repair_stale_runtime_if_needed(self):
            """If no worker exists, make DB/UI state truthfully paused and retry-safe."""
            if self.live_engine_pid():
                return {"HYDRATING":0,"UPLOADING":0,"VERIFYING":0}
            return recover_stale_runtime_state(self.db_path)

        def stop_engine_for_app_exit(self, grace_seconds=4.0):
            """Stop this database's engine before the GUI process exits."""
            pid=self.live_engine_pid()
            if not pid:
                return
            try:
                os.killpg(pid,signal.SIGINT)
            except OSError:
                return

            deadline=time.monotonic()+grace_seconds
            while time.monotonic()<deadline:
                try:
                    os.kill(pid,0)
                except OSError:
                    return
                QApplication.processEvents()
                time.sleep(0.05)

            # Graceful pause did not finish. Durable state recovery makes TERM safe.
            try:
                os.killpg(pid,signal.SIGTERM)
            except OSError:
                return

            deadline=time.monotonic()+2.0
            while time.monotonic()<deadline:
                try:
                    os.kill(pid,0)
                except OSError:
                    return
                QApplication.processEvents()
                time.sleep(0.05)

            # Last resort: never leave an invisible migration engine behind.
            try:
                os.killpg(pid,signal.SIGKILL)
            except OSError:
                pass

        def closeEvent(self,event):
            if self.setup_page.scan_running():
                self.setup_page.request_scan_cancel(); event.ignore(); return
            if self.live_engine_pid():
                self.notice.setText("Closing CloudMover… pausing the migration safely.")
                self.stop_engine_for_app_exit()
            event.accept()

        def primary_action(self):
            try:
                s=snapshot(self.db_path)
                status=str(s["m"]["status"]).upper()
            except Exception:
                status=""
            if status=="COMPLETE":
                self.notice.setText("This migration is complete. Choose New Migration... to create another migration plan.")
            else:
                self.resume_migration()

        def secondary_action(self):
            try:
                s=snapshot(self.db_path)
                status=str(s["m"]["status"]).upper()
            except Exception:
                status=""
            if status=="COMPLETE":
                self.view_report()
            else:
                self.pause_migration()

        def new_migration(self):
            if self.live_engine_pid():
                self.notice.setText("Pause the active migration before starting a new one.")
                return
            try:
                status=str(snapshot(self.db_path)["m"]["status"]).upper()
            except Exception:
                status=""
            if status!="COMPLETE":
                answer=QMessageBox.question(
                    self,
                    "Create New Migration",
                    "The existing paused migration will remain unchanged in history.\n\n"
                    "The new migration will become the active migration.",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer!=QMessageBox.Yes:
                    return
            # Recreate setup state in this same window.
            self.setup_page=SetupDialog(self.db_path)
            self.setup_page.setWindowFlags(Qt.Widget)
            self.setup_page.accepted.connect(self._setup_finished)
            old=self.root_stack.widget(1)
            self.root_stack.removeWidget(old)
            old.deleteLater()
            self.root_stack.insertWidget(1,self.setup_page)
            self.root_stack.setCurrentWidget(self.setup_page)

        def _setup_finished(self):
            self.root_stack.setCurrentIndex(0)
            self.notice.setText("✓ New migration created • choose what to move, then Start Migration.")
            self.load_selection_tree()
            self.refresh()

        def view_report(self):
            try:
                s=snapshot(self.db_path); m=s["m"]
                dialog=CompletionDialog(s,self)
                dialog.exec()
            except Exception as exc:
                QMessageBox.warning(self,"Migration Report",str(exc))

        def set_control_state(self,status,engine_live):
            """Controls reflect the real live engine PID, not stale transition text."""
            running=bool(engine_live)
            if running:
                self.resume_btn.setText("Running")
                self.resume_btn.setEnabled(False)
                self.pause_btn.setText("Pause Migration")
                self.pause_btn.setEnabled(True)
                self.new_btn.setVisible(False)
                self.new_btn.setEnabled(False)
                self.tree.setEnabled(False)
                if self.notice.text().startswith("Resuming"):
                    self.notice.setText("✓ Migration resumed safely • engine is running.")
            elif status=="COMPLETE":
                self.resume_btn.setText("Completed")
                self.resume_btn.setEnabled(False)
                self.pause_btn.setText("View Report")
                self.pause_btn.setEnabled(True)
                self.new_btn.setVisible(True)
                self.new_btn.setEnabled(True)
                self.tree.setEnabled(False)
                if self.notice.text().startswith(("Starting","Resuming","Pausing")):
                    self.notice.setText("Migration complete.")
            else:
                self.resume_btn.setText("Resume Migration" if migration_has_started(self.db_path) else "Start Migration")
                self.resume_btn.setEnabled(True)
                self.pause_btn.setText("Paused")
                self.pause_btn.setEnabled(False)
                self.new_btn.setVisible(True)
                self.new_btn.setEnabled(True)
                self.tree.setEnabled(True)
                if self.notice.text().startswith("Pausing"):
                    self.notice.setText("✓ Paused safely • selection is unlocked.")

        def pause_migration(self):
            pid=self.live_engine_pid()
            if not pid:
                self.notice.setText("Already paused • no running engine process was found.")
                return
            try:
                self.notice.setText("Pausing… stopping the active transfer safely.")
                self.pause_btn.setText("Pausing…")
                self.pause_btn.setEnabled(False); self.resume_btn.setEnabled(False)
                os.killpg(pid,signal.SIGINT)
                QTimer.singleShot(4000,lambda p=pid:self._escalate_pause(p))
            except OSError as exc:
                self.notice.setText(f"Could not request pause: {exc}")

        def _escalate_pause(self,pid):
            if self.live_engine_pid()!=pid: return
            try:
                os.killpg(pid,signal.SIGTERM)
                self.notice.setText("Pause required a forced stop • migration remains recoverable.")
            except OSError:
                pass

        def resume_migration(self):
            if self.live_engine_pid():
                self.notice.setText("CloudMover is already running."); return

            # Order is critical: repair stale transient/RUNNING state while there is
            # no worker, reconcile inventory while PAUSED, and only then launch.
            try:
                recovered=self.repair_stale_runtime_if_needed()
                reconcile_migration_inventory(self.db_path)
            except Exception as exc:
                self.notice.setText(f"Could not prepare migration for start: {exc}")
                return

            first_start=not migration_has_started(self.db_path)
            if first_start:
                self.notice.setText("Starting migration…")
                self.resume_btn.setText("Starting…")
            else:
                recovered_total=sum(recovered.values())
                self.notice.setText(
                    f"Resuming… recovered {recovered_total:,} interrupted file(s); starting worker."
                )
                self.resume_btn.setText("Resuming…")
            self.resume_btn.setEnabled(False); self.pause_btn.setEnabled(False)
            common=[
                "--db",str(self.db_path),
                "--target-mb","250",
                "--batch","5000",
                "--reserve-gb",str(RESERVE_BYTES/1_000_000_000),
            ]
            if getattr(sys,"frozen",False):
                # In CloudMover.app sys.executable is the bundled CloudMover binary,
                # not Python. Re-enter the bundle through its private worker mode.
                cmd=[sys.executable,"--engine-worker",*common]
                cwd=None
            else:
                cmd=[sys.executable,"-m","src.cloudmover.start",*common]
                cwd=str(Path(__file__).resolve().parents[2])
            try:
                subprocess.Popen(cmd,cwd=cwd,start_new_session=True)
            except OSError as exc:
                self.notice.setText(f"Could not resume migration: {exc}")

        def load_selection_tree(self, reconcile_live=False, include_live_directories=False):
            m,rows=selection_rows(self.db_path)
            self._selection_loading=True
            self.tree.clear()
            if not m:
                self._selection_loading=False; return

            status=str(m["status"]).upper()
            if reconcile_live and status not in ("RUNNING","COMPLETE"):
                try:
                    changes=reconcile_migration_inventory(self.db_path)
                    if any(changes.values()):
                        self.notice.setText(
                            f"Source refreshed • {changes['added']} added • "
                            f"{changes['updated']} updated • {changes['removed']} removed."
                        )
                    m,rows=selection_rows(self.db_path)
                except Exception as exc:
                    self.notice.setText(f"Could not refresh iCloud inventory: {exc}")

            directories=source_directories(Path(str(m["source"]))) if include_live_directories else []
            index=build_tree_index(rows,directories)

            root_item=QTreeWidgetItem([source_label(m["source"]),human_bytes(index["bytes"]),""])
            root_item.setData(0,Qt.UserRole,"")
            root_item.setFlags(root_item.flags()|Qt.ItemIsUserCheckable)
            self.apply_check(root_item,index["selected"],index["total"])
            self.tree.addTopLevelItem(root_item)

            def add_node(parent,node,prefix):
                for name,child in sorted(node["dirs"].items(),key=lambda x:x[0].lower()):
                    path=f"{prefix}/{name}".strip("/")
                    folder_status="Folder" if child["total"] else "Empty Folder"
                    item=QTreeWidgetItem([name,human_bytes(child["bytes"]),folder_status])
                    item.setData(0,Qt.UserRole,path)
                    item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
                    self.apply_check(item,child["selected"],child["total"])
                    parent.addChild(item)
                    add_node(item,child,path)
                for name,path,size,selected,state in sorted(node["files"],key=lambda x:x[0].lower()):
                    item=QTreeWidgetItem([name,human_bytes(size),state.title()])
                    item.setData(0,Qt.UserRole,path)
                    item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
                    item.setCheckState(0,Qt.Checked if selected else Qt.Unchecked)
                    if state=="COMPLETE":
                        item.setToolTip(0,"Already completed; remains part of migration history.")
                    parent.addChild(item)

            add_node(root_item,index,"")
            root_item.setExpanded(True)
            self._selection_loading=False

        def apply_check(self,item,selected,total):
            if total and selected==total: item.setCheckState(0,Qt.Checked)
            elif selected==0: item.setCheckState(0,Qt.Unchecked)
            else: item.setCheckState(0,Qt.PartiallyChecked)

        def selection_changed(self,item,column):
            if self._selection_loading or column!=0: return
            path=item.data(0,Qt.UserRole)
            if path is None: return
            state=item.checkState(0)
            if state==Qt.PartiallyChecked: return
            try:
                changed=set_selection_prefix(self.db_path,str(path),state==Qt.Checked)
                self.notice.setText(f"Selection updated • {changed:,} unfinished file(s) changed.")
                self.load_selection_tree()
                self.refresh()
            except Exception as exc:
                self.notice.setText(str(exc))
                self.load_selection_tree()

        def tree_expand_all(self):
            self.tree.expandAll()

        def tree_collapse_all(self):
            self.tree.collapseAll()
            if self.tree.topLevelItemCount():
                self.tree.topLevelItem(0).setExpanded(True)

        def filter_tree(self, text):
            query=(text or "").strip().lower()

            def visit(item):
                child_match=False
                for i in range(item.childCount()):
                    if visit(item.child(i)):
                        child_match=True
                own_match=(not query) or (query in item.text(0).lower())
                visible=own_match or child_match
                item.setHidden(not visible)
                if query and child_match:
                    item.setExpanded(True)
                return visible

            for i in range(self.tree.topLevelItemCount()):
                visit(self.tree.topLevelItem(i))

        def completion_summary_text(self,s,m):
            destination=str(m["destination"] or "Not set")
            completed_bytes=int(s["done"]["b"])
            released_count=int(s["released"]["n"])
            released_bytes=int(s["released"]["b"])
            released_line=(
                f"Local iCloud cache released: {released_count:,} file(s), {human_bytes(released_bytes)}"
                if released_count else
                "Local iCloud cache released: none recorded"
            )
            return (
                f"Source path: {m['source']}\n"
                f"Proton destination: {destination}\n"
                f"Total selected files: {s['selected_files']:,}\n"
                f"Completed files: {int(s['done']['n']):,}\n"
                f"Verified files: {s['verified_files']:,}\n"
                f"Failed files: {s['failed_files']:,}\n"
                f"Total logical bytes migrated: {human_bytes(completed_bytes)}\n"
                f"{released_line}\n"
                f"{released_count:,} / {s['verified_files']:,} local copies released • {int(s.get('cleanup_warnings',0)):,} local cleanup warnings\n"
                f"Migration start time: {m['created_at']}\n"
                f"Completion time: {m['updated_at']}\n"
                f"Elapsed duration: {elapsed_between(m['created_at'],m['updated_at'])}\n\n"
                "Source files remain in iCloud. Verified Proton copies were required before local cache release."
            )

        def refresh(self):
            try:
                s=snapshot(self.db_path); m=s["m"]; d=s["done"]; a=s["active"]
                destination=str(m["destination"] or "")
                self.route.setText(f"{source_label(m['source'])}   →   Proton Drive   ({friendly_destination(destination) if destination else 'destination not set'})")
                total=s["selected_bytes"]; done=int(d["b"])
                pct=(done/total*100 if total else 0.0)
                self.bar.setValue(round(max(0,min(100,pct))*100))
                self.status.setText(f"Overall migration progress   {pct:.2f}%")
                self.overall.setText(
                    f'{s["verified_files"]:,} / {s["selected_files"]:,} files verified   •   '
                    f'{human_bytes(done)} / {human_bytes(total)} completed')
                self.pick_summary.setText(
                    f'{s["selected_files"]:,} files selected • {human_bytes(s["selected_bytes"])}')
                failed_files=int(s.get("failed_files",0))
                failed_rows=s.get("failed_rows") or []
                cleanup_warnings=int(s.get("cleanup_warnings",0))
                self.cleanup_notice.setVisible(cleanup_warnings > 0)
                self.cleanup_notice.setText(f"Local cleanup warning: {cleanup_warnings:,} local {'copy' if cleanup_warnings==1 else 'copies'} could not be released. Their Proton copies remain verified; original files remain in iCloud.")
                self.attention_title.setVisible(failed_files > 0)
                self.attention.setVisible(failed_files > 0)
                if failed_files:
                    lines=[f"{failed_files:,} failed file(s). Source iCloud files remain safe and are not deleted."]
                    for row in failed_rows:
                        err=str(row["last_error"] or "No error message recorded.")
                        lines.append(f"- {row['relative_path']}: {err}")
                    if failed_files > len(failed_rows):
                        lines.append(f"- {failed_files-len(failed_rows):,} more failed file(s) not shown.")
                    lines.append("Fix the underlying issue, then start or resume CloudMover to retry safe pending work.")
                    self.attention.setText("\n".join(lines))
                else:
                    self.attention.setText("No failed files. If an item fails later, CloudMover will keep the source iCloud file safe and show the latest error here.")

                db_status=str(m["status"]).upper()
                live=self.live_engine_pid() is not None

                # Runtime truth wins over stale database text. A migration cannot be
                # visibly RUNNING when there is no worker process.
                status="RUNNING" if live else db_status
                if not live and db_status=="RUNNING":
                    recovered=self.repair_stale_runtime_if_needed()
                    s=snapshot(self.db_path); m=s["m"]; d=s["done"]; a=s["active"]
                    status=str(m["status"]).upper()
                    if any(recovered.values()) and not self.notice.text().startswith("Recovered"):
                        self.notice.setText(
                            f"Recovered {sum(recovered.values()):,} interrupted file(s) • ready to resume."
                        )

                self.set_control_state(status,live)
                if status=="COMPLETE":
                    self.pick_help.setText("Completed migration history. Choose New Migration to create another migration plan.")
                elif status=="RUNNING":
                    self.pick_help.setText("Selection is locked while CloudMover is running.")
                else:
                    self.pick_help.setText("Available while paused. Completed transfers stay in the migration history.")

                if status!="RUNNING":
                    self.filebar.hide(); self.fileprogress.hide()
                    if status=="COMPLETE":
                        self.completion_title.show()
                        self.completion_detail.show()
                        self.status.setText("Complete")
                        self.bar.setValue(10000 if total else 0)
                        self.stage.setText("Migration finished")
                        if failed_files:
                            self.completion_title.setText("Completed with attention needed")
                            completion_notice=f"Migration completed with {failed_files:,} failed file(s)."
                        else:
                            self.completion_title.setText("Migration completed successfully")
                            completion_notice="Migration completed successfully. No further action is required."
                        if cleanup_warnings and not failed_files:
                            self.status.setText("Migration complete — some local space could not be released")
                            self.completion_title.setText("Migration completed with local cleanup warnings")
                            completion_notice=f"All transfers completed. {cleanup_warnings:,} local {'copy' if cleanup_warnings==1 else 'copies'} could not be released."
                        self.completion_detail.setText(self.completion_summary_text(s,m))
                        self.current.setText(
                            f"{s['selected_files']:,} selected files • "
                            f"{human_bytes(s['selected_bytes'])} • "
                            f"{s['verified_files']:,} verified • {s['failed_files']:,} failed"
                        )
                        if not self.notice.text() or self.notice.text().startswith(("Starting","Resuming","Pausing")):
                            self.notice.setText(completion_notice)
                    else:
                        self.completion_title.hide()
                        self.completion_detail.hide()
                        self.stage.setText("Paused / ready")
                        queued=int(s.get("active_count",0))
                        self.current.setText(
                            "Choose what to move, then start the migration."
                            if not queued else
                            f"{queued:,} interrupted file(s) are queued for safe retry when you resume."
                        )
                        if self.notice.text().startswith("Pausing"):
                            self.notice.setText("✓ Paused safely • selection is unlocked.")
                elif a:
                    self.completion_title.hide()
                    self.completion_detail.hide()
                    state=str(a["transfer_state"])
                    self.stage.setText(active_stage_text(s))
                    self.current.setText(
                        f'{a["relative_path"]}   •   {human_bytes(a["logical_bytes"])}   •   '
                        f'active batch: {s["active_count"]:,} file(s) • {human_bytes(s["active_bytes"])}')
                    if self.notice.text().startswith("Resuming"):
                        self.notice.setText("✓ Migration resumed safely • recovery check complete.")
                    self.filebar.show(); self.fileprogress.show()
                    if state=="HYDRATING":
                        fs=inspect_file(a["source_path"]); logical=max(0,int(a["logical_bytes"]))
                        allocated=max(0,int(fs.allocated_bytes))
                        if logical>0 and 0<allocated<logical and fs.hydration is not HydrationState.LOCAL:
                            p=min(1.0,allocated/logical); self.filebar.setRange(0,1000)
                            self.filebar.setValue(round(p*1000))
                            self.fileprogress.setText(
                                f"Current file — downloading from iCloud locally for transfer: {human_bytes(allocated)} of {human_bytes(logical)} • {p*100:.1f}%")
                        else:
                            self.filebar.setRange(0,0)
                            self.fileprogress.setText(
                                "Current file is local • preparing it for transfer…" if fs.hydration is HydrationState.LOCAL
                                else "Current file — downloading from iCloud locally in preparation for transfer…")
                    else:
                        activity=s.get("activity") or {}
                        completed=int(activity.get("completed",0))
                        total_units=int(activity.get("total",0))
                        detail=str(activity.get("detail",""))
                        a_stage=str(activity.get("stage",""))
                        expected_stage={"UPLOADING":"upload","VERIFYING":"verify"}.get(state)
                        if state=="UPLOADING" and a_stage=="upload" and activity.get("phase"):
                            self.current.setText(f"{activity['relative_path']} • {human_bytes(int(activity.get('logical_bytes',0)))}" if activity.get("file_index") else detail)
                            self.filebar.setRange(0,0)
                            self.fileprogress.setText(f"Current upload: {current_upload_text(activity)} {completed:,} of {total_units:,} safe uploads finished in this batch. No byte percentage available.")
                        elif total_units>0 and a_stage==expected_stage:
                            progress=max(0.0,min(1.0,completed/total_units))
                            self.filebar.setRange(0,1000)
                            self.filebar.setValue(round(progress*1000))
                            label="Current batch — verified folder groups" if a_stage=="verify" else "Current batch — upload operations"
                            self.fileprogress.setText(
                                f"{label}: {completed:,} of {total_units:,} • {progress*100:.1f}%"
                                + (f" • {detail}" if detail else "")
                            )
                        else:
                            self.filebar.setRange(0,0)
                            self.fileprogress.setText(
                                "Current batch — uploading to Proton… progress unavailable from Proton"
                                if state=="UPLOADING" else "Working…"
                            )
                else:
                    self.completion_title.hide()
                    self.completion_detail.hide()
                    self.stage.setText(active_stage_text(s))
                    self.current.setText("CloudMover is finishing the remaining migration steps." if int(d["n"]) >= s["selected_files"] else "CloudMover is running; preparing the next batch.")
                    self.filebar.show(); self.filebar.setRange(0,0); self.fileprogress.show()
                    self.fileprogress.setText("Working…")

                failure=self.engine_events.failure(m.get("id") if isinstance(m,dict) else m["id"])
                stopped=bool(failure and not live and status!="COMPLETE")
                self.error_details.setVisible(stopped)
                if stopped:
                    self.engine_failure_details=failure
                    self.status.setText("Migration stopped — needs attention")
                    self.stage.setText("Stopped due to an error")
                    self.current.setText(friendly_engine_failure(failure))
                    self.filebar.hide(); self.fileprogress.hide()
                    self.attention_title.show(); self.attention.show()
                    self.attention.setText(friendly_engine_failure(failure))
                    self.attention.setStyleSheet("color:#fda4af")
                else:
                    self.engine_failure_details=""
                    self.attention.setStyleSheet("")

                # Stage comes from each fresh database snapshot, never a click message.
                # Keep pause requests and actionable errors visible in the notice area.
                if self.notice.text().startswith(("Starting", "Resuming", "✓ Migration resumed")):
                    self.notice.clear()

                fs=os.statvfs(m["source"]); free=fs.f_bavail*fs.f_frsize
                self.safety.setText(
                    "✓ Verified Proton copy required before local release\n"
                    "✓ Originals remain in iCloud\n"
                    f"✓ {human_bytes(free)} free • {human_bytes(m['reserve_bytes'])} kept free\n"
                    f"✓ {s['released']['n']:,} selected verified local copies released")
                if status=="COMPLETE":
                    selection_state="completed migration history"
                elif status=="RUNNING":
                    selection_state="locked while running"
                else:
                    selection_state="editable"
                self.foot.setText(
                    f"Engine: {str(m['status']).title()}  •  Selection {selection_state}  •  refreshes every second")
            except Exception as exc:
                self.stage.setText("Unable to read migration"); self.current.setText(str(exc))

    ap=argparse.ArgumentParser(description="CloudMover migration application")
    ap.add_argument("--db",default=None)
    args=ap.parse_args()
    app=QApplication([])
    ensure_app_support()
    db_path=Path(args.db).expanduser() if args.db else default_db_path()
    w=Window(db_path)
    if not has_migration(db_path):
        w.root_stack.setCurrentWidget(w.setup_page)
    else:
        w.root_stack.setCurrentIndex(0)
    w.show()
    try:
        rc=app.exec()
    except KeyboardInterrupt:
        w.stop_engine_for_app_exit()
        raise SystemExit(130)
    raise SystemExit(rc)

if __name__=="__main__": main()
