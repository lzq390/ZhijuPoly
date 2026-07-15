from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import stat
from time import monotonic


@dataclass(frozen=True)
class ByteFF2RuntimeAsset:
    relative_path: Path
    size: int
    sha256: str


BYTEFF2_RUNTIME_ASSETS = (
    ByteFF2RuntimeAsset(
        Path(
            "submodules/bytemol/bytemol/toolkit/infer_molecule/"
            "bond_length_ref.csv"
        ),
        802,
        "caa78ff02c7e65fb0c8bcf240382fa8d90b0dfea85a4d9888c96eab04cc4a40e",
    ),
    ByteFF2RuntimeAsset(
        Path("byteff2/trained_models/fftrainer_config_in_use.yaml"),
        986,
        "8245a5c6ad9b4aa9d180c8bb24d6f05c210f1724ffae93aec0ef4f88e5fd7ea3",
    ),
    ByteFF2RuntimeAsset(
        Path("byteff2/trained_models/optimal.pt"),
        111_892_932,
        "ae47a6e6860b563908a2e0a83d4a3f6adc1c36b48f544e2241d24066d43d539c",
    ),
)


def validate_byteff2_runtime_assets(
    root: Path,
    *,
    deadline: float | None = None,
) -> str | None:
    """Validate audited data needed by protocol import and model loading."""

    try:
        resolved_root = root.resolve(strict=True)
        root_metadata = resolved_root.lstat()
    except OSError:
        return "ByteFF2 runtime asset root is unavailable"
    if not stat.S_ISDIR(root_metadata.st_mode):
        return "ByteFF2 runtime asset root is unavailable"

    for asset in BYTEFF2_RUNTIME_ASSETS:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError
        candidate = resolved_root / asset.relative_path
        if not _parents_are_real_directories(resolved_root, asset.relative_path):
            return f"required ByteFF2 runtime asset is unsafe: {asset.relative_path.name}"
        try:
            metadata = candidate.lstat()
        except OSError:
            return f"required ByteFF2 runtime asset is missing: {asset.relative_path.name}"
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return f"required ByteFF2 runtime asset is unsafe: {asset.relative_path.name}"
        if metadata.st_size != asset.size:
            return f"required ByteFF2 runtime asset size mismatch: {asset.relative_path.name}"
        try:
            digest = _hash_runtime_asset(
                candidate,
                metadata,
                deadline=deadline,
            )
        except TimeoutError:
            raise
        except OSError:
            return f"required ByteFF2 runtime asset is unreadable: {asset.relative_path.name}"
        if digest != asset.sha256:
            return f"required ByteFF2 runtime asset digest mismatch: {asset.relative_path.name}"
    return None


def _parents_are_real_directories(root: Path, relative_path: Path) -> bool:
    current = root
    for component in relative_path.parts[:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return False
    return True


def _hash_runtime_asset(
    path: Path,
    metadata: os.stat_result,
    *,
    deadline: float | None,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        expected_identity = _file_identity(metadata)
        if not stat.S_ISREG(before.st_mode) or _file_identity(before) != expected_identity:
            raise OSError("runtime asset identity changed before hashing")
        digest = hashlib.sha256()
        total = 0
        while True:
            if deadline is not None and monotonic() >= deadline:
                raise TimeoutError
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _file_identity(after) != expected_identity or total != after.st_size:
            raise OSError("runtime asset identity changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
