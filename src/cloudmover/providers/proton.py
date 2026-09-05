
from __future__ import annotations
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
            f"Proton CLI timed out after {timeout:g}s running: {' '.join(cmd[1:])}"
        )
    return stdout or "",stderr or "",int(proc.returncode or 0)

def run(args: list[str], *, json_output: bool=False, timeout: float | None=120.0) -> str:
    cmd=[cli()] + list(args)
    if json_output:
        if len(cmd) >= 3 and cmd[1] == "filesystem" and cmd[2] == "list":
            cmd.insert(3,"-j")
        else:
            cmd.append("-j")
    stdout,stderr,returncode=_run_cli_process(cmd,timeout=timeout)
    if returncode != 0:
        details=[]
        if stderr.strip(): details.append(f"stderr: {stderr.strip()}")
        if stdout.strip(): details.append(f"stdout: {stdout.strip()}")
        detail=" | ".join(details) if details else "no output"
        raise RuntimeError(
            f"Proton CLI failed (exit {returncode}) running: {' '.join(cmd[1:])} | {detail}"
        )
    return stdout

def ensure_folder(parent: str, name: str) -> str:
    target=parent.rstrip("/")+"/"+name
    try:
        run(["filesystem","list",target], json_output=True)
        return target
    except Exception:
        run(["filesystem","create-folder",parent,name])
        run(["filesystem","list",target], json_output=True)
        return target


def _run_interruptible_upload(args: list[str]) -> str:
    cmd=[cli()] + list(args)
    stdout,stderr,returncode=_run_cli_process(cmd,timeout=None)
    if returncode != 0:
        details=[]
        if stderr.strip(): details.append(f"stderr: {stderr.strip()}")
        if stdout.strip(): details.append(f"stdout: {stdout.strip()}")
        detail=" | ".join(details) if details else "no output"
        raise RuntimeError(
            f"Proton CLI failed (exit {returncode}) running: {' '.join(cmd[1:])} | {detail}"
        )
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

def safe_upload_promote(local: Path, remote_parent: str, final_name: str) -> RemoteFile:
    """Upload via a unique temp name, then promote only the uploaded node to final_name."""
    local=Path(local)
    remote_parent=remote_parent.rstrip("/") or "/"
    final_path=remote_parent.rstrip()+"/"+final_name
    temp_name=f".cloudmover-upload-{uuid.uuid4().hex}.tmp"
    temp_path=remote_parent.rstrip()+"/"+temp_name

    assert_no_remote_file(final_path)
    assert_no_remote_file(temp_path)
    with tempfile.TemporaryDirectory(prefix="cloudmover-upload-") as td:
        temp_local=Path(td)/temp_name
        try:
            os.link(local,temp_local)
        except OSError:
            shutil.copy2(local,temp_local)
        _run_interruptible_upload(["filesystem","upload","-f","rename","-t",str(temp_local),remote_parent])

    temp_remote=verify_remote_exact(local,temp_path,temp_name)
    try:
        # Proton CLI 0.8.0 exposes no atomic no-clobber rename; the dedicated
        # migration folder plus this immediate preflight minimizes the residual race.
        assert_no_remote_file(final_path)
        run(["filesystem","rename",temp_path,final_name],timeout=120.0)
        final_remote=verify_remote_exact(local,final_path,final_name)
        if final_remote.node_id != temp_remote.node_id:
            raise RuntimeError("remote node identity changed during promotion")
        return final_remote
    except Exception:
        _cleanup_temp_remote(local,remote_parent,temp_remote)
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
