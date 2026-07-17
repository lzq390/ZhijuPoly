#!/usr/bin/env python3
"""Prefetch and seal every artifact needed by the F -> B maintenance window.

This command runs from the clean, owner-private F bootstrap clone before the
legacy checkout is taken over.  It never changes the production checkout,
service state, PostgreSQL, or the live asset pointer.  Its only mutations are:

* content-addressed Docker image pulls;
* a private, hash-locked Worker wheel cache;
* a private F+B Git bundle and recovery-tool archive; and
* one atomically published readiness record under the deployment runtime.

The later Pull prepare path consumes the readiness record and must reject any
cache drift instead of downloading artifacts after the old readers stop.
"""

from __future__ import annotations

import argparse
import base64
import csv
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
import zipfile

import bootstrap_pull_deploy
import bridge_deploy_core
import worker_slot_runtime
from worker_slot_runtime import WorkerSlotError, inspect_base_python_identity


PREFETCH_SCHEMA_VERSION = 2
PREFETCH_STATUS = "ready"
SOURCE_URL = "https://github.com/lzq390/ZhijuPoly"
POSTGRES16_IMAGE = (
    "postgres:16-alpine@"
    "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
WORKER_LOCK_RELATIVE_PATH = "workers/monomer_md_worker/requirements.lock"
CONTROL_MANIFEST_RELATIVE_PATH = "scripts/control-release.json"
REQUIRED_RECOVERY_PATHS = {
    "scripts/bootstrap_pull_deploy.py",
    "scripts/control-release.json",
    "scripts/control_runtime_selector.py",
    "scripts/install_legacy_takeover_prerequisites.py",
    "scripts/legacy_takeover.py",
    "scripts/legacy_takeover_evidence.py",
    "scripts/nexpoly-legacy-takeover",
    "scripts/nexpoly-maintenance-prefetch",
    "scripts/pull_deploy_controller.py",
    "scripts/site_helper_contracts.py",
}
PREFETCH_CONTROLLER_PATHS = {
    "scripts/bootstrap_pull_deploy.py",
    "scripts/bridge_deploy_core.py",
    "scripts/maintenance_prefetch.py",
    "scripts/worker_slot_runtime.py",
}
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_ID_RE = DIGEST_RE
OPERATION_ID_RE = re.compile(r"^prefetch-[a-z0-9][a-z0-9._-]{7,111}$")
ROLE_IMAGE_ROOTS = dict(bridge_deploy_core.IMAGE_ROOTS)
READY_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "source",
    "source_readiness",
    "source_readiness_sha256",
    "controller",
    "policy",
    "policy_sha256",
    "docker_config",
    "git_bundle",
    "images",
    "wheel_caches",
    "asset",
    "recovery_tools",
    "created_at",
    "identity_sha256",
}


class MaintenancePrefetchError(RuntimeError):
    """The maintenance artifact set cannot be proven complete and immutable."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino)
            != (current.st_dev, current.st_ino)
        ):
            raise MaintenancePrefetchError(
                f"file changed while being made durable: {path}"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_private_tree(root: Path) -> None:
    """Flush every private artifact before a durable readiness record exists."""

    require_private_directory(root)
    directories = [root]
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            require_private_directory(path)
            directories.append(path)
        elif stat.S_ISREG(metadata.st_mode):
            if (
                path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
            ):
                raise MaintenancePrefetchError(
                    f"artifact file is unsafe before fsync: {path}"
                )
            fsync_file(path)
        else:
            raise MaintenancePrefetchError(
                f"artifact tree contains a symlink or special file: {path}"
            )
    for directory in sorted(
        directories,
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        fsync_directory(directory)


def atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def atomic_json(path: Path, document: object) -> None:
    atomic_bytes(path, canonical_json_bytes(document) + b"\n")


def require_private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MaintenancePrefetchError(
            f"private directory is unavailable: {path}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MaintenancePrefetchError(f"private directory is unsafe: {path}")
    if create:
        fsync_directory(path)
        fsync_directory(path.parent)


def require_private_file(path: Path, *, mode: int = 0o600) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MaintenancePrefetchError(f"private file is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_nlink != 1
    ):
        raise MaintenancePrefetchError(f"private file is unsafe: {path}")


def parse_private_literal_env(path: Path) -> dict[str, str]:
    require_private_file(path)
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MaintenancePrefetchError(
            f"private deployment configuration is unreadable: {path}"
        ) from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise MaintenancePrefetchError(
                f"invalid deployment configuration line {number}"
            )
        key, value = line.split("=", 1)
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None
            or key in result
            or any(character in value for character in ("\x00", "\r", "\n"))
        ):
            raise MaintenancePrefetchError(
                f"invalid deployment configuration line {number}"
            )
        result[key] = value
    return result


def remove_private_tree(path: Path) -> None:
    """Remove only an owner-private, symlink-free staging tree."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MaintenancePrefetchError(
            f"stale staging path is unavailable: {path}"
        ) from exc
    if stat.S_ISREG(metadata.st_mode):
        require_private_file(path)
        path.unlink()
        fsync_directory(path.parent)
        return
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise MaintenancePrefetchError(f"stale staging path is unsafe: {path}")
    for child in path.iterdir():
        remove_private_tree(child)
    path.rmdir()
    fsync_directory(path.parent)


def require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise MaintenancePrefetchError(f"{label} is not a full commit SHA")
    return value


def require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise MaintenancePrefetchError(f"{label} is not a sha256 digest")
    return value


def require_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_ID_RE.fullmatch(value) is None:
        raise MaintenancePrefetchError("prefetch operation ID is invalid")
    return value


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def clean_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", "/home/devuser"),
        "PATH": SAFE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONNOUSERSITE": "1",
    }
    if extra:
        environment.update(extra)
    return environment


@contextlib.contextmanager
def private_umask():  # type: ignore[no-untyped-def]
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def directory_inventory_digest(root: Path) -> tuple[str, int]:
    """Bind private artifact content and mode while rejecting unsafe entries."""

    require_private_directory(root)
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if (
                path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                raise MaintenancePrefetchError(
                    f"inventory directory is unsafe: {path}"
                )
            digest.update(
                b"D\0"
                + relative
                + b"\0"
                + format(stat.S_IMODE(metadata.st_mode), "04o").encode("ascii")
                + b"\0"
            )
        elif stat.S_ISREG(metadata.st_mode):
            if (
                path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
            ):
                raise MaintenancePrefetchError(f"inventory file is unsafe: {path}")
            file_count += 1
            digest.update(
                b"F\0"
                + relative
                + b"\0"
                + format(stat.S_IMODE(metadata.st_mode), "04o").encode("ascii")
                + b"\0"
            )
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise MaintenancePrefetchError(
                f"inventory contains a symlink or special file: {path}"
            )
    return "sha256:" + digest.hexdigest(), file_count


def validate_image_reference(reference: object, role: str) -> str:
    root = ROLE_IMAGE_ROOTS.get(role)
    if (
        root is None
        or not isinstance(reference, str)
        or reference.count("@") != 1
        or not reference.startswith(root + "@")
    ):
        raise MaintenancePrefetchError(f"{role} image is not an exact GHCR digest")
    require_digest(reference.split("@", 1)[1], f"{role} OCI reference")
    return reference


def canonical_repo_digest(reference: str) -> str:
    named, digest = reference.rsplit("@", 1)
    last_slash = named.rfind("/")
    last_colon = named.rfind(":")
    repository = named[:last_colon] if last_colon > last_slash else named
    return f"{repository}@{digest}"


def validate_image_evidence(
    document: object,
    *,
    expected_reference: str,
    expected_revision: str | None,
    enforce_revision: bool = True,
) -> dict[str, Any]:
    fields = {
        "digest_ref",
        "oci_reference_digest",
        "local_image_id",
        "repo_digests",
        "revision",
        "source",
        "version",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise MaintenancePrefetchError("prefetched image evidence is malformed")
    if (
        document.get("digest_ref") != expected_reference
        or document.get("oci_reference_digest") != expected_reference.split("@", 1)[1]
    ):
        raise MaintenancePrefetchError("prefetched OCI reference digest differs")
    local_id = require_digest(document.get("local_image_id"), "local Docker image ID")
    if IMAGE_ID_RE.fullmatch(local_id) is None:
        raise MaintenancePrefetchError("local Docker image ID is malformed")
    repo_digests = document.get("repo_digests")
    canonical_reference = canonical_repo_digest(expected_reference)
    if (
        not isinstance(repo_digests, list)
        or repo_digests != sorted(set(repo_digests))
        or canonical_reference not in repo_digests
        or any(
            not isinstance(value, str)
            or value.count("@") != 1
            or DIGEST_RE.fullmatch(value.split("@", 1)[1]) is None
            for value in repo_digests
        )
    ):
        raise MaintenancePrefetchError("Docker RepoDigests lack the exact OCI digest")
    revision = document.get("revision")
    if revision is not None and (
        not isinstance(revision, str)
        or not revision
        or len(revision) > 256
        or enforce_revision
        and SHA_RE.fullmatch(revision) is None
    ):
        raise MaintenancePrefetchError("prefetched image revision label is malformed")
    if enforce_revision and revision != expected_revision:
        raise MaintenancePrefetchError("prefetched image revision label differs")
    source = document.get("source")
    version = document.get("version")
    if enforce_revision and (
        source != SOURCE_URL
        or version != f"sha-{expected_revision}"
    ):
        raise MaintenancePrefetchError(
            "prefetched application image source/version labels differ"
        )
    if not enforce_revision and (
        source is not None
        and (not isinstance(source, str) or len(source) > 1024)
        or version is not None
        and (not isinstance(version, str) or len(version) > 256)
    ):
        raise MaintenancePrefetchError(
            "prefetched restore image labels are malformed"
        )
    return {
        "digest_ref": expected_reference,
        "oci_reference_digest": expected_reference.split("@", 1)[1],
        "local_image_id": local_id,
        "repo_digests": list(repo_digests),
        "revision": revision,
        "source": source,
        "version": version,
    }


def wheel_cache_key(
    lock_payload: bytes,
    *,
    base_python_identity_sha256: str,
    platform: str,
) -> dict[str, str]:
    identity = require_digest(
        base_python_identity_sha256,
        "Worker base Python identity",
    )
    worker_lock = sha256_bytes(lock_payload)
    key = sha256_bytes(
        canonical_json_bytes(
            {
                "worker_lock_sha256": worker_lock,
                "base_python_identity_sha256": identity,
                "platform": platform,
            }
        )
    )
    return {
        "wheel_cache_key": key,
        "worker_lock_sha256": worker_lock,
        "requirements_sha256": sha256_bytes(
            lock_payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        ),
        "base_python_identity_sha256": identity,
        "platform": platform,
    }


def wheel_archive_evidence(path: Path) -> dict[str, Any]:
    require_private_file(path)
    if path.suffix != ".whl":
        raise MaintenancePrefetchError(f"wheel cache has a non-wheel file: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            records = [
                name
                for name in names
                if name.endswith(".dist-info/RECORD")
            ]
            if len(records) != 1 or len(names) != len(set(names)):
                raise MaintenancePrefetchError(
                    f"wheel RECORD inventory is invalid: {path.name}"
                )
            record_payload = archive.read(records[0])
            rows = list(
                csv.reader(record_payload.decode("utf-8").splitlines())
            )
            if not rows or any(len(row) != 3 or not row[0] for row in rows):
                raise MaintenancePrefetchError(
                    f"wheel RECORD rows are invalid: {path.name}"
                )
            by_name = {name: archive.read(name) for name in names}
            recorded_paths = {row[0] for row in rows}
            if recorded_paths != set(names):
                raise MaintenancePrefetchError(
                    f"wheel RECORD paths are incomplete: {path.name}"
                )
            for member, hash_value, size_value in rows:
                payload = by_name[member]
                if member == records[0]:
                    if hash_value or size_value:
                        raise MaintenancePrefetchError(
                            f"wheel RECORD self-entry is not empty: {path.name}"
                        )
                    continue
                if not hash_value.startswith("sha256="):
                    raise MaintenancePrefetchError(
                        f"wheel RECORD hash is not sha256: {path.name}"
                    )
                encoded = hash_value.split("=", 1)[1]
                padding = "=" * (-len(encoded) % 4)
                try:
                    expected = base64.urlsafe_b64decode(encoded + padding)
                except ValueError as exc:
                    raise MaintenancePrefetchError(
                        f"wheel RECORD hash is malformed: {path.name}"
                    ) from exc
                if (
                    hashlib.sha256(payload).digest() != expected
                    or size_value != str(len(payload))
                ):
                    raise MaintenancePrefetchError(
                        f"wheel RECORD content differs: {path.name}"
                    )
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise MaintenancePrefetchError(
            f"wheel archive is invalid: {path.name}"
        ) from exc
    return {
        "filename": path.name,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "record_path": records[0],
        "record_sha256": sha256_bytes(record_payload),
        "member_count": len(names),
    }


def validate_wheel_cache_completion(
    root: Path,
    *,
    wheel_cache_key_value: str,
    worker_lock_sha256: str,
    base_python_identity_sha256: str,
) -> dict[str, Any]:
    complete_path = root / ".complete.json"
    lock_path = root / "requirements.lock"
    require_private_file(complete_path)
    require_private_file(lock_path)
    try:
        document = json.loads(complete_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaintenancePrefetchError(
            "wheel cache completion marker is invalid"
        ) from exc
    fields = {
        "schema_version",
        "wheel_cache_key",
        "worker_lock_sha256",
        "base_python_identity_sha256",
        "offline_install_verified",
        "pip_check_verified",
        "wheels",
    }
    wheels = document.get("wheels") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("wheel_cache_key") != wheel_cache_key_value
        or document.get("worker_lock_sha256") != worker_lock_sha256
        or sha256_file(lock_path) != worker_lock_sha256
        or document.get("base_python_identity_sha256")
        != base_python_identity_sha256
        or document.get("offline_install_verified") is not True
        or document.get("pip_check_verified") is not True
        or not isinstance(wheels, list)
        or not wheels
    ):
        raise MaintenancePrefetchError(
            "wheel cache completion binding differs"
        )
    actual_wheels = sorted(root.glob("*.whl"), key=lambda path: path.name)
    observed = [wheel_archive_evidence(path) for path in actual_wheels]
    if wheels != observed:
        raise MaintenancePrefetchError(
            "wheel cache exact archive inventory differs"
        )
    expected_names = {
        ".complete.json",
        ".owner.json",
        "requirements.lock",
        *(record["filename"] for record in observed),
    }
    actual_names = {path.name for path in root.iterdir()}
    if actual_names != expected_names or any(path.is_dir() for path in root.iterdir()):
        raise MaintenancePrefetchError(
            "wheel cache contains missing or extra entries"
        )
    return dict(document)


def _validate_source_record(document: object, label: str) -> dict[str, str]:
    if not isinstance(document, dict) or set(document) != {"sha", "tree"}:
        raise MaintenancePrefetchError(f"{label} Git identity is malformed")
    return {
        "sha": require_sha(document.get("sha"), f"{label} SHA"),
        "tree": require_sha(document.get("tree"), f"{label} tree"),
    }


def validate_git_bundle_evidence(
    document: object,
    *,
    runtime_root: Path,
    operation_id: str,
    authority: Mapping[str, str],
    target: Mapping[str, str],
) -> dict[str, Any]:
    fields = {
        "path",
        "sha256",
        "size_bytes",
        "authority",
        "target",
        "bundle_verified",
        "temporary_clone_fsck",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise MaintenancePrefetchError("prefetched Git bundle evidence is malformed")
    path = Path(str(document.get("path", "")))
    expected = (
        runtime_root
        / "prefetch"
        / operation_id
        / "git"
        / "authority-and-bridge.bundle"
    )
    if path != expected or not path.is_absolute():
        raise MaintenancePrefetchError("prefetched Git bundle path differs")
    require_private_file(path)
    if (
        document.get("sha256") != sha256_file(path)
        or document.get("size_bytes") != path.stat().st_size
        or document.get("bundle_verified") is not True
        or document.get("temporary_clone_fsck") is not True
        or _validate_source_record(document.get("authority"), "bundle authority")
        != dict(authority)
        or _validate_source_record(document.get("target"), "bundle target")
        != dict(target)
    ):
        raise MaintenancePrefetchError("prefetched Git bundle evidence differs")
    return dict(document)


def validate_wheel_record(
    document: object,
    *,
    runtime_root: Path,
    expected_sources: set[str],
) -> dict[str, Any]:
    fields = {
        "source_sha",
        "source_tree",
        "lock_path",
        "worker_lock_sha256",
        "requirements_sha256",
        "base_python",
        "base_python_identity_sha256",
        "platform",
        "wheel_cache_key",
        "cache_path",
        "inventory_sha256",
        "file_count",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise MaintenancePrefetchError("prefetched wheel cache record is malformed")
    source_sha = require_sha(document.get("source_sha"), "wheel source SHA")
    require_sha(document.get("source_tree"), "wheel source tree")
    if source_sha not in expected_sources:
        raise MaintenancePrefetchError("wheel cache belongs to an unexpected source")
    if document.get("lock_path") != WORKER_LOCK_RELATIVE_PATH:
        raise MaintenancePrefetchError("wheel cache lock path differs")
    for field in (
        "worker_lock_sha256",
        "requirements_sha256",
        "base_python_identity_sha256",
        "wheel_cache_key",
        "inventory_sha256",
    ):
        require_digest(document.get(field), f"wheel cache {field}")
    base_python = Path(str(document.get("base_python", "")))
    if not base_python.is_absolute() or ".." in base_python.parts:
        raise MaintenancePrefetchError("wheel cache base Python path is unsafe")
    if document.get("platform") != sys.platform:
        raise MaintenancePrefetchError("wheel cache platform differs")
    cache = Path(str(document.get("cache_path", "")))
    if (
        cache
        != runtime_root / "wheel-cache" / document["wheel_cache_key"]
        or not cache.is_absolute()
    ):
        raise MaintenancePrefetchError("wheel cache path differs")
    inventory, count = directory_inventory_digest(cache)
    validate_wheel_cache_completion(
        cache,
        wheel_cache_key_value=str(document["wheel_cache_key"]),
        worker_lock_sha256=str(document["worker_lock_sha256"]),
        base_python_identity_sha256=str(
            document["base_python_identity_sha256"]
        ),
    )
    if (
        document.get("inventory_sha256") != inventory
        or document.get("file_count") != count
        or not isinstance(count, int)
        or count < 4
    ):
        raise MaintenancePrefetchError("wheel cache inventory differs")
    return dict(document)


def validate_asset_evidence(
    document: object,
    *,
    expected_digest: str,
) -> dict[str, Any]:
    fields = {
        "root",
        "manifest_path",
        "manifest_sha256",
        "predecessor_asset_digest",
        "changed_asset_trees",
        "inventory_sha256",
        "file_count",
        "read_only",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise MaintenancePrefetchError("prefetched asset evidence is malformed")
    expected_digest = require_digest(expected_digest, "asset manifest")
    root = Path(str(document.get("root", "")))
    expected_root = Path("/data/lzq/nexpoly-assets/releases") / expected_digest.removeprefix(
        "sha256:"
    )
    manifest = root / "ASSET-MANIFEST.json"
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise MaintenancePrefetchError("schema-v2 asset root is unavailable") from exc
    if (
        root != expected_root
        or root.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o500
        or document.get("manifest_path") != str(manifest)
        or document.get("manifest_sha256") != expected_digest
        or sha256_file(manifest) != expected_digest
        or document.get("read_only") is not True
    ):
        raise MaintenancePrefetchError("prefetched schema-v2 asset identity differs")
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaintenancePrefetchError("schema-v2 asset manifest is invalid") from exc
    if (
        not isinstance(parsed, dict)
        or parsed.get("schema_version") != 2
        or parsed.get("predecessor_asset_digest")
        != "sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2"
        or parsed.get("changed_asset_trees") != ["byteff2"]
        or document.get("predecessor_asset_digest")
        != parsed.get("predecessor_asset_digest")
        or document.get("changed_asset_trees") != ["byteff2"]
    ):
        raise MaintenancePrefetchError("schema-v2 predecessor contract differs")
    assets = parsed.get("assets")
    if (
        not isinstance(assets, dict)
        or set(assets) != {"backend-data", "byteff2", "database", "model"}
    ):
        raise MaintenancePrefetchError("schema-v2 asset tree set differs")
    expected_files = {"ASSET-MANIFEST.json"}
    expected_directories = set(assets)
    for tree_name, records in assets.items():
        if not isinstance(records, list):
            raise MaintenancePrefetchError("schema-v2 asset records are malformed")
        seen: set[str] = set()
        for record in records:
            if (
                not isinstance(record, dict)
                or set(record) != {"path", "sha256", "size"}
                or not isinstance(record.get("path"), str)
                or not record["path"]
                or record["path"] in seen
                or Path(record["path"]).is_absolute()
                or ".." in Path(record["path"]).parts
                or not isinstance(record.get("sha256"), str)
                or re.fullmatch(r"^[0-9a-f]{64}$", record["sha256"]) is None
                or not isinstance(record.get("size"), int)
                or isinstance(record.get("size"), bool)
                or record["size"] < 0
            ):
                raise MaintenancePrefetchError(
                    "schema-v2 asset record is invalid"
                )
            seen.add(record["path"])
            relative = Path(tree_name) / record["path"]
            expected_files.add(relative.as_posix())
            current = relative.parent
            while current != Path("."):
                expected_directories.add(current.as_posix())
                current = current.parent
            path = root / relative
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise MaintenancePrefetchError(
                    "schema-v2 asset file is unavailable"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o400
                or metadata.st_nlink != 1
                or metadata.st_size != record["size"]
                or sha256_file(path).removeprefix("sha256:") != record["sha256"]
            ):
                raise MaintenancePrefetchError(
                    "schema-v2 asset file differs from its manifest"
                )
    digest = hashlib.sha256()
    count = 0
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if stat.S_ISDIR(metadata.st_mode):
            if (
                path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o500
            ):
                raise MaintenancePrefetchError("asset directory is unsafe or writable")
            actual_directories.add(relative.decode("utf-8"))
            digest.update(b"D\0" + relative + b"\0")
        elif stat.S_ISREG(metadata.st_mode):
            if (
                path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o400
                or metadata.st_nlink != 1
            ):
                raise MaintenancePrefetchError("asset file is unsafe or writable")
            count += 1
            actual_files.add(relative.decode("utf-8"))
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise MaintenancePrefetchError("asset contains a symlink or special file")
    inventory = "sha256:" + digest.hexdigest()
    if (
        actual_files != expected_files
        or actual_directories != expected_directories
        or document.get("inventory_sha256") != inventory
        or document.get("file_count") != count
        or count < 1
    ):
        raise MaintenancePrefetchError("prefetched asset inventory differs")
    return dict(document)


def validate_recovery_tools(
    document: object,
    *,
    runtime_root: Path,
    operation_id: str,
    expected_sources: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != set(expected_sources):
        raise MaintenancePrefetchError("recovery-tool source set is incomplete")
    result: dict[str, Any] = {}
    for role, identity in expected_sources.items():
        record = document.get(role)
        fields = {
            "source_sha",
            "source_tree",
            "root",
            "inventory_sha256",
            "file_count",
            "paths",
        }
        if not isinstance(record, dict) or set(record) != fields:
            raise MaintenancePrefetchError("recovery-tool record is malformed")
        if (
            record.get("source_sha") != identity["sha"]
            or record.get("source_tree") != identity["tree"]
        ):
            raise MaintenancePrefetchError("recovery-tool Git identity differs")
        root = runtime_root / "prefetch" / operation_id / "tools" / role
        paths = record.get("paths")
        if (
            record.get("root") != str(root)
            or not isinstance(paths, list)
            or paths != sorted(set(paths))
            or not REQUIRED_RECOVERY_PATHS.issubset(set(paths))
        ):
            raise MaintenancePrefetchError("recovery-tool path inventory differs")
        inventory, count = directory_inventory_digest(root)
        if (
            record.get("inventory_sha256") != inventory
            or record.get("file_count") != count
            or count != len(paths)
        ):
            raise MaintenancePrefetchError("recovery-tool content inventory differs")
        result[role] = dict(record)
    return result


def validate_controller_evidence(
    document: object,
    *,
    source_root: Path,
    authority: Mapping[str, str],
) -> dict[str, Any]:
    fields = {"source_root", "source_sha", "source_tree", "files"}
    if not isinstance(document, dict) or set(document) != fields:
        raise MaintenancePrefetchError("prefetch controller evidence is malformed")
    if (
        document.get("source_root") != str(source_root)
        or document.get("source_sha") != authority["sha"]
        or document.get("source_tree") != authority["tree"]
    ):
        raise MaintenancePrefetchError(
            "prefetch controller is not bound to authority F"
        )
    files = document.get("files")
    if (
        not isinstance(files, dict)
        or set(files) != PREFETCH_CONTROLLER_PATHS
    ):
        raise MaintenancePrefetchError(
            "prefetch controller file evidence is incomplete"
        )
    for relative, digest in files.items():
        require_digest(digest, f"controller file {relative}")
        path = source_root / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise MaintenancePrefetchError(
                f"prefetch controller file is unavailable: {relative}"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or sha256_file(path) != digest
        ):
            raise MaintenancePrefetchError(
                f"prefetch controller file changed: {relative}"
            )
    return dict(document)


def validate_created_at(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise MaintenancePrefetchError("prefetch timestamp is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise MaintenancePrefetchError("prefetch timestamp is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != dt.timedelta(0)
        or parsed.microsecond != 0
        or parsed > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    ):
        raise MaintenancePrefetchError("prefetch timestamp is not canonical UTC")
    return value


def ready_identity(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: document[key]
        for key in sorted(READY_FIELDS - {"identity_sha256"})
    }


def validate_ready_evidence(
    document: object,
    *,
    runtime_root: Path,
) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != READY_FIELDS
        or document.get("schema_version") != PREFETCH_SCHEMA_VERSION
        or document.get("status") != PREFETCH_STATUS
    ):
        raise MaintenancePrefetchError("maintenance prefetch evidence is malformed")
    operation_id = require_operation_id(document.get("operation_id"))
    source = document.get("source")
    if not isinstance(source, dict) or set(source) != {"authority", "target"}:
        raise MaintenancePrefetchError("maintenance source identity is malformed")
    authority = _validate_source_record(source["authority"], "prefetch authority")
    target = _validate_source_record(source["target"], "prefetch target")
    readiness = document.get("source_readiness")
    if (
        not isinstance(readiness, dict)
        or readiness.get("schema_version") != 2
        or readiness.get("ready") is not True
        or readiness.get("source_sha") != authority["sha"]
        or readiness.get("source_tree") != authority["tree"]
        or readiness.get("branch") != "main"
        or readiness.get("origin") != bootstrap_pull_deploy.REPOSITORY_SSH_URL
        or readiness.get("remote_names") != ["origin"]
        or readiness.get("origin_fetch_urls")
        != [bootstrap_pull_deploy.REPOSITORY_SSH_URL]
        or readiness.get("origin_push_urls")
        != [bootstrap_pull_deploy.REPOSITORY_SSH_URL]
        or readiness.get("origin_main_sha") != authority["sha"]
        or readiness.get("standalone_object_database") is not True
        or readiness.get("shallow") is not False
        or readiness.get("dirty_entries") != 0
        or readiness.get("ignored_entries") != 0
        or readiness.get("unreachable_objects") != 0
        or readiness.get("replace_refs") != 0
        or readiness.get("special_index_entries") != 0
        or readiness.get("sparse_index") is not False
        or readiness.get("group_or_world_writable") is not False
        or (
            "owner_private" in readiness
            and readiness.get("owner_private") is not True
        )
    ):
        raise MaintenancePrefetchError("bootstrap source readiness is incomplete")
    if document.get("source_readiness_sha256") != sha256_bytes(
        canonical_json_bytes(readiness)
    ):
        raise MaintenancePrefetchError("bootstrap source readiness digest differs")
    validate_controller_evidence(
        document.get("controller"),
        source_root=Path(str(readiness.get("source_root", ""))),
        authority=authority,
    )
    try:
        policy = bridge_deploy_core.validate_policy(document.get("policy"))
    except Exception as exc:
        raise MaintenancePrefetchError("prefetch bridge policy is invalid") from exc
    if (
        document.get("policy_sha256") != sha256_bytes(canonical_json_bytes(policy))
        or policy["target_sha"] != target["sha"]
        or policy["target_tree"] != target["tree"]
    ):
        raise MaintenancePrefetchError("prefetch policy binding differs")
    docker_config = document.get("docker_config")
    expected_docker_config = runtime_root / "config/docker"
    if (
        not isinstance(docker_config, dict)
        or set(docker_config) != {"path"}
        or docker_config.get("path") != str(expected_docker_config)
    ):
        raise MaintenancePrefetchError("private Docker configuration binding differs")
    validate_git_bundle_evidence(
        document.get("git_bundle"),
        runtime_root=runtime_root,
        operation_id=operation_id,
        authority=authority,
        target=target,
    )
    images = document.get("images")
    if not isinstance(images, dict) or set(images) != {
        "authority",
        "target",
        "postgres_restore",
    }:
        raise MaintenancePrefetchError("prefetched image set is incomplete")
    if (
        not isinstance(images["authority"], dict)
        or set(images["authority"]) != set(ROLE_IMAGE_ROOTS)
        or not isinstance(images["target"], dict)
        or set(images["target"]) != set(ROLE_IMAGE_ROOTS)
    ):
        raise MaintenancePrefetchError("prefetched application image set is incomplete")
    for role in sorted(ROLE_IMAGE_ROOTS):
        authority_record = images["authority"][role]
        authority_ref = validate_image_reference(
            authority_record.get("digest_ref")
            if isinstance(authority_record, dict)
            else None,
            role,
        )
        validate_image_evidence(
            authority_record,
            expected_reference=authority_ref,
            expected_revision=authority["sha"],
        )
        target_ref = policy["target_images"][role]
        target_record = images["target"][role]
        validate_image_evidence(
            target_record,
            expected_reference=target_ref,
            expected_revision=target["sha"],
        )
    validate_image_evidence(
        images["postgres_restore"],
        expected_reference=POSTGRES16_IMAGE,
        expected_revision=None,
        enforce_revision=False,
    )
    wheels = document.get("wheel_caches")
    if not isinstance(wheels, list) or not 1 <= len(wheels) <= 2:
        raise MaintenancePrefetchError("prefetched wheel caches are incomplete")
    normalized_wheels = [
        validate_wheel_record(
            record,
            runtime_root=runtime_root,
            expected_sources={authority["sha"], target["sha"]},
        )
        for record in wheels
    ]
    if [record["source_sha"] for record in normalized_wheels] != sorted(
        {authority["sha"], target["sha"]}
    ):
        raise MaintenancePrefetchError("F/B wheel source coverage is incomplete")
    validate_asset_evidence(
        document.get("asset"),
        expected_digest=policy["asset_manifest_digest"],
    )
    validate_recovery_tools(
        document.get("recovery_tools"),
        runtime_root=runtime_root,
        operation_id=operation_id,
        expected_sources={"authority": authority, "target": target},
    )
    validate_created_at(document.get("created_at"))
    if document.get("identity_sha256") != sha256_bytes(
        canonical_json_bytes(ready_identity(document))
    ):
        raise MaintenancePrefetchError("prefetch readiness identity differs")
    return dict(document)


class CommandRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            list(arguments),
            cwd=cwd,
            env=None if env is None else dict(env),
            check=check,
            text=text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


class MaintenancePrefetch:
    def __init__(
        self,
        *,
        source_root: Path,
        runtime_root: Path,
        operation_id: str,
        authority_images: Mapping[str, str],
        docker_config: Path,
        base_python: Path,
        base_python_identity_sha256: str,
        runner: CommandRunner | None = None,
        allow_test: bool = False,
    ) -> None:
        self.source_root = source_root.absolute()
        self.runtime_root = runtime_root.absolute()
        self.allow_test = allow_test
        if (
            not allow_test
            and (
                self.runtime_root != bootstrap_pull_deploy.RUNTIME_ROOT
                or self.runtime_root.resolve()
                != bootstrap_pull_deploy.RUNTIME_ROOT
            )
        ):
            raise MaintenancePrefetchError(
                "production prefetch requires the fixed runtime root"
            )
        self.operation_id = require_operation_id(operation_id)
        if set(authority_images) != set(ROLE_IMAGE_ROOTS):
            raise MaintenancePrefetchError("authority image set is incomplete")
        self.authority_images = {
            role: validate_image_reference(authority_images[role], role)
            for role in sorted(ROLE_IMAGE_ROOTS)
        }
        self.docker_config = docker_config.absolute()
        expected_docker_config = self.runtime_root / "config/docker"
        if self.docker_config != expected_docker_config:
            raise MaintenancePrefetchError(
                "Docker configuration must use the fixed private runtime path"
            )
        if not base_python.is_absolute() or ".." in base_python.parts:
            raise MaintenancePrefetchError("Worker base Python path is unsafe")
        self.base_python = base_python
        self.base_python_identity_sha256 = require_digest(
            base_python_identity_sha256,
            "Worker base Python identity",
        )
        self.runner = runner or CommandRunner()
        self.prefetch_root = self.runtime_root / "prefetch"
        self.operation_root = self.prefetch_root / self.operation_id
        self.ready_path = self.operation_root / "ready.json"

    def _validate_controller_root(self) -> None:
        if self.allow_test:
            return
        expected = self.source_root.resolve()
        module_paths = {
            Path(__file__).resolve(),
            Path(str(bootstrap_pull_deploy.__file__)).resolve(),
            Path(str(bridge_deploy_core.__file__)).resolve(),
            Path(str(worker_slot_runtime.__file__)).resolve(),
        }
        if any(path.parents[1] != expected for path in module_paths):
            raise MaintenancePrefetchError(
                "prefetch executable/imports are not loaded from authority F"
            )

    def _validate_production_contract(self) -> None:
        if self.allow_test:
            return
        values = parse_private_literal_env(
            self.runtime_root / "config/deploy.env"
        )
        if (
            values.get("NEXPOLY_RUNTIME_ROOT") != str(self.runtime_root)
            or values.get("NEXPOLY_WORKER_BASE_PYTHON")
            != str(self.base_python)
            or values.get("NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256")
            != self.base_python_identity_sha256
        ):
            raise MaintenancePrefetchError(
                "prefetch Python/runtime parameters differ from sealed deploy.env"
            )

    def _git(
        self,
        *arguments: str,
        check: bool = True,
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        return self.runner.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                *arguments,
            ],
            cwd=self.source_root,
            env=clean_environment(),
            check=check,
            text=text,
        )

    def _git_show(self, source_sha: str, relative: str) -> bytes:
        result = self._git("show", f"{source_sha}:{relative}", text=False)
        payload = result.stdout
        if not isinstance(payload, bytes) or not payload:
            raise MaintenancePrefetchError(
                f"Git source payload is unavailable: {relative}"
            )
        return payload

    def _source_and_policy(
        self,
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, str], dict[str, Any]]:
        try:
            readiness = bootstrap_pull_deploy.bootstrap_source_readiness(
                self.source_root
            )
        except Exception as exc:
            raise MaintenancePrefetchError(
                "bootstrap source readiness failed before prefetch"
            ) from exc
        authority = {
            "sha": require_sha(readiness.get("source_sha"), "authority SHA"),
            "tree": require_sha(readiness.get("source_tree"), "authority tree"),
        }
        try:
            policy = bridge_deploy_core.parse_policy(
                self._git_show(
                    authority["sha"],
                    bridge_deploy_core.POLICY_RELATIVE_PATH,
                )
            )
        except Exception as exc:
            raise MaintenancePrefetchError(
                "authority bridge policy is unavailable"
            ) from exc
        target = {
            "sha": policy["target_sha"],
            "tree": policy["target_tree"],
        }
        actual_target_tree = str(
            self._git("rev-parse", f"{target['sha']}^{{tree}}").stdout
        ).strip()
        ancestor = self._git(
            "merge-base",
            "--is-ancestor",
            target["sha"],
            authority["sha"],
            check=False,
        )
        if actual_target_tree != target["tree"] or ancestor.returncode != 0:
            raise MaintenancePrefetchError(
                "bridge target is not the exact authority ancestor"
            )
        return readiness, authority, target, policy

    def _controller_evidence(
        self,
        *,
        authority: Mapping[str, str],
    ) -> dict[str, Any]:
        self._validate_controller_root()
        files: dict[str, str] = {}
        for relative in sorted(PREFETCH_CONTROLLER_PATHS):
            path = self.source_root / relative
            payload = self._git_show(authority["sha"], relative)
            try:
                metadata = path.lstat()
                current = path.read_bytes()
            except OSError as exc:
                raise MaintenancePrefetchError(
                    f"authority controller file is unavailable: {relative}"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
                or metadata.st_nlink != 1
                or current != payload
            ):
                raise MaintenancePrefetchError(
                    f"running controller differs from authority F: {relative}"
                )
            files[relative] = sha256_bytes(payload)
        document = {
            "source_root": str(self.source_root),
            "source_sha": authority["sha"],
            "source_tree": authority["tree"],
            "files": files,
        }
        return validate_controller_evidence(
            document,
            source_root=self.source_root,
            authority=authority,
        )

    def _publish_bundle(
        self,
        *,
        authority: Mapping[str, str],
        target: Mapping[str, str],
    ) -> dict[str, Any]:
        root = self.operation_root / "git"
        require_private_directory(root, create=True)
        final = root / "authority-and-bridge.bundle"
        for stale in sorted(root.iterdir(), key=lambda path: path.name):
            if stale == final:
                continue
            if stale.name.startswith((".bundle-", ".verify-")):
                remove_private_tree(stale)
                continue
            raise MaintenancePrefetchError(
                "Git bundle staging contains an unrecognized entry"
            )
        if final.exists() or final.is_symlink():
            require_private_file(final)
        else:
            temporary = root / f".bundle-{secrets.token_hex(16)}"
            self._git(
                "bundle",
                "create",
                str(temporary),
                "refs/heads/main",
            )
            os.chmod(temporary, 0o600)
            require_private_file(temporary)
            fsync_file(temporary)
            self._verify_bundle(
                temporary,
                authority=authority,
                target=target,
            )
            os.replace(temporary, final)
            fsync_file(final)
            fsync_directory(root)
        self._verify_bundle(
            final,
            authority=authority,
            target=target,
        )
        return {
            "path": str(final),
            "sha256": sha256_file(final),
            "size_bytes": final.stat().st_size,
            "authority": dict(authority),
            "target": dict(target),
            "bundle_verified": True,
            "temporary_clone_fsck": True,
        }

    def _verify_bundle(
        self,
        path: Path,
        *,
        authority: Mapping[str, str],
        target: Mapping[str, str],
    ) -> None:
        require_private_file(path)
        self.runner.run(
            ["git", "bundle", "verify", str(path)],
            cwd=self.source_root,
            env=clean_environment(),
        )
        listed = self.runner.run(
            ["git", "bundle", "list-heads", str(path)],
            cwd=self.source_root,
            env=clean_environment(),
        )
        advertised: dict[str, str] = {}
        for line in str(listed.stdout).splitlines():
            try:
                sha, reference = line.split(maxsplit=1)
            except ValueError as exc:
                raise MaintenancePrefetchError(
                    "Git bundle advertised refs are malformed"
                ) from exc
            advertised[reference] = require_sha(sha, "bundle advertised SHA")
        if advertised != {"refs/heads/main": authority["sha"]}:
            raise MaintenancePrefetchError(
                "Git bundle advertised main differs from captured authority F"
            )
        with tempfile.TemporaryDirectory(
            prefix=".verify-",
            dir=path.parent,
        ) as raw:
            clone = Path(raw) / "clone"
            self.runner.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--template=/dev/null",
                    str(path),
                    str(clone),
                ],
                env=clean_environment(),
            )
            common = str(
                self.runner.run(
                    ["git", "-C", str(clone), "rev-parse", "--git-common-dir"],
                    env=clean_environment(),
                ).stdout
            ).strip()
            alternates = clone / ".git/objects/info/alternates"
            if common not in {".git", str(clone / ".git")} or alternates.exists():
                raise MaintenancePrefetchError(
                    "temporary bundle clone uses shared/external Git objects"
                )
            for identity in (authority, target):
                observed = str(
                    self.runner.run(
                        [
                            "git",
                            "-C",
                            str(clone),
                            "rev-parse",
                            f"{identity['sha']}^{{tree}}",
                        ],
                        env=clean_environment(),
                    ).stdout
                ).strip()
                if observed != identity["tree"]:
                    raise MaintenancePrefetchError(
                        "temporary bundle clone has another source tree"
                    )
            self.runner.run(
                ["git", "-C", str(clone), "fsck", "--full", "--strict"],
                env=clean_environment(),
            )

    def _pull_image(
        self,
        reference: str,
        *,
        expected_revision: str | None,
    ) -> dict[str, Any]:
        self.runner.run(
            ["docker", "pull", reference],
            env=clean_environment({"DOCKER_CONFIG": str(self.docker_config)}),
        )
        return self._inspect_image(
            reference,
            expected_revision=expected_revision,
        )

    def _inspect_image(
        self,
        reference: str,
        *,
        expected_revision: str | None,
    ) -> dict[str, Any]:
        inspected = self.runner.run(
            ["docker", "image", "inspect", reference],
            env=clean_environment({"DOCKER_CONFIG": str(self.docker_config)}),
        )
        try:
            values = json.loads(str(inspected.stdout))
        except json.JSONDecodeError as exc:
            raise MaintenancePrefetchError("Docker image inspection is invalid") from exc
        if not isinstance(values, list) or len(values) != 1:
            raise MaintenancePrefetchError("Docker selected an ambiguous image")
        value = values[0]
        labels = value.get("Config", {}).get("Labels") or {}
        record = {
            "digest_ref": reference,
            "oci_reference_digest": reference.split("@", 1)[1],
            "local_image_id": value.get("Id"),
            "repo_digests": sorted(set(value.get("RepoDigests") or [])),
            "revision": labels.get("org.opencontainers.image.revision"),
            "source": labels.get("org.opencontainers.image.source"),
            "version": labels.get("org.opencontainers.image.version"),
        }
        return validate_image_evidence(
            record,
            expected_reference=reference,
            expected_revision=expected_revision,
            enforce_revision=expected_revision is not None,
        )

    def _prefetch_images(
        self,
        *,
        authority: Mapping[str, str],
        target: Mapping[str, str],
        policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "authority": {
                role: self._pull_image(
                    self.authority_images[role],
                    expected_revision=authority["sha"],
                )
                for role in sorted(ROLE_IMAGE_ROOTS)
            },
            "target": {
                role: self._pull_image(
                    policy["target_images"][role],
                    expected_revision=target["sha"],
                )
                for role in sorted(ROLE_IMAGE_ROOTS)
            },
            "postgres_restore": self._pull_image(
                POSTGRES16_IMAGE,
                expected_revision=None,
            ),
        }

    def _base_python_identity(self) -> dict[str, Any]:
        try:
            return inspect_base_python_identity(
                self.base_python,
                expected_identity=self.base_python_identity_sha256,
                environment=clean_environment(),
            )
        except WorkerSlotError as exc:
            raise MaintenancePrefetchError(
                "Worker base Python identity differs before wheel prefetch"
            ) from exc

    def _prefetch_wheel_cache(
        self,
        *,
        source: Mapping[str, str],
        base_identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        lock_payload = self._git_show(source["sha"], WORKER_LOCK_RELATIVE_PATH)
        identities = wheel_cache_key(
            lock_payload,
            base_python_identity_sha256=self.base_python_identity_sha256,
            platform=sys.platform,
        )
        cache_root = self.runtime_root / "wheel-cache"
        require_private_directory(cache_root, create=True)
        cache = cache_root / identities["wheel_cache_key"]
        current_staging_prefix = (
            f".{identities['wheel_cache_key']}.{self.operation_id}.prefetch-"
        )
        all_staging_prefix = f".{identities['wheel_cache_key']}."
        for stale in sorted(cache_root.iterdir(), key=lambda path: path.name):
            if not stale.name.startswith(all_staging_prefix):
                continue
            if stale.name.startswith(current_staging_prefix):
                remove_private_tree(stale)
                continue
            raise MaintenancePrefetchError(
                "wheel cache has staging owned by another prefetch operation"
            )
        if not cache.exists() and not cache.is_symlink():
            staging = cache_root / (
                f"{current_staging_prefix}{secrets.token_hex(12)}"
            )
            staging.mkdir(mode=0o700)
            atomic_json(
                staging / ".owner.json",
                {
                    "schema_version": 1,
                    "operation_id": self.operation_id,
                    "wheel_cache_key": identities["wheel_cache_key"],
                    "worker_lock_sha256": identities["worker_lock_sha256"],
                    "base_python_identity_sha256": self.base_python_identity_sha256,
                },
            )
            lock_path = staging / "requirements.lock"
            atomic_bytes(lock_path, lock_payload)
            try:
                network_home = self.operation_root / "network-home"
                require_private_directory(network_home, create=True)
                self.runner.run(
                    [
                        str(base_identity["resolved_path"]),
                        "-I",
                        "-m",
                        "pip",
                        "download",
                        "--require-hashes",
                        "--only-binary=:all:",
                        "--dest",
                        str(staging),
                        "-r",
                        str(lock_path),
                    ],
                    env=clean_environment(
                        {
                            "HOME": str(network_home),
                            "NETRC": os.devnull,
                            "PIP_CONFIG_FILE": os.devnull,
                            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                            "PIP_KEYRING_PROVIDER": "disabled",
                            "PIP_NO_CACHE_DIR": "1",
                            "PIP_NO_INPUT": "1",
                        }
                    ),
                )
                wheel_records = [
                    wheel_archive_evidence(path)
                    for path in sorted(
                        staging.glob("*.whl"),
                        key=lambda path: path.name,
                    )
                ]
                if not wheel_records:
                    raise MaintenancePrefetchError(
                        "Worker wheel prefetch produced no wheel files"
                    )
                self._verify_offline_wheel_install(
                    cache=staging,
                    lock_path=lock_path,
                    base_identity=base_identity,
                )
                atomic_json(
                    staging / ".complete.json",
                    {
                        "schema_version": 1,
                        "wheel_cache_key": identities["wheel_cache_key"],
                        "worker_lock_sha256": identities[
                            "worker_lock_sha256"
                        ],
                        "base_python_identity_sha256": (
                            self.base_python_identity_sha256
                        ),
                        "offline_install_verified": True,
                        "pip_check_verified": True,
                        "wheels": wheel_records,
                    },
                )
                validate_wheel_cache_completion(
                    staging,
                    wheel_cache_key_value=identities["wheel_cache_key"],
                    worker_lock_sha256=identities[
                        "worker_lock_sha256"
                    ],
                    base_python_identity_sha256=(
                        self.base_python_identity_sha256
                    ),
                )
                fsync_private_tree(staging)
                os.replace(staging, cache)
                fsync_private_tree(cache)
                fsync_directory(cache_root)
            except BaseException:
                if staging.exists() and staging.is_dir() and not staging.is_symlink():
                    shutil.rmtree(staging)
                raise
        require_private_directory(cache)
        owner_path = cache / ".owner.json"
        require_private_file(owner_path)
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MaintenancePrefetchError("wheel cache owner is invalid") from exc
        if (
            not isinstance(owner, dict)
            or owner.get("schema_version") != 1
            or owner.get("wheel_cache_key") != identities["wheel_cache_key"]
            or owner.get("worker_lock_sha256") != identities["worker_lock_sha256"]
            or owner.get("base_python_identity_sha256")
            != self.base_python_identity_sha256
            or not isinstance(owner.get("operation_id"), str)
        ):
            raise MaintenancePrefetchError("wheel cache ownership differs")
        validate_wheel_cache_completion(
            cache,
            wheel_cache_key_value=identities["wheel_cache_key"],
            worker_lock_sha256=identities["worker_lock_sha256"],
            base_python_identity_sha256=self.base_python_identity_sha256,
        )
        inventory, count = directory_inventory_digest(cache)
        return {
            "source_sha": source["sha"],
            "source_tree": source["tree"],
            "lock_path": WORKER_LOCK_RELATIVE_PATH,
            **identities,
            "base_python": str(self.base_python),
            "cache_path": str(cache),
            "inventory_sha256": inventory,
            "file_count": count,
        }

    def _verify_offline_wheel_install(
        self,
        *,
        cache: Path,
        lock_path: Path,
        base_identity: Mapping[str, Any],
    ) -> None:
        scratch_root = self.operation_root / "wheel-verify"
        require_private_directory(scratch_root, create=True)
        scratch = scratch_root / f".venv-{secrets.token_hex(12)}"
        if scratch.exists() or scratch.is_symlink():
            raise MaintenancePrefetchError(
                "wheel verification scratch path already exists"
            )
        network_home = self.operation_root / "network-home"
        require_private_directory(network_home, create=True)
        environment = clean_environment(
            {
                "HOME": str(network_home),
                "NETRC": os.devnull,
                "PIP_CONFIG_FILE": os.devnull,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_KEYRING_PROVIDER": "disabled",
                "PIP_NO_CACHE_DIR": "1",
                "PIP_NO_INDEX": "1",
                "PIP_NO_INPUT": "1",
            }
        )
        try:
            self.runner.run(
                [
                    str(base_identity["resolved_path"]),
                    "-I",
                    "-m",
                    "venv",
                    "--copies",
                    str(scratch),
                ],
                env=environment,
            )
            python = scratch / "bin/python"
            self.runner.run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "--no-index",
                    "--find-links",
                    str(cache),
                    "-r",
                    str(lock_path),
                ],
                env=environment,
            )
            self.runner.run(
                [str(python), "-I", "-m", "pip", "check"],
                env=environment,
            )
        finally:
            if scratch.exists() or scratch.is_symlink():
                metadata = scratch.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or scratch.is_symlink()
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077
                ):
                    raise MaintenancePrefetchError(
                        "wheel verification scratch tree is unsafe"
                    )
                shutil.rmtree(scratch)
            fsync_directory(scratch_root)

    def _prefetch_wheels(
        self,
        *,
        authority: Mapping[str, str],
        target: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        base_identity = self._base_python_identity()
        unique = {
            identity["sha"]: identity
            for identity in (authority, target)
        }
        return [
            self._prefetch_wheel_cache(
                source=unique[source_sha],
                base_identity=base_identity,
            )
            for source_sha in sorted(unique)
        ]

    def _asset_evidence(self, expected_digest: str) -> dict[str, Any]:
        root = Path("/data/lzq/nexpoly-assets/releases") / expected_digest.removeprefix(
            "sha256:"
        )
        manifest = root / "ASSET-MANIFEST.json"
        try:
            parsed = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MaintenancePrefetchError(
                "schema-v2 asset manifest is unavailable"
            ) from exc
        digest = hashlib.sha256()
        count = 0
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix().encode("utf-8")
            if stat.S_ISDIR(metadata.st_mode):
                if (
                    path.is_symlink()
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o500
                ):
                    raise MaintenancePrefetchError(
                        "asset directory is unsafe or writable"
                    )
                digest.update(b"D\0" + relative + b"\0")
            elif stat.S_ISREG(metadata.st_mode):
                if (
                    path.is_symlink()
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                    or metadata.st_nlink != 1
                ):
                    raise MaintenancePrefetchError("asset file is unsafe or writable")
                count += 1
                digest.update(b"F\0" + relative + b"\0")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest.update(b"\0")
            else:
                raise MaintenancePrefetchError(
                    "asset contains a symlink or special file"
                )
        evidence = {
            "root": str(root),
            "manifest_path": str(manifest),
            "manifest_sha256": sha256_file(manifest),
            "predecessor_asset_digest": parsed.get("predecessor_asset_digest"),
            "changed_asset_trees": parsed.get("changed_asset_trees"),
            "inventory_sha256": "sha256:" + digest.hexdigest(),
            "file_count": count,
            "read_only": True,
        }
        return validate_asset_evidence(
            evidence,
            expected_digest=expected_digest,
        )

    def _control_paths(self, source_sha: str) -> list[str]:
        try:
            manifest = json.loads(
                self._git_show(source_sha, CONTROL_MANIFEST_RELATIVE_PATH)
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MaintenancePrefetchError("control-release manifest is invalid") from exc
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, list):
            raise MaintenancePrefetchError("control-release file list is invalid")
        paths = {
            value.get("source")
            for value in files
            if isinstance(value, dict) and isinstance(value.get("source"), str)
        }
        paths.add(CONTROL_MANIFEST_RELATIVE_PATH)
        paths.update(REQUIRED_RECOVERY_PATHS)
        if any(
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in Path(path).parts
            for path in paths
        ):
            raise MaintenancePrefetchError("control-release path is unsafe")
        return sorted(paths)

    def _stage_recovery_tools(
        self,
        *,
        role: str,
        source: Mapping[str, str],
    ) -> dict[str, Any]:
        root = self.operation_root / "tools" / role
        if root.exists() or root.is_symlink():
            require_private_directory(root)
        else:
            root.mkdir(parents=True, mode=0o700)
        paths = self._control_paths(source["sha"])
        for relative in paths:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target.parent, 0o700)
            for stale in target.parent.glob(f".{target.name}.*.tmp"):
                remove_private_tree(stale)
            payload = self._git_show(source["sha"], relative)
            tree = str(
                self._git("ls-tree", source["sha"], "--", relative).stdout
            ).strip()
            if not tree:
                raise MaintenancePrefetchError(
                    f"recovery tool is absent from source: {relative}"
                )
            mode = 0o700 if tree.split(maxsplit=1)[0] == "100755" else 0o600
            if target.exists() or target.is_symlink():
                require_private_file(target, mode=mode)
                if target.read_bytes() != payload:
                    raise MaintenancePrefetchError(
                        f"staged recovery tool differs: {relative}"
                    )
            else:
                atomic_bytes(target, payload, mode=mode)
        observed = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        if observed != set(paths):
            raise MaintenancePrefetchError(
                "recovery-tool staging contains missing or extra files"
            )
        fsync_private_tree(root)
        fsync_directory(root.parent)
        inventory, count = directory_inventory_digest(root)
        return {
            "source_sha": source["sha"],
            "source_tree": source["tree"],
            "root": str(root),
            "inventory_sha256": inventory,
            "file_count": count,
            "paths": paths,
        }

    def _prefetch_recovery_tools(
        self,
        *,
        authority: Mapping[str, str],
        target: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            "authority": self._stage_recovery_tools(
                role="authority",
                source=authority,
            ),
            "target": self._stage_recovery_tools(
                role="target",
                source=target,
            ),
        }

    def _revalidate_ready(
        self,
        existing: object,
    ) -> dict[str, Any]:
        validated = validate_ready_evidence(
            existing,
            runtime_root=self.runtime_root,
        )
        readiness, authority, target, policy = self._source_and_policy()
        if (
            validated["source_readiness"] != readiness
            or validated["source"] != {
                "authority": authority,
                "target": target,
            }
            or validated["policy"] != policy
        ):
            raise MaintenancePrefetchError(
                "ready prefetch source or policy changed"
            )
        self._verify_bundle(
            Path(validated["git_bundle"]["path"]),
            authority=authority,
            target=target,
        )
        for role in sorted(ROLE_IMAGE_ROOTS):
            current_authority = self._inspect_image(
                self.authority_images[role],
                expected_revision=authority["sha"],
            )
            if current_authority != validated["images"]["authority"][role]:
                raise MaintenancePrefetchError(
                    "prefetched authority image changed"
                )
            current_target = self._inspect_image(
                policy["target_images"][role],
                expected_revision=target["sha"],
            )
            if current_target != validated["images"]["target"][role]:
                raise MaintenancePrefetchError("prefetched target image changed")
        current_postgres = self._inspect_image(
            POSTGRES16_IMAGE,
            expected_revision=None,
        )
        if current_postgres != validated["images"]["postgres_restore"]:
            raise MaintenancePrefetchError(
                "prefetched PostgreSQL restore image changed"
            )
        base_identity = self._base_python_identity()
        identities = {
            identity["sha"]: identity
            for identity in (authority, target)
        }
        by_source = {
            record["source_sha"]: record
            for record in validated["wheel_caches"]
        }
        for source_sha, source in identities.items():
            record = by_source.get(source_sha)
            if record is None:
                raise MaintenancePrefetchError("F/B wheel cache record is missing")
            expected = wheel_cache_key(
                self._git_show(source_sha, WORKER_LOCK_RELATIVE_PATH),
                base_python_identity_sha256=self.base_python_identity_sha256,
                platform=sys.platform,
            )
            if (
                any(record.get(key) != value for key, value in expected.items())
                or record.get("source_tree") != source["tree"]
                or record.get("base_python")
                != str(self.base_python)
                or base_identity.get("identity_sha256")
                != self.base_python_identity_sha256
            ):
                raise MaintenancePrefetchError(
                    "prefetched wheel cache source binding changed"
                )
        current_tools = self._prefetch_recovery_tools(
            authority=authority,
            target=target,
        )
        if current_tools != validated["recovery_tools"]:
            raise MaintenancePrefetchError("prefetched recovery tools changed")
        return validated

    def _open_lock(self):  # type: ignore[no-untyped-def]
        lock = self.runtime_root / "state/deploy.lock"
        flags = (
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lock, flags)
        except OSError as exc:
            raise MaintenancePrefetchError(
                "deploy.lock is unavailable or unsafe"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            current = lock.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise MaintenancePrefetchError("deploy.lock is unsafe")
            stream = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = -1
            return stream
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _assert_lock_inode(self, descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        current = (self.runtime_root / "state/deploy.lock").lstat()
        if (
            (metadata.st_dev, metadata.st_ino)
            != (current.st_dev, current.st_ino)
            or not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
        ):
            raise MaintenancePrefetchError(
                "deploy.lock path changed while locked"
            )

    def _remove_ephemeral_network_state(self) -> None:
        for path in (
            self.operation_root / "network-home",
            self.operation_root / "wheel-verify",
        ):
            if not path.exists() and not path.is_symlink():
                continue
            remove_private_tree(path)

    def run(self) -> dict[str, Any]:
        with private_umask():
            return self._run()

    def _run(self) -> dict[str, Any]:
        require_private_directory(self.runtime_root)
        require_private_directory(self.docker_config)
        docker_config_path = self.docker_config / "config.json"
        require_private_file(docker_config_path)
        with self._open_lock() as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MaintenancePrefetchError(
                    "another deployment holds deploy.lock"
                ) from exc
            self._assert_lock_inode(stream.fileno())
            self._validate_production_contract()
            require_private_directory(self.prefetch_root, create=True)
            require_private_directory(self.operation_root, create=True)
            for stale in self.operation_root.glob(".ready.json.*.tmp"):
                remove_private_tree(stale)
            if self.ready_path.exists() or self.ready_path.is_symlink():
                require_private_file(self.ready_path)
                try:
                    existing = json.loads(
                        self.ready_path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise MaintenancePrefetchError(
                        "existing prefetch readiness is invalid"
                    ) from exc
                return self._revalidate_ready(existing)
            readiness, authority, target, policy = self._source_and_policy()
            controller = self._controller_evidence(authority=authority)
            document: dict[str, Any] = {
                "schema_version": PREFETCH_SCHEMA_VERSION,
                "status": PREFETCH_STATUS,
                "operation_id": self.operation_id,
                "source": {
                    "authority": authority,
                    "target": target,
                },
                "source_readiness": readiness,
                "source_readiness_sha256": sha256_bytes(
                    canonical_json_bytes(readiness)
                ),
                "controller": controller,
                "policy": policy,
                "policy_sha256": sha256_bytes(canonical_json_bytes(policy)),
                "docker_config": {
                    "path": str(self.docker_config),
                },
                "git_bundle": self._publish_bundle(
                    authority=authority,
                    target=target,
                ),
                "images": self._prefetch_images(
                    authority=authority,
                    target=target,
                    policy=policy,
                ),
                "wheel_caches": self._prefetch_wheels(
                    authority=authority,
                    target=target,
                ),
                "asset": self._asset_evidence(policy["asset_manifest_digest"]),
                "recovery_tools": self._prefetch_recovery_tools(
                    authority=authority,
                    target=target,
                ),
                "created_at": utc_now(),
                "identity_sha256": "",
            }
            self._remove_ephemeral_network_state()
            document["identity_sha256"] = sha256_bytes(
                canonical_json_bytes(ready_identity(document))
            )
            final_readiness, final_authority, final_target, final_policy = (
                self._source_and_policy()
            )
            if (
                final_readiness != readiness
                or final_authority != authority
                or final_target != target
                or final_policy != policy
                or self._controller_evidence(authority=authority) != controller
            ):
                raise MaintenancePrefetchError(
                    "authority source/policy changed during prefetch"
                )
            self._assert_lock_inode(stream.fileno())
            fsync_private_tree(self.operation_root)
            validated = validate_ready_evidence(
                document,
                runtime_root=self.runtime_root,
            )
            atomic_json(self.ready_path, validated)
            return validate_ready_evidence(
                json.loads(self.ready_path.read_text(encoding="utf-8")),
                runtime_root=self.runtime_root,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prefetch exact F/B maintenance artifacts without touching production",
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--authority-backend-image", required=True)
    parser.add_argument("--authority-web-image", required=True)
    parser.add_argument("--docker-config", required=True, type=Path)
    parser.add_argument("--base-python", required=True, type=Path)
    parser.add_argument("--base-python-identity-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        controller = MaintenancePrefetch(
            source_root=arguments.source_root,
            runtime_root=arguments.runtime_root,
            operation_id=arguments.operation_id,
            authority_images={
                "backend": arguments.authority_backend_image,
                "web": arguments.authority_web_image,
            },
            docker_config=arguments.docker_config,
            base_python=arguments.base_python,
            base_python_identity_sha256=arguments.base_python_identity_sha256,
        )
        result = controller.run()
    except (
        MaintenancePrefetchError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"maintenance-prefetch: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
