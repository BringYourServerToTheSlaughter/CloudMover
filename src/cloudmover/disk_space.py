"""Conservative bounds for hydration plus a possible staging copy."""
import shutil
import tempfile

RESERVE_BYTES = 5_000_000_000
BATCH_BYTES = 250_000_000

def require_batch_space(items, reserve_bytes=RESERVE_BYTES):
    reserve_bytes = max(RESERVE_BYTES, reserve_bytes)
    payload = sum(item.logical_bytes for item in items)
    # Assume every source byte needs hydration and hard-link staging falls back to copy.
    # Checking both volumes conservatively also covers a separate temporary volume.
    for path in {str(item.source_path) for item in items} | {tempfile.gettempdir()}:
        if shutil.disk_usage(path).free < reserve_bytes + 2 * payload:
            raise RuntimeError("Not enough free disk space for this batch plus the 5 GB minimum reserve. Free space and resume.")
