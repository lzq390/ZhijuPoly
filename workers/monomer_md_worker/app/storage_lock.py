from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator


@contextmanager
def job_storage_lock(job_root: Path, job_id: str) -> Iterator[None]:
    """Serialize readers and destructive storage operations across processes."""

    if not job_id or Path(job_id).name != job_id or job_id in {".", ".."}:
        raise RuntimeError("monomer MD storage lock job id is unsafe")
    lock_root = job_root / ".locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = lock_root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("monomer MD storage lock root is unsafe")
    lock_path = lock_root / f"{job_id}.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("monomer MD storage lock file is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
