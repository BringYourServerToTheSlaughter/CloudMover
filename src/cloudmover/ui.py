
"""Small dependency-free terminal UI helpers for CloudMover."""
from __future__ import annotations
import itertools
import sys
import threading
import time

def progress_bar(done: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return "[" + "-" * width + "]"
    ratio=max(0.0,min(1.0,done/total))
    filled=int(ratio*width)
    return "[" + "█"*filled + "░"*(width-filled) + f"] {ratio*100:5.1f}%"

class Activity:
    """Truthful spinner for opaque operations that do not expose byte progress."""
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str, detail: str = "", enabled: bool = True):
        self.label = label
        self.detail = detail
        self.enabled = enabled
        self._stop = None
        self._thread = None
        self._started = None

    def _render(self, frame: str, elapsed: float):
        import sys
        mins, secs = divmod(int(elapsed), 60)
        detail = f"  {self.detail}" if self.detail else ""
        sys.stdout.write(f"\r{self.label}  {frame}{detail}  elapsed {mins:02d}:{secs:02d}\033[K")
        sys.stdout.flush()

    def __enter__(self):
        if not self.enabled:
            return self
        import threading, time
        self._started = time.monotonic()
        self._stop = threading.Event()

        def spin():
            i = 0
            while not self._stop.is_set():
                self._render(self.frames[i % len(self.frames)], time.monotonic() - self._started)
                i += 1
                self._stop.wait(0.12)

        self._thread = threading.Thread(target=spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.enabled:
            return False
        import sys, time
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        elapsed = time.monotonic() - self._started
        symbol = "!" if exc_type else "✓"
        sys.stdout.write(f"\r{self.label}  {symbol}  {elapsed:.1f}s\033[K\n")
        sys.stdout.flush()
        return False

def dashboard(completed:int,total:int,done_bytes:int,total_bytes:int,free_gb:float,reserve_gb:float):
    print("iCloud Drive → Proton Drive\n")
    print(f"Overall   {progress_bar(completed,total)}")
    print(f"          {completed:,} / {total:,} files")
    if total_bytes:
        print(f"          {done_bytes/1024**2:,.0f} MB / {total_bytes/1024**3:,.2f} GB")
    print()
    print(f"Disk       ✓ {free_gb:.1f} GB free")
    print(f"Keep free    {reserve_gb:.0f} GB minimum")
    print()
    print("✓ Originals remain in iCloud")
    print("✓ Completed transfers are verified")
    print("✓ Safe to pause with Ctrl+C")


def stage_line(name: str, state: str, detail: str = "") -> str:
    icons={"done":"✓","active":"⠹","waiting":"·","failed":"!"}
    icon=icons.get(state,"·")
    suffix=f"  {detail}" if detail else ""
    return f"{name:<11}{icon}{suffix}"

def batch_summary(payload_bytes:int, hydrate_seconds:float|None=None,
                  upload_seconds:float|None=None, verify_seconds:float|None=None) -> str:
    mb=payload_bytes/1024**2
    lines=[f"Current batch: {mb:,.1f} MB",""]
    lines.append(stage_line("Hydrating","done" if hydrate_seconds is not None else "waiting",
                            f"{hydrate_seconds:.1f}s" if hydrate_seconds is not None else ""))
    lines.append(stage_line("Uploading","done" if upload_seconds is not None else "waiting",
                            f"{upload_seconds:.1f}s" if upload_seconds is not None else ""))
    lines.append(stage_line("Verifying","done" if verify_seconds is not None else "waiting",
                            f"{verify_seconds:.1f}s" if verify_seconds is not None else ""))
    return "\n".join(lines)
