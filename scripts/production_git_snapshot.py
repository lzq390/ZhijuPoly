#!/usr/bin/env python3
"""Create and verify the one-time production ``.git`` golden snapshot.

The snapshot is deliberately independent from Git's normal ref/object update
machinery.  Every byte is copied through descriptor-relative reads and writes;
hard links, symbolic links, special files, group/world-accessible entries and
shared (reflink) extents are rejected.  The resulting manifest is the restore
authority used by the first-deployment bootstrap router.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4


PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
AUTHORITY_RELATIVE_PATH = Path("state/production-git-snapshot.json")
JOURNAL_DIRECTORY_RELATIVE_PATH = Path(
    "state/production-git-snapshot-transactions"
)
BACKUP_ROOT_RELATIVE_PATH = Path("backups/production-git")
LOCK_RELATIVE_PATH = Path("state/production-git-snapshot.lock")
AUTHORITY_KIND = "manual-runtime-adoption-production-git-snapshot"
POLICY = "nexpoly-production-git-golden-snapshot-v1"
MANIFEST_POLICY = "nexpoly-production-git-raw-manifest-v1"
COPY_POLICY = "descriptor-relative-read-write-no-link-no-reflink-v1"
PUBLICATION_POLICY = "create-once-private-snapshot-authority-v1"
OPERATION_RE = re.compile(
    r"snapshot-git-[a-z0-9][a-z0-9._-]{7,95}\Z"
)
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
MAX_RECORDS = 10_000_000
MAX_FILE_BYTES = 1024**4
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
READ_SIZE = 1024 * 1024
FIEMAP_IOCTL = 0xC020660B
FIEMAP_EXTENT_SHARED = 0x00002000
FIEMAP_EXTENT_LAST = 0x00000001
FIEMAP_HEADER_FORMAT = "=QQIIII"
FIEMAP_EXTENT_FORMAT = "=QQQQQIIII"
FIEMAP_HEADER_SIZE = struct.calcsize(FIEMAP_HEADER_FORMAT)
FIEMAP_EXTENT_SIZE = struct.calcsize(FIEMAP_EXTENT_FORMAT)
FIEMAP_BATCH_EXTENTS = 256


class SnapshotError(RuntimeError):
    """The production Git snapshot cannot be proved safely."""


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_digest(value: object) -> str:
    return digest_bytes(canonical_json_bytes(value))


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise SnapshotError(f"{label} is invalid")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise SnapshotError(f"{label} is invalid")
    return value


def _require_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value) is None:
        raise SnapshotError("snapshot operation ID is invalid")
    return value


def validate_delivery_gate(
    document: object,
    *,
    target_sha: str,
) -> dict[str, object]:
    fields = {
        "workflow_run_id",
        "run_attempt",
        "head_sha",
        "head_branch",
        "event",
        "path",
        "conclusion",
        "required_jobs",
    }
    if (
        not isinstance(document, dict)
        or set(document) != {"remote_main", "ci"}
        or document.get("remote_main") != target_sha
        or not isinstance(document.get("ci"), dict)
        or set(document["ci"]) != fields
    ):
        raise SnapshotError("snapshot delivery gate has an invalid shape")
    ci = document["ci"]
    if (
        not isinstance(ci.get("workflow_run_id"), int)
        or isinstance(ci.get("workflow_run_id"), bool)
        or ci["workflow_run_id"] <= 0
        or not isinstance(ci.get("run_attempt"), int)
        or isinstance(ci.get("run_attempt"), bool)
        or ci["run_attempt"] <= 0
        or ci.get("head_sha") != target_sha
        or ci.get("head_branch") != "main"
        or ci.get("event") != "push"
        or ci.get("path") != ".github/workflows/ci.yml"
        or ci.get("conclusion") != "success"
        or not isinstance(ci.get("required_jobs"), list)
        or not ci["required_jobs"]
        or ci["required_jobs"] != sorted(set(ci["required_jobs"]))
        or any(not isinstance(name, str) or not name for name in ci["required_jobs"])
    ):
        raise SnapshotError("snapshot delivery CI authority is invalid")
    return {"remote_main": target_sha, "ci": dict(ci)}


def _require_private_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"private directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SnapshotError(f"private directory is unsafe: {path}")
    return metadata


def _open_directory(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SnapshotError(f"directory cannot be opened safely: {path}") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise SnapshotError(f"directory identity is unsafe: {path}")
    return descriptor


def _safe_component(name: str) -> bool:
    return bool(
        name
        and name not in {".", ".."}
        and "/" not in name
        and "\0" not in name
        and PurePosixPath(name).as_posix() == name
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise SnapshotError("private file write made no progress")
        offset += written


def _read_regular_file_at(
    parent_fd: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise SnapshotError(f"Git file cannot be opened safely: {name}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in {0o600, 0o700}
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > MAX_FILE_BYTES
        ):
            raise SnapshotError(f"Git file identity is unsafe: {name}")
        if expected is not None and (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_uid,
            expected.st_nlink,
            expected.st_size,
            expected.st_mtime_ns,
        ):
            raise SnapshotError(f"Git file changed before read: {name}")
        payload = bytearray()
        while len(payload) < before.st_size:
            chunk = os.read(descriptor, min(READ_SIZE, before.st_size - len(payload)))
            if not chunk:
                raise SnapshotError(f"Git file was truncated while reading: {name}")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            raise SnapshotError(f"Git file grew while reading: {name}")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SnapshotError(f"Git file changed while reading: {name}")
        return bytes(payload), before
    finally:
        os.close(descriptor)


def _hash_regular_file_at(
    parent_fd: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
) -> tuple[str, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise SnapshotError(f"Git file cannot be opened safely: {name}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in {0o600, 0o700}
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > MAX_FILE_BYTES
        ):
            raise SnapshotError(f"Git file identity is unsafe: {name}")
        if expected is not None and (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_uid,
            expected.st_nlink,
            expected.st_size,
            expected.st_mtime_ns,
        ):
            raise SnapshotError(f"Git file changed before hashing: {name}")
        hasher = hashlib.sha256()
        read_bytes = 0
        while read_bytes < before.st_size:
            chunk = os.read(descriptor, min(READ_SIZE, before.st_size - read_bytes))
            if not chunk:
                raise SnapshotError(f"Git file was truncated while hashing: {name}")
            hasher.update(chunk)
            read_bytes += len(chunk)
        if os.read(descriptor, 1):
            raise SnapshotError(f"Git file grew while hashing: {name}")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SnapshotError(f"Git file changed while hashing: {name}")
        return "sha256:" + hasher.hexdigest(), before
    finally:
        os.close(descriptor)


def _scan_directory_fd(
    descriptor: int,
    *,
    prefix: str,
    records: list[dict[str, object]],
    seen_inodes: set[tuple[int, int]],
) -> None:
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise SnapshotError("Git directory cannot be enumerated") from exc
    if len(records) + len(names) > MAX_RECORDS:
        raise SnapshotError("Git snapshot inventory is oversized")
    for name in names:
        if not _safe_component(name):
            raise SnapshotError("Git snapshot contains an unsafe pathname")
        relative = f"{prefix}/{name}" if prefix else name
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SnapshotError(f"Git entry disappeared: {relative}") from exc
        inode = (metadata.st_dev, metadata.st_ino)
        if inode in seen_inodes:
            raise SnapshotError(f"Git snapshot contains an aliased inode: {relative}")
        seen_inodes.add(inode)
        if stat.S_ISDIR(metadata.st_mode):
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise SnapshotError(f"Git directory is not owner-private: {relative}")
            records.append(
                {"path": relative, "kind": "directory", "mode": "0700"}
            )
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise SnapshotError(f"Git directory cannot be opened: {relative}") from exc
            try:
                opened = os.fstat(child_fd)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_uid,
                ) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                ):
                    raise SnapshotError(f"Git directory changed while opening: {relative}")
                _scan_directory_fd(
                    child_fd,
                    prefix=relative,
                    records=records,
                    seen_inodes=seen_inodes,
                )
                closed = os.fstat(child_fd)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_uid,
                    opened.st_mtime_ns,
                ) != (
                    closed.st_dev,
                    closed.st_ino,
                    closed.st_mode,
                    closed.st_uid,
                    closed.st_mtime_ns,
                ):
                    raise SnapshotError(f"Git directory changed while reading: {relative}")
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            content_digest, opened = _hash_regular_file_at(
                descriptor,
                name,
                expected=metadata,
            )
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": format(stat.S_IMODE(opened.st_mode), "04o"),
                    "size": opened.st_size,
                    "sha256": content_digest,
                }
            )
        else:
            raise SnapshotError(f"Git snapshot contains a special entry: {relative}")


def scan_git_directory(git_dir: Path) -> dict[str, object]:
    """Return the canonical raw-path/content inventory of one Git directory."""

    root = _require_private_directory(git_dir)
    descriptor = _open_directory(git_dir)
    records: list[dict[str, object]] = []
    try:
        _scan_directory_fd(
            descriptor,
            prefix="",
            records=records,
            seen_inodes={(root.st_dev, root.st_ino)},
        )
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        root.st_dev,
        root.st_ino,
        root.st_mode,
        root.st_uid,
        root.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_mtime_ns,
    ):
        raise SnapshotError("Git root changed while scanning")
    records.sort(key=lambda record: str(record["path"]))
    files = [record for record in records if record["kind"] == "file"]
    directories = [record for record in records if record["kind"] == "directory"]
    inventory_digest = canonical_digest(records)
    return {
        "schema_version": 1,
        "policy": MANIFEST_POLICY,
        "root_mode": "0700",
        "records": records,
        "records_sha256": inventory_digest,
        "file_count": len(files),
        "directory_count": len(directories),
        "total_file_bytes": sum(int(record["size"]) for record in files),
    }


def validate_manifest(document: object) -> dict[str, object]:
    fields = {
        "schema_version",
        "policy",
        "root_mode",
        "records",
        "records_sha256",
        "file_count",
        "directory_count",
        "total_file_bytes",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("policy") != MANIFEST_POLICY
        or document.get("root_mode") != "0700"
        or not isinstance(document.get("records"), list)
        or len(document["records"]) > MAX_RECORDS
    ):
        raise SnapshotError("Git snapshot manifest has an invalid shape")
    records = document["records"]
    paths: list[str] = []
    files = 0
    directories = 0
    total = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise SnapshotError("Git snapshot manifest record is invalid")
        path = record["path"]
        pure = PurePosixPath(path)
        if (
            not path
            or pure.is_absolute()
            or ".." in pure.parts
            or pure.as_posix() != path
        ):
            raise SnapshotError("Git snapshot manifest path is invalid")
        paths.append(path)
        if record.get("kind") == "directory":
            if set(record) != {"path", "kind", "mode"} or record.get("mode") != "0700":
                raise SnapshotError("Git snapshot directory record is invalid")
            directories += 1
        elif record.get("kind") == "file":
            if (
                set(record) != {"path", "kind", "mode", "size", "sha256"}
                or record.get("mode") not in {"0600", "0700"}
                or isinstance(record.get("size"), bool)
                or not isinstance(record.get("size"), int)
                or record["size"] < 0
                or record["size"] > MAX_FILE_BYTES
            ):
                raise SnapshotError("Git snapshot file record is invalid")
            _require_digest(record.get("sha256"), "Git snapshot file digest")
            files += 1
            total += record["size"]
        else:
            raise SnapshotError("Git snapshot manifest entry kind is invalid")
    if (
        paths != sorted(set(paths))
        or document.get("records_sha256") != canonical_digest(records)
        or document.get("file_count") != files
        or document.get("directory_count") != directories
        or document.get("total_file_bytes") != total
    ):
        raise SnapshotError("Git snapshot manifest summary differs")
    kinds = {str(record["path"]): str(record["kind"]) for record in records}
    for path in paths:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            if kinds.get(parent.as_posix()) != "directory":
                raise SnapshotError("Git snapshot manifest parent is missing")
            parent = parent.parent
    return dict(document)


def _fiemap_has_shared_extents(descriptor: int, size: int) -> bool:
    """Return true when Linux reports a shared physical extent."""

    if size == 0:
        return False
    start = 0
    while start < size:
        header = struct.pack(
            FIEMAP_HEADER_FORMAT,
            start,
            size - start,
            0,
            0,
            FIEMAP_BATCH_EXTENTS,
            0,
        )
        buffer = bytearray(
            header + b"\0" * (FIEMAP_EXTENT_SIZE * FIEMAP_BATCH_EXTENTS)
        )
        try:
            fcntl.ioctl(descriptor, FIEMAP_IOCTL, buffer, True)
        except OSError as exc:
            # Filesystems without FIEMAP cannot prove the no-reflink contract.
            raise SnapshotError("filesystem cannot prove non-shared snapshot extents") from exc
        _start, _length, _flags, mapped, _count, _reserved = struct.unpack_from(
            FIEMAP_HEADER_FORMAT,
            buffer,
            0,
        )
        if mapped == 0:
            return False
        last_end = start
        last_flags = 0
        for index in range(mapped):
            extent = struct.unpack_from(
                FIEMAP_EXTENT_FORMAT,
                buffer,
                FIEMAP_HEADER_SIZE + index * FIEMAP_EXTENT_SIZE,
            )
            logical = extent[0]
            length = extent[2]
            flags = extent[5]
            if flags & FIEMAP_EXTENT_SHARED:
                return True
            if length <= 0 or logical < last_end:
                raise SnapshotError("filesystem returned an invalid extent map")
            last_end = logical + length
            last_flags = flags
        if last_flags & FIEMAP_EXTENT_LAST:
            return False
        if last_end <= start:
            raise SnapshotError("filesystem extent scan did not advance")
        start = last_end
    return False


def _write_file_copy(
    source_parent_fd: int,
    destination_parent_fd: int,
    name: str,
    *,
    expected: Mapping[str, object],
) -> None:
    try:
        source_descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=source_parent_fd,
        )
    except OSError as exc:
        raise SnapshotError(
            f"source Git file cannot be opened: {expected.get('path')}"
        ) from exc
    source = os.fstat(source_descriptor)
    if (
        not stat.S_ISREG(source.st_mode)
        or source.st_uid != os.geteuid()
        or source.st_nlink != 1
        or source.st_size != expected.get("size")
        or format(stat.S_IMODE(source.st_mode), "04o") != expected.get("mode")
    ):
        os.close(source_descriptor)
        raise SnapshotError(f"source Git file differs from plan: {expected.get('path')}")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    destination = -1
    try:
        destination = os.open(
            name,
            flags,
            int(str(expected["mode"]), 8),
            dir_fd=destination_parent_fd,
        )
        hasher = hashlib.sha256()
        written = 0
        while written < source.st_size:
            chunk = os.read(
                source_descriptor,
                min(READ_SIZE, source.st_size - written),
            )
            if not chunk:
                raise SnapshotError("source Git file was truncated during copy")
            hasher.update(chunk)
            offset = 0
            while offset < len(chunk):
                count = os.write(destination, chunk[offset:])
                if count <= 0:
                    raise SnapshotError("snapshot write made no progress")
                offset += count
            written += len(chunk)
        if os.read(source_descriptor, 1):
            raise SnapshotError("source Git file grew during copy")
        source_after = os.fstat(source_descriptor)
        if (
            source.st_dev,
            source.st_ino,
            source.st_mode,
            source.st_uid,
            source.st_nlink,
            source.st_size,
            source.st_mtime_ns,
        ) != (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_mode,
            source_after.st_uid,
            source_after.st_nlink,
            source_after.st_size,
            source_after.st_mtime_ns,
        ) or "sha256:" + hasher.hexdigest() != expected.get("sha256"):
            raise SnapshotError("source Git file changed during copy")
        os.fchmod(destination, int(str(expected["mode"]), 8))
        os.fsync(destination)
        copied = os.fstat(destination)
        if (
            copied.st_uid != os.geteuid()
            or copied.st_nlink != 1
            or (copied.st_dev, copied.st_ino) == (source.st_dev, source.st_ino)
            or _fiemap_has_shared_extents(destination, copied.st_size)
        ):
            raise SnapshotError("snapshot copy is linked or shared with its source")
    except OSError as exc:
        raise SnapshotError(
            f"snapshot file cannot be created or written: {expected.get('path')}"
        ) from exc
    finally:
        if destination >= 0:
            os.close(destination)
        os.close(source_descriptor)


def _copy_directory_fd(
    source_fd: int,
    destination_fd: int,
    *,
    prefix: str,
    by_path: Mapping[str, Mapping[str, object]],
) -> None:
    names = sorted(os.listdir(source_fd))
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        expected = by_path.get(relative)
        if expected is None:
            raise SnapshotError(f"source Git entry is absent from plan: {relative}")
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if expected.get("kind") == "directory":
            if not stat.S_ISDIR(metadata.st_mode):
                raise SnapshotError(f"source Git directory changed: {relative}")
            try:
                os.mkdir(name, 0o700, dir_fd=destination_fd)
                source_child = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=source_fd,
                )
                destination_child = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=destination_fd,
                )
            except OSError as exc:
                raise SnapshotError(f"snapshot directory cannot be created: {relative}") from exc
            try:
                _copy_directory_fd(
                    source_child,
                    destination_child,
                    prefix=relative,
                    by_path=by_path,
                )
                os.fsync(destination_child)
            finally:
                os.close(destination_child)
                os.close(source_child)
        elif expected.get("kind") == "file":
            if not stat.S_ISREG(metadata.st_mode):
                raise SnapshotError(f"source Git file changed: {relative}")
            _write_file_copy(
                source_fd,
                destination_fd,
                name,
                expected=expected,
            )
        else:
            raise SnapshotError(f"planned Git entry kind is invalid: {relative}")
    os.fsync(destination_fd)


def copy_git_directory(
    source: Path,
    destination: Path,
    manifest: Mapping[str, object],
) -> None:
    validated = validate_manifest(dict(manifest))
    if destination.exists() or destination.is_symlink():
        raise SnapshotError("snapshot destination already exists")
    _require_private_directory(destination.parent)
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise SnapshotError("snapshot destination cannot be created") from exc
    source_fd = _open_directory(source)
    destination_fd = _open_directory(destination)
    try:
        by_path = {
            str(record["path"]): record
            for record in validated["records"]  # type: ignore[index]
        }
        _copy_directory_fd(
            source_fd,
            destination_fd,
            prefix="",
            by_path=by_path,
        )
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    observed = scan_git_directory(destination)
    if observed != validated:
        raise SnapshotError("completed Git snapshot differs from its manifest")


def _remove_private_tree(path: Path) -> None:
    """Remove only a private, single-link operation-owned directory tree."""

    _require_private_directory(path.parent)
    parent_fd = _open_directory(path.parent)

    def remove_at(directory_fd: int, name: str, relative: str) -> None:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise SnapshotError(f"partial snapshot directory is unsafe: {relative}")
            child_fd = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                for child in sorted(os.listdir(child_fd)):
                    if not _safe_component(child):
                        raise SnapshotError("partial snapshot pathname is unsafe")
                    remove_at(child_fd, child, f"{relative}/{child}")
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(metadata.st_mode):
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o700}
                or metadata.st_nlink != 1
            ):
                raise SnapshotError(f"partial snapshot file is unsafe: {relative}")
            os.unlink(name, dir_fd=directory_fd)
        else:
            raise SnapshotError(f"partial snapshot entry is unsafe: {relative}")

    try:
        remove_at(parent_fd, path.name, path.name)
        os.fsync(parent_fd)
    except OSError as exc:
        raise SnapshotError("partial snapshot tree cannot be removed safely") from exc
    finally:
        os.close(parent_fd)


def _run_git(
    repository: Path,
    *arguments: str,
    git_dir_only: bool = False,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    command = ["/usr/bin/git"]
    if git_dir_only:
        command.extend([f"--git-dir={repository}"])
        cwd = repository.parent
    else:
        command.extend(["-C", str(repository)])
        cwd = repository
    command.extend(arguments)
    environment = {
        "HOME": "/home/devuser",
        "USER": "devuser",
        "LOGNAME": "devuser",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SnapshotError(f"Git command failed: {' '.join(arguments)}") from exc


def strict_fsck(git_dir: Path) -> dict[str, object]:
    completed = _run_git(
        git_dir,
        "fsck",
        "--strict",
        "--full",
        "--no-reflogs",
        "--no-progress",
        git_dir_only=True,
    )
    stdout = completed.stdout.encode("utf-8")
    stderr = completed.stderr.encode("utf-8")
    if len(stdout) + len(stderr) > 64 * 1024 * 1024:
        raise SnapshotError("strict Git fsck evidence is oversized")
    return {
        "schema_version": 1,
        "policy": "git-fsck-strict-full-no-reflogs-v1",
        "exit_code": 0,
        "stdout_sha256": digest_bytes(stdout),
        "stderr_sha256": digest_bytes(stderr),
        "stdout_lines": len(completed.stdout.splitlines()),
        "stderr_lines": len(completed.stderr.splitlines()),
    }


def _atomic_private_json(path: Path, document: object) -> None:
    payload = canonical_json_bytes(document)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise SnapshotError("snapshot JSON document is oversized")
    _require_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _write_all(descriptor, payload)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        parent_fd = _open_directory(path.parent)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise SnapshotError(f"snapshot JSON cannot be committed: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_private_json(
    path: Path,
    *,
    maximum: int = MAX_MANIFEST_BYTES,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> tuple[dict[str, Any], str]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SnapshotError(f"snapshot JSON is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink not in allowed_nlinks
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            raise SnapshotError(f"snapshot JSON identity is unsafe: {path}")
        payload = bytearray()
        while len(payload) < before.st_size:
            chunk = os.read(descriptor, min(READ_SIZE, before.st_size - len(payload)))
            if not chunk:
                raise SnapshotError(f"snapshot JSON changed while reading: {path}")
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
        ):
            raise SnapshotError(f"snapshot JSON changed while reading: {path}")
    finally:
        os.close(descriptor)
    raw = bytes(payload)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"snapshot JSON is malformed: {path}") from exc
    if not isinstance(document, dict) or canonical_json_bytes(document) != raw:
        raise SnapshotError(f"snapshot JSON is not canonical: {path}")
    return document, digest_bytes(raw)


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _require_private_directory(path)
        return
    try:
        path.mkdir(mode=0o700)
    except OSError as exc:
        raise SnapshotError(f"private directory cannot be created: {path}") from exc
    _require_private_directory(path)


def validate_authority(
    document: object,
    *,
    runtime_root: Path = RUNTIME_ROOT,
    production_root: Path | None = PRODUCTION_ROOT,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "authority_kind",
        "policy",
        "operation_id",
        "target_source_sha",
        "target_source_tree",
        "production_source_sha",
        "production_source_tree",
        "production_git_dir",
        "backup_git_dir",
        "manifest_path",
        "manifest_sha256",
        "manifest_summary",
        "fsck",
        "delivery_gate",
        "delivery_gate_sha256",
        "plan_sha256",
        "snapshot_impact_sha256",
        "copy_policy",
        "completed_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("status") != "completed"
        or document.get("authority_kind") != AUTHORITY_KIND
        or document.get("policy") != POLICY
        or document.get("copy_policy") != COPY_POLICY
    ):
        raise SnapshotError("production Git snapshot authority has an invalid shape")
    _require_operation_id(document.get("operation_id"))
    for field in (
        "target_source_sha",
        "target_source_tree",
        "production_source_sha",
        "production_source_tree",
    ):
        _require_sha(document.get(field), f"snapshot authority {field}")
    for field in (
        "manifest_sha256",
        "delivery_gate_sha256",
        "plan_sha256",
        "snapshot_impact_sha256",
    ):
        _require_digest(document.get(field), f"snapshot authority {field}")
    operation_id = str(document["operation_id"])
    expected_root = runtime_root / BACKUP_ROOT_RELATIVE_PATH / operation_id
    production_path_valid = (
        document.get("production_git_dir") == str(production_root / ".git")
        if production_root is not None
        else Path(str(document.get("production_git_dir", ""))).name == ".git"
    )
    if not production_path_valid:
        raise SnapshotError("snapshot production Git path is invalid")
    backup = Path(str(document.get("backup_git_dir", "")))
    manifest_path = Path(str(document.get("manifest_path", "")))
    if (
        not backup.is_absolute()
        or not manifest_path.is_absolute()
        or backup.name != "git"
        or manifest_path.name != "MANIFEST.json"
        or backup.parent != manifest_path.parent
        or backup.parent != expected_root
    ):
        raise SnapshotError("snapshot backup paths are invalid")
    summary = document.get("manifest_summary")
    if (
        not isinstance(summary, dict)
        or set(summary)
        != {"records_sha256", "file_count", "directory_count", "total_file_bytes"}
    ):
        raise SnapshotError("snapshot manifest summary is invalid")
    _require_digest(summary.get("records_sha256"), "snapshot records digest")
    for field in ("file_count", "directory_count", "total_file_bytes"):
        if (
            isinstance(summary.get(field), bool)
            or not isinstance(summary.get(field), int)
            or summary[field] < 0
        ):
            raise SnapshotError("snapshot manifest count is invalid")
    fsck = document.get("fsck")
    if (
        not isinstance(fsck, dict)
        or fsck.get("schema_version") != 1
        or fsck.get("policy") != "git-fsck-strict-full-no-reflogs-v1"
        or fsck.get("exit_code") != 0
    ):
        raise SnapshotError("snapshot fsck authority is invalid")
    for field in ("stdout_sha256", "stderr_sha256"):
        _require_digest(fsck.get(field), f"snapshot fsck {field}")
    delivery = validate_delivery_gate(
        document.get("delivery_gate"),
        target_sha=document["target_source_sha"],
    )
    if document.get("delivery_gate_sha256") != canonical_digest(delivery):
        raise SnapshotError("snapshot delivery gate digest differs")
    completed = document.get("completed_at")
    if not isinstance(completed, str) or UTC_RE.fullmatch(completed) is None:
        raise SnapshotError("snapshot completion timestamp is invalid")
    return dict(document)


def validate_journal(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "phase",
        "operation_id",
        "plan",
        "plan_sha256",
        "snapshot_impact_sha256",
        "created_at",
        "completed_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("phase")
        not in {"intent", "copy-intent", "authority-commit-intent", "completed"}
        or not isinstance(document.get("plan"), dict)
    ):
        raise SnapshotError("production Git snapshot journal has an invalid shape")
    _require_operation_id(document.get("operation_id"))
    plan = document["plan"]
    plan_digest = _require_digest(
        document.get("plan_sha256"),
        "snapshot journal plan",
    )
    impact_digest = _require_digest(
        document.get("snapshot_impact_sha256"),
        "snapshot journal impact",
    )
    if (
        plan_digest != canonical_digest(plan)
        or plan.get("operation_id") != document["operation_id"]
        or plan.get("snapshot_impact_sha256") != impact_digest
    ):
        raise SnapshotError("production Git snapshot journal plan differs")
    created_at = document.get("created_at")
    completed_at = document.get("completed_at")
    if not isinstance(created_at, str) or UTC_RE.fullmatch(created_at) is None:
        raise SnapshotError("snapshot journal creation timestamp is invalid")
    if document["phase"] == "completed":
        if (
            document.get("status") != "completed"
            or not isinstance(completed_at, str)
            or UTC_RE.fullmatch(completed_at) is None
            or completed_at < created_at
        ):
            raise SnapshotError("completed snapshot journal is invalid")
    elif document.get("status") != "in-progress" or completed_at is not None:
        raise SnapshotError("nonterminal snapshot journal is invalid")
    return dict(document)


def verify_completed_snapshot(
    runtime_root: Path = RUNTIME_ROOT,
    *,
    production_root: Path | None = PRODUCTION_ROOT,
    full: bool = True,
) -> tuple[dict[str, Any], str]:
    authority_path = runtime_root / AUTHORITY_RELATIVE_PATH
    authority, raw_digest = _load_private_json(authority_path)
    normalized = validate_authority(
        authority,
        runtime_root=runtime_root,
        production_root=production_root,
    )
    manifest_path = Path(normalized["manifest_path"])
    backup_git_dir = Path(normalized["backup_git_dir"])
    manifest, manifest_raw_digest = _load_private_json(manifest_path)
    validated_manifest = validate_manifest(manifest)
    summary = {
        "records_sha256": validated_manifest["records_sha256"],
        "file_count": validated_manifest["file_count"],
        "directory_count": validated_manifest["directory_count"],
        "total_file_bytes": validated_manifest["total_file_bytes"],
    }
    if (
        manifest_raw_digest != normalized["manifest_sha256"]
        or summary != normalized["manifest_summary"]
    ):
        raise SnapshotError("durable snapshot manifest differs from authority")
    if full:
        first = scan_git_directory(backup_git_dir)
        fsck = strict_fsck(backup_git_dir)
        second = scan_git_directory(backup_git_dir)
        if first != validated_manifest or second != first:
            raise SnapshotError("durable production Git snapshot changed")
        if fsck != normalized["fsck"]:
            raise SnapshotError("durable production Git fsck evidence changed")
    return normalized, raw_digest


class ProductionGitSnapshotManager:
    def __init__(
        self,
        source_root: Path,
        production_root: Path,
        runtime_root: Path,
        *,
        allow_test: bool = False,
        delivery_gate_probe: Callable[[str], Mapping[str, object]] | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.source_root = source_root.absolute()
        self.production_root = production_root.absolute()
        self.runtime_root = runtime_root.absolute()
        self.allow_test = allow_test
        self.delivery_gate_probe = delivery_gate_probe
        self.checkpoint = checkpoint or (lambda _name: None)
        self.git_dir = self.production_root / ".git"
        self.state_root = self.runtime_root / "state"
        self.authority_path = self.runtime_root / AUTHORITY_RELATIVE_PATH
        self.journal_root = self.runtime_root / JOURNAL_DIRECTORY_RELATIVE_PATH
        self.backup_root = self.runtime_root / BACKUP_ROOT_RELATIVE_PATH
        self.lock_path = self.runtime_root / LOCK_RELATIVE_PATH
        if not allow_test and (
            self.source_root == self.production_root
            or self.production_root != PRODUCTION_ROOT
            or self.runtime_root != RUNTIME_ROOT
        ):
            raise SnapshotError("production snapshot requires fixed isolated paths")

    def _source_identity(self, target_sha: str) -> tuple[str, dict[str, object]]:
        target_sha = _require_sha(target_sha, "snapshot target SHA")
        head = _run_git(self.source_root, "rev-parse", "HEAD").stdout.strip()
        if head != target_sha:
            raise SnapshotError("snapshot target is not the private clone HEAD")
        status = _run_git(
            self.source_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        branch = _run_git(
            self.source_root,
            "symbolic-ref",
            "-q",
            "HEAD",
        ).stdout.strip()
        if status or branch != "refs/heads/main":
            raise SnapshotError("snapshot target clone is not clean local main")
        target_tree = _require_sha(
            _run_git(self.source_root, "rev-parse", f"{target_sha}^{{tree}}").stdout.strip(),
            "snapshot target tree",
        )
        if self.delivery_gate_probe is not None:
            delivery = dict(self.delivery_gate_probe(target_sha))
        elif self.allow_test:
            delivery = {
                "remote_main": target_sha,
                "ci": {
                    "workflow_run_id": 1,
                    "run_attempt": 1,
                    "head_sha": target_sha,
                    "head_branch": "main",
                    "event": "push",
                    "path": ".github/workflows/ci.yml",
                    "conclusion": "success",
                    "required_jobs": ["test-delivery"],
                },
            }
        else:
            bootstrap_path = self.source_root / "scripts/bootstrap_pull_deploy.py"
            specification = importlib.util.spec_from_file_location(
                "nexpoly_snapshot_bootstrap",
                bootstrap_path,
            )
            if specification is None or specification.loader is None:
                raise SnapshotError("snapshot bootstrap verifier is unavailable")
            module = importlib.util.module_from_spec(specification)
            try:
                specification.loader.exec_module(module)
                delivery = dict(
                    module._delivery_gate(
                        self.production_root,
                        self.runtime_root,
                        target_sha,
                        allow_test=False,
                    )
                )
            except BaseException as exc:
                raise SnapshotError("snapshot delivery gate failed") from exc
        return target_tree, validate_delivery_gate(
            delivery,
            target_sha=target_sha,
        )

    def _production_identity(self) -> tuple[str, str]:
        status = _run_git(
            self.production_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        if status:
            raise SnapshotError("production checkout is not clean")
        head = _require_sha(
            _run_git(self.production_root, "rev-parse", "HEAD").stdout.strip(),
            "production snapshot HEAD",
        )
        tree = _require_sha(
            _run_git(self.production_root, "rev-parse", "HEAD^{tree}").stdout.strip(),
            "production snapshot tree",
        )
        branch = _run_git(
            self.production_root,
            "symbolic-ref",
            "-q",
            "HEAD",
        ).stdout.strip()
        if branch != "refs/heads/main":
            raise SnapshotError("production snapshot is not on local main")
        return head, tree

    @staticmethod
    def _manifest_summary(manifest: Mapping[str, object]) -> dict[str, object]:
        return {
            "records_sha256": manifest["records_sha256"],
            "file_count": manifest["file_count"],
            "directory_count": manifest["directory_count"],
            "total_file_bytes": manifest["total_file_bytes"],
        }

    def _build_plan(self, target_sha: str, operation_id: str) -> tuple[dict[str, Any], dict[str, object]]:
        operation_id = _require_operation_id(operation_id)
        target_tree, delivery = self._source_identity(target_sha)
        production_sha, production_tree = self._production_identity()
        first = scan_git_directory(self.git_dir)
        fsck = strict_fsck(self.git_dir)
        second = scan_git_directory(self.git_dir)
        if first != second:
            raise SnapshotError("production Git directory changed during snapshot plan")
        operation_root = self.backup_root / operation_id
        manifest_path = operation_root / "MANIFEST.json"
        backup_git_dir = operation_root / "git"
        snapshot_staging_dir = operation_root / ".git.copy"
        impact = {
            "schema_version": 1,
            "policy": "nexpoly-production-git-snapshot-impact-v1",
            "operation_id": operation_id,
            "source": {"sha": production_sha, "tree": production_tree},
            "target": {"sha": target_sha, "tree": target_tree},
            "source_git_dir": str(self.git_dir),
            "backup_git_dir": str(backup_git_dir),
            "snapshot_staging_dir": str(snapshot_staging_dir),
            "manifest_path": str(manifest_path),
            "manifest_sha256": canonical_digest(first),
            "manifest_summary": self._manifest_summary(first),
            "copy_policy": COPY_POLICY,
            "mutations": {
                "production_source": False,
                "production_git": False,
                "services": False,
                "database": False,
                "containers": False,
                "runtime_snapshot_authority": True,
            },
        }
        plan = {
            "schema_version": 1,
            "authority_kind": AUTHORITY_KIND,
            "policy": POLICY,
            "operation_id": operation_id,
            "target_source_sha": target_sha,
            "target_source_tree": target_tree,
            "production_source_sha": production_sha,
            "production_source_tree": production_tree,
            "production_git_dir": str(self.git_dir),
            "backup_git_dir": str(backup_git_dir),
            "snapshot_staging_dir": str(snapshot_staging_dir),
            "manifest_path": str(manifest_path),
            "manifest_sha256": canonical_digest(first),
            "manifest_summary": self._manifest_summary(first),
            "fsck": fsck,
            "delivery_gate": delivery,
            "delivery_gate_sha256": canonical_digest(delivery),
            "copy_policy": COPY_POLICY,
            "publication_policy": PUBLICATION_POLICY,
            "snapshot_impact": impact,
            "snapshot_impact_sha256": canonical_digest(impact),
        }
        return plan, first

    def plan(self, *, target_sha: str, operation_id: str) -> dict[str, object]:
        operation_id = _require_operation_id(operation_id)
        self._assert_initial_namespace_absent(operation_id)
        plan, _manifest = self._build_plan(target_sha, operation_id)
        self._assert_initial_namespace_absent(operation_id)
        return {
            "action": "production-git-snapshot-plan",
            "apply": False,
            "logical_zero_write": True,
            "atime_zero_write": False,
            "plan": plan,
            "plan_sha256": canonical_digest(plan),
            "snapshot_impact_sha256": plan["snapshot_impact_sha256"],
        }

    def _journal_path(self, operation_id: str) -> Path:
        return self.journal_root / f"{operation_id}.json"

    def _authority_staging_path(self, operation_id: str) -> Path:
        return self.state_root / (
            f".production-git-snapshot.json.create-{operation_id}"
        )

    def _assert_initial_namespace_absent(self, operation_id: str) -> None:
        paths = (
            self.authority_path,
            self._authority_staging_path(operation_id),
            self._journal_path(operation_id),
            self.backup_root / operation_id,
        )
        if any(path.exists() or path.is_symlink() for path in paths):
            raise SnapshotError("production Git snapshot namespace is occupied")
        if self.journal_root.exists() or self.journal_root.is_symlink():
            _require_private_directory(self.journal_root)
            if any(self.journal_root.iterdir()):
                raise SnapshotError("foreign snapshot transaction exists")
        if self.backup_root.exists() or self.backup_root.is_symlink():
            _require_private_directory(self.backup_root)
            if any(self.backup_root.iterdir()):
                raise SnapshotError("foreign production Git snapshot exists")

    def _authority(self, plan: Mapping[str, Any], completed_at: str) -> dict[str, Any]:
        return validate_authority(
            {
                "schema_version": 1,
                "status": "completed",
                "authority_kind": AUTHORITY_KIND,
                "policy": POLICY,
                "operation_id": plan["operation_id"],
                "target_source_sha": plan["target_source_sha"],
                "target_source_tree": plan["target_source_tree"],
                "production_source_sha": plan["production_source_sha"],
                "production_source_tree": plan["production_source_tree"],
                "production_git_dir": plan["production_git_dir"],
                "backup_git_dir": plan["backup_git_dir"],
                "manifest_path": plan["manifest_path"],
                "manifest_sha256": plan["manifest_sha256"],
                "manifest_summary": plan["manifest_summary"],
                "fsck": plan["fsck"],
                "delivery_gate": plan["delivery_gate"],
                "delivery_gate_sha256": plan["delivery_gate_sha256"],
                "plan_sha256": canonical_digest(plan),
                "snapshot_impact_sha256": plan["snapshot_impact_sha256"],
                "copy_policy": COPY_POLICY,
                "completed_at": completed_at,
            },
            runtime_root=self.runtime_root,
            production_root=self.production_root,
        )

    def _publish_authority(self, authority: Mapping[str, object], operation_id: str) -> None:
        payload = canonical_json_bytes(authority)
        staging = self._authority_staging_path(operation_id)
        if self.authority_path.exists() or self.authority_path.is_symlink():
            existing = self._recover_authority_publication(operation_id)
            if existing != authority:
                raise SnapshotError("published snapshot authority differs")
            return
        if staging.exists() or staging.is_symlink():
            existing, _digest = _load_private_json(staging)
            if existing != authority:
                raise SnapshotError("snapshot authority staging differs")
        else:
            descriptor = os.open(
                staging,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                _write_all(descriptor, payload)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        try:
            os.link(staging, self.authority_path, follow_symlinks=False)
        except FileExistsError:
            existing = self._recover_authority_publication(operation_id)
            if existing != authority:
                raise SnapshotError("snapshot authority publication raced")
        parent_fd = _open_directory(self.state_root)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        try:
            staging.unlink()
        except FileNotFoundError:
            pass

    def _recover_authority_publication(self, operation_id: str) -> dict[str, Any]:
        staging = self._authority_staging_path(operation_id)
        try:
            final_metadata = self.authority_path.lstat()
        except OSError as exc:
            raise SnapshotError("published snapshot authority is unavailable") from exc
        if (
            not stat.S_ISREG(final_metadata.st_mode)
            or self.authority_path.is_symlink()
            or final_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(final_metadata.st_mode) != 0o600
            or final_metadata.st_nlink not in {1, 2}
        ):
            raise SnapshotError("published snapshot authority identity is unsafe")
        if final_metadata.st_nlink == 2:
            try:
                staging_metadata = staging.lstat()
            except OSError as exc:
                raise SnapshotError(
                    "linked snapshot authority lacks its exact staging name"
                ) from exc
            if (
                staging.is_symlink()
                or not stat.S_ISREG(staging_metadata.st_mode)
                or (staging_metadata.st_dev, staging_metadata.st_ino)
                != (final_metadata.st_dev, final_metadata.st_ino)
                or staging_metadata.st_nlink != 2
            ):
                raise SnapshotError("snapshot authority hard links differ")
            document, _digest = _load_private_json(
                self.authority_path,
                allowed_nlinks=frozenset({2}),
            )
            staging.unlink()
            parent_fd = _open_directory(self.state_root)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return document
        if staging.exists() or staging.is_symlink():
            raise SnapshotError("completed snapshot authority has staging residue")
        document, _digest = _load_private_json(self.authority_path)
        return document

    def apply(
        self,
        *,
        target_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_snapshot_impact_sha256: str,
    ) -> dict[str, object]:
        operation_id = _require_operation_id(operation_id)
        _require_digest(confirm_plan_sha256, "confirmed snapshot plan")
        _require_digest(confirm_snapshot_impact_sha256, "confirmed snapshot impact")
        _require_private_directory(self.runtime_root)
        _require_private_directory(self.state_root)
        try:
            lock_fd = os.open(
                self.lock_path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise SnapshotError("snapshot transaction lock is unavailable") from exc
        try:
            if self.authority_path.exists() or self.authority_path.is_symlink():
                recovered_authority = self._recover_authority_publication(
                    operation_id
                )
                authority, _digest = verify_completed_snapshot(
                    self.runtime_root,
                    production_root=self.production_root,
                    full=True,
                )
                if (
                    authority["operation_id"] != operation_id
                    or authority["target_source_sha"] != target_sha
                    or authority["plan_sha256"] != confirm_plan_sha256
                    or authority["snapshot_impact_sha256"]
                    != confirm_snapshot_impact_sha256
                ):
                    raise SnapshotError("completed snapshot authority differs")
                if recovered_authority != authority:
                    raise SnapshotError("recovered snapshot authority differs")
                journal_path = self._journal_path(operation_id)
                journal_raw, _journal_digest = _load_private_json(journal_path)
                journal = validate_journal(journal_raw)
                if (
                    journal.get("operation_id") != operation_id
                    or journal.get("plan_sha256") != confirm_plan_sha256
                    or journal.get("snapshot_impact_sha256")
                    != confirm_snapshot_impact_sha256
                    or journal.get("plan", {}).get("target_source_sha")
                    != target_sha
                ):
                    raise SnapshotError("completed snapshot journal differs")
                if journal.get("phase") != "completed":
                    if journal.get("phase") != "authority-commit-intent":
                        raise SnapshotError(
                            "snapshot authority exists before durable commit intent"
                        )
                    journal.update(
                        {
                            "status": "completed",
                            "phase": "completed",
                            "completed_at": authority["completed_at"],
                        }
                    )
                    _atomic_private_json(journal_path, journal)
                    self.checkpoint("snapshot-completed")
                elif (
                    journal.get("status") != "completed"
                    or journal.get("completed_at") != authority["completed_at"]
                ):
                    raise SnapshotError("completed snapshot journal is inconsistent")
                return authority
            journal_path = self._journal_path(operation_id)
            recovering = journal_path.exists() or journal_path.is_symlink()
            if not recovering:
                self._assert_initial_namespace_absent(operation_id)
            plan, manifest = self._build_plan(target_sha, operation_id)
            plan_digest = canonical_digest(plan)
            if (
                plan_digest != confirm_plan_sha256
                or plan["snapshot_impact_sha256"]
                != confirm_snapshot_impact_sha256
            ):
                raise SnapshotError("snapshot confirmations differ from live plan")
            _ensure_private_directory(self.journal_root)
            _ensure_private_directory(self.backup_root.parent)
            _ensure_private_directory(self.backup_root)
            operation_root = self.backup_root / operation_id
            manifest_path = Path(plan["manifest_path"])
            backup_git_dir = Path(plan["backup_git_dir"])
            snapshot_staging_dir = Path(plan["snapshot_staging_dir"])
            created_at = _now_utc()
            journal = {
                "schema_version": 1,
                "status": "in-progress",
                "phase": "intent",
                "operation_id": operation_id,
                "plan": plan,
                "plan_sha256": plan_digest,
                "snapshot_impact_sha256": plan["snapshot_impact_sha256"],
                "created_at": created_at,
                "completed_at": None,
            }
            if journal_path.exists() or journal_path.is_symlink():
                existing_raw, _digest = _load_private_json(journal_path)
                existing = validate_journal(existing_raw)
                if (
                    existing.get("operation_id") != operation_id
                    or existing.get("plan") != plan
                    or existing.get("plan_sha256") != plan_digest
                    or existing.get("snapshot_impact_sha256")
                    != plan["snapshot_impact_sha256"]
                ):
                    raise SnapshotError("snapshot recovery journal differs")
                journal = existing
                created_at = str(journal["created_at"])
            else:
                _atomic_private_json(journal_path, journal)
                self.checkpoint("snapshot-intent")
            if operation_root.exists() or operation_root.is_symlink():
                _require_private_directory(operation_root)
            else:
                operation_root.mkdir(mode=0o700)
                _require_private_directory(operation_root)
                self.checkpoint("snapshot-operation-root-ready")
            if manifest_path.exists() or manifest_path.is_symlink():
                durable_manifest, durable_digest = _load_private_json(manifest_path)
                if durable_manifest != manifest or durable_digest != plan["manifest_sha256"]:
                    raise SnapshotError("durable snapshot manifest differs")
            else:
                _atomic_private_json(manifest_path, manifest)
                self.checkpoint("snapshot-manifest-written")
            if backup_git_dir.exists() or backup_git_dir.is_symlink():
                if scan_git_directory(backup_git_dir) != manifest:
                    raise SnapshotError("partial snapshot copy differs")
                if snapshot_staging_dir.exists() or snapshot_staging_dir.is_symlink():
                    raise SnapshotError("completed snapshot has copy staging residue")
            else:
                journal["phase"] = "copy-intent"
                _atomic_private_json(journal_path, journal)
                self.checkpoint("snapshot-copy-intent")
                if snapshot_staging_dir.exists() or snapshot_staging_dir.is_symlink():
                    try:
                        staged_manifest = scan_git_directory(snapshot_staging_dir)
                    except SnapshotError:
                        staged_manifest = None
                    if staged_manifest != manifest:
                        _remove_private_tree(snapshot_staging_dir)
                        self.checkpoint("snapshot-partial-copy-removed")
                if not snapshot_staging_dir.exists():
                    copy_git_directory(
                        self.git_dir,
                        snapshot_staging_dir,
                        manifest,
                    )
                if scan_git_directory(snapshot_staging_dir) != manifest:
                    raise SnapshotError("snapshot copy staging differs")
                self.checkpoint("snapshot-copy-complete")
                try:
                    os.rename(snapshot_staging_dir, backup_git_dir)
                except OSError as exc:
                    raise SnapshotError("snapshot copy cannot be published") from exc
                operation_fd = _open_directory(operation_root)
                try:
                    os.fsync(operation_fd)
                finally:
                    os.close(operation_fd)
                self.checkpoint("snapshot-copy-published")
            snapshot_fsck = strict_fsck(backup_git_dir)
            after = scan_git_directory(self.git_dir)
            if after != manifest or snapshot_fsck != plan["fsck"]:
                raise SnapshotError("snapshot copy or production source changed")
            journal["phase"] = "authority-commit-intent"
            _atomic_private_json(journal_path, journal)
            self.checkpoint("snapshot-authority-commit-intent")
            completed_at = _now_utc()
            authority = self._authority(plan, completed_at)
            self._publish_authority(authority, operation_id)
            self.checkpoint("snapshot-authority-published")
            journal.update(
                {
                    "status": "completed",
                    "phase": "completed",
                    "completed_at": completed_at,
                }
            )
            _atomic_private_json(journal_path, journal)
            verified, _digest = verify_completed_snapshot(
                self.runtime_root,
                production_root=self.production_root,
                full=True,
            )
            if verified != authority:
                raise SnapshotError("completed snapshot verification differs")
            self.checkpoint("snapshot-completed")
            return authority
        finally:
            os.close(lock_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply", "verify"):
        child = subparsers.add_parser(command)
        if command != "verify":
            child.add_argument("--sha", required=True)
            child.add_argument("--operation-id", required=True)
        if command == "apply":
            child.add_argument("--confirm-plan-sha256", required=True)
            child.add_argument(
                "--confirm-snapshot-impact-sha256",
                required=True,
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "verify":
            authority, authority_digest = verify_completed_snapshot(full=True)
            result: object = {
                "action": "production-git-snapshot-verify",
                "verified": True,
                "authority": authority,
                "authority_sha256": authority_digest,
            }
        else:
            manager = ProductionGitSnapshotManager(
                Path.cwd(),
                PRODUCTION_ROOT,
                RUNTIME_ROOT,
            )
            if arguments.command == "plan":
                result = manager.plan(
                    target_sha=arguments.sha,
                    operation_id=arguments.operation_id,
                )
            else:
                result = manager.apply(
                    target_sha=arguments.sha,
                    operation_id=arguments.operation_id,
                    confirm_plan_sha256=arguments.confirm_plan_sha256,
                    confirm_snapshot_impact_sha256=(
                        arguments.confirm_snapshot_impact_sha256
                    ),
                )
        sys.stdout.buffer.write(canonical_json_bytes(result))
        return 0
    except SnapshotError as exc:
        print(f"production Git snapshot failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
