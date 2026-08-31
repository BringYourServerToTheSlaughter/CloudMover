
from __future__ import annotations
import hashlib, json, shutil, subprocess, os, signal
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class RemoteFile:
    name: str
    claimed_size: int | None
    sha1: str | None

def cli() -> str:
    exe = shutil.which("proton-drive")
    if not exe:
        raise RuntimeError("proton-drive CLI not found in PATH")
    return exe

def run(args: list[str], *, json_output: bool=False) -> str:
    cmd=[cli()] + list(args)
    if json_output:
        # Proton Drive CLI 0.8.x exposes JSON as a command-level option.
        # For filesystem listing, `-j` must appear after `list`.
        if len(cmd) >= 3 and cmd[1] == "filesystem" and cmd[2] == "list":
            cmd.insert(3, "-j")
        else:
            cmd.append("-j")
    p=subprocess.run(cmd,text=True,capture_output=True)
    if p.returncode != 0:
        stderr=(p.stderr or "").strip()
        stdout=(p.stdout or "").strip()
        details=[]
        if stderr:
            details.append(f"stderr: {stderr}")
        if stdout:
            details.append(f"stdout: {stdout}")
        detail=" | ".join(details) if details else "no output"
        raise RuntimeError(
            f"Proton CLI failed (exit {p.returncode}) running: {' '.join(cmd[1:])} | {detail}"
        )
    return p.stdout

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
    """Run a long Proton upload so CloudMover can stop the whole child process group."""
    cmd=[cli()] + list(args)
    proc=subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout,stderr=proc.communicate()
    except KeyboardInterrupt:
        # Proton CLI can otherwise keep running after CloudMover receives Ctrl+C.
        try:
            os.killpg(proc.pid,signal.SIGINT)
        except (ProcessLookupError,PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid,signal.SIGTERM)
            except (ProcessLookupError,PermissionError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid,signal.SIGKILL)
                except (ProcessLookupError,PermissionError):
                    pass
        raise

    if proc.returncode != 0:
        stderr=(stderr or "").strip()
        stdout=(stdout or "").strip()
        details=[]
        if stderr: details.append(f"stderr: {stderr}")
        if stdout: details.append(f"stdout: {stdout}")
        detail=" | ".join(details) if details else "no output"
        raise RuntimeError(
            f"Proton CLI failed (exit {proc.returncode}) running: {' '.join(cmd[1:])} | {detail}"
        )
    return stdout

def upload(local: Path, remote_parent: str) -> None:
    _run_interruptible_upload(
        ["filesystem","upload","-f","skip","-t",str(local),remote_parent]
    )

def upload_many(local_paths: list[Path], remote_parent: str) -> None:
    """Upload multiple files/folders in one Proton CLI process."""
    if not local_paths:
        return
    _run_interruptible_upload([
        "filesystem", "upload",
        "-f", "skip",
        "-d", "merge",
        "-t",
        *[str(p) for p in local_paths],
        remote_parent,
    ])

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

def list_files(path: str) -> list[RemoteFile]:
    raw=run(["filesystem","list","-t","file",path],json_output=True)
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
        item=_unwrap(item)
        name=_unwrap(item.get("name")) if isinstance(item,dict) else None
        rev=_unwrap(item.get("activeRevision")) if isinstance(item,dict) else None
        size=sha1=None
        if isinstance(rev,dict):
            size=_unwrap(rev.get("claimedSize"))
            dig=_unwrap(rev.get("claimedDigests"))
            if isinstance(dig,dict):
                sha1=_unwrap(dig.get("sha1"))
        if name:
            out.append(RemoteFile(str(name), int(size) if size is not None else None, str(sha1) if sha1 else None))
    return out

def verify(local: Path, remote_parent: str) -> RemoteFile:
    expected_size=local.stat().st_size
    expected_sha1=sha1_file(local)
    matches=[r for r in list_files(remote_parent) if r.name == local.name]
    if not matches:
        raise RuntimeError("uploaded filename not found in Proton destination")
    r=matches[0]
    if r.claimed_size is not None and r.claimed_size != expected_size:
        raise RuntimeError(f"remote claimed size mismatch: {r.claimed_size} != {expected_size}")
    if r.sha1 and r.sha1.lower() != expected_sha1.lower():
        raise RuntimeError("remote SHA-1 digest mismatch")
    return r


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
        if r.sha1 and r.sha1.lower() != expected_sha1.lower():
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
        size=_unwrap(rev.get("claimedSize"))
        dig=_unwrap(rev.get("claimedDigests"))
        sha1=_unwrap(dig.get("sha1")) if isinstance(dig,dict) else None
        out[current]=RemoteFile(str(name),int(size) if size is not None else None,str(sha1) if sha1 else None)
    for key in ("items","nodes","entries","children","value"):
        if key in data:
            child=_unwrap(data[key])
            out.update(_extract_tree_entries(child,current))
    return out

def list_tree(path:str)->dict[str,RemoteFile]:
    """List a destination tree once and flatten any descendants returned by Proton."""
    raw=run(["filesystem","list",path],json_output=True)
    data=json.loads(raw)
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
        if r.sha1 and r.sha1.lower()!=expected_sha1.lower():
            raise RuntimeError(f"remote SHA-1 digest mismatch for {rel}")
    if missing:
        raise RuntimeError("tree snapshot did not contain exact uploaded paths: "+", ".join(missing[:3]))
