from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


# macOS exposes iCloud placeholders as files with the SF_DATALESS flag.
# Python versions may not always publish the symbolic constant, so keep the
# Darwin value as a fallback. This is filesystem metadata only; checking it
# does not open/hydrate the file.
SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)


class HydrationState(str, Enum):
    LOCAL = "LOCAL"
    CLOUD_ONLY = "CLOUD_ONLY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FileState:
    hydration: HydrationState
    logical_bytes: int
    allocated_bytes: int


def inspect_file(path: str | Path, *, stat_result: os.stat_result | None = None) -> FileState:
    """Inspect an iCloud Drive file without opening or hydrating it.

    `logical_bytes` is the apparent file size. `allocated_bytes` is the amount
    currently backed by local filesystem blocks when the platform exposes
    st_blocks. A macOS SF_DATALESS flag classifies the item as CLOUD_ONLY.
    """
    try:
        st = stat_result or os.stat(path, follow_symlinks=False)
    except OSError:
        return FileState(HydrationState.UNKNOWN, 0, 0)

    flags = getattr(st, "st_flags", None)
    if flags is None:
        hydration = HydrationState.UNKNOWN
    elif flags & SF_DATALESS:
        hydration = HydrationState.CLOUD_ONLY
    else:
        hydration = HydrationState.LOCAL

    allocated = getattr(st, "st_blocks", 0) * 512
    return FileState(hydration, st.st_size, allocated)
