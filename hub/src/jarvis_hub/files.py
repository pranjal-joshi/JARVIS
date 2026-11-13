import os
from pathlib import Path


def write_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write via a temp file and os.replace: a crash mid-write leaves the old file, never an empty or cut one."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
