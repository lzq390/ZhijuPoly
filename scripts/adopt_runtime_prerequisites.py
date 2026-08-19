#!/usr/bin/python3 -I
"""Adopt production prerequisites and narrow permission successors.

This tool runs from an exact private Git checkout.  It installs only tracked
configuration helpers for the original plan/apply transaction.  Its separate
permission-* transaction can owner-harden the adopted checkout and publish a
content-bound successor authority.  Its unit-permission-* transaction replaces
only the legacy MD user-unit inode while proving that MD and DFT keep running.
No transaction contacts PostgreSQL or restarts a service.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import ctypes
import datetime as dt
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import types
from typing import Any, Callable


sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
OPERATION_RE = re.compile(r"adopt-prereq-[a-z0-9][a-z0-9._-]{7,95}\Z")
AUTHORITY_KIND = "manual-runtime-adoption-prerequisites"
TRANSACTION_DIRECTORY = Path("state/adopted-prerequisite-transactions")
AUTHORITY_PATH = Path("state/adopted-prerequisites.json")
PERMISSION_TRANSACTION_DIRECTORY = Path(
    "state/adopted-git-permission-transactions"
)
PERMISSION_AUTHORITY_PATH = Path("state/adopted-git-permissions.json")
PERMISSION_AUTHORITY_KIND = "manual-runtime-adoption-permission-hardening"
PERMISSION_OPERATION_RE = re.compile(
    r"adopt-git-permission-[a-z0-9][a-z0-9._-]{7,95}\Z"
)
PERMISSION_TRANSACTION_PHASES = frozenset(
    {
        "intent",
        "permission-change-intent",
        "permission-ready",
        "source-verified",
        "authority-commit-intent",
        "completed",
        "aborted",
    }
)
# The wrapper embeds one complete permission marker plan plus bounded source
# and adoption evidence, so its journal/authority ceiling must exceed the
# marker engine's independent 128 MiB ceiling.
PERMISSION_JSON_MAX_BYTES = 256 * 1024 * 1024
SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY = Path(
    "state/adopted-git-permission-source-successor-transactions"
)
SOURCE_SUCCESSOR_AUTHORITY_PATH = Path(
    "state/adopted-git-permission-source-successor.json"
)
SOURCE_SUCCESSOR_AUTHORITY_KIND = (
    "manual-runtime-adoption-git-permission-source-successor"
)
SOURCE_SUCCESSOR_POLICY = (
    "nexpoly-adopted-git-permission-source-successor-v1"
)
SOURCE_SUCCESSOR_IMPACT_POLICY = (
    "nexpoly-adopted-git-permission-source-successor-impact-v1"
)
SOURCE_SUCCESSOR_VERIFIER_POLICY = (
    "nexpoly-frozen-predecessor-verifier-agreement-v1"
)
SOURCE_SUCCESSOR_PUBLICATION_POLICY = (
    "nexpoly-source-successor-authority-publication-v1"
)
SOURCE_SUCCESSOR_REPOSITORY_TRANSITION_POLICY = (
    "nexpoly-production-repository-materialization-transition-v1"
)
SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF = "refs/remotes/nexpoly-deploy/main"
SOURCE_SUCCESSOR_PREPARED_REF_PREFIX = "refs/nexpoly/prepared/"
SOURCE_SUCCESSOR_GIT_AUXILIARY_POLICY = (
    "baseline-exact-fetch-head-and-transition-reflogs-only-v1"
)
SOURCE_SUCCESSOR_GIT_OBJECT_STORAGE_POLICY = (
    "canonical-loose-pack-index-rev-commit-graph-no-locks-v1"
)
SOURCE_SUCCESSOR_OPERATION_RE = re.compile(
    r"adopt-git-successor-[a-z0-9][a-z0-9._-]{7,95}\Z"
)
SOURCE_SUCCESSOR_TRANSACTION_PHASES = frozenset(
    {
        "intent",
        "predecessor-verified",
        "source-verified",
        "authority-commit-intent",
        "completed",
        "aborted",
    }
)
SOURCE_SUCCESSOR_JSON_MAX_BYTES = 32 * 1024 * 1024
UNIT_PERMISSION_TRANSACTION_DIRECTORY = Path(
    "state/adopted-unit-permission-transactions"
)
UNIT_PERMISSION_BACKUP_DIRECTORY = Path(
    "state/adopted-unit-permission-backups"
)
UNIT_PERMISSION_AUTHORITY_PATH = Path(
    "state/adopted-unit-permissions.json"
)
UNIT_PERMISSION_AUTHORITY_KIND = (
    "manual-runtime-adoption-unit-permission-hardening"
)
UNIT_PERMISSION_OPERATION_RE = re.compile(
    r"adopt-unit-permission-[a-z0-9][a-z0-9._-]{7,95}\Z"
)
UNIT_PERMISSION_TRANSACTION_PHASES = frozenset(
    {
        "intent",
        "replacement-intent",
        "unit-ready",
        "source-verified",
        "authority-commit-intent",
        "completed",
        "aborted",
    }
)
UNIT_PERMISSION_JSON_MAX_BYTES = 16 * 1024 * 1024
MD_UNIT_NAME = "nexpoly-monomer-md-worker.service"
DFT_UNIT_NAME = "nexpoly-monomer-dft-worker.service"
MD_UNIT_PATH = Path("/home/devuser/.config/systemd/user") / MD_UNIT_NAME
DFT_UNIT_PATH = Path("/home/devuser/.config/systemd/user") / DFT_UNIT_NAME
ADOPTED_DEPLOYMENT_PATH = Path("state/adopted-deployment.json")
BOOTSTRAP_CONTROL_PATH = Path("state/bootstrap-control.json")
ADOPTION_AUTHORITY_KIND = "manual-runtime-adoption"
SOURCE_READINESS_FIELDS = {
    "schema_version",
    "ready",
    "source_root",
    "source_sha",
    "source_tree",
    "branch",
    "origin",
    "remote_names",
    "origin_fetch_urls",
    "origin_push_urls",
    "origin_main_sha",
    "standalone_object_database",
    "shallow",
    "dirty_entries",
    "ignored_entries",
    "unreachable_objects",
    "replace_refs",
    "special_index_entries",
    "sparse_index",
    "owner_private",
    "group_or_world_writable",
}
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2

TRACKED_INSTALLS = (
    (
        "ops/config/bootstrap-quiesce.example",
        "bootstrap-quiesce",
        0o700,
        "reviewed-wrapper",
    ),
    (
        "ops/config/bootstrap-status.example",
        "bootstrap-status",
        0o700,
        "reviewed-wrapper",
    ),
    (
        "ops/config/bootstrap-resume-unchanged.example",
        "bootstrap-resume-unchanged",
        0o700,
        "reviewed-wrapper",
    ),
    (
        "ops/config/bootstrap-rollback.example",
        "bootstrap-rollback",
        0o700,
        "reviewed-wrapper",
    ),
    (
        "ops/config/bootstrap-active-jobs-probe.example",
        "bootstrap-active-jobs-probe",
        0o700,
        "adopted-non-applicable-fail-closed",
    ),
    (
        "ops/config/bootstrap-legacy-runtime-status.example",
        "bootstrap-legacy-runtime-status",
        0o700,
        "adopted-non-applicable-fail-closed",
    ),
    (
        "ops/config/bootstrap-legacy-runtime-resume-unchanged.example",
        "bootstrap-legacy-runtime-resume-unchanged",
        0o700,
        "adopted-non-applicable-fail-closed",
    ),
    (
        "ops/config/bootstrap-legacy-runtime-restore.example",
        "bootstrap-legacy-runtime-restore",
        0o700,
        "adopted-non-applicable-fail-closed",
    ),
    (
        "ops/config/deployment-mutable-data-audit.example",
        "deployment-mutable-data-audit",
        0o700,
        "generic-mutable-audit",
    ),
    (
        "ops/config/mutable-data-audit.pg_service.conf.example",
        "mutable-data-audit.pg_service.conf",
        0o600,
        "generic-mutable-audit-service",
    ),
)
UNIT_PERMISSION_SUCCESSOR_BLOBS = tuple(
    record[0] for record in TRACKED_INSTALLS
) + (
    "scripts/bootstrap_pull_deploy.py",
    "scripts/git_source_trust.py",
)
UNIT_PERMISSION_SUCCESSOR_V2_BLOBS = UNIT_PERMISSION_SUCCESSOR_BLOBS + (
    "scripts/bridge_deploy_core.py",
)
SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS = (
    "scripts/bootstrap_pull_deploy.py",
    "scripts/git_source_trust.py",
)
SOURCE_SUCCESSOR_MUTATIONS = {
    "services": False,
    "source": False,
    "source_refs": False,
    "database": False,
    "credentials": False,
    "git_permissions": False,
    "units": False,
    "runtime_authority": True,
}
PGPASS_NAME = "mutable-data-audit.pgpass"


def _load_git_source_trust() -> Any:
    module_name = "nexpoly_adopt_git_source_trust"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).with_name("git_source_trust.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Git source trust policy cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


GIT_SOURCE_TRUST = _load_git_source_trust()


class PrerequisiteError(RuntimeError):
    pass


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _open_readonly_noatime(
    path: Path,
    *,
    directory_fd: int | None = None,
) -> tuple[int, bool]:
    """Open one path without following its final component.

    ``O_NOATIME`` is best-effort because some filesystems do not implement it.
    Callers must not infer an end-to-end atime guarantee from the returned flag:
    Git subprocesses open their own files, so plan reports only claim an atime
    zero-write when both source and runtime mounts suppress atime updates.
    """

    base_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        return os.open(path, base_flags | noatime, dir_fd=directory_fd), bool(
            noatime
        )
    except OSError as exc:
        if not noatime or exc.errno not in {errno.EPERM, errno.EINVAL, errno.ENOTSUP}:
            raise
        return os.open(path, base_flags, dir_fd=directory_fd), False


def _file_digest(path: Path, *, mode: int | None = None) -> str:
    descriptor, _noatime = _open_readonly_noatime(path)
    try:
        metadata = os.fstat(descriptor)
        digest = _descriptor_digest(descriptor)
        after = os.fstat(descriptor)
        observed = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or mode is not None
            and stat.S_IMODE(metadata.st_mode) != mode
            or _stat_identity(metadata) != _stat_identity(after)
            or _stat_identity(metadata) != _stat_identity(observed)
        ):
            raise PrerequisiteError(f"private file changed while reading: {path}")
        return digest
    finally:
        os.close(descriptor)


def _file_digest_at(
    directory_fd: int,
    name: str,
    *,
    mode: int,
) -> str:
    descriptor = _open_private_regular_at(
        directory_fd,
        name,
        mode=mode,
    )
    try:
        return _descriptor_digest(descriptor)
    finally:
        os.close(descriptor)


def _mount_suppresses_atime(path: Path) -> bool:
    """Conservatively prove that reads through ``path`` cannot update atime."""

    try:
        resolved = path.resolve(strict=True)
        records = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    selected: tuple[int, set[str]] | None = None
    for record in records:
        fields = record.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 6:
            continue
        mount_point = Path(
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        options = set(fields[5].split(","))
        length = len(os.fspath(mount_point))
        if selected is None or length > selected[0]:
            selected = (length, options)
    if selected is None:
        return False
    options = selected[1]
    return "ro" in options or "noatime" in options


def _descriptor_digest(descriptor: int) -> str:
    hasher = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        hasher.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return "sha256:" + hasher.hexdigest()


def _descriptor_bytes(descriptor: int, *, maximum_bytes: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = bytearray()
    while True:
        remaining = maximum_bytes + 1 - len(payload)
        if remaining <= 0:
            raise PrerequisiteError("private file is oversized")
        block = os.read(descriptor, min(1024 * 1024, remaining))
        if not block:
            break
        payload.extend(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return bytes(payload)


def _stable_regular_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    """Return every non-atime field needed to detect an in-place rewrite."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_private_json_descriptor(
    descriptor: int,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[dict[str, object], str, os.stat_result]:
    """Parse and hash exactly the same stable bytes from one open inode."""

    before = os.fstat(descriptor)
    payload = _descriptor_bytes(descriptor, maximum_bytes=maximum_bytes)
    after = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or _stable_regular_identity(before) != _stable_regular_identity(after)
    ):
        raise PrerequisiteError(f"{label} changed while reading")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PrerequisiteError(f"{label} is invalid") from exc
    if not isinstance(document, dict):
        raise PrerequisiteError(f"{label} is not an object")
    return document, _digest(payload), after


def _canonical_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _canonical_digest(document: object) -> str:
    return _digest(_canonical_bytes(document))


def _has_exact_schema_version(document: object, expected: int) -> bool:
    """Reject JSON booleans/floats that compare equal to an integer version."""

    return (
        isinstance(document, dict)
        and type(document.get("schema_version")) is int
        and document["schema_version"] == expected
    )


def _private_tree_inventory_digest(root: Path) -> str:
    """Match the controller's content inventory for one private operation tree."""

    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or root.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise PrerequisiteError(f"private inventory root is unsafe: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        observed = path.lstat()
        if observed.st_uid != os.geteuid():
            raise PrerequisiteError(
                f"private inventory entry has another owner: {path}"
            )
        if stat.S_ISLNK(observed.st_mode):
            digest.update(
                b"L\0"
                + relative
                + b"\0"
                + os.fsencode(os.readlink(path))
                + b"\0"
            )
        elif stat.S_ISDIR(observed.st_mode):
            if observed.st_mode & 0o022:
                raise PrerequisiteError(
                    f"private inventory directory is writable: {path}"
                )
            digest.update(b"D\0" + relative + b"\0")
        elif stat.S_ISREG(observed.st_mode):
            if observed.st_nlink != 1:
                raise PrerequisiteError(
                    f"private inventory file has another link: {path}"
                )
            digest.update(b"F\0" + relative + b"\0")
            descriptor, _noatime = _open_readonly_noatime(path)
            try:
                before = os.fstat(descriptor)
                while True:
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                after = os.fstat(descriptor)
                if _stable_regular_identity(before) != _stable_regular_identity(
                    after
                ):
                    raise PrerequisiteError(
                        f"private inventory file changed: {path}"
                    )
            finally:
                os.close(descriptor)
            digest.update(b"\0")
        else:
            raise PrerequisiteError(
                f"private inventory contains a special file: {path}"
            )
    return "sha256:" + digest.hexdigest()


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _parse_canonical_utc_timestamp(value: object) -> dt.datetime | None:
    """Return a real UTC second only for the one canonical wire format."""

    pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        return None
    try:
        parsed = dt.datetime.strptime(
            value,
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        return None
    return parsed


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_operation_id(value: str) -> str:
    if OPERATION_RE.fullmatch(value) is None:
        raise PrerequisiteError("prerequisite adoption operation ID is invalid")
    return value


def _require_permission_operation_id(value: str) -> str:
    if not isinstance(value, str) or PERMISSION_OPERATION_RE.fullmatch(value) is None:
        raise PrerequisiteError("permission hardening operation ID is invalid")
    return value


def _require_source_successor_operation_id(value: str) -> str:
    if SOURCE_SUCCESSOR_OPERATION_RE.fullmatch(value) is None:
        raise PrerequisiteError(
            "Git permission source successor operation ID is invalid"
        )
    return value


def _require_unit_permission_operation_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or UNIT_PERMISSION_OPERATION_RE.fullmatch(value) is None
    ):
        raise PrerequisiteError(
            "unit permission hardening operation ID is invalid"
        )
    return value


def _require_sha(value: str) -> str:
    if SHA_RE.fullmatch(value) is None:
        raise PrerequisiteError("source SHA must be 40 lowercase hexadecimal characters")
    return value


def _private_metadata(
    path: Path,
    *,
    mode: int,
    regular: bool,
    require_single_link: bool = True,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PrerequisiteError(f"required private path is unavailable: {path}") from exc
    expected_type = stat.S_ISREG if regular else stat.S_ISDIR
    if (
        not expected_type(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or regular
        and require_single_link
        and metadata.st_nlink != 1
    ):
        raise PrerequisiteError(f"required private path is unsafe: {path}")
    return metadata


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _open_private_directory(
    path: Path,
    *,
    mode: int = 0o700,
    parent_fd: int | None = None,
) -> int:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise PrerequisiteError(f"required private directory is unsafe: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise PrerequisiteError(f"required private directory is unsafe: {path}")
        if parent_fd is None:
            observed = path.stat(follow_symlinks=False)
        else:
            observed = os.stat(path, dir_fd=parent_fd, follow_symlinks=False)
        if _directory_identity(observed) != _directory_identity(metadata):
            raise PrerequisiteError(f"private directory path changed: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _owned_directory_inode_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int]:
    """Identity stable across the permission transaction's planned chmods."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
    )


def _open_owned_directory_for_cas(
    path: Path,
    *,
    parent_fd: int | None = None,
) -> tuple[int, tuple[int, int, int, int]]:
    """Pin an owned directory while deliberately allowing its mode to change."""

    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise PrerequisiteError(
            f"permission authority directory is unsafe: {path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        observed = (
            path.stat(follow_symlinks=False)
            if parent_fd is None
            else os.stat(path, dir_fd=parent_fd, follow_symlinks=False)
        )
        identity = _owned_directory_inode_identity(metadata)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or not stat.S_ISDIR(observed.st_mode)
            or _owned_directory_inode_identity(observed) != identity
        ):
            raise PrerequisiteError(
                f"permission authority directory is unsafe: {path}"
            )
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_regular_at(
    directory_fd: int,
    name: str,
    *,
    mode: int,
    writable: bool = False,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | getattr(
        os, "O_NOFOLLOW", 0
    )
    noatime = 0 if writable else getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(name, flags | noatime, dir_fd=directory_fd)
    except OSError as exc:
        if noatime and exc.errno in {errno.EPERM, errno.EINVAL, errno.ENOTSUP}:
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except OSError as fallback:
                raise PrerequisiteError(
                    f"required private file is unsafe: {name}"
                ) from fallback
        else:
            raise PrerequisiteError(f"required private file is unsafe: {name}") from exc
    try:
        metadata = os.fstat(descriptor)
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_nlink not in allowed_nlinks
            or _stat_identity(observed) != _stat_identity(metadata)
        ):
            raise PrerequisiteError(f"required private file is unsafe: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _fd_json(
    descriptor: int,
    *,
    label: str,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> dict[str, object]:
    try:
        document, _digest_value, _metadata = _stable_private_json_descriptor(
            descriptor,
            label=label,
            maximum_bytes=maximum_bytes,
        )
    except OSError as exc:
        raise PrerequisiteError(f"{label} is invalid") from exc
    return document


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.absolute()
    right = right.absolute()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _private_source_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one pre-Git source-policy file without following a link."""

    try:
        descriptor, _noatime = _open_readonly_noatime(path)
    except OSError as exc:
        raise PrerequisiteError(
            f"prerequisite source policy is unavailable: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        payload = _descriptor_bytes(descriptor, maximum_bytes=maximum_bytes)
        after = os.fstat(descriptor)
        observed = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o077
            or before.st_nlink != 1
            or _stat_identity(before) != _stat_identity(after)
            or _stat_identity(before) != _stat_identity(observed)
        ):
            raise PrerequisiteError(
                f"prerequisite source policy is unsafe: {path}"
            )
        return payload
    finally:
        os.close(descriptor)


def _validate_pre_git_config(payload: bytes, *, label: str) -> None:
    """Reject local Git policy that can execute or redirect a Git operation."""

    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(payload.decode("utf-8"))
    except (UnicodeError, configparser.Error) as exc:
        raise PrerequisiteError(f"{label} is malformed") from exc
    allowed: dict[str, set[str]] = {
        "core": {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "ignorecase",
            "precomposeunicode",
        },
        'remote "origin"': {"url", "fetch", "tagopt"},
        'branch "main"': {"remote", "merge", "vscode-merge-base"},
        "user": {"name", "email"},
    }
    for section in parser.sections():
        permitted = allowed.get(section.lower())
        if permitted is None:
            raise PrerequisiteError(
                f"{label} contains executable or unsupported Git policy"
            )
        keys = {key.lower() for key, _value in parser.items(section, raw=True)}
        if not keys.issubset(permitted):
            raise PrerequisiteError(
                f"{label} contains executable or unsupported Git policy"
            )


def _validate_pre_git_attributes(payload: bytes, *, label: str) -> None:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise PrerequisiteError(f"{label} is malformed") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        attributes = {
            token.strip("\"'").lstrip("-!").split("=", 1)[0].lower()
            for token in tokens[1:]
        }
        if attributes & {"filter", "diff", "merge", "textconv"}:
            raise PrerequisiteError(
                f"{label} contains an executable Git attribute"
            )


def _walk_private_source_tree(
    directory_fd: int,
    relative: Path,
    *,
    regular_paths: set[str],
    directory_paths: set[str],
) -> None:
    """Verify a private standalone tree through pinned directory descriptors."""

    before = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o700
    ):
        raise PrerequisiteError("prerequisite source directory is unsafe")
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise PrerequisiteError("cannot inventory prerequisite source") from exc
    for name in names:
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise PrerequisiteError("prerequisite source entry name is unsafe")
        child_relative = relative / name
        child_name = child_relative.as_posix()
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise PrerequisiteError(
                f"prerequisite source entry is unavailable: {child_name}"
            ) from exc
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_private_directory(Path(name), parent_fd=directory_fd)
            try:
                if _directory_identity(os.fstat(child_fd)) != _directory_identity(
                    metadata
                ):
                    raise PrerequisiteError(
                        f"prerequisite source directory changed: {child_name}"
                    )
                directory_paths.add(child_name)
                _walk_private_source_tree(
                    child_fd,
                    child_relative,
                    regular_paths=regular_paths,
                    directory_paths=directory_paths,
                )
                observed = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if _directory_identity(observed) != _directory_identity(metadata):
                    raise PrerequisiteError(
                        f"prerequisite source directory changed: {child_name}"
                    )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PrerequisiteError(
                f"prerequisite source entry is not regular: {child_name}"
            )
        if (
            metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise PrerequisiteError(
                f"prerequisite source file is unsafe: {child_name}"
            )
        descriptor, _noatime = _open_readonly_noatime(
            Path(name), directory_fd=directory_fd
        )
        try:
            opened = os.fstat(descriptor)
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                _stat_identity(opened) != _stat_identity(metadata)
                or _stat_identity(observed) != _stat_identity(metadata)
            ):
                raise PrerequisiteError(
                    f"prerequisite source file changed: {child_name}"
                )
        finally:
            os.close(descriptor)
        regular_paths.add(child_name)
    after = os.fstat(directory_fd)
    if _directory_identity(after) != _directory_identity(before):
        raise PrerequisiteError("prerequisite source directory changed")


def _assert_pre_git_source_safety(source_root: Path) -> None:
    """Prove source policy/storage safety before *every* Git subprocess.

    Git must never diagnose a dirty checkout before local config and
    attributes have been rejected: ``git status`` may invoke a clean/process
    filter while discovering that very dirtiness.
    """

    source_root = source_root.absolute()
    if any(
        _paths_overlap(source_root, protected)
        for protected in (PRODUCTION_ROOT, RUNTIME_ROOT)
    ):
        raise PrerequisiteError(
            "prerequisite source must be independent of production/runtime"
        )
    try:
        parent = source_root.parent.lstat()
    except OSError as exc:
        raise PrerequisiteError("prerequisite source parent is unavailable") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or source_root.parent.is_symlink()
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
    ):
        raise PrerequisiteError("prerequisite source parent is not private")
    root_fd = _open_private_directory(source_root)
    try:
        regular_paths: set[str] = set()
        directory_paths: set[str] = set()
        _walk_private_source_tree(
            root_fd,
            Path(),
            regular_paths=regular_paths,
            directory_paths=directory_paths,
        )
    finally:
        os.close(root_fd)
    required_directories = {".git", ".git/objects", ".git/refs"}
    required_files = {".git/config", ".git/HEAD", ".git/index"}
    if not required_directories.issubset(directory_paths) or not required_files.issubset(
        regular_paths
    ):
        raise PrerequisiteError("prerequisite source Git layout is incomplete")
    forbidden = {
        ".git/commondir",
        ".git/info/grafts",
        ".git/objects/info/alternates",
        ".git/objects/info/http-alternates",
        ".git/shallow",
    }
    if forbidden & (regular_paths | directory_paths):
        raise PrerequisiteError("prerequisite source uses external Git storage")
    if any(
        path.startswith(".git/") and path.endswith(".lock")
        for path in regular_paths | directory_paths
    ):
        raise PrerequisiteError("prerequisite source has an active Git lock")
    head = _private_source_bytes(source_root / ".git/HEAD", maximum_bytes=4096)
    if head.decode("ascii", errors="replace").strip() != "ref: refs/heads/main":
        raise PrerequisiteError("prerequisite source HEAD is not local main")
    config_paths = [".git/config"]
    if ".git/config.worktree" in regular_paths:
        config_paths.append(".git/config.worktree")
    for relative in config_paths:
        _validate_pre_git_config(
            _private_source_bytes(source_root / relative, maximum_bytes=1024 * 1024),
            label=relative,
        )
    attribute_paths = sorted(
        path
        for path in regular_paths
        if path == ".git/info/attributes"
        or path == ".gitattributes"
        or path.endswith("/.gitattributes") and not path.startswith(".git/")
    )
    for relative in attribute_paths:
        _validate_pre_git_attributes(
            _private_source_bytes(source_root / relative, maximum_bytes=1024 * 1024),
            label=relative,
        )


def _run_git(source_root: Path, *arguments: str) -> bytes:
    _assert_pre_git_source_safety(source_root)
    environment = {
        "HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_COUNT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "/bin/false",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_DIR": str(source_root / ".git"),
        "GIT_WORK_TREE": str(source_root),
        "GIT_LITERAL_PATHSPECS": "1",
    }
    try:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "--no-optional-locks",
                "-c",
                "credential.helper=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.file.allow=never",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                f"core.worktree={source_root}",
                *arguments,
            ],
            cwd=source_root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrerequisiteError("cannot verify prerequisite source checkout") from exc
    return result.stdout


def _validate_source_checkout(source_root: Path, source_sha: str) -> str:
    _private_metadata(source_root, mode=0o700, regular=False)
    _private_metadata(source_root / ".git", mode=0o700, regular=False)
    if _run_git(source_root, "rev-parse", "--verify", "HEAD").decode().strip() != source_sha:
        raise PrerequisiteError("source checkout HEAD differs from requested SHA")
    source_tree = _run_git(
        source_root, "rev-parse", "--verify", f"{source_sha}^{{tree}}"
    ).decode().strip()
    if SHA_RE.fullmatch(source_tree) is None:
        raise PrerequisiteError("source tree identity is invalid")
    if _run_git(source_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PrerequisiteError("source checkout is not clean")
    origin = _run_git(source_root, "remote", "get-url", "origin").decode().strip()
    remotes = _run_git(source_root, "remote").decode().splitlines()
    fetch_urls = _run_git(
        source_root, "remote", "get-url", "--all", "origin"
    ).decode().splitlines()
    push_urls = _run_git(
        source_root, "remote", "get-url", "--push", "--all", "origin"
    ).decode().splitlines()
    if (
        origin != REPOSITORY_SSH_URL
        or remotes != ["origin"]
        or fetch_urls != [REPOSITORY_SSH_URL]
        or push_urls != [REPOSITORY_SSH_URL]
    ):
        raise PrerequisiteError("source checkout origin is not the private SSH authority")
    if (
        _run_git(source_root, "rev-parse", "--is-shallow-repository")
        .decode()
        .strip()
        != "false"
        or _run_git(
            source_root,
            "for-each-ref",
            "--format=%(refname)",
            "refs/replace/",
        )
    ):
        raise PrerequisiteError("source checkout is shallow or uses replacement refs")
    for marker in (
        ".git/commondir",
        ".git/info/grafts",
        ".git/objects/info/alternates",
        ".git/objects/info/http-alternates",
    ):
        if (source_root / marker).exists() or (source_root / marker).is_symlink():
            raise PrerequisiteError("source checkout uses external Git object storage")
    return source_tree


def _git_blob(source_root: Path, source_sha: str, relative: str) -> bytes:
    payload = _run_git(source_root, "show", f"{source_sha}:{relative}")
    worktree = source_root / relative
    try:
        descriptor, _noatime = _open_readonly_noatime(worktree)
    except OSError as exc:
        raise PrerequisiteError(
            f"tracked prerequisite is unavailable: {relative}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        observed = worktree.stat(follow_symlinks=False)
        content = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or _stat_identity(metadata) != _stat_identity(after)
            or _stat_identity(metadata) != _stat_identity(observed)
            or bytes(content) != payload
        ):
            raise PrerequisiteError(f"tracked prerequisite differs from Git: {relative}")
    finally:
        os.close(descriptor)
    return payload


def _git_object_blob(
    source_root: Path,
    source_sha: str,
    relative: str,
    *,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> bytes:
    """Read one historical Git blob without consulting the live worktree.

    The source-successor transaction needs the predecessor verifier bytes
    after the worktree has advanced to the exact target.  The surrounding
    checkout trust gate rejects redirects, alternates, replacements and
    executable attributes before this fixed `/usr/bin/git` object read.
    """

    source_sha = _require_sha(source_sha)
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or ".." in Path(relative).parts
        or "\x00" in relative
        or "\n" in relative
    ):
        raise PrerequisiteError("historical verifier path is invalid")
    object_type = _run_git(
        source_root,
        "cat-file",
        "-t",
        f"{source_sha}:{relative}",
    ).decode().strip()
    if object_type != "blob":
        raise PrerequisiteError(
            f"historical verifier is not a Git blob: {relative}"
        )
    payload = _run_git(source_root, "show", f"{source_sha}:{relative}")
    if not payload or len(payload) > maximum_bytes:
        raise PrerequisiteError(
            f"historical verifier has an invalid size: {relative}"
        )
    return payload


def _bootstrap_contract(source_root: Path, source_sha: str) -> Any:
    """Load only the exact reviewed bootstrap source-authority implementation."""

    relative = "scripts/bootstrap_pull_deploy.py"
    payload = _git_blob(source_root, source_sha, relative)
    module = types.ModuleType("nexpoly_adopt_prerequisite_bootstrap_contract")
    module.__file__ = str(source_root / relative)
    try:
        exec(compile(payload, f"git:{source_sha}:{relative}", "exec"), module.__dict__)
    except Exception as exc:
        raise PrerequisiteError(
            "cannot load exact bootstrap source-authority contract"
        ) from exc
    if not callable(getattr(module, "bootstrap_source_readiness", None)) or not callable(
        getattr(module, "_delivery_gate", None)
    ):
        raise PrerequisiteError("bootstrap source-authority contract is incomplete")
    return module


def _validate_source_readiness(
    document: object,
    *,
    source_root: Path,
    source_sha: str,
    source_tree: str,
) -> dict[str, object]:
    if (
        not isinstance(document, dict)
        or set(document) != SOURCE_READINESS_FIELDS
        or not _has_exact_schema_version(document, 2)
        or document.get("ready") is not True
        or document.get("source_root") != str(source_root.absolute())
        or document.get("source_sha") != source_sha
        or document.get("source_tree") != source_tree
        or document.get("branch") != "main"
        or document.get("origin") != REPOSITORY_SSH_URL
        or document.get("remote_names") != ["origin"]
        or document.get("origin_fetch_urls") != [REPOSITORY_SSH_URL]
        or document.get("origin_push_urls") != [REPOSITORY_SSH_URL]
        or document.get("origin_main_sha") != source_sha
        or document.get("standalone_object_database") is not True
        or document.get("shallow") is not False
        or document.get("dirty_entries") != 0
        or document.get("ignored_entries") != 0
        or document.get("unreachable_objects") != 0
        or document.get("replace_refs") != 0
        or document.get("special_index_entries") != 0
        or document.get("sparse_index") is not False
        or document.get("owner_private") is not True
        or document.get("group_or_world_writable") is not False
    ):
        raise PrerequisiteError("final prerequisite source readiness is invalid")
    return dict(document)


def _validate_delivery_gate(document: object, *, source_sha: str) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {"remote_main", "ci"}:
        raise PrerequisiteError("final prerequisite delivery gate is invalid")
    ci = document.get("ci")
    if (
        document.get("remote_main") != source_sha
        or not isinstance(ci, dict)
        or set(ci)
        != {
            "workflow_run_id",
            "run_attempt",
            "head_sha",
            "head_branch",
            "event",
            "path",
            "conclusion",
            "required_jobs",
        }
        or not isinstance(ci.get("workflow_run_id"), int)
        or isinstance(ci.get("workflow_run_id"), bool)
        or ci["workflow_run_id"] <= 0
        or not isinstance(ci.get("run_attempt"), int)
        or isinstance(ci.get("run_attempt"), bool)
        or ci["run_attempt"] <= 0
        or ci.get("head_sha") != source_sha
        or ci.get("head_branch") != "main"
        or ci.get("event") != "push"
        or ci.get("path") != ".github/workflows/ci.yml"
        or ci.get("conclusion") != "success"
        or not isinstance(ci.get("required_jobs"), list)
        or not ci["required_jobs"]
        or len(ci["required_jobs"]) > 32
        or any(not isinstance(name, str) or not name for name in ci["required_jobs"])
        or len(ci["required_jobs"]) != len(set(ci["required_jobs"]))
    ):
        raise PrerequisiteError("final prerequisite delivery gate is invalid")
    return dict(document)


def _validate_adopted_deployment(
    runtime_root: Path,
    *,
    state_fd: int | None = None,
) -> str:
    path = runtime_root / ADOPTED_DEPLOYMENT_PATH
    try:
        if state_fd is None:
            descriptor, _noatime = _open_readonly_noatime(path)
        else:
            descriptor = _open_private_regular_at(
                state_fd,
                ADOPTED_DEPLOYMENT_PATH.name,
                mode=0o600,
            )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise PrerequisiteError("adopted deployment authority is unsafe")
        document = _fd_json(descriptor, label="adopted deployment authority")
        adopted_digest = _descriptor_digest(descriptor)
        observed = (
            path.stat(follow_symlinks=False)
            if state_fd is None
            else os.stat(
                ADOPTED_DEPLOYMENT_PATH.name,
                dir_fd=state_fd,
                follow_symlinks=False,
            )
        )
        if _stat_identity(observed) != _stat_identity(metadata):
            raise PrerequisiteError("adopted deployment authority changed")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrerequisiteError("adopted deployment authority is invalid") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if (
        not isinstance(document, dict)
        or not _has_exact_schema_version(document, 1)
        or document.get("status") != "adopted"
    ):
        raise PrerequisiteError("adopted deployment authority is incomplete")
    bootstrap_path = runtime_root / BOOTSTRAP_CONTROL_PATH
    bootstrap = (
        _load_json(bootstrap_path)
        if state_fd is None
        else _load_json_at(state_fd, BOOTSTRAP_CONTROL_PATH.name)
    )
    if (
        not _has_exact_schema_version(bootstrap, 3)
        or not _has_exact_schema_version(
            bootstrap.get("adopted_deployment"), 1
        )
        or bootstrap.get("status") != "completed"
        or bootstrap.get("authority_kind") != ADOPTION_AUTHORITY_KIND
        or bootstrap.get("adopted_deployment") != document
        or bootstrap.get("adopted_deployment_sha256")
        != _canonical_digest(document)
    ):
        raise PrerequisiteError(
            "adopted deployment differs from bootstrap-control v3 authority"
        )
    return adopted_digest


def _validate_runtime(
    runtime_root: Path,
    *,
    runtime_fd: int | None = None,
    state_fd: int | None = None,
    config_fd: int | None = None,
    lock_fd: int | None = None,
) -> tuple[Path, str]:
    lock_path = runtime_root / "state/deploy.lock"
    pgpass = runtime_root / "config" / PGPASS_NAME
    descriptors = (runtime_fd, state_fd, config_fd, lock_fd)
    if all(descriptor is None for descriptor in descriptors):
        _private_metadata(runtime_root, mode=0o700, regular=False)
        for relative in ("config", "state", "audit/adoption"):
            _private_metadata(runtime_root / relative, mode=0o700, regular=False)
        _private_metadata(lock_path, mode=0o600, regular=True)
        metadata = _private_metadata(pgpass, mode=0o600, regular=True)
        adopted_digest = _validate_adopted_deployment(runtime_root)
    elif not all(isinstance(descriptor, int) for descriptor in descriptors):
        raise PrerequisiteError("pinned prerequisite runtime is incomplete")
    else:
        assert runtime_fd is not None
        assert state_fd is not None
        assert config_fd is not None
        assert lock_fd is not None
        audit_fd = _open_private_directory(Path("audit"), parent_fd=runtime_fd)
        try:
            adoption_fd = _open_private_directory(
                Path("adoption"), parent_fd=audit_fd
            )
            os.close(adoption_fd)
        finally:
            os.close(audit_fd)
        lock_metadata = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            or lock_metadata.st_nlink != 1
        ):
            raise PrerequisiteError("required private deploy lock is unsafe")
        pgpass_fd = _open_private_regular_at(
            config_fd,
            PGPASS_NAME,
            mode=0o600,
        )
        try:
            metadata = os.fstat(pgpass_fd)
        finally:
            os.close(pgpass_fd)
        adopted_digest = _validate_adopted_deployment(
            runtime_root,
            state_fd=state_fd,
        )
    if metadata.st_size < 1 or metadata.st_size > 64 * 1024:
        raise PrerequisiteError("mutable-data pgpass size is invalid")
    return lock_path, adopted_digest


def _validate_exact_file(path: Path, *, digest: str, mode: int) -> None:
    descriptor, _identity_value = _open_exact(
        path,
        digest=digest,
        mode=mode,
    )
    os.close(descriptor)


def _validate_exact_file_at(
    directory_fd: int,
    name: str,
    *,
    digest: str,
    mode: int,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> None:
    descriptor, _identity_value = _open_exact_at(
        directory_fd,
        name,
        digest=digest,
        mode=mode,
        allowed_nlinks=allowed_nlinks,
    )
    os.close(descriptor)


def _identity(metadata: os.stat_result) -> dict[str, int]:
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": metadata.st_size,
    }


def _open_exact(
    path: Path,
    *,
    digest: str,
    mode: int,
    expected_identity: dict[str, int] | None = None,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> tuple[int, dict[str, int]]:
    try:
        descriptor, _noatime = _open_readonly_noatime(path)
    except OSError as exc:
        raise PrerequisiteError(f"operation-owned file is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        observed = _identity(metadata)
        path_metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or observed["mode"] != mode
            or metadata.st_nlink not in allowed_nlinks
            or expected_identity is not None
            and observed != expected_identity
            or _descriptor_digest(descriptor) != digest
            or _stat_identity(path_metadata) != _stat_identity(metadata)
        ):
            raise PrerequisiteError(f"operation-owned file identity differs: {path}")
        return descriptor, observed
    except BaseException:
        os.close(descriptor)
        raise


def _open_exact_at(
    directory_fd: int,
    name: str,
    *,
    digest: str,
    mode: int,
    expected_identity: dict[str, int] | None = None,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> tuple[int, dict[str, int]]:
    descriptor = _open_private_regular_at(
        directory_fd,
        name,
        mode=mode,
        allowed_nlinks=allowed_nlinks,
    )
    try:
        metadata = os.fstat(descriptor)
        observed = _identity(metadata)
        if (
            expected_identity is not None
            and observed != expected_identity
            or _descriptor_digest(descriptor) != digest
        ):
            raise PrerequisiteError(
                f"operation-owned file identity differs: {name}"
            )
        return descriptor, observed
    except BaseException:
        os.close(descriptor)
        raise


def _rename_noreplace(
    source_directory: int,
    source_name: str,
    target_directory: int,
    target_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PrerequisiteError("renameat2 no-replace quarantine is unavailable")
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
            source_directory,
            os.fsencode(source_name),
            target_directory,
            os.fsencode(target_name),
            RENAME_NOREPLACE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source_name)


def _rename_exchange(
    first_directory: int,
    first_name: str,
    second_directory: int,
    second_name: str,
) -> None:
    """Atomically exchange two existing names without a replacement window."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PrerequisiteError("renameat2 exchange is unavailable")
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
            first_directory,
            os.fsencode(first_name),
            second_directory,
            os.fsencode(second_name),
            RENAME_EXCHANGE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), first_name)


def _quarantine_owned_link_at(
    directory: int,
    name: str,
    *,
    digest: str,
    mode: int,
    expected_identity: dict[str, int],
    quarantine_name: str,
) -> None:
    """Idempotently quarantine and unlink one exact operation-owned inode."""
    _quarantine_private_link_at(
        directory,
        name,
        mode=mode,
        quarantine_name=quarantine_name,
        allowed_nlinks=frozenset({1, 2}),
        digest=digest,
        expected_identity=expected_identity,
    )


def _quarantine_owned_link(
    path: Path,
    *,
    digest: str,
    mode: int,
    expected_identity: dict[str, int],
    quarantine_name: str,
) -> None:
    directory = _open_private_directory(path.parent)
    try:
        _quarantine_owned_link_at(
            directory,
            path.name,
            digest=digest,
            mode=mode,
            expected_identity=expected_identity,
            quarantine_name=quarantine_name,
        )
    finally:
        os.close(directory)


def _load_json(
    path: Path,
    *,
    require_single_link: bool = True,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> dict[str, object]:
    try:
        descriptor, _noatime = _open_readonly_noatime(path)
    except OSError as exc:
        raise PrerequisiteError(f"private JSON is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        observed = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or require_single_link
            and metadata.st_nlink != 1
            or not require_single_link
            and metadata.st_nlink not in {1, 2}
            or _stat_identity(observed) != _stat_identity(metadata)
        ):
            raise PrerequisiteError(f"private JSON is unsafe: {path}")
        return _fd_json(
            descriptor,
            label=f"private JSON {path}",
            maximum_bytes=maximum_bytes,
        )
    finally:
        os.close(descriptor)


def _load_json_at(
    directory_fd: int,
    name: str,
    *,
    require_single_link: bool = True,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> dict[str, object]:
    descriptor = _open_private_regular_at(
        directory_fd,
        name,
        mode=0o600,
        allowed_nlinks=(
            frozenset({1}) if require_single_link else frozenset({1, 2})
        ),
    )
    try:
        return _fd_json(
            descriptor,
            label=f"private JSON {name}",
            maximum_bytes=maximum_bytes,
        )
    finally:
        os.close(descriptor)


def _load_json_with_digest(
    path: Path,
    *,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> tuple[dict[str, object], str]:
    """Read one path-stable private JSON document and its exact raw digest."""

    try:
        descriptor, _noatime = _open_readonly_noatime(path)
    except OSError as exc:
        raise PrerequisiteError(f"private JSON is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise PrerequisiteError(f"private JSON is unsafe: {path}")
        document, digest, stable = _stable_private_json_descriptor(
            descriptor,
            label=f"private JSON {path}",
            maximum_bytes=maximum_bytes,
        )
        observed = path.stat(follow_symlinks=False)
        if _stable_regular_identity(observed) != _stable_regular_identity(
            stable
        ):
            raise PrerequisiteError(f"private JSON changed while reading: {path}")
        return document, digest
    finally:
        os.close(descriptor)


def _load_json_with_digest_at(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> tuple[dict[str, object], str]:
    """Read one dirfd-bound private JSON document and hash the same bytes."""

    descriptor = _open_private_regular_at(
        directory_fd,
        name,
        mode=0o600,
    )
    try:
        document, digest, stable = _stable_private_json_descriptor(
            descriptor,
            label=f"private JSON {name}",
            maximum_bytes=maximum_bytes,
        )
        observed = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if _stable_regular_identity(observed) != _stable_regular_identity(
            stable
        ):
            raise PrerequisiteError(f"private JSON changed while reading: {name}")
        return document, digest
    finally:
        os.close(descriptor)


def _entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _quarantine_private_link_at(
    directory_fd: int,
    name: str,
    *,
    mode: int,
    quarantine_name: str,
    allowed_nlinks: frozenset[int],
    digest: str | None = None,
    expected_identity: dict[str, int] | None = None,
) -> None:
    """Idempotently unlink one private name through an inode-checked rename.

    The quarantine name is deterministic and operation-scoped.  A replay can
    therefore finish a SIGKILL that happened after rename but before unlink,
    while a substituted inode is restored instead of being deleted.
    """

    source_exists = _entry_exists_at(directory_fd, name)
    quarantine_exists = _entry_exists_at(directory_fd, quarantine_name)
    if source_exists and quarantine_exists:
        raise PrerequisiteError(
            f"operation-owned source and quarantine both exist: {name}"
        )
    current_name = quarantine_name if quarantine_exists else name
    if not source_exists and not quarantine_exists:
        # Absence can be the visible result of an unlink whose parent-fsync
        # response was lost. Seal that namespace before a later journal can
        # treat cleanup as complete.
        os.fsync(directory_fd)
        return
    descriptor = _open_private_regular_at(
        directory_fd,
        current_name,
        mode=mode,
        allowed_nlinks=allowed_nlinks,
    )
    try:
        metadata = os.fstat(descriptor)
        opened_identity = _identity(metadata)
        if (
            expected_identity is not None
            and opened_identity != expected_identity
            or digest is not None
            and _descriptor_digest(descriptor) != digest
        ):
            raise PrerequisiteError(
                f"operation-owned file identity differs: {current_name}"
            )
        if source_exists:
            try:
                _rename_noreplace(
                    directory_fd,
                    name,
                    directory_fd,
                    quarantine_name,
                )
            except OSError as exc:
                raise PrerequisiteError(
                    f"cannot quarantine operation-owned file: {name}"
                ) from exc
            observed = os.stat(
                quarantine_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if _stat_identity(observed) != _stat_identity(os.fstat(descriptor)):
                with contextlib.suppress(OSError):
                    _rename_noreplace(
                        directory_fd,
                        quarantine_name,
                        directory_fd,
                        name,
                    )
                raise PrerequisiteError(
                    f"operation-owned file raced during quarantine: {name}"
                )
        os.unlink(quarantine_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)


def _remove_deterministic_temporary_at(
    directory_fd: int,
    name: str,
    *,
    mode: int,
    quarantine_name: str | None = None,
) -> None:
    _quarantine_private_link_at(
        directory_fd,
        name,
        mode=mode,
        quarantine_name=quarantine_name or f"{name}.quarantine",
        allowed_nlinks=frozenset({1}),
    )


def _atomic_owned_json_at(
    directory_fd: int,
    name: str,
    document: object,
) -> None:
    temporary_name = f".{name}.tmp"
    _remove_deterministic_temporary_at(
        directory_fd,
        temporary_name,
        mode=0o600,
        quarantine_name=f"{temporary_name}.quarantine",
    )
    payload = _canonical_bytes(document) + b"\n"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(
        temporary_name,
        name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    os.fsync(directory_fd)


def _atomic_owned_json(path: Path, document: object) -> None:
    path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    _private_metadata(path.parent, mode=0o700, regular=False)
    directory_fd = _open_private_directory(path.parent)
    try:
        _atomic_owned_json_at(directory_fd, path.name, document)
    finally:
        os.close(directory_fd)


def _stable_owned_payload_at(
    directory_fd: int,
    name: str,
    descriptor: int,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[bytes, os.stat_result]:
    """Read one held private inode and prove its path/version stayed exact."""

    before = os.fstat(descriptor)
    payload = _descriptor_bytes(descriptor, maximum_bytes=maximum_bytes)
    after = os.fstat(descriptor)
    try:
        observed = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise PrerequisiteError(f"{label} path changed") from exc
    if (
        _stable_regular_identity(before) != _stable_regular_identity(after)
        or _stable_regular_identity(observed)
        != _stable_regular_identity(after)
    ):
        raise PrerequisiteError(f"{label} changed while reading")
    return payload, after


def _reseal_exact_owned_payload_at(
    directory_fd: int,
    name: str,
    descriptor: int,
    *,
    expected_payload: bytes,
    maximum_bytes: int,
    label: str,
    mismatch_message: str,
) -> os.stat_result:
    """Re-fsync and revalidate one visible exact inode before publication."""

    observed_payload, before = _stable_owned_payload_at(
        directory_fd,
        name,
        descriptor,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    if observed_payload != expected_payload:
        raise PrerequisiteError(mismatch_message)
    # The visible file may be the result of a write or file-fsync response that
    # was lost. Re-seal that exact held inode before its pathname can authorize
    # a link publication or a completed transaction.
    os.fsync(descriptor)
    sealed = os.fstat(descriptor)
    try:
        observed = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise PrerequisiteError(f"{label} changed before fsync") from exc
    if (
        _stable_regular_identity(sealed)
        != _stable_regular_identity(before)
        or _stable_regular_identity(observed)
        != _stable_regular_identity(sealed)
    ):
        raise PrerequisiteError(f"{label} changed before fsync")
    verified_payload, verified = _stable_owned_payload_at(
        directory_fd,
        name,
        descriptor,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    if (
        verified_payload != expected_payload
        or _stable_regular_identity(verified)
        != _stable_regular_identity(sealed)
    ):
        raise PrerequisiteError(f"{label} changed after fsync")
    os.fsync(directory_fd)
    return verified


def _create_owned_json_once_at(
    directory_fd: int,
    name: str,
    document: object,
    *,
    operation_id: str,
    checkpoint: Callable[[str], None],
    maximum_bytes: int = 8 * 1024 * 1024,
) -> None:
    """Crash-safely publish one authority without replacing an existing inode."""

    payload = _canonical_bytes(document) + b"\n"
    if len(payload) > maximum_bytes:
        raise PrerequisiteError("prerequisite authority is oversized")
    temporary_name = f".{name}.create-{operation_id}"
    quarantine_name = f"{temporary_name}.quarantine"
    authority_exists = _entry_exists_at(directory_fd, name)
    temporary_exists = _entry_exists_at(directory_fd, temporary_name)
    quarantine_exists = _entry_exists_at(directory_fd, quarantine_name)
    if temporary_exists and quarantine_exists:
        raise PrerequisiteError(
            "prerequisite authority staging and quarantine both exist"
        )
    if authority_exists:
        authority_fd = _open_private_regular_at(
            directory_fd,
            name,
            mode=0o600,
            allowed_nlinks=frozenset({1, 2}),
        )
        try:
            authority_metadata = _reseal_exact_owned_payload_at(
                directory_fd,
                name,
                authority_fd,
                expected_payload=payload,
                maximum_bytes=maximum_bytes,
                label="prerequisite authority",
                mismatch_message="prerequisite authority path is occupied",
            )
            if authority_metadata.st_nlink == 2:
                companion_name = (
                    temporary_name
                    if temporary_exists
                    else quarantine_name if quarantine_exists else None
                )
                if companion_name is None:
                    raise PrerequisiteError(
                        "prerequisite authority has an unowned hard link"
                    )
                temporary_fd = _open_private_regular_at(
                    directory_fd,
                    companion_name,
                    mode=0o600,
                    allowed_nlinks=frozenset({2}),
                )
                try:
                    temporary_metadata = _reseal_exact_owned_payload_at(
                        directory_fd,
                        companion_name,
                        temporary_fd,
                        expected_payload=payload,
                        maximum_bytes=maximum_bytes,
                        label="prerequisite authority staging",
                        mismatch_message=(
                            "prerequisite authority staging differs"
                        ),
                    )
                    if _stable_regular_identity(temporary_metadata) != (
                        _stable_regular_identity(authority_metadata)
                    ):
                        raise PrerequisiteError(
                            "prerequisite authority staging identity differs"
                        )
                finally:
                    os.close(temporary_fd)
                _quarantine_private_link_at(
                    directory_fd,
                    temporary_name,
                    mode=0o600,
                    quarantine_name=quarantine_name,
                    allowed_nlinks=frozenset({2}),
                    digest=_digest(payload),
                    expected_identity=_identity(authority_metadata),
                )
            elif temporary_exists or quarantine_exists:
                _remove_deterministic_temporary_at(
                    directory_fd,
                    temporary_name,
                    mode=0o600,
                    quarantine_name=quarantine_name,
                )
        finally:
            os.close(authority_fd)
    else:
        if temporary_exists:
            temporary_fd = _open_private_regular_at(
                directory_fd,
                temporary_name,
                mode=0o600,
            )
            try:
                temporary_payload, _temporary_metadata = (
                    _stable_owned_payload_at(
                        directory_fd,
                        temporary_name,
                        temporary_fd,
                        maximum_bytes=maximum_bytes,
                        label="prerequisite authority staging",
                    )
                )
            finally:
                os.close(temporary_fd)
            if temporary_payload != payload:
                _remove_deterministic_temporary_at(
                    directory_fd,
                    temporary_name,
                    mode=0o600,
                    quarantine_name=quarantine_name,
                )
                temporary_exists = False
        elif quarantine_exists:
            _remove_deterministic_temporary_at(
                directory_fd,
                temporary_name,
                mode=0o600,
                quarantine_name=quarantine_name,
            )
            quarantine_exists = False
        if not temporary_exists:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                written = 0
                while written < len(payload):
                    written += os.write(descriptor, payload[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        temporary_fd = _open_private_regular_at(
            directory_fd,
            temporary_name,
            mode=0o600,
        )
        try:
            _reseal_exact_owned_payload_at(
                directory_fd,
                temporary_name,
                temporary_fd,
                expected_payload=payload,
                maximum_bytes=maximum_bytes,
                label="prerequisite authority staging",
                mismatch_message="prerequisite authority staging differs",
            )
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            os.fsync(directory_fd)
            checkpoint("authority-linked")
            authority_fd = _open_private_regular_at(
                directory_fd,
                name,
                mode=0o600,
                allowed_nlinks=frozenset({2}),
            )
            try:
                authority_metadata = _reseal_exact_owned_payload_at(
                    directory_fd,
                    name,
                    authority_fd,
                    expected_payload=payload,
                    maximum_bytes=maximum_bytes,
                    label="published prerequisite authority",
                    mismatch_message=(
                        "published prerequisite authority differs"
                    ),
                )
                temporary_payload, temporary_metadata = (
                    _stable_owned_payload_at(
                        directory_fd,
                        temporary_name,
                        temporary_fd,
                        maximum_bytes=maximum_bytes,
                        label="published prerequisite authority staging",
                    )
                )
                if (
                    temporary_payload != payload
                    or _stable_regular_identity(authority_metadata)
                    != _stable_regular_identity(temporary_metadata)
                ):
                    raise PrerequisiteError(
                        "published prerequisite authority identity differs"
                    )
            finally:
                os.close(authority_fd)
            os.unlink(temporary_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(temporary_fd)
    final_fd = _open_private_regular_at(
        directory_fd,
        name,
        mode=0o600,
    )
    try:
        _reseal_exact_owned_payload_at(
            directory_fd,
            name,
            final_fd,
            expected_payload=payload,
            maximum_bytes=maximum_bytes,
            label="published prerequisite authority",
            mismatch_message="published prerequisite authority differs",
        )
    finally:
        os.close(final_fd)
    # Re-observing the exact final authority is also a recovery action: the
    # prior unlink of its temporary hard link may be visible even though the
    # state-directory fsync response was lost.  Seal that namespace before a
    # caller persists its completed transaction.
    os.fsync(directory_fd)


class PrerequisiteInstaller:
    def __init__(
        self,
        source_root: Path,
        runtime_root: Path,
        *,
        checkpoint: Callable[[str], None] | None = None,
        source_readiness_probe: Callable[[Path, str], dict[str, object]] | None = None,
        delivery_gate_probe: Callable[
            [Path, Path, str, dict[str, object] | None], dict[str, object]
        ]
        | None = None,
    ) -> None:
        self.source_root = source_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.config_root = self.runtime_root / "config"
        self.transaction_root = self.runtime_root / TRANSACTION_DIRECTORY
        self.authority_path = self.runtime_root / AUTHORITY_PATH
        self.checkpoint = checkpoint or (lambda _phase: None)
        self.source_readiness_probe = source_readiness_probe
        self.delivery_gate_probe = delivery_gate_probe
        self._pinned_directories: dict[str, tuple[int, tuple[int, int, int, int]]] = {}
        self._pinned_lock: tuple[int, tuple[int, int, int, int, int]] | None = None
        self._transaction_directory_fd: int | None = None

    def _transaction_path(self, operation_id: str) -> Path:
        return self.transaction_root / f"{_require_operation_id(operation_id)}.json"

    @contextlib.contextmanager
    def _deployment_lock(self) -> Any:
        if self._pinned_directories or self._pinned_lock is not None:
            raise PrerequisiteError("prerequisite runtime is already pinned")
        runtime_fd = _open_private_directory(self.runtime_root)
        opened: list[int] = [runtime_fd]
        try:
            state_fd = _open_private_directory(
                Path("state"), parent_fd=runtime_fd
            )
            config_fd = _open_private_directory(
                Path("config"), parent_fd=runtime_fd
            )
            opened.extend((state_fd, config_fd))
            self._pinned_directories = {
                "runtime": (runtime_fd, _directory_identity(os.fstat(runtime_fd))),
                "state": (state_fd, _directory_identity(os.fstat(state_fd))),
                "config": (config_fd, _directory_identity(os.fstat(config_fd))),
            }
            try:
                transaction_fd = _open_private_directory(
                    Path(TRANSACTION_DIRECTORY.name), parent_fd=state_fd
                )
            except PrerequisiteError:
                try:
                    os.stat(
                        TRANSACTION_DIRECTORY.name,
                        dir_fd=state_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    transaction_fd = -1
                else:
                    raise
            if transaction_fd >= 0:
                opened.append(transaction_fd)
                self._transaction_directory_fd = transaction_fd
            lock_fd = _open_private_regular_at(
                state_fd,
                "deploy.lock",
                mode=0o600,
                writable=True,
            )
            opened.append(lock_fd)
            lock_identity = _stat_identity(os.fstat(lock_fd))
            self._pinned_lock = (lock_fd, lock_identity)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._assert_pinned_runtime()
            _validate_runtime(
                self.runtime_root,
                runtime_fd=runtime_fd,
                state_fd=state_fd,
                config_fd=config_fd,
                lock_fd=lock_fd,
            )
            yield
        finally:
            if (
                self._transaction_directory_fd is not None
                and self._transaction_directory_fd not in opened
            ):
                opened.append(self._transaction_directory_fd)
            self._pinned_lock = None
            self._transaction_directory_fd = None
            self._pinned_directories = {}
            for descriptor in reversed(opened):
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _assert_pinned_runtime(self) -> None:
        if set(self._pinned_directories) != {"runtime", "state", "config"}:
            raise PrerequisiteError("prerequisite runtime directories are not pinned")
        for name, path in (
            ("runtime", self.runtime_root),
            ("state", self.runtime_root / "state"),
            ("config", self.config_root),
        ):
            descriptor, expected = self._pinned_directories[name]
            if (
                _directory_identity(os.fstat(descriptor)) != expected
                or _directory_identity(path.stat(follow_symlinks=False)) != expected
            ):
                raise PrerequisiteError(
                    f"pinned prerequisite {name} directory changed"
                )
        if self._pinned_lock is None:
            raise PrerequisiteError("prerequisite deploy lock is not pinned")
        lock_fd, expected_lock = self._pinned_lock
        state_fd = self._pinned_directories["state"][0]
        observed = os.stat("deploy.lock", dir_fd=state_fd, follow_symlinks=False)
        if (
            _stat_identity(os.fstat(lock_fd)) != expected_lock
            or _stat_identity(observed) != expected_lock
            or _stat_identity(
                (self.runtime_root / "state/deploy.lock").stat(
                    follow_symlinks=False
                )
            )
            != expected_lock
        ):
            raise PrerequisiteError("pinned prerequisite deploy lock changed")

    def _ensure_transaction_directory_fd(self) -> int:
        if self._transaction_directory_fd is not None:
            if "state" not in self._pinned_directories:
                raise PrerequisiteError(
                    "prerequisite state directory is not pinned"
                )
            os.fsync(self._pinned_directories["state"][0])
            return self._transaction_directory_fd
        if "state" not in self._pinned_directories:
            raise PrerequisiteError("prerequisite state directory is not pinned")
        state_fd = self._pinned_directories["state"][0]
        try:
            os.mkdir(TRANSACTION_DIRECTORY.name, mode=0o700, dir_fd=state_fd)
        except FileExistsError:
            pass
        transaction_fd = _open_private_directory(
            Path(TRANSACTION_DIRECTORY.name), parent_fd=state_fd
        )
        try:
            os.fsync(state_fd)
        except Exception:
            os.close(transaction_fd)
            raise
        self._transaction_directory_fd = transaction_fd
        return transaction_fd

    def _pinned_directory_fd(self, path: Path) -> int | None:
        if path == self.config_root and "config" in self._pinned_directories:
            return self._pinned_directories["config"][0]
        if path == self.runtime_root / "state" and "state" in self._pinned_directories:
            return self._pinned_directories["state"][0]
        if path == self.transaction_root:
            return self._transaction_directory_fd
        return None

    def _runtime_adopted_digest(self) -> str:
        if not self._pinned_directories:
            return _validate_runtime(self.runtime_root)[1]
        self._assert_pinned_runtime()
        if self._pinned_lock is None:  # pragma: no cover - asserted above
            raise PrerequisiteError("prerequisite deploy lock is not pinned")
        return _validate_runtime(
            self.runtime_root,
            runtime_fd=self._pinned_directories["runtime"][0],
            state_fd=self._pinned_directories["state"][0],
            config_fd=self._pinned_directories["config"][0],
            lock_fd=self._pinned_lock[0],
        )[1]

    def _source_authority(
        self,
        source_sha: str,
        *,
        sealed_delivery_gate: dict[str, object] | None = None,
    ) -> tuple[str, dict[str, object], dict[str, object]]:
        source_tree = _validate_source_checkout(self.source_root, source_sha)
        contract = _bootstrap_contract(self.source_root, source_sha)
        try:
            raw_readiness = (
                self.source_readiness_probe(self.source_root, source_sha)
                if self.source_readiness_probe is not None
                else contract.bootstrap_source_readiness(
                    self.source_root, expected_sha=source_sha
                )
            )
            # The exact bootstrap delivery contract reads its required-job
            # source with its own Git subprocess.  Re-establish the pure
            # source-policy gate immediately before entering that contract.
            _assert_pre_git_source_safety(self.source_root)
            raw_delivery = (
                self.delivery_gate_probe(
                    self.source_root,
                    self.runtime_root,
                    source_sha,
                    sealed_delivery_gate,
                )
                if self.delivery_gate_probe is not None
                else contract._delivery_gate(
                    self.source_root,
                    self.runtime_root,
                    source_sha,
                    allow_test=False,
                    sealed=sealed_delivery_gate,
                )
            )
        except PrerequisiteError:
            raise
        except Exception as exc:
            raise PrerequisiteError(
                "cannot prove final prerequisite source delivery authority"
            ) from exc
        readiness = _validate_source_readiness(
            raw_readiness,
            source_root=self.source_root,
            source_sha=source_sha,
            source_tree=source_tree,
        )
        delivery = _validate_delivery_gate(raw_delivery, source_sha=source_sha)
        if sealed_delivery_gate is not None and delivery != sealed_delivery_gate:
            raise PrerequisiteError("sealed prerequisite delivery gate changed")
        return source_tree, readiness, delivery

    def _sealed_source_authority(
        self,
        source_sha: str,
        *,
        sealed_readiness: object,
        sealed_delivery_gate: object,
    ) -> tuple[str, dict[str, object], dict[str, object]]:
        """Revalidate immutable local evidence after durable transaction intent.

        Protected-main membership is a mutable admission condition.  It is
        required before the transaction is created, but replay and abort bind
        to the already-sealed workflow run/attempt and must remain possible
        after a newer protected-main commit is published.
        """

        source_tree = _validate_source_checkout(self.source_root, source_sha)
        # Do not call either source or delivery probes here.  Source readiness
        # includes mutable ref/repository inventory (for example origin/main),
        # and the delivery probe contacts the remote protected branch.  Both
        # are admission checks for the first intent only.  Once intent is
        # durable, recovery is bound to the sealed document and revalidates
        # only the exact local checkout/tree plus each planned Git blob.
        raw_readiness = sealed_readiness
        readiness = _validate_source_readiness(
            raw_readiness,
            source_root=self.source_root,
            source_sha=source_sha,
            source_tree=source_tree,
        )
        delivery = _validate_delivery_gate(
            sealed_delivery_gate,
            source_sha=source_sha,
        )
        if readiness != sealed_readiness:
            raise PrerequisiteError("sealed prerequisite source readiness changed")
        return source_tree, readiness, delivery

    def _source_plan(self, source_sha: str, operation_id: str) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_operation_id(operation_id)
        source_tree, source_readiness, delivery_gate = self._source_authority(
            source_sha
        )
        adopted_digest = self._runtime_adopted_digest()
        config_fd = self._pinned_directory_fd(self.config_root)
        files: list[dict[str, object]] = []
        for source_path, name, mode, classification in TRACKED_INSTALLS:
            payload = _git_blob(self.source_root, source_sha, source_path)
            target = self.config_root / name
            operation_residue = (
                f".adopt-prereq-{operation_id}-{name}.tmp",
                f".adopt-prereq-{operation_id}-{name}.staging-quarantine",
                f".adopt-prereq-{operation_id}-{name}.abort-staging",
                f".adopt-prereq-{operation_id}-{name}.abort-staging-quarantine",
                f".adopt-prereq-{operation_id}-{name}.abort-target",
            )
            residue_exists = (
                any(_entry_exists_at(config_fd, entry) for entry in operation_residue)
                if config_fd is not None
                else any(
                    (self.config_root / entry).exists()
                    or (self.config_root / entry).is_symlink()
                    for entry in operation_residue
                )
            )
            if residue_exists:
                raise PrerequisiteError(
                    f"unowned prerequisite operation residue exists: {name}"
                )
            file_digest = _digest(payload)
            target_exists = (
                _entry_exists_at(config_fd, name)
                if config_fd is not None
                else target.exists() or target.is_symlink()
            )
            if target_exists:
                if config_fd is not None:
                    _validate_exact_file_at(
                        config_fd,
                        name,
                        digest=file_digest,
                        mode=mode,
                    )
                else:
                    _validate_exact_file(target, digest=file_digest, mode=mode)
                disposition = "existing-exact"
            else:
                disposition = "create"
            files.append(
                {
                    "source_path": source_path,
                    "destination": str(target),
                    "name": name,
                    "sha256": file_digest,
                    "mode": f"{mode:04o}",
                    "classification": classification,
                    "disposition": disposition,
                }
            )
        pgpass = self.config_root / PGPASS_NAME
        plan = {
            "schema_version": 1,
            "authority_kind": AUTHORITY_KIND,
            "operation_id": operation_id,
            "source_sha": source_sha,
            "source_tree": source_tree,
            "source_readiness": source_readiness,
            "source_readiness_sha256": _canonical_digest(source_readiness),
            "delivery_gate": delivery_gate,
            "delivery_gate_sha256": _canonical_digest(delivery_gate),
            "adopted_deployment_sha256": adopted_digest,
            "files": files,
            "preserved_pgpass": {
                "path": str(pgpass),
                "sha256": (
                    _file_digest_at(config_fd, PGPASS_NAME, mode=0o600)
                    if config_fd is not None
                    else _file_digest(pgpass, mode=0o600)
                ),
                "mode": "0600",
            },
            "mutations": {
                "services": False,
                "source": False,
                "database": False,
                "credentials": False,
            },
        }
        return plan

    def _plan_result(self, plan: dict[str, object]) -> dict[str, object]:
        return {
            "action": "adopt-prerequisites-plan",
            "apply": False,
            "logical_zero_write": True,
            "atime_zero_write": (
                _mount_suppresses_atime(self.source_root)
                and _mount_suppresses_atime(self.runtime_root)
            ),
            "plan": plan,
            "plan_sha256": _digest(_canonical_bytes(plan)),
        }

    def _load_transaction(self, operation_id: str) -> dict[str, object] | None:
        path = self._transaction_path(operation_id)
        name = path.name
        if self._pinned_directories:
            transaction_fd = self._transaction_directory_fd
            if transaction_fd is None or not _entry_exists_at(transaction_fd, name):
                return None
            document = _load_json_at(transaction_fd, name)
        else:
            if not (path.exists() or path.is_symlink()):
                return None
            document = _load_json(path)
        if (
            not _has_exact_schema_version(document, 1)
            or document.get("operation_id") != operation_id
            or document.get("status") not in {"applying", "completed", "aborted"}
            or not isinstance(document.get("plan"), dict)
            or document.get("plan_sha256")
            != _digest(_canonical_bytes(document["plan"]))
            or not isinstance(document.get("installed"), list)
            or not isinstance(document.get("owned_targets"), dict)
        ):
            raise PrerequisiteError("prerequisite adoption transaction is invalid")
        installed = document["installed"]
        owned_targets = document["owned_targets"]
        if (
            any(not isinstance(name, str) for name in installed)
            or any(not isinstance(name, str) for name in owned_targets)
            or (
                all(isinstance(name, str) for name in installed)
                and len(installed) != len(set(installed))
            )
            or set(installed) != set(owned_targets)
            or any(
                not isinstance(name, str)
                or not isinstance(identity, dict)
                or set(identity) != {"device", "inode", "mode", "size"}
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in identity.values()
                )
                for name, identity in owned_targets.items()
            )
        ):
            raise PrerequisiteError("prerequisite ownership journal is invalid")
        return document

    def _assert_exclusive_authority(self, operation_id: str) -> None:
        authority: dict[str, object] | None = None
        authority_metadata: os.stat_result | None = None
        state_fd = (
            self._pinned_directories.get("state", (None, None))[0]
            if self._pinned_directories
            else None
        )
        if isinstance(state_fd, int):
            if _entry_exists_at(state_fd, self.authority_path.name):
                authority_fd = _open_private_regular_at(
                    state_fd,
                    self.authority_path.name,
                    mode=0o600,
                    allowed_nlinks=frozenset({1, 2}),
                )
                try:
                    authority_metadata = os.fstat(authority_fd)
                    authority = _fd_json(
                        authority_fd, label="adopted prerequisite authority"
                    )
                finally:
                    os.close(authority_fd)
        elif self.authority_path.exists() or self.authority_path.is_symlink():
            authority_metadata = _private_metadata(
                self.authority_path,
                mode=0o600,
                regular=True,
                require_single_link=False,
            )
            authority = _load_json(self.authority_path, require_single_link=False)
        if authority is not None:
            if authority.get("operation_id") != operation_id:
                raise PrerequisiteError(
                    "adopted prerequisites already have another authority"
                )
            if authority_metadata is not None and authority_metadata.st_nlink == 2:
                temporary_name = (
                    f".{self.authority_path.name}.create-{operation_id}"
                )
                quarantine_name = f"{temporary_name}.quarantine"
                if isinstance(state_fd, int):
                    companions = [
                        candidate
                        for candidate in (temporary_name, quarantine_name)
                        if _entry_exists_at(state_fd, candidate)
                    ]
                    if len(companions) != 1:
                        raise PrerequisiteError(
                            "adopted prerequisite authority has an unowned hard link"
                        )
                    temporary_fd = _open_private_regular_at(
                        state_fd,
                        companions[0],
                        mode=0o600,
                        allowed_nlinks=frozenset({2}),
                    )
                    try:
                        temporary_identity = _stat_identity(os.fstat(temporary_fd))
                    finally:
                        os.close(temporary_fd)
                else:
                    candidates = [
                        self.authority_path.parent / candidate
                        for candidate in (temporary_name, quarantine_name)
                        if (self.authority_path.parent / candidate).exists()
                        or (self.authority_path.parent / candidate).is_symlink()
                    ]
                    if len(candidates) != 1:
                        raise PrerequisiteError(
                            "adopted prerequisite authority has an unowned hard link"
                        )
                    temporary = candidates[0]
                    temporary_metadata = _private_metadata(
                        temporary,
                        mode=0o600,
                        regular=True,
                        require_single_link=False,
                    )
                    temporary_identity = _stat_identity(temporary_metadata)
                if temporary_identity != _stat_identity(authority_metadata):
                    raise PrerequisiteError(
                        "adopted prerequisite authority staging differs"
                    )
        if self._pinned_directories:
            transaction_fd = self._transaction_directory_fd
            if transaction_fd is None:
                return
            entries = sorted(os.listdir(transaction_fd))
        else:
            if not (self.transaction_root.exists() or self.transaction_root.is_symlink()):
                return
            _private_metadata(self.transaction_root, mode=0o700, regular=False)
            entries = sorted(path.name for path in self.transaction_root.iterdir())
            transaction_fd = None
        for name in entries:
            if name.startswith(".") and (
                name.endswith(".json.tmp")
                or name.endswith(".json.tmp.quarantine")
            ):
                suffix = ".tmp.quarantine" if name.endswith(".tmp.quarantine") else ".tmp"
                candidate = name[1 : -len(suffix)]
                if OPERATION_RE.fullmatch(candidate.removesuffix(".json")) is None:
                    raise PrerequisiteError(
                        "prerequisite transaction inventory contains an unknown entry"
                    )
                if transaction_fd is not None:
                    temporary_fd = _open_private_regular_at(
                        transaction_fd,
                        name,
                        mode=0o600,
                    )
                    os.close(temporary_fd)
                else:
                    _private_metadata(
                        self.transaction_root / name,
                        mode=0o600,
                        regular=True,
                    )
                continue
            if not name.endswith(".json") or name == ".json":
                raise PrerequisiteError(
                    "prerequisite transaction inventory contains an unknown entry"
                )
            transaction = (
                _load_json_at(transaction_fd, name)
                if transaction_fd is not None
                else _load_json(self.transaction_root / name)
            )
            other = str(transaction.get("operation_id", ""))
            _require_operation_id(other)
            if other != name.removesuffix(".json"):
                raise PrerequisiteError("prerequisite transaction filename differs")
            if other != operation_id and transaction.get("status") == "applying":
                raise PrerequisiteError(
                    "another prerequisite adoption transaction is active"
                )

    def _write_transaction(self, document: dict[str, object]) -> None:
        path = self._transaction_path(str(document["operation_id"]))
        if self._pinned_directories:
            transaction_fd = self._ensure_transaction_directory_fd()
            _atomic_owned_json_at(transaction_fd, path.name, document)
        else:
            _atomic_owned_json(path, document)

    def _reseal_transaction(self, document: dict[str, object]) -> None:
        """Durably re-publish an exact recovered prerequisite journal."""

        if document.get("status") not in {
            "applying",
            "completed",
            "aborted",
        }:
            raise PrerequisiteError(
                "prerequisite journal cannot be resealed"
            )
        self._write_transaction(document)
        self.checkpoint("prerequisite-journal-resealed")

    def _validate_plan_context(
        self,
        plan: dict[str, object],
        source_sha: str,
        operation_id: str,
        *,
        durable: bool,
    ) -> None:
        if (
            set(plan)
            != {
                "schema_version",
                "authority_kind",
                "operation_id",
                "source_sha",
                "source_tree",
                "source_readiness",
                "source_readiness_sha256",
                "delivery_gate",
                "delivery_gate_sha256",
                "adopted_deployment_sha256",
                "files",
                "preserved_pgpass",
                "mutations",
            }
            or not _has_exact_schema_version(plan, 1)
            or plan.get("authority_kind") != AUTHORITY_KIND
            or plan.get("operation_id") != operation_id
            or plan.get("source_sha") != source_sha
            or plan.get("adopted_deployment_sha256")
            != self._runtime_adopted_digest()
        ):
            raise PrerequisiteError("prerequisite adoption plan context changed")
        sealed = plan.get("delivery_gate")
        if not isinstance(sealed, dict):
            raise PrerequisiteError("prerequisite delivery authority is invalid")
        if durable:
            source_tree, readiness, delivery = self._sealed_source_authority(
                source_sha,
                sealed_readiness=plan.get("source_readiness"),
                sealed_delivery_gate=sealed,
            )
        else:
            source_tree, readiness, delivery = self._source_authority(
                source_sha,
                sealed_delivery_gate=dict(sealed),
            )
        if (
            plan.get("source_tree") != source_tree
            or plan.get("source_readiness") != readiness
            or plan.get("source_readiness_sha256") != _canonical_digest(readiness)
            or plan.get("delivery_gate") != delivery
            or plan.get("delivery_gate_sha256") != _canonical_digest(delivery)
        ):
            raise PrerequisiteError("prerequisite source authority changed")

    def _validate_plan_targets(self, plan: dict[str, object]) -> None:
        files = plan.get("files")
        config_fd = self._pinned_directory_fd(self.config_root)
        if not isinstance(files, list) or len(files) != len(TRACKED_INSTALLS):
            raise PrerequisiteError("prerequisite adoption file plan is invalid")
        for record, expected in zip(files, TRACKED_INSTALLS, strict=True):
            source_path, name, mode, classification = expected
            if not isinstance(record, dict) or record != {
                "source_path": source_path,
                "destination": str(self.config_root / name),
                "name": name,
                "sha256": record.get("sha256"),
                "mode": f"{mode:04o}",
                "classification": classification,
                "disposition": record.get("disposition"),
            }:
                raise PrerequisiteError("prerequisite adoption file plan differs")
            if record["disposition"] not in {"create", "existing-exact"}:
                raise PrerequisiteError("prerequisite disposition is invalid")
            payload = _git_blob(self.source_root, str(plan["source_sha"]), source_path)
            if record["sha256"] != _digest(payload):
                raise PrerequisiteError("prerequisite source digest changed")
            target = self.config_root / name
            if record["disposition"] == "existing-exact":
                if config_fd is not None:
                    _validate_exact_file_at(
                        config_fd,
                        name,
                        digest=str(record["sha256"]),
                        mode=mode,
                    )
                else:
                    _validate_exact_file(
                        target, digest=str(record["sha256"]), mode=mode
                    )
        pgpass = plan.get("preserved_pgpass")
        path = self.config_root / PGPASS_NAME
        if pgpass != {
            "path": str(path),
            "sha256": (
                _file_digest_at(config_fd, PGPASS_NAME, mode=0o600)
                if config_fd is not None
                else _file_digest(path, mode=0o600)
            ),
            "mode": "0600",
        }:
            raise PrerequisiteError("preserved pgpass identity changed")

    def plan(self, *, source_sha: str, operation_id: str) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_operation_id(operation_id)
        self._assert_exclusive_authority(operation_id)
        transaction = self._load_transaction(operation_id)
        if transaction is not None:
            if transaction["status"] == "aborted":
                raise PrerequisiteError("prerequisite adoption operation was aborted")
            plan = dict(transaction["plan"])
            self._validate_plan_context(
                plan, source_sha, operation_id, durable=True
            )
            self._validate_plan_targets(plan)
            if transaction["status"] == "completed":
                for record in plan["files"]:
                    _validate_exact_file(
                        Path(str(record["destination"])),
                        digest=str(record["sha256"]),
                        mode=int(str(record["mode"]), 8),
                    )
                if self._load_authority() != self._authority(transaction):
                    raise PrerequisiteError("completed prerequisite authority differs")
            return self._plan_result(plan)
        plan = self._source_plan(source_sha, operation_id)
        return self._plan_result(plan)

    def _create_target(
        self,
        record: dict[str, object],
        payload: bytes,
        operation_id: str,
    ) -> dict[str, int]:
        config_fd = self._pinned_directory_fd(self.config_root)
        if config_fd is None:
            raise PrerequisiteError("prerequisite config directory is not pinned")
        name = str(record["name"])
        mode = int(str(record["mode"]), 8)
        temporary_name = f".adopt-prereq-{operation_id}-{name}.tmp"
        staging_quarantine = (
            f".adopt-prereq-{operation_id}-{name}.staging-quarantine"
        )
        if _entry_exists_at(config_fd, staging_quarantine):
            if _entry_exists_at(config_fd, temporary_name):
                raise PrerequisiteError(
                    f"prerequisite staging and quarantine both exist: {name}"
                )
            # Before ownership is journaled this quarantine can only be a
            # partial deterministic staging write.  Finish its interrupted
            # cleanup before recreating the staging inode.
            _remove_deterministic_temporary_at(
                config_fd,
                temporary_name,
                mode=mode,
                quarantine_name=staging_quarantine,
            )
        if _entry_exists_at(config_fd, temporary_name):
            descriptor = _open_private_regular_at(
                config_fd,
                temporary_name,
                mode=mode,
                allowed_nlinks=frozenset({1, 2}),
            )
            try:
                metadata = os.fstat(descriptor)
                temporary_identity = _identity(metadata)
                temporary_digest = _descriptor_digest(descriptor)
            finally:
                os.close(descriptor)
            if temporary_digest != record["sha256"]:
                if metadata.st_nlink != 1 or _entry_exists_at(config_fd, name):
                    raise PrerequisiteError(
                        f"prerequisite staging identity differs: {name}"
                    )
                _remove_deterministic_temporary_at(
                    config_fd,
                    temporary_name,
                    mode=mode,
                    quarantine_name=staging_quarantine,
                )
                temporary_identity = {}
        else:
            temporary_identity = {}
        if not temporary_identity:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=config_fd,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.fsync(config_fd)
            temporary_metadata = os.stat(
                temporary_name,
                dir_fd=config_fd,
                follow_symlinks=False,
            )
            temporary_identity = _identity(temporary_metadata)
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=config_fd,
                dst_dir_fd=config_fd,
                follow_symlinks=False,
            )
            os.fsync(config_fd)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            descriptor, target_identity = _open_exact_at(
                config_fd,
                name,
                digest=str(record["sha256"]),
                mode=mode,
                allowed_nlinks=frozenset({1, 2}),
            )
            os.close(descriptor)
            if target_identity != temporary_identity:
                raise PrerequisiteError(
                    f"prerequisite target appeared without operation ownership: {name}"
                ) from exc
        descriptor, target_identity = _open_exact_at(
            config_fd,
            name,
            digest=str(record["sha256"]),
            mode=mode,
            allowed_nlinks=frozenset({2}),
        )
        os.close(descriptor)
        if target_identity != temporary_identity:
            raise PrerequisiteError(
                f"prerequisite target inode differs from operation staging: {name}"
            )
        # The target/staging hard-link pair may have been observed after a
        # link whose parent-fsync response was lost.  Re-seal the namespace
        # before its ownership identity is written to the journal.
        os.fsync(config_fd)
        # Keep the operation-specific staging hard link until this identity is
        # durable in the journal.  It is the lost-response ownership proof.
        return target_identity

    def _remove_staging_link(
        self,
        record: dict[str, object],
        operation_id: str,
        identity: dict[str, int],
    ) -> None:
        config_fd = self._pinned_directory_fd(self.config_root)
        if config_fd is None:
            raise PrerequisiteError("prerequisite config directory is not pinned")
        temporary_name = f".adopt-prereq-{operation_id}-{record['name']}.tmp"
        quarantine_name = (
            f".adopt-prereq-{operation_id}-{record['name']}.staging-quarantine"
        )
        _quarantine_owned_link_at(
            config_fd,
            temporary_name,
            digest=str(record["sha256"]),
            mode=int(str(record["mode"]), 8),
            expected_identity=identity,
            quarantine_name=quarantine_name,
        )

    def _validate_owned_target(
        self,
        record: dict[str, object],
        identity: dict[str, int],
        operation_id: str,
        *,
        allow_staging_link: bool,
    ) -> None:
        config_fd = self._pinned_directory_fd(self.config_root)
        if config_fd is None:
            raise PrerequisiteError("prerequisite config directory is not pinned")
        allowed = frozenset({1, 2}) if allow_staging_link else frozenset({1})
        descriptor, _observed = _open_exact_at(
            config_fd,
            str(record["name"]),
            digest=str(record["sha256"]),
            mode=int(str(record["mode"]), 8),
            expected_identity=identity,
            allowed_nlinks=allowed,
        )
        metadata = os.fstat(descriptor)
        os.close(descriptor)
        if metadata.st_nlink == 2:
            staging_names = (
                f".adopt-prereq-{operation_id}-{record['name']}.tmp",
                f".adopt-prereq-{operation_id}-{record['name']}.staging-quarantine",
            )
            matching = 0
            for staging_name in staging_names:
                if not _entry_exists_at(config_fd, staging_name):
                    continue
                staging_fd, _staging_identity = _open_exact_at(
                    config_fd,
                    staging_name,
                    digest=str(record["sha256"]),
                    mode=int(str(record["mode"]), 8),
                    expected_identity=identity,
                    allowed_nlinks=frozenset({2}),
                )
                os.close(staging_fd)
                matching += 1
            if matching != 1:
                raise PrerequisiteError(
                    "prerequisite target has an unowned hard link"
                )

    def _authority(self, transaction: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": AUTHORITY_KIND,
            "operation_id": transaction["operation_id"],
            "source_sha": transaction["plan"]["source_sha"],
            "source_tree": transaction["plan"]["source_tree"],
            "adopted_deployment_sha256": transaction["plan"][
                "adopted_deployment_sha256"
            ],
            "plan_sha256": transaction["plan_sha256"],
            "plan": transaction["plan"],
            "completed_at": transaction["completed_at"],
        }

    def _authority_exists(self) -> bool:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is not None:
            return _entry_exists_at(state_fd, self.authority_path.name)
        return self.authority_path.exists() or self.authority_path.is_symlink()

    def _load_authority(self) -> dict[str, object]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        authority = (
            _load_json_at(state_fd, self.authority_path.name)
            if state_fd is not None
            else _load_json(self.authority_path)
        )
        if (
            not _has_exact_schema_version(authority, 1)
            or not _has_exact_schema_version(authority.get("plan"), 1)
        ):
            raise PrerequisiteError(
                "prerequisite authority schema is invalid"
            )
        return authority

    def _publish_authority(
        self,
        authority: dict[str, object],
        operation_id: str,
    ) -> None:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is None:
            raise PrerequisiteError("prerequisite state directory is not pinned")
        _create_owned_json_once_at(
            state_fd,
            self.authority_path.name,
            authority,
            operation_id=operation_id,
            checkpoint=self.checkpoint,
        )

    def apply(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
    ) -> dict[str, object]:
        planned = self.plan(source_sha=source_sha, operation_id=operation_id)
        if planned["plan_sha256"] != confirm_plan_sha256:
            raise PrerequisiteError("prerequisite plan confirmation differs")
        with self._deployment_lock():
            self.checkpoint("apply-lock-acquired")
            self._assert_pinned_runtime()
            self._assert_exclusive_authority(operation_id)
            transaction = self._load_transaction(operation_id)
            recovered_transaction = transaction is not None
            if transaction is None:
                locked_plan = self._source_plan(source_sha, operation_id)
                locked_result = self._plan_result(locked_plan)
                if (
                    locked_result["plan_sha256"] != confirm_plan_sha256
                    or locked_plan != planned["plan"]
                ):
                    raise PrerequisiteError(
                        "prerequisite plan changed before locked apply"
                    )
                self._ensure_transaction_directory_fd()
                transaction = {
                    "schema_version": 1,
                    "status": "applying",
                    "phase": "intent",
                    "operation_id": operation_id,
                    "plan": planned["plan"],
                    "plan_sha256": planned["plan_sha256"],
                    "installed": [],
                    "owned_targets": {},
                    "install_intent": None,
                    "created_at": _utc_now(),
                    "completed_at": None,
                    "aborted_at": None,
                }
                self._write_transaction(transaction)
            if transaction["status"] == "aborted":
                raise PrerequisiteError("prerequisite adoption operation was aborted")
            if transaction["plan_sha256"] != confirm_plan_sha256:
                raise PrerequisiteError("durable prerequisite plan differs")
            plan = dict(transaction["plan"])
            self._validate_plan_context(
                plan, source_sha, operation_id, durable=True
            )
            self._validate_plan_targets(plan)
            if transaction["status"] == "completed":
                authority = self._load_authority()
                if authority != self._authority(transaction):
                    raise PrerequisiteError("completed prerequisite authority differs")
                self._reseal_transaction(transaction)
                return authority
            if transaction["phase"] == "authority-commit-intent":
                for record in plan["files"]:
                    if record["disposition"] == "create":
                        identity = transaction["owned_targets"].get(record["name"])
                        if not isinstance(identity, dict):
                            raise PrerequisiteError(
                                "prerequisite target lacks durable ownership"
                            )
                        self._validate_owned_target(
                            dict(record),
                            identity,
                            operation_id,
                            allow_staging_link=False,
                        )
                    else:
                        config_fd = self._pinned_directory_fd(self.config_root)
                        if config_fd is None:  # pragma: no cover - lock owns
                            raise PrerequisiteError(
                                "prerequisite config directory is not pinned"
                            )
                        _validate_exact_file_at(
                            config_fd,
                            str(record["name"]),
                            digest=str(record["sha256"]),
                            mode=int(str(record["mode"]), 8),
                        )
                if recovered_transaction:
                    self._reseal_transaction(transaction)
                authority = self._authority(transaction)
                self._publish_authority(authority, operation_id)
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_transaction(transaction)
                return authority
            if recovered_transaction:
                self._reseal_transaction(transaction)
            files = list(plan["files"])
            installed = list(transaction["installed"])
            owned_targets = dict(transaction["owned_targets"])
            for raw in files:
                record = dict(raw)
                name = str(record["name"])
                if record["disposition"] == "existing-exact":
                    continue
                if name in installed:
                    identity = owned_targets.get(name)
                    if not isinstance(identity, dict):
                        raise PrerequisiteError(
                            "installed prerequisite lacks durable ownership"
                        )
                    self._validate_owned_target(
                        record,
                        identity,
                        operation_id,
                        allow_staging_link=True,
                    )
                    self._remove_staging_link(record, operation_id, identity)
                    continue
                transaction["phase"] = "installing"
                transaction["install_intent"] = name
                self._write_transaction(transaction)
                self.checkpoint(f"install-intent:{name}")
                payload = _git_blob(self.source_root, source_sha, str(record["source_path"]))
                identity = self._create_target(record, payload, operation_id)
                self.checkpoint(f"target-created:{name}")
                installed.append(name)
                owned_targets[name] = identity
                transaction["installed"] = installed
                transaction["owned_targets"] = owned_targets
                transaction["install_intent"] = None
                self._write_transaction(transaction)
                self.checkpoint(f"ownership-recorded:{name}")
                self._remove_staging_link(record, operation_id, identity)
            transaction["phase"] = "files-ready"
            transaction["install_intent"] = None
            self._write_transaction(transaction)
            self._validate_plan_targets(plan)
            for raw in files:
                record = dict(raw)
                if record["disposition"] != "create":
                    continue
                identity = owned_targets.get(str(record["name"]))
                if not isinstance(identity, dict):
                    raise PrerequisiteError(
                        "prerequisite target lacks durable ownership"
                    )
                self._validate_owned_target(
                    record,
                    identity,
                    operation_id,
                    allow_staging_link=True,
                )
                self._remove_staging_link(record, operation_id, identity)
                self._validate_owned_target(
                    record,
                    identity,
                    operation_id,
                    allow_staging_link=False,
                )
            transaction["phase"] = "authority-commit-intent"
            transaction["completed_at"] = _utc_now()
            self._write_transaction(transaction)
            self.checkpoint("authority-commit-intent")
            authority = self._authority(transaction)
            self._publish_authority(authority, operation_id)
            transaction["phase"] = "completed"
            transaction["status"] = "completed"
            self._write_transaction(transaction)
            return authority

    def abort(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
    ) -> dict[str, object]:
        _require_sha(source_sha)
        _require_operation_id(operation_id)
        with self._deployment_lock():
            self.checkpoint("abort-lock-acquired")
            self._assert_pinned_runtime()
            self._assert_exclusive_authority(operation_id)
            transaction = self._load_transaction(operation_id)
            if transaction is None:
                raise PrerequisiteError("prerequisite adoption transaction is unavailable")
            if transaction["plan_sha256"] != confirm_plan_sha256:
                raise PrerequisiteError("prerequisite abort confirmation differs")
            plan = dict(transaction["plan"])
            self._validate_plan_context(
                plan, source_sha, operation_id, durable=True
            )
            self._validate_plan_targets(plan)
            if transaction["status"] == "aborted":
                self._reseal_transaction(transaction)
                return transaction
            if transaction["status"] == "completed" or transaction["phase"] in {
                "authority-commit-intent",
                "completed",
            }:
                raise PrerequisiteError("prerequisite authority commit cannot be aborted")
            self._reseal_transaction(transaction)
            records = {
                str(record["name"]): dict(record)
                for record in plan["files"]
                if record["disposition"] == "create"
            }
            config_fd = self._pinned_directory_fd(self.config_root)
            if config_fd is None:  # pragma: no cover - deployment lock owns it
                raise PrerequisiteError(
                    "prerequisite config directory is not pinned"
                )
            durable_owned = dict(transaction["owned_targets"])
            removals: list[
                tuple[str, dict[str, object], dict[str, int], str]
            ] = []
            for name in transaction["installed"]:
                record = records.get(name)
                identity = durable_owned.get(name)
                if record is None or not isinstance(identity, dict):
                    raise PrerequisiteError("abort ownership is outside the plan")
                # Remove the public target first so an interrupted abort keeps
                # a private hard-link ownership proof until cleanup finishes.
                removals.append(
                    (
                        name,
                        record,
                        identity,
                        f".adopt-prereq-{operation_id}-{name}.abort-target",
                    )
                )
                removals.extend(
                    (
                        (
                            f".adopt-prereq-{operation_id}-{name}.tmp",
                            record,
                            identity,
                            f".adopt-prereq-{operation_id}-{name}.abort-staging",
                        ),
                        (
                            f".adopt-prereq-{operation_id}-{name}.staging-quarantine",
                            record,
                            identity,
                            f".adopt-prereq-{operation_id}-{name}.abort-staging-quarantine",
                        ),
                    )
                )
            intent = transaction.get("install_intent")
            if isinstance(intent, str) and intent not in durable_owned:
                record = records.get(intent)
                if record is None:
                    raise PrerequisiteError("abort ownership is outside the plan")
                mode = int(str(record["mode"]), 8)
                temporary_name = (
                    f".adopt-prereq-{operation_id}-{intent}.tmp"
                )
                abort_staging = (
                    f".adopt-prereq-{operation_id}-{intent}.abort-staging"
                )
                partial_quarantine = (
                    f".adopt-prereq-{operation_id}-{intent}.staging-quarantine"
                )
                companions = [
                    candidate
                    for candidate in (temporary_name, abort_staging)
                    if _entry_exists_at(config_fd, candidate)
                ]
                if len(companions) > 1:
                    raise PrerequisiteError(
                        "abort staging and quarantine both exist"
                    )
                if companions:
                    companion = companions[0]
                    descriptor = _open_private_regular_at(
                        config_fd,
                        companion,
                        mode=mode,
                        allowed_nlinks=frozenset({1, 2}),
                    )
                    try:
                        metadata = os.fstat(descriptor)
                        temporary_identity = _identity(metadata)
                        exact = (
                            _descriptor_digest(descriptor) == record["sha256"]
                        )
                    finally:
                        os.close(descriptor)
                    if not exact:
                        if metadata.st_nlink != 1 or companion != temporary_name:
                            raise PrerequisiteError(
                                "operation-owned file identity differs: "
                                "partial prerequisite staging"
                            )
                        if _entry_exists_at(config_fd, intent) or _entry_exists_at(
                            config_fd,
                            f".adopt-prereq-{operation_id}-{intent}.abort-target",
                        ):
                            raise PrerequisiteError(
                                "partial prerequisite staging has a target"
                            )
                        _remove_deterministic_temporary_at(
                            config_fd,
                            temporary_name,
                            mode=mode,
                            quarantine_name=partial_quarantine,
                        )
                    else:
                        target_quarantine = (
                            f".adopt-prereq-{operation_id}-{intent}.abort-target"
                        )
                        target_owned = False
                        if _entry_exists_at(config_fd, target_quarantine):
                            target_fd, target_identity = _open_exact_at(
                                config_fd,
                                target_quarantine,
                                digest=str(record["sha256"]),
                                mode=mode,
                                expected_identity=temporary_identity,
                                allowed_nlinks=frozenset({2}),
                            )
                            os.close(target_fd)
                            target_owned = target_identity == temporary_identity
                        elif _entry_exists_at(config_fd, intent):
                            try:
                                target_fd, target_identity = _open_exact_at(
                                    config_fd,
                                    intent,
                                    digest=str(record["sha256"]),
                                    mode=mode,
                                    allowed_nlinks=frozenset({1, 2}),
                                )
                            except PrerequisiteError:
                                target_identity = None
                            else:
                                os.close(target_fd)
                            target_owned = target_identity == temporary_identity
                        if metadata.st_nlink == 2 and not target_owned:
                            raise PrerequisiteError(
                                "prerequisite staging has an unowned hard link"
                            )
                        if target_owned:
                            removals.append(
                                (
                                    intent,
                                    record,
                                    temporary_identity,
                                    target_quarantine,
                                )
                            )
                        removals.append(
                            (
                                temporary_name,
                                record,
                                temporary_identity,
                                abort_staging,
                            )
                        )
                elif _entry_exists_at(config_fd, partial_quarantine):
                    if _entry_exists_at(
                        config_fd,
                        f".adopt-prereq-{operation_id}-{intent}.abort-target",
                    ):
                        raise PrerequisiteError(
                            "partial prerequisite quarantine lacks ownership proof"
                        )
                    _remove_deterministic_temporary_at(
                        config_fd,
                        temporary_name,
                        mode=mode,
                        quarantine_name=partial_quarantine,
                    )
                elif _entry_exists_at(
                    config_fd,
                    f".adopt-prereq-{operation_id}-{intent}.abort-target",
                ):
                    raise PrerequisiteError(
                        "abort target quarantine lacks ownership proof"
                    )
            # Every removal is now inode-pinned.  Each name is moved with
            # RENAME_NOREPLACE and compared to its already-opened identity
            # before that quarantined inode can be unlinked.
            for name, record, identity, quarantine_name in removals:
                _quarantine_owned_link_at(
                    config_fd,
                    name,
                    digest=str(record["sha256"]),
                    mode=int(str(record["mode"]), 8),
                    expected_identity=identity,
                    quarantine_name=quarantine_name,
                )
            transaction["status"] = "aborted"
            transaction["phase"] = "aborted"
            transaction["installed"] = []
            transaction["owned_targets"] = {}
            transaction["install_intent"] = None
            transaction["aborted_at"] = _utc_now()
            self._write_transaction(transaction)
            return transaction


def _permission_impact_document(
    evidence: dict[str, object],
) -> dict[str, object]:
    fields = (
        "schema_version",
        "policy",
        "repository",
        "marker_path",
        "records",
        "inventory_sha256",
        "original_permissions_sha256",
        "hardened_permissions_sha256",
    )
    if any(field not in evidence for field in fields):
        raise PrerequisiteError("permission hardening impact is incomplete")
    return {field: evidence[field] for field in fields}


class PermissionHardeningInstaller(PrerequisiteInstaller):
    """One-time successor authority for an already adopted production tree."""

    def __init__(
        self,
        source_root: Path,
        runtime_root: Path,
        *,
        production_root: Path = PRODUCTION_ROOT,
        checkpoint: Callable[[str], None] | None = None,
        source_readiness_probe: Callable[[Path, str], dict[str, object]] | None = None,
        delivery_gate_probe: Callable[
            [Path, Path, str, dict[str, object] | None], dict[str, object]
        ]
        | None = None,
    ) -> None:
        super().__init__(
            source_root,
            runtime_root,
            checkpoint=checkpoint,
            source_readiness_probe=source_readiness_probe,
            delivery_gate_probe=delivery_gate_probe,
        )
        self.production_root = production_root.absolute()
        self.permission_transaction_root = (
            self.runtime_root / PERMISSION_TRANSACTION_DIRECTORY
        )
        self.permission_authority_path = (
            self.runtime_root / PERMISSION_AUTHORITY_PATH
        )
        self.permission_marker_path = (
            GIT_SOURCE_TRUST.permission_takeover_marker_path(self.runtime_root)
        )
        self._pinned_production_directories: dict[
            str,
            tuple[int, tuple[int, int, int, int]],
        ] = {}

    @contextlib.contextmanager
    def _deployment_lock(self) -> Any:
        """Pin both state authority and the checkout across the outer journal."""

        with super()._deployment_lock():
            if self._pinned_production_directories:
                raise PrerequisiteError(
                    "production permission authority is already pinned"
                )
            root_fd, root_identity = _open_owned_directory_for_cas(
                self.production_root
            )
            opened = [root_fd]
            try:
                git_fd, git_identity = _open_owned_directory_for_cas(
                    Path(".git"),
                    parent_fd=root_fd,
                )
                opened.append(git_fd)
                self._pinned_production_directories = {
                    "root": (root_fd, root_identity),
                    "git": (git_fd, git_identity),
                }
                self._assert_permission_paths_pinned()
                yield
                self._assert_permission_paths_pinned()
            finally:
                self._pinned_production_directories = {}
                for descriptor in reversed(opened):
                    with contextlib.suppress(OSError):
                        os.close(descriptor)

    def _assert_pinned_production(self) -> None:
        if set(self._pinned_production_directories) != {"root", "git"}:
            raise PrerequisiteError(
                "production permission authority is not pinned"
            )
        root_fd, root_expected = self._pinned_production_directories["root"]
        git_fd, git_expected = self._pinned_production_directories["git"]
        try:
            root_descriptor = os.fstat(root_fd)
            root_path = self.production_root.stat(follow_symlinks=False)
            git_descriptor = os.fstat(git_fd)
            git_at_root = os.stat(
                ".git",
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            git_path = (self.production_root / ".git").stat(
                follow_symlinks=False
            )
        except OSError as exc:
            raise PrerequisiteError(
                "pinned production permission authority changed"
            ) from exc
        if (
            not stat.S_ISDIR(root_descriptor.st_mode)
            or not stat.S_ISDIR(root_path.st_mode)
            or _owned_directory_inode_identity(root_descriptor) != root_expected
            or _owned_directory_inode_identity(root_path) != root_expected
            or not stat.S_ISDIR(git_descriptor.st_mode)
            or not stat.S_ISDIR(git_at_root.st_mode)
            or not stat.S_ISDIR(git_path.st_mode)
            or _owned_directory_inode_identity(git_descriptor) != git_expected
            or _owned_directory_inode_identity(git_at_root) != git_expected
            or _owned_directory_inode_identity(git_path) != git_expected
        ):
            raise PrerequisiteError(
                "pinned production permission authority changed"
            )

    def _assert_permission_paths_pinned(self) -> None:
        self._assert_pinned_runtime()
        self._assert_pinned_production()

    def _assert_permission_paths_if_pinned(self) -> None:
        if self._pinned_directories or self._pinned_production_directories:
            self._assert_permission_paths_pinned()

    def _permission_marker_exists(self) -> bool:
        self._assert_permission_paths_if_pinned()
        observed = (
            self.permission_marker_path.exists()
            or self.permission_marker_path.is_symlink()
        )
        self._assert_permission_paths_if_pinned()
        return observed

    def _permission_marker_digest(self) -> str:
        self._assert_permission_paths_if_pinned()
        try:
            return _file_digest(self.permission_marker_path, mode=0o600)
        finally:
            self._assert_permission_paths_if_pinned()

    def _plan_current_permission_inventory(self) -> dict[str, object]:
        self._assert_permission_paths_if_pinned()
        try:
            return GIT_SOURCE_TRUST.plan_repository_permission_takeover(
                self.production_root,
                self.permission_marker_path,
            )
        finally:
            self._assert_permission_paths_if_pinned()

    def _permission_transaction_path(self, operation_id: str) -> Path:
        return self.permission_transaction_root / (
            f"{_require_permission_operation_id(operation_id)}.json"
        )

    def _permission_transaction_directory_fd(
        self,
        *,
        create: bool,
    ) -> int | None:
        if "state" not in self._pinned_directories:
            if not (
                self.permission_transaction_root.exists()
                or self.permission_transaction_root.is_symlink()
            ):
                return None
            return _open_private_directory(self.permission_transaction_root)
        state_fd = self._pinned_directories["state"][0]
        try:
            directory_fd = _open_private_directory(
                Path(PERMISSION_TRANSACTION_DIRECTORY.name),
                parent_fd=state_fd,
            )
        except PrerequisiteError:
            try:
                os.stat(
                    PERMISSION_TRANSACTION_DIRECTORY.name,
                    dir_fd=state_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    return None
            else:
                raise
            try:
                os.mkdir(
                    PERMISSION_TRANSACTION_DIRECTORY.name,
                    mode=0o700,
                    dir_fd=state_fd,
                )
            except FileExistsError:
                pass
            directory_fd = _open_private_directory(
                Path(PERMISSION_TRANSACTION_DIRECTORY.name),
                parent_fd=state_fd,
            )
        if create:
            try:
                os.fsync(state_fd)
            except Exception:
                os.close(directory_fd)
                raise
        return directory_fd

    def _load_permission_transaction(
        self,
        operation_id: str,
    ) -> dict[str, object] | None:
        operation_id = _require_permission_operation_id(operation_id)
        name = f"{operation_id}.json"
        directory_fd = self._permission_transaction_directory_fd(create=False)
        if directory_fd is None:
            return None
        try:
            if not _entry_exists_at(directory_fd, name):
                return None
            document = _load_json_at(
                directory_fd,
                name,
                maximum_bytes=PERMISSION_JSON_MAX_BYTES,
            )
        finally:
            os.close(directory_fd)
        fields = {
            "schema_version",
            "status",
            "phase",
            "operation_id",
            "plan",
            "plan_sha256",
            "permission_impact_sha256",
            "permission_checkpoint",
            "permission_marker_sha256",
            "permission_evidence_sha256",
            "source_trust_sha256",
            "created_at",
            "completed_at",
            "aborted_at",
        }
        if (
            set(document) != fields
            or not _has_exact_schema_version(document, 1)
            or document.get("operation_id") != operation_id
            or document.get("status") not in {"applying", "completed", "aborted"}
            or document.get("phase") not in PERMISSION_TRANSACTION_PHASES
            or not isinstance(document.get("plan"), dict)
            or document.get("plan_sha256")
            != _canonical_digest(document["plan"])
            or document.get("permission_impact_sha256")
            != document["plan"].get("permission_impact_sha256")
            or (
                document["status"] == "completed"
                and document["phase"] != "completed"
            )
            or (
                document["status"] == "aborted"
                and document["phase"] != "aborted"
            )
            or (
                document["status"] == "applying"
                and document["phase"] in {"completed", "aborted"}
            )
        ):
            raise PrerequisiteError(
                "permission hardening transaction is invalid"
            )
        for field in (
            "permission_marker_sha256",
            "permission_evidence_sha256",
            "source_trust_sha256",
        ):
            value = document.get(field)
            if value is not None and (
                not isinstance(value, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
            ):
                raise PrerequisiteError(
                    "permission hardening journal digest is invalid"
                )
        checkpoint = document.get("permission_checkpoint")
        if checkpoint is not None and (
            not isinstance(checkpoint, str)
            or not checkpoint.startswith("permission:")
            or len(checkpoint) > 512
        ):
            raise PrerequisiteError(
                "permission hardening journal checkpoint is invalid"
            )
        return document

    def _write_permission_transaction(
        self,
        document: dict[str, object],
    ) -> None:
        if not self._pinned_directories:
            raise PrerequisiteError(
                "permission hardening journal requires the deploy lock"
            )
        directory_fd = self._permission_transaction_directory_fd(create=True)
        if directory_fd is None:  # pragma: no cover - create owns
            raise PrerequisiteError(
                "permission hardening transaction directory is unavailable"
            )
        try:
            _atomic_owned_json_at(
                directory_fd,
                self._permission_transaction_path(
                    str(document["operation_id"])
                ).name,
                document,
            )
        finally:
            os.close(directory_fd)

    def _reseal_permission_transaction(
        self,
        document: dict[str, object],
    ) -> None:
        """Durably re-publish an exact recovered permission journal."""

        if document.get("status") not in {
            "applying",
            "completed",
            "aborted",
        }:
            raise PrerequisiteError(
                "permission hardening journal cannot be resealed"
            )
        self._write_permission_transaction(document)
        self.checkpoint("permission-journal-resealed")

    def _permission_authority_exists(self) -> bool:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is not None:
            return _entry_exists_at(
                state_fd, PERMISSION_AUTHORITY_PATH.name
            )
        return (
            self.permission_authority_path.exists()
            or self.permission_authority_path.is_symlink()
        )

    def _load_permission_authority(
        self,
        *,
        require_single_link: bool = True,
    ) -> dict[str, object]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        authority = (
            _load_json_at(
                state_fd,
                PERMISSION_AUTHORITY_PATH.name,
                require_single_link=require_single_link,
                maximum_bytes=PERMISSION_JSON_MAX_BYTES,
            )
            if state_fd is not None
            else _load_json(
                self.permission_authority_path,
                require_single_link=require_single_link,
                maximum_bytes=PERMISSION_JSON_MAX_BYTES,
            )
        )
        if (
            not _has_exact_schema_version(authority, 1)
            or not _has_exact_schema_version(authority.get("plan"), 1)
        ):
            raise PrerequisiteError(
                "permission authority schema is invalid"
            )
        return authority

    def _publish_permission_authority(
        self,
        authority: dict[str, object],
        operation_id: str,
    ) -> None:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is None:
            raise PrerequisiteError(
                "permission authority requires the pinned state directory"
            )
        _create_owned_json_once_at(
            state_fd,
            PERMISSION_AUTHORITY_PATH.name,
            authority,
            operation_id=operation_id,
            checkpoint=self.checkpoint,
            maximum_bytes=PERMISSION_JSON_MAX_BYTES,
        )

    def _assert_permission_exclusive(self, operation_id: str) -> None:
        operation_id = _require_permission_operation_id(operation_id)
        if self._permission_authority_exists():
            authority = self._load_permission_authority(
                require_single_link=False
            )
            if authority.get("operation_id") != operation_id:
                raise PrerequisiteError(
                    "adopted Git permissions already have another authority"
                )
            state_fd = self._pinned_directory_fd(
                self.runtime_root / "state"
            )
            metadata = (
                os.stat(
                    PERMISSION_AUTHORITY_PATH.name,
                    dir_fd=state_fd,
                    follow_symlinks=False,
                )
                if state_fd is not None
                else self.permission_authority_path.stat(
                    follow_symlinks=False
                )
            )
            if metadata.st_nlink == 2:
                temporary_name = (
                    f".{PERMISSION_AUTHORITY_PATH.name}.create-{operation_id}"
                )
                quarantine_name = f"{temporary_name}.quarantine"
                if state_fd is not None:
                    companions = [
                        name
                        for name in (temporary_name, quarantine_name)
                        if _entry_exists_at(state_fd, name)
                    ]
                    if len(companions) != 1:
                        raise PrerequisiteError(
                            "permission authority has an unowned hard link"
                        )
                    companion_fd = _open_private_regular_at(
                        state_fd,
                        companions[0],
                        mode=0o600,
                        allowed_nlinks=frozenset({2}),
                    )
                    try:
                        companion_identity = _stat_identity(
                            os.fstat(companion_fd)
                        )
                    finally:
                        os.close(companion_fd)
                else:
                    companions = [
                        self.permission_authority_path.parent / name
                        for name in (temporary_name, quarantine_name)
                        if (
                            self.permission_authority_path.parent / name
                        ).exists()
                        or (
                            self.permission_authority_path.parent / name
                        ).is_symlink()
                    ]
                    if len(companions) != 1:
                        raise PrerequisiteError(
                            "permission authority has an unowned hard link"
                        )
                    companion_metadata = _private_metadata(
                        companions[0],
                        mode=0o600,
                        regular=True,
                        require_single_link=False,
                    )
                    companion_identity = _stat_identity(companion_metadata)
                if companion_identity != _stat_identity(metadata):
                    raise PrerequisiteError(
                        "permission authority staging identity differs"
                    )
        directory_fd = self._permission_transaction_directory_fd(create=False)
        if directory_fd is None:
            return
        try:
            entries = sorted(os.listdir(directory_fd))
            for name in entries:
                if name in {
                    f".{operation_id}.json.tmp",
                    f".{operation_id}.json.tmp.quarantine",
                }:
                    continue
                if not name.endswith(".json") or name == ".json":
                    raise PrerequisiteError(
                        "permission transaction inventory has an unknown entry"
                    )
                other = name.removesuffix(".json")
                _require_permission_operation_id(other)
                document = _load_json_at(
                    directory_fd,
                    name,
                    maximum_bytes=PERMISSION_JSON_MAX_BYTES,
                )
                if (
                    not _has_exact_schema_version(document, 1)
                    or document.get("operation_id") != other
                    or document.get("status")
                    not in {"applying", "completed", "aborted"}
                ):
                    raise PrerequisiteError(
                        "permission transaction inventory is invalid"
                    )
                if (
                    other != operation_id
                    and document.get("status") == "applying"
                ):
                    raise PrerequisiteError(
                        "another permission hardening transaction is active"
                    )
        finally:
            os.close(directory_fd)

    def _read_adoption_permission_authorities(
        self,
    ) -> dict[str, tuple[dict[str, object], str]]:
        """Read each base document and its raw digest from one stable inode."""

        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is None:
            adopted = _load_json_with_digest(
                self.runtime_root / ADOPTED_DEPLOYMENT_PATH
            )
            bootstrap = _load_json_with_digest(
                self.runtime_root / BOOTSTRAP_CONTROL_PATH
            )
            base = _load_json_with_digest(self.authority_path)
        else:
            adopted = _load_json_with_digest_at(
                state_fd,
                ADOPTED_DEPLOYMENT_PATH.name,
            )
            bootstrap = _load_json_with_digest_at(
                state_fd,
                BOOTSTRAP_CONTROL_PATH.name,
            )
            base = _load_json_with_digest_at(
                state_fd,
                AUTHORITY_PATH.name,
            )
        return {
            "adopted": adopted,
            "bootstrap": bootstrap,
            "base": base,
        }

    def _adoption_permission_context(
        self,
        *,
        permit_prepared_abort: bool = False,
    ) -> dict[str, object]:
        for relative in (
            Path("state/current-deployment.json"),
            Path("state/deploy-in-progress.json"),
        ):
            path = self.runtime_root / relative
            if path.exists() or path.is_symlink():
                raise PrerequisiteError(
                    "permission hardening is restricted to raw manual adoption"
                )
        prepared_root = self.runtime_root / "state/prepared"
        if (
            not permit_prepared_abort
            and (prepared_root.exists() or prepared_root.is_symlink())
        ):
            _private_metadata(
                prepared_root,
                mode=0o700,
                regular=False,
            )
            if any(prepared_root.iterdir()):
                raise PrerequisiteError(
                    "prepared deployment exists before permission hardening"
                )
        self._assert_permission_paths_if_pinned()
        adopted_digest = self._runtime_adopted_digest()
        authorities = self._read_adoption_permission_authorities()
        self._assert_permission_paths_if_pinned()
        adopted, stable_adopted_digest = authorities["adopted"]
        bootstrap, bootstrap_digest = authorities["bootstrap"]
        base, base_digest = authorities["base"]
        # The runtime validator and every document+digest read must describe
        # one unchanged generation. A second complete pass rejects atomic
        # replacement between otherwise individually stable file opens.
        authorities_after = self._read_adoption_permission_authorities()
        adopted_digest_after = self._runtime_adopted_digest()
        self._assert_permission_paths_if_pinned()
        if (
            adopted_digest != stable_adopted_digest
            or adopted_digest_after != stable_adopted_digest
            or authorities_after != authorities
        ):
            raise PrerequisiteError(
                "manual adoption permission authorities changed while validating"
            )
        production_sha = adopted.get("source_sha")
        production_tree = adopted.get("source_tree")
        if (
            not _has_exact_schema_version(adopted, 1)
            or adopted.get("status") != "adopted"
            or adopted.get("authority_kind") != ADOPTION_AUTHORITY_KIND
            or not isinstance(production_sha, str)
            or SHA_RE.fullmatch(production_sha) is None
            or not isinstance(production_tree, str)
            or SHA_RE.fullmatch(production_tree) is None
            or not _has_exact_schema_version(bootstrap, 3)
            or not _has_exact_schema_version(
                bootstrap.get("adopted_deployment"), 1
            )
            or bootstrap.get("status") != "completed"
            or bootstrap.get("authority_kind") != ADOPTION_AUTHORITY_KIND
            or bootstrap.get("adopted_deployment") != adopted
            or bootstrap.get("adopted_deployment_sha256")
            != _canonical_digest(adopted)
        ):
            raise PrerequisiteError(
                "manual adoption permission context is invalid"
            )
        base_fields = {
            "schema_version",
            "status",
            "authority_kind",
            "operation_id",
            "source_sha",
            "source_tree",
            "adopted_deployment_sha256",
            "plan_sha256",
            "plan",
            "completed_at",
        }
        base_plan = base.get("plan")
        base_operation = str(base.get("operation_id", ""))
        base_source_sha = str(base.get("source_sha", ""))
        base_source_tree = str(base.get("source_tree", ""))
        if (
            set(base) != base_fields
            or not _has_exact_schema_version(base, 1)
            or base.get("status") != "completed"
            or base.get("authority_kind") != AUTHORITY_KIND
            or OPERATION_RE.fullmatch(base_operation) is None
            or SHA_RE.fullmatch(base_source_sha) is None
            or SHA_RE.fullmatch(base_source_tree) is None
            or not isinstance(base_plan, dict)
            or not _has_exact_schema_version(base_plan, 1)
            or base.get("plan_sha256") != _canonical_digest(base_plan)
            or base.get("adopted_deployment_sha256") != adopted_digest
            or base_plan.get("adopted_deployment_sha256") != adopted_digest
            or base_plan.get("operation_id") != base.get("operation_id")
            or base_plan.get("source_sha") != base.get("source_sha")
            or base_plan.get("source_tree") != base.get("source_tree")
        ):
            raise PrerequisiteError(
                "completed adopted prerequisite authority is invalid"
            )
        self._validate_plan_targets(dict(base_plan))
        return {
            "adopted_deployment_sha256": adopted_digest,
            "bootstrap_control_sha256": bootstrap_digest,
            "adopted_prerequisites_sha256": base_digest,
            "adopted_prerequisites_plan_sha256": base["plan_sha256"],
            "production_source": {
                "source_sha": production_sha,
                "source_tree": production_tree,
            },
        }

    def _permission_source_plan(
        self,
        source_sha: str,
        operation_id: str,
    ) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_permission_operation_id(operation_id)
        source_tree, source_readiness, delivery_gate = self._source_authority(
            source_sha
        )
        context = self._adoption_permission_context()
        if self._permission_marker_exists():
            raise PrerequisiteError(
                "unowned production Git permission marker already exists"
            )
        try:
            permission = self._plan_current_permission_inventory()
        except Exception as exc:
            raise PrerequisiteError(
                "cannot plan production Git permission hardening"
            ) from exc
        impact = _permission_impact_document(permission)
        impact_digest = _canonical_digest(impact)
        return {
            "schema_version": 1,
            "authority_kind": PERMISSION_AUTHORITY_KIND,
            "operation_id": operation_id,
            "source_sha": source_sha,
            "source_tree": source_tree,
            "source_readiness": source_readiness,
            "source_readiness_sha256": _canonical_digest(source_readiness),
            "delivery_gate": delivery_gate,
            "delivery_gate_sha256": _canonical_digest(delivery_gate),
            **context,
            "permission_takeover": permission,
            "permission_impact_sha256": impact_digest,
            "mutations": {
                "services": False,
                "source_content": False,
                "source_refs": False,
                "database": False,
                "credentials": False,
                "git_permissions": True,
                "runtime_authority": True,
            },
        }

    def _permission_plan_result(
        self,
        plan: dict[str, object],
    ) -> dict[str, object]:
        return {
            "action": "adopt-git-permission-plan",
            "apply": False,
            "logical_zero_write": True,
            "atime_zero_write": (
                _mount_suppresses_atime(self.source_root)
                and _mount_suppresses_atime(self.runtime_root)
                and _mount_suppresses_atime(self.production_root)
            ),
            "plan": plan,
            "plan_sha256": _canonical_digest(plan),
            "permission_impact_sha256": plan[
                "permission_impact_sha256"
            ],
        }

    @staticmethod
    def _permission_immutable_projection(
        evidence: dict[str, object],
    ) -> dict[str, object]:
        return _permission_impact_document(evidence)

    def _validate_permission_plan_context(
        self,
        plan: dict[str, object],
        source_sha: str,
        operation_id: str,
        *,
        durable: bool,
    ) -> None:
        expected_fields = {
            "schema_version",
            "authority_kind",
            "operation_id",
            "source_sha",
            "source_tree",
            "source_readiness",
            "source_readiness_sha256",
            "delivery_gate",
            "delivery_gate_sha256",
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "adopted_prerequisites_plan_sha256",
            "production_source",
            "permission_takeover",
            "permission_impact_sha256",
            "mutations",
        }
        context = self._adoption_permission_context()
        if (
            set(plan) != expected_fields
            or not _has_exact_schema_version(plan, 1)
            or plan.get("authority_kind") != PERMISSION_AUTHORITY_KIND
            or plan.get("operation_id") != operation_id
            or plan.get("source_sha") != source_sha
            or any(plan.get(field) != value for field, value in context.items())
            or plan.get("mutations")
            != {
                "services": False,
                "source_content": False,
                "source_refs": False,
                "database": False,
                "credentials": False,
                "git_permissions": True,
                "runtime_authority": True,
            }
        ):
            raise PrerequisiteError(
                "permission hardening plan context changed"
            )
        sealed_delivery = plan.get("delivery_gate")
        if not isinstance(sealed_delivery, dict):
            raise PrerequisiteError(
                "permission hardening delivery authority is invalid"
            )
        if durable:
            source_tree, readiness, delivery = self._sealed_source_authority(
                source_sha,
                sealed_readiness=plan.get("source_readiness"),
                sealed_delivery_gate=sealed_delivery,
            )
        else:
            source_tree, readiness, delivery = self._source_authority(
                source_sha,
                sealed_delivery_gate=dict(sealed_delivery),
            )
        if (
            plan.get("source_tree") != source_tree
            or plan.get("source_readiness") != readiness
            or plan.get("source_readiness_sha256")
            != _canonical_digest(readiness)
            or plan.get("delivery_gate") != delivery
            or plan.get("delivery_gate_sha256")
            != _canonical_digest(delivery)
        ):
            raise PrerequisiteError(
                "permission hardening source authority changed"
            )
        raw_permission = plan.get("permission_takeover")
        try:
            permission = GIT_SOURCE_TRUST.validate_permission_takeover_evidence(
                raw_permission,
                repository=self.production_root,
                marker_path=self.permission_marker_path,
                allowed_phases={"captured"},
            )
        except Exception as exc:
            raise PrerequisiteError(
                "sealed permission hardening inventory is invalid"
            ) from exc
        impact = self._permission_immutable_projection(permission)
        if plan.get("permission_impact_sha256") != _canonical_digest(impact):
            raise PrerequisiteError(
                "permission hardening impact digest changed"
            )

    def _validate_permission_marker_against_plan(
        self,
        plan: dict[str, object],
        *,
        require_hardened: bool,
        require_original_mutable: bool = False,
    ) -> dict[str, object]:
        captured = dict(plan["permission_takeover"])
        self._assert_permission_paths_if_pinned()
        try:
            try:
                if require_hardened:
                    observed = (
                        GIT_SOURCE_TRUST.verify_repository_permission_takeover(
                            self.production_root,
                            self.permission_marker_path,
                            verify_content=True,
                            require_original_mutable=require_original_mutable,
                        )
                    )
                else:
                    observed = (
                        GIT_SOURCE_TRUST.read_repository_permission_takeover(
                            self.production_root,
                            self.permission_marker_path,
                            allowed_phases={
                                "captured",
                                "root-intent",
                                "root-hardened",
                                "metadata-directories-intent",
                                "metadata-directories-hardened",
                                "metadata-files-intent",
                                "metadata-files-hardened",
                                "hardened",
                            },
                        )
                    )
            except Exception as exc:
                raise PrerequisiteError(
                    "production Git permission marker is invalid"
                ) from exc
        finally:
            self._assert_permission_paths_if_pinned()
        if self._permission_immutable_projection(observed) != (
            self._permission_immutable_projection(captured)
        ):
            raise PrerequisiteError(
                "production Git permission inventory differs from the plan"
            )
        return dict(observed)

    def plan(
        self,
        *,
        source_sha: str,
        operation_id: str,
    ) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_permission_operation_id(operation_id)
        self._assert_permission_exclusive(operation_id)
        transaction = self._load_permission_transaction(operation_id)
        if transaction is None:
            if self._permission_authority_exists():
                raise PrerequisiteError(
                    "permission authority exists without its transaction"
                )
            plan = self._permission_source_plan(source_sha, operation_id)
            return self._permission_plan_result(plan)
        if transaction["status"] == "aborted":
            raise PrerequisiteError(
                "permission hardening operation was aborted"
            )
        plan = dict(transaction["plan"])
        self._validate_permission_plan_context(
            plan, source_sha, operation_id, durable=True
        )
        marker_exists = self._permission_marker_exists()
        if transaction["phase"] == "intent" or (
            transaction["phase"] == "permission-change-intent"
            and not marker_exists
        ):
            if transaction["phase"] == "intent" and marker_exists:
                raise PrerequisiteError(
                    "permission marker appeared before durable mutation intent"
                )
            current = self._plan_current_permission_inventory()
            if current != plan["permission_takeover"]:
                raise PrerequisiteError(
                    "permission inventory changed after durable intent"
                )
        else:
            self._validate_permission_marker_against_plan(
                plan,
                require_hardened=transaction["phase"]
                in {
                    "permission-ready",
                    "source-verified",
                    "authority-commit-intent",
                    "completed",
                },
            )
        if transaction["status"] == "completed":
            if self._load_permission_authority() != self._authority(transaction):
                raise PrerequisiteError(
                    "completed permission authority differs"
                )
        return self._permission_plan_result(plan)

    def _production_source_trust(
        self,
        plan: dict[str, object],
    ) -> str:
        expected = plan["production_source"]
        if not isinstance(expected, dict):  # pragma: no cover - validator owns
            raise PrerequisiteError("production source authority is invalid")
        self._assert_permission_paths_if_pinned()
        try:
            try:
                before = GIT_SOURCE_TRUST.repository_preflight_evidence(
                    self.production_root,
                    ambient={},
                )
                status = GIT_SOURCE_TRUST.run_git(
                    self.production_root,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    ambient={},
                ).stdout
                if status:
                    raise PrerequisiteError(
                        "production checkout changed after manual adoption"
                    )
                trust = GIT_SOURCE_TRUST.repository_trust_evidence(
                    self.production_root,
                    source_sha=str(expected["source_sha"]),
                    source_tree=str(expected["source_tree"]),
                    branch="refs/heads/main",
                    origin=None,
                    ambient={},
                )
                GIT_SOURCE_TRUST.require_stable_trust_surface(before, trust)
            except PrerequisiteError:
                raise
            except Exception as exc:
                raise PrerequisiteError(
                    "production source differs from manual adoption"
                ) from exc
        finally:
            self._assert_permission_paths_if_pinned()
        return str(trust["evidence_sha256"])

    def _authority(
        self,
        transaction: dict[str, object],
    ) -> dict[str, object]:
        plan = transaction["plan"]
        production = plan["production_source"]
        return {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": PERMISSION_AUTHORITY_KIND,
            "operation_id": transaction["operation_id"],
            "source_sha": plan["source_sha"],
            "source_tree": plan["source_tree"],
            "production_source_sha": production["source_sha"],
            "production_source_tree": production["source_tree"],
            "adopted_deployment_sha256": plan[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": plan[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": plan[
                "adopted_prerequisites_sha256"
            ],
            "plan_sha256": transaction["plan_sha256"],
            "permission_impact_sha256": transaction[
                "permission_impact_sha256"
            ],
            "permission_marker_sha256": transaction[
                "permission_marker_sha256"
            ],
            "permission_evidence_sha256": transaction[
                "permission_evidence_sha256"
            ],
            "permission_inventory_sha256": plan[
                "permission_takeover"
            ]["inventory_sha256"],
            "original_permissions_sha256": plan[
                "permission_takeover"
            ]["original_permissions_sha256"],
            "hardened_permissions_sha256": plan[
                "permission_takeover"
            ]["hardened_permissions_sha256"],
            "plan": plan,
            "completed_at": transaction["completed_at"],
        }

    def _revalidate_permission_commit_evidence(
        self,
        transaction: dict[str, object],
        plan: dict[str, object],
    ) -> None:
        """Keep live evidence and both pinned roots adjacent to publication."""

        self._assert_permission_paths_pinned()
        marker = self._validate_permission_marker_against_plan(
            plan,
            require_hardened=True,
        )
        if (
            self._permission_marker_digest()
            != transaction["permission_marker_sha256"]
            or marker["evidence_sha256"]
            != transaction["permission_evidence_sha256"]
            or self._production_source_trust(plan)
            != transaction["source_trust_sha256"]
        ):
            raise PrerequisiteError(
                "permission authority commit evidence changed"
            )
        self._assert_permission_paths_pinned()

    def apply(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_permission_impact_sha256: str,
    ) -> dict[str, object]:
        planned = self.plan(
            source_sha=source_sha,
            operation_id=operation_id,
        )
        if planned["plan_sha256"] != confirm_plan_sha256:
            raise PrerequisiteError(
                "permission hardening plan confirmation differs"
            )
        if (
            planned["permission_impact_sha256"]
            != confirm_permission_impact_sha256
        ):
            raise PrerequisiteError(
                "permission hardening impact confirmation differs"
            )
        with self._deployment_lock():
            self.checkpoint("permission-apply-lock-acquired")
            self._assert_permission_paths_pinned()
            self._assert_permission_exclusive(operation_id)
            transaction = self._load_permission_transaction(operation_id)
            recovered_transaction = transaction is not None
            if transaction is None:
                locked_plan = self._permission_source_plan(
                    source_sha, operation_id
                )
                if (
                    locked_plan != planned["plan"]
                    or _canonical_digest(locked_plan) != confirm_plan_sha256
                    or locked_plan["permission_impact_sha256"]
                    != confirm_permission_impact_sha256
                ):
                    raise PrerequisiteError(
                        "permission hardening plan changed before locked apply"
                    )
                transaction = {
                    "schema_version": 1,
                    "status": "applying",
                    "phase": "intent",
                    "operation_id": operation_id,
                    "plan": locked_plan,
                    "plan_sha256": confirm_plan_sha256,
                    "permission_impact_sha256": (
                        confirm_permission_impact_sha256
                    ),
                    "permission_checkpoint": None,
                    "permission_marker_sha256": None,
                    "permission_evidence_sha256": None,
                    "source_trust_sha256": None,
                    "created_at": _utc_now(),
                    "completed_at": None,
                    "aborted_at": None,
                }
                self._write_permission_transaction(transaction)
                self.checkpoint("permission-intent")
            if transaction["status"] == "aborted":
                raise PrerequisiteError(
                    "permission hardening operation was aborted"
                )
            if (
                transaction["plan_sha256"] != confirm_plan_sha256
                or transaction["permission_impact_sha256"]
                != confirm_permission_impact_sha256
            ):
                raise PrerequisiteError(
                    "durable permission hardening plan differs"
                )
            plan = dict(transaction["plan"])
            self._validate_permission_plan_context(
                plan, source_sha, operation_id, durable=True
            )
            if transaction["status"] == "completed":
                authority = self._load_permission_authority()
                if authority != self._authority(transaction):
                    raise PrerequisiteError(
                        "completed permission authority differs"
                    )
                self._revalidate_permission_commit_evidence(
                    transaction,
                    plan,
                )
                self._reseal_permission_transaction(transaction)
                return authority
            if transaction["phase"] == "authority-commit-intent":
                self._revalidate_permission_commit_evidence(
                    transaction,
                    plan,
                )
                if recovered_transaction:
                    self._reseal_permission_transaction(transaction)
                authority = self._authority(transaction)
                self._publish_permission_authority(authority, operation_id)
                self._assert_permission_paths_pinned()
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_permission_transaction(transaction)
                return authority
            if recovered_transaction:
                self._reseal_permission_transaction(transaction)
            if transaction["phase"] == "intent":
                if self._permission_marker_exists():
                    raise PrerequisiteError(
                        "permission marker appeared before mutation intent"
                    )
                transaction["phase"] = "permission-change-intent"
                self._write_permission_transaction(transaction)
                self.checkpoint("permission-change-intent")
            if transaction["phase"] == "permission-change-intent":
                captured = dict(plan["permission_takeover"])

                def permission_checkpoint(label: str) -> None:
                    self._assert_permission_paths_pinned()
                    if label in {
                        "permission:captured",
                        "permission:root-intent",
                        "permission:root-hardened",
                        "permission:metadata-directories-intent",
                        "permission:metadata-directories-hardened",
                        "permission:metadata-files-intent",
                        "permission:metadata-files-hardened",
                        "permission:hardened",
                    }:
                        transaction["permission_checkpoint"] = label
                        self._write_permission_transaction(transaction)
                    self.checkpoint(label)
                    self._assert_permission_paths_pinned()

                try:
                    self._assert_permission_paths_pinned()
                    try:
                        marker = (
                            GIT_SOURCE_TRUST.takeover_repository_permissions(
                                self.production_root,
                                self.permission_marker_path,
                                checkpoint=permission_checkpoint,
                                expected_inventory_sha256=captured[
                                    "inventory_sha256"
                                ],
                                expected_original_permissions_sha256=captured[
                                    "original_permissions_sha256"
                                ],
                                expected_hardened_permissions_sha256=captured[
                                    "hardened_permissions_sha256"
                                ],
                            )
                        )
                    finally:
                        self._assert_permission_paths_pinned()
                except Exception as exc:
                    raise PrerequisiteError(
                        "production Git permission hardening did not complete"
                    ) from exc
                if self._permission_immutable_projection(marker) != (
                    self._permission_immutable_projection(captured)
                ):
                    raise PrerequisiteError(
                        "hardened permission inventory differs"
                    )
                transaction["permission_marker_sha256"] = (
                    self._permission_marker_digest()
                )
                transaction["permission_evidence_sha256"] = marker[
                    "evidence_sha256"
                ]
                transaction["phase"] = "permission-ready"
                transaction["permission_checkpoint"] = "permission:hardened"
                self._write_permission_transaction(transaction)
                self.checkpoint("permission-ready")
            if transaction["phase"] == "permission-ready":
                marker = self._validate_permission_marker_against_plan(
                    plan,
                    require_hardened=True,
                    require_original_mutable=True,
                )
                if (
                    self._permission_marker_digest()
                    != transaction["permission_marker_sha256"]
                    or marker["evidence_sha256"]
                    != transaction["permission_evidence_sha256"]
                ):
                    raise PrerequisiteError(
                        "permission marker changed before source verification"
                    )
                transaction["source_trust_sha256"] = (
                    self._production_source_trust(plan)
                )
                transaction["phase"] = "source-verified"
                self._write_permission_transaction(transaction)
                self.checkpoint("permission-source-verified")
            if transaction["phase"] == "source-verified":
                marker = self._validate_permission_marker_against_plan(
                    plan, require_hardened=True
                )
                if (
                    self._permission_marker_digest()
                    != transaction["permission_marker_sha256"]
                    or marker["evidence_sha256"]
                    != transaction["permission_evidence_sha256"]
                    or self._production_source_trust(plan)
                    != transaction["source_trust_sha256"]
                ):
                    raise PrerequisiteError(
                        "permission source evidence changed before commit"
                    )
                transaction["phase"] = "authority-commit-intent"
                transaction["completed_at"] = _utc_now()
                self._write_permission_transaction(transaction)
                self.checkpoint("permission-authority-commit-intent")
                self._revalidate_permission_commit_evidence(
                    transaction,
                    plan,
                )
                authority = self._authority(transaction)
                self._publish_permission_authority(authority, operation_id)
                self._assert_permission_paths_pinned()
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_permission_transaction(transaction)
                return authority
            raise PrerequisiteError(
                "permission hardening transaction is in an unknown phase"
            )

    def abort(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_permission_impact_sha256: str,
    ) -> dict[str, object]:
        _require_sha(source_sha)
        _require_permission_operation_id(operation_id)
        with self._deployment_lock():
            self.checkpoint("permission-abort-lock-acquired")
            self._assert_permission_paths_pinned()
            self._assert_permission_exclusive(operation_id)
            transaction = self._load_permission_transaction(operation_id)
            if transaction is None:
                raise PrerequisiteError(
                    "permission hardening transaction is unavailable"
                )
            if (
                transaction["plan_sha256"] != confirm_plan_sha256
                or transaction["permission_impact_sha256"]
                != confirm_permission_impact_sha256
            ):
                raise PrerequisiteError(
                    "permission abort confirmation differs"
                )
            plan = dict(transaction["plan"])
            self._validate_permission_plan_context(
                plan, source_sha, operation_id, durable=True
            )
            if (
                transaction["status"] != "aborted"
                and transaction["phase"] != "intent"
            ):
                raise PrerequisiteError(
                    "permission change intent is forward-only and cannot be aborted"
                )
            if self._permission_marker_exists():
                raise PrerequisiteError(
                    "permission marker exists before abortable boundary"
                )
            current = self._plan_current_permission_inventory()
            if current != plan["permission_takeover"]:
                raise PrerequisiteError(
                    "permission inventory changed before abort"
                )
            self._reseal_permission_transaction(transaction)
            if transaction["status"] == "aborted":
                return transaction
            transaction["status"] = "aborted"
            transaction["phase"] = "aborted"
            transaction["aborted_at"] = _utc_now()
            self._write_permission_transaction(transaction)
            return transaction


class UnitPermissionHardeningInstaller(PermissionHardeningInstaller):
    """Adopt the legacy MD unit mode without changing either running Worker."""

    def __init__(
        self,
        source_root: Path,
        runtime_root: Path,
        *,
        production_root: Path = PRODUCTION_ROOT,
        md_unit_path: Path = MD_UNIT_PATH,
        dft_unit_path: Path = DFT_UNIT_PATH,
        checkpoint: Callable[[str], None] | None = None,
        source_readiness_probe: Callable[[Path, str], dict[str, object]]
        | None = None,
        delivery_gate_probe: Callable[
            [Path, Path, str, dict[str, object] | None], dict[str, object]
        ]
        | None = None,
        systemd_probe: Callable[[str, Path], dict[str, str]] | None = None,
        daemon_reload: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            source_root,
            runtime_root,
            production_root=production_root,
            checkpoint=checkpoint,
            source_readiness_probe=source_readiness_probe,
            delivery_gate_probe=delivery_gate_probe,
        )
        self.md_unit_path = md_unit_path.absolute()
        self.dft_unit_path = dft_unit_path.absolute()
        if self.md_unit_path.parent != self.dft_unit_path.parent:
            raise PrerequisiteError(
                "adopted Worker units must share one private systemd directory"
            )
        self.unit_parent = self.md_unit_path.parent
        self.unit_transaction_root = (
            self.runtime_root / UNIT_PERMISSION_TRANSACTION_DIRECTORY
        )
        self.unit_backup_root = (
            self.runtime_root / UNIT_PERMISSION_BACKUP_DIRECTORY
        )
        self.unit_authority_path = (
            self.runtime_root / UNIT_PERMISSION_AUTHORITY_PATH
        )
        self.systemd_probe = systemd_probe or self._live_systemd_probe
        self.daemon_reload = daemon_reload or self._live_daemon_reload
        self._pinned_unit_parent: tuple[
            int, tuple[int, int, int, int, int, int]
        ] | None = None

    @staticmethod
    def _unit_parent_identity(
        metadata: os.stat_result,
    ) -> tuple[int, int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
        )

    @contextlib.contextmanager
    def _deployment_lock(self) -> Any:
        with super()._deployment_lock():
            if self._pinned_unit_parent is not None:
                raise PrerequisiteError("Worker unit directory is already pinned")
            descriptor, _identity_value = _open_owned_directory_for_cas(
                self.unit_parent
            )
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_mode & 0o022:
                    raise PrerequisiteError(
                        "deploy-user systemd unit directory is unsafe"
                    )
                self._pinned_unit_parent = (
                    descriptor,
                    self._unit_parent_identity(metadata),
                )
                self._assert_unit_paths_pinned()
                yield
                self._assert_unit_paths_pinned()
            finally:
                self._pinned_unit_parent = None
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _assert_unit_parent_pinned(self) -> None:
        if self._pinned_unit_parent is None:
            raise PrerequisiteError("Worker unit directory is not pinned")
        descriptor, expected = self._pinned_unit_parent
        try:
            observed_descriptor = os.fstat(descriptor)
            observed_path = self.unit_parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise PrerequisiteError("Worker unit directory changed") from exc
        if (
            not stat.S_ISDIR(observed_descriptor.st_mode)
            or not stat.S_ISDIR(observed_path.st_mode)
            or self.unit_parent.is_symlink()
            or self._unit_parent_identity(observed_descriptor) != expected
            or self._unit_parent_identity(observed_path) != expected
            or observed_descriptor.st_mode & 0o022
        ):
            raise PrerequisiteError("Worker unit directory changed")

    def _assert_unit_paths_pinned(self) -> None:
        self._assert_permission_paths_pinned()
        self._assert_unit_parent_pinned()

    def _unit_parent_fd(self) -> int | None:
        return (
            self._pinned_unit_parent[0]
            if self._pinned_unit_parent is not None
            else None
        )

    @staticmethod
    def _systemd_environment() -> dict[str, str]:
        uid = os.geteuid()
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/devuser",
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def _live_systemd_probe(self, name: str, _path: Path) -> dict[str, str]:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                name,
                "--property=LoadState",
                "--property=FragmentPath",
                "--property=DropInPaths",
                "--property=NeedDaemonReload",
                "--property=UnitFileState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=InvocationID",
            ],
            env=self._systemd_environment(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return {
            key: value
            for key, value in (
                line.split("=", 1)
                for line in result.stdout.splitlines()
                if "=" in line
            )
        }

    def _live_daemon_reload(self) -> None:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            env=self._systemd_environment(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _normalized_systemd_identity(
        self,
        *,
        name: str,
        path: Path,
        allow_reload_pending: bool = False,
    ) -> tuple[dict[str, str], dict[str, object]]:
        raw = self.systemd_probe(name, path)
        fields = {
            "LoadState",
            "FragmentPath",
            "DropInPaths",
            "NeedDaemonReload",
            "UnitFileState",
            "ActiveState",
            "SubState",
            "MainPID",
            "InvocationID",
        }
        try:
            main_pid = int(raw.get("MainPID", ""))
        except (TypeError, ValueError) as exc:
            raise PrerequisiteError(
                f"{name} process identity is malformed"
            ) from exc
        invocation_id = raw.get("InvocationID")
        if (
            set(raw) != fields
            or raw.get("LoadState") != "loaded"
            or raw.get("FragmentPath") != str(path)
            or raw.get("DropInPaths") != ""
            or raw.get("NeedDaemonReload")
            not in ({"no", "yes"} if allow_reload_pending else {"no"})
            or raw.get("UnitFileState") != "enabled"
            or raw.get("ActiveState") != "active"
            or raw.get("SubState") != "running"
            or main_pid <= 0
            or not isinstance(invocation_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
        ):
            raise PrerequisiteError(
                f"{name} is not the unchanged active systemd instance"
            )
        return (
            {
                key: str(raw[key])
                for key in (
                    "LoadState",
                    "FragmentPath",
                    "DropInPaths",
                    "NeedDaemonReload",
                    "UnitFileState",
                    "ActiveState",
                    "SubState",
                )
            },
            {"main_pid": main_pid, "invocation_id": invocation_id},
        )

    def _parent_record(self, descriptor: int) -> dict[str, object]:
        metadata = os.fstat(descriptor)
        observed = self.unit_parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or self.unit_parent.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or self._unit_parent_identity(metadata)
            != self._unit_parent_identity(observed)
        ):
            raise PrerequisiteError(
                "deploy-user systemd unit directory is unsafe"
            )
        return {
            "path": str(self.unit_parent),
            "type": "directory",
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "nlink": metadata.st_nlink,
            "size": metadata.st_size,
        }

    def _read_unit_file(
        self,
        *,
        parent_fd: int,
        path: Path,
        mode: int,
        allowed_nlinks: frozenset[int] = frozenset({1}),
    ) -> tuple[dict[str, object], bytes]:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise PrerequisiteError(
                f"adopted Worker unit is unavailable: {path}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            payload = _descriptor_bytes(
                descriptor,
                maximum_bytes=1024 * 1024,
            )
            after = os.fstat(descriptor)
            observed = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != mode
                or before.st_nlink not in allowed_nlinks
                or not 1 <= before.st_size <= 1024 * 1024
                or _stable_regular_identity(before)
                != _stable_regular_identity(after)
                or _stable_regular_identity(before)
                != _stable_regular_identity(observed)
            ):
                raise PrerequisiteError(
                    f"adopted Worker unit identity is unsafe: {path}"
                )
            return (
                {
                    "type": "file",
                    "device": before.st_dev,
                    "inode": before.st_ino,
                    "uid": before.st_uid,
                    "gid": before.st_gid,
                    "mode": f"{mode:04o}",
                    "nlink": before.st_nlink,
                    "size": before.st_size,
                    "content_sha256": _digest(payload),
                },
                payload,
            )
        finally:
            os.close(descriptor)

    def _adopted_unit_bindings(
        self,
        *,
        expected_adopted_digest: str,
    ) -> dict[str, dict[str, object]]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        adopted, adopted_digest = (
            _load_json_with_digest(
                self.runtime_root / ADOPTED_DEPLOYMENT_PATH
            )
            if state_fd is None
            else _load_json_with_digest_at(
                state_fd,
                ADOPTED_DEPLOYMENT_PATH.name,
            )
        )
        if adopted_digest != expected_adopted_digest:
            raise PrerequisiteError("adopted unit authority changed")
        bindings: dict[str, dict[str, object]] = {}
        for role, key, path in (
            ("monomer-md", "monomer_md", self.md_unit_path),
            ("monomer-dft", "monomer_dft", self.dft_unit_path),
        ):
            component = adopted.get(key)
            unit = (
                component.get("systemd_unit")
                if isinstance(component, dict)
                else None
            )
            if (
                not isinstance(unit, dict)
                or unit.get("target_path") != str(path)
                or not isinstance(unit.get("sha256"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", unit["sha256"])
                is None
                or not isinstance(unit.get("systemd_state"), dict)
                or not isinstance(unit.get("process_identity"), dict)
            ):
                raise PrerequisiteError(
                    f"adopted {role} unit authority is invalid"
                )
            bindings[role] = {
                "target_path": str(path),
                "sha256": str(unit["sha256"]),
                "systemd_state": dict(unit["systemd_state"]),
                "process_identity": dict(unit["process_identity"]),
            }
        return bindings

    def _capture_units(
        self,
        *,
        md_mode: int,
        adopted_digest: str,
        allow_reload_pending: bool = False,
    ) -> tuple[list[dict[str, object]], dict[str, bytes]]:
        owned_fd = self._unit_parent_fd()
        close_parent = owned_fd is None
        parent_fd = (
            _open_private_directory(self.unit_parent)
            if owned_fd is None
            else owned_fd
        )
        try:
            parent = self._parent_record(parent_fd)
            bindings = self._adopted_unit_bindings(
                expected_adopted_digest=adopted_digest
            )
            records: list[dict[str, object]] = []
            payloads: dict[str, bytes] = {}
            for role, name, path, mode, action in (
                (
                    "monomer-md",
                    MD_UNIT_NAME,
                    self.md_unit_path,
                    md_mode,
                    "atomic-inode-replace",
                ),
                (
                    "monomer-dft",
                    DFT_UNIT_NAME,
                    self.dft_unit_path,
                    0o600,
                    "no-op-cas",
                ),
            ):
                before_systemd = self._normalized_systemd_identity(
                    name=name,
                    path=path,
                    allow_reload_pending=allow_reload_pending,
                )
                file_record, payload = self._read_unit_file(
                    parent_fd=parent_fd,
                    path=path,
                    mode=mode,
                )
                after_systemd = self._normalized_systemd_identity(
                    name=name,
                    path=path,
                    allow_reload_pending=allow_reload_pending,
                )
                if before_systemd != after_systemd:
                    raise PrerequisiteError(
                        f"{role} systemd identity changed while reading"
                    )
                systemd_state, process_identity = before_systemd
                adopted_systemd = bindings[role]["systemd_state"]
                systemd_matches = adopted_systemd == systemd_state
                if (
                    allow_reload_pending
                    and isinstance(adopted_systemd, dict)
                    and adopted_systemd.get("NeedDaemonReload") == "no"
                ):
                    systemd_matches = {
                        key: value
                        for key, value in adopted_systemd.items()
                        if key != "NeedDaemonReload"
                    } == {
                        key: value
                        for key, value in systemd_state.items()
                        if key != "NeedDaemonReload"
                    }
                if (
                    bindings[role]["target_path"] != str(path)
                    or bindings[role]["sha256"]
                    != file_record["content_sha256"]
                    or not systemd_matches
                    or bindings[role]["process_identity"]
                    != process_identity
                ):
                    raise PrerequisiteError(
                        f"{role} unit differs from manual adoption"
                    )
                records.append(
                    {
                        "role": role,
                        "name": name,
                        "path": str(path),
                        "parent": dict(parent),
                        **file_record,
                        "target_mode": "0600",
                        "action": action,
                        "systemd_state": systemd_state,
                        "process_identity": process_identity,
                    }
                )
                payloads[role] = payload
            return records, payloads
        finally:
            if close_parent:
                os.close(parent_fd)

    def _read_git_permission_authority(
        self,
    ) -> tuple[dict[str, object], str, dict[str, str]]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        authority, authority_digest = (
            _load_json_with_digest(self.permission_authority_path)
            if state_fd is None
            else _load_json_with_digest_at(
                state_fd,
                PERMISSION_AUTHORITY_PATH.name,
                maximum_bytes=PERMISSION_JSON_MAX_BYTES,
            )
        )
        journal = self._read_git_permission_completed_journal(
            authority,
            state_fd=state_fd,
        )
        return authority, authority_digest, journal

    def _read_git_permission_completed_journal(
        self,
        authority: dict[str, object],
        *,
        state_fd: int | None,
    ) -> dict[str, str]:
        """Bind the root wrapper to its one canonical completed journal."""

        operation_id = str(authority.get("operation_id", ""))
        _require_permission_operation_id(operation_id)
        close_state = False
        if state_fd is None:
            state_fd = _open_private_directory(self.runtime_root / "state")
            close_state = True
        try:
            transaction_fd = _open_private_directory(
                Path(PERMISSION_TRANSACTION_DIRECTORY.name),
                parent_fd=state_fd,
            )
            try:
                name = f"{operation_id}.json"
                if sorted(os.listdir(transaction_fd)) != [name]:
                    raise PrerequisiteError(
                        "adopted Git permission completed journal lineage "
                        "is incomplete"
                    )
                journal, journal_digest = _load_json_with_digest_at(
                    transaction_fd,
                    name,
                    maximum_bytes=PERMISSION_JSON_MAX_BYTES,
                )
            finally:
                os.close(transaction_fd)
        finally:
            if close_state:
                os.close(state_fd)
        fields = {
            "schema_version",
            "status",
            "phase",
            "operation_id",
            "plan",
            "plan_sha256",
            "permission_impact_sha256",
            "permission_checkpoint",
            "permission_marker_sha256",
            "permission_evidence_sha256",
            "source_trust_sha256",
            "created_at",
            "completed_at",
            "aborted_at",
        }
        source_trust = journal.get("source_trust_sha256")
        timestamp_pattern = (
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
            r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
        )
        if (
            set(journal) != fields
            or journal_digest
            != _digest(_canonical_bytes(journal) + b"\n")
            or not _has_exact_schema_version(journal, 1)
            or journal.get("status") != "completed"
            or journal.get("phase") != "completed"
            or journal.get("operation_id") != operation_id
            or journal.get("plan") != authority.get("plan")
            or journal.get("plan_sha256") != authority.get("plan_sha256")
            or journal.get("permission_impact_sha256")
            != authority.get("permission_impact_sha256")
            or journal.get("permission_checkpoint") != "permission:hardened"
            or journal.get("permission_marker_sha256")
            != authority.get("permission_marker_sha256")
            or journal.get("permission_evidence_sha256")
            != authority.get("permission_evidence_sha256")
            or not isinstance(source_trust, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", source_trust) is None
            or not isinstance(journal.get("created_at"), str)
            or re.fullmatch(timestamp_pattern, journal["created_at"]) is None
            or journal.get("completed_at") != authority.get("completed_at")
            or not isinstance(journal.get("completed_at"), str)
            or re.fullmatch(timestamp_pattern, journal["completed_at"])
            is None
            or journal.get("aborted_at") is not None
        ):
            raise PrerequisiteError(
                "adopted Git permission completed journal differs"
            )
        return {
            "completed_journal_sha256": journal_digest,
            "source_trust_sha256": source_trust,
        }

    def _source_successor_lineage_entries(self) -> list[str]:
        """Return every fixed-path successor sentinel, including residue."""

        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        close_state = False
        if state_fd is None:
            state_fd = _open_private_directory(self.runtime_root / "state")
            close_state = True
        try:
            prefix = f".{SOURCE_SUCCESSOR_AUTHORITY_PATH.name}.create-"
            transaction_prefix = (
                f".{SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY.name}.create-"
            )
            entries = [
                name
                for name in os.listdir(state_fd)
                if name == SOURCE_SUCCESSOR_AUTHORITY_PATH.name
                or name.startswith(prefix)
                or name.startswith(transaction_prefix)
                or name == SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY.name
            ]
            return sorted(entries)
        finally:
            if close_state:
                os.close(state_fd)

    @staticmethod
    def _source_successor_publication_plan(
        operation_id: str,
        state_root: Path,
    ) -> dict[str, object]:
        final = SOURCE_SUCCESSOR_AUTHORITY_PATH.name
        staging = f".{final}.create-{operation_id}"
        quarantine = f"{staging}.quarantine"
        return {
            "schema_version": 1,
            "policy": SOURCE_SUCCESSOR_PUBLICATION_POLICY,
            "directory": str(state_root),
            "entries": [
                {
                    "role": "final",
                    "name": final,
                    "path": str(state_root / final),
                    "initially_absent": True,
                },
                {
                    "role": "staging",
                    "name": staging,
                    "path": str(state_root / staging),
                    "initially_absent": True,
                },
                {
                    "role": "staging-quarantine",
                    "name": quarantine,
                    "path": str(state_root / quarantine),
                    "initially_absent": True,
                },
            ],
        }

    @staticmethod
    def _source_successor_file_records(
        value: object,
    ) -> list[dict[str, object]]:
        if not isinstance(value, list) or len(value) != len(
            UNIT_PERMISSION_SUCCESSOR_V2_BLOBS
        ):
            raise PrerequisiteError(
                "source successor fixed file manifest is invalid"
            )
        records: list[dict[str, object]] = []
        changed: list[str] = []
        for record, expected_path in zip(
            value,
            UNIT_PERMISSION_SUCCESSOR_V2_BLOBS,
            strict=True,
        ):
            if (
                not isinstance(record, dict)
                or set(record)
                != {"path", "relation", "predecessor", "target"}
                or record.get("path") != expected_path
                or record.get("relation")
                not in {"byte-identical", "changed"}
            ):
                raise PrerequisiteError(
                    "source successor fixed file manifest differs"
                )
            identities: dict[str, dict[str, str]] = {}
            for label in ("predecessor", "target"):
                identity = record.get(label)
                if (
                    not isinstance(identity, dict)
                    or set(identity)
                    != {"object_type", "mode", "blob_sha", "sha256"}
                    or identity.get("object_type") != "blob"
                    or identity.get("mode") not in {"100644", "100755"}
                    or not isinstance(identity.get("blob_sha"), str)
                    or SHA_RE.fullmatch(identity["blob_sha"]) is None
                    or not isinstance(identity.get("sha256"), str)
                    or re.fullmatch(
                        r"sha256:[0-9a-f]{64}",
                        identity["sha256"],
                    )
                    is None
                ):
                    raise PrerequisiteError(
                        "source successor Git blob identity is invalid"
                    )
                identities[label] = dict(identity)
            same = identities["predecessor"] == identities["target"]
            expected_mode = (
                "100644"
                if expected_path
                == "ops/config/mutable-data-audit.pg_service.conf.example"
                else "100755"
            )
            if (
                same != (record["relation"] == "byte-identical")
                or identities["predecessor"]["mode"] != expected_mode
                or identities["target"]["mode"] != expected_mode
            ):
                raise PrerequisiteError(
                    "source successor blob relation is inconsistent"
                )
            if record["relation"] == "changed":
                changed.append(expected_path)
            records.append(
                {
                    "path": expected_path,
                    "relation": record["relation"],
                    "predecessor": identities["predecessor"],
                    "target": identities["target"],
                }
            )
        if tuple(changed) != SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS:
            raise PrerequisiteError(
                "source successor changed paths differ from authorization"
            )
        return records

    @staticmethod
    def _valid_source_successor_ref_name(value: str) -> bool:
        if (
            not value.startswith("refs/")
            or len(value) > 1024
            or value.endswith(("/", "."))
            or "//" in value
            or "@{" in value
            or any(character in value for character in " ~^:?*[\\\x00\n\r")
        ):
            return False
        components = value.split("/")
        return all(
            component
            and component not in {".", "..", "@"}
            and not component.startswith(".")
            and not component.endswith(".lock")
            for component in components
        )

    @classmethod
    def _valid_source_successor_ref_directory(cls, value: str) -> bool:
        return value == "refs" or cls._valid_source_successor_ref_name(
            f"{value}/sentinel"
        )

    def _validate_source_successor_repository_transition(
        self,
        value: object,
        *,
        production_sha: str,
        production_tree: str,
        target_sha: str,
        target_tree: str,
        baseline_trust_sha256: str,
    ) -> dict[str, object]:
        """Validate the immutable pre-materialization repository baseline."""

        fields = {
            "schema_version",
            "policy",
            "source",
            "target",
            "baseline_evidence_sha256",
            "stable_projection",
            "stable_projection_sha256",
            "logical_refs",
            "logical_refs_sha256",
            "raw_ref_inventory",
            "raw_ref_inventory_sha256",
            "baseline_auxiliary_inventory",
            "baseline_auxiliary_inventory_sha256",
            "baseline_semantic_object_count",
            "baseline_semantic_objects_sha256",
            "baseline_only_object_count",
            "baseline_only_objects_sha256",
            "target_reachable_object_count",
            "target_reachable_objects_sha256",
            "expected_materialized_object_count",
            "expected_materialized_objects_sha256",
            "mutable_refs",
            "storage_policy",
            "auxiliary_policy",
            "object_storage_policy",
            "object_materialization_policy",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or not _has_exact_schema_version(value, 1)
            or value.get("policy")
            != SOURCE_SUCCESSOR_REPOSITORY_TRANSITION_POLICY
            or value.get("source")
            != {"sha": production_sha, "tree": production_tree}
            or value.get("target")
            != {"sha": target_sha, "tree": target_tree}
            or value.get("baseline_evidence_sha256")
            != baseline_trust_sha256
            or value.get("mutable_refs")
            != {
                "deploy_remote": SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF,
                "prepared_prefix": SOURCE_SUCCESSOR_PREPARED_REF_PREFIX,
            }
            or value.get("storage_policy")
            != {
                "standalone": True,
                "promisor": False,
                "alternates": False,
                "replace_refs": 0,
            }
            or value.get("object_materialization_policy")
            != (
                "strict-fsck-owner-private-content-addressed-target-closure-v1"
            )
            or value.get("auxiliary_policy")
            != SOURCE_SUCCESSOR_GIT_AUXILIARY_POLICY
            or value.get("object_storage_policy")
            != SOURCE_SUCCESSOR_GIT_OBJECT_STORAGE_POLICY
        ):
            raise PrerequisiteError(
                "source successor repository transition is invalid"
            )
        stable = value.get("stable_projection")
        stable_fields = {
            "schema_version",
            "policy",
            "repository_root",
            "git_dir",
            "object_dir",
            "index_path",
            "source",
            "git_binary",
            "local_config",
            "head",
            "index",
            "forbidden_markers_absent",
            "execution_environment",
        }
        if (
            not isinstance(stable, dict)
            or set(stable) != stable_fields
            or stable.get("repository_root") != str(self.production_root)
            or stable.get("git_dir") != str(self.production_root / ".git")
            or stable.get("object_dir")
            != str(self.production_root / ".git/objects")
            or stable.get("index_path")
            != str(self.production_root / ".git/index")
            or stable.get("source")
            != {
                "sha": production_sha,
                "tree": production_tree,
                "branch": "refs/heads/main",
                "origin": None,
            }
            or value.get("stable_projection_sha256")
            != _canonical_digest(stable)
        ):
            raise PrerequisiteError(
                "source successor repository stable projection differs"
            )
        logical = value.get("logical_refs")
        logical_names: list[str] = []
        if not isinstance(logical, list):
            raise PrerequisiteError(
                "source successor logical ref baseline is invalid"
            )
        for record in logical:
            if (
                not isinstance(record, dict)
                or set(record)
                != {
                    "name",
                    "object_sha",
                    "object_type",
                    "symbolic_target",
                }
                or not isinstance(record.get("name"), str)
                or not self._valid_source_successor_ref_name(record["name"])
                or record["name"].startswith("refs/replace/")
                or record.get("object_type")
                not in {"blob", "tree", "commit", "tag"}
                or record.get("symbolic_target") is not None
                and (
                    not isinstance(record.get("symbolic_target"), str)
                    or not self._valid_source_successor_ref_name(
                        str(record["symbolic_target"])
                    )
                )
            ):
                raise PrerequisiteError(
                    "source successor logical ref record is invalid"
                )
            object_sha = record.get("object_sha")
            if not isinstance(object_sha, str) or SHA_RE.fullmatch(
                object_sha
            ) is None:
                raise PrerequisiteError(
                    "source successor logical ref object is invalid"
                )
            logical_names.append(record["name"])
        logical_map = {
            record["name"]: record["object_sha"] for record in logical
        }
        logical_records = {
            record["name"]: record for record in logical
        }
        if (
            logical_names != sorted(set(logical_names))
            or len(logical_names) > 10_000
            or any(
                name.startswith(SOURCE_SUCCESSOR_PREPARED_REF_PREFIX)
                for name in logical_names
            )
            or logical_map.get("refs/heads/main") != production_sha
            or logical_records.get("refs/heads/main", {}).get(
                "object_type"
            )
            != "commit"
            or logical_records.get("refs/heads/main", {}).get(
                "symbolic_target"
            )
            is not None
            or logical_records.get(
                SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF, {}
            ).get("object_type")
            != "commit"
            or logical_records.get(
                SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF, {}
            ).get("symbolic_target")
            is not None
            or value.get("logical_refs_sha256")
            != _canonical_digest(logical)
        ):
            raise PrerequisiteError(
                "source successor logical ref baseline differs"
            )
        raw = value.get("raw_ref_inventory")
        paths: list[str] = []
        if not isinstance(raw, list):
            raise PrerequisiteError(
                "source successor raw ref baseline is invalid"
            )
        for record in raw:
            if not isinstance(record, dict) or not isinstance(
                record.get("path"), str
            ):
                raise PrerequisiteError(
                    "source successor raw ref record is invalid"
                )
            path = record["path"]
            paths.append(path)
            if record.get("kind") == "directory":
                if (
                    set(record) != {"path", "kind", "mode"}
                    or not self._valid_source_successor_ref_directory(path)
                    or record.get("mode") != "0700"
                ):
                    raise PrerequisiteError(
                        "source successor raw ref directory is invalid"
                    )
            elif record.get("kind") == "file":
                digest = record.get("raw_sha256")
                if (
                    set(record)
                    != {"path", "kind", "mode", "size", "raw_sha256"}
                    or record.get("mode") != "0600"
                    or isinstance(record.get("size"), bool)
                    or not isinstance(record.get("size"), int)
                    or not 0 <= record["size"] <= SOURCE_SUCCESSOR_JSON_MAX_BYTES
                    or not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                    or path != "packed-refs"
                    and (
                        not self._valid_source_successor_ref_name(path)
                        or path not in logical_names
                    )
                ):
                    raise PrerequisiteError(
                        "source successor raw ref file is invalid"
                    )
            else:
                raise PrerequisiteError(
                    "source successor raw ref kind is invalid"
                )
        auxiliary = value.get("baseline_auxiliary_inventory")
        if (
            not isinstance(auxiliary, list)
            or value.get("baseline_auxiliary_inventory_sha256")
            != _canonical_digest(auxiliary)
        ):
            raise PrerequisiteError(
                "source successor Git auxiliary baseline differs"
            )
        auxiliary_paths: list[str] = []
        for record in auxiliary:
            if not isinstance(record, dict) or not isinstance(
                record.get("path"), str
            ):
                raise PrerequisiteError(
                    "source successor Git auxiliary record is invalid"
                )
            path = record["path"]
            auxiliary_paths.append(path)
            if (
                path.startswith(("objects/", "refs/"))
                or path
                in {
                    "objects",
                    "refs",
                    "HEAD",
                    "config",
                    "index",
                    "packed-refs",
                }
                or path.endswith(".lock")
                or path.startswith(
                    f"logs/{SOURCE_SUCCESSOR_PREPARED_REF_PREFIX}"
                )
                or Path(path).is_absolute()
                or ".." in Path(path).parts
                or Path(path).as_posix() != path
            ):
                raise PrerequisiteError(
                    "source successor Git auxiliary path is invalid"
                )
            if record.get("kind") == "directory":
                if (
                    set(record) != {"path", "kind", "mode"}
                    or not isinstance(record.get("mode"), str)
                    or re.fullmatch(r"0[4-7]00", record["mode"]) is None
                ):
                    raise PrerequisiteError(
                        "source successor Git auxiliary directory is invalid"
                    )
            elif record.get("kind") == "file":
                digest = record.get("raw_sha256")
                if (
                    set(record)
                    != {"path", "kind", "mode", "size", "raw_sha256"}
                    or not isinstance(record.get("mode"), str)
                    or re.fullmatch(r"0[4-7]00", record["mode"]) is None
                    or isinstance(record.get("size"), bool)
                    or not isinstance(record.get("size"), int)
                    or not 0 <= record["size"] <= PERMISSION_JSON_MAX_BYTES
                    or not isinstance(digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                ):
                    raise PrerequisiteError(
                        "source successor Git auxiliary file is invalid"
                    )
            else:
                raise PrerequisiteError(
                    "source successor Git auxiliary record is invalid"
                )
        counts: dict[str, int] = {}
        for field, allow_zero in (
            ("baseline_semantic_object_count", True),
            ("baseline_only_object_count", True),
            ("target_reachable_object_count", False),
            ("expected_materialized_object_count", False),
        ):
            count = value.get(field)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < (0 if allow_zero else 1)
                or count > 10_000_000
            ):
                raise PrerequisiteError(
                    f"source successor {field} is invalid"
                )
            counts[field] = count
        for field in (
            "baseline_semantic_objects_sha256",
            "baseline_only_objects_sha256",
            "target_reachable_objects_sha256",
            "expected_materialized_objects_sha256",
        ):
            digest = value.get(field)
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            ):
                raise PrerequisiteError(
                    f"source successor {field} is invalid"
                )
        if (
            paths != sorted(set(paths))
            or "refs" not in paths
            or value.get("raw_ref_inventory_sha256")
            != _canonical_digest(raw)
            or auxiliary_paths != sorted(set(auxiliary_paths))
            or counts["expected_materialized_object_count"]
            != counts["baseline_only_object_count"]
            + counts["target_reachable_object_count"]
            or counts["expected_materialized_object_count"]
            < counts["baseline_semantic_object_count"]
        ):
            raise PrerequisiteError(
                "source successor repository transition baseline differs"
            )
        return dict(value)

    def _validate_source_successor_authority(
        self,
        document: dict[str, object],
        raw_digest: str,
        *,
        root_authority: dict[str, object],
        root_digest: str,
        root_journal: dict[str, str],
        target_sha: str,
        target_tree: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        authority_fields = {
            "schema_version",
            "status",
            "authority_kind",
            "policy",
            "operation_id",
            "source_sha",
            "source_tree",
            "predecessor_source_sha",
            "predecessor_source_tree",
            "predecessor_authority_sha256",
            "predecessor_marker_sha256",
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "plan_sha256",
            "source_successor_impact_sha256",
            "files_sha256",
            "changed_paths",
            "changed_paths_sha256",
            "delivery_gate",
            "delivery_gate_sha256",
            "verifier_agreement_sha256",
            "production_source_trust_sha256",
            "production_repository_transition_sha256",
            "plan",
            "completed_at",
        }
        plan_fields = {
            "schema_version",
            "authority_kind",
            "policy",
            "operation_id",
            "source_sha",
            "source_tree",
            "source_readiness",
            "source_readiness_sha256",
            "delivery_gate",
            "delivery_gate_sha256",
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "production_source_trust_sha256",
            "production_repository_transition",
            "production_repository_transition_sha256",
            "production_source",
            "predecessor",
            "marker",
            "verifier_agreement",
            "files",
            "files_sha256",
            "changed_paths",
            "changed_paths_sha256",
            "authority_publication",
            "source_successor_impact",
            "source_successor_impact_sha256",
            "mutations",
        }
        plan = document.get("plan")
        operation_id = str(document.get("operation_id", ""))
        if (
            set(document) != authority_fields
            or raw_digest
            != _digest(_canonical_bytes(document) + b"\n")
            or not _has_exact_schema_version(document, 1)
            or document.get("status") != "completed"
            or document.get("authority_kind")
            != SOURCE_SUCCESSOR_AUTHORITY_KIND
            or document.get("policy") != SOURCE_SUCCESSOR_POLICY
            or SOURCE_SUCCESSOR_OPERATION_RE.fullmatch(operation_id) is None
            or not isinstance(plan, dict)
            or set(plan) != plan_fields
            or not _has_exact_schema_version(plan, 1)
            or document.get("plan_sha256") != _canonical_digest(plan)
            or plan.get("operation_id") != operation_id
            or plan.get("authority_kind")
            != SOURCE_SUCCESSOR_AUTHORITY_KIND
            or plan.get("policy") != SOURCE_SUCCESSOR_POLICY
        ):
            raise PrerequisiteError(
                "source successor authority has an invalid shape"
            )
        if _parse_canonical_utc_timestamp(document.get("completed_at")) is None:
            raise PrerequisiteError(
                "source successor authority completion time is invalid"
            )
        for field in (
            "source_sha",
            "source_tree",
            "predecessor_source_sha",
            "predecessor_source_tree",
        ):
            value = document.get(field)
            if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
                raise PrerequisiteError(
                    "source successor authority identity is invalid"
                )
        for field in (
            "predecessor_authority_sha256",
            "predecessor_marker_sha256",
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "plan_sha256",
            "source_successor_impact_sha256",
            "files_sha256",
            "changed_paths_sha256",
            "delivery_gate_sha256",
            "verifier_agreement_sha256",
            "production_source_trust_sha256",
            "production_repository_transition_sha256",
        ):
            value = document.get(field)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
            ):
                raise PrerequisiteError(
                    "source successor authority digest is invalid"
                )
        predecessor = plan.get("predecessor")
        marker = plan.get("marker")
        production = plan.get("production_source")
        delivery = plan.get("delivery_gate")
        ci = delivery.get("ci") if isinstance(delivery, dict) else None
        changed_paths = plan.get("changed_paths")
        files = self._source_successor_file_records(plan.get("files"))
        if (
            not isinstance(delivery, dict)
            or set(delivery) != {"remote_main", "ci"}
            or delivery.get("remote_main") != target_sha
            or not isinstance(ci, dict)
            or set(ci)
            != {
                "workflow_run_id",
                "run_attempt",
                "head_sha",
                "head_branch",
                "event",
                "path",
                "conclusion",
                "required_jobs",
            }
            or type(ci.get("workflow_run_id")) is not int
            or ci["workflow_run_id"] <= 0
            or type(ci.get("run_attempt")) is not int
            or ci["run_attempt"] <= 0
            or ci.get("head_sha") != target_sha
            or ci.get("head_branch") != "main"
            or ci.get("event") != "push"
            or ci.get("path") != ".github/workflows/ci.yml"
            or ci.get("conclusion") != "success"
            or not isinstance(ci.get("required_jobs"), list)
            or not ci["required_jobs"]
            or ci["required_jobs"] != sorted(set(ci["required_jobs"]))
            or any(
                not isinstance(name, str) or not name
                for name in ci["required_jobs"]
            )
        ):
            raise PrerequisiteError(
                "source successor delivery authority is invalid"
            )
        publication = self._source_successor_publication_plan(
            operation_id,
            self.runtime_root / "state",
        )
        repository_transition = (
            self._validate_source_successor_repository_transition(
                plan.get("production_repository_transition"),
                production_sha=str(
                    root_authority.get("production_source_sha", "")
                ),
                production_tree=str(
                    root_authority.get("production_source_tree", "")
                ),
                target_sha=target_sha,
                target_tree=target_tree,
                baseline_trust_sha256=str(
                    plan.get("production_source_trust_sha256", "")
                ),
            )
        )
        transition_logical_refs = {
            str(record["name"]): str(record["object_sha"])
            for record in repository_transition["logical_refs"]
        }
        expected_predecessor = {
            "authority_kind": root_authority.get("authority_kind"),
            "operation_id": root_authority.get("operation_id"),
            "source_sha": root_authority.get("source_sha"),
            "source_tree": root_authority.get("source_tree"),
            "authority_sha256": root_digest,
            "plan_sha256": root_authority.get("plan_sha256"),
            "permission_marker_sha256": root_authority.get(
                "permission_marker_sha256"
            ),
            "permission_evidence_sha256": root_authority.get(
                "permission_evidence_sha256"
            ),
            "permission_inventory_sha256": root_authority.get(
                "permission_inventory_sha256"
            ),
            "original_permissions_sha256": root_authority.get(
                "original_permissions_sha256"
            ),
            "hardened_permissions_sha256": root_authority.get(
                "hardened_permissions_sha256"
            ),
            "completed_journal_sha256": root_journal.get(
                "completed_journal_sha256"
            ),
            "source_trust_sha256": root_journal.get(
                "source_trust_sha256"
            ),
        }
        expected_marker = {
            "path": str(self.permission_marker_path),
            "raw_sha256": root_authority.get("permission_marker_sha256"),
            "evidence_sha256": root_authority.get(
                "permission_evidence_sha256"
            ),
            "inventory_sha256": root_authority.get(
                "permission_inventory_sha256"
            ),
            "original_permissions_sha256": root_authority.get(
                "original_permissions_sha256"
            ),
            "hardened_permissions_sha256": root_authority.get(
                "hardened_permissions_sha256"
            ),
        }
        records_by_path = {str(record["path"]): record for record in files}
        expected_verifier = {
            "schema_version": 1,
            "policy": SOURCE_SUCCESSOR_VERIFIER_POLICY,
            "candidate_execution": "forbidden-before-authority",
            "predecessor_source_sha": root_authority.get("source_sha"),
            "predecessor_source_tree": root_authority.get("source_tree"),
            "bootstrap": records_by_path["scripts/bootstrap_pull_deploy.py"],
            "git_source_trust": records_by_path["scripts/git_source_trust.py"],
            "ci_contract": records_by_path["scripts/bridge_deploy_core.py"],
            "required_jobs": ci["required_jobs"],
            "required_jobs_sha256": _canonical_digest(ci["required_jobs"]),
        }
        expected_impact = {
            "schema_version": 1,
            "policy": SOURCE_SUCCESSOR_IMPACT_POLICY,
            "predecessor_authority_sha256": root_digest,
            "predecessor_marker_sha256": root_authority.get(
                "permission_marker_sha256"
            ),
            "production_source_trust_sha256": plan.get(
                "production_source_trust_sha256"
            ),
            "production_repository_transition_sha256": plan.get(
                "production_repository_transition_sha256"
            ),
            "target": {
                "source_sha": target_sha,
                "source_tree": target_tree,
            },
            "files": files,
            "files_sha256": _canonical_digest(files),
            "changed_paths": list(SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS),
            "changed_paths_sha256": _canonical_digest(
                list(SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS)
            ),
            "authority_publication": publication,
            "mutations": SOURCE_SUCCESSOR_MUTATIONS,
        }
        if (
            predecessor != expected_predecessor
            or marker != expected_marker
            or document.get("source_sha") != target_sha
            or document.get("source_tree") != target_tree
            or document.get("predecessor_source_sha")
            != root_authority.get("source_sha")
            or document.get("predecessor_source_tree")
            != root_authority.get("source_tree")
            or document.get("predecessor_authority_sha256") != root_digest
            or document.get("predecessor_marker_sha256")
            != root_authority.get("permission_marker_sha256")
            or plan.get("source_sha") != target_sha
            or plan.get("source_tree") != target_tree
            or plan.get("files_sha256") != _canonical_digest(files)
            or document.get("files_sha256") != plan.get("files_sha256")
            or changed_paths != list(SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS)
            or plan.get("changed_paths_sha256")
            != _canonical_digest(changed_paths)
            or document.get("changed_paths") != changed_paths
            or document.get("changed_paths_sha256")
            != plan.get("changed_paths_sha256")
            or not isinstance(delivery, dict)
            or set(delivery) != {"remote_main", "ci"}
            or delivery.get("remote_main") != target_sha
            or not isinstance(ci, dict)
            or set(ci)
            != {
                "workflow_run_id",
                "run_attempt",
                "head_sha",
                "head_branch",
                "event",
                "path",
                "conclusion",
                "required_jobs",
            }
            or not isinstance(ci.get("workflow_run_id"), int)
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
            or any(
                not isinstance(name, str) or not name
                for name in ci["required_jobs"]
            )
            or plan.get("delivery_gate_sha256")
            != _canonical_digest(delivery)
            or document.get("delivery_gate") != delivery
            or document.get("delivery_gate_sha256")
            != plan.get("delivery_gate_sha256")
            or document.get("source_successor_impact_sha256")
            != plan.get("source_successor_impact_sha256")
            or document.get("production_source_trust_sha256")
            != plan.get("production_source_trust_sha256")
            or transition_logical_refs.get(
                SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF
            )
            != root_authority.get("source_sha")
            or plan.get("production_repository_transition_sha256")
            != _canonical_digest(repository_transition)
            or document.get("production_repository_transition_sha256")
            != plan.get("production_repository_transition_sha256")
            or plan.get("source_successor_impact") != expected_impact
            or plan.get("source_successor_impact_sha256")
            != _canonical_digest(expected_impact)
            or plan.get("verifier_agreement") != expected_verifier
            or document.get("verifier_agreement_sha256")
            != _canonical_digest(expected_verifier)
            or document.get("adopted_deployment_sha256")
            != root_authority.get("adopted_deployment_sha256")
            or document.get("bootstrap_control_sha256")
            != root_authority.get("bootstrap_control_sha256")
            or document.get("adopted_prerequisites_sha256")
            != root_authority.get("adopted_prerequisites_sha256")
            or plan.get("adopted_deployment_sha256")
            != document.get("adopted_deployment_sha256")
            or plan.get("bootstrap_control_sha256")
            != document.get("bootstrap_control_sha256")
            or plan.get("adopted_prerequisites_sha256")
            != document.get("adopted_prerequisites_sha256")
            or not isinstance(production, dict)
            or production
            != {
                "source_sha": root_authority.get("production_source_sha"),
                "source_tree": root_authority.get("production_source_tree"),
            }
            or plan.get("mutations") != SOURCE_SUCCESSOR_MUTATIONS
            or plan.get("authority_publication") != publication
        ):
            raise PrerequisiteError(
                "source successor authority differs from its root or target"
            )
        readiness = _validate_source_readiness(
            plan.get("source_readiness"),
            source_root=self.source_root,
            source_sha=target_sha,
            source_tree=target_tree,
        )
        if plan.get("source_readiness_sha256") != _canonical_digest(readiness):
            raise PrerequisiteError(
                "source successor readiness digest differs"
            )
        # Recompute every old/new Git identity from the exact target clone.
        for record in files:
            relative = str(record["path"])
            for label, commit in (
                ("predecessor", str(root_authority["source_sha"])),
                ("target", target_sha),
            ):
                raw = _run_git(
                    self.source_root,
                    "ls-tree",
                    commit,
                    "--",
                    relative,
                ).decode().strip()
                match = re.fullmatch(
                    r"([0-7]{6}) (blob) ([0-9a-f]{40})\t(.+)",
                    raw,
                )
                payload = _git_object_blob(
                    self.source_root,
                    commit,
                    relative,
                )
                identity = record[label]
                if (
                    match is None
                    or match.group(4) != relative
                    or identity
                    != {
                        "object_type": match.group(2),
                        "mode": match.group(1),
                        "blob_sha": match.group(3),
                        "sha256": _digest(payload),
                    }
                ):
                    raise PrerequisiteError(
                        "source successor Git blob differs from its manifest"
                    )
        compact: dict[str, object] = {
            "schema_version": 1,
            "authority_kind": SOURCE_SUCCESSOR_AUTHORITY_KIND,
            "operation_id": operation_id,
            "predecessor_authority_sha256": root_digest,
            "predecessor_source_sha": root_authority["source_sha"],
            "predecessor_source_tree": root_authority["source_tree"],
            "predecessor_marker_sha256": root_authority[
                "permission_marker_sha256"
            ],
            "target_source_sha": target_sha,
            "target_source_tree": target_tree,
            "production_source_sha": root_authority[
                "production_source_sha"
            ],
            "production_source_tree": root_authority[
                "production_source_tree"
            ],
            "adopted_deployment_sha256": document[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": document[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": document[
                "adopted_prerequisites_sha256"
            ],
            "plan_sha256": document["plan_sha256"],
            "source_successor_impact_sha256": document[
                "source_successor_impact_sha256"
            ],
            "source_trust_sha256": document[
                "production_source_trust_sha256"
            ],
            "production_repository_transition": repository_transition,
            "production_repository_transition_sha256": document[
                "production_repository_transition_sha256"
            ],
            "delivery_gate": delivery,
            "delivery_gate_sha256": document["delivery_gate_sha256"],
            "fixed_files": files,
            "fixed_files_sha256": document["files_sha256"],
            "changed_files": changed_paths,
            "changed_files_sha256": document[
                "changed_paths_sha256"
            ],
            "completed_at": document["completed_at"],
            "authority_file_sha256": raw_digest,
        }
        compact["identity_sha256"] = _canonical_digest(compact)
        return files, compact

    def _read_source_successor_authority(
        self,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        authority = (
            _load_json_with_digest(self.runtime_root / SOURCE_SUCCESSOR_AUTHORITY_PATH)
            if state_fd is None
            else _load_json_with_digest_at(
                state_fd,
                SOURCE_SUCCESSOR_AUTHORITY_PATH.name,
                maximum_bytes=SOURCE_SUCCESSOR_JSON_MAX_BYTES,
            )
        )
        entries = self._source_successor_lineage_entries()
        if entries != sorted(
            [
                SOURCE_SUCCESSOR_AUTHORITY_PATH.name,
                SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY.name,
            ]
        ):
            raise PrerequisiteError(
                "source successor lineage contains publication residue"
            )
        transaction_root = self.runtime_root / SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
        transaction_fd = _open_private_directory(transaction_root)
        try:
            names = sorted(os.listdir(transaction_fd))
            document = authority[0]
            operation_id = str(document.get("operation_id", ""))
            if names != [f"{operation_id}.json"]:
                raise PrerequisiteError(
                    "source successor journal lineage is incomplete"
                )
            journal, journal_digest = _load_json_with_digest_at(
                transaction_fd,
                names[0],
                maximum_bytes=SOURCE_SUCCESSOR_JSON_MAX_BYTES,
            )
            journal_fields = {
                "schema_version",
                "status",
                "phase",
                "operation_id",
                "plan",
                "plan_sha256",
                "source_successor_impact_sha256",
                "production_source_trust_sha256",
                "created_at",
                "completed_at",
                "aborted_at",
            }
            created_at = _parse_canonical_utc_timestamp(
                journal.get("created_at")
            )
            completed_at = _parse_canonical_utc_timestamp(
                journal.get("completed_at")
            )
            authority_completed_at = _parse_canonical_utc_timestamp(
                document.get("completed_at")
            )
            if (
                set(journal) != journal_fields
                or journal_digest
                != _digest(_canonical_bytes(journal) + b"\n")
                or not _has_exact_schema_version(journal, 1)
                or journal.get("operation_id") != operation_id
                or journal.get("status") != "completed"
                or journal.get("phase") != "completed"
                or journal.get("plan") != document.get("plan")
                or journal.get("plan_sha256")
                != document.get("plan_sha256")
                or journal.get("source_successor_impact_sha256")
                != document.get("source_successor_impact_sha256")
                or journal.get("production_source_trust_sha256")
                != document.get("production_source_trust_sha256")
                or journal.get("completed_at")
                != document.get("completed_at")
                or created_at is None
                or completed_at is None
                or authority_completed_at is None
                or completed_at != authority_completed_at
                or created_at > completed_at
                or journal.get("aborted_at") is not None
            ):
                raise PrerequisiteError(
                    "source successor completed journal differs"
                )
        finally:
            os.close(transaction_fd)
        journal_snapshot: dict[str, object] = {
            "entries": names,
            "raw_sha256": journal_digest,
        }
        return authority[0], authority[1], journal_snapshot

    def _git_permission_successor(
        self,
        authority: dict[str, object],
        authority_digest: str,
        *,
        target_sha: str,
        target_tree: str,
        root_journal: dict[str, str] | None = None,
        source_successor_authority: dict[str, object] | None = None,
        source_successor_digest: str | None = None,
    ) -> dict[str, object]:
        plan = authority.get("plan")
        authority_sha = authority.get("source_sha")
        authority_tree = authority.get("source_tree")
        if (
            not _has_exact_schema_version(authority, 1)
            or authority.get("status") != "completed"
            or authority.get("authority_kind") != PERMISSION_AUTHORITY_KIND
            or not isinstance(plan, dict)
            or authority.get("plan_sha256") != _canonical_digest(plan)
            or not isinstance(authority_sha, str)
            or SHA_RE.fullmatch(authority_sha) is None
            or not isinstance(authority_tree, str)
            or SHA_RE.fullmatch(authority_tree) is None
        ):
            raise PrerequisiteError(
                "adopted Git permission authority is invalid"
            )
        observed_tree = _run_git(
            self.source_root,
            "rev-parse",
            "--verify",
            f"{authority_sha}^{{tree}}",
        ).decode().strip()
        if observed_tree != authority_tree:
            raise PrerequisiteError(
                "adopted Git permission source tree differs"
            )
        exact = authority_sha == target_sha and authority_tree == target_tree
        if authority_sha == target_sha and not exact:
            raise PrerequisiteError(
                "adopted Git permission commit has another tree"
            )
        if not exact:
            try:
                _run_git(
                    self.source_root,
                    "merge-base",
                    "--is-ancestor",
                    authority_sha,
                    target_sha,
                )
            except PrerequisiteError as exc:
                raise PrerequisiteError(
                    "adopted Git permission source is not a target ancestor"
                ) from exc
        if source_successor_authority is not None:
            if (
                not isinstance(root_journal, dict)
                or set(root_journal)
                != {
                    "completed_journal_sha256",
                    "source_trust_sha256",
                }
                or not isinstance(source_successor_digest, str)
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    source_successor_digest,
                )
                is None
            ):
                raise PrerequisiteError(
                    "source successor raw authority digest is invalid"
                )
            files, compact = self._validate_source_successor_authority(
                source_successor_authority,
                source_successor_digest,
                root_authority=authority,
                root_digest=authority_digest,
                root_journal=root_journal,
                target_sha=target_sha,
                target_tree=target_tree,
            )
            body_v2: dict[str, object] = {
                "schema_version": 2,
                "policy": "nexpoly-adopted-git-permission-successor-v2",
                "mode": "protected-main-ci-exact-target",
                "root_authority": {
                    "source_sha": authority_sha,
                    "source_tree": authority_tree,
                    "raw_sha256": authority_digest,
                },
                "source_successor_authority": compact,
                "target": {
                    "source_sha": target_sha,
                    "source_tree": target_tree,
                },
                "files": files,
                "files_sha256": _canonical_digest(files),
            }
            body_v2["identity_sha256"] = _canonical_digest(body_v2)
            return body_v2
        files: list[dict[str, str]] = []
        for relative in UNIT_PERMISSION_SUCCESSOR_BLOBS:
            authority_payload = _run_git(
                self.source_root,
                "show",
                f"{authority_sha}:{relative}",
            )
            target_payload = _git_blob(
                self.source_root,
                target_sha,
                relative,
            )
            if authority_payload != target_payload:
                raise PrerequisiteError(
                    f"unit permission successor blob differs: {relative}"
                )
            files.append(
                {"path": relative, "sha256": _digest(target_payload)}
            )
        body: dict[str, object] = {
            "schema_version": 1,
            "policy": "nexpoly-adopted-git-permission-successor-v1",
            "mode": "exact-source" if exact else "ancestor-byte-identical",
            "authority": {
                "source_sha": authority_sha,
                "source_tree": authority_tree,
                "raw_sha256": authority_digest,
            },
            "target": {
                "source_sha": target_sha,
                "source_tree": target_tree,
            },
            "files": files,
            "files_sha256": _canonical_digest(files),
        }
        body["identity_sha256"] = _canonical_digest(body)
        return body

    def _unit_adoption_context(
        self,
        *,
        source_sha: str,
        source_tree: str,
    ) -> dict[str, object]:
        context = self._adoption_permission_context(
            permit_prepared_abort=True
        )
        first = self._read_git_permission_authority()
        second = self._read_git_permission_authority()
        if first != second:
            raise PrerequisiteError(
                "adopted Git permission authority changed while reading"
            )
        authority, authority_digest, root_journal = first
        if (
            authority.get("adopted_deployment_sha256")
            != context["adopted_deployment_sha256"]
            or authority.get("bootstrap_control_sha256")
            != context["bootstrap_control_sha256"]
            or authority.get("adopted_prerequisites_sha256")
            != context["adopted_prerequisites_sha256"]
            or authority.get("production_source_sha")
            != context["production_source"]["source_sha"]
            or authority.get("production_source_tree")
            != context["production_source"]["source_tree"]
        ):
            raise PrerequisiteError(
                "adopted Git permission authority differs from adoption"
            )
        try:
            marker = GIT_SOURCE_TRUST.verify_repository_permission_takeover(
                self.production_root,
                self.permission_marker_path,
            )
        except Exception as exc:
            raise PrerequisiteError(
                "adopted Git permission marker is invalid"
            ) from exc
        if (
            authority.get("permission_marker_sha256")
            != _file_digest(self.permission_marker_path, mode=0o600)
            or authority.get("permission_evidence_sha256")
            != marker.get("evidence_sha256")
            or authority.get("permission_inventory_sha256")
            != marker.get("inventory_sha256")
        ):
            raise PrerequisiteError(
                "adopted Git permission evidence differs"
            )
        lineage_entries = self._source_successor_lineage_entries()
        source_successor: (
            tuple[dict[str, object], str, dict[str, object]] | None
        ) = None
        if lineage_entries:
            source_first = self._read_source_successor_authority()
            source_second = self._read_source_successor_authority()
            if source_first != source_second:
                raise PrerequisiteError(
                    "source successor authority changed while reading"
                )
            source_successor = source_first
        successor = self._git_permission_successor(
            authority,
            authority_digest,
            target_sha=source_sha,
            target_tree=source_tree,
            root_journal=root_journal,
            source_successor_authority=(
                source_successor[0] if source_successor is not None else None
            ),
            source_successor_digest=(
                source_successor[1] if source_successor is not None else None
            ),
        )
        self._validate_prepare_abort_gate()
        result: dict[str, object] = {
            **context,
            "adopted_git_permissions_sha256": authority_digest,
            "git_permission_successor": successor,
        }
        if source_successor is not None:
            result["adopted_git_permission_source_successor_sha256"] = (
                source_successor[1]
            )
        return result

    @staticmethod
    def _prepare_abort_digest(value: object, label: str) -> str:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
        ):
            raise PrerequisiteError(f"{label} is invalid")
        return value

    @staticmethod
    def _prepare_abort_operation_id(value: object) -> str:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,127}", value) is None
        ):
            raise PrerequisiteError("prepare-abort operation ID is invalid")
        return value

    def _validate_prepare_abort_journal_record(
        self,
        document: object,
        operation_id: str,
    ) -> dict[str, object]:
        """Validate the exact late prepare-abort schema accepted for takeover."""

        fields = {
            "schema_version",
            "operation_id",
            "status",
            "phase",
            "prepare_owner",
            "prepare_owner_sha256",
            "target_sha",
            "target_tree",
            "control_handoff_sha256",
            "control_handoff_schema_version",
            "executor_control_sha256",
            "operation_inventory_sha256",
            "descriptor_sha256",
            "prepare_staging",
            "wheel_staging",
            "dft_staging",
            "owned_slots",
            "prepared_ref",
            "current_state_sha256",
            "active_control_sha256",
            "active_slot_sha256",
            "active_slot",
            "bridge_token_sha256",
            "bridge_token_operation_id",
            "bridge_token_status",
            "archive_path",
            "archive_inventory_sha256",
            "created_at",
            "completed_at",
        }
        if (
            not isinstance(document, dict)
            or set(document) != fields
            or not _has_exact_schema_version(document, 1)
            or document.get("operation_id") != operation_id
        ):
            raise PrerequisiteError("prepare-abort journal has an invalid shape")
        owner = document.get("prepare_owner")
        owner_fields = {
            "schema_version",
            "operation_id",
            "target_sha",
            "controller_sha256",
            "created_at",
        }
        if (
            not isinstance(owner, dict)
            or set(owner) != owner_fields
            or not _has_exact_schema_version(owner, 1)
            or owner.get("operation_id") != operation_id
            or owner.get("target_sha") != document.get("target_sha")
            or not isinstance(owner.get("created_at"), str)
            or not owner["created_at"]
            or document.get("prepare_owner_sha256")
            != _canonical_digest(owner)
        ):
            raise PrerequisiteError("prepare-abort owner identity is invalid")
        target_sha = document.get("target_sha")
        if not isinstance(target_sha, str) or SHA_RE.fullmatch(target_sha) is None:
            raise PrerequisiteError("prepare-abort target SHA is invalid")
        self._prepare_abort_digest(
            owner.get("controller_sha256"),
            "prepare-abort owner controller digest",
        )
        self._prepare_abort_digest(
            document.get("operation_inventory_sha256"),
            "prepare-abort operation inventory",
        )
        self._prepare_abort_digest(
            document.get("active_control_sha256"),
            "prepare-abort active control",
        )
        # The one-time takeover intentionally supports only the sealed late
        # residue seen in production.  Descriptor-bearing or staged prepares
        # remain outside this narrow authority and fail closed.
        if (
            document.get("descriptor_sha256") is not None
            or document.get("prepare_staging")
            != {
                "live_inventory_sha256": None,
                "tombstone_inventory_sha256": None,
            }
            or document.get("wheel_staging") != []
            or document.get("dft_staging")
            != {
                "staging_inventory_sha256": None,
                "cache_inventory_sha256": None,
                "incomplete_release_inventory_sha256": None,
                "ready_sha256": None,
                "ready_runtime_inventory_sha256": None,
                "ready_owner_sha256": None,
            }
            or document.get("owned_slots") != []
            or any(
                document.get(field) is not None
                for field in (
                    "bridge_token_sha256",
                    "bridge_token_operation_id",
                    "bridge_token_status",
                )
            )
        ):
            raise PrerequisiteError(
                "prepare-abort journal is outside the sealed late-residue policy"
            )
        current_state = document.get("current_state_sha256")
        if current_state is not None:
            self._prepare_abort_digest(
                current_state,
                "prepare-abort current state",
            )
        target_tree = document.get("target_tree")
        if target_tree is not None and (
            not isinstance(target_tree, str) or SHA_RE.fullmatch(target_tree) is None
        ):
            raise PrerequisiteError("prepare-abort target tree is invalid")
        handoff_digest = document.get("control_handoff_sha256")
        handoff_schema = document.get("control_handoff_schema_version")
        executor_digest = document.get("executor_control_sha256")
        if handoff_digest is None:
            if handoff_schema is not None or executor_digest is not None:
                raise PrerequisiteError(
                    "prepare-abort handoff evidence is incomplete"
                )
        else:
            self._prepare_abort_digest(
                handoff_digest,
                "prepare-abort handoff",
            )
            self._prepare_abort_digest(
                executor_digest,
                "prepare-abort executor control",
            )
            if (
                type(handoff_schema) is not int
                or handoff_schema not in {1, 2}
                or target_tree is None
            ):
                raise PrerequisiteError(
                    "prepare-abort handoff schema is invalid"
                )
        prepared_ref = document.get("prepared_ref")
        if (
            not isinstance(prepared_ref, dict)
            or set(prepared_ref) != {"name", "target_sha"}
            or prepared_ref.get("name")
            != f"refs/nexpoly/prepared/{operation_id}"
        ):
            raise PrerequisiteError("prepare-abort prepared ref is invalid")
        ref_target = prepared_ref.get("target_sha")
        if ref_target is not None and ref_target != target_sha:
            raise PrerequisiteError("prepare-abort prepared ref target differs")
        active_slot = document.get("active_slot")
        active_slot_digest = document.get("active_slot_sha256")
        if active_slot is None:
            if active_slot_digest is not None:
                raise PrerequisiteError(
                    "prepare-abort active slot digest lacks a record"
                )
        else:
            active_fields = {
                "schema_version",
                "component",
                "slot",
                "source_sha",
                "source_tree",
                "worker_lock_sha256",
                "slot_record_sha256",
                "operation_id",
                "activated_at",
            }
            if (
                not isinstance(active_slot, dict)
                or set(active_slot) != active_fields
                or not _has_exact_schema_version(active_slot, 1)
                or active_slot.get("component") != "monomer-md"
                or active_slot.get("slot") not in {"a", "b"}
                or not isinstance(active_slot.get("activated_at"), str)
                or not active_slot["activated_at"]
            ):
                raise PrerequisiteError("prepare-abort active slot is invalid")
            for field in ("source_sha", "source_tree"):
                value = active_slot.get(field)
                if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
                    raise PrerequisiteError(
                        "prepare-abort active slot source is invalid"
                    )
            self._prepare_abort_operation_id(active_slot.get("operation_id"))
            for field in (
                "worker_lock_sha256",
                "slot_record_sha256",
            ):
                self._prepare_abort_digest(
                    active_slot.get(field),
                    f"prepare-abort active slot {field}",
                )
            self._prepare_abort_digest(
                active_slot_digest,
                "prepare-abort active slot record",
            )
        archive = (
            self.runtime_root
            / "state/prepare-aborts/archives"
            / operation_id
        )
        if document.get("archive_path") != str(archive):
            raise PrerequisiteError("prepare-abort archive path differs")
        phase = document.get("phase")
        if phase == "completed":
            if (
                document.get("status") != "aborted"
                or not isinstance(document.get("completed_at"), str)
                or not document["completed_at"]
            ):
                raise PrerequisiteError(
                    "completed prepare-abort journal is invalid"
                )
            self._prepare_abort_digest(
                document.get("archive_inventory_sha256"),
                "completed prepare-abort archive inventory",
            )
        elif (
            phase != "operation-archive-intent"
            or document.get("status") != "aborting"
            or document.get("archive_inventory_sha256") is not None
            or document.get("completed_at") is not None
        ):
            raise PrerequisiteError(
                "incomplete prepare-abort is outside operation-archive-intent"
            )
        if (
            not isinstance(document.get("created_at"), str)
            or not document["created_at"]
        ):
            raise PrerequisiteError("prepare-abort timestamp is invalid")
        return dict(document)

    @staticmethod
    def _prepare_abort_directory_names(path: Path) -> set[str]:
        if not (path.exists() or path.is_symlink()):
            return set()
        _private_metadata(path, mode=0o700, regular=False)
        return {entry.name for entry in path.iterdir()}

    def _assert_no_prepared_refs(self) -> None:
        observed = GIT_SOURCE_TRUST.run_git(
            self.production_root,
            "for-each-ref",
            "--format=%(refname)",
            "refs/nexpoly/prepared/",
            ambient={},
            check=False,
        )
        if observed.returncode != 0:
            raise PrerequisiteError("prepared Git ref inventory cannot be read")
        if not isinstance(observed.stdout, str):
            raise PrerequisiteError("prepared Git ref inventory is malformed")
        references = [
            line
            for line in observed.stdout.splitlines()
            if line
        ]
        if references:
            raise PrerequisiteError(
                "prepared Git ref remains during unit permission adoption"
            )

    def _validate_prepare_abort_archive(
        self,
        journal: dict[str, object],
        *,
        live_operation: bool,
    ) -> None:
        operation_id = str(journal["operation_id"])
        archive = Path(str(journal["archive_path"]))
        _private_metadata(archive, mode=0o700, regular=False)
        owner = {
            "schema_version": 1,
            "operation_id": operation_id,
            "prepare_owner_sha256": journal["prepare_owner_sha256"],
            "created_at": journal["created_at"],
        }
        observed_owner = _load_json(archive / "ARCHIVE-OWNER.json")
        if (
            not _has_exact_schema_version(observed_owner, 1)
            or observed_owner != owner
        ):
            raise PrerequisiteError("prepare-abort archive owner differs")
        expected_names = {
            "ARCHIVE-OWNER.json",
            "wheel-staging",
            "wheel-staging-tombstones",
            "monomer-dft-runtime",
        }
        for empty_directory in (
            "wheel-staging",
            "wheel-staging-tombstones",
            "monomer-dft-runtime",
        ):
            path = archive / empty_directory
            _private_metadata(path, mode=0o700, regular=False)
            if any(path.iterdir()):
                raise PrerequisiteError(
                    "prepare-abort late-residue archive is not empty"
                )
        handoff_digest = journal["control_handoff_sha256"]
        if handoff_digest is not None:
            expected_names.add("control-handoff.json")
            if (
                _file_digest(archive / "control-handoff.json", mode=0o600)
                != handoff_digest
            ):
                raise PrerequisiteError("prepare-abort handoff archive differs")
        elif (
            (archive / "control-handoff.json").exists()
            or (archive / "control-handoff.json").is_symlink()
        ):
            raise PrerequisiteError("unrecorded prepare handoff archive exists")
        prepared_ref = journal["prepared_ref"]
        if not isinstance(prepared_ref, dict):  # pragma: no cover - validated
            raise PrerequisiteError("prepare-abort prepared ref is invalid")
        ref_target = prepared_ref["target_sha"]
        if ref_target is not None:
            expected_names.add("prepared-ref.json")
            expected_ref = {
                "schema_version": 1,
                "operation_id": operation_id,
                "ref": prepared_ref["name"],
                "target_sha": ref_target,
            }
            observed_ref = _load_json(archive / "prepared-ref.json")
            if (
                not _has_exact_schema_version(observed_ref, 1)
                or observed_ref != expected_ref
            ):
                raise PrerequisiteError("prepare-abort ref archive differs")
        elif (
            (archive / "prepared-ref.json").exists()
            or (archive / "prepared-ref.json").is_symlink()
        ):
            raise PrerequisiteError("unrecorded prepared ref archive exists")
        if not live_operation:
            expected_names.add("operation")
        if self._prepare_abort_directory_names(archive) != expected_names:
            raise PrerequisiteError(
                "prepare-abort archive contains an unknown or missing entry"
            )
        operation = (
            self.runtime_root / "state/prepared" / operation_id
            if live_operation
            else archive / "operation"
        )
        _private_metadata(operation, mode=0o700, regular=False)
        if (
            _private_tree_inventory_digest(operation)
            != journal["operation_inventory_sha256"]
        ):
            raise PrerequisiteError(
                "prepare operation differs from its abort journal"
            )
        for forbidden in ("descriptor.json", "ready.json", "READY"):
            path = operation / forbidden
            if path.exists() or path.is_symlink():
                raise PrerequisiteError(
                    "prepared descriptor/READY blocks unit permission adoption"
                )
        if journal["phase"] == "completed" and (
            _private_tree_inventory_digest(archive)
            != journal["archive_inventory_sha256"]
        ):
            raise PrerequisiteError("completed prepare-abort archive changed")

    def _validate_prepare_abort_gate(self) -> None:
        """Validate the complete prepare-abort namespace without writing it."""

        self._assert_no_prepared_refs()
        state = self.runtime_root / "state"
        prepared_root = state / "prepared"
        abort_root = state / "prepare-aborts"
        archives_root = abort_root / "archives"
        handoffs_root = state / "control-handoffs"
        journals: dict[str, dict[str, object]] = {}
        if abort_root.exists() or abort_root.is_symlink():
            _private_metadata(abort_root, mode=0o700, regular=False)
            entries = sorted(abort_root.iterdir(), key=lambda path: path.name)
            archive_entries = [entry for entry in entries if entry.name == "archives"]
            if len(archive_entries) != 1:
                raise PrerequisiteError(
                    "prepare-abort journal root lacks its exact archives directory"
                )
            _private_metadata(archives_root, mode=0o700, regular=False)
            for entry in entries:
                if entry.name == "archives":
                    continue
                if not entry.name.endswith(".json"):
                    raise PrerequisiteError(
                        "prepare-abort journal root contains an unknown entry"
                    )
                operation_id = self._prepare_abort_operation_id(entry.name[:-5])
                document = _load_json(entry)
                journals[operation_id] = (
                    self._validate_prepare_abort_journal_record(
                        document,
                        operation_id,
                    )
                )
            archive_names = self._prepare_abort_directory_names(archives_root)
            if archive_names != set(journals):
                raise PrerequisiteError(
                    "prepare-abort archive inventory has an orphan or omission"
                )
        incomplete = [
            operation_id
            for operation_id, journal in journals.items()
            if journal["phase"] != "completed"
        ]
        if len(incomplete) > 1:
            raise PrerequisiteError(
                "unit permission adoption requires at most one incomplete prepare-abort"
            )
        prepared_names = self._prepare_abort_directory_names(prepared_root)
        live_expected: set[str] = set()
        for operation_id, journal in journals.items():
            archive_operation = Path(str(journal["archive_path"])) / "operation"
            live_present = operation_id in prepared_names
            archived_present = (
                archive_operation.exists() or archive_operation.is_symlink()
            )
            if journal["phase"] == "completed":
                if live_present or not archived_present:
                    raise PrerequisiteError(
                        "completed prepare-abort operation position is invalid"
                    )
            elif live_present == archived_present:
                raise PrerequisiteError(
                    "incomplete prepare-abort operation must be live or archived exactly once"
                )
            if live_present:
                live_expected.add(operation_id)
            self._validate_prepare_abort_archive(
                journal,
                live_operation=live_present,
            )
            live_handoff = handoffs_root / f"{operation_id}.json"
            if live_handoff.exists() or live_handoff.is_symlink():
                raise PrerequisiteError(
                    "prepare-abort handoff remains live at the sealed late boundary"
                )
            prepared_ref = journal["prepared_ref"]
            if not isinstance(prepared_ref, dict):  # pragma: no cover - validated
                raise PrerequisiteError("prepare-abort prepared ref is invalid")
            self._assert_prepared_ref_absent(str(prepared_ref["name"]))
        if prepared_names != live_expected:
            raise PrerequisiteError(
                "prepared operation inventory contains an orphan or omission"
            )
        if self._prepare_abort_directory_names(handoffs_root):
            raise PrerequisiteError(
                "control handoff inventory contains an orphan entry"
            )
        self._assert_no_prepared_refs()

    def _assert_prepared_ref_absent(self, reference: str) -> None:
        """Observe one full ref as absent across Git 2.43 return semantics."""

        if re.fullmatch(r"refs/nexpoly/prepared/[a-z0-9._-]+", reference) is None:
            raise PrerequisiteError("prepared Git ref name is invalid")
        symbolic = GIT_SOURCE_TRUST.run_git(
            self.production_root,
            "symbolic-ref",
            "--quiet",
            reference,
            ambient={},
            check=False,
        )
        if symbolic.returncode == 0:
            raise PrerequisiteError(
                "prepared Git ref remains as a symbolic ref"
            )
        if symbolic.returncode != 1:
            raise PrerequisiteError(
                "prepared Git symbolic-ref absence cannot be proved"
            )
        # `show-ref --verify --quiet` reports a missing full ref as 128 on the
        # production Git 2.43 build.  `rev-parse --verify --quiet` has the
        # documented observer-friendly rc=1.  Observe the raw ref on both
        # sides of the commit peel so direct, annotated, non-commit, and
        # between-query ref drift all fail closed.
        for expression in (reference, f"{reference}^{{commit}}", reference):
            observed = GIT_SOURCE_TRUST.run_git(
                self.production_root,
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                expression,
                ambient={},
                check=False,
            )
            if observed.returncode == 0:
                raise PrerequisiteError(
                    "prepared Git ref remains during unit permission adoption"
                )
            if observed.returncode != 1:
                raise PrerequisiteError(
                    "prepared Git ref absence cannot be proved"
                )

    def _unit_authority_publication_plan(
        self,
        operation_id: str,
    ) -> dict[str, object]:
        """Bind every create-once authority name before transaction intent."""

        operation_id = _require_unit_permission_operation_id(operation_id)
        final_name = UNIT_PERMISSION_AUTHORITY_PATH.name
        staging_name = f".{final_name}.create-{operation_id}"
        quarantine_name = f"{staging_name}.quarantine"
        state = self.runtime_root / "state"
        return {
            "schema_version": 1,
            "policy": "nexpoly-adopted-unit-authority-publication-v1",
            "directory": str(state),
            "entries": [
                {
                    "role": "final",
                    "name": final_name,
                    "path": str(state / final_name),
                    "initially_absent": True,
                },
                {
                    "role": "staging",
                    "name": staging_name,
                    "path": str(state / staging_name),
                    "initially_absent": True,
                },
                {
                    "role": "staging-quarantine",
                    "name": quarantine_name,
                    "path": str(state / quarantine_name),
                    "initially_absent": True,
                },
            ],
        }

    def _validate_unit_authority_publication_plan(
        self,
        value: object,
        operation_id: str,
    ) -> dict[str, object]:
        expected = self._unit_authority_publication_plan(operation_id)
        if value != expected:
            raise PrerequisiteError(
                "unit authority publication ownership plan changed"
            )
        return expected

    @staticmethod
    def _unit_permission_impact(
        units: list[dict[str, object]],
        authority_publication: dict[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "policy": "nexpoly-adopted-unit-permission-hardening-v1",
            "units": units,
            "authority_publication": authority_publication,
        }

    def _unit_source_plan(
        self,
        source_sha: str,
        operation_id: str,
    ) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_unit_permission_operation_id(operation_id)
        # The fixed successor authority must be validated before the target
        # bootstrap contract is allowed to execute for the ordinary unit
        # transaction.  The standalone publisher itself never executes it.
        source_tree = _validate_source_checkout(self.source_root, source_sha)
        context = self._unit_adoption_context(
            source_sha=source_sha,
            source_tree=source_tree,
        )
        verified_tree, readiness, delivery = self._source_authority(source_sha)
        if verified_tree != source_tree:
            raise PrerequisiteError(
                "unit permission target tree changed after successor validation"
            )
        successor = context.get("git_permission_successor")
        successor_compact = (
            successor.get("source_successor_authority")
            if isinstance(successor, dict)
            else None
        )
        if (
            "adopted_git_permission_source_successor_sha256" in context
            and (
                not isinstance(successor_compact, dict)
                or successor_compact.get("delivery_gate") != delivery
                or successor_compact.get("source_trust_sha256")
                != self._production_source_trust(context)
            )
        ):
            raise PrerequisiteError(
                "unit permission source evidence differs from successor"
            )
        authority_publication = self._unit_authority_publication_plan(
            operation_id
        )
        self._assert_unit_authority_namespace_absent(
            authority_publication,
            operation_id,
            durable=False,
        )
        units, _payloads = self._capture_units(
            md_mode=0o664,
            adopted_digest=str(context["adopted_deployment_sha256"]),
        )
        context_after = self._unit_adoption_context(
            source_sha=source_sha,
            source_tree=source_tree,
        )
        if context_after != context:
            raise PrerequisiteError(
                "unit permission authorities changed while planning"
            )
        self._assert_unit_authority_namespace_absent(
            authority_publication,
            operation_id,
            durable=False,
        )
        impact = self._unit_permission_impact(
            units,
            authority_publication,
        )
        schema_version = (
            2
            if "adopted_git_permission_source_successor_sha256" in context
            else 1
        )
        return {
            "schema_version": schema_version,
            "authority_kind": UNIT_PERMISSION_AUTHORITY_KIND,
            "operation_id": operation_id,
            "source_sha": source_sha,
            "source_tree": source_tree,
            "source_readiness": readiness,
            "source_readiness_sha256": _canonical_digest(readiness),
            "delivery_gate": delivery,
            "delivery_gate_sha256": _canonical_digest(delivery),
            **context,
            "units": units,
            "authority_publication": authority_publication,
            "unit_permission_impact_sha256": _canonical_digest(impact),
            "mutations": {
                "services_restarted": False,
                "source": False,
                "database": False,
                "credentials": False,
                "md_unit_inode": True,
                "dft_unit": False,
                "runtime_authority": True,
                "systemd_daemon_reload": True,
            },
        }

    def _unit_plan_result(
        self,
        plan: dict[str, object],
    ) -> dict[str, object]:
        return {
            "action": "adopt-unit-permission-plan",
            "apply": False,
            "logical_zero_write": True,
            "atime_zero_write": (
                _mount_suppresses_atime(self.source_root)
                and _mount_suppresses_atime(self.runtime_root)
                and _mount_suppresses_atime(self.production_root)
                and _mount_suppresses_atime(self.unit_parent)
            ),
            "plan": plan,
            "plan_sha256": _canonical_digest(plan),
            "unit_permission_impact_sha256": plan[
                "unit_permission_impact_sha256"
            ],
        }

    def _unit_authority_state_fd(self, *, durable: bool) -> tuple[int, bool]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if durable and state_fd is None:
            raise PrerequisiteError(
                "unit authority namespace requires pinned state"
            )
        if state_fd is not None:
            return state_fd, False
        return _open_private_directory(self.runtime_root / "state"), True

    def _unit_authority_names(
        self,
        publication: object,
        operation_id: str,
    ) -> tuple[str, str, str]:
        validated = self._validate_unit_authority_publication_plan(
            publication,
            operation_id,
        )
        entries = validated["entries"]
        if not isinstance(entries, list):  # pragma: no cover - exact validator
            raise PrerequisiteError(
                "unit authority publication ownership plan is invalid"
            )
        return tuple(str(entry["name"]) for entry in entries)  # type: ignore[return-value]

    def _assert_unit_authority_namespace_absent(
        self,
        publication: object,
        operation_id: str,
        *,
        durable: bool,
    ) -> None:
        operation_id = _require_unit_permission_operation_id(operation_id)
        names = self._unit_authority_names(publication, operation_id)
        state_fd, close_state = self._unit_authority_state_fd(
            durable=durable
        )
        try:
            for _pass in range(2 if durable else 1):
                if any(_entry_exists_at(state_fd, name) for name in names):
                    raise PrerequisiteError(
                        "unit authority publication namespace predates commit intent"
                    )
                if durable and _pass == 0:
                    os.fsync(state_fd)
                    self._assert_pinned_runtime()
        finally:
            if close_state:
                os.close(state_fd)

    def _read_unit_authority_residue_at(
        self,
        state_fd: int,
        name: str,
        *,
        expected_payload: bytes,
        exact: bool,
        allowed_nlinks: frozenset[int],
        durable: bool,
    ) -> tuple[bytes, os.stat_result]:
        descriptor = _open_private_regular_at(
            state_fd,
            name,
            mode=0o600,
            allowed_nlinks=allowed_nlinks,
        )
        try:
            observed_payload, observed = _stable_owned_payload_at(
                state_fd,
                name,
                descriptor,
                maximum_bytes=len(expected_payload),
                label="unit authority publication residue",
            )
            if (
                exact
                and observed_payload != expected_payload
                or not exact
                and not expected_payload.startswith(observed_payload)
            ):
                raise PrerequisiteError(
                    "unit authority publication residue payload differs"
                )
            if durable:
                os.fsync(descriptor)
                sealed_payload, sealed = _stable_owned_payload_at(
                    state_fd,
                    name,
                    descriptor,
                    maximum_bytes=len(expected_payload),
                    label="unit authority publication residue",
                )
                if (
                    sealed_payload != observed_payload
                    or _stable_regular_identity(sealed)
                    != _stable_regular_identity(observed)
                ):
                    raise PrerequisiteError(
                        "unit authority publication residue changed before fsync"
                    )
                os.fsync(state_fd)
                verified_payload, verified = _stable_owned_payload_at(
                    state_fd,
                    name,
                    descriptor,
                    maximum_bytes=len(expected_payload),
                    label="unit authority publication residue",
                )
                if (
                    verified_payload != observed_payload
                    or _stable_regular_identity(verified)
                    != _stable_regular_identity(sealed)
                ):
                    raise PrerequisiteError(
                        "unit authority publication residue changed after fsync"
                    )
                observed = verified
            return observed_payload, observed
        finally:
            os.close(descriptor)

    @staticmethod
    def _assert_unit_authority_namespace_snapshot(
        state_fd: int,
        snapshot: dict[str, os.stat_result | None],
    ) -> None:
        for name, expected in snapshot.items():
            try:
                observed = os.stat(
                    name,
                    dir_fd=state_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if expected is not None:
                    raise PrerequisiteError(
                        "unit authority publication residue disappeared"
                    )
                continue
            if expected is None or _stable_regular_identity(
                observed
            ) != _stable_regular_identity(expected):
                raise PrerequisiteError(
                    "unit authority publication namespace changed"
                )

    def _validate_unit_authority_commit_namespace(
        self,
        transaction: dict[str, object],
        *,
        durable: bool,
        require_final: bool,
    ) -> None:
        operation_id = str(transaction["operation_id"])
        plan = transaction.get("plan")
        if not isinstance(plan, dict):
            raise PrerequisiteError(
                "unit authority publication lacks a durable plan"
            )
        names = self._unit_authority_names(
            plan.get("authority_publication"),
            operation_id,
        )
        final_name, staging_name, quarantine_name = names
        expected_payload = _canonical_bytes(
            self._unit_authority(transaction)
        ) + b"\n"
        state_fd, close_state = self._unit_authority_state_fd(
            durable=durable
        )
        try:
            final_exists = _entry_exists_at(state_fd, final_name)
            staging_exists = _entry_exists_at(state_fd, staging_name)
            quarantine_exists = _entry_exists_at(state_fd, quarantine_name)
            if staging_exists and quarantine_exists:
                raise PrerequisiteError(
                    "unit authority staging and quarantine both exist"
                )
            snapshot: dict[str, os.stat_result | None] = {
                name: None for name in names
            }
            if final_exists:
                _final_payload, final_metadata = (
                    self._read_unit_authority_residue_at(
                        state_fd,
                        final_name,
                        expected_payload=expected_payload,
                        exact=True,
                        allowed_nlinks=frozenset({1, 2}),
                        durable=durable,
                    )
                )
                snapshot[final_name] = final_metadata
                companion_name = (
                    staging_name
                    if staging_exists
                    else quarantine_name if quarantine_exists else None
                )
                if final_metadata.st_nlink == 1:
                    if companion_name is not None:
                        raise PrerequisiteError(
                            "unit authority has an unowned publication residue"
                        )
                elif companion_name is None:
                    raise PrerequisiteError(
                        "unit authority has an unowned hard link"
                    )
                else:
                    _companion_payload, companion_metadata = (
                        self._read_unit_authority_residue_at(
                            state_fd,
                            companion_name,
                            expected_payload=expected_payload,
                            exact=True,
                            allowed_nlinks=frozenset({2}),
                            durable=durable,
                        )
                    )
                    if _stable_regular_identity(
                        companion_metadata
                    ) != _stable_regular_identity(final_metadata):
                        raise PrerequisiteError(
                            "unit authority publication hard links differ"
                        )
                    snapshot[companion_name] = companion_metadata
                if require_final and final_metadata.st_nlink != 1:
                    raise PrerequisiteError(
                        "completed unit authority publication is incomplete"
                    )
            else:
                if require_final:
                    raise PrerequisiteError(
                        "completed unit authority is unavailable"
                    )
                companion_name = (
                    staging_name
                    if staging_exists
                    else quarantine_name if quarantine_exists else None
                )
                if companion_name is not None:
                    _residue_payload, residue_metadata = (
                        self._read_unit_authority_residue_at(
                            state_fd,
                            companion_name,
                            expected_payload=expected_payload,
                            exact=False,
                            allowed_nlinks=frozenset({1}),
                            durable=durable,
                        )
                    )
                    snapshot[companion_name] = residue_metadata
            if durable:
                os.fsync(state_fd)
                self._assert_pinned_runtime()
            self._assert_unit_authority_namespace_snapshot(
                state_fd,
                snapshot,
            )
        finally:
            if close_state:
                os.close(state_fd)

    def _validate_unit_authority_transaction_namespace(
        self,
        transaction: dict[str, object],
        *,
        durable: bool,
    ) -> None:
        phase = transaction.get("phase")
        plan = transaction.get("plan")
        if not isinstance(plan, dict):
            raise PrerequisiteError(
                "unit authority publication lacks a durable plan"
            )
        if phase in {
            "intent",
            "replacement-intent",
            "unit-ready",
            "source-verified",
            "aborted",
        }:
            self._assert_unit_authority_namespace_absent(
                plan.get("authority_publication"),
                str(transaction["operation_id"]),
                durable=durable,
            )
            return
        if phase == "authority-commit-intent":
            self._validate_unit_authority_commit_namespace(
                transaction,
                durable=durable,
                require_final=False,
            )
            return
        if phase == "completed":
            self._validate_unit_authority_commit_namespace(
                transaction,
                durable=durable,
                require_final=True,
            )
            return
        raise PrerequisiteError(
            "unit authority publication has an unknown transaction phase"
        )

    def _unit_transaction_path(self, operation_id: str) -> Path:
        return self.unit_transaction_root / (
            f"{_require_unit_permission_operation_id(operation_id)}.json"
        )

    def _unit_transaction_directory_fd(self, *, create: bool) -> int | None:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is None:
            if not (
                self.unit_transaction_root.exists()
                or self.unit_transaction_root.is_symlink()
            ):
                return None
            return _open_private_directory(self.unit_transaction_root)
        name = UNIT_PERMISSION_TRANSACTION_DIRECTORY.name
        try:
            directory_fd = _open_private_directory(
                Path(name), parent_fd=state_fd
            )
        except PrerequisiteError:
            try:
                os.stat(name, dir_fd=state_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    return None
            else:
                raise
            os.mkdir(name, mode=0o700, dir_fd=state_fd)
            directory_fd = _open_private_directory(
                Path(name), parent_fd=state_fd
            )
        if create:
            try:
                os.fsync(state_fd)
            except Exception:
                os.close(directory_fd)
                raise
        return directory_fd

    @staticmethod
    def _unit_plan_schema_fields(schema_version: int) -> set[str]:
        fields = {
            "schema_version",
            "authority_kind",
            "operation_id",
            "source_sha",
            "source_tree",
            "source_readiness",
            "source_readiness_sha256",
            "delivery_gate",
            "delivery_gate_sha256",
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "adopted_prerequisites_plan_sha256",
            "production_source",
            "adopted_git_permissions_sha256",
            "git_permission_successor",
            "units",
            "authority_publication",
            "unit_permission_impact_sha256",
            "mutations",
        }
        if schema_version == 2:
            fields.add(
                "adopted_git_permission_source_successor_sha256"
            )
        return fields

    @staticmethod
    def _is_sha256_digest(value: object) -> bool:
        return (
            isinstance(value, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
        )

    def _validate_unit_journal_successor_schema(
        self,
        plan: dict[str, object],
        schema_version: int,
    ) -> None:
        """Validate the self-contained successor sealed in any journal."""

        successor = plan.get("git_permission_successor")
        if (
            set(plan) != self._unit_plan_schema_fields(schema_version)
            or not isinstance(successor, dict)
            or not _has_exact_schema_version(successor, schema_version)
        ):
            raise PrerequisiteError(
                "unit permission hardening transaction is invalid"
            )
        identity = successor.get("identity_sha256")
        successor_body = dict(successor)
        successor_body.pop("identity_sha256", None)
        target = successor.get("target")
        files = successor.get("files")
        if (
            not self._is_sha256_digest(identity)
            or identity != _canonical_digest(successor_body)
            or not isinstance(target, dict)
            or set(target) != {"source_sha", "source_tree"}
            or target.get("source_sha") != plan.get("source_sha")
            or target.get("source_tree") != plan.get("source_tree")
            or not isinstance(files, list)
            or successor.get("files_sha256") != _canonical_digest(files)
        ):
            raise PrerequisiteError(
                "unit permission hardening transaction is invalid"
            )
        if schema_version == 1:
            if (
                set(successor)
                != {
                    "schema_version",
                    "policy",
                    "mode",
                    "authority",
                    "target",
                    "files",
                    "files_sha256",
                    "identity_sha256",
                }
                or successor.get("policy")
                != "nexpoly-adopted-git-permission-successor-v1"
                or successor.get("mode")
                not in {"exact-source", "ancestor-byte-identical"}
                or "adopted_git_permission_source_successor_sha256" in plan
            ):
                raise PrerequisiteError(
                    "unit permission hardening transaction is invalid"
                )
            authority = successor.get("authority")
            if (
                not isinstance(authority, dict)
                or set(authority)
                != {"source_sha", "source_tree", "raw_sha256"}
                or authority.get("raw_sha256")
                != plan.get("adopted_git_permissions_sha256")
                or len(files) != len(UNIT_PERMISSION_SUCCESSOR_BLOBS)
            ):
                raise PrerequisiteError(
                    "unit permission hardening transaction is invalid"
                )
            for record, expected_path in zip(
                files,
                UNIT_PERMISSION_SUCCESSOR_BLOBS,
                strict=True,
            ):
                if (
                    not isinstance(record, dict)
                    or set(record) != {"path", "sha256"}
                    or record.get("path") != expected_path
                    or not self._is_sha256_digest(record.get("sha256"))
                ):
                    raise PrerequisiteError(
                        "unit permission hardening transaction is invalid"
                    )
            return

        if (
            set(successor)
            != {
                "schema_version",
                "policy",
                "mode",
                "root_authority",
                "source_successor_authority",
                "target",
                "files",
                "files_sha256",
                "identity_sha256",
            }
            or successor.get("policy")
            != "nexpoly-adopted-git-permission-successor-v2"
            or successor.get("mode") != "protected-main-ci-exact-target"
        ):
            raise PrerequisiteError(
                "unit permission hardening transaction is invalid"
            )
        successor_digest = plan.get(
            "adopted_git_permission_source_successor_sha256"
        )
        root = successor.get("root_authority")
        compact = successor.get("source_successor_authority")
        compact_fields = {
            "schema_version",
            "authority_kind",
            "operation_id",
            "predecessor_authority_sha256",
            "predecessor_source_sha",
            "predecessor_source_tree",
            "predecessor_marker_sha256",
            "target_source_sha",
            "target_source_tree",
            "production_source_sha",
            "production_source_tree",
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "plan_sha256",
            "source_successor_impact_sha256",
            "source_trust_sha256",
            "production_repository_transition",
            "production_repository_transition_sha256",
            "delivery_gate",
            "delivery_gate_sha256",
            "fixed_files",
            "fixed_files_sha256",
            "changed_files",
            "changed_files_sha256",
            "completed_at",
            "authority_file_sha256",
            "identity_sha256",
        }
        if (
            not self._is_sha256_digest(successor_digest)
            or not isinstance(root, dict)
            or set(root) != {"source_sha", "source_tree", "raw_sha256"}
            or root.get("raw_sha256")
            != plan.get("adopted_git_permissions_sha256")
            or not isinstance(compact, dict)
            or set(compact) != compact_fields
            or not _has_exact_schema_version(compact, 1)
            or compact.get("authority_kind")
            != SOURCE_SUCCESSOR_AUTHORITY_KIND
            or compact.get("authority_file_sha256") != successor_digest
            or compact.get("predecessor_authority_sha256")
            != root.get("raw_sha256")
            or compact.get("predecessor_source_sha")
            != root.get("source_sha")
            or compact.get("predecessor_source_tree")
            != root.get("source_tree")
            or compact.get("target_source_sha") != target.get("source_sha")
            or compact.get("target_source_tree")
            != target.get("source_tree")
            or compact.get("delivery_gate") != plan.get("delivery_gate")
            or compact.get("delivery_gate_sha256")
            != plan.get("delivery_gate_sha256")
            or compact.get("adopted_deployment_sha256")
            != plan.get("adopted_deployment_sha256")
            or compact.get("bootstrap_control_sha256")
            != plan.get("bootstrap_control_sha256")
            or compact.get("adopted_prerequisites_sha256")
            != plan.get("adopted_prerequisites_sha256")
            or compact.get("production_repository_transition_sha256")
            != _canonical_digest(
                compact.get("production_repository_transition")
            )
        ):
            raise PrerequisiteError(
                "unit permission hardening transaction is invalid"
            )
        normalized_files = self._source_successor_file_records(files)
        compact_identity = compact.get("identity_sha256")
        compact_body = dict(compact)
        compact_body.pop("identity_sha256", None)
        production = plan.get("production_source")
        if not isinstance(production, dict):
            raise PrerequisiteError(
                "unit permission hardening transaction is invalid"
            )
        repository_transition = (
            self._validate_source_successor_repository_transition(
                compact.get("production_repository_transition"),
                production_sha=str(production.get("source_sha", "")),
                production_tree=str(production.get("source_tree", "")),
                target_sha=str(target.get("source_sha", "")),
                target_tree=str(target.get("source_tree", "")),
                baseline_trust_sha256=str(
                    compact.get("source_trust_sha256", "")
                ),
            )
        )
        transition_logical_refs = {
            str(record["name"]): str(record["object_sha"])
            for record in repository_transition["logical_refs"]
        }
        if (
            files != normalized_files
            or compact.get("fixed_files") != normalized_files
            or compact.get("fixed_files_sha256")
            != _canonical_digest(normalized_files)
            or compact.get("changed_files")
            != list(SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS)
            or compact.get("changed_files_sha256")
            != _canonical_digest(
                list(SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS)
            )
            or not self._is_sha256_digest(compact_identity)
            or compact_identity != _canonical_digest(compact_body)
            or not self._is_sha256_digest(
                compact.get("source_trust_sha256")
            )
            or _parse_canonical_utc_timestamp(compact.get("completed_at"))
            is None
            or compact.get("production_source_sha")
            != production.get("source_sha")
            or compact.get("production_source_tree")
            != production.get("source_tree")
            or transition_logical_refs.get(
                SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF
            )
            != root.get("source_sha")
        ):
            raise PrerequisiteError(
                "unit permission hardening transaction is invalid"
            )

    def _validate_unit_transaction_document(
        self,
        document: dict[str, object],
        operation_id: str,
    ) -> dict[str, object]:
        """Validate one already-read journal without reopening its path."""

        fields = {
            "schema_version",
            "status",
            "phase",
            "operation_id",
            "plan",
            "plan_sha256",
            "unit_permission_impact_sha256",
            "replacement_checkpoint",
            "backup",
            "staging",
            "replacement",
            "unit_evidence",
            "source_trust_sha256",
            "created_at",
            "completed_at",
            "aborted_at",
        }
        schema_version = document.get("schema_version")
        plan = document.get("plan")
        plan_schema = (
            plan.get("schema_version") if isinstance(plan, dict) else None
        )
        if (
            set(document) != fields
            or type(schema_version) is not int
            or schema_version not in {1, 2}
            or type(plan_schema) is not int
            or plan_schema != schema_version
            or document.get("operation_id") != operation_id
            or document.get("status")
            not in {"applying", "completed", "aborted"}
            or document.get("phase") not in UNIT_PERMISSION_TRANSACTION_PHASES
            or not isinstance(plan, dict)
            or document.get("plan_sha256") != _canonical_digest(plan)
            or document.get("unit_permission_impact_sha256")
            != plan.get("unit_permission_impact_sha256")
            or (
                document["status"] == "completed"
                and document["phase"] != "completed"
            )
            or (
                document["status"] == "aborted"
                and document["phase"] != "aborted"
            )
            or (
                document["status"] == "applying"
                and document["phase"] in {"completed", "aborted"}
            )
        ):
            raise PrerequisiteError(
                "unit permission hardening transaction is invalid"
            )
        self._validate_unit_journal_successor_schema(
            plan,
            schema_version,
        )
        checkpoint = document.get("replacement_checkpoint")
        if checkpoint is not None and checkpoint not in {
            "backup-create-intent",
            "backup-ready",
            "staging-create-intent",
            "staged",
            "exchanged",
            "retired-unlinked",
            "hardened",
        }:
            raise PrerequisiteError(
                "unit permission replacement checkpoint is invalid"
            )
        for field in ("backup", "staging", "replacement"):
            value = document.get(field)
            if value is not None and not isinstance(value, dict):
                raise PrerequisiteError(
                    "unit permission journal identity is invalid"
                )
        evidence = document.get("unit_evidence")
        if evidence is not None and not isinstance(evidence, list):
            raise PrerequisiteError(
                "unit permission journal evidence is invalid"
            )
        source_trust = document.get("source_trust_sha256")
        if source_trust is not None and not self._is_sha256_digest(
            source_trust
        ):
            raise PrerequisiteError(
                "unit permission source trust digest is invalid"
            )
        return document

    def _load_unit_transaction(
        self,
        operation_id: str,
    ) -> dict[str, object] | None:
        operation_id = _require_unit_permission_operation_id(operation_id)
        directory_fd = self._unit_transaction_directory_fd(create=False)
        if directory_fd is None:
            return None
        try:
            name = f"{operation_id}.json"
            if not _entry_exists_at(directory_fd, name):
                return None
            document = _load_json_at(
                directory_fd,
                name,
                maximum_bytes=UNIT_PERMISSION_JSON_MAX_BYTES,
            )
        finally:
            os.close(directory_fd)
        return self._validate_unit_transaction_document(
            document,
            operation_id,
        )

    def _write_unit_transaction(self, document: dict[str, object]) -> None:
        if not self._pinned_directories:
            raise PrerequisiteError(
                "unit permission journal requires the deploy lock"
            )
        directory_fd = self._unit_transaction_directory_fd(create=True)
        if directory_fd is None:  # pragma: no cover - create owns
            raise PrerequisiteError(
                "unit permission transaction directory is unavailable"
            )
        try:
            _atomic_owned_json_at(
                directory_fd,
                self._unit_transaction_path(
                    str(document["operation_id"])
                ).name,
                document,
            )
        finally:
            os.close(directory_fd)

    def _reseal_unit_transaction(
        self,
        document: dict[str, object],
    ) -> None:
        """Durably re-publish an exact recovered unit permission journal."""

        if document.get("status") not in {
            "applying",
            "completed",
            "aborted",
        }:
            raise PrerequisiteError(
                "unit permission journal cannot be resealed"
            )
        self._write_unit_transaction(document)
        self.checkpoint("unit-permission-journal-resealed")

    def _unit_authority_exists(self) -> bool:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        return (
            _entry_exists_at(state_fd, UNIT_PERMISSION_AUTHORITY_PATH.name)
            if state_fd is not None
            else self.unit_authority_path.exists()
            or self.unit_authority_path.is_symlink()
        )

    def _load_unit_authority(
        self,
        *,
        require_single_link: bool = True,
    ) -> dict[str, object]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        authority = (
            _load_json_at(
                state_fd,
                UNIT_PERMISSION_AUTHORITY_PATH.name,
                require_single_link=require_single_link,
                maximum_bytes=UNIT_PERMISSION_JSON_MAX_BYTES,
            )
            if state_fd is not None
            else _load_json(
                self.unit_authority_path,
                require_single_link=require_single_link,
                maximum_bytes=UNIT_PERMISSION_JSON_MAX_BYTES,
            )
        )
        backup = authority.get("backup")
        schema_version = authority.get("schema_version")
        if (
            type(schema_version) is not int
            or schema_version not in {1, 2}
            or not _has_exact_schema_version(
                authority.get("plan"), schema_version
            )
            or not _has_exact_schema_version(backup, 1)
            or not _has_exact_schema_version(
                backup.get("owner") if isinstance(backup, dict) else None,
                1,
            )
        ):
            raise PrerequisiteError(
                "unit permission authority schema is invalid"
            )
        return authority

    def _publish_unit_authority(
        self,
        transaction: dict[str, object],
        authority: dict[str, object],
        operation_id: str,
    ) -> None:
        if (
            transaction.get("status") != "applying"
            or transaction.get("phase") != "authority-commit-intent"
            or transaction.get("operation_id") != operation_id
            or authority != self._unit_authority(transaction)
        ):
            raise PrerequisiteError(
                "unit authority publication lacks commit intent"
            )
        self._validate_unit_authority_commit_namespace(
            transaction,
            durable=True,
            require_final=False,
        )
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is None:
            raise PrerequisiteError(
                "unit permission authority requires pinned state"
            )
        _create_owned_json_once_at(
            state_fd,
            UNIT_PERMISSION_AUTHORITY_PATH.name,
            authority,
            operation_id=operation_id,
            checkpoint=self.checkpoint,
            maximum_bytes=UNIT_PERMISSION_JSON_MAX_BYTES,
        )

    def _assert_unit_exclusive(self, operation_id: str) -> None:
        operation_id = _require_unit_permission_operation_id(operation_id)
        if self._unit_authority_exists():
            authority = self._load_unit_authority(require_single_link=False)
            if authority.get("operation_id") != operation_id:
                raise PrerequisiteError(
                    "adopted unit permissions already have another authority"
                )
        directory_fd = self._unit_transaction_directory_fd(create=False)
        if directory_fd is None:
            return
        try:
            for name in sorted(os.listdir(directory_fd)):
                if name.startswith("."):
                    continue
                if not name.endswith(".json"):
                    raise PrerequisiteError(
                        "unit permission transaction inventory has an unknown entry"
                    )
                other = name.removesuffix(".json")
                _require_unit_permission_operation_id(other)
                document = _load_json_at(
                    directory_fd,
                    name,
                    maximum_bytes=UNIT_PERMISSION_JSON_MAX_BYTES,
                )
                try:
                    document = self._validate_unit_transaction_document(
                        document,
                        other,
                    )
                except PrerequisiteError as exc:
                    raise PrerequisiteError(
                        "unit permission transaction inventory is invalid"
                    ) from exc
                if (
                    other != operation_id
                    and document.get("status") == "applying"
                ):
                    raise PrerequisiteError(
                        "another unit permission transaction is active"
                    )
        finally:
            os.close(directory_fd)

    @staticmethod
    def _unit_record_fields() -> set[str]:
        return {
            "role",
            "name",
            "path",
            "parent",
            "type",
            "device",
            "inode",
            "uid",
            "gid",
            "mode",
            "target_mode",
            "nlink",
            "size",
            "content_sha256",
            "action",
            "systemd_state",
            "process_identity",
        }

    def _validate_sealed_unit_records(
        self,
        value: object,
    ) -> list[dict[str, object]]:
        if not isinstance(value, list) or len(value) != 2:
            raise PrerequisiteError(
                "sealed unit permission inventory is invalid"
            )
        result: list[dict[str, object]] = []
        expected = (
            (
                "monomer-md",
                MD_UNIT_NAME,
                self.md_unit_path,
                "0664",
                "atomic-inode-replace",
            ),
            (
                "monomer-dft",
                DFT_UNIT_NAME,
                self.dft_unit_path,
                "0600",
                "no-op-cas",
            ),
        )
        parent_fields = {
            "path",
            "type",
            "device",
            "inode",
            "uid",
            "gid",
            "mode",
            "nlink",
            "size",
        }
        systemd_fields = {
            "LoadState",
            "FragmentPath",
            "DropInPaths",
            "NeedDaemonReload",
            "UnitFileState",
            "ActiveState",
            "SubState",
        }
        for record, (role, name, path, mode, action) in zip(
            value, expected, strict=True
        ):
            if (
                not isinstance(record, dict)
                or set(record) != self._unit_record_fields()
                or record.get("role") != role
                or record.get("name") != name
                or record.get("path") != str(path)
                or record.get("type") != "file"
                or record.get("mode") != mode
                or record.get("target_mode") != "0600"
                or record.get("action") != action
                or record.get("nlink") != 1
                or not isinstance(record.get("size"), int)
                or not 1 <= record["size"] <= 1024 * 1024
                or not isinstance(record.get("content_sha256"), str)
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}", record["content_sha256"]
                )
                is None
            ):
                raise PrerequisiteError(
                    "sealed unit permission record is invalid"
                )
            for field in ("device", "inode", "uid", "gid"):
                field_value = record.get(field)
                if (
                    isinstance(field_value, bool)
                    or not isinstance(field_value, int)
                    or field_value < 0
                ):
                    raise PrerequisiteError(
                        "sealed unit permission identity is invalid"
                    )
            parent = record.get("parent")
            if (
                not isinstance(parent, dict)
                or set(parent) != parent_fields
                or parent.get("path") != str(self.unit_parent)
                or parent.get("type") != "directory"
                or not isinstance(parent.get("mode"), str)
                or re.fullmatch(r"[0-7]{4}", parent["mode"]) is None
                or int(parent["mode"], 8) & 0o022
            ):
                raise PrerequisiteError(
                    "sealed Worker unit parent is invalid"
                )
            for field in ("device", "inode", "uid", "gid", "nlink", "size"):
                field_value = parent.get(field)
                if (
                    isinstance(field_value, bool)
                    or not isinstance(field_value, int)
                    or field_value < 0
                ):
                    raise PrerequisiteError(
                        "sealed Worker unit parent identity is invalid"
                    )
            systemd_state = record.get("systemd_state")
            process = record.get("process_identity")
            if (
                not isinstance(systemd_state, dict)
                or set(systemd_state) != systemd_fields
                or systemd_state.get("LoadState") != "loaded"
                or systemd_state.get("FragmentPath") != str(path)
                or systemd_state.get("DropInPaths") != ""
                or systemd_state.get("NeedDaemonReload") != "no"
                or systemd_state.get("UnitFileState") != "enabled"
                or systemd_state.get("ActiveState") != "active"
                or systemd_state.get("SubState") != "running"
                or not isinstance(process, dict)
                or set(process) != {"main_pid", "invocation_id"}
                or isinstance(process.get("main_pid"), bool)
                or not isinstance(process.get("main_pid"), int)
                or process["main_pid"] <= 0
                or not isinstance(process.get("invocation_id"), str)
                or re.fullmatch(
                    r"[0-9a-f]{32}", process["invocation_id"]
                )
                is None
            ):
                raise PrerequisiteError(
                    "sealed Worker systemd identity is invalid"
                )
            result.append(dict(record))
        if result[0]["parent"] != result[1]["parent"]:
            raise PrerequisiteError(
                "sealed Worker units have different parent identities"
            )
        return result

    def _validate_unit_plan_context(
        self,
        plan: dict[str, object],
        source_sha: str,
        operation_id: str,
        *,
        durable: bool,
    ) -> list[dict[str, object]]:
        schema_version = plan.get("schema_version")
        if type(schema_version) is not int or schema_version not in {1, 2}:
            raise PrerequisiteError(
                "unit permission hardening plan schema changed"
            )
        expected_fields = self._unit_plan_schema_fields(schema_version)
        if (
            set(plan) != expected_fields
            or plan.get("authority_kind") != UNIT_PERMISSION_AUTHORITY_KIND
            or plan.get("operation_id") != operation_id
            or plan.get("source_sha") != source_sha
            or plan.get("mutations")
            != {
                "services_restarted": False,
                "source": False,
                "database": False,
                "credentials": False,
                "md_unit_inode": True,
                "dft_unit": False,
                "runtime_authority": True,
                "systemd_daemon_reload": True,
            }
        ):
            raise PrerequisiteError(
                "unit permission hardening plan context changed"
            )
        sealed_delivery = plan.get("delivery_gate")
        if not isinstance(sealed_delivery, dict):
            raise PrerequisiteError(
                "unit permission delivery authority is invalid"
            )
        source_tree = _validate_source_checkout(self.source_root, source_sha)
        context = self._unit_adoption_context(
            source_sha=source_sha,
            source_tree=source_tree,
        )
        if durable:
            verified_tree, readiness, delivery = self._sealed_source_authority(
                source_sha,
                sealed_readiness=plan.get("source_readiness"),
                sealed_delivery_gate=sealed_delivery,
            )
        else:
            verified_tree, readiness, delivery = self._source_authority(
                source_sha,
                sealed_delivery_gate=dict(sealed_delivery),
            )
        successor = plan.get("git_permission_successor")
        successor_delivery = None
        successor_source_trust = None
        if isinstance(context.get("git_permission_successor"), dict):
            compact_source = context["git_permission_successor"].get(
                "source_successor_authority"
            )
            if isinstance(compact_source, dict):
                successor_delivery = compact_source.get("delivery_gate")
                successor_source_trust = compact_source.get(
                    "source_trust_sha256"
                )
        production_source_trust = (
            self._production_source_trust(plan)
            if schema_version == 2
            else None
        )
        if (
            verified_tree != source_tree
            or plan.get("source_tree") != source_tree
            or plan.get("source_readiness") != readiness
            or plan.get("source_readiness_sha256")
            != _canonical_digest(readiness)
            or plan.get("delivery_gate") != delivery
            or plan.get("delivery_gate_sha256")
            != _canonical_digest(delivery)
            or not _has_exact_schema_version(successor, schema_version)
            or (
                schema_version == 2
                and "adopted_git_permission_source_successor_sha256"
                not in context
            )
            or (schema_version == 2 and successor_delivery != delivery)
            or (
                schema_version == 2
                and successor_source_trust != production_source_trust
            )
            or (
                schema_version == 1
                and self._source_successor_lineage_entries()
            )
            or any(plan.get(field) != value for field, value in context.items())
        ):
            raise PrerequisiteError(
                "unit permission source or predecessor authority changed"
            )
        units = self._validate_sealed_unit_records(plan.get("units"))
        authority_publication = (
            self._validate_unit_authority_publication_plan(
                plan.get("authority_publication"),
                operation_id,
            )
        )
        if plan.get("unit_permission_impact_sha256") != _canonical_digest(
            self._unit_permission_impact(units, authority_publication)
        ):
            raise PrerequisiteError(
                "unit permission impact digest changed"
            )
        return units

    @staticmethod
    def _unit_parent_transition_projection(
        parent: dict[str, object],
    ) -> dict[str, object]:
        """Exclude only filesystem-managed directory allocation size.

        Creating and removing the operation-owned replacement pathname may
        permanently grow an ext4 directory.  Every security-relevant parent
        identity remains stable; ``st_size`` is retained in the before/after
        evidence but is not a valid cross-transition CAS field.
        """

        return {
            key: value
            for key, value in parent.items()
            if key != "size"
        }

    @classmethod
    def _unit_filesystem_projection(
        cls,
        record: dict[str, object],
    ) -> dict[str, object]:
        return {
            "path": record["path"],
            "parent": cls._unit_parent_transition_projection(
                dict(record["parent"])
            ),
            "type": record["type"],
            "device": record["device"],
            "inode": record["inode"],
            "uid": record["uid"],
            "gid": record["gid"],
            "mode": record["mode"],
            "nlink": record["nlink"],
            "size": record["size"],
            "content_sha256": record["content_sha256"],
        }

    @classmethod
    def _unit_transition_projection(
        cls,
        record: dict[str, object],
    ) -> dict[str, object]:
        """Compare a complete unit record while tolerating parent growth."""

        projected = dict(record)
        projected["parent"] = cls._unit_parent_transition_projection(
            dict(record["parent"])
        )
        return projected

    def _validate_live_owned_staging(
        self,
        transaction: dict[str, object],
        original_md: dict[str, object],
    ) -> bool | None:
        """Prove that pre-exchange parent growth has an owned staging inode.

        A create intent is published only after proving both the deterministic
        source and quarantine names absent.  It therefore owns an absent name,
        a safe payload prefix, or a full exact file after a write/fsync fault.
        Merely reaching backup-ready never claims a preplanted staging inode.
        """

        durable_staging = transaction.get("staging")
        staged = (
            transaction.get("replacement_checkpoint") == "staged"
            and isinstance(durable_staging, dict)
        )
        create_intent = (
            transaction.get("replacement_checkpoint")
            == "staging-create-intent"
            and durable_staging is None
        )
        before_create_intent = (
            transaction.get("replacement_checkpoint") == "backup-ready"
            and durable_staging is None
        )
        if (
            transaction.get("phase") != "replacement-intent"
            or transaction.get("replacement") is not None
            or not isinstance(transaction.get("backup"), dict)
            or not (staged or create_intent or before_create_intent)
        ):
            return None
        # The backup and its create-only owner bind this staging window to the
        # exact transaction before parent-size drift is tolerated.
        backup_payload = self._load_backup_payload(transaction)
        owned_fd = self._unit_parent_fd()
        close_parent = owned_fd is None
        parent_fd = (
            _open_private_directory(self.unit_parent)
            if owned_fd is None
            else owned_fd
        )
        staging_name, _retired_name = self._replacement_names(
            str(transaction["operation_id"])
        )
        quarantine_name = self._partial_quarantine_name(
            str(transaction["operation_id"]),
            "unit-staging",
        )
        try:
            try:
                status, record = self._owned_prefix_status_at(
                    parent_fd,
                    staging_name,
                    quarantine_name=quarantine_name,
                    expected_payload=backup_payload,
                    mode=0o600,
                )
            except PrerequisiteError:
                return False
        finally:
            if close_parent:
                os.close(parent_fd)
        if (
            len(backup_payload) != original_md["size"]
            or _digest(backup_payload) != original_md["content_sha256"]
        ):
            return False
        if before_create_intent:
            return None if status == "absent" else False
        if create_intent:
            return bool(
                status in {"absent", "partial", "exact"}
                and (
                    record is None
                    or record["device"] == original_md["device"]
                )
            )
        if status != "exact" or not isinstance(record, dict):
            return False
        observed = {
            "path": str(self.unit_parent / staging_name),
            **record,
        }
        return bool(
            observed["device"] == original_md["device"]
            and observed == durable_staging
        )

    def _validate_original_units(
        self,
        plan: dict[str, object],
    ) -> tuple[list[dict[str, object]], dict[str, bytes]]:
        observed, payloads = self._capture_units(
            md_mode=0o664,
            adopted_digest=str(plan["adopted_deployment_sha256"]),
        )
        if observed != plan["units"]:
            raise PrerequisiteError(
                "adopted Worker unit inventory changed before replacement"
            )
        return observed, payloads

    def _validate_final_units(
        self,
        plan: dict[str, object],
        transaction: dict[str, object],
    ) -> list[dict[str, object]]:
        originals = self._validate_sealed_unit_records(plan["units"])
        observed, _payloads = self._capture_units(
            md_mode=0o600,
            adopted_digest=str(plan["adopted_deployment_sha256"]),
        )
        replacement = transaction.get("replacement")
        if not isinstance(replacement, dict):
            raise PrerequisiteError(
                "unit replacement identity is unavailable"
            )
        md_original, dft_original = originals
        md_observed, dft_observed = observed
        stable_md_fields = {
            "role",
            "name",
            "path",
            "type",
            "uid",
            "gid",
            "target_mode",
            "size",
            "content_sha256",
            "action",
            "systemd_state",
            "process_identity",
        }
        if (
            any(
                md_observed[field] != md_original[field]
                for field in stable_md_fields
            )
            or md_observed["mode"] != "0600"
            or md_observed["nlink"] != 1
            or md_observed["inode"] == md_original["inode"]
            or md_observed["device"] != md_original["device"]
            or replacement.get("device") != md_observed["device"]
            or replacement.get("inode") != md_observed["inode"]
            or replacement.get("content_sha256")
            != md_observed["content_sha256"]
            or replacement.get("mode") != "0600"
            or self._unit_filesystem_projection(dft_observed)
            != self._unit_filesystem_projection(dft_original)
            or dft_observed["systemd_state"]
            != dft_original["systemd_state"]
            or dft_observed["process_identity"]
            != dft_original["process_identity"]
        ):
            raise PrerequisiteError(
                "final Worker unit evidence differs from the sealed transition"
            )
        for field in ("device", "inode", "uid", "gid", "mode", "nlink"):
            if md_observed["parent"][field] != md_original["parent"][field]:
                raise PrerequisiteError(
                    "Worker unit parent changed during replacement"
                )
        return observed

    def _replacement_names(self, operation_id: str) -> tuple[str, str]:
        operation_id = _require_unit_permission_operation_id(operation_id)
        return (
            f".{MD_UNIT_NAME}.{operation_id}.replacement",
            f".{MD_UNIT_NAME}.{operation_id}.retired",
        )

    def _assert_staging_operation_absent(self, operation_id: str) -> None:
        """CAS the complete deterministic staging namespace before intent."""

        parent_fd = self._unit_parent_fd()
        close_parent = parent_fd is None
        if parent_fd is None:
            parent_fd = _open_private_directory(self.unit_parent)
        staging_name, _retired_name = self._replacement_names(operation_id)
        quarantine_name = self._partial_quarantine_name(
            operation_id,
            "unit-staging",
        )
        try:
            if _entry_exists_at(parent_fd, staging_name) or _entry_exists_at(
                parent_fd, quarantine_name
            ):
                raise PrerequisiteError(
                    "unit staging namespace predates operation intent"
                )
            # Absence is the ownership CAS for the deterministic staging name.
            # Seal this namespace before a journal in another directory can
            # make that observation authorize forward replay.
            os.fsync(parent_fd)
        finally:
            if close_parent:
                os.close(parent_fd)

    def _validate_replacement_progress(
        self,
        plan: dict[str, object],
        transaction: dict[str, object],
    ) -> None:
        originals = self._validate_sealed_unit_records(plan["units"])
        live_staging = self._validate_live_owned_staging(
            transaction,
            originals[0],
        )
        try:
            _original_observed, original_payloads = (
                self._validate_original_units(plan)
            )
        except PrerequisiteError:
            pass
        else:
            self._validate_backup_creation_progress(
                operation_id=str(transaction["operation_id"]),
                payload=original_payloads["monomer-md"],
                transaction=transaction,
            )
            if live_staging is False:
                raise PrerequisiteError(
                    "unit replacement staging is not operation-owned"
                )
            return
        try:
            original_observed, original_payloads = self._capture_units(
                md_mode=0o664,
                adopted_digest=str(plan["adopted_deployment_sha256"]),
            )
        except PrerequisiteError:
            original_observed = None
            original_payloads = {}
        if original_observed is not None:
            self._validate_backup_creation_progress(
                operation_id=str(transaction["operation_id"]),
                payload=original_payloads["monomer-md"],
                transaction=transaction,
            )
        if (
            original_observed is not None
            and all(
                self._unit_transition_projection(observed)
                == self._unit_transition_projection(original)
                for observed, original in zip(
                    original_observed,
                    originals,
                    strict=True,
                )
            )
            and live_staging is True
        ):
            return
        observed, _payloads = self._capture_units(
            md_mode=0o600,
            adopted_digest=str(plan["adopted_deployment_sha256"]),
            allow_reload_pending=True,
        )
        md, dft = observed
        replacement = transaction.get("staging") or transaction.get(
            "replacement"
        )
        if (
            not isinstance(replacement, dict)
            or md["device"] != replacement.get("device")
            or md["inode"] != replacement.get("inode")
            or md["content_sha256"] != originals[0]["content_sha256"]
            or md["process_identity"] != originals[0]["process_identity"]
            or {
                key: value
                for key, value in md["systemd_state"].items()
                if key != "NeedDaemonReload"
            }
            != {
                key: value
                for key, value in originals[0]["systemd_state"].items()
                if key != "NeedDaemonReload"
            }
            or self._unit_filesystem_projection(dft)
            != self._unit_filesystem_projection(originals[1])
            or dft["process_identity"] != originals[1]["process_identity"]
        ):
            raise PrerequisiteError(
                "unit replacement progress is not operation-owned"
            )

    def plan(
        self,
        *,
        source_sha: str,
        operation_id: str,
    ) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_unit_permission_operation_id(operation_id)
        self._assert_unit_exclusive(operation_id)
        transaction = self._load_unit_transaction(operation_id)
        if transaction is None:
            if self._unit_authority_exists():
                raise PrerequisiteError(
                    "unit permission authority exists without its transaction"
                )
            return self._unit_plan_result(
                self._unit_source_plan(source_sha, operation_id)
            )
        if transaction["status"] == "aborted":
            raise PrerequisiteError(
                "unit permission hardening operation was aborted"
            )
        plan = dict(transaction["plan"])
        self._validate_unit_plan_context(
            plan,
            source_sha,
            operation_id,
            durable=True,
        )
        self._validate_unit_authority_transaction_namespace(
            transaction,
            durable=False,
        )
        if transaction["phase"] == "intent":
            self._validate_original_units(plan)
        elif transaction["phase"] == "replacement-intent":
            self._validate_replacement_progress(plan, transaction)
        else:
            evidence = self._validate_final_units(plan, transaction)
            if transaction.get("unit_evidence") != evidence:
                raise PrerequisiteError(
                    "durable unit evidence differs from live units"
                )
        if transaction["status"] == "completed":
            if self._load_unit_authority() != self._unit_authority(transaction):
                raise PrerequisiteError(
                    "completed unit permission authority differs"
                )
        return self._unit_plan_result(plan)

    @staticmethod
    def _write_exact_file_at(
        directory_fd: int,
        name: str,
        payload: bytes,
        *,
        mode: int,
    ) -> None:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_fd,
        )
        try:
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)

    @staticmethod
    def _partial_quarantine_name(operation_id: str, label: str) -> str:
        operation_id = _require_unit_permission_operation_id(operation_id)
        if re.fullmatch(r"[a-z0-9-]+", label) is None:
            raise PrerequisiteError("unit partial-residue label is invalid")
        return f".{operation_id}.{label}.partial-quarantine"

    @staticmethod
    def _read_owned_prefix_at(
        directory_fd: int,
        name: str,
        *,
        expected_payload: bytes,
        mode: int,
    ) -> tuple[dict[str, object], bytes]:
        """Read one intent-owned absent/full/partial file without mutation."""

        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise PrerequisiteError(
                f"operation-owned partial file is unavailable: {name}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            payload = _descriptor_bytes(
                descriptor,
                maximum_bytes=max(1, len(expected_payload)),
            )
            after = os.fstat(descriptor)
            observed = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            parent = os.fstat(directory_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != mode
                or before.st_nlink != 1
                or before.st_dev != parent.st_dev
                or before.st_size > len(expected_payload)
                or payload != expected_payload[: len(payload)]
                or _stable_regular_identity(before)
                != _stable_regular_identity(after)
                or _stable_regular_identity(before)
                != _stable_regular_identity(observed)
            ):
                raise PrerequisiteError(
                    f"operation-owned partial file is unsafe: {name}"
                )
            return (
                {
                    "type": "file",
                    "device": before.st_dev,
                    "inode": before.st_ino,
                    "uid": before.st_uid,
                    "gid": before.st_gid,
                    "mode": f"{mode:04o}",
                    "nlink": before.st_nlink,
                    "size": before.st_size,
                    "content_sha256": _digest(payload),
                },
                payload,
            )
        finally:
            os.close(descriptor)

    @classmethod
    def _owned_prefix_status_at(
        cls,
        directory_fd: int,
        name: str,
        *,
        quarantine_name: str,
        expected_payload: bytes,
        mode: int,
    ) -> tuple[str, dict[str, object] | None]:
        source_exists = _entry_exists_at(directory_fd, name)
        quarantine_exists = _entry_exists_at(directory_fd, quarantine_name)
        if source_exists and quarantine_exists:
            raise PrerequisiteError(
                f"operation-owned source and quarantine both exist: {name}"
            )
        if not source_exists and not quarantine_exists:
            return "absent", None
        current_name = quarantine_name if quarantine_exists else name
        record, payload = cls._read_owned_prefix_at(
            directory_fd,
            current_name,
            expected_payload=expected_payload,
            mode=mode,
        )
        if quarantine_exists or payload != expected_payload:
            return "partial", record
        return "exact", record

    def _repair_or_create_exact_file_at(
        self,
        directory_fd: int,
        name: str,
        payload: bytes,
        *,
        mode: int,
        quarantine_name: str,
        allow_residue: bool,
    ) -> dict[str, object]:
        """Forward-converge one file after a durable create intent."""

        status, record = self._owned_prefix_status_at(
            directory_fd,
            name,
            quarantine_name=quarantine_name,
            expected_payload=payload,
            mode=mode,
        )
        if not allow_residue and status != "absent":
            raise PrerequisiteError(
                f"operation-owned file appeared after absence CAS: {name}"
            )
        if status == "partial":
            if not isinstance(record, dict):  # pragma: no cover - validated
                raise PrerequisiteError("partial file identity is unavailable")
            _quarantine_owned_link_at(
                directory_fd,
                name,
                digest=str(record["content_sha256"]),
                mode=mode,
                expected_identity={
                    "device": int(record["device"]),
                    "inode": int(record["inode"]),
                    "mode": mode,
                    "size": int(record["size"]),
                },
                quarantine_name=quarantine_name,
            )
            status = "absent"
        if status == "absent":
            self._write_exact_file_at(
                directory_fd,
                name,
                payload,
                mode=mode,
            )
            record, observed_payload = self._read_owned_prefix_at(
                directory_fd,
                name,
                expected_payload=payload,
                mode=mode,
            )
            if observed_payload != payload:
                raise PrerequisiteError(
                    f"operation-owned file remained partial: {name}"
                )
        if not isinstance(record, dict):  # pragma: no cover - exact owns
            raise PrerequisiteError("operation-owned file identity is unavailable")
        descriptor, _identity_value = _open_exact_at(
            directory_fd,
            name,
            digest=_digest(payload),
            mode=mode,
            expected_identity={
                "device": int(record["device"]),
                "inode": int(record["inode"]),
                "mode": mode,
                "size": len(payload),
            },
        )
        try:
            # Existing exact residue may have survived only in page cache.
            # Re-establish both file and directory durability before sealing it.
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
        return record

    @staticmethod
    def _open_or_create_private_directory_at(
        parent_fd: int,
        name: str,
    ) -> tuple[int, bool]:
        try:
            descriptor = _open_private_directory(
                Path(name), parent_fd=parent_fd
            )
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return descriptor, False
        except PrerequisiteError:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            else:
                raise
        descriptor = _open_private_directory(Path(name), parent_fd=parent_fd)
        os.fsync(descriptor)
        os.fsync(parent_fd)
        return descriptor, True

    def _backup_owner_document(
        self,
        transaction: dict[str, object],
        payload: bytes,
    ) -> dict[str, object]:
        plan = transaction.get("plan")
        if not isinstance(plan, dict):
            raise PrerequisiteError("unit backup lacks a durable plan")
        units = self._validate_sealed_unit_records(plan.get("units"))
        return {
            "schema_version": 1,
            "authority_kind": UNIT_PERMISSION_AUTHORITY_KIND,
            "operation_id": transaction["operation_id"],
            "plan_sha256": transaction["plan_sha256"],
            "md_original_sha256": _canonical_digest(units[0]),
            "content_sha256": _digest(payload),
        }

    def _assert_backup_operation_absent(self, operation_id: str) -> None:
        """Prove the deterministic backup pathname was free before intent."""

        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is None:
            raise PrerequisiteError("unit backup requires pinned state")
        root_name = UNIT_PERMISSION_BACKUP_DIRECTORY.name
        if not _entry_exists_at(state_fd, root_name):
            # When the backup root is absent, its parent is the authority
            # namespace whose absence must precede create intent durably.
            os.fsync(state_fd)
            return
        root_fd = _open_private_directory(Path(root_name), parent_fd=state_fd)
        try:
            claim_name = f".{operation_id}.owner.json"
            claim_quarantine = self._partial_quarantine_name(
                operation_id,
                "backup-claim",
            )
            if (
                _entry_exists_at(root_fd, operation_id)
                or _entry_exists_at(root_fd, claim_name)
                or _entry_exists_at(root_fd, claim_quarantine)
            ):
                raise PrerequisiteError(
                    "unit backup operation path was not created by this transaction"
                )
            # An existing shared backup root owns operation-specific names.
            # Persist both their absence and the root's own parent entry before
            # a journal in another directory can claim them.
            os.fsync(root_fd)
            os.fsync(state_fd)
        finally:
            os.close(root_fd)

    def _validate_backup_creation_progress(
        self,
        *,
        operation_id: str,
        payload: bytes,
        transaction: dict[str, object],
    ) -> None:
        """Read-only proof of every namespace owned by backup-create-intent."""

        if transaction.get("replacement_checkpoint") != "backup-create-intent":
            return
        if any(
            transaction.get(field) is not None
            for field in ("backup", "staging", "replacement")
        ):
            raise PrerequisiteError("unit backup create intent has later evidence")
        owner = self._backup_owner_document(transaction, payload)
        owner_payload = _canonical_bytes(owner) + b"\n"
        root = self.unit_backup_root
        if not (root.exists() or root.is_symlink()):
            return
        root_fd = _open_private_directory(root)
        try:
            claim_name = f".{operation_id}.owner.json"
            claim_status, _claim = self._owned_prefix_status_at(
                root_fd,
                claim_name,
                quarantine_name=self._partial_quarantine_name(
                    operation_id,
                    "backup-claim",
                ),
                expected_payload=owner_payload,
                mode=0o600,
            )
            operation_exists = _entry_exists_at(root_fd, operation_id)
            if not operation_exists:
                return
            if claim_status != "exact":
                raise PrerequisiteError(
                    "unit backup directory lacks a durable exact claim"
                )
            operation_fd = _open_private_directory(
                Path(operation_id), parent_fd=root_fd
            )
            try:
                owner_name = ".owner.json"
                owner_quarantine = self._partial_quarantine_name(
                    operation_id,
                    "backup-owner",
                )
                unit_quarantine = self._partial_quarantine_name(
                    operation_id,
                    "backup-unit",
                )
                allowed = {
                    owner_name,
                    owner_quarantine,
                    MD_UNIT_NAME,
                    unit_quarantine,
                }
                if set(os.listdir(operation_fd)) - allowed:
                    raise PrerequisiteError(
                        "unit backup create intent has an unknown entry"
                    )
                owner_status, _owner = self._owned_prefix_status_at(
                    operation_fd,
                    owner_name,
                    quarantine_name=owner_quarantine,
                    expected_payload=owner_payload,
                    mode=0o600,
                )
                unit_status, _unit = self._owned_prefix_status_at(
                    operation_fd,
                    MD_UNIT_NAME,
                    quarantine_name=unit_quarantine,
                    expected_payload=payload,
                    mode=0o600,
                )
                if unit_status != "absent" and owner_status != "exact":
                    raise PrerequisiteError(
                        "unit backup file lacks a durable exact owner"
                    )
            finally:
                os.close(operation_fd)
        finally:
            os.close(root_fd)

    def _ensure_unit_backup(
        self,
        *,
        operation_id: str,
        payload: bytes,
        transaction: dict[str, object],
        allow_residue: bool,
    ) -> dict[str, object]:
        state_fd = self._pinned_directory_fd(self.runtime_root / "state")
        if state_fd is None:
            raise PrerequisiteError("unit backup requires pinned state")
        if (
            transaction.get("phase") != "replacement-intent"
            or transaction.get("replacement_checkpoint")
            != "backup-create-intent"
            or transaction.get("backup") is not None
            or transaction.get("staging") is not None
            or transaction.get("replacement") is not None
        ):
            raise PrerequisiteError("unit backup lacks its durable create intent")
        owner = self._backup_owner_document(transaction, payload)
        owner_payload = _canonical_bytes(owner) + b"\n"
        owner_sha256 = _canonical_digest(owner)
        backup_root_fd, _backup_root_created = (
            self._open_or_create_private_directory_at(
                state_fd,
                UNIT_PERMISSION_BACKUP_DIRECTORY.name,
            )
        )
        try:
            claim_name = f".{operation_id}.owner.json"
            claim_quarantine = self._partial_quarantine_name(
                operation_id,
                "backup-claim",
            )
            claim_pre_status, _claim_pre_record = (
                self._owned_prefix_status_at(
                    backup_root_fd,
                    claim_name,
                    quarantine_name=claim_quarantine,
                    expected_payload=owner_payload,
                    mode=0o600,
                )
            )
            self._repair_or_create_exact_file_at(
                backup_root_fd,
                claim_name,
                owner_payload,
                mode=0o600,
                quarantine_name=claim_quarantine,
                allow_residue=allow_residue,
            )
            operation_fd, _operation_created = (
                self._open_or_create_private_directory_at(
                    backup_root_fd, operation_id
                )
            )
            if not _operation_created and (
                not allow_residue or claim_pre_status != "exact"
            ):
                os.close(operation_fd)
                raise PrerequisiteError(
                    "unit backup directory appeared before operation ownership"
                )
        finally:
            os.close(backup_root_fd)
        try:
            owner_name = ".owner.json"
            owner_quarantine = self._partial_quarantine_name(
                operation_id,
                "backup-owner",
            )
            unit_quarantine = self._partial_quarantine_name(
                operation_id,
                "backup-unit",
            )
            if set(os.listdir(operation_fd)) - {
                owner_name,
                owner_quarantine,
                MD_UNIT_NAME,
                unit_quarantine,
            }:
                raise PrerequisiteError(
                    "unit backup directory has an unknown entry"
                )
            owner_pre_status, _owner_pre_record = (
                self._owned_prefix_status_at(
                    operation_fd,
                    owner_name,
                    quarantine_name=owner_quarantine,
                    expected_payload=owner_payload,
                    mode=0o600,
                )
            )
            unit_pre_status, _unit_pre_record = (
                self._owned_prefix_status_at(
                    operation_fd,
                    MD_UNIT_NAME,
                    quarantine_name=unit_quarantine,
                    expected_payload=payload,
                    mode=0o600,
                )
            )
            if (
                unit_pre_status != "absent"
                and owner_pre_status != "exact"
            ):
                raise PrerequisiteError(
                    "unit backup file appeared before exact owner"
                )
            self._repair_or_create_exact_file_at(
                operation_fd,
                owner_name,
                owner_payload,
                mode=0o600,
                quarantine_name=owner_quarantine,
                allow_residue=allow_residue,
            )
            name = MD_UNIT_NAME
            unit_record = self._repair_or_create_exact_file_at(
                operation_fd,
                name,
                payload,
                mode=0o600,
                quarantine_name=unit_quarantine,
                allow_residue=allow_residue,
            )
            unit_record = {
                "path": str(
                    self.unit_backup_root / operation_id / MD_UNIT_NAME
                ),
                **unit_record,
            }
            os.fsync(operation_fd)
        finally:
            os.close(operation_fd)
        os.fsync(state_fd)
        operation_path = self.unit_backup_root / operation_id
        if set(path.name for path in operation_path.iterdir()) != {
            ".owner.json",
            MD_UNIT_NAME,
        }:
            raise PrerequisiteError("unit backup inventory differs")
        record: dict[str, object] = {
            "schema_version": 1,
            "operation_id": operation_id,
            "owner": owner,
            "owner_sha256": owner_sha256,
            "claim_path": str(
                self.unit_backup_root / f".{operation_id}.owner.json"
            ),
            "claim_sha256": owner_sha256,
            "unit": unit_record,
            "unit_sha256": _canonical_digest(unit_record),
            "inventory_sha256": _private_tree_inventory_digest(operation_path),
        }
        transaction["backup"] = record
        transaction["replacement_checkpoint"] = "backup-ready"
        self._write_unit_transaction(transaction)
        self.checkpoint("unit-backup-ready")
        return record

    @staticmethod
    def _file_record_matches(
        observed: dict[str, object],
        expected: dict[str, object],
        *,
        include_mode: bool = True,
    ) -> bool:
        fields = {
            "type",
            "device",
            "inode",
            "uid",
            "gid",
            "nlink",
            "size",
            "content_sha256",
        }
        if include_mode:
            fields.add("mode")
        return all(observed.get(field) == expected.get(field) for field in fields)

    def _create_or_validate_staging(
        self,
        *,
        operation_id: str,
        payload: bytes,
        transaction: dict[str, object],
    ) -> dict[str, object]:
        parent_fd = self._unit_parent_fd()
        if parent_fd is None:
            raise PrerequisiteError("Worker unit directory is not pinned")
        staging_name, _retired_name = self._replacement_names(operation_id)
        quarantine_name = self._partial_quarantine_name(
            operation_id,
            "unit-staging",
        )
        durable = transaction.get("staging")
        if durable is None:
            checkpoint = transaction.get("replacement_checkpoint")
            staging_intent_replay = checkpoint == "staging-create-intent"
            if checkpoint == "backup-ready":
                self._assert_staging_operation_absent(operation_id)
                transaction["replacement_checkpoint"] = (
                    "staging-create-intent"
                )
                self._write_unit_transaction(transaction)
                self.checkpoint("unit-staging-create-intent")
            elif checkpoint != "staging-create-intent":
                raise PrerequisiteError(
                    "unit staging lacks its durable create intent"
                )
            record = self._repair_or_create_exact_file_at(
                parent_fd,
                staging_name,
                payload,
                mode=0o600,
                quarantine_name=quarantine_name,
                allow_residue=staging_intent_replay,
            )
        else:
            if transaction.get("replacement_checkpoint") != "staged":
                raise PrerequisiteError(
                    "durable staging has an invalid checkpoint"
                )
            record, observed_payload = self._read_unit_file(
                parent_fd=parent_fd,
                path=self.unit_parent / staging_name,
                mode=0o600,
            )
            if observed_payload != payload:
                raise PrerequisiteError("unit replacement staging differs")
            descriptor, _identity_value = _open_exact_at(
                parent_fd,
                staging_name,
                digest=_digest(payload),
                mode=0o600,
                expected_identity={
                    "device": int(record["device"]),
                    "inode": int(record["inode"]),
                    "mode": 0o600,
                    "size": len(payload),
                },
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)
        staging = {
            "path": str(self.unit_parent / staging_name),
            **record,
        }
        if durable is not None and durable != staging:
            raise PrerequisiteError("durable unit replacement staging differs")
        if durable is None:
            transaction["staging"] = staging
            transaction["replacement_checkpoint"] = "staged"
            self._write_unit_transaction(transaction)
            self.checkpoint("unit-replacement-staged")
        return staging

    def _restore_original_from_retired(
        self,
        *,
        parent_fd: int,
        staging_name: str,
        original: dict[str, object],
        payload: bytes,
    ) -> bool:
        """Restore only an exact sealed original; never swap an unknown inode."""

        if not _entry_exists_at(parent_fd, staging_name):
            return False
        try:
            retired, retired_payload = self._read_unit_file(
                parent_fd=parent_fd,
                path=self.unit_parent / staging_name,
                mode=0o664,
            )
        except PrerequisiteError:
            return False
        if (
            retired_payload != payload
            or not self._file_record_matches(retired, original)
        ):
            return False
        retired_fd, _retired_identity = _open_exact_at(
            parent_fd,
            staging_name,
            digest=str(original["content_sha256"]),
            mode=0o664,
            expected_identity={
                "device": int(original["device"]),
                "inode": int(original["inode"]),
                "mode": 0o664,
                "size": int(original["size"]),
            },
        )
        try:
            _rename_exchange(
                parent_fd,
                staging_name,
                parent_fd,
                MD_UNIT_NAME,
            )
            os.fsync(parent_fd)
            restored, restored_payload = self._read_unit_file(
                parent_fd=parent_fd,
                path=self.md_unit_path,
                mode=0o664,
            )
            if (
                restored_payload != payload
                or not self._file_record_matches(restored, original)
                or _stable_regular_identity(os.fstat(retired_fd))
                != _stable_regular_identity(
                    os.stat(
                        MD_UNIT_NAME,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                )
            ):
                raise PrerequisiteError(
                    "atomic unit recovery lost its original-inode CAS"
                )
        finally:
            os.close(retired_fd)
        return True

    def _exchange_md_unit(
        self,
        *,
        operation_id: str,
        plan: dict[str, object],
        payload: bytes,
        transaction: dict[str, object],
    ) -> dict[str, object]:
        parent_fd = self._unit_parent_fd()
        if parent_fd is None:
            raise PrerequisiteError("Worker unit directory is not pinned")
        original = self._validate_sealed_unit_records(plan["units"])[0]
        staging_name, _retired_name = self._replacement_names(operation_id)
        replacement = transaction.get("replacement")
        exchange_cas_lost = False
        target_metadata = os.stat(
            MD_UNIT_NAME,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        target_mode = stat.S_IMODE(target_metadata.st_mode)
        staging_exists = _entry_exists_at(parent_fd, staging_name)
        if target_mode == 0o664:
            if replacement is not None:
                raise PrerequisiteError(
                    "unit replacement regressed after durable exchange"
                )
            current, current_payload = self._read_unit_file(
                parent_fd=parent_fd,
                path=self.md_unit_path,
                mode=0o664,
            )
            if (
                current_payload != payload
                or not self._file_record_matches(current, original)
            ):
                raise PrerequisiteError(
                    "MD unit changed before atomic exchange"
                )
            staging = self._create_or_validate_staging(
                operation_id=operation_id,
                payload=payload,
                transaction=transaction,
            )
            original_identity = {
                "device": int(original["device"]),
                "inode": int(original["inode"]),
                "mode": 0o664,
                "size": int(original["size"]),
            }
            staging_identity = {
                "device": int(staging["device"]),
                "inode": int(staging["inode"]),
                "mode": 0o600,
                "size": int(staging["size"]),
            }
            original_fd, _observed_original = _open_exact_at(
                parent_fd,
                MD_UNIT_NAME,
                digest=str(original["content_sha256"]),
                mode=0o664,
                expected_identity=original_identity,
            )
            try:
                staging_fd, _observed_staging = _open_exact_at(
                    parent_fd,
                    staging_name,
                    digest=str(staging["content_sha256"]),
                    mode=0o600,
                    expected_identity=staging_identity,
                )
                try:
                    _rename_exchange(
                        parent_fd,
                        staging_name,
                        parent_fd,
                        MD_UNIT_NAME,
                    )
                    os.fsync(parent_fd)
                    canonical_after = os.stat(
                        MD_UNIT_NAME,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    retired_after = os.stat(
                        staging_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    exchange_cas_lost = (
                        _stable_regular_identity(canonical_after)
                        != _stable_regular_identity(os.fstat(staging_fd))
                        or _stable_regular_identity(retired_after)
                        != _stable_regular_identity(os.fstat(original_fd))
                    )
                finally:
                    os.close(staging_fd)
            finally:
                os.close(original_fd)
            target_mode = 0o600
            staging_exists = True
        elif target_mode != 0o600:
            raise PrerequisiteError("MD unit has an unknown replacement mode")

        try:
            target, target_payload = self._read_unit_file(
                parent_fd=parent_fd,
                path=self.md_unit_path,
                mode=0o600,
            )
        except PrerequisiteError as exc:
            self._restore_original_from_retired(
                parent_fd=parent_fd,
                staging_name=staging_name,
                original=original,
                payload=payload,
            )
            raise PrerequisiteError(
                "atomic unit exchange published an unsafe target"
            ) from exc
        durable_staging = transaction.get("staging")
        if (
            not isinstance(durable_staging, dict)
            or target_payload != payload
            or not self._file_record_matches(target, durable_staging)
        ):
            if staging_exists:
                # Restore only when the other side is still the exact sealed
                # original.  Blindly exchanging an unknown staging name would
                # publish a foreign inode at the canonical unit path.
                self._restore_original_from_retired(
                    parent_fd=parent_fd,
                    staging_name=staging_name,
                    original=original,
                    payload=payload,
                )
            raise PrerequisiteError(
                "atomic unit exchange published a foreign target"
            )
        if exchange_cas_lost:
            # The canonical side is nevertheless the exact replacement.  Keep
            # it in place and fail closed; the retired side is checked below
            # and is never exchanged back over a correct canonical path.
            raise PrerequisiteError(
                "atomic unit exchange lost its pinned-inode CAS"
            )
        if staging_exists:
            retired, retired_payload = self._read_unit_file(
                parent_fd=parent_fd,
                path=self.unit_parent / staging_name,
                mode=0o664,
            )
            if (
                retired_payload != payload
                or not self._file_record_matches(retired, original)
            ):
                # Canonical is already the exact replacement.  Keep it safe;
                # never publish an unrecognized retired pathname over it.
                raise PrerequisiteError(
                    "atomic unit exchange lost its original-inode CAS"
                )
        replacement_record: dict[str, object] = {
            "path": str(self.md_unit_path),
            **target,
        }
        if replacement is not None and replacement != replacement_record:
            raise PrerequisiteError("durable MD unit replacement differs")
        if replacement is None:
            # A replay may observe the exchange before its journal write.
            # Re-establish directory-entry durability before advancing it.
            os.fsync(parent_fd)
            transaction["replacement"] = replacement_record
            transaction["replacement_checkpoint"] = "exchanged"
            self._write_unit_transaction(transaction)
            self.checkpoint("unit-replacement-exchanged")
        if staging_exists:
            os.unlink(staging_name, dir_fd=parent_fd)
        # The retired name may already be absent after a lost journal write;
        # fsync again in both paths before sealing retired-unlinked.
        os.fsync(parent_fd)
        transaction["replacement_checkpoint"] = "retired-unlinked"
        self._write_unit_transaction(transaction)
        self.checkpoint("unit-retired-unlinked")
        return replacement_record

    def _load_backup_payload(
        self,
        transaction: dict[str, object],
    ) -> bytes:
        backup = transaction.get("backup")
        if (
            not isinstance(backup, dict)
            or set(backup)
            != {
                "schema_version",
                "operation_id",
                "owner",
                "owner_sha256",
                "claim_path",
                "claim_sha256",
                "unit",
                "unit_sha256",
                "inventory_sha256",
            }
            or not _has_exact_schema_version(backup, 1)
            or backup.get("operation_id") != transaction.get("operation_id")
            or not isinstance(backup.get("owner"), dict)
            or not _has_exact_schema_version(backup.get("owner"), 1)
            or not isinstance(backup.get("unit"), dict)
            or backup.get("owner_sha256")
            != _canonical_digest(backup.get("owner"))
            or backup.get("claim_sha256") != backup.get("owner_sha256")
            or backup.get("unit_sha256")
            != _canonical_digest(backup.get("unit"))
        ):
            raise PrerequisiteError("unit backup authority is unavailable")
        # The expected owner is recomputed below once the actual backup bytes
        # have been read.  This preliminary shape check intentionally does not
        # trust the owner-provided content digest.
        unit = dict(backup["unit"])
        expected_path = (
            self.unit_backup_root
            / str(transaction["operation_id"])
            / MD_UNIT_NAME
        )
        if unit.get("path") != str(expected_path):
            raise PrerequisiteError("unit backup path differs")
        operation_path = expected_path.parent
        expected_claim = (
            self.unit_backup_root
            / f".{transaction['operation_id']}.owner.json"
        )
        observed_claim = _load_json(expected_claim)
        if (
            backup.get("claim_path") != str(expected_claim)
            or not _has_exact_schema_version(observed_claim, 1)
            or observed_claim != backup["owner"]
            or
            set(path.name for path in operation_path.iterdir())
            != {".owner.json", MD_UNIT_NAME}
            or backup.get("inventory_sha256")
            != _private_tree_inventory_digest(operation_path)
        ):
            raise PrerequisiteError("unit backup inventory changed")
        owner = _load_json(operation_path / ".owner.json")
        if (
            not _has_exact_schema_version(owner, 1)
            or owner != backup["owner"]
        ):
            raise PrerequisiteError("unit backup owner authority changed")
        descriptor, _noatime = _open_readonly_noatime(expected_path)
        try:
            before = os.fstat(descriptor)
            payload = _descriptor_bytes(
                descriptor,
                maximum_bytes=1024 * 1024,
            )
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        observed = {
            "path": str(expected_path),
            "type": "file" if stat.S_ISREG(before.st_mode) else "other",
            "device": before.st_dev,
            "inode": before.st_ino,
            "uid": before.st_uid,
            "gid": before.st_gid,
            "mode": f"{stat.S_IMODE(before.st_mode):04o}",
            "nlink": before.st_nlink,
            "size": before.st_size,
            "content_sha256": _digest(payload),
        }
        if (
            _stable_regular_identity(before) != _stable_regular_identity(after)
            or observed != unit
        ):
            raise PrerequisiteError("private Worker unit backup changed")
        expected_owner = self._backup_owner_document(transaction, payload)
        if (
            owner != expected_owner
            or backup["owner_sha256"] != _canonical_digest(expected_owner)
            or backup["unit_sha256"] != _canonical_digest(observed)
        ):
            raise PrerequisiteError("unit backup evidence changed")
        return payload

    def _unit_authority(
        self,
        transaction: dict[str, object],
    ) -> dict[str, object]:
        plan = transaction["plan"]
        production = plan["production_source"]
        successor = plan["git_permission_successor"]
        backup = transaction["backup"]
        original_units = plan["units"]
        hardened_units = transaction["unit_evidence"]
        if (
            not isinstance(original_units, list)
            or not isinstance(hardened_units, list)
            or not isinstance(backup, dict)
        ):
            raise PrerequisiteError(
                "unit permission authority evidence is incomplete"
            )
        schema_version = int(plan["schema_version"])
        root_successor = (
            successor["root_authority"]
            if schema_version == 2
            else successor["authority"]
        )
        authority: dict[str, object] = {
            "schema_version": schema_version,
            "status": "completed",
            "authority_kind": UNIT_PERMISSION_AUTHORITY_KIND,
            "operation_id": transaction["operation_id"],
            "source_sha": plan["source_sha"],
            "source_tree": plan["source_tree"],
            "production_source_sha": production["source_sha"],
            "production_source_tree": production["source_tree"],
            "adopted_deployment_sha256": plan[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": plan[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": plan[
                "adopted_prerequisites_sha256"
            ],
            "adopted_git_permissions_sha256": plan[
                "adopted_git_permissions_sha256"
            ],
            "adopted_git_permission_source_sha": root_successor[
                "source_sha"
            ],
            "adopted_git_permission_source_tree": root_successor[
                "source_tree"
            ],
            "plan_sha256": transaction["plan_sha256"],
            "unit_permission_impact_sha256": transaction[
                "unit_permission_impact_sha256"
            ],
            "original_units": original_units,
            "original_units_sha256": _canonical_digest(original_units),
            "hardened_units": hardened_units,
            "hardened_units_sha256": _canonical_digest(hardened_units),
            "backup": backup,
            "backup_sha256": _canonical_digest(backup),
            "backup_content_sha256": backup["unit"]["content_sha256"],
            "plan": plan,
            "completed_at": transaction["completed_at"],
        }
        if schema_version == 2:
            authority[
                "adopted_git_permission_source_successor_sha256"
            ] = plan[
                "adopted_git_permission_source_successor_sha256"
            ]
        return authority

    @staticmethod
    def _unit_source_successor_trust_digest(
        plan: dict[str, object],
    ) -> str | None:
        if plan.get("schema_version") != 2:
            return None
        successor = plan.get("git_permission_successor")
        compact = (
            successor.get("source_successor_authority")
            if isinstance(successor, dict)
            else None
        )
        digest = (
            compact.get("source_trust_sha256")
            if isinstance(compact, dict)
            else None
        )
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise PrerequisiteError(
                "unit source successor trust digest is invalid"
            )
        return digest

    def _revalidate_unit_commit_evidence(
        self,
        transaction: dict[str, object],
        plan: dict[str, object],
    ) -> None:
        self._assert_unit_paths_pinned()
        self._validate_unit_plan_context(
            plan,
            str(plan["source_sha"]),
            str(plan["operation_id"]),
            durable=True,
        )
        evidence = self._validate_final_units(plan, transaction)
        source_trust = self._production_source_trust(plan)
        successor_source_trust = self._unit_source_successor_trust_digest(
            plan
        )
        if (
            evidence != transaction.get("unit_evidence")
            or source_trust != transaction.get("source_trust_sha256")
            or (
                successor_source_trust is not None
                and source_trust != successor_source_trust
            )
        ):
            raise PrerequisiteError(
                "unit permission commit evidence changed"
            )
        self._load_backup_payload(transaction)
        self._assert_unit_paths_pinned()

    def apply(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_unit_permission_impact_sha256: str,
    ) -> dict[str, object]:
        planned = self.plan(
            source_sha=source_sha,
            operation_id=operation_id,
        )
        if planned["plan_sha256"] != confirm_plan_sha256:
            raise PrerequisiteError(
                "unit permission hardening plan confirmation differs"
            )
        if (
            planned["unit_permission_impact_sha256"]
            != confirm_unit_permission_impact_sha256
        ):
            raise PrerequisiteError(
                "unit permission hardening impact confirmation differs"
            )
        with self._deployment_lock():
            self.checkpoint("unit-permission-apply-lock-acquired")
            self._assert_unit_paths_pinned()
            self._assert_unit_exclusive(operation_id)
            transaction = self._load_unit_transaction(operation_id)
            recovered_applying = (
                transaction is not None
                and transaction.get("status") == "applying"
            )
            if transaction is None:
                locked_plan = self._unit_source_plan(
                    source_sha,
                    operation_id,
                )
                if (
                    locked_plan != planned["plan"]
                    or _canonical_digest(locked_plan)
                    != confirm_plan_sha256
                    or locked_plan["unit_permission_impact_sha256"]
                    != confirm_unit_permission_impact_sha256
                ):
                    raise PrerequisiteError(
                        "unit permission plan changed before locked apply"
                    )
                # Preserve the only abortable boundary: a preplanted backup
                # or staging name must fail before this operation owns any
                # durable transaction record.
                self._assert_unit_authority_namespace_absent(
                    locked_plan["authority_publication"],
                    operation_id,
                    durable=True,
                )
                self._assert_backup_operation_absent(operation_id)
                self._assert_staging_operation_absent(operation_id)
                transaction = {
                    "schema_version": locked_plan["schema_version"],
                    "status": "applying",
                    "phase": "intent",
                    "operation_id": operation_id,
                    "plan": locked_plan,
                    "plan_sha256": confirm_plan_sha256,
                    "unit_permission_impact_sha256": (
                        confirm_unit_permission_impact_sha256
                    ),
                    "replacement_checkpoint": None,
                    "backup": None,
                    "staging": None,
                    "replacement": None,
                    "unit_evidence": None,
                    "source_trust_sha256": None,
                    "created_at": _utc_now(),
                    "completed_at": None,
                    "aborted_at": None,
                }
                self._write_unit_transaction(transaction)
                self.checkpoint("unit-permission-intent")
            if transaction["status"] == "aborted":
                raise PrerequisiteError(
                    "unit permission hardening operation was aborted"
                )
            if (
                transaction["plan_sha256"] != confirm_plan_sha256
                or transaction["unit_permission_impact_sha256"]
                != confirm_unit_permission_impact_sha256
            ):
                raise PrerequisiteError(
                    "durable unit permission plan differs"
                )
            plan = dict(transaction["plan"])
            self._validate_unit_plan_context(
                plan,
                source_sha,
                operation_id,
                durable=True,
            )
            self._validate_unit_authority_transaction_namespace(
                transaction,
                durable=True,
            )
            if transaction["status"] == "completed":
                authority = self._load_unit_authority()
                if authority != self._unit_authority(transaction):
                    raise PrerequisiteError(
                        "completed unit permission authority differs"
                    )
                self._revalidate_unit_commit_evidence(transaction, plan)
                self._reseal_unit_transaction(transaction)
                return authority
            if transaction["phase"] == "authority-commit-intent":
                self._revalidate_unit_commit_evidence(transaction, plan)
                if recovered_applying:
                    self._reseal_unit_transaction(transaction)
                authority = self._unit_authority(transaction)
                self._publish_unit_authority(
                    transaction,
                    authority,
                    operation_id,
                )
                self._validate_unit_authority_commit_namespace(
                    transaction,
                    durable=True,
                    require_final=True,
                )
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_unit_transaction(transaction)
                return authority
            if recovered_applying:
                # A journal rename may have been visible when its parent
                # fsync response was lost. Re-publish the exact recovered
                # document before its phase/checkpoint authorizes any replay
                # side effect, so another power loss cannot expose an older
                # namespace entry behind newer filesystem mutations.
                self._reseal_unit_transaction(transaction)
            if transaction["phase"] == "intent":
                self._validate_original_units(plan)
                self._assert_unit_authority_namespace_absent(
                    plan["authority_publication"],
                    operation_id,
                    durable=True,
                )
                self._assert_backup_operation_absent(operation_id)
                self._assert_staging_operation_absent(operation_id)
                transaction["phase"] = "replacement-intent"
                self._write_unit_transaction(transaction)
                self.checkpoint("unit-replacement-intent")
            if transaction["phase"] == "replacement-intent":
                if transaction.get("backup") is None:
                    backup_intent_replay = (
                        transaction.get("replacement_checkpoint")
                        == "backup-create-intent"
                    )
                    if transaction.get("replacement_checkpoint") is None:
                        # This is the last CAS before any deterministic backup
                        # pathname may be created or repaired.
                        self._assert_backup_operation_absent(operation_id)
                        transaction["replacement_checkpoint"] = (
                            "backup-create-intent"
                        )
                        self._write_unit_transaction(transaction)
                        self.checkpoint("unit-backup-create-intent")
                    elif (
                        transaction.get("replacement_checkpoint")
                        != "backup-create-intent"
                    ):
                        raise PrerequisiteError(
                            "unit backup journal lacks create intent"
                        )
                    _originals, payloads = self._validate_original_units(plan)
                    payload = payloads["monomer-md"]
                    self._ensure_unit_backup(
                        operation_id=operation_id,
                        payload=payload,
                        transaction=transaction,
                        allow_residue=backup_intent_replay,
                    )
                else:
                    payload = self._load_backup_payload(transaction)
                if _digest(payload) != plan["units"][0]["content_sha256"]:
                    raise PrerequisiteError(
                        "unit backup differs from the sealed MD unit"
                    )
                self._assert_unit_authority_namespace_absent(
                    plan["authority_publication"],
                    operation_id,
                    durable=True,
                )
                self._exchange_md_unit(
                    operation_id=operation_id,
                    plan=plan,
                    payload=payload,
                    transaction=transaction,
                )
                self.daemon_reload()
                self.checkpoint("unit-daemon-reloaded")
                evidence = self._validate_final_units(plan, transaction)
                transaction["unit_evidence"] = evidence
                transaction["replacement_checkpoint"] = "hardened"
                transaction["phase"] = "unit-ready"
                self._write_unit_transaction(transaction)
                self.checkpoint("unit-permission-ready")
            if transaction["phase"] == "unit-ready":
                evidence = self._validate_final_units(plan, transaction)
                if evidence != transaction.get("unit_evidence"):
                    raise PrerequisiteError(
                        "unit permission evidence changed before source verification"
                    )
                source_trust = self._production_source_trust(plan)
                successor_source_trust = (
                    self._unit_source_successor_trust_digest(plan)
                )
                if (
                    successor_source_trust is not None
                    and source_trust != successor_source_trust
                ):
                    raise PrerequisiteError(
                        "production source differs from successor authority"
                    )
                transaction["source_trust_sha256"] = source_trust
                transaction["phase"] = "source-verified"
                self._write_unit_transaction(transaction)
                self.checkpoint("unit-permission-source-verified")
            if transaction["phase"] == "source-verified":
                evidence = self._validate_final_units(plan, transaction)
                if (
                    evidence != transaction.get("unit_evidence")
                    or self._production_source_trust(plan)
                    != transaction.get("source_trust_sha256")
                ):
                    raise PrerequisiteError(
                        "unit permission evidence changed before commit"
                    )
                self._assert_unit_authority_namespace_absent(
                    plan["authority_publication"],
                    operation_id,
                    durable=True,
                )
                transaction["phase"] = "authority-commit-intent"
                transaction["completed_at"] = _utc_now()
                self._write_unit_transaction(transaction)
                self.checkpoint("unit-permission-authority-commit-intent")
                self._revalidate_unit_commit_evidence(transaction, plan)
                authority = self._unit_authority(transaction)
                self._publish_unit_authority(
                    transaction,
                    authority,
                    operation_id,
                )
                self._validate_unit_authority_commit_namespace(
                    transaction,
                    durable=True,
                    require_final=True,
                )
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_unit_transaction(transaction)
                return authority
            raise PrerequisiteError(
                "unit permission transaction is in an unknown phase"
            )

    def abort(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_unit_permission_impact_sha256: str,
    ) -> dict[str, object]:
        _require_sha(source_sha)
        _require_unit_permission_operation_id(operation_id)
        with self._deployment_lock():
            self.checkpoint("unit-permission-abort-lock-acquired")
            self._assert_unit_paths_pinned()
            self._assert_unit_exclusive(operation_id)
            transaction = self._load_unit_transaction(operation_id)
            if transaction is None:
                raise PrerequisiteError(
                    "unit permission transaction is unavailable"
                )
            if (
                transaction["plan_sha256"] != confirm_plan_sha256
                or transaction["unit_permission_impact_sha256"]
                != confirm_unit_permission_impact_sha256
            ):
                raise PrerequisiteError(
                    "unit permission abort confirmation differs"
                )
            plan = dict(transaction["plan"])
            self._validate_unit_plan_context(
                plan,
                source_sha,
                operation_id,
                durable=True,
            )
            self._validate_unit_authority_transaction_namespace(
                transaction,
                durable=True,
            )
            if (
                transaction["status"] != "aborted"
                and transaction["phase"] != "intent"
            ):
                raise PrerequisiteError(
                    "unit replacement intent is forward-only and cannot be aborted"
                )
            self._validate_original_units(plan)
            self._assert_backup_operation_absent(operation_id)
            self._assert_staging_operation_absent(operation_id)
            if (
                transaction.get("backup") is not None
                or transaction.get("staging") is not None
                or transaction.get("replacement") is not None
                or transaction.get("replacement_checkpoint") is not None
            ):
                raise PrerequisiteError(
                    "unit permission intent has unexpected mutation residue"
                )
            self._reseal_unit_transaction(transaction)
            if transaction["status"] == "aborted":
                return transaction
            transaction["status"] = "aborted"
            transaction["phase"] = "aborted"
            transaction["aborted_at"] = _utc_now()
            self._write_unit_transaction(transaction)
            return transaction


def _parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "abort"):
        command = commands.add_parser(name)
        command.add_argument("--sha", required=True)
        command.add_argument("--operation-id", required=True)
        if name in {"apply", "abort"}:
            command.add_argument("--confirm-plan-sha256", required=True)
    for name in (
        "permission-plan",
        "permission-apply",
        "permission-abort",
    ):
        command = commands.add_parser(name)
        command.add_argument("--sha", required=True)
        command.add_argument("--operation-id", required=True)
        if name in {"permission-apply", "permission-abort"}:
            command.add_argument("--confirm-plan-sha256", required=True)
            command.add_argument(
                "--confirm-permission-impact-sha256",
                required=True,
            )
    for name in (
        "unit-permission-plan",
        "unit-permission-apply",
        "unit-permission-abort",
    ):
        command = commands.add_parser(name)
        command.add_argument("--sha", required=True)
        command.add_argument("--operation-id", required=True)
        if name in {"unit-permission-apply", "unit-permission-abort"}:
            command.add_argument("--confirm-plan-sha256", required=True)
            command.add_argument(
                "--confirm-unit-permission-impact-sha256",
                required=True,
            )
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        if args.command.startswith("unit-permission-"):
            unit_installer = UnitPermissionHardeningInstaller(
                REPOSITORY_ROOT,
                RUNTIME_ROOT,
            )
            if args.command == "unit-permission-plan":
                result = unit_installer.plan(
                    source_sha=args.sha,
                    operation_id=args.operation_id,
                )
            elif args.command == "unit-permission-apply":
                result = unit_installer.apply(
                    source_sha=args.sha,
                    operation_id=args.operation_id,
                    confirm_plan_sha256=args.confirm_plan_sha256,
                    confirm_unit_permission_impact_sha256=(
                        args.confirm_unit_permission_impact_sha256
                    ),
                )
            else:
                result = unit_installer.abort(
                    source_sha=args.sha,
                    operation_id=args.operation_id,
                    confirm_plan_sha256=args.confirm_plan_sha256,
                    confirm_unit_permission_impact_sha256=(
                        args.confirm_unit_permission_impact_sha256
                    ),
                )
        elif args.command.startswith("permission-"):
            permission_installer = PermissionHardeningInstaller(
                REPOSITORY_ROOT,
                RUNTIME_ROOT,
            )
            if args.command == "permission-plan":
                result = permission_installer.plan(
                    source_sha=args.sha,
                    operation_id=args.operation_id,
                )
            elif args.command == "permission-apply":
                result = permission_installer.apply(
                    source_sha=args.sha,
                    operation_id=args.operation_id,
                    confirm_plan_sha256=args.confirm_plan_sha256,
                    confirm_permission_impact_sha256=(
                        args.confirm_permission_impact_sha256
                    ),
                )
            else:
                result = permission_installer.abort(
                    source_sha=args.sha,
                    operation_id=args.operation_id,
                    confirm_plan_sha256=args.confirm_plan_sha256,
                    confirm_permission_impact_sha256=(
                        args.confirm_permission_impact_sha256
                    ),
                )
        elif args.command == "plan":
            installer = PrerequisiteInstaller(REPOSITORY_ROOT, RUNTIME_ROOT)
            result = installer.plan(
                source_sha=args.sha, operation_id=args.operation_id
            )
        elif args.command == "apply":
            installer = PrerequisiteInstaller(REPOSITORY_ROOT, RUNTIME_ROOT)
            result = installer.apply(
                source_sha=args.sha,
                operation_id=args.operation_id,
                confirm_plan_sha256=args.confirm_plan_sha256,
            )
        else:
            installer = PrerequisiteInstaller(REPOSITORY_ROOT, RUNTIME_ROOT)
            result = installer.abort(
                source_sha=args.sha,
                operation_id=args.operation_id,
                confirm_plan_sha256=args.confirm_plan_sha256,
            )
    except (PrerequisiteError, OSError, subprocess.SubprocessError) as exc:
        print(f"adopt-prerequisites: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
