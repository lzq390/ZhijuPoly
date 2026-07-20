from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Iterable

import numpy as np

from .schemas import (
    MAX_ARTIFACT_SIZE_BYTES,
    ArtifactDescriptor,
    validate_artifact_name,
)


MAX_BUNDLE_SIZE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_EXPANDED_BYTES = 256 * 1024 * 1024
BUNDLE_CREATING_NAME = ".artifact_bundle.zip.creating"


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"artifact directory must be a real directory: {path}")
    os.chmod(path, 0o700)
    return path


def atomic_write_bytes(path: Path, content: bytes) -> None:
    parent = ensure_private_directory(path.parent)
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace a symlinked artifact: {path}")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    atomic_write_bytes(path, content)


def atomic_write_npz(path: Path, **arrays: Any) -> None:
    """Write a non-pickle NumPy archive through the same atomic path."""
    buffer = io.BytesIO()
    np.savez_compressed(
        buffer, **{key: np.asarray(value) for key, value in arrays.items()}
    )
    atomic_write_bytes(path, buffer.getvalue())


def open_readonly_regular(path: Path) -> BinaryIO:
    """Open one immutable read handle without following a final symlink."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"artifact must be a regular file: {path}")
        return os.fdopen(file_descriptor, "rb", closefd=True)
    except BaseException:
        os.close(file_descriptor)
        raise


def sha256_open_file(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    with open_readonly_regular(path) as stream:
        return sha256_open_file(stream)


def verify_artifact_stream(
    stream: BinaryIO,
    descriptor: ArtifactDescriptor,
) -> None:
    validate_artifact_name(descriptor.name)
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("manifest artifact is not a regular file")
    if metadata.st_size > MAX_ARTIFACT_SIZE_BYTES:
        raise RuntimeError("artifact exceeds the 64 MiB worker limit")
    if metadata.st_size != descriptor.size_bytes:
        raise RuntimeError("artifact no longer matches its manifest checksum")
    if sha256_open_file(stream) != descriptor.sha256:
        raise RuntimeError("artifact no longer matches its manifest checksum")


def open_verified_artifact(
    path: Path,
    descriptor: ArtifactDescriptor,
) -> BinaryIO:
    stream = open_readonly_regular(path)
    try:
        verify_artifact_stream(stream, descriptor)
        return stream
    except BaseException:
        stream.close()
        raise


def describe_artifact(
    *,
    artifact_id: str,
    path: Path,
    media_type: str,
) -> ArtifactDescriptor:
    validate_artifact_name(path.name)
    with open_readonly_regular(path) as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_size > MAX_ARTIFACT_SIZE_BYTES:
            raise RuntimeError("artifact exceeds the 64 MiB worker limit")
        return ArtifactDescriptor(
            artifact_id=artifact_id,
            name=path.name,
            media_type=media_type,
            size_bytes=metadata.st_size,
            sha256=sha256_open_file(stream),
        )


def write_xyz(
    path: Path,
    *,
    symbols: Iterable[str],
    coordinates: Any,
    comment: str,
) -> None:
    coordinates_array = np.asarray(coordinates, dtype=np.float64)
    symbol_list = list(symbols)
    if coordinates_array.shape != (len(symbol_list), 3):
        raise ValueError("XYZ coordinates must have shape (N, 3)")
    rows = [str(len(symbol_list)), comment.replace("\n", " ")]
    for symbol, xyz in zip(symbol_list, coordinates_array, strict=True):
        rows.append(f"{symbol:<3s} {xyz[0]: .12f} {xyz[1]: .12f} {xyz[2]: .12f}")
    atomic_write_bytes(path, ("\n".join(rows) + "\n").encode("utf-8"))


def build_bundle(
    path: Path,
    artifacts: Iterable[tuple[ArtifactDescriptor, BinaryIO]],
) -> BinaryIO:
    parent = ensure_private_directory(path.parent)
    temporary_path = parent / BUNDLE_CREATING_NAME
    for candidate in (path, temporary_path):
        if candidate.is_symlink():
            raise RuntimeError("refusing an unsafe artifact bundle path")
    if temporary_path.exists():
        if not temporary_path.is_file():
            raise RuntimeError("stale artifact bundle creation path is not a file")
        temporary_path.unlink()
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    file_descriptor = os.open(
        temporary_path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    bundle_stream = os.fdopen(file_descriptor, "w+b", closefd=True)
    try:
        os.fchmod(bundle_stream.fileno(), 0o600)
        with zipfile.ZipFile(
            bundle_stream,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            seen_names: set[str] = set()
            expanded_size = 0
            for descriptor, artifact_stream in artifacts:
                try:
                    validate_artifact_name(descriptor.name)
                except ValueError as exc:
                    raise RuntimeError(
                        "refusing to bundle an unsafe artifact name"
                    ) from exc
                folded_name = descriptor.name.casefold()
                if folded_name in seen_names:
                    raise RuntimeError(
                        "refusing to bundle case-insensitive duplicate artifact names"
                    )
                seen_names.add(folded_name)
                verify_artifact_stream(artifact_stream, descriptor)
                expanded_size += descriptor.size_bytes
                if expanded_size > MAX_BUNDLE_EXPANDED_BYTES:
                    raise RuntimeError(
                        "artifact bundle expanded content exceeds the 256 MiB limit"
                    )
                with archive.open(descriptor.name, mode="w") as member:
                    for chunk in iter(lambda: artifact_stream.read(1024 * 1024), b""):
                        member.write(chunk)
                artifact_stream.seek(0)
        bundle_stream.flush()
        os.fsync(bundle_stream.fileno())
        if os.fstat(bundle_stream.fileno()).st_size > MAX_BUNDLE_SIZE_BYTES:
            raise RuntimeError("artifact bundle exceeds the 256 MiB transfer limit")
        os.replace(temporary_path, path)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        bundle_stream.seek(0)
        return bundle_stream
    except BaseException:
        bundle_stream.close()
        if temporary_path.exists():
            temporary_path.unlink()
        raise
