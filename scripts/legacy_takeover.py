#!/usr/bin/python3 -I -B
"""Crash-safe, exact-identity takeover of the legacy production checkout.

This controller is deliberately independent from the normal deploy controller.
It moves only paths named by a private, digest-pinned classification document,
stops only the sealed legacy containers and Worker unit, and changes the Git
origin only with compare-and-swap semantics.  Every externally visible action
has a durable intent so a lost response can be resumed safely.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, NoReturn


def _load_site_helper_contracts() -> Any:
    try:
        import site_helper_contracts  # type: ignore

        return site_helper_contracts
    except ModuleNotFoundError:
        path = Path(__file__).with_name("site_helper_contracts.py")
        spec = importlib.util.spec_from_file_location(
            "legacy_takeover_site_helper_contracts",
            path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("site-helper contracts cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


SITE_HELPERS = _load_site_helper_contracts()

PRODUCTION_REPOSITORY = Path("/data/lzq/gith/nexpoly")
PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
REPOSITORY_HTTPS_URL = "https://github.com/lzq390/ZhijuPoly.git"
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
CLASSIFICATION_RELATIVE_PATH = Path("config/legacy-takeover-classification.json")
STATE_DIRECTORY_RELATIVE_PATH = Path("state/legacy-takeover")
EXTERNAL_ROOTS = {
    "runtime": Path("/data/lzq/gith/nexpoly-runtime/legacy-takeover/runtime"),
    "secret": Path(
        "/data/lzq/gith/nexpoly-runtime/private/legacy-takeover/secrets"
    ),
    "asset": Path("/data/lzq/nexpoly-assets/legacy-takeover"),
}

SCHEMA_VERSION = 1
MAX_CLASSIFICATION_BYTES = 128 * 1024
MAX_STATE_BYTES = 64 * 1024 * 1024
OPERATION_RE = re.compile(r"^takeover-[a-z0-9][a-z0-9-]{7,79}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REVIEW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,159}$")
ZERO_ACTIVE_JOB_FIELDS = frozenset(SITE_HELPERS.ACTIVE_JOB_FIELDS_V2)
MOVE_STATUSES = {
    "pending",
    "copy-intent",
    "destination-ready",
    "source-remove-intent",
    "externalized",
}
RESTORE_MOVE_STATUSES = {
    "pending",
    "copy-intent",
    "source-ready",
    "destination-remove-intent",
    "restored",
}
CONTROL_LAYOUT_RELATIVE_PATHS = (
    "bin",
    "config/docker",
    "control-releases",
    "state/prepared",
    "state/control-handoffs",
    "state/worker-slots",
    "state/contract-operations",
    "state/contract-verification-databases",
    "state/maintenance",
    "state/monomer-md-worker-socket",
    "state/monomer-md-worker-runs",
    "state/gpu-resource",
    "state/active-control.json",
    "state/bootstrap-control.json",
    "audit",
    "backups",
    "wheel-cache",
    "worker-venvs",
)
CONTROL_LAYOUT_RESTORE_STATUSES = {
    "pending",
    "remove-intent",
    "removed",
    "copy-intent",
    "restored",
}
PRESERVED_CONTROL_LAYOUT_PATHS = {"audit", "backups"}
CHECKOUT_PERMISSION_RESTORE_STATUSES = {
    "pending",
    "restore-intent",
    "restored",
}
APPLY_PHASES = {
    "sealed",
    "drain-intent",
    "drained",
    "web-stop-intent",
    "web-stopped",
    "backend-stop-intent",
    "backend-stopped",
    "worker-stop-intent",
    "runtime-stopped",
    "externalizing",
    "externalized",
    "origin-switch-intent",
    "complete",
}
RESTORE_PHASES = {
    "origin-restore-intent",
    "origin-restored",
    "checkout-permissions-restore-intent",
    "checkout-permissions-restored",
    "files-restoring",
    "files-restored",
    "control-layout-restore-intent",
    "control-layout-restored",
    "worker-unit-restore-intent",
    "worker-unit-restored",
    "runtime-restore-intent",
    "restored",
}


class LegacyTakeoverError(RuntimeError):
    """The requested takeover cannot be proven safe."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        before = path.stat(follow_symlinks=False)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LegacyTakeoverError(f"cannot hash sealed file: {path}") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise LegacyTakeoverError(f"sealed file changed while hashing: {path}")
    return "sha256:" + digest.hexdigest()


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LegacyTakeoverError(f"duplicate JSON key is forbidden: {key}")
        value[key] = item
    return value


def _load_json_file(path: Path, maximum: int) -> Any:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise LegacyTakeoverError(f"cannot read JSON file: {path}") from exc
    if len(payload) > maximum:
        raise LegacyTakeoverError(f"JSON file is too large: {path}")
    try:
        return json.loads(payload, object_pairs_hook=_json_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyTakeoverError(f"invalid JSON file: {path}") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path, *, create: bool = True) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LegacyTakeoverError(f"private directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LegacyTakeoverError(f"private directory is unsafe: {path}")


def _ensure_private_descendant(path: Path, anchor: Path) -> None:
    anchor = anchor.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise LegacyTakeoverError(
            f"private path escapes its fixed root: {path}"
        ) from exc
    _ensure_private_directory(anchor)
    current = anchor
    for component in relative.parts:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _ensure_private_directory(current, create=False)


def _validate_inherited_parent_lock(descriptor: int, path: Path) -> None:
    if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 3:
        raise LegacyTakeoverError("parent deploy lock FD is invalid")
    try:
        fd_metadata = os.fstat(descriptor)
        path_metadata = path.lstat()
    except OSError as exc:
        raise LegacyTakeoverError(
            "parent deploy lock FD is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(fd_metadata.st_mode)
        or not stat.S_ISREG(path_metadata.st_mode)
        or path.is_symlink()
        or (fd_metadata.st_dev, fd_metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
        or fd_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(fd_metadata.st_mode) != 0o600
    ):
        raise LegacyTakeoverError(
            "parent deploy lock FD does not identify the fixed lock"
        )
    expected_device = (
        os.major(fd_metadata.st_dev),
        os.minor(fd_metadata.st_dev),
        fd_metadata.st_ino,
    )
    parent_pid = os.getppid()
    try:
        lock_lines = Path("/proc/locks").read_text(
            encoding="ascii"
        ).splitlines()
    except OSError as exc:
        raise LegacyTakeoverError(
            "cannot authenticate inherited parent deploy lock"
        ) from exc
    parent_holds_lock = False
    for line in lock_lines:
        fields = line.split()
        if (
            len(fields) < 8
            or fields[1] != "FLOCK"
            or fields[3] != "WRITE"
        ):
            continue
        try:
            major_text, minor_text, inode_text = fields[5].split(":", 2)
            identity = (
                int(major_text, 16),
                int(minor_text, 16),
                int(inode_text),
            )
            owner_pid = int(fields[4])
        except (ValueError, IndexError):
            continue
        if identity == expected_device and owner_pid == parent_pid:
            parent_holds_lock = True
            break
    if not parent_holds_lock:
        raise LegacyTakeoverError(
            "direct parent does not hold the inherited deploy lock"
        )
    try:
        # On the inherited open-file description this is idempotent. A second
        # FD for the same inode conflicts with the parent's lock and is refused.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise LegacyTakeoverError(
            "parent deploy lock FD is not the locked inherited description"
        ) from exc


def _validate_private_file(path: Path, *, expected_mode: int = 0o600) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LegacyTakeoverError(f"private file is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        raise LegacyTakeoverError(f"private file is unsafe: {path}")


def _atomic_json(path: Path, value: object) -> None:
    _ensure_private_directory(path.parent)
    payload = canonical_json_bytes(value) + b"\n"
    prefix = f".{path.name}.tmp-"
    _cleanup_stale_temps(path.parent, prefix)
    temporary = path.parent / f"{prefix}{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _validate_private_file(path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise LegacyTakeoverError(f"cannot persist takeover state: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _cleanup_stale_temps(parent: Path, prefix: str) -> None:
    changed = False
    for candidate in parent.glob(f"{prefix}*"):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or candidate.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise LegacyTakeoverError(
                f"unsafe stale takeover temp exists: {candidate}"
            )
        candidate.unlink()
        changed = True
    if changed:
        _fsync_directory(parent)


def _create_json_exclusive(path: Path, value: object) -> None:
    _ensure_private_directory(path.parent)
    payload = canonical_json_bytes(value) + b"\n"
    prefix = f".{path.name}.create-"
    _cleanup_stale_temps(path.parent, prefix)
    temporary = path.parent / f"{prefix}{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_private_file(temporary)
        _rename_noreplace(temporary, path)
        _validate_private_file(path)
    except OSError as exc:
        raise LegacyTakeoverError(
            f"cannot create exclusive takeover record: {path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _path_kind(metadata: os.stat_result) -> str:
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    raise LegacyTakeoverError("special files cannot be externalized")


def _seal_entry(root: Path, path: Path, relative: str) -> list[dict[str, Any]]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LegacyTakeoverError(f"cannot seal legacy path: {path}") from exc
    kind = _path_kind(metadata)
    record: dict[str, Any] = {
        "path": relative,
        "type": kind,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }
    if kind == "file":
        record["size"] = metadata.st_size
        record["sha256"] = sha256_file(path)
    elif kind == "symlink":
        try:
            record["target"] = os.readlink(path)
        except OSError as exc:
            raise LegacyTakeoverError(f"cannot read sealed symlink: {path}") from exc
    records = [record]
    if kind == "directory":
        try:
            children = sorted(
                (child.name for child in os.scandir(path)),
                key=os.fsencode,
            )
        except OSError as exc:
            raise LegacyTakeoverError(f"cannot enumerate sealed path: {path}") from exc
        for name in children:
            child_relative = name if relative == "." else f"{relative}/{name}"
            records.extend(_seal_entry(root, path / name, child_relative))
    return records


def seal_path(path: Path) -> dict[str, Any]:
    records = _seal_entry(path, path, ".")
    seal = {
        "schema_version": 1,
        "records": records,
    }
    seal["digest"] = sha256_bytes(canonical_json_bytes(seal))
    return seal


def validate_seal_document(expected: object) -> dict[str, Any]:
    if not isinstance(expected, dict) or set(expected) != {
        "schema_version",
        "records",
        "digest",
    }:
        raise LegacyTakeoverError("stored path seal is malformed")
    if expected.get("schema_version") != 1:
        raise LegacyTakeoverError("stored path seal schema is unsupported")
    digest = expected.get("digest")
    unsigned = {
        "schema_version": expected["schema_version"],
        "records": expected["records"],
    }
    if (
        not isinstance(digest, str)
        or DIGEST_RE.fullmatch(digest) is None
        or sha256_bytes(canonical_json_bytes(unsigned)) != digest
    ):
        raise LegacyTakeoverError("stored path seal digest is inconsistent")
    records = expected.get("records")
    if not isinstance(records, list) or not records:
        raise LegacyTakeoverError("stored path seal inventory is empty")
    prior_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise LegacyTakeoverError("stored path seal record is invalid")
        kind = record.get("type")
        common = {"path", "type", "mode", "uid", "gid"}
        expected_fields = {
            "file": common | {"size", "sha256"},
            "directory": common,
            "symlink": common | {"target"},
        }.get(kind)
        if (
            expected_fields is None
            or set(record) != expected_fields
            or not isinstance(record.get("path"), str)
            or record["path"] in prior_paths
            or not isinstance(record.get("mode"), str)
            or re.fullmatch(r"[0-7]{4}", record["mode"]) is None
            or isinstance(record.get("uid"), bool)
            or not isinstance(record.get("uid"), int)
            or record["uid"] < 0
            or isinstance(record.get("gid"), bool)
            or not isinstance(record.get("gid"), int)
            or record["gid"] < 0
        ):
            raise LegacyTakeoverError("stored path seal record is invalid")
        if kind == "file" and (
            isinstance(record.get("size"), bool)
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
            or not isinstance(record.get("sha256"), str)
            or DIGEST_RE.fullmatch(record["sha256"]) is None
        ):
            raise LegacyTakeoverError("stored file seal is invalid")
        if kind == "symlink" and not isinstance(record.get("target"), str):
            raise LegacyTakeoverError("stored symlink seal is invalid")
        prior_paths.add(record["path"])
    if records[0].get("path") != ".":
        raise LegacyTakeoverError("stored path seal has no root")
    return expected


def verify_path_seal(path: Path, expected: object) -> None:
    expected = validate_seal_document(expected)
    actual = seal_path(path)
    if actual != expected:
        raise LegacyTakeoverError(f"legacy path no longer matches its seal: {path}")


def verify_path_seal_subset(path: Path, expected: object) -> None:
    """Allow only an unmodified remainder of a sealed recursive deletion.

    A process can die after unlinking some children but before removing the
    deterministic trash root. Missing records are therefore safe to resume;
    an added or modified surviving record is not.
    """

    expected = validate_seal_document(expected)
    expected_records = {
        record["path"]: record for record in expected["records"]
    }
    actual = seal_path(path)
    for record in actual["records"]:
        if expected_records.get(record["path"]) != record:
            raise LegacyTakeoverError(
                f"takeover trash is not a sealed subset: {path}"
            )


def _permission_record(root: Path, path: Path, relative: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LegacyTakeoverError(
            f"checkout permission path is unavailable: {path}"
        ) from exc
    return {
        "path": relative,
        "type": _path_kind(metadata),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def _permission_path_excluded(relative: str, excluded: tuple[str, ...]) -> bool:
    if relative == ".":
        return False
    path = PurePosixPath(relative)
    return any(
        path == PurePosixPath(value)
        or PurePosixPath(value) in path.parents
        for value in excluded
    )


def _normal_checkout_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise LegacyTakeoverError("checkout permission path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LegacyTakeoverError("checkout permission path escapes repository")
    return value


def snapshot_checkout_permissions(
    repository: Path,
    *,
    excluded_paths: list[str] | tuple[str, ...] = (),
    expected_paths: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    repository = repository.absolute()
    excluded = tuple(
        sorted((_normal_relative_path(value) for value in excluded_paths))
    )
    if expected_paths is not None:
        records: list[dict[str, Any]] = []
        for relative in expected_paths:
            if relative != ".":
                _normal_checkout_relative_path(relative)
            records.append(
                _permission_record(
                    repository,
                    repository if relative == "." else repository / relative,
                    relative,
                )
            )
        return records

    records = [_permission_record(repository, repository, ".")]

    def walk(directory: Path, prefix: str) -> None:
        try:
            children = sorted(
                os.scandir(directory),
                key=lambda item: os.fsencode(item.name),
            )
        except OSError as exc:
            raise LegacyTakeoverError(
                f"cannot inventory checkout permissions: {directory}"
            ) from exc
        for child in children:
            relative = child.name if prefix == "." else f"{prefix}/{child.name}"
            if _permission_path_excluded(relative, excluded):
                continue
            path = directory / child.name
            record = _permission_record(repository, path, relative)
            records.append(record)
            if record["type"] == "directory":
                walk(path, relative)

    walk(repository, ".")
    return records


def checkout_permissions_digest(records: list[dict[str, Any]]) -> str:
    identity = {
        "schema_version": 1,
        "records": [
            {
                "path": record["path"],
                "type": record["type"],
                "mode": record["mode"],
                "uid": record["uid"],
                "gid": record["gid"],
            }
            for record in records
        ],
    }
    return sha256_bytes(canonical_json_bytes(identity))


def _remove_tree(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()
    _fsync_directory(path.parent)


def _set_owner(path: Path, uid: int, gid: int, *, symlink: bool = False) -> None:
    try:
        os.chown(path, uid, gid, follow_symlinks=not symlink)
    except PermissionError as exc:
        raise LegacyTakeoverError(f"cannot preserve ownership for {path}") from exc


def _copy_preserving(source: Path, destination: Path) -> None:
    metadata = source.lstat()
    kind = _path_kind(metadata)
    mode = stat.S_IMODE(metadata.st_mode)
    if kind == "symlink":
        os.symlink(os.readlink(source), destination)
        _set_owner(destination, metadata.st_uid, metadata.st_gid, symlink=True)
        return
    if kind == "file":
        source_fd = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        destination_fd: int | None = None
        try:
            destination_fd = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            while True:
                payload = os.read(source_fd, 1024 * 1024)
                if not payload:
                    break
                view = memoryview(payload)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fchmod(destination_fd, mode)
            os.fchown(destination_fd, metadata.st_uid, metadata.st_gid)
            os.fsync(destination_fd)
        finally:
            os.close(source_fd)
            if destination_fd is not None:
                os.close(destination_fd)
        return
    destination.mkdir(mode=0o700)
    _set_owner(destination, metadata.st_uid, metadata.st_gid)
    for child in sorted(os.scandir(source), key=lambda item: os.fsencode(item.name)):
        _copy_preserving(source / child.name, destination / child.name)
    os.chmod(destination, mode, follow_symlinks=False)
    _set_owner(destination, metadata.st_uid, metadata.st_gid)
    _fsync_directory(destination)


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise LegacyTakeoverError("renameat2 is required for no-clobber publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise LegacyTakeoverError(
                f"external destination already exists: {destination}"
            )
        raise LegacyTakeoverError(
            f"cannot publish externalized path: {os.strerror(error)}"
        )
    _fsync_directory(destination.parent)


def _ensure_copy(
    source: Path,
    destination: Path,
    expected: dict[str, Any],
    *,
    stage_label: str,
    require_private_parent: bool = True,
    private_anchor: Path | None = None,
) -> None:
    if require_private_parent:
        if private_anchor is None:
            raise LegacyTakeoverError("external copy has no fixed private root")
        _ensure_private_descendant(destination.parent, private_anchor)
    else:
        try:
            parent = destination.parent.lstat()
        except OSError as exc:
            raise LegacyTakeoverError(
                f"restore parent is unavailable: {destination.parent}"
            ) from exc
        if (
            not stat.S_ISDIR(parent.st_mode)
            or destination.parent.is_symlink()
            or parent.st_uid != os.geteuid()
            or bool(stat.S_IMODE(parent.st_mode) & 0o022)
        ):
            raise LegacyTakeoverError(
                f"restore parent is unsafe: {destination.parent}"
            )
    if destination.exists() or destination.is_symlink():
        verify_path_seal(destination, expected)
        if source.exists() or source.is_symlink():
            verify_path_seal(source, expected)
        return
    if not (source.exists() or source.is_symlink()):
        raise LegacyTakeoverError(
            f"both source and external destination are absent: {source}"
        )
    verify_path_seal(source, expected)
    stage = destination.parent / f".takeover-stage-{stage_label}"
    if stage.exists() or stage.is_symlink():
        _remove_tree(stage)
    _copy_preserving(source, stage)
    verify_path_seal(stage, expected)
    verify_path_seal(source, expected)
    _rename_noreplace(stage, destination)
    verify_path_seal(destination, expected)


def _ensure_detached_removed(
    path: Path,
    trash: Path,
    expected: dict[str, Any],
    *,
    private_anchor: Path,
    after_detach: Callable[[], None],
) -> None:
    _ensure_private_descendant(trash.parent, private_anchor)
    path_exists = path.exists() or path.is_symlink()
    trash_exists = trash.exists() or trash.is_symlink()
    if path_exists and trash_exists:
        raise LegacyTakeoverError(
            f"both live path and deterministic trash exist: {path}"
        )
    if path_exists:
        verify_path_seal(path, expected)
        _rename_noreplace(path, trash)
        _fsync_directory(path.parent)
        after_detach()
        trash_exists = True
    if trash_exists:
        verify_path_seal_subset(trash, expected)
        _remove_tree(trash)
    if (
        path.exists()
        or path.is_symlink()
        or trash.exists()
        or trash.is_symlink()
    ):
        raise LegacyTakeoverError(f"detached legacy path was not removed: {path}")


def _ensure_detached_archived(
    path: Path,
    archive: Path,
    expected: dict[str, Any],
    *,
    private_anchor: Path,
    after_detach: Callable[[], None],
) -> None:
    """Atomically retain exact audit/backup evidence outside live controls."""

    _ensure_private_descendant(archive.parent, private_anchor)
    path_exists = path.exists() or path.is_symlink()
    archive_exists = archive.exists() or archive.is_symlink()
    if path_exists and archive_exists:
        raise LegacyTakeoverError(
            f"both live path and preserved archive exist: {path}"
        )
    if path_exists:
        verify_path_seal(path, expected)
        _rename_noreplace(path, archive)
        _fsync_directory(path.parent)
        after_detach()
        archive_exists = True
    if not archive_exists:
        raise LegacyTakeoverError(
            f"preserved control archive is unavailable: {path}"
        )
    verify_path_seal(archive, expected)
    if path.exists() or path.is_symlink():
        raise LegacyTakeoverError(
            f"preserved live control path remains: {path}"
        )


def _normal_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise LegacyTakeoverError("classification contains an invalid path")
    stripped = value[:-1] if value.endswith("/") else value
    path = PurePosixPath(stripped)
    if (
        path.is_absolute()
        or str(path) != stripped
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] == ".git"
    ):
        raise LegacyTakeoverError("classification path escapes the repository")
    return stripped


def _paths_overlap(first: str, second: str) -> bool:
    left = PurePosixPath(first).parts
    right = PurePosixPath(second).parts
    shortest = min(len(left), len(right))
    return left[:shortest] == right[:shortest]


def validate_classification(
    document: object,
    *,
    ignored_paths: list[str],
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "review_id",
        "paths",
    }:
        raise LegacyTakeoverError("classification map has an invalid shape")
    if document.get("schema_version") != 1:
        raise LegacyTakeoverError("classification map schema is unsupported")
    review_id = document.get("review_id")
    if not isinstance(review_id, str) or REVIEW_RE.fullmatch(review_id) is None:
        raise LegacyTakeoverError("classification review identity is invalid")
    raw_paths = document.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise LegacyTakeoverError("classification map is empty")
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_paths:
        if not isinstance(raw, dict) or set(raw) != {"path", "class"}:
            raise LegacyTakeoverError("classification record has an invalid shape")
        relative = _normal_relative_path(raw.get("path"))
        category = raw.get("class")
        if category not in EXTERNAL_ROOTS:
            raise LegacyTakeoverError("classification class is unsupported")
        if relative in seen:
            raise LegacyTakeoverError("classification path occurs more than once")
        for prior in seen:
            if _paths_overlap(prior, relative):
                raise LegacyTakeoverError("classification paths overlap")
        seen.add(relative)
        records.append({"path": relative, "class": str(category)})
    normalized_ignored = [_normal_relative_path(value) for value in ignored_paths]
    if len(normalized_ignored) != len(set(normalized_ignored)):
        raise LegacyTakeoverError("Git returned duplicate ignored paths")
    missing = sorted(set(normalized_ignored) - seen)
    extra = sorted(seen - set(normalized_ignored))
    if missing or extra:
        raise LegacyTakeoverError(
            "classification does not cover ignored paths exactly "
            f"(missing={missing!r}, extra={extra!r})"
        )
    return {
        "schema_version": 1,
        "review_id": review_id,
        "paths": sorted(records, key=lambda item: os.fsencode(item["path"])),
    }


def _runtime_process_identity(document: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "backend_container_id",
        "backend_pid",
        "backend_started_at",
        "backend_restart_count",
        "worker_unit_name",
        "worker_main_pid",
        "worker_invocation_id",
        "worker_active_enter_monotonic",
    )
    return {name: document[name] for name in fields}


def _require_live_status(
    document: object,
    *,
    expected_runtime_digest: str | None = None,
    expected_process: dict[str, Any] | None = None,
    allowed_states: set[str],
) -> dict[str, Any]:
    try:
        validated = SITE_HELPERS.validate_legacy_status(
            document,
            expected_runtime_digest=expected_runtime_digest,
        )
    except SITE_HELPERS.SiteHelperContractError as exc:
        raise LegacyTakeoverError(str(exc)) from exc
    if validated.get("schema_version") != 2:
        raise LegacyTakeoverError("legacy takeover requires runtime schema v2")
    if validated.get("legacy_runtime_state") not in allowed_states:
        raise LegacyTakeoverError("legacy runtime is not in an allowed state")
    if (
        expected_process is not None
        and _runtime_process_identity(validated) != expected_process
    ):
        raise LegacyTakeoverError("legacy runtime process identity changed")
    return validated


class LiveSystem:
    """Fixed-command production adapter used by the installed controller."""

    def __init__(self, repository: Path, runtime_root: Path):
        self.repository = repository
        self.runtime_root = runtime_root
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": str(runtime_root / "private-home"),
        }

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 120,
        environment: dict[str, str] | None = None,
    ) -> str:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd or Path("/"),
                env=environment or self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LegacyTakeoverError(f"command failed: {command[0]}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[:500]
            raise LegacyTakeoverError(
                f"command failed ({command[0]}): {detail}"
            )
        return completed.stdout

    def _git(self, arguments: list[str]) -> str:
        return self._run(
            ["/usr/bin/git", *arguments],
            cwd=self.repository,
        )

    def origin_urls(self) -> tuple[list[str], list[str]]:
        fetch = self._git(["remote", "get-url", "--all", "origin"]).splitlines()
        push = self._git(
            ["remote", "get-url", "--push", "--all", "origin"]
        ).splitlines()
        return fetch, push

    def switch_origin(self, expected: str, target: str) -> None:
        fetch, push = self.origin_urls()
        if (
            len(fetch) != 1
            or len(push) != 1
            or fetch[0] not in {expected, target}
            or push[0] not in {expected, target}
        ):
            raise LegacyTakeoverError("Git origin failed compare-and-swap")
        if fetch[0] == expected:
            self._git(["remote", "set-url", "origin", target])
        fetch, push = self.origin_urls()
        if push[0] == expected:
            self._git(["remote", "set-url", "--push", "origin", target])
        if self.origin_urls() != ([target], [target]):
            raise LegacyTakeoverError("Git origin switch is incomplete")

    def ignored_paths(self) -> list[str]:
        payload = self._run(
            [
                "/usr/bin/git",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "--no-empty-directory",
                "-z",
            ],
            cwd=self.repository,
        )
        return sorted(
            (_normal_relative_path(value) for value in payload.split("\0") if value),
            key=os.fsencode,
        )

    def worktree_clean(self) -> bool:
        payload = self._run(
            [
                "/usr/bin/git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=self.repository,
        )
        return payload == ""

    def git_identity(self) -> dict[str, str]:
        branch = self._git(["symbolic-ref", "--quiet", "HEAD"]).strip()
        identity = {
            "branch": branch,
            "head_sha": self._git(
                ["rev-parse", "--verify", "HEAD^{commit}"]
            ).strip(),
            "head_tree": self._git(
                ["rev-parse", "--verify", "HEAD^{tree}"]
            ).strip(),
            "local_main_sha": self._git(
                ["rev-parse", "--verify", "refs/heads/main^{commit}"]
            ).strip(),
        }
        if (
            identity["branch"] != "refs/heads/main"
            or any(
                GIT_SHA_RE.fullmatch(identity[name]) is None
                for name in ("head_sha", "head_tree", "local_main_sha")
            )
            or identity["head_sha"] != identity["local_main_sha"]
        ):
            raise LegacyTakeoverError(
                "production checkout is not exact local main"
            )
        return identity

    def helper_report(self) -> dict[str, Any]:
        try:
            return SITE_HELPERS.inspect_helper_installation(self.runtime_root)
        except SITE_HELPERS.SiteHelperContractError as exc:
            raise LegacyTakeoverError(str(exc)) from exc

    def _helper_json(self, name: str) -> Any:
        helper = self.runtime_root / "config" / name
        output = self._run([str(helper)])
        if len(output.encode("utf-8")) > SITE_HELPERS.MAX_EVIDENCE_BYTES:
            raise LegacyTakeoverError(f"site helper output is too large: {name}")
        try:
            return json.loads(output, object_pairs_hook=_json_no_duplicates)
        except json.JSONDecodeError as exc:
            raise LegacyTakeoverError(
                f"site helper returned invalid JSON: {name}"
            ) from exc

    def legacy_status(self) -> dict[str, Any]:
        value = self._helper_json("bootstrap-legacy-runtime-status")
        if not isinstance(value, dict):
            raise LegacyTakeoverError("legacy status helper did not return an object")
        return value

    def drain(self) -> dict[str, Any]:
        self._run([str(self.runtime_root / "config" / "bootstrap-quiesce")])
        evidence = self._helper_json("bootstrap-active-jobs-probe")
        try:
            validated = SITE_HELPERS.validate_active_jobs(evidence)
        except SITE_HELPERS.SiteHelperContractError as exc:
            raise LegacyTakeoverError(str(exc)) from exc
        if (
            validated.get("active_jobs_schema_version") != 2
            or set(validated["active_jobs"]) != set(ZERO_ACTIVE_JOB_FIELDS)
            or validated["active_total"] != 0
        ):
            raise LegacyTakeoverError("legacy active work did not drain to zero")
        return validated

    def _container(self, container_id: str) -> dict[str, Any]:
        output = self._run(["/usr/bin/docker", "inspect", container_id])
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise LegacyTakeoverError("Docker returned invalid inspect JSON") from exc
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise LegacyTakeoverError("Docker inspect selected another container")
        return value[0]

    def _validate_container(
        self,
        role: str,
        runtime: dict[str, Any],
        *,
        require_process_match: bool,
    ) -> tuple[str, dict[str, Any]]:
        container_id = runtime[f"{role}_container_id"]
        record = self._container(container_id)
        state = record.get("State")
        config = record.get("Config")
        if (
            record.get("Id") != container_id
            or record.get("Image") != runtime[f"{role}_image_id"]
            or not isinstance(state, dict)
            or not isinstance(config, dict)
        ):
            raise LegacyTakeoverError(f"sealed {role} container identity changed")
        process_spec = {
            "entrypoint": config.get("Entrypoint"),
            "command": config.get("Cmd"),
        }
        if any(
            value is not None
            and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            )
            for value in process_spec.values()
        ) or sha256_bytes(canonical_json_bytes(process_spec)) != runtime[
            f"{role}_process_spec_sha256"
        ]:
            raise LegacyTakeoverError(
                f"sealed {role} entrypoint/command changed"
            )
        if require_process_match and state.get("Running") is True:
            expected = {
                "Pid": runtime[f"{role}_pid"],
                "StartedAt": runtime[f"{role}_started_at"],
            }
            if any(state.get(name) != value for name, value in expected.items()):
                raise LegacyTakeoverError(f"sealed {role} process identity changed")
            if record.get("RestartCount") != runtime[f"{role}_restart_count"]:
                raise LegacyTakeoverError(f"sealed {role} restart count changed")
        return container_id, record

    def stop_container(self, role: str, runtime: dict[str, Any]) -> None:
        if role not in {"backend", "web"}:
            raise LegacyTakeoverError("unsupported legacy container role")
        container_id, before = self._validate_container(
            role, runtime, require_process_match=True
        )
        if before["State"].get("Running") is True:
            self._run(
                ["/usr/bin/docker", "stop", "--time", "30", container_id],
                timeout=60,
            )
        _, after = self._validate_container(
            role, runtime, require_process_match=False
        )
        if after["State"].get("Running") is not False:
            raise LegacyTakeoverError(f"sealed {role} container did not stop")

    def _worker_environment(self, runtime: dict[str, Any]) -> dict[str, str]:
        uid = runtime["worker_manager_uid"]
        runtime_dir = runtime["worker_manager_runtime_dir"]
        if uid != os.geteuid() or runtime_dir != f"/run/user/{uid}":
            raise LegacyTakeoverError("sealed Worker user-manager identity changed")
        bus = f"unix:path={runtime_dir}/bus"
        identity = {
            "uid": uid,
            "xdg_runtime_dir": runtime_dir,
            "dbus_session_bus_address": bus,
        }
        if (
            sha256_bytes(canonical_json_bytes(identity))
            != runtime["worker_manager_environment_sha256"]
        ):
            raise LegacyTakeoverError("sealed Worker manager environment changed")
        try:
            runtime_metadata = Path(runtime_dir).lstat()
            bus_metadata = (Path(runtime_dir) / "bus").lstat()
        except OSError as exc:
            raise LegacyTakeoverError("Worker user-manager bus is unavailable") from exc
        if (
            not stat.S_ISDIR(runtime_metadata.st_mode)
            or Path(runtime_dir).is_symlink()
            or runtime_metadata.st_uid != uid
            or not stat.S_ISSOCK(bus_metadata.st_mode)
            or bus_metadata.st_uid != uid
        ):
            raise LegacyTakeoverError("Worker user-manager bus identity changed")
        return {
            **self.environment,
            "XDG_RUNTIME_DIR": runtime_dir,
            "DBUS_SESSION_BUS_ADDRESS": bus,
        }

    def _systemd_property(
        self,
        runtime: dict[str, Any],
        name: str,
    ) -> str:
        unit = runtime["worker_unit_name"]
        return self._run(
            [
                "/usr/bin/systemctl",
                "--user",
                "show",
                unit,
                f"--property={name}",
                "--value",
            ],
            environment=self._worker_environment(runtime),
        ).strip()

    def _validate_worker_unit(self, runtime: dict[str, Any]) -> None:
        unit = runtime["worker_unit_name"]
        fragment = Path(self._systemd_property(runtime, "FragmentPath"))
        try:
            metadata = fragment.lstat()
        except OSError as exc:
            raise LegacyTakeoverError("sealed Worker unit is unavailable") from exc
        if (
            str(fragment) != runtime["worker_unit_path"]
            or not fragment.is_absolute()
            or fragment.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or f"{stat.S_IMODE(metadata.st_mode):04o}"
            != runtime["worker_unit_mode"]
            or metadata.st_uid != runtime["worker_unit_uid"]
            or metadata.st_gid != runtime["worker_unit_gid"]
            or sha256_file(fragment) != runtime["worker_unit_sha256"]
        ):
            raise LegacyTakeoverError("sealed Worker unit changed")

    def _validate_postgres(self, runtime: dict[str, Any]) -> dict[str, Any]:
        record = self._container(runtime["postgres_container_id"])
        state = record.get("State")
        mounts = record.get("Mounts")
        matching_volumes = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Type") == "volume"
            and mount.get("Name") == runtime["postgres_data_volume"]
            and mount.get("Destination") == "/var/lib/postgresql/data"
            and mount.get("RW") is True
        ] if isinstance(mounts, list) else []
        if (
            record.get("Id") != runtime["postgres_container_id"]
            or record.get("Image") != runtime["postgres_image_id"]
            or not isinstance(state, dict)
            or state.get("Running") is not True
            or len(matching_volumes) != 1
        ):
            raise LegacyTakeoverError("sealed PostgreSQL runtime identity changed")
        control = self._run(
            [
                "/usr/bin/docker",
                "exec",
                "--user",
                "postgres",
                runtime["postgres_container_id"],
                "pg_controldata",
                "-D",
                "/var/lib/postgresql/data",
            ]
        )
        identifiers = re.findall(
            r"^Database system identifier:\s*([0-9]{10,30})\s*$",
            control,
            flags=re.MULTILINE,
        )
        if identifiers != [runtime["postgres_system_identifier"]]:
            raise LegacyTakeoverError(
                "sealed PostgreSQL system identifier changed"
            )
        return {
            "postgres_container_id": runtime["postgres_container_id"],
            "postgres_image_id": runtime["postgres_image_id"],
            "postgres_data_volume": runtime["postgres_data_volume"],
            "postgres_system_identifier": runtime[
                "postgres_system_identifier"
            ],
        }

    def stop_worker(self, runtime: dict[str, Any]) -> None:
        self._validate_worker_unit(runtime)
        unit = runtime["worker_unit_name"]
        main_pid = self._systemd_property(runtime, "MainPID")
        if main_pid not in {"", "0"}:
            if (
                main_pid != str(runtime["worker_main_pid"])
                or self._systemd_property(runtime, "InvocationID")
                != runtime["worker_invocation_id"]
                or self._systemd_property(
                    runtime,
                    "ActiveEnterTimestampMonotonic",
                )
                != str(runtime["worker_active_enter_monotonic"])
            ):
                raise LegacyTakeoverError("sealed Worker process identity changed")
            self._run(
                ["/usr/bin/systemctl", "--user", "stop", unit],
                environment=self._worker_environment(runtime),
            )
        if self._systemd_property(runtime, "MainPID") not in {"", "0"}:
            raise LegacyTakeoverError("sealed Worker unit did not stop")

    def assert_runtime_stopped(
        self,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        for role in ("backend", "web"):
            _, record = self._validate_container(
                role,
                runtime,
                require_process_match=False,
            )
            if record["State"].get("Running") is not False:
                raise LegacyTakeoverError(
                    f"sealed {role} container restarted during takeover"
                )
        self._validate_worker_unit(runtime)
        if self._systemd_property(runtime, "MainPID") not in {"", "0"}:
            raise LegacyTakeoverError(
                "sealed Worker restarted during takeover"
            )
        postgres = self._validate_postgres(runtime)
        return {
            "schema_version": 1,
            "readers_stopped": True,
            "postgres_running_untouched": True,
            "backend_container_id": runtime["backend_container_id"],
            "backend_image_id": runtime["backend_image_id"],
            "web_container_id": runtime["web_container_id"],
            "web_image_id": runtime["web_image_id"],
            "worker_unit_name": runtime["worker_unit_name"],
            "worker_unit_sha256": runtime["worker_unit_sha256"],
            "worker_manager_uid": runtime["worker_manager_uid"],
            **postgres,
        }

    def _start_container(self, role: str, runtime: dict[str, Any]) -> None:
        container_id, record = self._validate_container(
            role, runtime, require_process_match=False
        )
        if record["State"].get("Running") is False:
            self._run(["/usr/bin/docker", "start", container_id])
        _, after = self._validate_container(
            role, runtime, require_process_match=False
        )
        if after["State"].get("Running") is not True:
            raise LegacyTakeoverError(f"sealed {role} container did not start")

    def restore_runtime(self, runtime: dict[str, Any]) -> dict[str, Any]:
        self._validate_postgres(runtime)
        self._start_container("backend", runtime)
        self._validate_postgres(runtime)
        self._validate_worker_unit(runtime)
        unit = runtime["worker_unit_name"]
        if self._systemd_property(runtime, "MainPID") in {"", "0"}:
            self._validate_postgres(runtime)
            self._run(
                ["/usr/bin/systemctl", "--user", "start", unit],
                environment=self._worker_environment(runtime),
            )
            self._validate_postgres(runtime)
        else:
            self._validate_postgres(runtime)
        self._validate_postgres(runtime)
        self._start_container("web", runtime)
        self._validate_postgres(runtime)
        evidence = self._helper_json("bootstrap-legacy-runtime-restore")
        if not isinstance(evidence, dict):
            raise LegacyTakeoverError("legacy restore helper returned invalid evidence")
        return evidence

    def reload_worker_manager(self, runtime: dict[str, Any]) -> None:
        self._run(
            ["/usr/bin/systemctl", "--user", "daemon-reload"],
            environment=self._worker_environment(runtime),
        )
        self._validate_worker_unit(runtime)


class LegacyTakeover:
    def __init__(
        self,
        *,
        repository: Path,
        runtime_root: Path,
        external_roots: dict[str, Path],
        system: Any,
        checkpoint: Callable[[str], None] | None = None,
    ):
        self.repository = repository.absolute()
        self.runtime_root = runtime_root.absolute()
        self.external_roots = {
            name: value.absolute() for name, value in external_roots.items()
        }
        if set(self.external_roots) != set(EXTERNAL_ROOTS):
            raise LegacyTakeoverError("external root classes are incomplete")
        self.system = system
        self.checkpoint = checkpoint or (lambda _label: None)
        self.classification_path = (
            self.runtime_root / CLASSIFICATION_RELATIVE_PATH
        )
        self.state_directory = (
            self.runtime_root / STATE_DIRECTORY_RELATIVE_PATH
        )
        self.operations_directory = self.state_directory / "operations"
        self.active_path = self.state_directory / "active.json"
        self.lock_path = self.state_directory / "controller.lock"
        # Fixed lock order: the bootstrap/Pull global lock, then the
        # takeover-specific execution lock.
        self.global_deploy_lock_path = self.runtime_root / "state/deploy.lock"
        self.execution_lock_path = self.state_directory / "execution.lock"

    @staticmethod
    def _control_layout_identity(
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "records": [
                {
                    "relative_path": record["relative_path"],
                    "present": record["present"],
                    "seal": record["seal"],
                }
                for record in records
            ],
        }

    @classmethod
    def _control_layout_digest(
        cls,
        records: list[dict[str, Any]],
    ) -> str:
        return sha256_bytes(
            canonical_json_bytes(cls._control_layout_identity(records))
        )

    def _snapshot_control_layout(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for relative in CONTROL_LAYOUT_RELATIVE_PATHS:
            path = self.runtime_root / relative
            present = path.exists() or path.is_symlink()
            records.append(
                {
                    "relative_path": relative,
                    "present": present,
                    "seal": seal_path(path) if present else None,
                }
            )
        return records

    @staticmethod
    def _same_control_layout_record(
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> bool:
        return (
            left["relative_path"] == right["relative_path"]
            and left["present"] is right["present"]
            and left["seal"] == right["seal"]
        )

    def _verify_original_control_layout(
        self,
        state: dict[str, Any],
    ) -> None:
        current = self._snapshot_control_layout()
        if self._control_layout_digest(current) != state[
            "control_layout_sha256"
        ]:
            raise LegacyTakeoverError(
                "bootstrap control layout changed before takeover completed"
            )
        for original, actual in zip(
            state["control_layout"],
            current,
            strict=True,
        ):
            if not self._same_control_layout_record(original, actual):
                raise LegacyTakeoverError(
                    "bootstrap control layout changed before takeover completed"
                )

    @staticmethod
    def _checkout_permission_identity(
        record: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "path": record["path"],
            "type": record["type"],
            "mode": record["mode"],
            "uid": record["uid"],
            "gid": record["gid"],
        }

    def _current_checkout_permissions(
        self,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return snapshot_checkout_permissions(
            self.repository,
            expected_paths=[
                record["path"]
                for record in state["checkout_permissions"]
            ],
        )

    def _verify_original_checkout_permissions(
        self,
        state: dict[str, Any],
    ) -> None:
        current = self._current_checkout_permissions(state)
        if checkout_permissions_digest(current) != state[
            "checkout_permissions_sha256"
        ]:
            raise LegacyTakeoverError(
                "production checkout permissions changed before takeover completed"
            )

    def _state_path(self, operation_id: str) -> Path:
        if OPERATION_RE.fullmatch(operation_id) is None:
            raise LegacyTakeoverError("takeover operation ID is invalid")
        return self.operations_directory / f"{operation_id}.json"

    @contextmanager
    def _state_lock(self) -> Any:
        _ensure_private_directory(self.state_directory)
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR
            | os.O_CREAT
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise LegacyTakeoverError("takeover controller lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _execution_lock(
        self,
        *,
        parent_deploy_lock_fd: int | None = None,
    ) -> Any:
        _ensure_private_directory(self.state_directory)
        _ensure_private_directory(self.global_deploy_lock_path.parent)
        descriptors: list[int] = []
        try:
            if parent_deploy_lock_fd is None:
                lock_paths = (
                    (self.global_deploy_lock_path, "global deploy"),
                    (self.execution_lock_path, "takeover execution"),
                )
            else:
                _validate_inherited_parent_lock(
                    parent_deploy_lock_fd,
                    self.global_deploy_lock_path,
                )
                lock_paths = (
                    (self.execution_lock_path, "takeover execution"),
                )
            for path, label in lock_paths:
                descriptor = os.open(
                    path,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise LegacyTakeoverError(f"{label} lock is unsafe")
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError as exc:
                    raise LegacyTakeoverError(
                        f"another process holds the {label} lock"
                    ) from exc
            yield
        finally:
            for descriptor in reversed(descriptors):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _active_document(self) -> dict[str, Any] | None:
        if not (self.active_path.exists() or self.active_path.is_symlink()):
            return None
        _validate_private_file(self.active_path)
        value = _load_json_file(self.active_path, 4096)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "operation_id"}
            or value.get("schema_version") != 1
            or not isinstance(value.get("operation_id"), str)
            or OPERATION_RE.fullmatch(value["operation_id"]) is None
        ):
            raise LegacyTakeoverError("global takeover activity record is invalid")
        return value

    def _claim_active(self, operation_id: str) -> bool:
        self._state_path(operation_id)
        with self._state_lock():
            _ensure_private_directory(self.operations_directory)
            active = self._active_document()
            if active is not None:
                if active["operation_id"] != operation_id:
                    raise LegacyTakeoverError(
                        "another legacy takeover operation is active"
                    )
                return False
            for path in sorted(self.operations_directory.glob("takeover-*.json")):
                _validate_private_file(path)
                record = _load_json_file(path, MAX_STATE_BYTES)
                if not isinstance(record, dict):
                    raise LegacyTakeoverError(
                        "archived takeover operation is invalid"
                    )
                terminal = record.get("restore_phase") == "restored" or (
                    record.get("apply_phase") == "complete"
                    and record.get("restore_phase") is None
                )
                archived_operation = record.get("operation_id")
                if (
                    not terminal
                    and archived_operation != operation_id
                ):
                    raise LegacyTakeoverError(
                        "an unowned non-terminal takeover record exists"
                    )
            _create_json_exclusive(
                self.active_path,
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                },
            )
            return True

    def _release_active(self, operation_id: str) -> None:
        with self._state_lock():
            active = self._active_document()
            if active is None:
                return
            if active["operation_id"] != operation_id:
                raise LegacyTakeoverError(
                    "cannot release another takeover operation"
                )
            self.active_path.unlink()
            _fsync_directory(self.active_path.parent)

    def _save(self, state: dict[str, Any]) -> None:
        generation = state.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise LegacyTakeoverError("takeover state generation is invalid")
        state["generation"] = generation + 1
        _atomic_json(self._state_path(state["operation_id"]), state)

    def _checkpoint(self, label: str) -> None:
        self.checkpoint(label)

    def _load(self, operation_id: str) -> dict[str, Any]:
        state_path = self._state_path(operation_id)
        _validate_private_file(state_path)
        value = _load_json_file(state_path, MAX_STATE_BYTES)
        required_fields = {
            "schema_version",
            "operation_id",
            "repository",
            "generation",
            "apply_phase",
            "restore_phase",
            "classification_sha256",
            "classification_review_id",
            "classification_paths",
            "origin_before",
            "origin_after",
            "git_identity",
            "helper_report_sha256",
            "helper_hashes",
            "runtime_identity_sha256",
            "runtime_evidence_sha256",
            "runtime",
            "worker_unit_seal",
            "worker_unit_backup",
            "control_layout",
            "control_layout_sha256",
            "checkout_permissions",
            "checkout_permissions_sha256",
            "moves",
        }
        allowed_fields = required_fields | {
            "drained_evidence_sha256",
            "restore_evidence_sha256",
            "applied_record_sha256",
            "restored_terminal_sha256",
            "worker_unit_replacement_sha256",
            "control_layout_replacement",
            "control_layout_replacement_sha256",
            "checkout_permissions_replacement",
            "checkout_permissions_replacement_sha256",
            "active_jobs_zero",
            "active_jobs_zero_sha256",
            "drained_runtime",
            "stopped_runtime",
            "pre_stopped_fence",
            "pre_stopped_fence_sha256",
            "sealed_at",
            "drained_at",
            "runtime_stopped_at",
        }
        if (
            not isinstance(value, dict)
            or not required_fields.issubset(value)
            or not set(value).issubset(allowed_fields)
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("operation_id") != operation_id
            or value.get("repository") != str(self.repository)
            or isinstance(value.get("generation"), bool)
            or not isinstance(value.get("generation"), int)
            or value["generation"] <= 0
            or value.get("apply_phase") not in APPLY_PHASES
            or (
                value.get("restore_phase") is not None
                and value.get("restore_phase") not in RESTORE_PHASES
            )
            or not isinstance(value.get("moves"), list)
        ):
            raise LegacyTakeoverError("takeover state is invalid or belongs elsewhere")
        for name in (
            "classification_sha256",
            "helper_report_sha256",
            "runtime_identity_sha256",
            "runtime_evidence_sha256",
            "control_layout_sha256",
            "checkout_permissions_sha256",
        ):
            if (
                not isinstance(value.get(name), str)
                or DIGEST_RE.fullmatch(value[name]) is None
            ):
                raise LegacyTakeoverError("takeover state digest is invalid")
        for name in (
            "drained_evidence_sha256",
            "restore_evidence_sha256",
            "applied_record_sha256",
            "restored_terminal_sha256",
            "active_jobs_zero_sha256",
            "pre_stopped_fence_sha256",
            "worker_unit_replacement_sha256",
            "control_layout_replacement_sha256",
            "checkout_permissions_replacement_sha256",
        ):
            if name in value and (
                not isinstance(value[name], str)
                or DIGEST_RE.fullmatch(value[name]) is None
            ):
                raise LegacyTakeoverError("takeover state evidence digest is invalid")
        for name in (
            "sealed_at",
            "drained_at",
            "runtime_stopped_at",
        ):
            if name in value and (
                not isinstance(value[name], str) or not value[name]
            ):
                raise LegacyTakeoverError("takeover state timestamp is invalid")
        if value.get("origin_before") != REPOSITORY_HTTPS_URL or value.get(
            "origin_after"
        ) != REPOSITORY_SSH_URL:
            raise LegacyTakeoverError("takeover state origin identity is invalid")
        git_identity = value.get("git_identity")
        if (
            not isinstance(git_identity, dict)
            or set(git_identity)
            != {"branch", "head_sha", "head_tree", "local_main_sha"}
            or git_identity.get("branch") != "refs/heads/main"
            or any(
                not isinstance(git_identity.get(name), str)
                or GIT_SHA_RE.fullmatch(git_identity[name]) is None
                for name in ("head_sha", "head_tree", "local_main_sha")
            )
            or git_identity["head_sha"] != git_identity["local_main_sha"]
        ):
            raise LegacyTakeoverError("takeover Git seal is invalid")
        classification_paths = value.get("classification_paths")
        if (
            not isinstance(classification_paths, list)
            or not classification_paths
            or not isinstance(value.get("classification_review_id"), str)
            or REVIEW_RE.fullmatch(value["classification_review_id"]) is None
        ):
            raise LegacyTakeoverError("takeover classification state is invalid")
        helper_hashes = value.get("helper_hashes")
        if (
            not isinstance(helper_hashes, dict)
            or set(helper_hashes) != set(SITE_HELPERS.HELPERS)
            or any(
                not isinstance(digest, str)
                or DIGEST_RE.fullmatch(digest) is None
                for digest in helper_hashes.values()
            )
        ):
            raise LegacyTakeoverError("takeover helper seal is invalid")
        runtime = _require_live_status(
            value.get("runtime"),
            expected_runtime_digest=value["runtime_identity_sha256"],
            allowed_states={"open"},
        )
        if (
            sha256_bytes(canonical_json_bytes(runtime))
            != value["runtime_evidence_sha256"]
        ):
            raise LegacyTakeoverError("takeover runtime evidence digest differs")
        if "active_jobs_zero" in value:
            try:
                jobs = SITE_HELPERS.validate_active_jobs(
                    value["active_jobs_zero"]
                )
            except SITE_HELPERS.SiteHelperContractError as exc:
                raise LegacyTakeoverError(str(exc)) from exc
            if (
                jobs.get("active_jobs_schema_version") != 2
                or jobs.get("active_total") != 0
                or sha256_bytes(canonical_json_bytes(jobs))
                != value.get("active_jobs_zero_sha256")
            ):
                raise LegacyTakeoverError("active-job zero evidence differs")
        if "pre_stopped_fence" in value and sha256_bytes(
            canonical_json_bytes(value["pre_stopped_fence"])
        ) != value.get("pre_stopped_fence_sha256"):
            raise LegacyTakeoverError("pre-stopped runtime fence digest differs")
        worker_unit_seal = validate_seal_document(
            value.get("worker_unit_seal")
        )
        if worker_unit_seal["records"][0].get("sha256") != runtime[
            "worker_unit_sha256"
        ]:
            raise LegacyTakeoverError("Worker unit seal differs from runtime")
        expected_backup = (
            self.external_roots["runtime"]
            / ".takeover-metadata"
            / operation_id
            / "legacy-worker-unit.service"
        )
        if value.get("worker_unit_backup") != str(expected_backup):
            raise LegacyTakeoverError("Worker unit backup path is invalid")
        control_layout = value.get("control_layout")
        if (
            not isinstance(control_layout, list)
            or len(control_layout) != len(CONTROL_LAYOUT_RELATIVE_PATHS)
        ):
            raise LegacyTakeoverError("bootstrap control layout state is invalid")
        for index, (record, relative) in enumerate(
            zip(
                control_layout,
                CONTROL_LAYOUT_RELATIVE_PATHS,
                strict=True,
            )
        ):
            expected_control_backup = (
                self.external_roots["runtime"]
                / ".takeover-metadata"
                / operation_id
                / "prior-control"
                / str(index)
            )
            expected_control_trash = (
                self.external_roots["runtime"]
                / ".takeover-control-trash"
                / operation_id
                / str(index)
            )
            if (
                not isinstance(record, dict)
                or set(record)
                != {
                    "index",
                    "relative_path",
                    "present",
                    "seal",
                    "backup",
                    "trash",
                    "restore_status",
                }
                or record.get("index") != index
                or record.get("relative_path") != relative
                or not isinstance(record.get("present"), bool)
                or record.get("backup") != str(expected_control_backup)
                or record.get("trash") != str(expected_control_trash)
                or record.get("restore_status")
                not in CONTROL_LAYOUT_RESTORE_STATUSES
            ):
                raise LegacyTakeoverError(
                    "bootstrap control layout record is invalid"
                )
            if record["present"]:
                validate_seal_document(record.get("seal"))
            elif record.get("seal") is not None:
                raise LegacyTakeoverError(
                    "absent bootstrap control has an unexpected seal"
                )
        if self._control_layout_digest(control_layout) != value[
            "control_layout_sha256"
        ]:
            raise LegacyTakeoverError(
                "bootstrap control layout digest differs"
            )
        replacement_layout = value.get("control_layout_replacement")
        if replacement_layout is not None:
            if (
                not isinstance(replacement_layout, list)
                or len(replacement_layout)
                != len(CONTROL_LAYOUT_RELATIVE_PATHS)
            ):
                raise LegacyTakeoverError(
                    "bootstrap replacement control layout is invalid"
                )
            for record, relative in zip(
                replacement_layout,
                CONTROL_LAYOUT_RELATIVE_PATHS,
                strict=True,
            ):
                if (
                    not isinstance(record, dict)
                    or set(record)
                    != {"relative_path", "present", "seal"}
                    or record.get("relative_path") != relative
                    or not isinstance(record.get("present"), bool)
                ):
                    raise LegacyTakeoverError(
                        "bootstrap replacement control record is invalid"
                    )
                if record["present"]:
                    validate_seal_document(record.get("seal"))
                elif record.get("seal") is not None:
                    raise LegacyTakeoverError(
                        "absent replacement control has an unexpected seal"
                    )
            if (
                value.get("control_layout_replacement_sha256")
                != self._control_layout_digest(replacement_layout)
            ):
                raise LegacyTakeoverError(
                    "bootstrap replacement control digest differs"
                )
        elif "control_layout_replacement_sha256" in value:
            raise LegacyTakeoverError(
                "bootstrap replacement digest has no layout"
            )
        checkout_permissions = value.get("checkout_permissions")
        if (
            not isinstance(checkout_permissions, list)
            or not checkout_permissions
        ):
            raise LegacyTakeoverError(
                "checkout permission inventory is invalid"
            )
        permission_paths: list[str] = []
        for record in checkout_permissions:
            if (
                not isinstance(record, dict)
                or set(record)
                != {
                    "path",
                    "type",
                    "mode",
                    "uid",
                    "gid",
                    "restore_status",
                }
                or record.get("type")
                not in {"file", "directory", "symlink"}
                or not isinstance(record.get("mode"), str)
                or re.fullmatch(r"[0-7]{4}", record["mode"]) is None
                or isinstance(record.get("uid"), bool)
                or not isinstance(record.get("uid"), int)
                or record["uid"] < 0
                or isinstance(record.get("gid"), bool)
                or not isinstance(record.get("gid"), int)
                or record["gid"] < 0
                or record.get("restore_status")
                not in CHECKOUT_PERMISSION_RESTORE_STATUSES
            ):
                raise LegacyTakeoverError(
                    "checkout permission record is invalid"
                )
            relative = record.get("path")
            if relative != ".":
                relative = _normal_checkout_relative_path(relative)
            if relative in permission_paths:
                raise LegacyTakeoverError(
                    "checkout permission path occurs more than once"
                )
            permission_paths.append(relative)
        if permission_paths[0] != "." or checkout_permissions_digest(
            checkout_permissions
        ) != value["checkout_permissions_sha256"]:
            raise LegacyTakeoverError(
                "checkout permission inventory digest differs"
            )
        replacement_permissions = value.get(
            "checkout_permissions_replacement"
        )
        if replacement_permissions is not None:
            if (
                not isinstance(replacement_permissions, list)
                or len(replacement_permissions)
                != len(checkout_permissions)
            ):
                raise LegacyTakeoverError(
                    "checkout replacement permission inventory is invalid"
                )
            for original, replacement in zip(
                checkout_permissions,
                replacement_permissions,
                strict=True,
            ):
                if (
                    not isinstance(replacement, dict)
                    or set(replacement)
                    != {"path", "type", "mode", "uid", "gid"}
                    or replacement.get("path") != original["path"]
                    or replacement.get("type") != original["type"]
                    or not isinstance(replacement.get("mode"), str)
                    or re.fullmatch(r"[0-7]{4}", replacement["mode"])
                    is None
                    or isinstance(replacement.get("uid"), bool)
                    or not isinstance(replacement.get("uid"), int)
                    or replacement["uid"] < 0
                    or isinstance(replacement.get("gid"), bool)
                    or not isinstance(replacement.get("gid"), int)
                    or replacement["gid"] < 0
                ):
                    raise LegacyTakeoverError(
                        "checkout replacement permission record is invalid"
                    )
            if value.get(
                "checkout_permissions_replacement_sha256"
            ) != checkout_permissions_digest(replacement_permissions):
                raise LegacyTakeoverError(
                    "checkout replacement permission digest differs"
                )
        elif "checkout_permissions_replacement_sha256" in value:
            raise LegacyTakeoverError(
                "checkout replacement permission digest has no inventory"
            )
        if len(value["moves"]) != len(classification_paths):
            raise LegacyTakeoverError("takeover move inventory is incomplete")
        seen: set[str] = set()
        for index, move in enumerate(value["moves"]):
            if (
                not isinstance(move, dict)
                or set(move)
                != {
                    "index",
                    "path",
                    "class",
                    "destination",
                    "source_trash",
                    "destination_trash",
                    "seal",
                    "status",
                    "restore_status",
                }
                or move.get("index") != index
                or move.get("status") not in MOVE_STATUSES
                or move.get("restore_status") not in RESTORE_MOVE_STATUSES
            ):
                raise LegacyTakeoverError("takeover move state is invalid")
            relative = _normal_relative_path(move.get("path"))
            category = move.get("class")
            classification = classification_paths[index]
            if (
                category not in self.external_roots
                or relative in seen
                or classification
                != {"path": relative, "class": category}
                or move.get("destination")
                != str(
                    self.external_roots[category]
                    / operation_id
                    / relative
                )
                or move.get("source_trash")
                != str(
                    self.repository
                    / ".git/nexpoly-legacy-takeover-trash"
                    / operation_id
                    / f"source-{index}"
                )
                or move.get("destination_trash")
                != str(
                    self.external_roots[category]
                    / ".takeover-trash"
                    / operation_id
                    / f"destination-{index}"
                )
            ):
                raise LegacyTakeoverError("takeover move binding is invalid")
            validate_seal_document(move.get("seal"))
            seen.add(relative)
        if value["apply_phase"] == "complete" and value.get(
            "applied_record_sha256"
        ) != self._applied_record_digest(value):
            raise LegacyTakeoverError("applied takeover record digest differs")
        if value["restore_phase"] == "restored" and value.get(
            "restored_terminal_sha256"
        ) != self._restored_terminal_digest(value):
            raise LegacyTakeoverError("restored takeover terminal digest differs")
        return value

    def _classification(
        self,
        *,
        expected_digest: str,
        ignored_paths: list[str],
    ) -> tuple[dict[str, Any], str]:
        if DIGEST_RE.fullmatch(expected_digest) is None:
            raise LegacyTakeoverError("classification digest must be a full SHA-256")
        _ensure_private_directory(self.classification_path.parent, create=False)
        _validate_private_file(self.classification_path)
        actual_digest = sha256_file(self.classification_path)
        if actual_digest != expected_digest:
            raise LegacyTakeoverError("classification digest differs from review")
        document = _load_json_file(
            self.classification_path,
            MAX_CLASSIFICATION_BYTES,
        )
        return (
            validate_classification(document, ignored_paths=ignored_paths),
            actual_digest,
        )

    def _require_origin(self, expected: str) -> None:
        if self.system.origin_urls() != ([expected], [expected]):
            raise LegacyTakeoverError("Git origin identity is not canonical")

    def _git_identity(self) -> dict[str, str]:
        value = self.system.git_identity()
        if (
            not isinstance(value, dict)
            or set(value)
            != {"branch", "head_sha", "head_tree", "local_main_sha"}
            or value.get("branch") != "refs/heads/main"
            or any(
                not isinstance(value.get(name), str)
                or GIT_SHA_RE.fullmatch(value[name]) is None
                for name in ("head_sha", "head_tree", "local_main_sha")
            )
            or value["head_sha"] != value["local_main_sha"]
        ):
            raise LegacyTakeoverError(
                "production checkout Git identity is invalid"
            )
        return dict(value)

    def _assert_checkout(
        self,
        state: dict[str, Any],
        *,
        allowed_origins: set[str],
    ) -> None:
        fetch, push = self.system.origin_urls()
        if (
            len(fetch) != 1
            or len(push) != 1
            or fetch[0] != push[0]
            or fetch[0] not in allowed_origins
        ):
            raise LegacyTakeoverError("Git origin failed takeover CAS")
        if self._git_identity() != state["git_identity"]:
            raise LegacyTakeoverError("production checkout HEAD/tree/main drifted")
        if not self.system.worktree_clean():
            raise LegacyTakeoverError(
                "production checkout has tracked or non-ignored untracked changes"
            )
        if (
            state["restore_phase"] is None
            and state["apply_phase"] != "complete"
        ):
            self._verify_original_control_layout(state)
            self._verify_original_checkout_permissions(state)
        expected_ignored = sorted(
            (
                move["path"]
                for move in state["moves"]
                if (self.repository / move["path"]).exists()
                or (self.repository / move["path"]).is_symlink()
            ),
            key=os.fsencode,
        )
        if self.system.ignored_paths() != expected_ignored:
            raise LegacyTakeoverError(
                "production checkout ignored inventory drifted"
            )

    def _helper_report(self) -> tuple[dict[str, Any], str]:
        report = self.system.helper_report()
        if (
            not isinstance(report, dict)
            or report.get("ready") is not True
            or report.get("executed_helpers") is not False
            or set(report.get("helpers", {})) != set(SITE_HELPERS.HELPERS)
        ):
            raise LegacyTakeoverError("site-helper readiness is incomplete")
        return report, sha256_bytes(canonical_json_bytes(report))

    def seal(self, operation_id: str, classification_digest: str) -> dict[str, Any]:
        with self._execution_lock():
            return self._seal_exclusive(operation_id, classification_digest)

    def _seal_exclusive(
        self,
        operation_id: str,
        classification_digest: str,
    ) -> dict[str, Any]:
        if OPERATION_RE.fullmatch(operation_id) is None:
            raise LegacyTakeoverError("takeover operation ID is invalid")
        state_path = self._state_path(operation_id)
        claimed = self._claim_active(operation_id)
        try:
            return self._seal_claimed(operation_id, classification_digest)
        except BaseException:
            if claimed and not (state_path.exists() or state_path.is_symlink()):
                self._release_active(operation_id)
            raise

    def _seal_claimed(
        self,
        operation_id: str,
        classification_digest: str,
    ) -> dict[str, Any]:
        state_path = self._state_path(operation_id)
        if state_path.exists() or state_path.is_symlink():
            state = self._load(operation_id)
            if state.get("classification_sha256") != classification_digest:
                raise LegacyTakeoverError("takeover classification changed")
            return state
        self._require_origin(REPOSITORY_HTTPS_URL)
        git_identity = self._git_identity()
        if not self.system.worktree_clean():
            raise LegacyTakeoverError(
                "production checkout has tracked or non-ignored untracked changes"
            )
        ignored = self.system.ignored_paths()
        classification, actual_digest = self._classification(
            expected_digest=classification_digest,
            ignored_paths=ignored,
        )
        helper_report, helper_digest = self._helper_report()
        status = _require_live_status(
            self.system.legacy_status(),
            allowed_states={"open"},
        )
        runtime_digest = SITE_HELPERS.legacy_runtime_identity(status)
        worker_unit_path = Path(status["worker_unit_path"])
        worker_unit_seal = seal_path(worker_unit_path)
        if worker_unit_seal["records"][0].get("sha256") != status[
            "worker_unit_sha256"
        ]:
            raise LegacyTakeoverError(
                "legacy status Worker unit hash differs from its path"
            )
        worker_unit_backup = (
            self.external_roots["runtime"]
            / ".takeover-metadata"
            / operation_id
            / "legacy-worker-unit.service"
        )
        if worker_unit_backup.exists() or worker_unit_backup.is_symlink():
            raise LegacyTakeoverError("Worker unit backup destination is occupied")
        control_snapshot = self._snapshot_control_layout()
        control_layout: list[dict[str, Any]] = []
        for index, snapshot in enumerate(control_snapshot):
            backup = (
                self.external_roots["runtime"]
                / ".takeover-metadata"
                / operation_id
                / "prior-control"
                / str(index)
            )
            trash = (
                self.external_roots["runtime"]
                / ".takeover-control-trash"
                / operation_id
                / str(index)
            )
            if (
                backup.exists()
                or backup.is_symlink()
                or trash.exists()
                or trash.is_symlink()
            ):
                raise LegacyTakeoverError(
                    "bootstrap control backup destination is occupied"
                )
            control_layout.append(
                {
                    "index": index,
                    **snapshot,
                    "backup": str(backup),
                    "trash": str(trash),
                    "restore_status": "pending",
                }
            )
        checkout_permissions = [
            {**record, "restore_status": "pending"}
            for record in snapshot_checkout_permissions(
                self.repository,
                excluded_paths=[
                    record["path"] for record in classification["paths"]
                ],
            )
        ]
        moves: list[dict[str, Any]] = []
        for index, record in enumerate(classification["paths"]):
            source = self.repository / record["path"]
            expected = seal_path(source)
            destination = (
                self.external_roots[record["class"]]
                / operation_id
                / record["path"]
            )
            if destination.exists() or destination.is_symlink():
                raise LegacyTakeoverError(
                    f"external destination is not empty: {destination}"
                )
            moves.append(
                {
                    "index": index,
                    "path": record["path"],
                    "class": record["class"],
                    "destination": str(destination),
                    "source_trash": str(
                        self.repository
                        / ".git/nexpoly-legacy-takeover-trash"
                        / operation_id
                        / f"source-{index}"
                    ),
                    "destination_trash": str(
                        self.external_roots[record["class"]]
                        / ".takeover-trash"
                        / operation_id
                        / f"destination-{index}"
                    ),
                    "seal": expected,
                    "status": "pending",
                    "restore_status": "pending",
                }
            )
        if self.system.ignored_paths() != ignored:
            raise LegacyTakeoverError("ignored path inventory changed while sealing")
        if self._git_identity() != git_identity or not self.system.worktree_clean():
            raise LegacyTakeoverError("production checkout changed while sealing")
        for move in moves:
            verify_path_seal(self.repository / move["path"], move["seal"])
        verify_path_seal(worker_unit_path, worker_unit_seal)
        second_status = _require_live_status(
            self.system.legacy_status(),
            expected_runtime_digest=runtime_digest,
            expected_process=_runtime_process_identity(status),
            allowed_states={"open"},
        )
        if second_status != status:
            raise LegacyTakeoverError("legacy runtime changed while sealing")
        _, second_helper_digest = self._helper_report()
        if second_helper_digest != helper_digest:
            raise LegacyTakeoverError("site-helper installation changed while sealing")
        second_control_layout = self._snapshot_control_layout()
        if second_control_layout != control_snapshot:
            raise LegacyTakeoverError(
                "bootstrap control layout changed while sealing"
            )
        if snapshot_checkout_permissions(
            self.repository,
            expected_paths=[
                record["path"] for record in checkout_permissions
            ],
        ) != [
            self._checkout_permission_identity(record)
            for record in checkout_permissions
        ]:
            raise LegacyTakeoverError(
                "production checkout permissions changed while sealing"
            )
        state = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_id,
            "sealed_at": utc_now(),
            "repository": str(self.repository),
            "generation": 0,
            "apply_phase": "sealed",
            "restore_phase": None,
            "classification_sha256": actual_digest,
            "classification_review_id": classification["review_id"],
            "classification_paths": classification["paths"],
            "origin_before": REPOSITORY_HTTPS_URL,
            "origin_after": REPOSITORY_SSH_URL,
            "git_identity": git_identity,
            "helper_report_sha256": helper_digest,
            "helper_hashes": {
                name: helper_report["helpers"][name]["sha256"]
                for name in sorted(helper_report["helpers"])
            },
            "runtime_identity_sha256": runtime_digest,
            "runtime_evidence_sha256": sha256_bytes(canonical_json_bytes(status)),
            "runtime": status,
            "worker_unit_seal": worker_unit_seal,
            "worker_unit_backup": str(worker_unit_backup),
            "control_layout": control_layout,
            "control_layout_sha256": self._control_layout_digest(
                control_layout
            ),
            "checkout_permissions": checkout_permissions,
            "checkout_permissions_sha256": checkout_permissions_digest(
                checkout_permissions
            ),
            "moves": moves,
        }
        self._save(state)
        self._checkpoint("sealed")
        return state

    def _verify_static_evidence(self, state: dict[str, Any]) -> None:
        _validate_private_file(self.classification_path)
        if sha256_file(self.classification_path) != state["classification_sha256"]:
            raise LegacyTakeoverError("classification changed after sealing")
        _, helper_digest = self._helper_report()
        if helper_digest != state["helper_report_sha256"]:
            raise LegacyTakeoverError("site-helper installation changed after sealing")

    def _transition(
        self,
        state: dict[str, Any],
        field: str,
        value: str,
        label: str,
    ) -> None:
        state[field] = value
        self._save(state)
        self._checkpoint(label)

    def _stop_action(
        self,
        state: dict[str, Any],
        *,
        intent: str,
        complete: str,
        action: Callable[[], None],
    ) -> None:
        if state["apply_phase"] != intent:
            self._transition(state, "apply_phase", intent, intent)
        self._assert_checkout(
            state,
            allowed_origins={REPOSITORY_HTTPS_URL},
        )
        action()
        self._checkpoint(f"{intent}:action")
        self._transition(state, "apply_phase", complete, complete)

    def _backup_control_layout(self, state: dict[str, Any]) -> None:
        self._verify_original_control_layout(state)
        for record in state["control_layout"]:
            source = self.runtime_root / record["relative_path"]
            backup = Path(record["backup"])
            if record["present"]:
                _ensure_copy(
                    source,
                    backup,
                    record["seal"],
                    stage_label=(
                        f"{state['operation_id']}-prior-control-"
                        f"{record['index']}"
                    ),
                    private_anchor=self.external_roots["runtime"],
                )
                self._checkpoint(
                    f"control-layout-{record['index']}:backup-ready"
                )
            elif (
                source.exists()
                or source.is_symlink()
                or backup.exists()
                or backup.is_symlink()
            ):
                raise LegacyTakeoverError(
                    "absent bootstrap control appeared during backup"
                )

    def _applied_record_digest(self, state: dict[str, Any]) -> str:
        evidence = {
            "schema_version": 1,
            "operation_id": state["operation_id"],
            "classification_sha256": state["classification_sha256"],
            "classification_review_id": state["classification_review_id"],
            "git_identity": state["git_identity"],
            "helper_report_sha256": state["helper_report_sha256"],
            "runtime_identity_sha256": state["runtime_identity_sha256"],
            "runtime_evidence_sha256": state["runtime_evidence_sha256"],
            "pre_stopped_fence_sha256": state["pre_stopped_fence_sha256"],
            "control_layout_sha256": state["control_layout_sha256"],
            "checkout_permissions_sha256": state[
                "checkout_permissions_sha256"
            ],
            "origin_before": REPOSITORY_HTTPS_URL,
            "origin_after": REPOSITORY_SSH_URL,
            "moves": [
                {
                    "index": move["index"],
                    "path": move["path"],
                    "class": move["class"],
                    "destination": move["destination"],
                    "seal_sha256": move["seal"]["digest"],
                }
                for move in state["moves"]
            ],
        }
        return sha256_bytes(canonical_json_bytes(evidence))

    def _restored_terminal_digest(self, state: dict[str, Any]) -> str:
        replacement_layout = state.get("control_layout_replacement") or []
        evidence = {
            "schema_version": 1,
            "operation_id": state["operation_id"],
            "applied_record_sha256": state.get("applied_record_sha256"),
            "pre_stopped_fence": state.get("pre_stopped_fence"),
            "pre_stopped_fence_sha256": state.get(
                "pre_stopped_fence_sha256"
            ),
            "classification_sha256": state["classification_sha256"],
            "git_identity": state["git_identity"],
            "origin": REPOSITORY_HTTPS_URL,
            "restore_evidence_sha256": state["restore_evidence_sha256"],
            "worker_unit_seal_sha256": state["worker_unit_seal"]["digest"],
            "worker_unit_replacement_sha256": state.get(
                "worker_unit_replacement_sha256"
            ),
            "control_layout_sha256": state["control_layout_sha256"],
            "control_layout_replacement_sha256": state.get(
                "control_layout_replacement_sha256"
            ),
            "checkout_permissions_sha256": state[
                "checkout_permissions_sha256"
            ],
            "checkout_permissions_replacement_sha256": state.get(
                "checkout_permissions_replacement_sha256"
            ),
            "preserved_replacement_controls": [
                {
                    "relative_path": replacement["relative_path"],
                    "archive": str(
                        self.external_roots["runtime"]
                        / ".takeover-preserved-control"
                        / state["operation_id"]
                        / str(index)
                    ),
                    "seal_sha256": replacement["seal"]["digest"],
                }
                for index, replacement in enumerate(replacement_layout)
                if replacement["relative_path"]
                in PRESERVED_CONTROL_LAYOUT_PATHS
                and replacement["present"]
                and not self._same_control_layout_record(
                    state["control_layout"][index],
                    replacement,
                )
            ],
            "restored_paths": [
                {
                    "path": move["path"],
                    "seal_sha256": move["seal"]["digest"],
                }
                for move in state["moves"]
            ],
        }
        return sha256_bytes(canonical_json_bytes(evidence))

    def _externalize_move(
        self,
        state: dict[str, Any],
        move: dict[str, Any],
    ) -> None:
        source = self.repository / move["path"]
        destination = Path(move["destination"])
        label = f"move-{move['index']}"
        if move["status"] == "pending":
            move["status"] = "copy-intent"
            self._save(state)
            self._checkpoint(f"{label}:copy-intent")
        if move["status"] == "copy-intent":
            _ensure_copy(
                source,
                destination,
                move["seal"],
                stage_label=f"{state['operation_id']}-{move['index']}",
                private_anchor=self.external_roots[move["class"]],
            )
            self._checkpoint(f"{label}:copy-action")
            move["status"] = "destination-ready"
            self._save(state)
            self._checkpoint(f"{label}:destination-ready")
        if move["status"] == "destination-ready":
            move["status"] = "source-remove-intent"
            self._save(state)
            self._checkpoint(f"{label}:source-remove-intent")
        if move["status"] == "source-remove-intent":
            verify_path_seal(destination, move["seal"])
            _ensure_detached_removed(
                source,
                Path(move["source_trash"]),
                move["seal"],
                private_anchor=(
                    self.repository / ".git/nexpoly-legacy-takeover-trash"
                ),
                after_detach=lambda: self._checkpoint(
                    f"{label}:source-detached"
                ),
            )
            self._checkpoint(f"{label}:source-remove-action")
            move["status"] = "externalized"
            self._save(state)
            self._checkpoint(f"{label}:externalized")
        if move["status"] != "externalized":
            raise LegacyTakeoverError("legacy path did not externalize")
        verify_path_seal(destination, move["seal"])
        if source.exists() or source.is_symlink():
            raise LegacyTakeoverError("externalized source path reappeared")

    def apply(self, operation_id: str) -> dict[str, Any]:
        with self._execution_lock():
            return self._apply_exclusive(operation_id)

    def _apply_exclusive(self, operation_id: str) -> dict[str, Any]:
        existing = self._load(operation_id)
        if existing["restore_phase"] is not None:
            raise LegacyTakeoverError("takeover restore has already started")
        self._claim_active(operation_id)
        state = self._load(operation_id)
        if state["apply_phase"] == "complete":
            self._assert_checkout(
                state,
                allowed_origins={REPOSITORY_SSH_URL},
            )
            self._release_active(operation_id)
            return state
        self._verify_static_evidence(state)
        runtime = state["runtime"]
        runtime_digest = state["runtime_identity_sha256"]
        expected_process = _runtime_process_identity(runtime)
        while state["apply_phase"] != "complete":
            phase = state["apply_phase"]
            self._assert_checkout(
                state,
                allowed_origins=(
                    {REPOSITORY_HTTPS_URL, REPOSITORY_SSH_URL}
                    if phase == "origin-switch-intent"
                    else {REPOSITORY_HTTPS_URL}
                ),
            )
            if phase == "sealed":
                for move in state["moves"]:
                    verify_path_seal(
                        self.repository / move["path"],
                        move["seal"],
                    )
                current = _require_live_status(
                    self.system.legacy_status(),
                    expected_runtime_digest=runtime_digest,
                    expected_process=expected_process,
                    allowed_states={"open"},
                )
                if current != runtime:
                    raise LegacyTakeoverError("legacy runtime changed after sealing")
                self._transition(
                    state,
                    "apply_phase",
                    "drain-intent",
                    "drain-intent",
                )
                continue
            if phase == "drain-intent":
                self._assert_checkout(
                    state,
                    allowed_origins={REPOSITORY_HTTPS_URL},
                )
                zero_evidence = self.system.drain()
                try:
                    zero_evidence = SITE_HELPERS.validate_active_jobs(
                        zero_evidence
                    )
                except SITE_HELPERS.SiteHelperContractError as exc:
                    raise LegacyTakeoverError(str(exc)) from exc
                if (
                    zero_evidence.get("active_jobs_schema_version") != 2
                    or zero_evidence.get("active_total") != 0
                ):
                    raise LegacyTakeoverError(
                        "takeover drain evidence is not canonical zero"
                    )
                self._checkpoint("drain-intent:action")
                isolated = _require_live_status(
                    self.system.legacy_status(),
                    expected_runtime_digest=runtime_digest,
                    expected_process=expected_process,
                    allowed_states={"isolated"},
                )
                state["drained_evidence_sha256"] = sha256_bytes(
                    canonical_json_bytes(isolated)
                )
                state["active_jobs_zero"] = zero_evidence
                state["active_jobs_zero_sha256"] = sha256_bytes(
                    canonical_json_bytes(zero_evidence)
                )
                state["drained_runtime"] = isolated
                state["drained_at"] = utc_now()
                self._transition(state, "apply_phase", "drained", "drained")
                continue
            if phase == "drained":
                self._stop_action(
                    state,
                    intent="web-stop-intent",
                    complete="web-stopped",
                    action=lambda: self.system.stop_container("web", runtime),
                )
                continue
            if phase == "web-stop-intent":
                self._stop_action(
                    state,
                    intent="web-stop-intent",
                    complete="web-stopped",
                    action=lambda: self.system.stop_container("web", runtime),
                )
                continue
            if phase == "web-stopped":
                self._stop_action(
                    state,
                    intent="backend-stop-intent",
                    complete="backend-stopped",
                    action=lambda: self.system.stop_container("backend", runtime),
                )
                continue
            if phase == "backend-stop-intent":
                self._stop_action(
                    state,
                    intent="backend-stop-intent",
                    complete="backend-stopped",
                    action=lambda: self.system.stop_container("backend", runtime),
                )
                continue
            if phase == "backend-stopped":
                self._stop_action(
                    state,
                    intent="worker-stop-intent",
                    complete="runtime-stopped",
                    action=lambda: self.system.stop_worker(runtime),
                )
                continue
            if phase == "worker-stop-intent":
                self._stop_action(
                    state,
                    intent="worker-stop-intent",
                    complete="runtime-stopped",
                    action=lambda: self.system.stop_worker(runtime),
                )
                continue
            if phase == "runtime-stopped":
                runtime_fence = self.system.assert_runtime_stopped(runtime)
                stopped = _require_live_status(
                    self.system.legacy_status(),
                    expected_runtime_digest=runtime_digest,
                    allowed_states={"stopped"},
                )
                unit_path = Path(runtime["worker_unit_path"])
                verify_path_seal(unit_path, state["worker_unit_seal"])
                _ensure_copy(
                    unit_path,
                    Path(state["worker_unit_backup"]),
                    state["worker_unit_seal"],
                    stage_label=(
                        f"{state['operation_id']}-legacy-worker-unit"
                    ),
                    private_anchor=self.external_roots["runtime"],
                )
                self._checkpoint("worker-unit-backup:ready")
                self._backup_control_layout(state)
                if "pre_stopped_fence" not in state:
                    stopped_at = utc_now()
                    fence = {
                        "schema_version": 1,
                        "operation_id": state["operation_id"],
                        "captured_at": stopped_at,
                        "git_identity": state["git_identity"],
                        "helper_report_sha256": state[
                            "helper_report_sha256"
                        ],
                        "runtime_identity_sha256": runtime_digest,
                        "active_jobs_zero": state["active_jobs_zero"],
                        "active_jobs_zero_sha256": state[
                            "active_jobs_zero_sha256"
                        ],
                        "isolated_runtime": state["drained_runtime"],
                        "stopped_runtime": stopped,
                        "runtime_fence": runtime_fence,
                        "worker_unit_backup": state["worker_unit_backup"],
                        "worker_unit_seal_sha256": state[
                            "worker_unit_seal"
                        ]["digest"],
                        "control_layout_sha256": state[
                            "control_layout_sha256"
                        ],
                        "checkout_permissions_sha256": state[
                            "checkout_permissions_sha256"
                        ],
                        "control_layout_backups": [
                            {
                                "relative_path": record["relative_path"],
                                "present": record["present"],
                                "backup": (
                                    record["backup"]
                                    if record["present"]
                                    else None
                                ),
                                "seal_sha256": (
                                    record["seal"]["digest"]
                                    if record["present"]
                                    else None
                                ),
                            }
                            for record in state["control_layout"]
                        ],
                    }
                    state["stopped_runtime"] = stopped
                    state["runtime_stopped_at"] = stopped_at
                    state["pre_stopped_fence"] = fence
                    state["pre_stopped_fence_sha256"] = sha256_bytes(
                        canonical_json_bytes(fence)
                    )
                    self._save(state)
                    self._checkpoint("pre-stopped-fence:sealed")
                elif (
                    state["pre_stopped_fence"].get("runtime_fence")
                    != runtime_fence
                    or state.get("stopped_runtime") != stopped
                ):
                    raise LegacyTakeoverError(
                        "pre-stopped runtime fence drifted"
                    )
                self._transition(
                    state,
                    "apply_phase",
                    "externalizing",
                    "externalizing",
                )
                continue
            if phase == "externalizing":
                for move in state["moves"]:
                    self._assert_checkout(
                        state,
                        allowed_origins={REPOSITORY_HTTPS_URL},
                    )
                    self.system.assert_runtime_stopped(runtime)
                    self._externalize_move(state, move)
                self.system.assert_runtime_stopped(runtime)
                self._transition(
                    state,
                    "apply_phase",
                    "externalized",
                    "externalized",
                )
                continue
            if phase == "externalized":
                self._assert_checkout(
                    state,
                    allowed_origins={REPOSITORY_HTTPS_URL},
                )
                self.system.assert_runtime_stopped(runtime)
                if self.system.ignored_paths():
                    raise LegacyTakeoverError(
                        "ignored paths remain after reviewed externalization"
                    )
                self._transition(
                    state,
                    "apply_phase",
                    "origin-switch-intent",
                    "origin-switch-intent",
                )
                continue
            if phase == "origin-switch-intent":
                self._assert_checkout(
                    state,
                    allowed_origins={
                        REPOSITORY_HTTPS_URL,
                        REPOSITORY_SSH_URL,
                    },
                )
                self.system.switch_origin(
                    REPOSITORY_HTTPS_URL,
                    REPOSITORY_SSH_URL,
                )
                self._checkpoint("origin-switch-intent:action")
                self._assert_checkout(
                    state,
                    allowed_origins={REPOSITORY_SSH_URL},
                )
                state["applied_record_sha256"] = self._applied_record_digest(
                    state
                )
                self._transition(
                    state,
                    "apply_phase",
                    "complete",
                    "complete",
                )
                continue
            raise LegacyTakeoverError(f"unsupported apply phase: {phase}")
        self._release_active(operation_id)
        return state

    def _restore_move(
        self,
        state: dict[str, Any],
        move: dict[str, Any],
    ) -> None:
        source = self.repository / move["path"]
        destination = Path(move["destination"])
        label = f"restore-move-{move['index']}"
        if move["restore_status"] == "pending":
            if source.exists() or source.is_symlink():
                verify_path_seal(source, move["seal"])
                move["restore_status"] = "source-ready"
                self._save(state)
                self._checkpoint(f"{label}:source-ready")
            else:
                move["restore_status"] = "copy-intent"
                self._save(state)
                self._checkpoint(f"{label}:copy-intent")
        if move["restore_status"] == "copy-intent":
            _ensure_copy(
                destination,
                source,
                move["seal"],
                stage_label=f"restore-{state['operation_id']}-{move['index']}",
                require_private_parent=False,
            )
            self._checkpoint(f"{label}:copy-action")
            move["restore_status"] = "source-ready"
            self._save(state)
            self._checkpoint(f"{label}:source-ready")
        if move["restore_status"] == "source-ready":
            verify_path_seal(source, move["seal"])
            source_trash = Path(move["source_trash"])
            if source_trash.exists() or source_trash.is_symlink():
                _remove_tree(source_trash)
            move["restore_status"] = "destination-remove-intent"
            self._save(state)
            self._checkpoint(f"{label}:destination-remove-intent")
        if move["restore_status"] == "destination-remove-intent":
            _ensure_detached_removed(
                destination,
                Path(move["destination_trash"]),
                move["seal"],
                private_anchor=(
                    self.external_roots[move["class"]]
                    / ".takeover-trash"
                ),
                after_detach=lambda: self._checkpoint(
                    f"{label}:destination-detached"
                ),
            )
            self._checkpoint(f"{label}:destination-remove-action")
            move["restore_status"] = "restored"
            self._save(state)
            self._checkpoint(f"{label}:restored")
        if move["restore_status"] != "restored":
            raise LegacyTakeoverError("legacy path did not restore")
        verify_path_seal(source, move["seal"])
        if destination.exists() or destination.is_symlink():
            raise LegacyTakeoverError("external destination remains after restore")

    def _bind_control_layout_replacement(
        self,
        state: dict[str, Any],
        expected_digest: str | None,
    ) -> None:
        if (
            expected_digest is not None
            and DIGEST_RE.fullmatch(expected_digest) is None
        ):
            raise LegacyTakeoverError(
                "bootstrap control replacement digest is invalid"
            )
        stored = state.get("control_layout_replacement")
        stored_digest = state.get("control_layout_replacement_sha256")
        if stored is not None:
            if (
                expected_digest is not None
                and expected_digest != stored_digest
            ):
                raise LegacyTakeoverError(
                    "bootstrap control replacement digest changed"
                )
            if state["restore_phase"] in {
                None,
                "origin-restore-intent",
                "origin-restored",
                "files-restoring",
                "files-restored",
            }:
                current = self._snapshot_control_layout()
                if (
                    self._control_layout_digest(current) != stored_digest
                    or any(
                        not self._same_control_layout_record(expected, actual)
                        for expected, actual in zip(
                            stored,
                            current,
                            strict=True,
                        )
                    )
                ):
                    raise LegacyTakeoverError(
                        "bootstrap control replacement changed before restore"
                    )
            elif state["restore_phase"] in {
                "control-layout-restored",
                "worker-unit-restore-intent",
                "worker-unit-restored",
                "runtime-restore-intent",
                "restored",
            }:
                self._verify_original_control_layout(state)
            return

        current = self._snapshot_control_layout()
        current_digest = self._control_layout_digest(current)
        original_digest = state["control_layout_sha256"]
        if current_digest == original_digest and all(
            self._same_control_layout_record(original, actual)
            for original, actual in zip(
                state["control_layout"],
                current,
                strict=True,
            )
        ):
            if (
                expected_digest is not None
                and expected_digest != current_digest
            ):
                raise LegacyTakeoverError(
                    "bootstrap control replacement CAS digest differs"
                )
            return
        if expected_digest is None:
            raise LegacyTakeoverError(
                "changed bootstrap controls have no parent CAS digest"
            )
        if expected_digest != current_digest:
            raise LegacyTakeoverError(
                "bootstrap control replacement failed compare-and-swap"
            )
        state["control_layout_replacement"] = current
        state["control_layout_replacement_sha256"] = current_digest
        self._save(state)

    def _bind_checkout_permissions_replacement(
        self,
        state: dict[str, Any],
        expected_digest: str | None,
    ) -> None:
        if (
            expected_digest is not None
            and DIGEST_RE.fullmatch(expected_digest) is None
        ):
            raise LegacyTakeoverError(
                "checkout permission replacement digest is invalid"
            )
        stored = state.get("checkout_permissions_replacement")
        stored_digest = state.get(
            "checkout_permissions_replacement_sha256"
        )
        if stored is not None:
            if (
                expected_digest is not None
                and expected_digest != stored_digest
            ):
                raise LegacyTakeoverError(
                    "checkout permission replacement digest changed"
                )
            if state["restore_phase"] in {
                None,
                "origin-restore-intent",
                "origin-restored",
            }:
                current = self._current_checkout_permissions(state)
                if (
                    checkout_permissions_digest(current) != stored_digest
                    or current != stored
                ):
                    raise LegacyTakeoverError(
                        "checkout permissions changed before restore"
                    )
            elif state["restore_phase"] in {
                "checkout-permissions-restored",
                "files-restoring",
                "files-restored",
                "control-layout-restore-intent",
                "control-layout-restored",
                "worker-unit-restore-intent",
                "worker-unit-restored",
                "runtime-restore-intent",
                "restored",
            }:
                self._verify_original_checkout_permissions(state)
            return
        current = self._current_checkout_permissions(state)
        current_digest = checkout_permissions_digest(current)
        original = [
            self._checkout_permission_identity(record)
            for record in state["checkout_permissions"]
        ]
        if current_digest == state["checkout_permissions_sha256"] and (
            current == original
        ):
            if (
                expected_digest is not None
                and expected_digest != current_digest
            ):
                raise LegacyTakeoverError(
                    "checkout permission replacement CAS digest differs"
                )
            return
        if expected_digest is None:
            raise LegacyTakeoverError(
                "changed checkout permissions have no parent CAS digest"
            )
        if current_digest != expected_digest:
            raise LegacyTakeoverError(
                "checkout permission replacement failed compare-and-swap"
            )
        state["checkout_permissions_replacement"] = current
        state["checkout_permissions_replacement_sha256"] = current_digest
        self._save(state)

    def _restore_checkout_permissions(
        self,
        state: dict[str, Any],
    ) -> None:
        replacement_records = state.get(
            "checkout_permissions_replacement"
        )
        ordered = sorted(
            range(len(state["checkout_permissions"])),
            key=lambda index: (
                len(
                    PurePosixPath(
                        state["checkout_permissions"][index]["path"]
                    ).parts
                ),
                os.fsencode(
                    state["checkout_permissions"][index]["path"]
                ),
            ),
            reverse=True,
        )
        for index in ordered:
            record = state["checkout_permissions"][index]
            replacement = (
                replacement_records[index]
                if replacement_records is not None
                else self._checkout_permission_identity(record)
            )
            original = self._checkout_permission_identity(record)
            path = (
                self.repository
                if record["path"] == "."
                else self.repository / record["path"]
            )
            current = _permission_record(
                self.repository,
                path,
                record["path"],
            )
            label = f"checkout-permission-{index}"
            if record["restore_status"] == "pending":
                if current != replacement:
                    raise LegacyTakeoverError(
                        "checkout permission failed replacement CAS"
                    )
                if current == original:
                    record["restore_status"] = "restored"
                    self._save(state)
                    self._checkpoint(
                        f"restore:{label}:already-original"
                    )
                else:
                    record["restore_status"] = "restore-intent"
                    self._save(state)
                    self._checkpoint(f"restore:{label}:intent")
            if record["restore_status"] == "restore-intent":
                current = _permission_record(
                    self.repository,
                    path,
                    record["path"],
                )
                if current != original:
                    if current != replacement:
                        raise LegacyTakeoverError(
                            "checkout permission changed during restore"
                        )
                    if (
                        current["uid"],
                        current["gid"],
                    ) != (original["uid"], original["gid"]):
                        _set_owner(
                            path,
                            original["uid"],
                            original["gid"],
                            symlink=original["type"] == "symlink",
                        )
                    if original["type"] != "symlink":
                        os.chmod(
                            path,
                            int(original["mode"], 8),
                            follow_symlinks=False,
                        )
                    _fsync_directory(path.parent)
                    self._checkpoint(f"restore:{label}:action")
                if _permission_record(
                    self.repository,
                    path,
                    record["path"],
                ) != original:
                    raise LegacyTakeoverError(
                        "checkout permission did not restore"
                    )
                record["restore_status"] = "restored"
                self._save(state)
                self._checkpoint(f"restore:{label}:restored")
            if record["restore_status"] != "restored" or _permission_record(
                self.repository,
                path,
                record["path"],
            ) != original:
                raise LegacyTakeoverError(
                    "checkout permission restore is incomplete"
                )
        self._verify_original_checkout_permissions(state)

    def _restore_control_layout_record(
        self,
        state: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        replacement_records = state.get("control_layout_replacement")
        replacement = (
            replacement_records[record["index"]]
            if replacement_records is not None
            else record
        )
        path = self.runtime_root / record["relative_path"]
        backup = Path(record["backup"])
        trash = Path(record["trash"])
        label = f"control-layout-{record['index']}"
        if record["restore_status"] == "pending":
            current_present = path.exists() or path.is_symlink()
            if current_present is not replacement["present"]:
                raise LegacyTakeoverError(
                    "bootstrap control changed before compare-and-swap"
                )
            if current_present:
                verify_path_seal(path, replacement["seal"])
            if self._same_control_layout_record(record, replacement):
                record["restore_status"] = "restored"
                self._save(state)
                self._checkpoint(f"restore:{label}:already-original")
            else:
                record["restore_status"] = "remove-intent"
                self._save(state)
                self._checkpoint(f"restore:{label}:remove-intent")
        if record["restore_status"] == "remove-intent":
            if replacement["present"]:
                if (
                    record["relative_path"]
                    in PRESERVED_CONTROL_LAYOUT_PATHS
                ):
                    archive = (
                        self.external_roots["runtime"]
                        / ".takeover-preserved-control"
                        / state["operation_id"]
                        / str(record["index"])
                    )
                    _ensure_detached_archived(
                        path,
                        archive,
                        replacement["seal"],
                        private_anchor=self.external_roots["runtime"],
                        after_detach=lambda: self._checkpoint(
                            f"restore:{label}:archived"
                        ),
                    )
                else:
                    _ensure_detached_removed(
                        path,
                        trash,
                        replacement["seal"],
                        private_anchor=self.external_roots["runtime"],
                        after_detach=lambda: self._checkpoint(
                            f"restore:{label}:detached"
                        ),
                    )
            elif (
                path.exists()
                or path.is_symlink()
                or trash.exists()
                or trash.is_symlink()
            ):
                raise LegacyTakeoverError(
                    "unexpected bootstrap control failed absent CAS"
                )
            self._checkpoint(f"restore:{label}:remove-action")
            record["restore_status"] = "removed"
            self._save(state)
            self._checkpoint(f"restore:{label}:removed")
        if record["restore_status"] == "removed":
            if (
                path.exists()
                or path.is_symlink()
                or trash.exists()
                or trash.is_symlink()
            ):
                raise LegacyTakeoverError(
                    "bootstrap replacement remains after removal"
                )
            record["restore_status"] = (
                "copy-intent" if record["present"] else "restored"
            )
            self._save(state)
            self._checkpoint(
                f"restore:{label}:{record['restore_status']}"
            )
        if record["restore_status"] == "copy-intent":
            verify_path_seal(backup, record["seal"])
            _ensure_copy(
                backup,
                path,
                record["seal"],
                stage_label=(
                    f"restore-{state['operation_id']}-control-"
                    f"{record['index']}"
                ),
                require_private_parent=False,
            )
            self._checkpoint(f"restore:{label}:copy-action")
            record["restore_status"] = "restored"
            self._save(state)
            self._checkpoint(f"restore:{label}:restored")
        if record["restore_status"] != "restored":
            raise LegacyTakeoverError(
                "bootstrap control layout did not restore"
            )
        present = path.exists() or path.is_symlink()
        if present is not record["present"]:
            raise LegacyTakeoverError(
                "restored bootstrap control presence differs"
            )
        if present:
            verify_path_seal(path, record["seal"])

    def _restore_control_layout(self, state: dict[str, Any]) -> None:
        for record in state["control_layout"]:
            self._restore_control_layout_record(state, record)
        self._verify_original_control_layout(state)

    def _restore_worker_unit(self, state: dict[str, Any]) -> None:
        runtime = state["runtime"]
        unit = Path(runtime["worker_unit_path"])
        expected = state["worker_unit_seal"]
        if unit.exists() or unit.is_symlink():
            actual = seal_path(unit)
            if actual == expected:
                self.system.reload_worker_manager(runtime)
                return
        else:
            actual = None
        replacement = state.get("worker_unit_replacement_sha256")
        if replacement is None:
            raise LegacyTakeoverError(
                "changed Worker unit has no bootstrap CAS digest"
            )
        if (
            actual is None
            or len(actual["records"]) != 1
            or actual["records"][0].get("type") != "file"
            or actual["records"][0].get("sha256") != replacement
        ):
            raise LegacyTakeoverError(
                "Worker unit replacement failed compare-and-swap"
            )
        backup = Path(state["worker_unit_backup"])
        verify_path_seal(backup, expected)
        stage = unit.parent / (
            f".legacy-takeover-unit-{state['operation_id']}"
        )
        if stage.exists() or stage.is_symlink():
            _remove_tree(stage)
        _copy_preserving(backup, stage)
        verify_path_seal(stage, expected)
        if seal_path(unit) != actual:
            raise LegacyTakeoverError(
                "Worker unit changed during restore compare-and-swap"
            )
        os.replace(stage, unit)
        _fsync_directory(unit.parent)
        verify_path_seal(unit, expected)
        self.system.reload_worker_manager(runtime)

    def restore(
        self,
        operation_id: str,
        *,
        expected_worker_unit_sha256: str | None = None,
        expected_control_layout_sha256: str | None = None,
        expected_checkout_permissions_sha256: str | None = None,
        parent_deploy_lock_fd: int | None = None,
    ) -> dict[str, Any]:
        with self._execution_lock(
            parent_deploy_lock_fd=parent_deploy_lock_fd,
        ):
            return self._restore_exclusive(
                operation_id,
                expected_worker_unit_sha256=expected_worker_unit_sha256,
                expected_control_layout_sha256=(
                    expected_control_layout_sha256
                ),
                expected_checkout_permissions_sha256=(
                    expected_checkout_permissions_sha256
                ),
            )

    def _restore_exclusive(
        self,
        operation_id: str,
        *,
        expected_worker_unit_sha256: str | None,
        expected_control_layout_sha256: str | None,
        expected_checkout_permissions_sha256: str | None,
    ) -> dict[str, Any]:
        self._claim_active(operation_id)
        state = self._load(operation_id)
        if expected_worker_unit_sha256 is not None:
            if DIGEST_RE.fullmatch(expected_worker_unit_sha256) is None:
                raise LegacyTakeoverError(
                    "Worker unit replacement digest is invalid"
                )
            prior = state.get("worker_unit_replacement_sha256")
            if prior not in {None, expected_worker_unit_sha256}:
                raise LegacyTakeoverError(
                    "Worker unit replacement digest changed"
                )
            if prior is None:
                state["worker_unit_replacement_sha256"] = (
                    expected_worker_unit_sha256
                )
                self._save(state)
        self._bind_control_layout_replacement(
            state,
            expected_control_layout_sha256,
        )
        self._bind_checkout_permissions_replacement(
            state,
            expected_checkout_permissions_sha256,
        )
        self._verify_static_evidence(state)
        if state["restore_phase"] == "restored":
            self._assert_checkout(
                state,
                allowed_origins={REPOSITORY_HTTPS_URL},
            )
            for move in state["moves"]:
                verify_path_seal(self.repository / move["path"], move["seal"])
            self._release_active(operation_id)
            return state
        if state["restore_phase"] is None:
            self._assert_checkout(
                state,
                allowed_origins={
                    REPOSITORY_HTTPS_URL,
                    REPOSITORY_SSH_URL,
                },
            )
            self._transition(
                state,
                "restore_phase",
                "origin-restore-intent",
                "restore:origin-restore-intent",
            )
        if state["restore_phase"] == "origin-restore-intent":
            self._assert_checkout(
                state,
                allowed_origins={
                    REPOSITORY_HTTPS_URL,
                    REPOSITORY_SSH_URL,
                },
            )
            self.system.switch_origin(
                REPOSITORY_SSH_URL,
                REPOSITORY_HTTPS_URL,
            )
            self._checkpoint("restore:origin-restore-intent:action")
            self._assert_checkout(
                state,
                allowed_origins={REPOSITORY_HTTPS_URL},
            )
            self._transition(
                state,
                "restore_phase",
                "origin-restored",
                "restore:origin-restored",
            )
        if state["restore_phase"] == "origin-restored":
            self._transition(
                state,
                "restore_phase",
                "checkout-permissions-restore-intent",
                "restore:checkout-permissions-restore-intent",
            )
        if (
            state["restore_phase"]
            == "checkout-permissions-restore-intent"
        ):
            self._restore_checkout_permissions(state)
            self._transition(
                state,
                "restore_phase",
                "checkout-permissions-restored",
                "restore:checkout-permissions-restored",
            )
        if state["restore_phase"] == "checkout-permissions-restored":
            self._transition(
                state,
                "restore_phase",
                "files-restoring",
                "restore:files-restoring",
            )
        if state["restore_phase"] == "files-restoring":
            for move in reversed(state["moves"]):
                self._assert_checkout(
                    state,
                    allowed_origins={REPOSITORY_HTTPS_URL},
                )
                self._restore_move(state, move)
            expected_ignored = sorted(
                (record["path"] for record in state["classification_paths"]),
                key=os.fsencode,
            )
            if self.system.ignored_paths() != expected_ignored:
                raise LegacyTakeoverError(
                    "restored ignored inventory differs from the reviewed map"
                )
            self._transition(
                state,
                "restore_phase",
                "files-restored",
                "restore:files-restored",
            )
        if state["restore_phase"] == "files-restored":
            self._transition(
                state,
                "restore_phase",
                "control-layout-restore-intent",
                "restore:control-layout-restore-intent",
            )
        if state["restore_phase"] == "control-layout-restore-intent":
            self._restore_control_layout(state)
            self._transition(
                state,
                "restore_phase",
                "control-layout-restored",
                "restore:control-layout-restored",
            )
        if state["restore_phase"] == "control-layout-restored":
            self._transition(
                state,
                "restore_phase",
                "worker-unit-restore-intent",
                "restore:worker-unit-restore-intent",
            )
        if state["restore_phase"] == "worker-unit-restore-intent":
            self._restore_worker_unit(state)
            self._checkpoint("restore:worker-unit-restore-intent:action")
            self._transition(
                state,
                "restore_phase",
                "worker-unit-restored",
                "restore:worker-unit-restored",
            )
        if state["restore_phase"] == "worker-unit-restored":
            self._transition(
                state,
                "restore_phase",
                "runtime-restore-intent",
                "restore:runtime-restore-intent",
            )
        if state["restore_phase"] == "runtime-restore-intent":
            self._assert_checkout(
                state,
                allowed_origins={REPOSITORY_HTTPS_URL},
            )
            evidence = self.system.restore_runtime(state["runtime"])
            self._checkpoint("restore:runtime-restore-intent:action")
            try:
                validated = SITE_HELPERS.validate_legacy_restore(
                    evidence,
                    expected_runtime_digest=state["runtime_identity_sha256"],
                )
            except SITE_HELPERS.SiteHelperContractError as exc:
                raise LegacyTakeoverError(str(exc)) from exc
            if validated.get("schema_version") != 2:
                raise LegacyTakeoverError("legacy restore requires evidence schema v2")
            state["restore_evidence_sha256"] = sha256_bytes(
                canonical_json_bytes(validated)
            )
            state["restored_terminal_sha256"] = (
                self._restored_terminal_digest(state)
            )
            self._transition(
                state,
                "restore_phase",
                "restored",
                "restore:restored",
            )
        self._release_active(operation_id)
        return state

    def status(self, operation_id: str) -> dict[str, Any]:
        state = self._load(operation_id)
        active = self._active_document()
        return {
            "schema_version": 2,
            "operation_id": operation_id,
            "active": (
                active is not None
                and active["operation_id"] == operation_id
            ),
            "apply_phase": state["apply_phase"],
            "restore_phase": state["restore_phase"],
            "generation": state["generation"],
            "classification_sha256": state["classification_sha256"],
            "runtime_identity_sha256": state["runtime_identity_sha256"],
            "git_identity": state["git_identity"],
            "applied_record_sha256": state.get("applied_record_sha256"),
            "pre_stopped_fence": state.get("pre_stopped_fence"),
            "pre_stopped_fence_sha256": state.get(
                "pre_stopped_fence_sha256"
            ),
            "control_layout_sha256": state["control_layout_sha256"],
            "control_layout_replacement_sha256": state.get(
                "control_layout_replacement_sha256"
            ),
            "checkout_permissions_sha256": state[
                "checkout_permissions_sha256"
            ],
            "checkout_permissions_replacement_sha256": state.get(
                "checkout_permissions_replacement_sha256"
            ),
            "restored_terminal_sha256": state.get(
                "restored_terminal_sha256"
            ),
            "moves": [
                {
                    "path": move["path"],
                    "class": move["class"],
                    "status": move["status"],
                    "restore_status": move["restore_status"],
                }
                for move in state["moves"]
            ],
        }


def _print_json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crash-safe Nexpoly legacy checkout takeover",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    seal = subparsers.add_parser("seal")
    seal.add_argument("--operation-id", required=True)
    seal.add_argument("--classification-sha256", required=True)
    for name in ("apply", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--operation-id", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--operation-id", required=True)
    restore.add_argument("--expected-worker-unit-sha256")
    restore.add_argument("--expected-control-layout-sha256")
    restore.add_argument("--expected-checkout-permissions-sha256")
    restore.add_argument("--parent-deploy-lock-fd", type=int)
    return parser


def _die(message: str) -> NoReturn:
    print(f"legacy-takeover: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        _ensure_private_directory(PRODUCTION_RUNTIME_ROOT, create=False)
        system = LiveSystem(PRODUCTION_REPOSITORY, PRODUCTION_RUNTIME_ROOT)
        controller = LegacyTakeover(
            repository=PRODUCTION_REPOSITORY,
            runtime_root=PRODUCTION_RUNTIME_ROOT,
            external_roots=EXTERNAL_ROOTS,
            system=system,
        )
        if arguments.command == "seal":
            controller.seal(
                arguments.operation_id,
                arguments.classification_sha256,
            )
        elif arguments.command == "apply":
            controller.apply(arguments.operation_id)
        elif arguments.command == "restore":
            controller.restore(
                arguments.operation_id,
                expected_worker_unit_sha256=(
                    arguments.expected_worker_unit_sha256
                ),
                expected_control_layout_sha256=(
                    arguments.expected_control_layout_sha256
                ),
                expected_checkout_permissions_sha256=(
                    arguments.expected_checkout_permissions_sha256
                ),
                parent_deploy_lock_fd=(
                    arguments.parent_deploy_lock_fd
                ),
            )
        result = controller.status(arguments.operation_id)
        _print_json(result)
        return 0
    except LegacyTakeoverError as exc:
        _die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
