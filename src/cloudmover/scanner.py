from __future__ import annotations
from .disk_space import RESERVE_BYTES, BATCH_BYTES
from .version import VERSION

import argparse
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .providers.icloud import HydrationState, inspect_file


@dataclass
class ScanResult:
    source: str
    files: int
    directories: int
    symlinks: int
    logical_bytes: int
    free_bytes: int
    local_files: int
    cloud_only_files: int
    unknown_files: int
    local_logical_bytes: int
    cloud_only_logical_bytes: int
    unknown_logical_bytes: int
    local_allocated_bytes: int
    recommended_strategy: str
    staging_bytes: int
    reserve_bytes: int


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} PB"


def walk_entries(root: Path, checkpoint=None, strict=False) -> Iterable[os.DirEntry]:
    stack = [root]
    while stack:
        if checkpoint:
            checkpoint()
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                while True:
                    # Check before asking the provider for another directory entry.
                    if checkpoint:
                        checkpoint()
                    try:
                        entry = next(entries)
                    except StopIteration:
                        break
                    if checkpoint:
                        checkpoint()
                    if entry.name == ".DS_Store" and entry.is_file(follow_symlinks=False):
                        continue
                    yield entry
                    if checkpoint:
                        checkpoint()
                    is_directory = entry.is_dir(follow_symlinks=False)
                    if checkpoint:
                        checkpoint()
                    if is_directory:
                        stack.append(Path(entry.path))
        except (PermissionError, FileNotFoundError, OSError) as exc:
            if strict:
                raise
            print(f"warning: could not scan {current}: {exc}")


def choose_strategy(total_bytes: int, free_bytes: int) -> tuple[str, int, int]:
    # The engine stages bounded batches even when the whole source fits locally.
    reserve = RESERVE_BYTES
    usable = max(0, free_bytes - reserve)

    if usable >= int(total_bytes * 1.10):
        return "FULL_LOCAL_STAGING", min(total_bytes, BATCH_BYTES), reserve

    staging = min(usable, BATCH_BYTES)
    return "SMART_STAGING", staging, reserve


def scan(source: Path) -> ScanResult:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"source does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"source is not a directory: {source}")

    files = 0
    directories = 1
    symlinks = 0
    logical_bytes = 0

    local_files = cloud_only_files = unknown_files = 0
    local_logical_bytes = cloud_only_logical_bytes = unknown_logical_bytes = 0
    local_allocated_bytes = 0

    for entry in walk_entries(source):
        try:
            if entry.is_symlink():
                symlinks += 1
            elif entry.is_dir(follow_symlinks=False):
                directories += 1
            elif entry.is_file(follow_symlinks=False):
                files += 1
                st = entry.stat(follow_symlinks=False)
                state = inspect_file(entry.path, stat_result=st)
                logical_bytes += state.logical_bytes

                if state.hydration is HydrationState.LOCAL:
                    local_files += 1
                    local_logical_bytes += state.logical_bytes
                    local_allocated_bytes += state.allocated_bytes
                elif state.hydration is HydrationState.CLOUD_ONLY:
                    cloud_only_files += 1
                    cloud_only_logical_bytes += state.logical_bytes
                else:
                    unknown_files += 1
                    unknown_logical_bytes += state.logical_bytes
        except (PermissionError, FileNotFoundError, OSError) as exc:
            print(f"warning: could not inspect {entry.path}: {exc}")

    usage = shutil.disk_usage(source)
    strategy, staging_bytes, reserve_bytes = choose_strategy(logical_bytes, usage.free)

    return ScanResult(
        source=str(source),
        files=files,
        directories=directories,
        symlinks=symlinks,
        logical_bytes=logical_bytes,
        free_bytes=usage.free,
        local_files=local_files,
        cloud_only_files=cloud_only_files,
        unknown_files=unknown_files,
        local_logical_bytes=local_logical_bytes,
        cloud_only_logical_bytes=cloud_only_logical_bytes,
        unknown_logical_bytes=unknown_logical_bytes,
        local_allocated_bytes=local_allocated_bytes,
        recommended_strategy=strategy,
        staging_bytes=staging_bytes,
        reserve_bytes=reserve_bytes,
    )


def print_report(result: ScanResult) -> None:
    print(f"CloudMover {VERSION}")
    print()
    print(f"Source:      {result.source}")
    print()
    print(f"Files:       {result.files:,}")
    print(f"Folders:     {result.directories:,}")
    print(f"Symlinks:    {result.symlinks:,}")
    print(f"Data:        {human_bytes(result.logical_bytes)}")
    print(f"Free space:  {human_bytes(result.free_bytes)}")
    print()
    print("iCloud hydration:")
    print(f"  Local:       {result.local_files:,} files / {human_bytes(result.local_logical_bytes)} logical")
    print(f"  Cloud-only:  {result.cloud_only_files:,} files / {human_bytes(result.cloud_only_logical_bytes)}")
    if result.unknown_files:
        print(f"  Unknown:     {result.unknown_files:,} files / {human_bytes(result.unknown_logical_bytes)}")
    print(f"  Local disk blocks in use: {human_bytes(result.local_allocated_bytes)}")
    print()
    print("Recommended strategy:")
    print(f"  {result.recommended_strategy}")
    print(f"  staging target: {human_bytes(result.staging_bytes)}")
    print(f"  protected reserve: {human_bytes(result.reserve_bytes)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan a source folder for CloudMover.")
    parser.add_argument("source", type=Path, help="Source folder to scan")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text report")
    args = parser.parse_args()

    result = scan(args.source)
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
