from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("journal must not be a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("journal is missing or unsafe")
    with path.open() as handle:
        return json.load(handle, parse_constant=lambda text: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def restore_file(root: Path, relative: str, source: Path) -> Path:
    """Atomically restore a private input, rejecting symlinks at every component."""
    candidate = Path(relative)
    if (not isinstance(relative, str) or candidate.is_absolute() or not candidate.parts
            or any(part in {".", ".."} for part in candidate.parts) or root.is_symlink()):
        raise ValueError("unsafe checkpoint destination")
    parent = root
    for part in candidate.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError("unsafe checkpoint directory")
        parent.mkdir(mode=0o700, exist_ok=True)
    target = parent / candidate.name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("unsafe checkpoint destination")
    descriptor, temporary = tempfile.mkstemp(prefix=".restore-", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as original:
            os.fchmod(output.fileno(), 0o600)
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def safe_file(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or any(part in {".", ".."} for part in candidate.parts):
        raise ValueError("unsafe artifact path")
    current = root
    if root.is_symlink():
        raise ValueError("unsafe artifact root")
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symbolic links are not permitted in artifacts")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("artifact is not a regular contained file")
    return current


class RootLock:
    def __init__(self, root: Path):
        self.root = root
        self.fd: int | None = None

    def acquire(self):
        if self.root.is_symlink():
            raise ValueError("worker root must not be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.fd = os.open(self.root / ".supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
