import os
import time
from pathlib import Path


def write_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write via a temp file and os.replace: a crash mid-write leaves the old file, never an empty or cut one."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    # Windows refuses the swap while another program (Obsidian, Sync, an antivirus scan) has the file open for a
    # moment: try again briefly, and leave no temp file behind if it never frees up.
    for wait in (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 0):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not wait:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(wait)
