
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from ..diagnostics import safe_diagnostic
import hashlib, json, shutil, subprocess, os, signal, tempfile, uuid, logging
from dataclasses import dataclass
from pathlib import Path

log=logging.getLogger(__name__)

@dataclass(frozen=True)
class RemoteFile:
    name: str
    claimed_size: int | None
    sha1: str | None
    node_id: str | None = None
    parent_uid: str | None = None


class ProtonCLIError(RuntimeError):
    def __init__(self, cmd, stdout, stderr, returncode):
        self.argv = list(cmd)
        self.returncode = returncode
        stderr = safe_diagnostic(stderr)
        stdout = safe_diagnostic(stdout)
        self.stderr = stderr
        self.stdout = stdout
        details = []
        if stderr.strip(): details.append(f"stderr: {stderr.strip()}")
        if stdout.strip(): details.append(f"stdout: {stdout.strip()}")
        detail = " | ".join(details) if details else "no output"
        super().__init__(f"Proton CLI failed (exit {returncode}) running: {json.dumps(cmd[1:], ensure_ascii=False)} | {detail}")


class TreeVerificationUnavailable(RuntimeError):
    """Tree listings are unavailable or incomplete; exact-parent verification may retry."""

def cli() -> str:
    exe = shutil.which("proton-drive")
    if not exe:
        raise RuntimeError("proton-drive CLI not found in PATH")
    return exe



def _terminate_child(proc: subprocess.Popen, first_signal=signal.SIGINT) -> None:
    """Terminate one Proton child.

    Proton inherits the worker's process group so an external CloudMover pause
    reaches worker + Proton together. Internal timeouts still need direct cleanup.
    """
    try:
        proc.send_signal(first_signal)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _run_cli_process(cmd: list[str], *, timeout: float | None=120.0) -> tuple[str,str,int]:
    proc=subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if timeout is None:
            stdout,stderr=proc.communicate()
        else:
            stdout,stderr=proc.communicate(timeout=timeout)
    except KeyboardInterrupt:
        _terminate_child(proc,signal.SIGINT)
        raise
    except subprocess.TimeoutExpired:
        _terminate_child(proc,signal.SIGTERM)
        raise TimeoutError(
            f"Proton CLI timed out after {timeout:g}s running: {json.dumps(cmd[1:], ensure_ascii=False)}"
        )
    return stdout or "",stderr or "",int(proc.returncode or 0)

def run(args: list[str], *, json_output: bool=False, timeout: float | None=120.0) -> str:
    cmd=[cli()] + list(args)
    if json_output:
        if len(cmd) >= 3 and cmd[1] == "filesystem" and cmd[2] == "list":
            cmd.insert(3,"-j")
        else:
            cmd.append("-j")
    stdout,stderr,returncode=_observed_cli_process(cmd,timeout=timeout)
    if returncode != 0:
        raise ProtonCLIError(cmd, stdout, stderr, returncode)
    return stdout

def ensure_folder(parent: str, name: str) -> str:
    """Ensure ancestors before the leaf; create-folder takes parentPath, then name.

    Names are argv elements, never shell fragments. Preserve whitespace and Unicode.
    """
    parent = parent.rstrip("/") or "/"
    target = parent.rstrip("/") + "/" + name
    try:
        run(["filesystem", "list", target], json_output=True)
        return target
    except ProtonCLIError as exc:
        # Authentication, network and ambiguous-node errors must not trigger creates.
        if not exc.stderr.strip().startswith("Node not found:"):
            raise
    if parent != "/" and parent.count("/") > 1:
        ancestor, parent_name = parent.rsplit("/", 1)
        ensure_folder(ancestor or "/", parent_name)
    run(["filesystem", "create-folder", parent, name])
    run(["filesystem", "list", target], json_output=True)
    return target


def _run_interruptible_upload(args: list[str]) -> str:
    cmd=[cli()] + list(args)
    stdout,stderr,returncode=_observed_cli_process(cmd,timeout=None)
    if returncode != 0:
        raise ProtonCLIError(cmd, stdout, stderr, returncode)
    return stdout


def upload(local: Path, remote_parent: str) -> None:
    safe_upload_promote(local, remote_parent, local.name)

def upload_many(local_paths: list[Path], remote_parent: str) -> None:
    """Upload multiple files safely; directory bulk upload is no longer supported here."""
    if not local_paths:
        return
    for local in local_paths:
        if Path(local).is_dir():
            raise RuntimeError(
                "upload_many no longer supports directory inputs; use the bulk pipeline for tree uploads"
            )
        safe_upload_promote(local, remote_parent, local.name)

def sha1_file(path: Path) -> str:
    h=hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def _unwrap(v):
    if isinstance(v,dict) and v.get("ok") is True and "value" in v:
        return v["value"]
    return v

def _first_present(data: dict, keys: tuple[str, ...]):
    for key in keys:
        value=_unwrap(data.get(key))
        if value is not None:
            return value
    return None

def _remote_file_from_item(item) -> RemoteFile | None:
    item=_unwrap(item)
    if not isinstance(item,dict):
        return None
    name=_first_present(item,("name","Name"))
    rev=_unwrap(_first_present(item,("activeRevision","ActiveRevision","revision","Revision")))
    size=sha1=None
    if isinstance(rev,dict):
        size=_first_present(rev,("claimedSize","size","Size"))
        dig=_unwrap(_first_present(rev,("claimedDigests","digests","Digests")))
        if isinstance(dig,dict):
            sha1=_first_present(dig,("sha1","SHA1"))
    if size is None:
        size=_first_present(item,("claimedSize","size","Size"))
    dig=_unwrap(_first_present(item,("claimedDigests","digests","Digests")))
    if sha1 is None and isinstance(dig,dict):
        sha1=_first_present(dig,("sha1","SHA1"))
    node_id=_first_present(item,("uid",))
    parent_uid=_first_present(item,("parentUid",))
    if not name:
        return None
    return RemoteFile(
        str(name),
        int(size) if size is not None else None,
        str(sha1) if sha1 else None,
        str(node_id) if node_id else None,
        str(parent_uid) if parent_uid else None,
    )

VERIFY_LIST_TIMEOUT = 600.0

def _run_verification_list(args: list[str]) -> str:
    """Large Proton folders may take minutes to enumerate; retry once."""
    try:
        return run(args, json_output=True, timeout=VERIFY_LIST_TIMEOUT)
    except TimeoutError:
        return run(args, json_output=True, timeout=VERIFY_LIST_TIMEOUT)

def list_files(path: str) -> list[RemoteFile]:
    raw=_run_verification_list(["filesystem","list","-t","file",path])
    data=json.loads(raw)
    if isinstance(data,dict):
        data=_unwrap(data)
        if isinstance(data,dict):
            for key in ("items","nodes","entries","value"):
                if key in data:
                    data=_unwrap(data[key]); break
    if not isinstance(data,list):
        raise RuntimeError("unexpected Proton JSON listing shape")
    out=[]
    for item in data:
        remote=_remote_file_from_item(item)
        if remote:
            out.append(remote)
    return out

def info_file(path: str) -> RemoteFile:
    raw=run(["filesystem","info",path],json_output=True)
    data=_unwrap(json.loads(raw))
    remote=_remote_file_from_item(data)
    if not remote:
        raise RuntimeError("unexpected Proton JSON info shape")
    return remote

def verify(local: Path, remote_parent: str) -> RemoteFile:
    expected_size=local.stat().st_size
    expected_sha1=sha1_file(local)
    matches=[r for r in list_files(remote_parent) if r.name == local.name]
    if not matches:
        raise RuntimeError("uploaded filename not found in Proton destination")
    r=matches[0]
    if r.claimed_size is not None and r.claimed_size != expected_size:
        raise RuntimeError(f"remote claimed size mismatch: {r.claimed_size} != {expected_size}")
    if not r.sha1:
        raise RuntimeError("remote SHA-1 digest unavailable; cannot prove uploaded content")
    if r.sha1.lower() != expected_sha1.lower():
        raise RuntimeError("remote SHA-1 digest mismatch")
    return r


def assert_no_remote_file(remote_file: str) -> None:
    """Refuse to upload where a remote file already exists."""
    parent,name=remote_file.rstrip("/").rsplit("/",1)
    parent=parent or "/"
    matches=[r for r in list_files(parent) if r.name == name]
    if matches:
        raise RuntimeError(
            f"remote destination already contains this path; refusing to treat pre-existing content as migrated: {remote_file}"
        )

def verify_remote_exact(local: Path, remote_path: str, expected_name: str | None=None) -> RemoteFile:
    remote=info_file(remote_path)
    if expected_name is not None and remote.name != expected_name:
        raise RuntimeError(f"remote filename mismatch: {remote.name} != {expected_name}")
    expected_size=local.stat().st_size
    expected_sha1=sha1_file(local)
    if remote.claimed_size is not None and remote.claimed_size != expected_size:
        raise RuntimeError(f"remote claimed size mismatch: {remote.claimed_size} != {expected_size}")
    if not remote.sha1:
        raise RuntimeError("remote SHA-1 digest unavailable; cannot prove uploaded content")
    if remote.sha1.lower() != expected_sha1.lower():
        raise RuntimeError("remote SHA-1 digest mismatch")
    if not remote.node_id:
        raise RuntimeError("remote node identity unavailable; cannot prove uploaded object")
    return remote

def _uid_path(reference_path: str, node_uid: str) -> str:
    section=reference_path.strip("/").split("/",1)[0]
    if not section:
        raise RuntimeError("cannot address remote node UID without a root section")
    return f"/{section}/{node_uid}"

def _verified_temp_by_uid(local: Path, remote_parent: str, temp: RemoteFile) -> str:
    if not temp.node_id or not temp.parent_uid:
        raise RuntimeError("temp node identity or parent unavailable; refusing cleanup")
    remote=info_file(_uid_path(remote_parent,temp.node_id))
    if remote.node_id != temp.node_id:
        raise RuntimeError("temp cleanup UID lookup returned a different node")
    if remote.parent_uid != temp.parent_uid:
        raise RuntimeError("temp cleanup parent mismatch")
    if remote.name != temp.name:
        raise RuntimeError("temp cleanup name mismatch")
    expected_size=local.stat().st_size
    expected_sha1=sha1_file(local)
    if remote.claimed_size is not None and remote.claimed_size != expected_size:
        raise RuntimeError("temp cleanup size mismatch")
    if not remote.sha1 or remote.sha1.lower() != expected_sha1.lower():
        raise RuntimeError("temp cleanup digest mismatch")
    return _uid_path(remote_parent,temp.node_id)

def _cleanup_temp_remote(local: Path, remote_parent: str, temp: RemoteFile) -> None:
    try:
        run(["filesystem","trash",_verified_temp_by_uid(local,remote_parent,temp)],timeout=120.0)
    except Exception as exc:
        log.warning("failed to cleanup Proton temp upload %s: %s",temp.node_id or temp.name,exc)

_upload_observer=ContextVar("upload_observer", default=None)
_cli_observer=ContextVar("cli_observer", default=None)


@contextmanager
def observe_cli(callback):
    """Record process timing without credentials, argv, or command output."""
    token=_cli_observer.set(callback)
    try:
        yield
    finally:
        _cli_observer.reset(token)


def _observed_cli_process(cmd, *, timeout):
    if _cli_observer.get() is None:
        return _run_cli_process(cmd,timeout=timeout)
    began=perf_counter()
    status=None
    error=None
    try:
        result=_run_cli_process(cmd,timeout=timeout)
        status=result[2]
        return result
    except BaseException as exc:
        error=type(exc).__name__
        raise
    finally:
        callback=_cli_observer.get()
        if callback:
            callback("cli_process",command=" ".join(cmd[1:3]),
                     duration_ms=round((perf_counter()-began)*1000),exit_status=status,error_type=error)


@contextmanager
def observe_safe_upload(callback):
    token=_upload_observer.set(callback)
    try:
        yield
    finally:
        _upload_observer.reset(token)


@contextmanager
def upload_timing(phase):
    callback=_upload_observer.get()
    began=perf_counter()
    if callback:
        callback("safe_upload_stage",phase=phase,status="started",duration_ms=0)
    try:
        yield
    except BaseException as exc:
        if callback:
            callback("safe_upload_stage",phase=phase,status="error",duration_ms=round((perf_counter()-began)*1000),exit_status=getattr(exc,"returncode",None))
        raise
    else:
        if callback:
            callback("safe_upload_stage",phase=phase,status="completed",duration_ms=round((perf_counter()-began)*1000),exit_status=0)


def safe_upload_promote(local: Path, remote_parent: str, final_name: str) -> RemoteFile:
    with upload_timing("safe_upload_total"):
        return _safe_upload_promote(local,remote_parent,final_name)


def _safe_upload_promote(local: Path, remote_parent: str, final_name: str) -> RemoteFile:
    """Upload via a unique temp name, then promote only the uploaded node to final_name."""
    local=Path(local)
    remote_parent=remote_parent.rstrip("/") or "/"
    final_path=remote_parent.rstrip("/")+"/"+final_name
    temp_name=f".cloudmover-upload-{uuid.uuid4().hex}.tmp"
    temp_path=remote_parent.rstrip("/")+"/"+temp_name

    with upload_timing("destination_preflight"):
        assert_no_remote_file(final_path)
        assert_no_remote_file(temp_path)
    with tempfile.TemporaryDirectory(prefix="cloudmover-upload-") as td:
        temp_local=Path(td)/temp_name
        try:
            os.link(local,temp_local)
        except OSError:
            shutil.copy2(local,temp_local)
        with upload_timing("cli_upload"):
            _run_interruptible_upload(["filesystem","upload","-f","rename","-t",str(temp_local),remote_parent])

    with upload_timing("temp_verification"):
        temp_remote=verify_remote_exact(local,temp_path,temp_name)
    try:
        # Proton CLI 0.8.0 exposes no atomic no-clobber rename; the dedicated
        # migration folder plus this immediate preflight minimizes the residual race.
        with upload_timing("rename_promote"):
            assert_no_remote_file(final_path)
            run(["filesystem","rename",temp_path,final_name],timeout=120.0)
        with upload_timing("final_verification"):
            final_remote=verify_remote_exact(local,final_path,final_name)
        if final_remote.node_id != temp_remote.node_id:
            raise RuntimeError("remote node identity changed during promotion")
        return final_remote
    except Exception:
        _cleanup_temp_remote(local,remote_parent,temp_remote)
        raise


TRANSPORT_MAX_FILES=32
TRANSPORT_ARGV_BYTES=32_000


def verify_group_snapshot(files, remote_parent, *, expected=None, on_verified=None):
    """Prove every exact child from a fresh listing, never from cached absence.

    An incomplete metadata entry may use an exact info fallback. Missing or
    ambiguous names, contradictory metadata, and changed UIDs fail closed.
    """
    children=list_files(remote_parent)
    verified=[]
    seen=set()
    for index,(local,name) in enumerate(files):
        path=remote_parent.rstrip("/")+"/"+name
        matches=[r for r in children if r.name==name]
        if len(matches)!=1:
            raise RuntimeError(f"Expected exactly one Proton file at {path}; found {len(matches)}")
        remote=matches[0]
        size=Path(local).stat().st_size
        digest=sha1_file(Path(local))
        if remote.claimed_size is not None and remote.claimed_size!=size:
            raise RuntimeError(f"remote claimed size mismatch: {path}")
        if remote.sha1 and remote.sha1.lower()!=digest.lower():
            raise RuntimeError(f"remote SHA-1 digest mismatch: {path}")
        before=expected[index] if expected is not None else None
        if before and remote.node_id and remote.node_id!=before.node_id:
            raise RuntimeError(f"remote node identity changed during promotion: {path}")
        if not remote.node_id or not remote.sha1 or remote.claimed_size is None:
            listed=remote
            try:
                remote=verify_remote_exact(Path(local),path,name)
            except Exception as exc:
                raise RuntimeError(f"Could not verify {path}: {exc}") from exc
            if listed.node_id and remote.node_id!=listed.node_id:
                raise RuntimeError(f"remote identity changed since listing: {path}")
        if remote.claimed_size is None:
            raise RuntimeError(f"remote size unavailable; cannot prove {path}")
        if remote.node_id in seen:
            raise RuntimeError(f"Different upload files returned the same remote UID: {path}")
        if before and (remote.node_id!=before.node_id or
                       (before.parent_uid and remote.parent_uid!=before.parent_uid)):
            raise RuntimeError(f"remote node identity or parent changed during promotion: {path}")
        seen.add(remote.node_id)
        verified.append(remote)
        if on_verified:
            on_verified(local,remote)
    return verified


def transport_groups(entries):
    """Group (local, parent, name) entries; no directory uploads or unbounded argv."""
    parents={}
    for local,parent,name in entries:
        parents.setdefault(parent,[]).append((Path(local),name))
    for parent,files in parents.items():
        # Temp local paths are bounded names under the system temporary directory.
        per_file=len(os.fsencode(tempfile.gettempdir()))+128
        available=TRANSPORT_ARGV_BYTES-len(os.fsencode(parent))-1024
        limit=min(TRANSPORT_MAX_FILES,available//per_file)
        if limit < 1:
            raise ValueError("Proton destination is too long for a bounded upload command")
        for offset in range(0,len(files),limit):
            yield parent,files[offset:offset+limit]


def safe_upload_group(files, remote_parent, *, observe_file=None):
    """One same-parent temp transport, independent verification and promotion.

    All temps must prove correct before any promotion. On errors only proven,
    still-temp UIDs may be cleaned up. Interrupts leave isolated temps for recovery;
    they never imply successful upload or completion.
    """
    files=[(Path(local),name) for local,name in files]
    if not files or len(files)>TRANSPORT_MAX_FILES:
        raise ValueError("Upload group must contain 1 to 32 files")
    names=[name for _,name in files]
    if len(set(names)) != len(names) or any(not n or n in (".","..") or "/" in n for n in names):
        raise ValueError("Upload group requires unique basenames")
    if any(not local.is_file() for local,_ in files):
        raise ValueError("Upload group accepts files only")
    if len(files)==1:
        local,name=files[0]
        callback=(lambda event,**data: observe_file(local,event,**data)) if observe_file else _upload_observer.get()
        with observe_safe_upload(callback):
            return [safe_upload_promote(local,remote_parent,name)]
    remote_parent=remote_parent.rstrip("/") or "/"
    records=[(local,name,f".cloudmover-upload-{uuid.uuid4().hex}.tmp") for local,name in files]
    path=lambda name: remote_parent.rstrip("/")+"/"+name
    proven=[]
    results=[]
    promoted=set()
    began=perf_counter()
    @contextmanager
    def file_phase(local,phase):
        callback=(lambda event,**data: observe_file(local,event,**data)) if observe_file else _upload_observer.get()
        with observe_safe_upload(callback),upload_timing(phase):
            yield
    try:
        with upload_timing("destination_preflight"):
            existing={r.name for r in list_files(remote_parent)}
            if existing.intersection(names+[temp for _,_,temp in records]):
                raise RuntimeError("remote destination already contains this path; refusing overwrite")
        with tempfile.TemporaryDirectory(prefix="cloudmover-upload-") as td:
            locals_=[]
            for local,_,temp in records:
                target=Path(td)/temp
                try:
                    os.link(local,target)
                except OSError:
                    from ..disk_space import RESERVE_BYTES
                    if shutil.disk_usage(td).free < RESERVE_BYTES+local.stat().st_size:
                        raise RuntimeError("Not enough free space for temporary upload copies and the 5 GB reserve")
                    shutil.copy2(local,target)
                locals_.append(str(target))
            argv=["filesystem","upload","-f","rename","-t",*locals_,remote_parent]
            if sum(len(os.fsencode(a))+1 for a in argv)>TRANSPORT_ARGV_BYTES:
                raise ValueError("Upload command exceeds transport argv budget")
            with upload_timing("cli_upload"):
                _run_interruptible_upload(argv)
        with upload_timing("temp_verification"):
            temps=verify_group_snapshot([(local,temp) for local,_,temp in records],remote_parent,
                                        on_verified=lambda local,remote:proven.append((local,remote)))
        for (local,name,temp),(_,remote) in zip(records,proven):
            with file_phase(local,"rename_promote"):
                assert_no_remote_file(path(name))  # Never reuse the initial snapshot here.
                run(["filesystem","rename",path(temp),name],timeout=120.0)
                promoted.add(remote.node_id)
        with upload_timing("final_verification"):
            results=verify_group_snapshot(files,remote_parent,expected=temps)
        for local,_ in files:
            if observe_file:
                observe_file(local,"safe_upload_stage",phase="safe_upload_total",status="completed",
                             duration_ms=round((perf_counter()-began)*1000),exit_status=0)
        return results
    except Exception:
        for local,remote in proven:
            if remote.node_id not in promoted:
                _cleanup_temp_remote(local,remote_parent,remote)
        raise


def verify_many_in_parent(local_files: list[Path], remote_parent: str) -> dict[str, RemoteFile]:
    """Verify many files that share one remote parent using one listing call."""
    remotes = {r.name: r for r in list_files(remote_parent)}
    verified: dict[str, RemoteFile] = {}
    for local in local_files:
        r = remotes.get(local.name)
        if r is None:
            raise RuntimeError(f"uploaded filename not found in Proton destination: {local.name}")
        expected_size = local.stat().st_size
        expected_sha1 = sha1_file(local)
        if r.claimed_size is not None and r.claimed_size != expected_size:
            raise RuntimeError(
                f"remote claimed size mismatch for {local.name}: {r.claimed_size} != {expected_size}"
            )
        if not r.sha1:
            raise RuntimeError(f"remote SHA-1 digest unavailable for {local.name}; cannot prove uploaded content")
        if r.sha1.lower() != expected_sha1.lower():
            raise RuntimeError(f"remote SHA-1 digest mismatch for {local.name}")
        verified[local.name] = r
    return verified


def _extract_tree_entries(data, prefix=""):
    """Best-effort flatten of Proton JSON tree/list shapes into path -> RemoteFile."""
    data=_unwrap(data)
    out={}
    if isinstance(data,list):
        for item in data:
            out.update(_extract_tree_entries(item,prefix))
        return out
    if not isinstance(data,dict):
        return out
    name=_unwrap(data.get("name"))
    current=prefix
    if name:
        current=(prefix.rstrip("/")+"/"+str(name)).lstrip("/")
    rev=_unwrap(data.get("activeRevision"))
    if isinstance(rev,dict) and name:
        remote=_remote_file_from_item(data)
        if remote:
            out[current]=remote
    for key in ("items","nodes","entries","children","value"):
        if key in data:
            child=_unwrap(data[key])
            out.update(_extract_tree_entries(child,current))
    return out

def list_tree(path:str)->dict[str,RemoteFile]:
    """List a destination tree once and flatten any descendants returned by Proton."""
    try:
        raw=_run_verification_list(["filesystem","list",path])
        data=json.loads(raw)
    except Exception as exc:
        raise TreeVerificationUnavailable(str(exc)) from exc
    return _extract_tree_entries(data,"")

def verify_tree(local_root:Path, remote_root:str, relative_paths:list[str])->None:
    """Verify exact relative paths from one remote tree snapshot when available."""
    remotes=list_tree(remote_root)
    # Normalize whether Proton includes the queried root's basename in flattened paths.
    root_name=remote_root.rstrip("/").split("/")[-1]
    normalized={}
    for k,v in remotes.items():
        key=k
        if key==root_name:
            key=""
        elif key.startswith(root_name+"/"):
            key=key[len(root_name)+1:]
        normalized[key]=v
    missing=[]
    for rel in relative_paths:
        r=normalized.get(rel)
        if r is None:
            missing.append(rel); continue
        local=local_root/rel
        expected_size=local.stat().st_size
        expected_sha1=sha1_file(local)
        if r.claimed_size is not None and r.claimed_size != expected_size:
            raise RuntimeError(f"remote claimed size mismatch for {rel}: {r.claimed_size} != {expected_size}")
        if not r.sha1:
            raise RuntimeError(f"remote SHA-1 digest unavailable for {rel}; cannot prove uploaded content")
        if r.sha1.lower()!=expected_sha1.lower():
            raise RuntimeError(f"remote SHA-1 digest mismatch for {rel}")
    if missing:
        raise TreeVerificationUnavailable("tree snapshot did not contain exact uploaded paths: "+", ".join(missing[:3]))
