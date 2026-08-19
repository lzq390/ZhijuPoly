#!/usr/bin/env python3
"""Install the one-time bootstrap-router successor without moving active-control.

The transaction installs the reviewed target control release and snapshot
verifier, publishes an immutable selector-swap intent, atomically replaces only
``runtime/bin/control_runtime_selector.py``, and finally publishes a create-once
successor authority. Worker routes remain on the adopted active-control release;
the new selector routes only the first formal deployment to the sealed target.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
import production_git_snapshot as snapshot  # noqa: E402


PRODUCTION_ROOT = snapshot.PRODUCTION_ROOT
RUNTIME_ROOT = snapshot.RUNTIME_ROOT
REPOSITORY_ROOT = SCRIPT_ROOT.parent
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
AUTHORITY_KIND = "manual-runtime-adoption-bootstrap-router-successor"
POLICY = "nexpoly-bootstrap-router-successor-v1"
OPERATION_RE = re.compile(r"adopt-router-[a-z0-9][a-z0-9._-]{7,95}\Z")
SHA_RE = snapshot.SHA_RE
DIGEST_RE = snapshot.DIGEST_RE
UTC_RE = snapshot.UTC_RE
AUTHORITY_RELATIVE = Path("state/bootstrap-router-successor.json")
INTENT_RELATIVE = Path("state/bootstrap-router-successor-intent.json")
TRANSACTION_ROOT_RELATIVE = Path("state/bootstrap-router-successor-transactions")
INSTALL_ROOT_RELATIVE = Path("bootstrap-router-successors")
LOCK_RELATIVE = Path("state/bootstrap-router-successor.lock")
DEPLOY_LOCK_RELATIVE = Path("state/deploy.lock")
BOOTSTRAP_RELATIVE = Path("state/bootstrap-control.json")
ACTIVE_CONTROL_RELATIVE = Path("state/active-control.json")
CURRENT_STATE_RELATIVE = Path("state/current-deployment.json")
DEPLOY_MARKER_RELATIVE = Path("state/deploy-in-progress.json")
ROUTER_FENCE_RELATIVE = Path("state/contract-0012-in-progress.json")
SNAPSHOT_AUTHORITY_RELATIVE = snapshot.AUTHORITY_RELATIVE_PATH
SOURCE_SUCCESSOR_RELATIVE = Path(
    "state/adopted-git-permission-source-successor.json"
)
UNIT_PERMISSION_RELATIVE = Path("state/adopted-unit-permissions.json")
SELECTOR_RELATIVE = Path("bin/control_runtime_selector.py")
CONTROL_RELEASE_ROOT_RELATIVE = Path("control-releases")
CONTROL_MANIFEST_NAME = "CONTROL-MANIFEST.json"
BOOTSTRAP_IMMUTABLE_FILES = {
    "control_runtime_selector.py",
    "nexpoly-pull-deploy",
    "nexpoly-postgres-media-evidence",
    "nexpoly-production-readiness",
    "nexpoly-pull-contract-0012",
    "nexpoly-reconcile-production-0005-polytao-alias",
}
ROUTER_SOURCE_FILES = {
    "production_git_snapshot.py": "scripts/production_git_snapshot.py",
    "restore_production_git_snapshot.py": (
        "scripts/restore_production_git_snapshot.py"
    ),
    "reviewed-selector.py": "scripts/control_runtime_selector.py",
}
CONTROL_DATA_SOURCES = {
    "ops/config/postgres-media-authority-rules.json": (
        "postgres-media-authority-rules.json"
    ),
    "ops/config/postgres-media-audit-role.sql.example": (
        "postgres-media-audit-role.sql.example"
    ),
}
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
JOURNAL_PHASES = {
    "intent",
    "interlock-ready",
    "control-release-ready",
    "router-files-ready",
    "selector-swap-intent",
    "selector-switched",
    "authority-commit-intent",
    "authority-published",
    "completed",
}
PLAN_FIELDS = {
    "schema_version",
    "authority_kind",
    "policy",
    "operation_id",
    "target_source_sha",
    "target_source_tree",
    "bootstrap_control_sha256",
    "predecessor_selector_sha256",
    "successor_selector_sha256",
    "snapshot_authority_sha256",
    "source_successor_authority_sha256",
    "unit_permission_authority_sha256",
    "delivery_gate",
    "delivery_gate_sha256",
    "target_control_release",
    "router_files",
    "predecessor_authorities",
    "interlock",
    "mutations",
    "router_successor_impact_sha256",
}
JOURNAL_FIELDS = {
    "schema_version",
    "status",
    "phase",
    "operation_id",
    "plan",
    "plan_sha256",
    "router_successor_impact_sha256",
    "created_at",
    "completed_at",
}


class RouterSuccessorError(RuntimeError):
    """The bootstrap-router successor cannot be proven or committed safely."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_operation(value: object) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value) is None:
        raise RouterSuccessorError("bootstrap-router operation ID is invalid")
    return value


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise RouterSuccessorError(f"{label} is invalid")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise RouterSuccessorError(f"{label} is invalid")
    return value


def _canonical_bytes(value: object) -> bytes:
    return snapshot.canonical_json_bytes(value)


def _digest_bytes(value: bytes) -> str:
    return snapshot.digest_bytes(value)


def _digest_document(value: object) -> str:
    return snapshot.canonical_digest(value)


def _run_git(
    repository: Path,
    *arguments: str,
    text: bool = True,
    timeout: int = 900,
) -> subprocess.CompletedProcess[Any]:
    git_dir = repository / ".git"
    environment = {
        "USER": "devuser",
        "LOGNAME": "devuser",
        "HOME": "/nonexistent",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(repository),
        "GIT_INDEX_FILE": str(git_dir / "index"),
        "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(repository), *arguments],
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RouterSuccessorError(
            f"Git command failed: {' '.join(arguments)}"
        ) from exc


def _git_blob(repository: Path, source_sha: str, relative: str) -> bytes:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != relative
        or not relative
    ):
        raise RouterSuccessorError("reviewed source path is invalid")
    completed = _run_git(
        repository,
        "show",
        f"{source_sha}:{relative}",
        text=False,
    )
    payload = bytes(completed.stdout)
    if not payload or len(payload) > 16 * 1024 * 1024:
        raise RouterSuccessorError("reviewed source blob is invalid")
    return payload


def _private_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        return snapshot._load_private_json(path, maximum=32 * 1024 * 1024)
    except snapshot.SnapshotError as exc:
        raise RouterSuccessorError(f"runtime authority is invalid: {path}") from exc


def _private_file(path: Path, *, mode: int, maximum: int = 16 * 1024 * 1024) -> tuple[bytes, str]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RouterSuccessorError(f"private file is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
            or not 1 <= before.st_size <= maximum
        ):
            raise RouterSuccessorError(f"private file is unsafe: {path}")
        payload = bytearray()
        while len(payload) < before.st_size:
            block = os.read(descriptor, min(1024 * 1024, before.st_size - len(payload)))
            if not block:
                raise RouterSuccessorError(f"private file changed: {path}")
            payload.extend(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RouterSuccessorError(f"private file changed: {path}")
    raw = bytes(payload)
    return raw, _digest_bytes(raw)


def _file_record(relative_path: str, payload: bytes) -> dict[str, object]:
    return {
        "relative_path": relative_path,
        "sha256": _digest_bytes(payload),
        "size": len(payload),
        "mode": 0o700,
    }


def _open_lock(path: Path) -> int:
    snapshot._require_private_directory(path.parent)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RouterSuccessorError("bootstrap-router lock identity is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except OSError as exc:
        raise RouterSuccessorError("bootstrap-router lock is unavailable") from exc


def _rename_noreplace(source: Path, destination: Path) -> None:
    source_parent = snapshot._open_directory(source.parent)
    destination_parent = snapshot._open_directory(destination.parent)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RouterSuccessorError("renameat2 no-replace is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                source_parent,
                source.name.encode(),
                destination_parent,
                destination.name.encode(),
                RENAME_NOREPLACE,
            )
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        os.fsync(source_parent)
        if destination_parent != source_parent:
            os.fsync(destination_parent)
    except OSError as exc:
        raise RouterSuccessorError("create-once rename failed") from exc
    finally:
        os.close(destination_parent)
        os.close(source_parent)


def _write_private_file(path: Path, payload: bytes, mode: int) -> None:
    snapshot._require_private_directory(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RouterSuccessorError("bootstrap-router file write stalled")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError as exc:
        raise RouterSuccessorError(f"bootstrap-router file cannot be written: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    parent = snapshot._open_directory(path.parent)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _publish_create_once(path: Path, document: Mapping[str, object], operation_id: str) -> None:
    payload = _canonical_bytes(document)
    staging = path.parent / f".{path.name}.{operation_id}.create"
    if path.exists() or path.is_symlink():
        existing, _digest = _private_json(path)
        if existing != document:
            raise RouterSuccessorError("create-once router authority differs")
        if staging.exists() or staging.is_symlink():
            staged, _staged_digest = _private_json(staging)
            if staged != document:
                raise RouterSuccessorError("router authority staging differs")
            staging.unlink()
        return
    if staging.exists() or staging.is_symlink():
        try:
            staged, _staged_digest = _private_json(staging)
        except RouterSuccessorError:
            staged = None
        if staged != document:
            metadata = staging.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or staging.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise RouterSuccessorError("router authority staging is unsafe")
            staging.unlink()
    if not staging.exists():
        _write_private_file(staging, payload, 0o600)
    _rename_noreplace(staging, path)
    existing, _digest = _private_json(path)
    if existing != document:
        raise RouterSuccessorError("published router authority differs")


def _validate_source_manifest(document: object) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "protocol_version",
            "compatibility",
            "entrypoints",
            "files",
        }
        or document.get("schema_version") != 1
        or document.get("protocol_version") != 1
        or not isinstance(document.get("files"), list)
        or not 1 <= len(document["files"]) <= 64
    ):
        raise RouterSuccessorError("target control source manifest is invalid")
    compatibility = document.get("compatibility")
    compatibility_fields = {
        "handoff_protocol_versions",
        "descriptor_schema_versions",
        "current_state_schema_versions",
        "marker_schema_versions",
        "worker_slot_schema_versions",
        "prepare_abort_abi_versions",
    }
    if not isinstance(compatibility, dict) or set(compatibility) != compatibility_fields:
        raise RouterSuccessorError("target control compatibility is invalid")
    for versions in compatibility.values():
        if (
            not isinstance(versions, list)
            or not versions
            or versions != sorted(set(versions))
            or any(type(item) is not int or not 1 <= item <= 1024 for item in versions)
        ):
            raise RouterSuccessorError("target control compatibility is invalid")
    files: list[dict[str, object]] = []
    names: set[str] = set()
    for record in document["files"]:
        if not isinstance(record, dict) or set(record) != {"name", "source", "mode"}:
            raise RouterSuccessorError("target control file record is invalid")
        name = record.get("name")
        source = record.get("source")
        source_path = PurePosixPath(source) if isinstance(source, str) else None
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", name) is None
            or name in names
            or not isinstance(source, str)
            or source_path is None
            or source_path.is_absolute()
            or ".." in source_path.parts
            or not (
                source.startswith("scripts/")
                and source_path.name == name
                or CONTROL_DATA_SOURCES.get(source) == name
            )
            or record.get("mode") != 0o700
        ):
            raise RouterSuccessorError("target control file record is unsafe")
        names.add(name)
        files.append(dict(record))
    entrypoints = document.get("entrypoints")
    if not isinstance(entrypoints, dict) or not 1 <= len(entrypoints) <= 32:
        raise RouterSuccessorError("target control entrypoints are invalid")
    for role, record in entrypoints.items():
        if (
            not isinstance(role, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", role) is None
            or not isinstance(record, dict)
            or record.get("kind") not in {"python", "worker"}
        ):
            raise RouterSuccessorError("target control entrypoint is invalid")
        if record["kind"] == "python":
            if set(record) != {"kind", "file"} or record.get("file") not in names:
                raise RouterSuccessorError("target Python entrypoint is invalid")
        elif (
            set(record)
            != {"kind", "environment_loader", "launcher", "config_relative"}
            or record.get("environment_loader") not in names
            or record.get("launcher") not in names
            or not isinstance(record.get("config_relative"), str)
            or re.fullmatch(r"config/[a-z][a-z0-9_.-]{0,127}", record["config_relative"])
            is None
        ):
            raise RouterSuccessorError("target Worker entrypoint is invalid")
    deploy = entrypoints.get("deploy")
    if not isinstance(deploy, dict) or deploy.get("kind") != "python":
        raise RouterSuccessorError("target control lacks deploy entrypoint")
    return {
        "schema_version": 1,
        "protocol_version": 1,
        "compatibility": {name: list(value) for name, value in compatibility.items()},
        "entrypoints": {name: dict(value) for name, value in entrypoints.items()},
        "files": files,
    }


def _target_control_artifacts(
    source_root: Path,
    target_sha: str,
    target_tree: str,
) -> dict[str, Any]:
    source_payload = _git_blob(
        source_root,
        target_sha,
        "scripts/control-release.json",
    )
    try:
        source_manifest = _validate_source_manifest(
            json.loads(source_payload.decode("utf-8"))
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RouterSuccessorError("target control source manifest is invalid") from exc
    payloads: dict[str, bytes] = {}
    records: dict[str, dict[str, object]] = {}
    identities: dict[str, dict[str, object]] = {}
    for record in source_manifest["files"]:
        name = str(record["name"])
        source = str(record["source"])
        payload = _git_blob(source_root, target_sha, source)
        payloads[name] = payload
        identity = {
            "sha256": _digest_bytes(payload),
            "size": len(payload),
            "mode": 0o700,
        }
        identities[name] = identity
        records[name] = {"source": source, **identity}
    identity_document = {
        "schema_version": 1,
        "protocol_version": 1,
        "source_sha": target_sha,
        "source_tree": target_tree,
        "compatibility": source_manifest["compatibility"],
        "entrypoints": source_manifest["entrypoints"],
        "files": identities,
    }
    release_id = hashlib.sha256(
        json.dumps(
            identity_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    manifest = {**identity_document, "release_id": release_id}
    manifest_payload = (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )
    deploy_name = str(source_manifest["entrypoints"]["deploy"]["file"])
    compact = {
        "release_id": release_id,
        "source_sha": target_sha,
        "source_tree": target_tree,
        "manifest_sha256": _digest_bytes(manifest_payload),
        "deploy_sha256": records[deploy_name]["sha256"],
    }
    return {
        "compact": compact,
        "records": records,
        "payloads": payloads,
        "manifest": manifest,
        "manifest_payload": manifest_payload,
    }


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        try:
            snapshot._require_private_directory(path)
        except snapshot.SnapshotError as exc:
            raise RouterSuccessorError(
                f"bootstrap-router directory is unsafe: {path}"
            ) from exc
        return
    try:
        path.mkdir(mode=0o700)
    except OSError as exc:
        raise RouterSuccessorError(
            f"bootstrap-router directory cannot be created: {path}"
        ) from exc
    _ensure_private_directory(path)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = snapshot._open_directory(path)
    except snapshot.SnapshotError as exc:
        raise RouterSuccessorError(
            f"bootstrap-router directory cannot be pinned: {path}"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise RouterSuccessorError(
            f"bootstrap-router directory cannot be synchronized: {path}"
        ) from exc
    finally:
        os.close(descriptor)


def _remove_owned_tree(path: Path) -> None:
    try:
        snapshot._remove_private_tree(path)
    except snapshot.SnapshotError as exc:
        raise RouterSuccessorError(
            f"owned bootstrap-router staging cannot be removed: {path}"
        ) from exc


def _rename_exchange(first: Path, second: Path) -> None:
    first_parent = snapshot._open_directory(first.parent)
    second_parent = snapshot._open_directory(second.parent)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RouterSuccessorError("renameat2 exchange is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                first_parent,
                first.name.encode(),
                second_parent,
                second.name.encode(),
                RENAME_EXCHANGE,
            )
            != 0
        ):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        os.fsync(first_parent)
        os.fsync(second_parent)
    except OSError as exc:
        raise RouterSuccessorError("bootstrap selector exchange failed") from exc
    finally:
        os.close(second_parent)
        os.close(first_parent)


def _open_existing_lock(path: Path) -> int:
    snapshot._require_private_directory(path.parent)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RouterSuccessorError("deploy lock identity is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except OSError as exc:
        raise RouterSuccessorError("deploy lock is unavailable") from exc


def _validate_tree(
    root: Path,
    payloads: Mapping[str, bytes],
    *,
    manifest_payload: bytes | None = None,
) -> None:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise RouterSuccessorError(
            f"installed bootstrap-router tree is unavailable: {root}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or root.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RouterSuccessorError("installed bootstrap-router tree is unsafe")
    expected_names = set(payloads)
    if manifest_payload is not None:
        expected_names.add(CONTROL_MANIFEST_NAME)
    try:
        observed_names = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise RouterSuccessorError(
            "installed bootstrap-router tree cannot be enumerated"
        ) from exc
    if observed_names != expected_names:
        raise RouterSuccessorError(
            "installed bootstrap-router tree inventory differs"
        )
    for name, expected in payloads.items():
        payload, digest = _private_file(root / name, mode=0o700)
        if payload != expected or digest != _digest_bytes(expected):
            raise RouterSuccessorError(
                f"installed bootstrap-router file differs: {name}"
            )
    if manifest_payload is not None:
        payload, digest = _private_file(
            root / CONTROL_MANIFEST_NAME,
            mode=0o600,
        )
        if (
            payload != manifest_payload
            or digest != _digest_bytes(manifest_payload)
        ):
            raise RouterSuccessorError("installed control manifest differs")


def _materialize_tree(
    staging: Path,
    payloads: Mapping[str, bytes],
    *,
    manifest_payload: bytes | None = None,
) -> None:
    if staging.exists() or staging.is_symlink():
        try:
            _validate_tree(
                staging,
                payloads,
                manifest_payload=manifest_payload,
            )
            return
        except RouterSuccessorError:
            _remove_owned_tree(staging)
    staging.mkdir(mode=0o700)
    _ensure_private_directory(staging)
    try:
        for name, payload in sorted(payloads.items()):
            _write_private_file(staging / name, payload, 0o700)
        if manifest_payload is not None:
            _write_private_file(
                staging / CONTROL_MANIFEST_NAME,
                manifest_payload,
                0o600,
            )
        _fsync_directory(staging)
        _validate_tree(
            staging,
            payloads,
            manifest_payload=manifest_payload,
        )
    except BaseException:
        # A normal exception is cleaned up; abrupt termination is recovered
        # from this operation-owned deterministic staging name.
        try:
            _remove_owned_tree(staging)
        except RouterSuccessorError:
            pass
        raise


def _publish_tree(
    final: Path,
    staging: Path,
    payloads: Mapping[str, bytes],
    *,
    manifest_payload: bytes | None = None,
) -> None:
    if final.exists() or final.is_symlink():
        _validate_tree(final, payloads, manifest_payload=manifest_payload)
        if staging.exists() or staging.is_symlink():
            _remove_owned_tree(staging)
        return
    _materialize_tree(
        staging,
        payloads,
        manifest_payload=manifest_payload,
    )
    try:
        os.rename(staging, final)
    except OSError as exc:
        if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
            raise RouterSuccessorError(
                "bootstrap-router tree cannot be published"
            ) from exc
        _validate_tree(final, payloads, manifest_payload=manifest_payload)
        _remove_owned_tree(staging)
    _fsync_directory(final.parent)
    _validate_tree(final, payloads, manifest_payload=manifest_payload)


class BootstrapRouterSuccessorManager:
    def __init__(
        self,
        source_root: Path,
        runtime_root: Path,
        production_root: Path,
        *,
        allow_test: bool = False,
        remote_main_probe: Callable[[str], str] | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.source_root = source_root.absolute()
        self.runtime_root = runtime_root.absolute()
        self.production_root = production_root.absolute()
        self.allow_test = allow_test
        self.remote_main_probe = remote_main_probe
        self.checkpoint = checkpoint or (lambda _name: None)
        self.state_root = self.runtime_root / "state"
        self.bin_root = self.runtime_root / "bin"
        self.control_root = self.runtime_root / CONTROL_RELEASE_ROOT_RELATIVE
        self.install_parent = self.runtime_root / INSTALL_ROOT_RELATIVE
        self.transaction_root = self.runtime_root / TRANSACTION_ROOT_RELATIVE
        self.bootstrap_path = self.runtime_root / BOOTSTRAP_RELATIVE
        self.active_control_path = self.runtime_root / ACTIVE_CONTROL_RELATIVE
        self.intent_path = self.runtime_root / INTENT_RELATIVE
        self.authority_path = self.runtime_root / AUTHORITY_RELATIVE
        self.fence_path = self.runtime_root / ROUTER_FENCE_RELATIVE
        self.selector_path = self.runtime_root / SELECTOR_RELATIVE
        self._source_object_database_verified = False

    def _journal_path(self, operation_id: str) -> Path:
        return self.transaction_root / f"{operation_id}.json"

    def _journal_staging(self, operation_id: str) -> Path:
        return self.transaction_root / f".{operation_id}.journal.create"

    def _selector_staging(self, operation_id: str) -> Path:
        return self.transaction_root / f".{operation_id}.selector-swap"

    def _control_staging(self, release_id: str, operation_id: str) -> Path:
        return self.control_root / f".{release_id}.{operation_id}.create"

    def _router_root(self, operation_id: str) -> Path:
        return self.runtime_root / INSTALL_ROOT_RELATIVE / operation_id

    def _router_staging(self, operation_id: str) -> Path:
        return self.install_parent / f".{operation_id}.create"

    @staticmethod
    def _validate_delivery(
        document: object,
        target_sha: str,
    ) -> dict[str, object]:
        try:
            return snapshot.validate_delivery_gate(
                document,
                target_sha=target_sha,
            )
        except snapshot.SnapshotError as exc:
            raise RouterSuccessorError(
                "bootstrap-router delivery authority is invalid"
            ) from exc

    def _remote_main(self, target_sha: str) -> str:
        if self.remote_main_probe is not None:
            observed = self.remote_main_probe(target_sha)
        elif self.allow_test:
            observed = target_sha
        else:
            completed = _run_git(
                self.source_root,
                "ls-remote",
                "--exit-code",
                "origin",
                "refs/heads/main",
            )
            fields = completed.stdout.strip().split()
            if len(fields) != 2 or fields[1] != "refs/heads/main":
                raise RouterSuccessorError("remote main identity is invalid")
            observed = fields[0]
        return _require_sha(observed, "remote main SHA")

    def _source_identity(self, target_sha: str) -> str:
        target_sha = _require_sha(target_sha, "target source SHA")
        git_dir = self.source_root / ".git"
        try:
            root = self.source_root.lstat()
            git_root = git_dir.lstat()
        except OSError as exc:
            raise RouterSuccessorError("target source clone is unavailable") from exc
        if (
            not stat.S_ISDIR(root.st_mode)
            or self.source_root.is_symlink()
            or root.st_uid != os.geteuid()
            or root.st_mode & 0o022
            or not stat.S_ISDIR(git_root.st_mode)
            or git_dir.is_symlink()
            or git_root.st_uid != os.geteuid()
            or git_root.st_mode & 0o022
        ):
            raise RouterSuccessorError("target source clone is unsafe")
        for relative in (
            "commondir",
            "info/grafts",
            "objects/info/alternates",
            "objects/info/http-alternates",
            "shallow",
        ):
            marker = git_dir / relative
            if marker.exists() or marker.is_symlink():
                raise RouterSuccessorError(
                    f"target source clone is not standalone: {relative}"
                )
        status = _run_git(
            self.source_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ).stdout
        branch = _run_git(
            self.source_root,
            "symbolic-ref",
            "-q",
            "HEAD",
        ).stdout.strip()
        head = _require_sha(
            _run_git(self.source_root, "rev-parse", "HEAD").stdout.strip(),
            "target clone HEAD",
        )
        local_main = _require_sha(
            _run_git(
                self.source_root,
                "rev-parse",
                "refs/heads/main",
            ).stdout.strip(),
            "target clone local main",
        )
        origin_main = _require_sha(
            _run_git(
                self.source_root,
                "rev-parse",
                "refs/remotes/origin/main",
            ).stdout.strip(),
            "target clone origin main",
        )
        tree = _require_sha(
            _run_git(
                self.source_root,
                "rev-parse",
                f"{target_sha}^{{tree}}",
            ).stdout.strip(),
            "target source tree",
        )
        ignored = bytes(
            _run_git(
                self.source_root,
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                text=False,
            ).stdout
        )
        replace_refs = _run_git(
            self.source_root,
            "for-each-ref",
            "--format=%(refname)",
            "refs/replace/",
        ).stdout.splitlines()
        index_entries = bytes(
            _run_git(
                self.source_root,
                "ls-files",
                "--sparse",
                "-v",
                "-z",
                text=False,
            ).stdout
        ).split(b"\0")
        special_index_entries = [
            entry for entry in index_entries if entry and not entry.startswith(b"H ")
        ]
        local_config_names = {
            name.lower()
            for name in _run_git(
                self.source_root,
                "config",
                "--local",
                "--name-only",
                "--list",
            ).stdout.splitlines()
        }
        forbidden_config = {
            "core.worktree",
            "core.hookspath",
            "core.fsmonitor",
            "core.sparsecheckout",
            "extensions.worktreeconfig",
            "extensions.objectformat",
            "extensions.partialclone",
        }
        redirected_config = any(
            name in forbidden_config
            or name.startswith("include.")
            or name.startswith("includeif.")
            or re.fullmatch(
                r"remote\..+\.(?:promisor|partialclonefilter)",
                name,
            )
            is not None
            for name in local_config_names
        )
        if not self._source_object_database_verified:
            _run_git(
                self.source_root,
                "fsck",
                "--full",
                "--strict",
                "--no-reflogs",
                "--no-dangling",
            )
            self._source_object_database_verified = True
        if (
            status
            or ignored
            or replace_refs
            or special_index_entries
            or redirected_config
            or branch != "refs/heads/main"
            or head != target_sha
            or local_main != target_sha
            or origin_main != target_sha
        ):
            raise RouterSuccessorError(
                "target source clone is not a clean standalone exact main"
            )
        if not self.allow_test:
            fetch_urls = _run_git(
                self.source_root,
                "remote",
                "get-url",
                "--all",
                "origin",
            ).stdout.splitlines()
            push_urls = _run_git(
                self.source_root,
                "remote",
                "get-url",
                "--push",
                "--all",
                "origin",
            ).stdout.splitlines()
            remotes = _run_git(self.source_root, "remote").stdout.splitlines()
            if (
                remotes != ["origin"]
                or fetch_urls != [REPOSITORY_SSH_URL]
                or push_urls != [REPOSITORY_SSH_URL]
            ):
                raise RouterSuccessorError(
                    "target source clone origin identity differs"
                )
        if self._remote_main(target_sha) != target_sha:
            raise RouterSuccessorError("target source is no longer remote main")
        return tree

    def _bootstrap_authority(
        self,
        *,
        allowed_selector_digests: set[str] | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        bootstrap, bootstrap_digest = _private_json(self.bootstrap_path)
        immutable = bootstrap.get("immutable_files")
        if (
            bootstrap.get("schema_version") != 3
            or bootstrap.get("status") != "completed"
            or bootstrap.get("authority_kind") != "manual-runtime-adoption"
            or not isinstance(immutable, dict)
            or set(immutable) != BOOTSTRAP_IMMUTABLE_FILES
            or any(
                not isinstance(value, str)
                or DIGEST_RE.fullmatch(value) is None
                for value in immutable.values()
            )
        ):
            raise RouterSuccessorError(
                "manual-adoption bootstrap authority is invalid"
            )
        predecessor_selector = str(immutable["control_runtime_selector.py"])
        observed_selector = _private_file(
            self.selector_path,
            mode=0o700,
        )[1]
        allowed = allowed_selector_digests or {predecessor_selector}
        if observed_selector not in allowed:
            raise RouterSuccessorError(
                "bootstrap selector is outside the authorized transition"
            )
        try:
            observed_names = {entry.name for entry in self.bin_root.iterdir()}
        except OSError as exc:
            raise RouterSuccessorError(
                "bootstrap bin inventory is unavailable"
            ) from exc
        if observed_names != BOOTSTRAP_IMMUTABLE_FILES:
            raise RouterSuccessorError("bootstrap bin inventory differs")
        for name, digest in immutable.items():
            if name == "control_runtime_selector.py":
                continue
            if _private_file(self.bin_root / name, mode=0o700)[1] != digest:
                raise RouterSuccessorError(
                    f"immutable bootstrap file differs: {name}"
                )
        active, _active_digest = _private_json(self.active_control_path)
        if active != bootstrap.get("active_control"):
            raise RouterSuccessorError(
                "active-control differs from adopted bootstrap authority"
            )
        return bootstrap, bootstrap_digest, predecessor_selector

    def _predecessor_authorities(
        self,
        *,
        target_sha: str,
        target_tree: str,
        bootstrap_digest: str,
    ) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
        try:
            snapshot_authority, snapshot_digest = (
                snapshot.verify_snapshot_integrity(
                    self.runtime_root,
                    production_root=self.production_root,
                )
            )
        except snapshot.SnapshotError as exc:
            raise RouterSuccessorError(
                "production Git snapshot integrity cannot be proved"
            ) from exc
        if (
            snapshot_authority.get("target_source_sha") != target_sha
            or snapshot_authority.get("target_source_tree") != target_tree
        ):
            raise RouterSuccessorError("production Git snapshot target differs")
        delivery = self._validate_delivery(
            snapshot_authority.get("delivery_gate"),
            target_sha,
        )
        if (
            snapshot_authority.get("delivery_gate_sha256")
            != _digest_document(delivery)
        ):
            raise RouterSuccessorError(
                "production Git snapshot delivery binding differs"
            )
        source, source_digest = _private_json(
            self.runtime_root / SOURCE_SUCCESSOR_RELATIVE
        )
        source_plan = source.get("plan")
        production_source = (
            source_plan.get("production_source")
            if isinstance(source_plan, dict)
            else None
        )
        if (
            source.get("schema_version") != 2
            or source.get("status") != "completed"
            or source.get("authority_kind")
            != "manual-runtime-adoption-git-permission-source-successor"
            or source.get("source_sha") != target_sha
            or source.get("source_tree") != target_tree
            or source.get("bootstrap_control_sha256") != bootstrap_digest
            or source.get("snapshot_authority_sha256") != snapshot_digest
            or not isinstance(production_source, dict)
            or production_source.get("source_sha")
            != snapshot_authority.get("production_source_sha")
            or production_source.get("source_tree")
            != snapshot_authority.get("production_source_tree")
        ):
            raise RouterSuccessorError("source-successor authority differs")
        unit, unit_digest = _private_json(
            self.runtime_root / UNIT_PERMISSION_RELATIVE
        )
        unit_plan = unit.get("plan")
        unit_successor = (
            unit_plan.get("git_permission_successor")
            if isinstance(unit_plan, dict)
            else None
        )
        unit_source_binding = (
            unit_successor.get("source_successor_authority")
            if isinstance(unit_successor, dict)
            else None
        )
        if (
            unit.get("schema_version") != 2
            or unit.get("status") != "completed"
            or unit.get("authority_kind")
            != "manual-runtime-adoption-unit-permission-hardening"
            or unit.get("source_sha") != target_sha
            or unit.get("source_tree") != target_tree
            or unit.get("bootstrap_control_sha256") != bootstrap_digest
            or unit.get("adopted_git_permission_source_successor_sha256")
            != source_digest
            or unit.get("production_source_sha")
            != snapshot_authority.get("production_source_sha")
            or unit.get("production_source_tree")
            != snapshot_authority.get("production_source_tree")
            or not isinstance(unit_source_binding, dict)
            or unit_source_binding.get("schema_version") != 2
            or unit_source_binding.get("authority_file_sha256")
            != source_digest
            or unit_source_binding.get("snapshot_authority_sha256")
            != snapshot_digest
        ):
            raise RouterSuccessorError("unit-permission authority differs")
        compact = {
            "snapshot": {
                "sha256": snapshot_digest,
                "operation_id": snapshot_authority["operation_id"],
                "authority_kind": snapshot_authority["authority_kind"],
            },
            "source_successor": {
                "sha256": source_digest,
                "operation_id": source["operation_id"],
                "authority_kind": source["authority_kind"],
            },
            "unit_permission": {
                "sha256": unit_digest,
                "operation_id": unit["operation_id"],
                "authority_kind": unit["authority_kind"],
            },
        }
        return compact, delivery

    def _predecessor_selector_payload(
        self,
        operation_id: str,
        predecessor_digest: str,
    ) -> bytes:
        candidates = (
            self._router_root(operation_id) / "predecessor-selector.py",
            self._selector_staging(operation_id),
            self.selector_path,
        )
        for path in candidates:
            if not (path.exists() or path.is_symlink()):
                continue
            try:
                payload, digest = _private_file(path, mode=0o700)
            except RouterSuccessorError:
                continue
            if digest == predecessor_digest:
                return payload
        raise RouterSuccessorError(
            "immutable predecessor selector payload is unavailable"
        )

    def _router_artifacts(
        self,
        *,
        target_sha: str,
        operation_id: str,
        predecessor_payload: bytes,
    ) -> tuple[dict[str, bytes], dict[str, dict[str, object]]]:
        payloads = {
            installed: _git_blob(self.source_root, target_sha, source)
            for installed, source in ROUTER_SOURCE_FILES.items()
        }
        payloads["predecessor-selector.py"] = predecessor_payload
        root_relative = INSTALL_ROOT_RELATIVE / operation_id
        records = {
            name: _file_record(
                (root_relative / name).as_posix(),
                payload,
            )
            for name, payload in sorted(payloads.items())
        }
        return payloads, records

    @staticmethod
    def _interlock(operation_id: str, target_sha: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "bootstrap-router-successor-fence",
            "authority_kind": AUTHORITY_KIND,
            "policy": POLICY,
            "operation_id": operation_id,
            "target_source_sha": target_sha,
        }

    @staticmethod
    def _impact(plan: Mapping[str, object]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "policy": "nexpoly-bootstrap-router-successor-impact-v1",
            "operation_id": plan["operation_id"],
            "target": {
                "source_sha": plan["target_source_sha"],
                "source_tree": plan["target_source_tree"],
            },
            "predecessor_selector_sha256": plan[
                "predecessor_selector_sha256"
            ],
            "successor_selector_sha256": plan[
                "successor_selector_sha256"
            ],
            "target_control_release": plan["target_control_release"],
            "router_files": plan["router_files"],
            "interlock": plan["interlock"],
            "mutations": plan["mutations"],
        }

    def _assert_initial_namespace_absent(self, operation_id: str) -> None:
        occupied = [
            self.intent_path,
            self.authority_path,
            self.fence_path,
            self.runtime_root / CURRENT_STATE_RELATIVE,
            self.runtime_root / DEPLOY_MARKER_RELATIVE,
            self._journal_path(operation_id),
            self._journal_staging(operation_id),
            self._selector_staging(operation_id),
            self._router_root(operation_id),
            self._router_staging(operation_id),
        ]
        if any(path.exists() or path.is_symlink() for path in occupied):
            raise RouterSuccessorError(
                "bootstrap-router initial namespace is occupied"
            )
        if self.transaction_root.exists() or self.transaction_root.is_symlink():
            snapshot._require_private_directory(self.transaction_root)
            if any(self.transaction_root.iterdir()):
                raise RouterSuccessorError(
                    "bootstrap-router transaction namespace is occupied"
                )
        if self.install_parent.exists() or self.install_parent.is_symlink():
            snapshot._require_private_directory(self.install_parent)
            if any(self.install_parent.iterdir()):
                raise RouterSuccessorError(
                    "bootstrap-router install namespace is occupied"
                )

    def _build_plan(
        self,
        target_sha: str,
        operation_id: str,
    ) -> tuple[dict[str, object], dict[str, Any], dict[str, bytes]]:
        target_sha = _require_sha(target_sha, "target source SHA")
        operation_id = _require_operation(operation_id)
        target_tree = self._source_identity(target_sha)
        bootstrap, bootstrap_digest, predecessor_digest = (
            self._bootstrap_authority()
        )
        predecessors, delivery = self._predecessor_authorities(
            target_sha=target_sha,
            target_tree=target_tree,
            bootstrap_digest=bootstrap_digest,
        )
        predecessor_payload = _private_file(
            self.selector_path,
            mode=0o700,
        )[0]
        control = _target_control_artifacts(
            self.source_root,
            target_sha,
            target_tree,
        )
        router_payloads, router_records = self._router_artifacts(
            target_sha=target_sha,
            operation_id=operation_id,
            predecessor_payload=predecessor_payload,
        )
        successor_digest = router_records["reviewed-selector.py"]["sha256"]
        if successor_digest == predecessor_digest:
            raise RouterSuccessorError(
                "reviewed selector is not a content successor"
            )
        mutations = {
            "source": False,
            "source_refs": False,
            "database": False,
            "services": False,
            "containers": False,
            "units": False,
            "active_control": False,
            "worker_routes": False,
            "target_control_release": True,
            "router_files": True,
            "bootstrap_selector": True,
            "runtime_authority": True,
            "temporary_deploy_interlock": True,
        }
        plan: dict[str, object] = {
            "schema_version": 1,
            "authority_kind": AUTHORITY_KIND,
            "policy": POLICY,
            "operation_id": operation_id,
            "target_source_sha": target_sha,
            "target_source_tree": target_tree,
            "bootstrap_control_sha256": bootstrap_digest,
            "predecessor_selector_sha256": predecessor_digest,
            "successor_selector_sha256": successor_digest,
            "snapshot_authority_sha256": predecessors["snapshot"]["sha256"],
            "source_successor_authority_sha256": predecessors[
                "source_successor"
            ]["sha256"],
            "unit_permission_authority_sha256": predecessors[
                "unit_permission"
            ]["sha256"],
            "delivery_gate": delivery,
            "delivery_gate_sha256": _digest_document(delivery),
            "target_control_release": control["compact"],
            "router_files": router_records,
            "predecessor_authorities": predecessors,
            "interlock": {
                "path": str(self.fence_path),
                "document": self._interlock(operation_id, target_sha),
            },
            "mutations": mutations,
            "router_successor_impact_sha256": "",
        }
        plan["router_successor_impact_sha256"] = _digest_document(
            self._impact(plan)
        )
        if set(plan) != PLAN_FIELDS:
            raise RouterSuccessorError(
                "internal bootstrap-router plan shape differs"
            )
        if bootstrap.get("source_sha") == target_sha:
            raise RouterSuccessorError(
                "bootstrap-router successor target is not a later release"
            )
        return plan, control, router_payloads

    def plan(
        self,
        *,
        target_sha: str,
        operation_id: str,
    ) -> dict[str, object]:
        operation_id = _require_operation(operation_id)
        self._assert_initial_namespace_absent(operation_id)
        plan, _control, _router_payloads = self._build_plan(
            target_sha,
            operation_id,
        )
        self._assert_initial_namespace_absent(operation_id)
        return {
            "action": "adopt-bootstrap-router-successor-plan",
            "apply": False,
            "logical_zero_write": True,
            "plan": plan,
            "plan_sha256": _digest_document(plan),
            "router_successor_impact_sha256": plan[
                "router_successor_impact_sha256"
            ],
        }

    def _validate_plan(self, document: object) -> dict[str, object]:
        if (
            not isinstance(document, dict)
            or set(document) != PLAN_FIELDS
            or document.get("schema_version") != 1
            or document.get("authority_kind") != AUTHORITY_KIND
            or document.get("policy") != POLICY
        ):
            raise RouterSuccessorError("bootstrap-router plan is invalid")
        operation_id = _require_operation(document.get("operation_id"))
        target_sha = _require_sha(
            document.get("target_source_sha"),
            "router plan target SHA",
        )
        _require_sha(
            document.get("target_source_tree"),
            "router plan target tree",
        )
        for field in (
            "bootstrap_control_sha256",
            "predecessor_selector_sha256",
            "successor_selector_sha256",
            "snapshot_authority_sha256",
            "source_successor_authority_sha256",
            "unit_permission_authority_sha256",
            "delivery_gate_sha256",
            "router_successor_impact_sha256",
        ):
            _require_digest(document.get(field), f"router plan {field}")
        if (
            document["predecessor_selector_sha256"]
            == document["successor_selector_sha256"]
        ):
            raise RouterSuccessorError("router plan selector transition is empty")
        delivery = self._validate_delivery(
            document.get("delivery_gate"),
            target_sha,
        )
        if document["delivery_gate_sha256"] != _digest_document(delivery):
            raise RouterSuccessorError("router plan delivery digest differs")
        target_control = document.get("target_control_release")
        if (
            not isinstance(target_control, dict)
            or set(target_control)
            != {
                "release_id",
                "source_sha",
                "source_tree",
                "manifest_sha256",
                "deploy_sha256",
            }
            or not isinstance(target_control.get("release_id"), str)
            or DIGEST_RE.fullmatch(
                "sha256:" + str(target_control["release_id"])
            )
            is None
            or target_control.get("source_sha") != target_sha
            or target_control.get("source_tree")
            != document["target_source_tree"]
        ):
            raise RouterSuccessorError("router plan target control is invalid")
        for field in ("manifest_sha256", "deploy_sha256"):
            _require_digest(
                target_control.get(field),
                f"router target control {field}",
            )
        router_files = document.get("router_files")
        expected_names = {
            *ROUTER_SOURCE_FILES,
            "predecessor-selector.py",
        }
        if not isinstance(router_files, dict) or set(router_files) != expected_names:
            raise RouterSuccessorError("router plan file inventory is invalid")
        for name, record in router_files.items():
            if (
                not isinstance(record, dict)
                or set(record) != {"relative_path", "sha256", "size", "mode"}
                or record.get("relative_path")
                != (INSTALL_ROOT_RELATIVE / operation_id / name).as_posix()
                or record.get("mode") != 0o700
                or type(record.get("size")) is not int
                or not 1 <= record["size"] <= 16 * 1024 * 1024
            ):
                raise RouterSuccessorError("router plan file record is invalid")
            _require_digest(record.get("sha256"), "router plan file digest")
        if (
            router_files["reviewed-selector.py"]["sha256"]
            != document["successor_selector_sha256"]
            or router_files["predecessor-selector.py"]["sha256"]
            != document["predecessor_selector_sha256"]
        ):
            raise RouterSuccessorError("router plan selector copies differ")
        predecessors = document.get("predecessor_authorities")
        expected_predecessors = {
            "snapshot": (
                "manual-runtime-adoption-production-git-snapshot",
                document["snapshot_authority_sha256"],
            ),
            "source_successor": (
                "manual-runtime-adoption-git-permission-source-successor",
                document["source_successor_authority_sha256"],
            ),
            "unit_permission": (
                "manual-runtime-adoption-unit-permission-hardening",
                document["unit_permission_authority_sha256"],
            ),
        }
        if not isinstance(predecessors, dict) or set(predecessors) != set(
            expected_predecessors
        ):
            raise RouterSuccessorError(
                "router plan predecessor inventory is invalid"
            )
        for name, (kind, digest) in expected_predecessors.items():
            value = predecessors[name]
            if (
                not isinstance(value, dict)
                or set(value) != {"sha256", "operation_id", "authority_kind"}
                or value.get("sha256") != digest
                or value.get("authority_kind") != kind
                or not isinstance(value.get("operation_id"), str)
                or not value["operation_id"]
            ):
                raise RouterSuccessorError(
                    "router plan predecessor record is invalid"
                )
        interlock = document.get("interlock")
        if interlock != {
            "path": str(self.fence_path),
            "document": self._interlock(operation_id, target_sha),
        }:
            raise RouterSuccessorError("router plan interlock differs")
        mutations = document.get("mutations")
        if (
            not isinstance(mutations, dict)
            or set(mutations)
            != {
                "source",
                "source_refs",
                "database",
                "services",
                "containers",
                "units",
                "active_control",
                "worker_routes",
                "target_control_release",
                "router_files",
                "bootstrap_selector",
                "runtime_authority",
                "temporary_deploy_interlock",
            }
            or any(type(value) is not bool for value in mutations.values())
            or any(
                mutations[name]
                for name in (
                    "source",
                    "source_refs",
                    "database",
                    "services",
                    "containers",
                    "units",
                    "active_control",
                    "worker_routes",
                )
            )
            or not all(
                mutations[name]
                for name in (
                    "target_control_release",
                    "router_files",
                    "bootstrap_selector",
                    "runtime_authority",
                    "temporary_deploy_interlock",
                )
            )
        ):
            raise RouterSuccessorError("router plan mutations are invalid")
        if document["router_successor_impact_sha256"] != _digest_document(
            self._impact(document)
        ):
            raise RouterSuccessorError("router plan impact digest differs")
        return dict(document)

    def _reprove_plan(
        self,
        plan: Mapping[str, object],
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        plan = self._validate_plan(plan)
        target_sha = str(plan["target_source_sha"])
        operation_id = str(plan["operation_id"])
        target_tree = self._source_identity(target_sha)
        if target_tree != plan["target_source_tree"]:
            raise RouterSuccessorError("router target tree changed")
        candidate_digest = str(plan["successor_selector_sha256"])
        _bootstrap, bootstrap_digest, predecessor_digest = (
            self._bootstrap_authority(
                allowed_selector_digests={
                    str(plan["predecessor_selector_sha256"]),
                    candidate_digest,
                }
            )
        )
        if (
            bootstrap_digest != plan["bootstrap_control_sha256"]
            or predecessor_digest != plan["predecessor_selector_sha256"]
        ):
            raise RouterSuccessorError("router bootstrap authority changed")
        predecessors, delivery = self._predecessor_authorities(
            target_sha=target_sha,
            target_tree=target_tree,
            bootstrap_digest=bootstrap_digest,
        )
        if (
            predecessors != plan["predecessor_authorities"]
            or delivery != plan["delivery_gate"]
            or _digest_document(delivery) != plan["delivery_gate_sha256"]
        ):
            raise RouterSuccessorError(
                "router predecessor authority chain changed"
            )
        predecessor_payload = self._predecessor_selector_payload(
            operation_id,
            predecessor_digest,
        )
        router_payloads, router_records = self._router_artifacts(
            target_sha=target_sha,
            operation_id=operation_id,
            predecessor_payload=predecessor_payload,
        )
        if router_records != plan["router_files"]:
            raise RouterSuccessorError("reviewed router files changed")
        control = _target_control_artifacts(
            self.source_root,
            target_sha,
            target_tree,
        )
        if control["compact"] != plan["target_control_release"]:
            raise RouterSuccessorError("reviewed target control changed")
        current = self.runtime_root / CURRENT_STATE_RELATIVE
        marker = self.runtime_root / DEPLOY_MARKER_RELATIVE
        if current.exists() or current.is_symlink():
            raise RouterSuccessorError(
                "bootstrap-router successor is restricted to first deployment"
            )
        if marker.exists() or marker.is_symlink():
            raise RouterSuccessorError(
                "deployment recovery must finish before router succession"
            )
        if self.fence_path.exists() or self.fence_path.is_symlink():
            fence, _fence_digest = _private_json(self.fence_path)
            if fence != plan["interlock"]["document"]:
                raise RouterSuccessorError("router interlock belongs to another task")
        return control, router_payloads

    def _validate_journal(self, document: object) -> dict[str, object]:
        if (
            not isinstance(document, dict)
            or set(document) != JOURNAL_FIELDS
            or document.get("schema_version") != 1
            or document.get("phase") not in JOURNAL_PHASES
            or not isinstance(document.get("plan"), dict)
        ):
            raise RouterSuccessorError("bootstrap-router journal is invalid")
        operation_id = _require_operation(document.get("operation_id"))
        plan = self._validate_plan(document["plan"])
        if (
            plan["operation_id"] != operation_id
            or document.get("plan_sha256") != _digest_document(plan)
            or document.get("router_successor_impact_sha256")
            != plan["router_successor_impact_sha256"]
        ):
            raise RouterSuccessorError("bootstrap-router journal plan differs")
        _require_digest(document.get("plan_sha256"), "router journal plan")
        created_at = document.get("created_at")
        completed_at = document.get("completed_at")
        if not isinstance(created_at, str) or UTC_RE.fullmatch(created_at) is None:
            raise RouterSuccessorError("router journal timestamp is invalid")
        phase = str(document["phase"])
        if phase == "completed":
            if (
                document.get("status") != "completed"
                or not isinstance(completed_at, str)
                or UTC_RE.fullmatch(completed_at) is None
                or completed_at < created_at
            ):
                raise RouterSuccessorError(
                    "completed bootstrap-router journal is invalid"
                )
        elif document.get("status") != "applying":
            raise RouterSuccessorError("bootstrap-router journal status is invalid")
        elif phase in {
            "authority-commit-intent",
            "authority-published",
        }:
            if (
                not isinstance(completed_at, str)
                or UTC_RE.fullmatch(completed_at) is None
                or completed_at < created_at
            ):
                raise RouterSuccessorError(
                    "bootstrap-router commit timestamp is invalid"
                )
        elif completed_at is not None:
            raise RouterSuccessorError(
                "bootstrap-router completion time appeared before commit"
            )
        return dict(document)

    @staticmethod
    def _phase_index(phase: object) -> int:
        ordered = (
            "intent",
            "interlock-ready",
            "control-release-ready",
            "router-files-ready",
            "selector-swap-intent",
            "selector-switched",
            "authority-commit-intent",
            "authority-published",
            "completed",
        )
        try:
            return ordered.index(str(phase))
        except ValueError as exc:
            raise RouterSuccessorError("router journal phase is invalid") from exc

    @staticmethod
    def _discard_owned_file(path: Path, *, mode: int) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RouterSuccessorError("owned router staging is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise RouterSuccessorError("owned router staging is unsafe")
        try:
            path.unlink()
        except OSError as exc:
            raise RouterSuccessorError("owned router staging cannot be removed") from exc
        _fsync_directory(path.parent)

    def _load_journal(
        self,
        operation_id: str,
        *,
        expected_plan_sha256: str,
        expected_impact_sha256: str,
    ) -> dict[str, object] | None:
        journal_path = self._journal_path(operation_id)
        staging = self._journal_staging(operation_id)
        final: dict[str, object] | None = None
        staged: dict[str, object] | None = None
        if journal_path.exists() or journal_path.is_symlink():
            raw, _digest = _private_json(journal_path)
            final = self._validate_journal(raw)
        if staging.exists() or staging.is_symlink():
            try:
                raw, _digest = _private_json(staging)
                staged = self._validate_journal(raw)
            except RouterSuccessorError:
                self._discard_owned_file(staging, mode=0o600)
                staged = None
            if staged is not None:
                if (
                    staged["operation_id"] != operation_id
                    or staged["plan_sha256"] != expected_plan_sha256
                    or staged["router_successor_impact_sha256"]
                    != expected_impact_sha256
                ):
                    raise RouterSuccessorError(
                        "router journal staging belongs to another plan"
                    )
                if final is not None:
                    stable_fields = {
                        "operation_id",
                        "plan",
                        "plan_sha256",
                        "router_successor_impact_sha256",
                        "created_at",
                    }
                    if any(staged[field] != final[field] for field in stable_fields):
                        raise RouterSuccessorError(
                            "router journal staging lineage differs"
                        )
                    if self._phase_index(staged["phase"]) < self._phase_index(
                        final["phase"]
                    ):
                        raise RouterSuccessorError(
                            "router journal staging would roll back phase"
                        )
                try:
                    os.replace(staging, journal_path)
                except OSError as exc:
                    raise RouterSuccessorError(
                        "router journal staging cannot be recovered"
                    ) from exc
                _fsync_directory(self.transaction_root)
                final = staged
        if final is None:
            return None
        if (
            final["operation_id"] != operation_id
            or final["plan_sha256"] != expected_plan_sha256
            or final["router_successor_impact_sha256"]
            != expected_impact_sha256
        ):
            raise RouterSuccessorError("durable router journal differs")
        return final

    def _write_journal(self, journal: Mapping[str, object]) -> None:
        normalized = self._validate_journal(journal)
        operation_id = str(normalized["operation_id"])
        staging = self._journal_staging(operation_id)
        if staging.exists() or staging.is_symlink():
            self._discard_owned_file(staging, mode=0o600)
        _write_private_file(staging, _canonical_bytes(normalized), 0o600)
        try:
            os.replace(staging, self._journal_path(operation_id))
        except OSError as exc:
            raise RouterSuccessorError("router journal cannot be committed") from exc
        _fsync_directory(self.transaction_root)

    def _advance(
        self,
        journal: dict[str, object],
        phase: str,
        checkpoint: str,
    ) -> None:
        if self._phase_index(phase) < self._phase_index(journal["phase"]):
            raise RouterSuccessorError("router journal phase cannot move backward")
        journal["phase"] = phase
        self._write_journal(journal)
        self.checkpoint(checkpoint)

    @staticmethod
    def _intent(
        plan: Mapping[str, object],
        journal: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "selector-swap-intent",
            "authority_kind": AUTHORITY_KIND,
            "policy": POLICY,
            "operation_id": plan["operation_id"],
            "target_source_sha": plan["target_source_sha"],
            "target_source_tree": plan["target_source_tree"],
            "bootstrap_control_sha256": plan["bootstrap_control_sha256"],
            "predecessor_selector_sha256": plan[
                "predecessor_selector_sha256"
            ],
            "successor_selector_sha256": plan["successor_selector_sha256"],
            "snapshot_authority_sha256": plan["snapshot_authority_sha256"],
            "source_successor_authority_sha256": plan[
                "source_successor_authority_sha256"
            ],
            "unit_permission_authority_sha256": plan[
                "unit_permission_authority_sha256"
            ],
            "target_control_release": plan["target_control_release"],
            "router_files": plan["router_files"],
            "delivery_gate_sha256": plan["delivery_gate_sha256"],
            "plan_sha256": journal["plan_sha256"],
            "created_at": journal["created_at"],
        }

    @staticmethod
    def _completion_authority(
        intent: Mapping[str, object],
        completed_at: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": AUTHORITY_KIND,
            "policy": POLICY,
            "operation_id": intent["operation_id"],
            "intent_sha256": _digest_document(intent),
            "intent": dict(intent),
            "completed_at": completed_at,
        }

    def _install_interlock(self, plan: Mapping[str, object]) -> None:
        expected = plan["interlock"]["document"]
        if not isinstance(expected, dict):
            raise RouterSuccessorError("router interlock plan is invalid")
        _publish_create_once(
            self.fence_path,
            expected,
            str(plan["operation_id"]),
        )
        observed, digest = _private_json(self.fence_path)
        if observed != expected or digest != _digest_document(expected):
            raise RouterSuccessorError("router interlock publication differs")

    def _remove_interlock(self, plan: Mapping[str, object]) -> None:
        if not (self.fence_path.exists() or self.fence_path.is_symlink()):
            return
        expected = plan["interlock"]["document"]
        observed, digest = _private_json(self.fence_path)
        if observed != expected or digest != _digest_document(expected):
            raise RouterSuccessorError("router interlock changed before removal")
        self._discard_owned_file(self.fence_path, mode=0o600)

    def _install_control_release(
        self,
        plan: Mapping[str, object],
        artifacts: Mapping[str, Any],
    ) -> None:
        compact = plan["target_control_release"]
        if not isinstance(compact, dict):
            raise RouterSuccessorError("target control plan is invalid")
        release_id = str(compact["release_id"])
        final = self.control_root / release_id
        staging = self._control_staging(
            release_id,
            str(plan["operation_id"]),
        )
        _publish_tree(
            final,
            staging,
            artifacts["payloads"],
            manifest_payload=artifacts["manifest_payload"],
        )
        if (
            _digest_bytes(artifacts["manifest_payload"])
            != compact["manifest_sha256"]
        ):
            raise RouterSuccessorError("installed control release digest differs")

    def _install_router_files(
        self,
        plan: Mapping[str, object],
        payloads: Mapping[str, bytes],
    ) -> None:
        operation_id = str(plan["operation_id"])
        _publish_tree(
            self._router_root(operation_id),
            self._router_staging(operation_id),
            payloads,
        )
        records = {
            name: _file_record(
                (INSTALL_ROOT_RELATIVE / operation_id / name).as_posix(),
                payload,
            )
            for name, payload in sorted(payloads.items())
        }
        if records != plan["router_files"]:
            raise RouterSuccessorError("installed router file identities differ")

    def _publish_intent(
        self,
        plan: Mapping[str, object],
        journal: Mapping[str, object],
    ) -> dict[str, object]:
        intent = self._intent(plan, journal)
        _publish_create_once(
            self.intent_path,
            intent,
            str(plan["operation_id"]),
        )
        observed, digest = _private_json(self.intent_path)
        if observed != intent or digest != _digest_document(intent):
            raise RouterSuccessorError("published router intent differs")
        return intent

    def _switch_selector(
        self,
        plan: Mapping[str, object],
        payloads: Mapping[str, bytes],
    ) -> None:
        operation_id = str(plan["operation_id"])
        predecessor_digest = str(plan["predecessor_selector_sha256"])
        successor_digest = str(plan["successor_selector_sha256"])
        candidate_payload = payloads["reviewed-selector.py"]
        predecessor_copy = self._router_root(operation_id) / "predecessor-selector.py"
        reviewed_copy = self._router_root(operation_id) / "reviewed-selector.py"
        if (
            _private_file(predecessor_copy, mode=0o700)[1]
            != predecessor_digest
            or _private_file(reviewed_copy, mode=0o700)[1]
            != successor_digest
            or _digest_bytes(candidate_payload) != successor_digest
        ):
            raise RouterSuccessorError("router selector copies differ")
        intent, _intent_digest = _private_json(self.intent_path)
        if intent != self._intent(plan, {
            "plan_sha256": intent.get("plan_sha256"),
            "created_at": intent.get("created_at"),
        }):
            # The full exact comparison is performed by _publish_intent.  This
            # secondary guard makes the swap impossible after any path/digest
            # field changed, while preserving the sealed timestamps.
            raise RouterSuccessorError("router intent changed before selector swap")
        staging = self._selector_staging(operation_id)
        selector_payload, selector_digest = _private_file(
            self.selector_path,
            mode=0o700,
        )
        staged_digest: str | None = None
        if staging.exists() or staging.is_symlink():
            _staged_payload, staged_digest = _private_file(staging, mode=0o700)
        if selector_digest == successor_digest:
            if staged_digest is not None:
                if staged_digest != predecessor_digest:
                    raise RouterSuccessorError(
                        "selector exchange residue is not the predecessor"
                    )
                self._discard_owned_file(staging, mode=0o700)
            return
        if selector_digest != predecessor_digest:
            raise RouterSuccessorError("bootstrap selector CAS source differs")
        if selector_payload != payloads["predecessor-selector.py"]:
            raise RouterSuccessorError("bootstrap predecessor selector bytes differ")
        if staged_digest is None:
            _write_private_file(staging, candidate_payload, 0o700)
            staged_digest = _private_file(staging, mode=0o700)[1]
        if staged_digest != successor_digest:
            raise RouterSuccessorError("selector exchange staging differs")
        # Re-read both sides immediately before the atomic exchange.  The
        # displaced predecessor remains at the deterministic staging name, so
        # every power-loss outcome is classifiable without a reset or unlink.
        if (
            _private_file(self.selector_path, mode=0o700)[1]
            != predecessor_digest
            or _private_file(staging, mode=0o700)[1] != successor_digest
        ):
            raise RouterSuccessorError("selector exchange CAS changed")
        _rename_exchange(self.selector_path, staging)
        self.checkpoint("bootstrap-router-selector-exchanged")
        if (
            _private_file(self.selector_path, mode=0o700)[1] != successor_digest
            or _private_file(staging, mode=0o700)[1] != predecessor_digest
        ):
            raise RouterSuccessorError("selector exchange result differs")
        self._discard_owned_file(staging, mode=0o700)

    def _load_expected_publications(
        self,
        plan: Mapping[str, object],
        journal: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        intent = self._intent(plan, journal)
        observed_intent, intent_digest = _private_json(self.intent_path)
        if observed_intent != intent or intent_digest != _digest_document(intent):
            raise RouterSuccessorError("durable router intent differs")
        authority: dict[str, object] | None = None
        if self.authority_path.exists() or self.authority_path.is_symlink():
            completed_at = journal.get("completed_at")
            if not isinstance(completed_at, str):
                raise RouterSuccessorError(
                    "router authority exists before commit timestamp"
                )
            expected = self._completion_authority(intent, completed_at)
            observed, _authority_digest = _private_json(self.authority_path)
            if observed != expected:
                raise RouterSuccessorError("durable router authority differs")
            authority = expected
        return intent, authority

    @staticmethod
    def _assert_confirmations(
        plan: Mapping[str, object],
        *,
        confirm_plan_sha256: str,
        confirm_router_successor_impact_sha256: str,
        confirm_snapshot_authority_sha256: str,
        confirm_source_successor_authority_sha256: str,
        confirm_unit_permission_authority_sha256: str,
        confirm_predecessor_selector_sha256: str,
    ) -> None:
        expected = {
            "plan": _digest_document(plan),
            "impact": plan["router_successor_impact_sha256"],
            "snapshot": plan["snapshot_authority_sha256"],
            "source": plan["source_successor_authority_sha256"],
            "unit": plan["unit_permission_authority_sha256"],
            "selector": plan["predecessor_selector_sha256"],
        }
        observed = {
            "plan": confirm_plan_sha256,
            "impact": confirm_router_successor_impact_sha256,
            "snapshot": confirm_snapshot_authority_sha256,
            "source": confirm_source_successor_authority_sha256,
            "unit": confirm_unit_permission_authority_sha256,
            "selector": confirm_predecessor_selector_sha256,
        }
        if observed != expected:
            raise RouterSuccessorError(
                "bootstrap-router apply confirmations differ"
            )

    def apply(
        self,
        *,
        target_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_router_successor_impact_sha256: str,
        confirm_snapshot_authority_sha256: str,
        confirm_source_successor_authority_sha256: str,
        confirm_unit_permission_authority_sha256: str,
        confirm_predecessor_selector_sha256: str,
    ) -> dict[str, object]:
        target_sha = _require_sha(target_sha, "target source SHA")
        operation_id = _require_operation(operation_id)
        for label, value in (
            ("confirmed plan", confirm_plan_sha256),
            ("confirmed router impact", confirm_router_successor_impact_sha256),
            ("confirmed snapshot authority", confirm_snapshot_authority_sha256),
            (
                "confirmed source-successor authority",
                confirm_source_successor_authority_sha256,
            ),
            (
                "confirmed unit-permission authority",
                confirm_unit_permission_authority_sha256,
            ),
            (
                "confirmed predecessor selector",
                confirm_predecessor_selector_sha256,
            ),
        ):
            _require_digest(value, label)

        journal_path = self._journal_path(operation_id)
        preliminary: dict[str, object] | None = None
        if not (journal_path.exists() or journal_path.is_symlink()):
            # A first invocation re-runs the independently reviewed zero-write
            # plan before creating even the transaction journal. A torn first
            # journal staging is safe to recover because the selector and
            # interlock cannot have changed before the final journal existed.
            stage_only = self._journal_staging(operation_id)
            if not (stage_only.exists() or stage_only.is_symlink()):
                self._assert_initial_namespace_absent(operation_id)
            plan, _control, _router_payloads = self._build_plan(
                target_sha,
                operation_id,
            )
            self._assert_confirmations(
                plan,
                confirm_plan_sha256=confirm_plan_sha256,
                confirm_router_successor_impact_sha256=(
                    confirm_router_successor_impact_sha256
                ),
                confirm_snapshot_authority_sha256=(
                    confirm_snapshot_authority_sha256
                ),
                confirm_source_successor_authority_sha256=(
                    confirm_source_successor_authority_sha256
                ),
                confirm_unit_permission_authority_sha256=(
                    confirm_unit_permission_authority_sha256
                ),
                confirm_predecessor_selector_sha256=(
                    confirm_predecessor_selector_sha256
                ),
            )
            preliminary = plan

        deploy_lock = _open_existing_lock(
            self.runtime_root / DEPLOY_LOCK_RELATIVE
        )
        router_lock = -1
        try:
            router_lock = _open_lock(self.runtime_root / LOCK_RELATIVE)
            self.checkpoint("bootstrap-router-apply-lock-acquired")
            _ensure_private_directory(self.transaction_root)
            journal = self._load_journal(
                operation_id,
                expected_plan_sha256=confirm_plan_sha256,
                expected_impact_sha256=(
                    confirm_router_successor_impact_sha256
                ),
            )
            if journal is None:
                if (
                    self.intent_path.exists()
                    or self.intent_path.is_symlink()
                    or self.authority_path.exists()
                    or self.authority_path.is_symlink()
                    or self.fence_path.exists()
                    or self.fence_path.is_symlink()
                ):
                    raise RouterSuccessorError(
                        "router publication exists without its journal"
                    )
                locked_plan, _control, _router_payloads = self._build_plan(
                    target_sha,
                    operation_id,
                )
                if preliminary is not None and locked_plan != preliminary:
                    raise RouterSuccessorError(
                        "bootstrap-router plan changed before intent"
                    )
                self._assert_confirmations(
                    locked_plan,
                    confirm_plan_sha256=confirm_plan_sha256,
                    confirm_router_successor_impact_sha256=(
                        confirm_router_successor_impact_sha256
                    ),
                    confirm_snapshot_authority_sha256=(
                        confirm_snapshot_authority_sha256
                    ),
                    confirm_source_successor_authority_sha256=(
                        confirm_source_successor_authority_sha256
                    ),
                    confirm_unit_permission_authority_sha256=(
                        confirm_unit_permission_authority_sha256
                    ),
                    confirm_predecessor_selector_sha256=(
                        confirm_predecessor_selector_sha256
                    ),
                )
                journal = {
                    "schema_version": 1,
                    "status": "applying",
                    "phase": "intent",
                    "operation_id": operation_id,
                    "plan": locked_plan,
                    "plan_sha256": confirm_plan_sha256,
                    "router_successor_impact_sha256": (
                        confirm_router_successor_impact_sha256
                    ),
                    "created_at": _now_utc(),
                    "completed_at": None,
                }
                self._write_journal(journal)
                self.checkpoint("bootstrap-router-intent")
            plan = self._validate_plan(journal["plan"])
            if (
                plan["target_source_sha"] != target_sha
                or plan["operation_id"] != operation_id
            ):
                raise RouterSuccessorError(
                    "durable bootstrap-router target differs"
                )
            self._assert_confirmations(
                plan,
                confirm_plan_sha256=confirm_plan_sha256,
                confirm_router_successor_impact_sha256=(
                    confirm_router_successor_impact_sha256
                ),
                confirm_snapshot_authority_sha256=(
                    confirm_snapshot_authority_sha256
                ),
                confirm_source_successor_authority_sha256=(
                    confirm_source_successor_authority_sha256
                ),
                confirm_unit_permission_authority_sha256=(
                    confirm_unit_permission_authority_sha256
                ),
                confirm_predecessor_selector_sha256=(
                    confirm_predecessor_selector_sha256
                ),
            )
            control, router_payloads = self._reprove_plan(plan)

            if self.authority_path.exists() or self.authority_path.is_symlink():
                _intent, authority = self._load_expected_publications(
                    plan,
                    journal,
                )
                if authority is None:
                    raise RouterSuccessorError(
                        "router authority disappeared during recovery"
                    )
                if (
                    _private_file(self.selector_path, mode=0o700)[1]
                    != plan["successor_selector_sha256"]
                ):
                    raise RouterSuccessorError(
                        "completed router authority lacks successor selector"
                    )
                _ensure_private_directory(self.control_root)
                _ensure_private_directory(self.install_parent)
                self._install_control_release(plan, control)
                self._install_router_files(plan, router_payloads)
                if self._phase_index(journal["phase"]) < self._phase_index(
                    "authority-published"
                ):
                    journal["phase"] = "authority-published"
                    self._write_journal(journal)
                self._remove_interlock(plan)
                journal["status"] = "completed"
                journal["phase"] = "completed"
                self._write_journal(journal)
                self.checkpoint("bootstrap-router-completed")
                return authority

            self._install_interlock(plan)
            if self._phase_index(journal["phase"]) < self._phase_index(
                "interlock-ready"
            ):
                self._advance(
                    journal,
                    "interlock-ready",
                    "bootstrap-router-interlock-ready",
                )
            # The durable fence now makes every predecessor controller reject
            # deployment before its first Git write even if this process loses
            # its advisory lock and power at any following instruction.
            control, router_payloads = self._reprove_plan(plan)
            _ensure_private_directory(self.control_root)
            self._install_control_release(plan, control)
            if self._phase_index(journal["phase"]) < self._phase_index(
                "control-release-ready"
            ):
                self._advance(
                    journal,
                    "control-release-ready",
                    "bootstrap-router-control-release-ready",
                )
            _ensure_private_directory(self.install_parent)
            self._install_router_files(plan, router_payloads)
            if self._phase_index(journal["phase"]) < self._phase_index(
                "router-files-ready"
            ):
                self._advance(
                    journal,
                    "router-files-ready",
                    "bootstrap-router-files-ready",
                )
            if self._phase_index(journal["phase"]) < self._phase_index(
                "selector-swap-intent"
            ):
                self._advance(
                    journal,
                    "selector-swap-intent",
                    "bootstrap-router-selector-swap-intent",
                )
            intent = self._publish_intent(plan, journal)
            self.checkpoint("bootstrap-router-selector-intent-published")
            self._switch_selector(plan, router_payloads)
            if self._phase_index(journal["phase"]) < self._phase_index(
                "selector-switched"
            ):
                self._advance(
                    journal,
                    "selector-switched",
                    "bootstrap-router-selector-switched",
                )
            self._reprove_plan(plan)
            if self._phase_index(journal["phase"]) < self._phase_index(
                "authority-commit-intent"
            ):
                journal["completed_at"] = _now_utc()
                self._advance(
                    journal,
                    "authority-commit-intent",
                    "bootstrap-router-authority-commit-intent",
                )
            completed_at = journal.get("completed_at")
            if not isinstance(completed_at, str):
                raise RouterSuccessorError(
                    "router completion timestamp is unavailable"
                )
            authority = self._completion_authority(intent, completed_at)
            _publish_create_once(
                self.authority_path,
                authority,
                operation_id,
            )
            observed, _authority_digest = _private_json(self.authority_path)
            if observed != authority:
                raise RouterSuccessorError("published router authority differs")
            self.checkpoint("bootstrap-router-authority-published")
            if self._phase_index(journal["phase"]) < self._phase_index(
                "authority-published"
            ):
                self._advance(
                    journal,
                    "authority-published",
                    "bootstrap-router-authority-sealed",
                )
            self._load_expected_publications(plan, journal)
            self._reprove_plan(plan)
            if (
                _private_file(self.selector_path, mode=0o700)[1]
                != plan["successor_selector_sha256"]
            ):
                raise RouterSuccessorError(
                    "successor selector changed before interlock release"
                )
            self._remove_interlock(plan)
            self.checkpoint("bootstrap-router-interlock-removed")
            journal["status"] = "completed"
            journal["phase"] = "completed"
            self._write_journal(journal)
            self.checkpoint("bootstrap-router-completed")
            return authority
        finally:
            if router_lock >= 0:
                os.close(router_lock)
            os.close(deploy_lock)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--confirm-router-successor-impact-sha256")
    parser.add_argument("--confirm-snapshot-authority-sha256")
    parser.add_argument("--confirm-source-successor-authority-sha256")
    parser.add_argument("--confirm-unit-permission-authority-sha256")
    parser.add_argument("--confirm-predecessor-selector-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manager = BootstrapRouterSuccessorManager(
        Path.cwd(),
        RUNTIME_ROOT,
        PRODUCTION_ROOT,
    )
    try:
        confirmations = (
            arguments.confirm_plan_sha256,
            arguments.confirm_router_successor_impact_sha256,
            arguments.confirm_snapshot_authority_sha256,
            arguments.confirm_source_successor_authority_sha256,
            arguments.confirm_unit_permission_authority_sha256,
            arguments.confirm_predecessor_selector_sha256,
        )
        if arguments.action == "plan":
            if any(value is not None for value in confirmations):
                raise RouterSuccessorError(
                    "bootstrap-router plan does not accept apply confirmations"
                )
            result = manager.plan(
                target_sha=arguments.sha,
                operation_id=arguments.operation_id,
            )
        else:
            if any(value is None for value in confirmations):
                raise RouterSuccessorError(
                    "bootstrap-router apply requires every confirmation"
                )
            result = manager.apply(
                target_sha=arguments.sha,
                operation_id=arguments.operation_id,
                confirm_plan_sha256=arguments.confirm_plan_sha256,
                confirm_router_successor_impact_sha256=(
                    arguments.confirm_router_successor_impact_sha256
                ),
                confirm_snapshot_authority_sha256=(
                    arguments.confirm_snapshot_authority_sha256
                ),
                confirm_source_successor_authority_sha256=(
                    arguments.confirm_source_successor_authority_sha256
                ),
                confirm_unit_permission_authority_sha256=(
                    arguments.confirm_unit_permission_authority_sha256
                ),
                confirm_predecessor_selector_sha256=(
                    arguments.confirm_predecessor_selector_sha256
                ),
            )
        sys.stdout.buffer.write(_canonical_bytes(result))
        return 0
    except (RouterSuccessorError, snapshot.SnapshotError) as exc:
        print(f"bootstrap-router successor failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
