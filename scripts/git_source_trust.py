#!/usr/bin/env python3
"""Fail-closed Git execution and content-bound production source evidence.

The production checkout is a deployment input, not a developer convenience.
Every caller must therefore use the same explicit Git directory, work tree,
index and object database, while system/global config and ambient Git
redirection are excluded.  The evidence emitted here binds the interpreted
local config, index, refs and object-store topology to the commit/tree that a
caller accepted.

This module intentionally uses only the Python standard library so it can be
installed in both bootstrap recovery tools and content-addressed controls.
"""

from __future__ import annotations

import configparser
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
from typing import Any, Callable, Mapping


GIT_BINARY = Path("/usr/bin/git")
POLICY_NAME = "nexpoly-production-git-source-v1"
SCHEMA_VERSION = 1
PERMISSION_POLICY_NAME = "nexpoly-production-git-permission-takeover-v1"
PERMISSION_SCHEMA_VERSION = 1
PERMISSION_MARKER_RELATIVE_PATH = Path(
    "state/legacy-git-permission-takeover.json"
)
PERMISSION_MARKER_MAX_BYTES = 128 * 1024 * 1024
PERMISSION_HISTORY_MAX_BYTES = 512 * 1024 * 1024
PERMISSION_HISTORY_FREE_MARGIN_BYTES = 64 * 1024 * 1024
PERMISSION_TAKEOVER_PHASE_SEQUENCE = (
    "captured",
    "root-intent",
    "root-hardened",
    "metadata-directories-intent",
    "metadata-directories-hardened",
    "metadata-files-intent",
    "metadata-files-hardened",
    "hardened",
)
PERMISSION_RESTORE_PHASE_SEQUENCE = (
    "restore-files-intent",
    "restore-files-restored",
    "restore-directories-intent",
    "restore-directories-restored",
    "restore-root-intent",
    "restored",
)
PERMISSION_LIFECYCLE_PHASE_SEQUENCE = (
    *PERMISSION_TAKEOVER_PHASE_SEQUENCE,
    *PERMISSION_RESTORE_PHASE_SEQUENCE,
)
PERMISSION_PHASES = frozenset(PERMISSION_LIFECYCLE_PHASE_SEQUENCE)
MAX_CONFIG_BYTES = 1024 * 1024
MAX_CONTROL_FILE_BYTES = 64 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RENAME_NOREPLACE = 1

# These values can redirect repository discovery, object reads, config,
# identity, hooks/helpers, or the binary implementation before our command
# line policy takes effect.  Harmless presentation values such as GIT_PAGER
# are not trusted either; they are simply omitted from the child environment.
FORBIDDEN_AMBIENT_EXACT = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_WORK_TREE",
        "GIT_CEILING_DIRECTORIES",
        "SSH_ASKPASS",
    }
)
FORBIDDEN_AMBIENT_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_TRACE",
)

# Local config is data in the evidence, never an extension point.  In
# particular, include/includeIf, extensions, partial clone, alternates,
# fsmonitor, sparse checkout, hooks, filters, credentials and external work
# trees have no permitted representation.
ALLOWED_CONFIG: dict[str, frozenset[str]] = {
    "core": frozenset(
        {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "ignorecase",
            "precomposeunicode",
        }
    ),
    # An explicit pushurl makes a two-step origin CAS crash-unsafe: changing
    # the fetch URL first leaves fetch and push pointing at different
    # authorities.  The legacy checkout uses the fetch URL as the implicit
    # push URL, so there is no valid production representation for pushurl.
    'remote "origin"': frozenset({"url", "fetch", "tagopt"}),
    'branch "main"': frozenset(
        {"remote", "merge", "vscode-merge-base"}
    ),
    "user": frozenset({"name", "email"}),
}

FORBIDDEN_MARKERS = (
    "commondir",
    "shallow",
    "config.worktree",
    "info/grafts",
    "info/sparse-checkout",
    "objects/info/alternates",
    "objects/info/http-alternates",
    "refs/replace",
)
FORBIDDEN_INDEX_EXTENSIONS = frozenset(
    {
        "FSMN",  # fsmonitor
        "UNTR",  # untracked cache
        "link",  # split/shared index
        "sdir",  # sparse index
    }
)
ALLOWED_INDEX_EXTENSIONS = frozenset(
    {
        "TREE",
        "REUC",
        "EOIE",
        "IEOT",
    }
)

SAFE_CONFIG_OVERRIDES = (
    "credential.helper=",
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.sparseCheckout=false",
    "core.sparseCheckoutCone=false",
    "core.attributesFile=/dev/null",
    "core.excludesFile=/dev/null",
    "diff.external=",
    "protocol.allow=never",
    "protocol.file.allow=never",
    "protocol.ext.allow=never",
    "protocol.ssh.allow=always",
    "protocol.https.allow=always",
    "fetch.fsckObjects=true",
    "transfer.fsckObjects=true",
    "fetch.writeCommitGraph=false",
    "maintenance.auto=false",
)
_OBJECT_DIGEST_CACHE: dict[
    tuple[int, int, int, int, int],
    str,
] = {}
_VERIFIED_OBJECT_STORES: set[str] = set()


class GitSourceTrustError(RuntimeError):
    """The repository cannot be interpreted as a trusted production source."""


class GitPermissionTakeoverError(GitSourceTrustError):
    """The pre-Git permission takeover cannot be resumed safely."""


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
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GitSourceTrustError(f"cannot hash trusted file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise GitSourceTrustError(f"trusted file changed while hashing: {path}")
    return "sha256:" + digest.hexdigest()


def _cached_object_digest(path: Path, expected: os.stat_result) -> str:
    """Hash immutable object bytes once per inode version in this process."""

    key = (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    )
    cached = _OBJECT_DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GitSourceTrustError(
            "Git object entry cannot be hashed safely"
        ) from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        before_key = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if before_key != key:
            raise GitSourceTrustError(
                "Git object entry changed before content hashing"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        after_key = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
    finally:
        os.close(descriptor)
    if after_key != key:
        raise GitSourceTrustError(
            "Git object entry changed during content hashing"
        )
    value = "sha256:" + digest.hexdigest()
    _OBJECT_DIGEST_CACHE[key] = value
    return value


def permission_takeover_marker_path(runtime_root: Path) -> Path:
    """Return the one fixed private marker used by the production checkout."""

    runtime_root = runtime_root.absolute()
    if not runtime_root.is_absolute() or ".." in runtime_root.parts:
        raise GitPermissionTakeoverError("runtime root must be absolute")
    return runtime_root / PERMISSION_MARKER_RELATIVE_PATH


def _permission_root(root: Path) -> Path:
    """Validate a pre-takeover root without requiring safe modes yet."""

    root = root.absolute()
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
        git_metadata = (root / ".git").lstat()
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "permission takeover repository is unavailable"
        ) from exc
    if (
        not root.is_absolute()
        or ".." in root.parts
        or resolved != root
        or root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or not stat.S_ISDIR(git_metadata.st_mode)
        or (root / ".git").is_symlink()
        or git_metadata.st_uid != os.geteuid()
    ):
        raise GitPermissionTakeoverError(
            "permission takeover requires an owned standalone checkout"
        )
    return root


def _private_permission_marker_parent(marker_path: Path) -> Path:
    marker_path = marker_path.absolute()
    parent = marker_path.parent
    try:
        metadata = parent.lstat()
        resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "permission takeover marker directory is unavailable"
        ) from exc
    if (
        not marker_path.is_absolute()
        or ".." in marker_path.parts
        or resolved != parent
        or parent.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GitPermissionTakeoverError(
            "permission takeover marker directory must be owner-private"
        )
    return marker_path


def _fsync_permission_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise GitPermissionTakeoverError(
            f"cannot fsync permission takeover directory: {path}"
        ) from exc


def _permission_json_no_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GitPermissionTakeoverError(
                "permission takeover marker contains a duplicate key"
            )
        value[key] = item
    return value


def _permission_entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _permission_staging_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
    )


def _permission_staging_version(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
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


def _permission_staging_content_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _permission_renameat2(
    directory_fd: int,
    source_name: str,
    target_name: str,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise GitPermissionTakeoverError(
            "renameat2 no-replace permission quarantine is unavailable"
        )
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
            directory_fd,
            os.fsencode(source_name),
            directory_fd,
            os.fsencode(target_name),
            flags,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source_name)


def _permission_rename_noreplace(
    directory_fd: int,
    source_name: str,
    target_name: str,
) -> None:
    _permission_renameat2(
        directory_fd,
        source_name,
        target_name,
        RENAME_NOREPLACE,
    )


def _open_permission_staging_at(
    directory_fd: int,
    name: str,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "permission takeover marker staging is unsafe"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        observed = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 <= metadata.st_size <= PERMISSION_MARKER_MAX_BYTES
            or _permission_staging_identity(observed)
            != _permission_staging_identity(metadata)
        ):
            raise GitPermissionTakeoverError(
                "permission takeover marker staging is unsafe"
            )
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _permission_staging_bytes_at(
    directory_fd: int,
    name: str,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    descriptor, before = _open_permission_staging_at(directory_fd, name)
    try:
        payload, after = _permission_staging_bytes_from_descriptor(
            descriptor, before
        )
        return bytes(payload), _permission_staging_identity(after)
    finally:
        os.close(descriptor)


def _permission_staging_bytes_from_descriptor(
    descriptor: int,
    before: os.stat_result,
) -> tuple[bytes, os.stat_result]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = bytearray()
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            raise GitPermissionTakeoverError(
                "permission takeover marker staging was truncated"
            )
        payload.extend(chunk)
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    if _permission_staging_version(after) != _permission_staging_version(
        before
    ):
        raise GitPermissionTakeoverError(
            "permission takeover marker staging changed while read"
        )
    return bytes(payload), after


def _assert_permission_held_path_at(
    directory_fd: int,
    name: str,
    descriptor: int,
    expected_payload: bytes,
    *,
    message: str,
) -> os.stat_result:
    held_payload, held = _permission_staging_bytes_from_descriptor(
        descriptor, os.fstat(descriptor)
    )
    try:
        observed = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise GitPermissionTakeoverError(message) from exc
    if (
        held_payload != expected_payload
        or _permission_staging_version(observed)
        != _permission_staging_version(held)
    ):
        raise GitPermissionTakeoverError(message)
    return held


def _rename_permission_held_noreplace_at(
    directory_fd: int,
    source_name: str,
    target_name: str,
    descriptor: int,
    expected_payload: bytes,
    *,
    message: str,
) -> os.stat_result:
    _assert_permission_held_path_at(
        directory_fd,
        source_name,
        descriptor,
        expected_payload,
        message=message,
    )
    _permission_rename_noreplace(
        directory_fd, source_name, target_name
    )
    try:
        held = _assert_permission_held_path_at(
            directory_fd,
            target_name,
            descriptor,
            expected_payload,
            message=message,
        )
    except BaseException:
        os.fsync(directory_fd)
        raise
    os.fsync(directory_fd)
    return held


def _remove_permission_staging_at(
    directory_fd: int,
    staging_name: str,
    quarantine_name: str,
    *,
    expected_identity: tuple[int, int, int, int, int] | None = None,
) -> None:
    staging_exists = _permission_entry_exists_at(directory_fd, staging_name)
    quarantine_exists = _permission_entry_exists_at(
        directory_fd, quarantine_name
    )
    if staging_exists and quarantine_exists:
        raise GitPermissionTakeoverError(
            "permission marker staging and quarantine both exist"
        )
    if not staging_exists and not quarantine_exists:
        return
    current_name = quarantine_name if quarantine_exists else staging_name
    descriptor, metadata = _open_permission_staging_at(
        directory_fd, current_name
    )
    try:
        identity = _permission_staging_identity(metadata)
        if expected_identity is not None and identity != expected_identity:
            raise GitPermissionTakeoverError(
                "permission marker staging identity changed before quarantine"
            )
        if staging_exists:
            _permission_rename_noreplace(
                directory_fd, staging_name, quarantine_name
            )
            observed = os.stat(
                quarantine_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if _permission_staging_identity(observed) != identity:
                os.fsync(directory_fd)
                raise GitPermissionTakeoverError(
                    "permission marker staging raced during quarantine"
                )
            os.fsync(directory_fd)
        observed = os.stat(
            quarantine_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if _permission_staging_identity(observed) != identity:
            raise GitPermissionTakeoverError(
                "permission marker quarantine identity changed"
            )
        os.unlink(quarantine_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)


def _prepare_permission_staging_at(
    directory_fd: int,
    staging_name: str,
    quarantine_name: str,
    payload: bytes,
) -> None:
    staging_exists = _permission_entry_exists_at(directory_fd, staging_name)
    quarantine_exists = _permission_entry_exists_at(
        directory_fd, quarantine_name
    )
    if staging_exists and quarantine_exists:
        raise GitPermissionTakeoverError(
            "permission marker staging and quarantine both exist"
        )
    if quarantine_exists:
        _remove_permission_staging_at(
            directory_fd, staging_name, quarantine_name
        )
        staging_exists = False
    if staging_exists:
        staged, identity = _permission_staging_bytes_at(
            directory_fd, staging_name
        )
        if staged != payload:
            _remove_permission_staging_at(
                directory_fd,
                staging_name,
                quarantine_name,
                expected_identity=identity,
            )
            staging_exists = False
    if staging_exists:
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(
            staging_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    os.fsync(directory_fd)


def _permission_marker_expectation(
    marker_path: Path,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    expected_payload = canonical_json_bytes(document) + b"\n"
    directory_fd = os.open(
        marker_path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        descriptor, before = _open_permission_staging_at(
            directory_fd, marker_path.name
        )
        try:
            payload, after = _permission_staging_bytes_from_descriptor(
                descriptor, before
            )
        finally:
            os.close(descriptor)
        if payload != expected_payload:
            raise GitPermissionTakeoverError(
                "permission marker raw generation changed before save"
            )
        return {
            "identity": _permission_staging_identity(after),
            "version": _permission_staging_version(after),
            "raw_sha256": sha256_bytes(payload),
        }
    finally:
        os.close(directory_fd)


def _assert_permission_marker_expectation(
    descriptor: int,
    before: os.stat_result,
    expectation: Mapping[str, Any],
) -> tuple[bytes, os.stat_result]:
    payload, after = _permission_staging_bytes_from_descriptor(
        descriptor, before
    )
    if (
        expectation.get("identity")
        != _permission_staging_identity(after)
        or expectation.get("version")
        != _permission_staging_version(after)
        or expectation.get("raw_sha256") != sha256_bytes(payload)
    ):
        raise GitPermissionTakeoverError(
            "permission marker target changed before generation save"
        )
    return payload, after


def _permission_predecessor_document(
    current: Mapping[str, Any],
) -> dict[str, Any]:
    phase = current.get("phase")
    if phase not in PERMISSION_LIFECYCLE_PHASE_SEQUENCE:
        raise GitPermissionTakeoverError(
            "permission marker current phase is invalid"
        )
    phase_index = PERMISSION_LIFECYCLE_PHASE_SEQUENCE.index(str(phase))
    if phase_index == 0:
        raise GitPermissionTakeoverError(
            "permission marker first phase has no predecessor"
        )
    generation = current.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 1
    ):
        raise GitPermissionTakeoverError(
            "permission marker current generation has no predecessor"
        )
    predecessor = dict(current)
    predecessor["phase"] = PERMISSION_LIFECYCLE_PHASE_SEQUENCE[
        phase_index - 1
    ]
    predecessor["generation"] = generation - 1
    predecessor["evidence_sha256"] = _permission_document_digest(
        predecessor
    )
    return predecessor


def _permission_retired_prefix(marker_path: Path) -> str:
    return f".{marker_path.name}.retired-g"


def _permission_retired_name(
    marker_path: Path,
    document: Mapping[str, Any],
    payload: bytes,
) -> str:
    generation = document.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise GitPermissionTakeoverError(
            "permission marker retired generation is invalid"
        )
    digest = sha256_bytes(payload).removeprefix("sha256:")
    return (
        f"{_permission_retired_prefix(marker_path)}"
        f"{generation:020d}-{digest}"
    )


def _permission_rebuild_name(
    marker_path: Path,
    document: Mapping[str, Any],
    payload: bytes,
) -> str:
    generation = document.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise GitPermissionTakeoverError(
            "permission marker rebuild generation is invalid"
        )
    digest = sha256_bytes(payload).removeprefix("sha256:")
    return (
        f".{marker_path.name}.rebuild-g{generation:020d}-{digest}"
    )


def _rebuild_permission_generation_at(
    directory_fd: int,
    marker_path: Path,
    destination_name: str,
    document: Mapping[str, Any],
) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    if len(payload) > PERMISSION_MARKER_MAX_BYTES:
        raise GitPermissionTakeoverError(
            "permission marker rebuild generation is oversized"
        )
    rebuild_name = _permission_rebuild_name(
        marker_path, document, payload
    )
    quarantine_name = f"{rebuild_name}.quarantine"
    if _permission_entry_exists_at(directory_fd, quarantine_name):
        raise GitPermissionTakeoverError(
            "permission marker rebuild quarantine is occupied"
        )
    if _permission_entry_exists_at(directory_fd, rebuild_name):
        existing, _identity = _permission_staging_bytes_at(
            directory_fd, rebuild_name
        )
        if existing != payload:
            raise GitPermissionTakeoverError(
                "permission marker rebuild staging is occupied"
            )
    else:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                rebuild_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        os.fsync(directory_fd)
    descriptor, before = _open_permission_staging_at(
        directory_fd, rebuild_name
    )
    try:
        observed, _after = _permission_staging_bytes_from_descriptor(
            descriptor, before
        )
        if observed != payload:
            raise GitPermissionTakeoverError(
                "permission marker rebuild staging differs"
            )
        _rename_permission_held_noreplace_at(
            directory_fd,
            rebuild_name,
            destination_name,
            descriptor,
            payload,
            message="permission marker rebuild publication raced",
        )
    finally:
        os.close(descriptor)


def _atomic_permission_marker(
    marker_path: Path,
    document: dict[str, Any],
    *,
    expected_previous: Mapping[str, Any] | None,
) -> None:
    marker_path = _private_permission_marker_parent(marker_path)
    payload = canonical_json_bytes(document) + b"\n"
    if len(payload) > PERMISSION_MARKER_MAX_BYTES:
        raise GitPermissionTakeoverError(
            "permission takeover marker is oversized"
        )
    staging_name = f".{marker_path.name}.staging"
    quarantine_name = f"{staging_name}.quarantine"
    previous_name = f".{marker_path.name}.previous"
    directory_fd: int | None = None
    try:
        directory_fd = os.open(
            marker_path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        _prepare_permission_staging_at(
            directory_fd, staging_name, quarantine_name, payload
        )
        previous_exists = _permission_entry_exists_at(
            directory_fd, previous_name
        )
        retired_entries = _permission_retired_entries_at(
            directory_fd,
            root=Path(document["repository"]),
            marker_path=marker_path,
        )
        staging_fd, staging_before = _open_permission_staging_at(
            directory_fd, staging_name
        )
        try:
            staged, _staging_validated = (
                _permission_staging_bytes_from_descriptor(
                    staging_fd, staging_before
                )
            )
            if staged != payload:
                raise GitPermissionTakeoverError(
                    "permission takeover marker staging payload differs"
                )
            if expected_previous is None:
                if previous_exists or retired_entries:
                    raise GitPermissionTakeoverError(
                        "first permission marker publication has a predecessor"
                    )
                _rename_permission_held_noreplace_at(
                    directory_fd,
                    staging_name,
                    marker_path.name,
                    staging_fd,
                    payload,
                    message=(
                        "permission marker first publication identity raced"
                    ),
                )
                return
            marker_fd, marker_before = _open_permission_staging_at(
                directory_fd, marker_path.name
            )
            try:
                current_payload, _current_validated = (
                    _assert_permission_marker_expectation(
                        marker_fd, marker_before, expected_previous
                    )
                )
                current_document = _permission_predecessor_document(
                    document
                )
                if (
                    current_payload
                    != canonical_json_bytes(current_document) + b"\n"
                ):
                    raise GitPermissionTakeoverError(
                        "permission marker current generation differs"
                    )
                if previous_exists:
                    retired_document, retired_payload, prior_identity = (
                        _permission_transaction_document_at(
                            directory_fd,
                            previous_name,
                            root=Path(document["repository"]),
                            marker_path=marker_path,
                        )
                    )
                    _validate_permission_generation_pair(
                        retired_document, current_document
                    )
                    _validate_permission_retired_history(
                        retired_entries, tail=retired_document
                    )
                    retired_name = _permission_retired_name(
                        marker_path, retired_document, retired_payload
                    )
                    if _permission_entry_exists_at(
                        directory_fd, retired_name
                    ):
                        raise GitPermissionTakeoverError(
                            "permission marker retired generation already exists"
                        )
                    retired_fd, retired_before = (
                        _open_permission_staging_at(
                            directory_fd, previous_name
                        )
                    )
                    try:
                        observed_retired, observed_retired_after = (
                            _permission_staging_bytes_from_descriptor(
                                retired_fd, retired_before
                            )
                        )
                        if (
                            observed_retired != retired_payload
                            or _permission_staging_identity(
                                observed_retired_after
                            )
                            != prior_identity
                        ):
                            raise GitPermissionTakeoverError(
                                "permission marker predecessor changed before rotation"
                            )
                        moved_retired = (
                            _rename_permission_held_noreplace_at(
                                directory_fd,
                                previous_name,
                                retired_name,
                                retired_fd,
                                retired_payload,
                                message=(
                                    "permission marker predecessor raced during rotation"
                                ),
                            )
                        )
                        if (
                            _permission_staging_identity(moved_retired)
                            != prior_identity
                        ):
                            raise GitPermissionTakeoverError(
                                "permission marker retired identity changed"
                            )
                    finally:
                        os.close(retired_fd)
                else:
                    if retired_entries:
                        raise GitPermissionTakeoverError(
                            "permission marker predecessor history has a gap"
                        )
                    _validate_permission_retired_history(
                        (), tail=current_document
                    )
                _assert_permission_marker_expectation(
                    marker_fd, os.fstat(marker_fd), expected_previous
                )
                _rename_permission_held_noreplace_at(
                    directory_fd,
                    marker_path.name,
                    previous_name,
                    marker_fd,
                    current_payload,
                    message=(
                        "permission marker target raced during predecessor save"
                    ),
                )
                _rename_permission_held_noreplace_at(
                    directory_fd,
                    staging_name,
                    marker_path.name,
                    staging_fd,
                    payload,
                    message=(
                        "permission marker generation publication identity raced"
                    ),
                )
            finally:
                os.close(marker_fd)
        finally:
            os.close(staging_fd)
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "cannot persist permission takeover marker"
        ) from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _permission_transaction_document_at(
    directory_fd: int,
    name: str,
    *,
    root: Path,
    marker_path: Path,
) -> tuple[
    dict[str, Any],
    bytes,
    tuple[int, int, int, int, int],
]:
    payload, identity = _permission_staging_bytes_at(directory_fd, name)
    try:
        raw = json.loads(
            payload,
            object_pairs_hook=_permission_json_no_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GitPermissionTakeoverError(
            "permission marker transaction document is malformed"
        ) from exc
    document = validate_permission_takeover_evidence(
        raw,
        repository=root,
        marker_path=marker_path,
    )
    if payload != canonical_json_bytes(document) + b"\n":
        raise GitPermissionTakeoverError(
            "permission marker transaction bytes are not canonical"
        )
    return document, payload, identity


def _permission_retired_entries_at(
    directory_fd: int,
    *,
    root: Path,
    marker_path: Path,
) -> tuple[
    tuple[
        str,
        dict[str, Any],
        bytes,
        tuple[int, int, int, int, int],
    ],
    ...,
]:
    prefix = _permission_retired_prefix(marker_path)
    pattern = re.compile(
        rf"^{re.escape(prefix)}([0-9]{{20}})-([0-9a-f]{{64}})$"
    )
    names = sorted(
        name
        for name in os.listdir(directory_fd)
        if name.startswith(prefix)
    )
    maximum = max(0, len(PERMISSION_LIFECYCLE_PHASE_SEQUENCE) - 2)
    if len(names) > maximum:
        raise GitPermissionTakeoverError(
            "permission marker retired history is oversized"
        )
    entries: list[
        tuple[
            str,
            dict[str, Any],
            bytes,
            tuple[int, int, int, int, int],
        ]
    ] = []
    generations: set[int] = set()
    for name in names:
        match = pattern.fullmatch(name)
        if match is None:
            raise GitPermissionTakeoverError(
                "permission marker retired name is invalid"
            )
        document, payload, identity = _permission_transaction_document_at(
            directory_fd,
            name,
            root=root,
            marker_path=marker_path,
        )
        generation = document["generation"]
        if (
            int(match.group(1)) != generation
            or name
            != _permission_retired_name(
                marker_path, document, payload
            )
            or generation in generations
        ):
            raise GitPermissionTakeoverError(
                "permission marker retired identity is invalid"
            )
        generations.add(generation)
        entries.append((name, document, payload, identity))
    entries.sort(key=lambda entry: int(entry[1]["generation"]))
    return tuple(entries)


def _permission_document_is_history_anchor(
    document: Mapping[str, Any],
) -> bool:
    return (
        document.get("phase") == "captured"
        and document.get("generation") == 1
    ) or (
        document.get("phase") == "hardened"
        and document.get("generation")
        == PERMISSION_LIFECYCLE_PHASE_SEQUENCE.index("hardened") + 1
    )


def _validate_permission_retired_history(
    entries: tuple[
        tuple[
            str,
            dict[str, Any],
            bytes,
            tuple[int, int, int, int, int],
        ],
        ...,
    ],
    *,
    tail: Mapping[str, Any] | None = None,
) -> None:
    documents = [entry[1] for entry in entries]
    if documents:
        if not _permission_document_is_history_anchor(documents[0]):
            raise GitPermissionTakeoverError(
                "permission marker retired history has no valid anchor"
            )
        for previous, current in zip(documents, documents[1:]):
            _validate_permission_generation_pair(previous, current)
        if tail is not None:
            _validate_permission_generation_pair(documents[-1], tail)
    elif tail is not None and not _permission_document_is_history_anchor(
        tail
    ):
        raise GitPermissionTakeoverError(
            "permission marker predecessor history has no valid anchor"
        )


def _permission_successor_document(
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    phase = previous.get("phase")
    if phase not in PERMISSION_LIFECYCLE_PHASE_SEQUENCE:
        raise GitPermissionTakeoverError(
            "permission marker previous phase is invalid"
        )
    phase_index = PERMISSION_LIFECYCLE_PHASE_SEQUENCE.index(str(phase))
    if phase_index + 1 >= len(PERMISSION_LIFECYCLE_PHASE_SEQUENCE):
        raise GitPermissionTakeoverError(
            "permission marker terminal phase has no successor"
        )
    generation = previous.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise GitPermissionTakeoverError(
            "permission marker previous generation is invalid"
        )
    successor = dict(previous)
    successor["phase"] = PERMISSION_LIFECYCLE_PHASE_SEQUENCE[
        phase_index + 1
    ]
    successor["generation"] = generation + 1
    successor["evidence_sha256"] = _permission_document_digest(successor)
    return successor


def _validate_permission_generation_pair(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    if dict(current) != _permission_successor_document(previous):
        raise GitPermissionTakeoverError(
            "permission marker generations are not exact successors"
        )


def _permission_marker_transaction_names(
    marker_path: Path,
) -> tuple[str, str, str, str]:
    staging_name = f".{marker_path.name}.staging"
    return (
        staging_name,
        f"{staging_name}.quarantine",
        f".{marker_path.name}.previous",
        _permission_retired_prefix(marker_path),
    )


def _permission_reconcile_marker_transaction(
    root: Path,
    marker_path: Path,
) -> None:
    (
        staging_name,
        _staging_quarantine_name,
        previous_name,
        _retired_prefix,
    ) = _permission_marker_transaction_names(marker_path)
    directory_fd = os.open(
        marker_path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    def open_expected(
        name: str,
        expected_raw: bytes,
        expected_identity: tuple[int, int, int, int, int],
    ) -> int:
        descriptor, before = _open_permission_staging_at(
            directory_fd, name
        )
        try:
            raw, after = _permission_staging_bytes_from_descriptor(
                descriptor, before
            )
            if (
                raw != expected_raw
                or _permission_staging_identity(after)
                != expected_identity
            ):
                raise GitPermissionTakeoverError(
                    "permission marker replay entry changed"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def move_expected(
        source_name: str,
        target_name: str,
        expected_raw: bytes,
        expected_identity: tuple[int, int, int, int, int],
        *,
        message: str,
    ) -> tuple[int, int, int, int, int]:
        descriptor = open_expected(
            source_name, expected_raw, expected_identity
        )
        try:
            moved = _rename_permission_held_noreplace_at(
                directory_fd,
                source_name,
                target_name,
                descriptor,
                expected_raw,
                message=message,
            )
            return _permission_staging_identity(moved)
        finally:
            os.close(descriptor)

    try:
        marker_exists = _permission_entry_exists_at(
            directory_fd, marker_path.name
        )
        staging_exists = _permission_entry_exists_at(
            directory_fd, staging_name
        )
        staging_quarantine_exists = _permission_entry_exists_at(
            directory_fd, _staging_quarantine_name
        )
        previous_exists = _permission_entry_exists_at(
            directory_fd, previous_name
        )
        retired_entries = _permission_retired_entries_at(
            directory_fd,
            root=root,
            marker_path=marker_path,
        )
        if not previous_exists and not retired_entries:
            if staging_quarantine_exists:
                _remove_permission_staging_at(
                    directory_fd,
                    staging_name,
                    _staging_quarantine_name,
                )
                staging_exists = _permission_entry_exists_at(
                    directory_fd, staging_name
                )
            if staging_exists:
                staging_is_valid = False
                recovery_anchor: dict[str, Any] | None = None
                try:
                    staged = _permission_transaction_document_at(
                        directory_fd,
                        staging_name,
                        root=root,
                        marker_path=marker_path,
                    )
                    if marker_exists:
                        current = _permission_transaction_document_at(
                            directory_fd,
                            marker_path.name,
                            root=root,
                            marker_path=marker_path,
                        )
                        _validate_permission_generation_pair(
                            current[0], staged[0]
                        )
                        staging_is_valid = True
                    else:
                        staging_is_valid = (
                            staged[0].get("phase") == "captured"
                            and staged[0].get("generation") == 1
                        )
                        if not staging_is_valid:
                            candidate_anchor = (
                                _permission_predecessor_document(
                                    staged[0]
                                )
                            )
                            if _permission_document_is_history_anchor(
                                candidate_anchor
                            ):
                                recovery_anchor = candidate_anchor
                except GitPermissionTakeoverError:
                    if marker_exists:
                        # A malformed live marker owns the failure.  Its
                        # adjacent staging must remain untouched for audit.
                        _permission_transaction_document_at(
                            directory_fd,
                            marker_path.name,
                            root=root,
                            marker_path=marker_path,
                        )
                if recovery_anchor is not None:
                    _rebuild_permission_generation_at(
                        directory_fd,
                        marker_path,
                        previous_name,
                        recovery_anchor,
                    )
                    staging_fd = open_expected(
                        staging_name, staged[1], staged[2]
                    )
                    try:
                        _rename_permission_held_noreplace_at(
                            directory_fd,
                            staging_name,
                            marker_path.name,
                            staging_fd,
                            staged[1],
                            message=(
                                "permission marker anchor replay publication raced"
                            ),
                        )
                    finally:
                        os.close(staging_fd)
                    return
                if not staging_is_valid:
                    _staged_raw, staged_identity = (
                        _permission_staging_bytes_at(
                            directory_fd, staging_name
                        )
                    )
                    _remove_permission_staging_at(
                        directory_fd,
                        staging_name,
                        _staging_quarantine_name,
                        expected_identity=staged_identity,
                    )
                    staging_exists = False
            if marker_exists:
                current_entry = _permission_transaction_document_at(
                    directory_fd,
                    marker_path.name,
                    root=root,
                    marker_path=marker_path,
                )
                current_document = current_entry[0]
                terminal_restored = (
                    current_document.get("phase") == "restored"
                    and current_document.get("generation")
                    == len(PERMISSION_LIFECYCLE_PHASE_SEQUENCE)
                )
                if (
                    _permission_document_is_history_anchor(
                        current_document
                    )
                    or terminal_restored
                ):
                    return
                missing_previous = _permission_predecessor_document(
                    current_document
                )
                if not _permission_document_is_history_anchor(
                    missing_previous
                ):
                    raise GitPermissionTakeoverError(
                        "permission marker predecessor history has no valid anchor"
                    )
                if staging_exists:
                    staged_entry = _permission_transaction_document_at(
                        directory_fd,
                        staging_name,
                        root=root,
                        marker_path=marker_path,
                    )
                    _validate_permission_generation_pair(
                        current_document, staged_entry[0]
                    )
                _rebuild_permission_generation_at(
                    directory_fd,
                    marker_path,
                    previous_name,
                    missing_previous,
                )
            return

        marker_entry = (
            _permission_transaction_document_at(
                directory_fd,
                marker_path.name,
                root=root,
                marker_path=marker_path,
            )
            if marker_exists
            else None
        )
        staging_entry = (
            _permission_transaction_document_at(
                directory_fd,
                staging_name,
                root=root,
                marker_path=marker_path,
            )
            if staging_exists
            else None
        )
        previous_entry = (
            _permission_transaction_document_at(
                directory_fd,
                previous_name,
                root=root,
                marker_path=marker_path,
            )
            if previous_exists
            else None
        )
        if marker_entry is not None and previous_entry is not None:
            _validate_permission_retired_history(
                retired_entries, tail=previous_entry[0]
            )
            _validate_permission_generation_pair(
                previous_entry[0], marker_entry[0]
            )
            if staging_entry is not None:
                _validate_permission_generation_pair(
                    marker_entry[0], staging_entry[0]
                )
            return

        if marker_entry is not None and previous_entry is None:
            if not retired_entries:
                _validate_permission_retired_history(
                    (), tail=marker_entry[0]
                )
                if staging_entry is not None:
                    _validate_permission_generation_pair(
                        marker_entry[0], staging_entry[0]
                    )
                return
            latest = retired_entries[-1]
            _validate_permission_retired_history(retired_entries)
            latest_successor = _permission_successor_document(latest[1])
            if marker_entry[0] != latest_successor:
                missing_previous = _permission_predecessor_document(
                    marker_entry[0]
                )
                _validate_permission_retired_history(
                    retired_entries, tail=missing_previous
                )
                _validate_permission_generation_pair(
                    missing_previous, marker_entry[0]
                )
                if staging_entry is not None:
                    _validate_permission_generation_pair(
                        marker_entry[0], staging_entry[0]
                    )
                _rebuild_permission_generation_at(
                    directory_fd,
                    marker_path,
                    previous_name,
                    missing_previous,
                )
                return
            if staging_entry is None:
                move_expected(
                    latest[0],
                    previous_name,
                    latest[2],
                    latest[3],
                    message="permission marker predecessor restore raced",
                )
                return
            _validate_permission_generation_pair(
                marker_entry[0], staging_entry[0]
            )
            move_expected(
                marker_path.name,
                previous_name,
                marker_entry[1],
                marker_entry[2],
                message="permission marker replay predecessor save raced",
            )
            staging_fd = open_expected(
                staging_name, staging_entry[1], staging_entry[2]
            )
            try:
                _rename_permission_held_noreplace_at(
                    directory_fd,
                    staging_name,
                    marker_path.name,
                    staging_fd,
                    staging_entry[1],
                    message="permission marker replay publication raced",
                )
            finally:
                os.close(staging_fd)
            return

        if marker_entry is None and previous_entry is not None:
            _validate_permission_retired_history(
                retired_entries, tail=previous_entry[0]
            )
            if staging_entry is not None:
                _validate_permission_generation_pair(
                    previous_entry[0], staging_entry[0]
                )
                staging_fd = open_expected(
                    staging_name, staging_entry[1], staging_entry[2]
                )
                try:
                    _rename_permission_held_noreplace_at(
                        directory_fd,
                        staging_name,
                        marker_path.name,
                        staging_fd,
                        staging_entry[1],
                        message="permission marker replay publication raced",
                    )
                finally:
                    os.close(staging_fd)
                return
            move_expected(
                previous_name,
                marker_path.name,
                previous_entry[1],
                previous_entry[2],
                message="permission marker previous restore raced",
            )
            if retired_entries:
                latest = retired_entries[-1]
                move_expected(
                    latest[0],
                    previous_name,
                    latest[2],
                    latest[3],
                    message="permission marker retired restore raced",
                )
            return

        if marker_entry is None and previous_entry is None:
            if not retired_entries:
                return
            _validate_permission_retired_history(retired_entries)
            latest = retired_entries[-1]
            reconstructed = _permission_successor_document(latest[1])
            if staging_entry is not None:
                missing_previous = _permission_predecessor_document(
                    staging_entry[0]
                )
                if reconstructed != missing_previous:
                    raise GitPermissionTakeoverError(
                        "permission marker missing generation is ambiguous"
                    )
                _rebuild_permission_generation_at(
                    directory_fd,
                    marker_path,
                    previous_name,
                    reconstructed,
                )
                staging_fd = open_expected(
                    staging_name, staging_entry[1], staging_entry[2]
                )
                try:
                    _rename_permission_held_noreplace_at(
                        directory_fd,
                        staging_name,
                        marker_path.name,
                        staging_fd,
                        staging_entry[1],
                        message="permission marker replay publication raced",
                    )
                finally:
                    os.close(staging_fd)
                return
            _rebuild_permission_generation_at(
                directory_fd,
                marker_path,
                marker_path.name,
                reconstructed,
            )
            move_expected(
                latest[0],
                previous_name,
                latest[2],
                latest[3],
                message="permission marker retired restore raced",
            )
            return

        raise GitPermissionTakeoverError(
            "permission marker generation replay is incomplete"
        )
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "cannot reconcile permission marker generation"
        ) from exc
    finally:
        os.close(directory_fd)


def _permission_pending_captured_projection(
    root: Path,
    marker_path: Path,
) -> dict[str, Any] | None:
    (
        staging_name,
        _staging_quarantine_name,
        previous_name,
        _retired_prefix,
    ) = _permission_marker_transaction_names(marker_path)
    directory_fd = os.open(
        marker_path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        retired_entries = _permission_retired_entries_at(
            directory_fd,
            root=root,
            marker_path=marker_path,
        )
        if (
            not _permission_entry_exists_at(directory_fd, previous_name)
            or not _permission_entry_exists_at(directory_fd, staging_name)
            or retired_entries
        ):
            return None
        previous, _raw, _identity = _permission_transaction_document_at(
            directory_fd,
            previous_name,
            root=root,
            marker_path=marker_path,
        )
        current, _current_raw, _current_identity = (
            _permission_transaction_document_at(
                directory_fd,
                staging_name,
                root=root,
                marker_path=marker_path,
            )
        )
        _validate_permission_retired_history((), tail=previous)
        _validate_permission_generation_pair(previous, current)
        captured = dict(previous)
        captured["phase"] = "captured"
        captured["generation"] = 1
        captured["evidence_sha256"] = _permission_document_digest(captured)
        return validate_permission_takeover_evidence(
            captured,
            repository=root,
            marker_path=marker_path,
            allowed_phases={"captured"},
        )
    finally:
        os.close(directory_fd)


def _read_permission_marker(marker_path: Path) -> dict[str, Any]:
    marker_path = _private_permission_marker_parent(marker_path)
    try:
        descriptor = os.open(
            marker_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "permission takeover marker is unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 0 <= before.st_size <= PERMISSION_MARKER_MAX_BYTES
        ):
            raise GitPermissionTakeoverError(
                "permission takeover marker metadata is unsafe"
            )
        payload = b""
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise GitPermissionTakeoverError(
                    "permission takeover marker was truncated"
                )
            payload += chunk
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise GitPermissionTakeoverError(
            "permission takeover marker changed while being read"
        )
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_permission_json_no_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GitPermissionTakeoverError(
            "permission takeover marker is malformed"
        ) from exc
    if not isinstance(value, dict):
        raise GitPermissionTakeoverError(
            "permission takeover marker must contain an object"
        )
    return value


def _permission_relative_path(value: object) -> str:
    if value == ".":
        return "."
    if (
        not isinstance(value, str)
        or not value.startswith(".git")
        or value not in {".git"} and not value.startswith(".git/")
        or "\\" in value
        or "\0" in value
    ):
        raise GitPermissionTakeoverError(
            "permission takeover path is outside Git authority"
        )
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise GitPermissionTakeoverError(
            "permission takeover path escapes the repository"
        )
    return value


def _permission_path(root: Path, relative: str) -> Path:
    return root if relative == "." else root / relative


def _permission_mutable(relative: str, kind: str) -> bool:
    # Git may atomically replace its config/index/refs/logs while changing the
    # canonical origin or moving between B and F. Existing object bytes are
    # immutable; newly fetched objects are allowed only after a hardened scan.
    return kind == "file" and not relative.startswith(".git/objects/")


def _permission_target_mode(kind: str, mode: int) -> int:
    if kind == "directory":
        return 0o700
    target = mode & 0o700
    if target & 0o400 == 0:
        raise GitPermissionTakeoverError(
            "Git authority file is not owner-readable"
        )
    return target


def _permission_file_digest(
    path: Path,
    expected: os.stat_result,
) -> str:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise GitPermissionTakeoverError(
            f"cannot hash Git permission authority: {path}"
        ) from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            != (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
                expected.st_mtime_ns,
            )
        ):
            raise GitPermissionTakeoverError(
                "Git permission authority changed before hashing"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise GitPermissionTakeoverError(
            "Git permission authority changed while hashing"
        )
    return "sha256:" + digest.hexdigest()


def _permission_record(
    root: Path,
    path: Path,
    relative: str,
) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GitPermissionTakeoverError(
            f"cannot inventory Git permission path: {path}"
        ) from exc
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
    else:
        raise GitPermissionTakeoverError(
            "Git permission authority contains a symlink or special file"
        )
    if (
        path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or kind == "file" and metadata.st_nlink != 1
        or kind == "directory" and metadata.st_mode & 0o700 != 0o700
    ):
        raise GitPermissionTakeoverError(
            "Git permission authority is not exclusively owner-controlled"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    content_sha256 = (
        _permission_file_digest(path, metadata)
        if kind == "file"
        else None
    )
    return {
        "path": relative,
        "type": kind,
        "mode": f"{mode:04o}",
        "target_mode": f"{_permission_target_mode(kind, mode):04o}",
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "nlink": metadata.st_nlink if kind == "file" else None,
        "size": metadata.st_size if kind == "file" else None,
        "content_sha256": content_sha256,
        "mutable": _permission_mutable(relative, kind),
    }


def _permission_walk(root: Path) -> list[tuple[Path, str]]:
    """Walk only the real .git directory without interpreting Git config."""

    git_dir = root / ".git"
    result: list[tuple[Path, str]] = []

    def walk(directory: Path, relative: str) -> None:
        result.append((directory, relative))
        try:
            children = sorted(
                os.scandir(directory),
                key=lambda item: os.fsencode(item.name),
            )
        except OSError as exc:
            raise GitPermissionTakeoverError(
                f"cannot enumerate Git permission authority: {directory}"
            ) from exc
        directories: list[tuple[Path, str]] = []
        files: list[tuple[Path, str]] = []
        for child in children:
            try:
                child.name.encode("utf-8")
            except UnicodeError as exc:
                raise GitPermissionTakeoverError(
                    "Git permission path is not UTF-8"
                ) from exc
            path = directory / child.name
            child_relative = f"{relative}/{child.name}"
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise GitPermissionTakeoverError(
                    "Git permission path disappeared during inventory"
                ) from exc
            if stat.S_ISDIR(metadata.st_mode) and not path.is_symlink():
                directories.append((path, child_relative))
            elif stat.S_ISREG(metadata.st_mode) and not path.is_symlink():
                files.append((path, child_relative))
            else:
                raise GitPermissionTakeoverError(
                    "Git authority contains a symlink or special file"
                )
        for path, child_relative in directories:
            walk(path, child_relative)
        result.extend(files)

    walk(git_dir, ".git")
    return result


def _inventory_permission_records(root: Path) -> list[dict[str, Any]]:
    root = _permission_root(root)
    root_record = _permission_record(root, root, ".")
    metadata_records = [
        _permission_record(root, path, relative)
        for path, relative in _permission_walk(root)
    ]
    directories = sorted(
        (
            record
            for record in metadata_records
            if record["type"] == "directory"
        ),
        key=lambda record: (
            len(Path(record["path"]).parts),
            os.fsencode(record["path"]),
        ),
    )
    files = sorted(
        (
            record
            for record in metadata_records
            if record["type"] == "file"
        ),
        key=lambda record: os.fsencode(record["path"]),
    )
    return [root_record, *directories, *files]


def _validate_permission_takeover_config(
    root: Path,
    records: list[dict[str, Any]],
) -> None:
    """Parse the sealed local config before the first permission mutation."""

    matches = [
        record
        for record in records
        if record["path"] == ".git/config" and record["type"] == "file"
    ]
    if len(matches) != 1:
        raise GitPermissionTakeoverError(
            "Git permission authority lacks one local config"
        )
    expected = matches[0]
    path = root / ".git/config"
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "Git local config cannot be opened safely"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected["uid"]
            or before.st_gid != expected["gid"]
            or before.st_dev != expected["device"]
            or before.st_ino != expected["inode"]
            or before.st_nlink != expected["nlink"]
            or before.st_size != expected["size"]
            or before.st_size < 1
            or before.st_size > MAX_CONFIG_BYTES
        ):
            raise GitPermissionTakeoverError(
                "Git local config changed after permission inventory"
            )
        payload = b""
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise GitPermissionTakeoverError(
                    "Git local config was truncated"
                )
            payload += chunk
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
        )
        or sha256_bytes(payload) != expected["content_sha256"]
    ):
        raise GitPermissionTakeoverError(
            "Git local config changed while validating takeover"
        )
    try:
        _canonical_config(payload)
    except GitSourceTrustError as exc:
        raise GitPermissionTakeoverError(
            "Git local config contains executable or redirect policy"
        ) from exc


def _permission_identity(
    records: list[dict[str, Any]],
    *,
    hardened: bool,
) -> list[dict[str, Any]]:
    return [
        {
            "path": record["path"],
            "type": record["type"],
            "mode": (
                record["target_mode"] if hardened else record["mode"]
            ),
            "uid": record["uid"],
            "gid": record["gid"],
        }
        for record in records
    ]


def _permission_document_digest(document: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                key: value
                for key, value in document.items()
                if key != "evidence_sha256"
            }
        )
    )


def _require_permission_marker_size_envelope(
    document: Mapping[str, Any],
    phases: tuple[str, ...],
) -> None:
    """Prove every remaining journal generation fits before any mutation."""

    generation = document.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise GitPermissionTakeoverError(
            "permission takeover generation is invalid"
        )
    candidate = dict(document)
    future_sizes: list[int] = []
    first_future_payload: bytes | None = None
    future_bytes = 0
    for phase in phases:
        generation += 1
        candidate["phase"] = phase
        candidate["generation"] = generation
        candidate["evidence_sha256"] = _permission_document_digest(
            candidate
        )
        candidate_payload = canonical_json_bytes(candidate) + b"\n"
        payload_bytes = len(candidate_payload)
        if payload_bytes > PERMISSION_MARKER_MAX_BYTES:
            raise GitPermissionTakeoverError(
                "permission takeover marker lifecycle is oversized"
            )
        future_bytes += payload_bytes
        if future_bytes > PERMISSION_HISTORY_MAX_BYTES:
            raise GitPermissionTakeoverError(
                "permission takeover marker history is oversized"
            )
        future_sizes.append(payload_bytes)
        if first_future_payload is None:
            first_future_payload = candidate_payload
        candidate_payload = b""
    marker_value = document.get("marker_path")
    if not isinstance(marker_value, str) or not Path(marker_value).is_absolute():
        raise GitPermissionTakeoverError(
            "permission takeover marker path is invalid"
        )
    marker_path = Path(marker_value)
    directory_fd = os.open(
        marker_path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        prefix = _permission_retired_prefix(marker_path)
        staging_name = f".{marker_path.name}.staging"
        staging_quarantine_name = f"{staging_name}.quarantine"
        staging_exists = _permission_entry_exists_at(
            directory_fd, staging_name
        )
        staging_quarantine_exists = _permission_entry_exists_at(
            directory_fd, staging_quarantine_name
        )
        if staging_exists and staging_quarantine_exists:
            raise GitPermissionTakeoverError(
                "permission marker staging and quarantine both exist"
            )
        if staging_quarantine_exists:
            raise GitPermissionTakeoverError(
                "permission marker staging quarantine requires replay"
            )
        staged_allocated_bytes = 0
        if staging_exists:
            if first_future_payload is None:
                raise GitPermissionTakeoverError(
                    "permission marker staging has no future generation"
                )
            staging_fd, staging_before = _open_permission_staging_at(
                directory_fd, staging_name
            )
            try:
                staged_payload, staging_after = (
                    _permission_staging_bytes_from_descriptor(
                        staging_fd, staging_before
                    )
                )
            finally:
                os.close(staging_fd)
            if staged_payload != first_future_payload:
                raise GitPermissionTakeoverError(
                    "permission marker staging differs from future generation"
                )
            staged_allocated_bytes = staging_after.st_blocks * 512
        retained_names = [
            name
            for name in os.listdir(directory_fd)
            if name in {
                marker_path.name,
                f".{marker_path.name}.previous",
            }
            or name.startswith(prefix)
        ]
        retained_bytes = 0
        for name in retained_names:
            metadata = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or not 0 <= metadata.st_size <= PERMISSION_MARKER_MAX_BYTES
            ):
                raise GitPermissionTakeoverError(
                    "permission marker retained history is unsafe"
                )
            retained_bytes += metadata.st_size
        if retained_bytes + future_bytes > PERMISSION_HISTORY_MAX_BYTES:
            raise GitPermissionTakeoverError(
                "permission takeover marker history is oversized"
            )
        if future_bytes:
            filesystem = os.fstatvfs(directory_fd)
            fragment_size = filesystem.f_frsize or filesystem.f_bsize
            available = filesystem.f_bavail * fragment_size
            future_allocated_bytes = sum(
                ((payload_size + fragment_size - 1) // fragment_size)
                * fragment_size
                for payload_size in future_sizes
            )
            required = (
                future_allocated_bytes
                - min(
                    staged_allocated_bytes,
                    (
                        (future_sizes[0] + fragment_size - 1)
                        // fragment_size
                    )
                    * fragment_size,
                )
                + PERMISSION_HISTORY_FREE_MARGIN_BYTES
            )
            if available < required:
                raise GitPermissionTakeoverError(
                    "permission takeover marker history lacks free space"
                )
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "cannot prove permission marker history capacity"
        ) from exc
    finally:
        os.close(directory_fd)


def _new_permission_takeover_document(
    root: Path,
    marker_path: Path,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the immutable captured inventory before any marker write."""

    return {
        "schema_version": PERMISSION_SCHEMA_VERSION,
        "policy": PERMISSION_POLICY_NAME,
        "repository": str(root),
        "marker_path": str(marker_path),
        "phase": "captured",
        "generation": 0,
        "records": records,
        "inventory_sha256": sha256_bytes(canonical_json_bytes(records)),
        "original_permissions_sha256": sha256_bytes(
            canonical_json_bytes(
                _permission_identity(records, hardened=False)
            )
        ),
        "hardened_permissions_sha256": sha256_bytes(
            canonical_json_bytes(
                _permission_identity(records, hardened=True)
            )
        ),
    }


def _validate_permission_takeover_expectations(
    document: Mapping[str, Any],
    *,
    expected_inventory_sha256: str | None,
    expected_original_permissions_sha256: str | None,
    expected_hardened_permissions_sha256: str | None,
) -> None:
    expected = {
        "inventory_sha256": expected_inventory_sha256,
        "original_permissions_sha256": expected_original_permissions_sha256,
        "hardened_permissions_sha256": expected_hardened_permissions_sha256,
    }
    for field, digest in expected.items():
        if digest is None:
            continue
        if (
            not isinstance(digest, str)
            or DIGEST_RE.fullmatch(digest) is None
        ):
            raise GitPermissionTakeoverError(
                "permission takeover expected digest is malformed"
            )
        if document.get(field) != digest:
            raise GitPermissionTakeoverError(
                f"permission takeover {field} changed before mutation"
            )


def plan_repository_permission_takeover(
    root: Path,
    marker_path: Path,
) -> dict[str, Any]:
    """Return the exact captured marker projection without writing anything."""

    root = _permission_root(root)
    marker_path = _private_permission_marker_parent(marker_path)
    if marker_path.exists() or marker_path.is_symlink():
        raise GitPermissionTakeoverError(
            "permission takeover marker already exists"
        )
    pending = _permission_pending_captured_projection(root, marker_path)
    if pending is not None:
        return pending
    records = _inventory_permission_records(root)
    _validate_permission_stage(
        root,
        records,
        root_state="original",
        directory_state="original",
        file_state="original",
        exact_paths=True,
        allow_mutable_changes=False,
        verify_content=True,
    )
    _validate_permission_takeover_config(root, records)
    document = _new_permission_takeover_document(root, marker_path, records)
    _require_permission_marker_size_envelope(
        document, PERMISSION_LIFECYCLE_PHASE_SEQUENCE
    )
    document["generation"] = 1
    document["evidence_sha256"] = _permission_document_digest(document)
    validated = validate_permission_takeover_evidence(
        document,
        repository=root,
        marker_path=marker_path,
        allowed_phases={"captured"},
    )
    return validated


def validate_permission_takeover_evidence(
    value: object,
    *,
    repository: Path | None = None,
    marker_path: Path | None = None,
    allowed_phases: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Validate a marker embedded in installer/takeover audit evidence."""

    fields = {
        "schema_version",
        "policy",
        "repository",
        "marker_path",
        "phase",
        "generation",
        "records",
        "inventory_sha256",
        "original_permissions_sha256",
        "hardened_permissions_sha256",
        "evidence_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema_version") != PERMISSION_SCHEMA_VERSION
        or value.get("policy") != PERMISSION_POLICY_NAME
        or value.get("phase") not in PERMISSION_PHASES
        or isinstance(value.get("generation"), bool)
        or not isinstance(value.get("generation"), int)
        or value["generation"] <= 0
        or not isinstance(value.get("repository"), str)
        or not Path(value["repository"]).is_absolute()
        or not isinstance(value.get("marker_path"), str)
        or not Path(value["marker_path"]).is_absolute()
        or any(
            not isinstance(value.get(name), str)
            or DIGEST_RE.fullmatch(value[name]) is None
            for name in (
                "inventory_sha256",
                "original_permissions_sha256",
                "hardened_permissions_sha256",
                "evidence_sha256",
            )
        )
        or value.get("evidence_sha256")
        != _permission_document_digest(value)
    ):
        raise GitPermissionTakeoverError(
            "permission takeover evidence is malformed"
        )
    if (
        repository is not None
        and value["repository"] != str(repository.absolute())
    ) or (
        marker_path is not None
        and value["marker_path"] != str(marker_path.absolute())
    ):
        raise GitPermissionTakeoverError(
            "permission takeover evidence belongs elsewhere"
        )
    if allowed_phases is not None and value["phase"] not in allowed_phases:
        raise GitPermissionTakeoverError(
            "permission takeover marker is in the wrong phase"
        )
    records = value.get("records")
    if not isinstance(records, list) or len(records) < 4:
        raise GitPermissionTakeoverError(
            "permission takeover inventory is incomplete"
        )
    expected_fields = {
        "path",
        "type",
        "mode",
        "target_mode",
        "uid",
        "gid",
        "device",
        "inode",
        "nlink",
        "size",
        "content_sha256",
        "mutable",
    }
    paths: list[str] = []
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != expected_fields
            or record.get("type") not in {"directory", "file"}
            or not isinstance(record.get("mode"), str)
            or re.fullmatch(r"[0-7]{4}", record["mode"]) is None
            or not isinstance(record.get("target_mode"), str)
            or re.fullmatch(r"[0-7]{4}", record["target_mode"]) is None
            or any(
                isinstance(record.get(name), bool)
                or not isinstance(record.get(name), int)
                or record[name] < 0
                for name in ("uid", "gid", "device", "inode")
            )
            or record["uid"] != os.geteuid()
            or not isinstance(record.get("mutable"), bool)
        ):
            raise GitPermissionTakeoverError(
                "permission takeover record is malformed"
            )
        relative = _permission_relative_path(record.get("path"))
        if relative in paths:
            raise GitPermissionTakeoverError(
                "permission takeover path occurs more than once"
            )
        paths.append(relative)
        mode = int(record["mode"], 8)
        if (
            record["target_mode"]
            != f"{_permission_target_mode(record['type'], mode):04o}"
            or record["mutable"]
            is not _permission_mutable(relative, record["type"])
        ):
            raise GitPermissionTakeoverError(
                "permission takeover target policy differs"
            )
        if record["type"] == "directory":
            if any(
                record[name] is not None
                for name in (
                    "nlink",
                    "size",
                    "content_sha256",
                )
            ):
                raise GitPermissionTakeoverError(
                    "permission directory record is malformed"
                )
        elif (
            isinstance(record.get("nlink"), bool)
            or not isinstance(record.get("nlink"), int)
            or record["nlink"] != 1
            or isinstance(record.get("size"), bool)
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
            or not isinstance(record.get("content_sha256"), str)
            or DIGEST_RE.fullmatch(record["content_sha256"]) is None
        ):
            raise GitPermissionTakeoverError(
                "permission file record is malformed"
            )
    if (
        paths[0] != "."
        or paths[1] != ".git"
        or not {
            ".",
            ".git",
            ".git/config",
            ".git/objects",
        }.issubset(paths)
    ):
        raise GitPermissionTakeoverError(
            "permission takeover inventory has no repository roots"
        )
    if value["inventory_sha256"] != sha256_bytes(
        canonical_json_bytes(records)
    ) or value["original_permissions_sha256"] != sha256_bytes(
        canonical_json_bytes(
            _permission_identity(records, hardened=False)
        )
    ) or value["hardened_permissions_sha256"] != sha256_bytes(
        canonical_json_bytes(
            _permission_identity(records, hardened=True)
        )
    ):
        raise GitPermissionTakeoverError(
            "permission takeover inventory digest differs"
        )
    return json.loads(canonical_json_bytes(value))


def _save_permission_document(
    marker_path: Path,
    document: dict[str, Any],
    *,
    expected_previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generation = document.get("generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise GitPermissionTakeoverError(
            "permission takeover generation is invalid"
        )
    if generation == 0 and expected_previous is not None:
        raise GitPermissionTakeoverError(
            "first permission marker generation cannot have a predecessor"
        )
    if generation != 0 and expected_previous is None:
        raise GitPermissionTakeoverError(
            "permission marker predecessor expectation is required"
        )
    document["generation"] = generation + 1
    document["evidence_sha256"] = _permission_document_digest(document)
    validated = validate_permission_takeover_evidence(
        document,
        repository=Path(document["repository"]),
        marker_path=marker_path,
    )
    _atomic_permission_marker(
        marker_path,
        validated,
        expected_previous=expected_previous,
    )
    return validated


def _load_permission_document(
    root: Path,
    marker_path: Path,
) -> dict[str, Any]:
    return validate_permission_takeover_evidence(
        _read_permission_marker(marker_path),
        repository=root,
        marker_path=marker_path,
    )


def read_repository_permission_takeover(
    root: Path,
    marker_path: Path,
    *,
    allowed_phases: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Read and strictly validate one durable permission marker."""

    root = _permission_root(root)
    marker_path = _private_permission_marker_parent(marker_path)
    document = _load_permission_document(root, marker_path)
    return validate_permission_takeover_evidence(
        document,
        repository=root,
        marker_path=marker_path,
        allowed_phases=allowed_phases,
    )


def _current_permission_paths(root: Path) -> dict[str, os.stat_result]:
    paths: dict[str, os.stat_result] = {}
    try:
        paths["."] = root.lstat()
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "permission repository root disappeared"
        ) from exc
    for path, relative in _permission_walk(root):
        try:
            paths[relative] = path.lstat()
        except OSError as exc:
            raise GitPermissionTakeoverError(
                "Git permission path disappeared"
            ) from exc
    return paths


def _validate_current_permission_record(
    root: Path,
    record: Mapping[str, Any],
    *,
    allowed_modes: set[str],
    allow_mutable_changes: bool,
    verify_content: bool,
    require_original_config: bool = False,
) -> None:
    relative = str(record["path"])
    path = _permission_path(root, relative)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GitPermissionTakeoverError(
            f"permission takeover path is unavailable: {relative}"
        ) from exc
    kind_matches = (
        stat.S_ISDIR(metadata.st_mode)
        if record["type"] == "directory"
        else stat.S_ISREG(metadata.st_mode)
    )
    mutable_changed = (
        record["mutable"] is True and allow_mutable_changes
    )
    if (
        not kind_matches
        or path.is_symlink()
        or metadata.st_uid != record["uid"]
        or metadata.st_gid != record["gid"]
        or f"{stat.S_IMODE(metadata.st_mode):04o}" not in allowed_modes
        or record["type"] == "file" and metadata.st_nlink != 1
        or (
            not mutable_changed
            and (
                metadata.st_dev != record["device"]
                or metadata.st_ino != record["inode"]
            )
        )
    ):
        raise GitPermissionTakeoverError(
            f"permission takeover compare-and-swap failed: {relative}"
        )
    compare_content = (
        record["type"] == "file"
        and verify_content
        and (
            not mutable_changed
            or require_original_config and relative == ".git/config"
        )
    )
    if compare_content and (
        metadata.st_size != record["size"]
        or _permission_file_digest(path, metadata)
        != record["content_sha256"]
    ):
        raise GitPermissionTakeoverError(
            f"permission takeover content changed: {relative}"
        )


def _validate_permission_stage(
    root: Path,
    records: list[dict[str, Any]],
    *,
    root_state: str,
    directory_state: str,
    file_state: str,
    exact_paths: bool,
    allow_mutable_changes: bool,
    verify_content: bool,
    require_original_config: bool = False,
) -> None:
    def allowed(record: Mapping[str, Any], state: str) -> set[str]:
        if state == "original":
            return {str(record["mode"])}
        if state == "target":
            return {str(record["target_mode"])}
        if state == "either":
            return {
                str(record["mode"]),
                str(record["target_mode"]),
            }
        raise GitPermissionTakeoverError(
            "permission validation stage is invalid"
        )

    current = _current_permission_paths(root)
    stored_paths = {record["path"] for record in records}
    if not stored_paths.issubset(current) or (
        exact_paths and set(current) != stored_paths
    ):
        raise GitPermissionTakeoverError(
            "Git permission inventory gained or lost paths"
        )
    by_path = {record["path"]: record for record in records}
    for relative, metadata in current.items():
        record = by_path.get(relative)
        if record is not None:
            state = (
                root_state
                if relative == "."
                else directory_state
                if record["type"] == "directory"
                else file_state
            )
            _validate_current_permission_record(
                root,
                record,
                allowed_modes=allowed(record, state),
                allow_mutable_changes=allow_mutable_changes,
                verify_content=verify_content,
                require_original_config=require_original_config,
            )
            continue
        # Git may add refs, logs and objects after the initial takeover. Such
        # entries have no original mode to restore, so they must remain
        # owner-private and single-linked.
        path = _permission_path(root, relative)
        if (
            path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink != 1
            )
            or not (
                stat.S_ISREG(metadata.st_mode)
                or stat.S_ISDIR(metadata.st_mode)
            )
        ):
            raise GitPermissionTakeoverError(
                f"new Git permission path is unsafe: {relative}"
            )


def _chmod_permission_record(
    root: Path,
    record: dict[str, Any],
    *,
    desired: str,
    alternate: str,
    allow_mutable_changes: bool,
    require_original_config: bool,
) -> bool:
    _validate_current_permission_record(
        root,
        record,
        allowed_modes={desired, alternate},
        allow_mutable_changes=allow_mutable_changes,
        verify_content=True,
        require_original_config=require_original_config,
    )
    path = _permission_path(root, record["path"])
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GitPermissionTakeoverError(
            "permission takeover path disappeared before chmod"
        ) from exc
    if f"{stat.S_IMODE(metadata.st_mode):04o}" == desired:
        return False
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if record["type"] == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                before.st_dev != metadata.st_dev
                or before.st_ino != metadata.st_ino
                or before.st_uid != record["uid"]
                or before.st_gid != record["gid"]
            ):
                raise GitPermissionTakeoverError(
                    "permission path changed before chmod"
                )
            os.fchmod(descriptor, int(desired, 8))
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_uid != record["uid"]
                or after.st_gid != record["gid"]
                or f"{stat.S_IMODE(after.st_mode):04o}" != desired
            ):
                raise GitPermissionTakeoverError(
                    "permission chmod did not persist exactly"
                )
        finally:
            os.close(descriptor)
        _fsync_permission_directory(path.parent)
    except OSError as exc:
        raise GitPermissionTakeoverError(
            f"cannot change Git permission path: {record['path']}"
        ) from exc
    return True


def _permission_transition(
    marker_path: Path,
    document: dict[str, Any],
    phase: str,
    checkpoint: Callable[[str], None],
) -> dict[str, Any]:
    expected_previous = _permission_marker_expectation(
        marker_path, document
    )
    document["phase"] = phase
    document = _save_permission_document(
        marker_path,
        document,
        expected_previous=expected_previous,
    )
    checkpoint(f"permission:{phase}")
    return document


def takeover_repository_permissions(
    root: Path,
    marker_path: Path,
    *,
    checkpoint: Callable[[str], None] | None = None,
    expected_inventory_sha256: str | None = None,
    expected_original_permissions_sha256: str | None = None,
    expected_hardened_permissions_sha256: str | None = None,
) -> dict[str, Any]:
    """Crash-safely harden the checkout before the first production Git call."""

    root = _permission_root(root)
    marker_path = _private_permission_marker_parent(marker_path)
    emit = checkpoint or (lambda _label: None)
    _permission_reconcile_marker_transaction(root, marker_path)
    if marker_path.exists() or marker_path.is_symlink():
        document = _load_permission_document(root, marker_path)
        _validate_permission_takeover_expectations(
            document,
            expected_inventory_sha256=expected_inventory_sha256,
            expected_original_permissions_sha256=(
                expected_original_permissions_sha256
            ),
            expected_hardened_permissions_sha256=(
                expected_hardened_permissions_sha256
            ),
        )
        if document["phase"] not in {
            "restore-files-intent",
            "restore-files-restored",
            "restore-directories-intent",
            "restore-directories-restored",
            "restore-root-intent",
            "restored",
        }:
            # A marker written by an older controller is not permission to
            # resume under policy that the current controller rejects.  This
            # check is read-only and still leaves the explicit restore path
            # available for a previously hardened checkout.
            _validate_permission_takeover_config(root, document["records"])
        if document["phase"] in PERMISSION_TAKEOVER_PHASE_SEQUENCE:
            phase_index = PERMISSION_TAKEOVER_PHASE_SEQUENCE.index(
                document["phase"]
            )
            _require_permission_marker_size_envelope(
                document,
                (
                    *PERMISSION_TAKEOVER_PHASE_SEQUENCE[
                        phase_index + 1 :
                    ],
                    *PERMISSION_RESTORE_PHASE_SEQUENCE,
                ),
            )
    else:
        records = _inventory_permission_records(root)
        _validate_permission_stage(
            root,
            records,
            root_state="original",
            directory_state="original",
            file_state="original",
            exact_paths=True,
            allow_mutable_changes=False,
            verify_content=True,
        )
        _validate_permission_takeover_config(root, records)
        document = _new_permission_takeover_document(
            root, marker_path, records
        )
        _require_permission_marker_size_envelope(
            document, PERMISSION_LIFECYCLE_PHASE_SEQUENCE
        )
        # This compare-and-swap happens before the first durable marker and
        # before the first chmod.  A reviewed read-only plan can therefore
        # never authorize a newly observed Git inventory.
        _validate_permission_takeover_expectations(
            document,
            expected_inventory_sha256=expected_inventory_sha256,
            expected_original_permissions_sha256=(
                expected_original_permissions_sha256
            ),
            expected_hardened_permissions_sha256=(
                expected_hardened_permissions_sha256
            ),
        )
        document = _save_permission_document(marker_path, document)
        emit("permission:captured")
    records = document["records"]
    root_record = records[0]
    directory_records = [
        record
        for record in records[1:]
        if record["type"] == "directory"
    ]
    file_records = [
        record
        for record in records
        if record["type"] == "file"
    ]
    if document["phase"] in {
        "restore-files-intent",
        "restore-files-restored",
        "restore-directories-intent",
        "restore-directories-restored",
        "restore-root-intent",
        "restored",
    }:
        raise GitPermissionTakeoverError(
            "restored permission takeover cannot be silently reused"
        )
    if document["phase"] == "captured":
        document = _permission_transition(
            marker_path, document, "root-intent", emit
        )
    if document["phase"] == "root-intent":
        changed = _chmod_permission_record(
            root,
            root_record,
            desired=root_record["target_mode"],
            alternate=root_record["mode"],
            allow_mutable_changes=False,
            require_original_config=False,
        )
        emit(
            "permission:root:action"
            if changed
            else "permission:root:already"
        )
        document = _permission_transition(
            marker_path, document, "root-hardened", emit
        )
    if document["phase"] == "root-hardened":
        document = _permission_transition(
            marker_path,
            document,
            "metadata-directories-intent",
            emit,
        )
    if document["phase"] == "metadata-directories-intent":
        for record in directory_records:
            changed = _chmod_permission_record(
                root,
                record,
                desired=record["target_mode"],
                alternate=record["mode"],
                allow_mutable_changes=False,
                require_original_config=False,
            )
            emit(
                f"permission:directory:{record['path']}:"
                + ("action" if changed else "already")
            )
        document = _permission_transition(
            marker_path,
            document,
            "metadata-directories-hardened",
            emit,
        )
    if document["phase"] == "metadata-directories-hardened":
        document = _permission_transition(
            marker_path,
            document,
            "metadata-files-intent",
            emit,
        )
    if document["phase"] == "metadata-files-intent":
        for record in file_records:
            changed = _chmod_permission_record(
                root,
                record,
                desired=record["target_mode"],
                alternate=record["mode"],
                allow_mutable_changes=False,
                require_original_config=False,
            )
            emit(
                f"permission:file:{record['path']}:"
                + ("action" if changed else "already")
            )
        document = _permission_transition(
            marker_path,
            document,
            "metadata-files-hardened",
            emit,
        )
    if document["phase"] == "metadata-files-hardened":
        _validate_permission_stage(
            root,
            records,
            root_state="target",
            directory_state="target",
            file_state="target",
            exact_paths=True,
            allow_mutable_changes=False,
            verify_content=True,
        )
        document = _permission_transition(
            marker_path, document, "hardened", emit
        )
    if document["phase"] != "hardened":
        raise GitPermissionTakeoverError(
            "permission takeover did not reach its terminal hardened phase"
        )
    return verify_repository_permission_takeover(
        root,
        marker_path,
        verify_content=True,
        require_original_mutable=True,
    )


def verify_repository_permission_takeover(
    root: Path,
    marker_path: Path,
    *,
    verify_content: bool = True,
    require_original_mutable: bool = False,
) -> dict[str, Any]:
    """Re-verify hardened modes and the content-bound original inventory."""

    root = _permission_root(root)
    marker_path = _private_permission_marker_parent(marker_path)
    document = _load_permission_document(root, marker_path)
    validate_permission_takeover_evidence(
        document,
        repository=root,
        marker_path=marker_path,
        allowed_phases={"hardened"},
    )
    records = document["records"]
    _validate_permission_stage(
        root,
        records,
        root_state="target",
        directory_state="target",
        file_state="target",
        exact_paths=False,
        allow_mutable_changes=not require_original_mutable,
        verify_content=verify_content,
        require_original_config=require_original_mutable,
    )
    return document


def restore_repository_permissions(
    root: Path,
    marker_path: Path,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore the exact pre-takeover mode/uid/gid as the final rollback step."""

    root = _permission_root(root)
    marker_path = _private_permission_marker_parent(marker_path)
    emit = checkpoint or (lambda _label: None)
    _permission_reconcile_marker_transaction(root, marker_path)
    document = _load_permission_document(root, marker_path)
    if document["phase"] not in {
        "hardened",
        "restore-files-intent",
        "restore-files-restored",
        "restore-directories-intent",
        "restore-directories-restored",
        "restore-root-intent",
        "restored",
    }:
        raise GitPermissionTakeoverError(
            "permission hardening must finish before it can be restored"
        )
    if document["phase"] == "hardened":
        remaining_restore_phases = PERMISSION_RESTORE_PHASE_SEQUENCE
    elif document["phase"] in PERMISSION_RESTORE_PHASE_SEQUENCE:
        restore_index = PERMISSION_RESTORE_PHASE_SEQUENCE.index(
            document["phase"]
        )
        remaining_restore_phases = PERMISSION_RESTORE_PHASE_SEQUENCE[
            restore_index + 1 :
        ]
    else:  # pragma: no cover - phase validation above owns this branch
        remaining_restore_phases = ()
    _require_permission_marker_size_envelope(
        document, remaining_restore_phases
    )
    records = document["records"]
    root_record = records[0]
    directory_records = sorted(
        (
            record
            for record in records[1:]
            if record["type"] == "directory"
        ),
        key=lambda record: (
            len(Path(record["path"]).parts),
            os.fsencode(record["path"]),
        ),
        reverse=True,
    )
    file_records = sorted(
        (
            record
            for record in records
            if record["type"] == "file"
        ),
        key=lambda record: os.fsencode(record["path"]),
        reverse=True,
    )
    if document["phase"] == "restored":
        _validate_permission_stage(
            root,
            records,
            root_state="original",
            directory_state="original",
            file_state="original",
            exact_paths=False,
            allow_mutable_changes=True,
            verify_content=True,
            require_original_config=True,
        )
        return document
    if document["phase"] == "hardened":
        verify_repository_permission_takeover(
            root,
            marker_path,
            verify_content=True,
        )
        document = _permission_transition(
            marker_path, document, "restore-files-intent", emit
        )
    if document["phase"] == "restore-files-intent":
        for record in file_records:
            changed = _chmod_permission_record(
                root,
                record,
                desired=record["mode"],
                alternate=record["target_mode"],
                allow_mutable_changes=True,
                require_original_config=True,
            )
            emit(
                f"permission:restore-file:{record['path']}:"
                + ("action" if changed else "already")
            )
        document = _permission_transition(
            marker_path, document, "restore-files-restored", emit
        )
    if document["phase"] == "restore-files-restored":
        document = _permission_transition(
            marker_path,
            document,
            "restore-directories-intent",
            emit,
        )
    if document["phase"] == "restore-directories-intent":
        for record in directory_records:
            changed = _chmod_permission_record(
                root,
                record,
                desired=record["mode"],
                alternate=record["target_mode"],
                allow_mutable_changes=False,
                require_original_config=False,
            )
            emit(
                f"permission:restore-directory:{record['path']}:"
                + ("action" if changed else "already")
            )
        document = _permission_transition(
            marker_path,
            document,
            "restore-directories-restored",
            emit,
        )
    if document["phase"] == "restore-directories-restored":
        document = _permission_transition(
            marker_path, document, "restore-root-intent", emit
        )
    if document["phase"] == "restore-root-intent":
        changed = _chmod_permission_record(
            root,
            root_record,
            desired=root_record["mode"],
            alternate=root_record["target_mode"],
            allow_mutable_changes=False,
            require_original_config=False,
        )
        emit(
            "permission:restore-root:action"
            if changed
            else "permission:restore-root:already"
        )
        document = _permission_transition(
            marker_path, document, "restored", emit
        )
    return restore_repository_permissions(
        root,
        marker_path,
        checkpoint=emit,
    )


def assert_trusted_ambient_environment(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Reject ambient variables that can redirect Git before isolation."""

    values = os.environ if environment is None else environment
    dangerous = sorted(
        key
        for key in values
        if key in FORBIDDEN_AMBIENT_EXACT
        or key.startswith(FORBIDDEN_AMBIENT_PREFIXES)
    )
    if dangerous:
        raise GitSourceTrustError(
            "ambient Git control variables are forbidden: "
            + ", ".join(dangerous)
        )


def _require_absolute_root(root: Path) -> Path:
    root = root.absolute()
    if not root.is_absolute() or ".." in root.parts:
        raise GitSourceTrustError("repository root must be absolute")
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise GitSourceTrustError("repository root is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or root.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or resolved != root
    ):
        raise GitSourceTrustError(
            "repository root must be owner-controlled and non-symlink"
        )
    return root


def _require_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GitSourceTrustError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise GitSourceTrustError(f"{label} is not owner-controlled")
    return metadata


def _read_control_file(
    path: Path,
    *,
    label: str,
    required: bool,
    maximum: int = MAX_CONTROL_FILE_BYTES,
) -> tuple[bytes, os.stat_result] | None:
    present = path.exists() or path.is_symlink()
    if not present:
        if required:
            raise GitSourceTrustError(f"{label} is unavailable")
        return None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GitSourceTrustError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or not 0 <= before.st_size <= maximum
        ):
            raise GitSourceTrustError(f"{label} has unsafe metadata")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise GitSourceTrustError(f"{label} was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise GitSourceTrustError(f"{label} changed while being read")
    return b"".join(chunks), before


def _canonical_config(payload: bytes) -> list[dict[str, str]]:
    if len(payload) > MAX_CONFIG_BYTES:
        raise GitSourceTrustError("local Git config is unexpectedly large")
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=False,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        parser.read_string(payload.decode("utf-8"))
    except (UnicodeError, configparser.Error) as exc:
        raise GitSourceTrustError("local Git config is malformed") from exc
    records: list[dict[str, str]] = []
    for section in parser.sections():
        normalized_section = section.lower()
        allowed = ALLOWED_CONFIG.get(normalized_section)
        if allowed is None:
            raise GitSourceTrustError(
                f"local Git config contains forbidden section: {section}"
            )
        for raw_key, raw_value in parser.items(section, raw=True):
            key = raw_key.lower()
            if key not in allowed:
                raise GitSourceTrustError(
                    "local Git config contains executable or redirect policy: "
                    f"{section}.{raw_key}"
                )
            value = raw_value.strip()
            if "\x00" in value or "\n" in value or "\r" in value:
                raise GitSourceTrustError("local Git config value is unsafe")
            records.append(
                {
                    "section": normalized_section,
                    "key": key,
                    "value": value,
                }
            )
    core = {
        record["key"]: record["value"].lower()
        for record in records
        if record["section"] == "core"
    }
    if (
        core.get("repositoryformatversion") not in {None, "0"}
        or core.get("bare") not in {None, "false", "no", "off", "0"}
    ):
        raise GitSourceTrustError("local Git repository format is unsupported")
    return sorted(
        records,
        key=lambda record: (
            record["section"],
            record["key"],
            record["value"],
        ),
    )


def _index_extensions(payload: bytes) -> list[str]:
    if len(payload) < 32 or payload[:4] != b"DIRC":
        raise GitSourceTrustError("Git index header is invalid")
    version, count = struct.unpack(">II", payload[4:12])
    if version not in {2, 3}:
        # Version 4 path compression and future formats are deliberately not
        # accepted by the production trust parser.
        raise GitSourceTrustError("Git index version is unsupported")
    offset = 12
    checksum_size = 20
    payload_end = len(payload) - checksum_size
    for _entry in range(count):
        start = offset
        if offset + 62 > payload_end:
            raise GitSourceTrustError("Git index entry is truncated")
        flags = struct.unpack(">H", payload[offset + 60 : offset + 62])[0]
        offset += 62
        if version == 3 and flags & 0x4000:
            if offset + 2 > payload_end:
                raise GitSourceTrustError("Git extended index entry is truncated")
            offset += 2
        name_length = flags & 0x0FFF
        if name_length == 0x0FFF:
            terminator = payload.find(b"\0", offset, payload_end)
            if terminator < 0:
                raise GitSourceTrustError("Git index pathname is unterminated")
            offset = terminator + 1
        else:
            offset += name_length
            if offset >= payload_end or payload[offset] != 0:
                raise GitSourceTrustError("Git index pathname is malformed")
            offset += 1
        offset = start + ((offset - start + 7) // 8) * 8
        if offset > payload_end:
            raise GitSourceTrustError("Git index padding is malformed")
    extensions: list[str] = []
    while offset < payload_end:
        if offset + 8 > payload_end:
            raise GitSourceTrustError("Git index extension is truncated")
        raw_signature = payload[offset : offset + 4]
        size = struct.unpack(">I", payload[offset + 4 : offset + 8])[0]
        offset += 8
        if offset + size > payload_end:
            raise GitSourceTrustError("Git index extension payload is truncated")
        try:
            signature = raw_signature.decode("ascii")
        except UnicodeError as exc:
            raise GitSourceTrustError("Git index extension is invalid") from exc
        if signature in FORBIDDEN_INDEX_EXTENSIONS:
            raise GitSourceTrustError(
                f"Git index contains forbidden extension: {signature}"
            )
        if signature not in ALLOWED_INDEX_EXTENSIONS:
            raise GitSourceTrustError(
                f"Git index contains unsupported extension: {signature}"
            )
        extensions.append(signature)
        offset += size
    if offset != payload_end:
        raise GitSourceTrustError("Git index structure is malformed")
    return extensions


def _object_store_evidence(objects: Path) -> dict[str, Any]:
    _require_directory(objects, label="Git object database")
    records: list[dict[str, Any]] = []
    total_size = 0
    for directory, directory_names, file_names in os.walk(
        objects,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        _require_directory(current, label="Git object directory")
        for name in sorted(directory_names):
            child = current / name
            _require_directory(child, label="Git object directory")
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(objects).as_posix()
            if name.endswith(".promisor"):
                raise GitSourceTrustError(
                    "promisor/partial-clone object storage is forbidden"
                )
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError as exc:
                raise GitSourceTrustError(
                    "Git object entry cannot be opened safely"
                ) from exc
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022
                    or metadata.st_nlink != 1
                    or metadata.st_size < 0
                ):
                    raise GitSourceTrustError(
                        "Git object entry has unsafe metadata"
                    )
                before = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
                pack_header = b""
                pack_trailer = b""
                if name.endswith(".pack"):
                    pack_header = os.read(descriptor, 4)
                    if metadata.st_size >= 20:
                        os.lseek(descriptor, -20, os.SEEK_END)
                        pack_trailer = os.read(descriptor, 20)
                after_metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after = (
                after_metadata.st_dev,
                after_metadata.st_ino,
                after_metadata.st_size,
                after_metadata.st_mtime_ns,
            )
            if before != after:
                raise GitSourceTrustError(
                    "Git object entry changed while being inventoried"
                )
            total_size += metadata.st_size
            item: dict[str, Any] = {
                "path": relative,
                "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                "size": metadata.st_size,
                "sha256": _cached_object_digest(path, metadata),
            }
            if name.endswith(".pack"):
                if (
                    metadata.st_size < 32
                    or pack_header != b"PACK"
                    or len(pack_trailer) != 20
                ):
                    raise GitSourceTrustError("Git pack file is malformed")
                # The trailing SHA-1 covers the full pack body and is the
                # object-format-native content binding used by this SHA-1 repo.
                item["pack_trailer_sha1"] = pack_trailer.hex()
            elif re.fullmatch(r"[0-9a-f]{38}", name) and re.fullmatch(
                r"[0-9a-f]{2}", path.parent.name
            ):
                item["object_id"] = path.parent.name + name
            records.append(item)
    return {
        "object_count": len(records),
        "total_size": total_size,
        "inventory_sha256": sha256_bytes(canonical_json_bytes(records)),
        "standalone": True,
        "promisor": False,
        "alternates": False,
    }


def _refs_evidence(git_dir: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    refs = git_dir / "refs"
    _require_directory(refs, label="Git refs directory")
    for directory, directory_names, file_names in os.walk(
        refs,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        _require_directory(current, label="Git refs directory")
        for name in sorted(directory_names):
            child = current / name
            _require_directory(child, label="Git refs directory")
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(git_dir).as_posix()
            if relative.startswith("refs/replace/"):
                raise GitSourceTrustError("Git replacement refs are forbidden")
            record = _read_control_file(
                path,
                label="Git ref",
                required=True,
                maximum=1024 * 1024,
            )
            assert record is not None
            payload, metadata = record
            records.append(
                {
                    "path": relative,
                    "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                    "sha256": sha256_bytes(payload),
                }
            )
    packed = _read_control_file(
        git_dir / "packed-refs",
        label="packed Git refs",
        required=False,
        maximum=MAX_CONFIG_BYTES,
    )
    packed_digest: str | None = None
    if packed is not None:
        packed_payload, _metadata = packed
        try:
            packed_text = packed_payload.decode("ascii")
        except UnicodeError as exc:
            raise GitSourceTrustError("packed Git refs are malformed") from exc
        if any(
            line.strip().endswith(" refs/replace")
            or " refs/replace/" in line
            for line in packed_text.splitlines()
        ):
            raise GitSourceTrustError("packed Git replacement refs are forbidden")
        packed_digest = sha256_bytes(packed_payload)
    return {
        "loose_count": len(records),
        "loose_sha256": sha256_bytes(canonical_json_bytes(records)),
        "packed_refs_sha256": packed_digest,
        "replace_refs": 0,
    }


def _git_binary_evidence() -> dict[str, Any]:
    try:
        metadata = GIT_BINARY.lstat()
        resolved = GIT_BINARY.resolve(strict=True)
    except OSError as exc:
        raise GitSourceTrustError("fixed Git binary is unavailable") from exc
    if (
        resolved != GIT_BINARY
        or not stat.S_ISREG(metadata.st_mode)
        or GIT_BINARY.is_symlink()
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
    ):
        raise GitSourceTrustError("fixed Git binary identity is unsafe")
    return {
        "path": str(GIT_BINARY),
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        "size": metadata.st_size,
        "sha256": sha256_file(GIT_BINARY),
    }


def safe_git_environment(
    root: Path,
    *,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
) -> dict[str, str]:
    """Return the complete environment for a trusted Git child process."""

    assert_trusted_ambient_environment(ambient)
    root = _require_absolute_root(root)
    git_dir = root / ".git"
    _require_directory(git_dir, label="Git metadata directory")
    _require_directory(git_dir / "objects", label="Git object database")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "LANG": "C",
        "LC_ALL": "C",
        "XDG_CONFIG_HOME": "/nonexistent",
        "GIT_DIR": str(git_dir),
        "GIT_COMMON_DIR": str(git_dir),
        "GIT_WORK_TREE": str(root),
        "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
        "GIT_INDEX_FILE": str(git_dir / "index"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "/bin/false",
        "GIT_SEQUENCE_EDITOR": "/bin/false",
    }
    environment["GIT_SSH_COMMAND"] = ssh_command or "/bin/false"
    return environment


def safe_git_command(
    root: Path,
    *arguments: str,
    executable: str = "/usr/bin/git",
) -> list[str]:
    """Build a Git command whose local policy is explicitly overridden."""

    root = _require_absolute_root(root)
    if (
        not arguments
        or not isinstance(arguments[0], str)
        or arguments[0].startswith("-")
    ):
        raise GitSourceTrustError("trusted Git command requires an explicit subcommand")
    if executable != "/usr/bin/git":
        raise GitSourceTrustError("trusted Git command uses an unexpected executable")
    forbidden = (
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
    )
    for argument in arguments:
        if (
            not isinstance(argument, str)
            or "\x00" in argument
            or "\n" in argument
            or "\r" in argument
            or argument in forbidden
            or any(argument.startswith(value + "=") for value in forbidden)
        ):
            raise GitSourceTrustError(
                "trusted Git command contains a control-plane redirect"
            )
    command = [executable]
    for value in SAFE_CONFIG_OVERRIDES:
        command.extend(("-c", value))
    command.extend(arguments)
    return command


def run_git(
    root: Path,
    *arguments: str,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
    text: bool = True,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[Any]:
    """Execute Git through the fixed production trust boundary."""

    try:
        return subprocess.run(
            safe_git_command(root, *arguments),
            cwd=root,
            env=safe_git_environment(
                root,
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            check=check,
            timeout=timeout,
            umask=0o077,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitSourceTrustError("trusted Git command failed") from exc


def repository_trust_evidence(
    root: Path,
    *,
    source_sha: str,
    source_tree: str,
    branch: str,
    origin: str | None,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
    verify_identity: bool = True,
) -> dict[str, Any]:
    """Seal the exact filesystem/config interpretation of a checkout."""

    if (
        SHA_RE.fullmatch(source_sha) is None
        or SHA_RE.fullmatch(source_tree) is None
        or branch != "refs/heads/main"
        or origin is not None
        and (
            not isinstance(origin, str)
            or not origin
            or "\x00" in origin
            or "\n" in origin
        )
    ):
        raise GitSourceTrustError("source commit/tree/branch identity is invalid")
    root = _require_absolute_root(root)
    git_dir = root / ".git"
    _require_directory(git_dir, label="Git metadata directory")
    for relative in FORBIDDEN_MARKERS:
        marker = git_dir / relative
        if marker.exists() or marker.is_symlink():
            raise GitSourceTrustError(
                f"forbidden Git storage or policy marker exists: {relative}"
            )
    config_record = _read_control_file(
        git_dir / "config",
        label="local Git config",
        required=True,
        maximum=MAX_CONFIG_BYTES,
    )
    head_record = _read_control_file(
        git_dir / "HEAD",
        label="Git HEAD",
        required=True,
        maximum=4096,
    )
    index_record = _read_control_file(
        git_dir / "index",
        label="Git index",
        required=True,
    )
    assert config_record is not None
    assert head_record is not None
    assert index_record is not None
    config_payload, config_metadata = config_record
    head_payload, head_metadata = head_record
    index_payload, index_metadata = index_record
    if head_payload.strip() != b"ref: refs/heads/main":
        raise GitSourceTrustError("Git HEAD is not exact local main")
    canonical_config = _canonical_config(config_payload)
    index_extensions = _index_extensions(index_payload)
    object_evidence = _object_store_evidence(git_dir / "objects")
    environment = safe_git_environment(
        root,
        ambient=ambient,
        home=home,
        ssh_command=ssh_command,
    )
    if verify_identity:
        try:
            observed_branch = run_git(
                root,
                "symbolic-ref",
                "--quiet",
                "HEAD",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_sha = run_git(
                root,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_tree = run_git(
                root,
                "rev-parse",
                "--verify",
                "HEAD^{tree}",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_top = run_git(
                root,
                "rev-parse",
                "--show-toplevel",
                ambient=ambient,
                home=home,
                ssh_command=ssh_command,
            ).stdout.strip()
            observed_origin = (
                run_git(
                    root,
                    "remote",
                    "get-url",
                    "origin",
                    ambient=ambient,
                    home=home,
                    ssh_command=ssh_command,
                ).stdout.strip()
                if origin is not None
                else None
            )
        except Exception as exc:
            raise GitSourceTrustError(
                "cannot independently verify Git source identity"
            ) from exc
        if (
            observed_branch != branch
            or observed_sha != source_sha
            or observed_tree != source_tree
            or observed_top != str(root)
            or observed_origin != origin
        ):
            raise GitSourceTrustError(
                "Git source identity differs from sealed evidence"
            )
        object_inventory = object_evidence["inventory_sha256"]
        if object_inventory not in _VERIFIED_OBJECT_STORES:
            try:
                run_git(
                    root,
                    "fsck",
                    "--full",
                    "--strict",
                    "--no-reflogs",
                    "--no-dangling",
                    ambient=ambient,
                    home=home,
                    ssh_command=ssh_command,
                    timeout=600,
                )
            except Exception as exc:
                raise GitSourceTrustError(
                    "Git object database failed strict verification"
                ) from exc
            _VERIFIED_OBJECT_STORES.add(object_inventory)
    # The SSH command binds credential *paths*, not credential bytes.  Secret
    # material remains outside evidence and is separately mode/hash governed.
    environment_evidence = {
        key: environment[key]
        for key in sorted(environment)
    }
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy": POLICY_NAME,
        "repository_root": str(root),
        "git_dir": str(git_dir),
        "object_dir": str(git_dir / "objects"),
        "index_path": str(git_dir / "index"),
        "source": {
            "sha": source_sha,
            "tree": source_tree,
            "branch": branch,
            "origin": origin,
        },
        "git_binary": _git_binary_evidence(),
        "local_config": {
            "mode": format(stat.S_IMODE(config_metadata.st_mode), "04o"),
            "size": config_metadata.st_size,
            "raw_sha256": sha256_bytes(config_payload),
            "canonical": canonical_config,
            "canonical_sha256": sha256_bytes(
                canonical_json_bytes(canonical_config)
            ),
            "includes": False,
            "conditional_includes": False,
            "external_worktree": False,
            "fsmonitor": False,
            "sparse_checkout": False,
            "promisor": False,
        },
        "head": {
            "mode": format(stat.S_IMODE(head_metadata.st_mode), "04o"),
            "sha256": sha256_bytes(head_payload),
            "symbolic_ref": "refs/heads/main",
        },
        "index": {
            "mode": format(stat.S_IMODE(index_metadata.st_mode), "04o"),
            "size": index_metadata.st_size,
            "sha256": sha256_bytes(index_payload),
            "version": struct.unpack(">I", index_payload[4:8])[0],
            "extensions": index_extensions,
            "external": False,
            "sparse": False,
            "fsmonitor": False,
            "split": False,
        },
        "refs": _refs_evidence(git_dir),
        "objects": object_evidence,
        "forbidden_markers_absent": list(FORBIDDEN_MARKERS),
        "execution_environment": {
            "keys": sorted(environment_evidence),
            "sha256": sha256_bytes(
                canonical_json_bytes(environment_evidence)
            ),
            "system_config": False,
            "global_config": False,
            "ambient_redirects": False,
            "replace_objects": False,
            "lazy_fetch": False,
        },
    }
    evidence["trust_surface_sha256"] = sha256_bytes(
        canonical_json_bytes(
            {
                key: value
                for key, value in evidence.items()
                if key != "source"
            }
        )
    )
    evidence["evidence_sha256"] = sha256_bytes(canonical_json_bytes(evidence))
    return evidence


def repository_preflight_evidence(
    root: Path,
    *,
    ambient: Mapping[str, str] | None = None,
    home: str = "/nonexistent",
    ssh_command: str | None = None,
) -> dict[str, Any]:
    """Validate the complete trust surface before the first Git invocation."""

    return repository_trust_evidence(
        root,
        source_sha="0" * 40,
        source_tree="0" * 40,
        branch="refs/heads/main",
        origin=None,
        ambient=ambient,
        home=home,
        ssh_command=ssh_command,
        verify_identity=False,
    )


def require_stable_trust_surface(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    before_digest = before.get("trust_surface_sha256")
    after_digest = after.get("trust_surface_sha256")
    if (
        not isinstance(before_digest, str)
        or DIGEST_RE.fullmatch(before_digest) is None
        or before_digest != after_digest
    ):
        raise GitSourceTrustError(
            "repository trust surface changed during verification"
        )


def require_stable_evidence(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    """Require exact evidence equality across a read-only identity operation."""

    before_digest = before.get("evidence_sha256")
    after_digest = after.get("evidence_sha256")
    if (
        not isinstance(before_digest, str)
        or DIGEST_RE.fullmatch(before_digest) is None
        or before != after
        or before_digest != after_digest
    ):
        raise GitSourceTrustError(
            "repository trust evidence changed during verification"
        )
