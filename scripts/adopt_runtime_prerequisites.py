#!/usr/bin/python3 -I
"""Adopt production prerequisites and one permission-hardening successor.

This tool runs from an exact private Git checkout.  It installs only tracked
configuration helpers for the original plan/apply transaction.  Its separate
permission-* transaction can owner-harden the adopted checkout and publish a
content-bound successor authority.  Neither transaction contacts PostgreSQL
or controls a service.
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


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


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
        or document.get("schema_version") != 2
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
        or document.get("schema_version") != 1
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
        bootstrap.get("schema_version") != 3
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
            authority_metadata = os.fstat(authority_fd)
            if _descriptor_bytes(authority_fd, maximum_bytes=maximum_bytes) != payload:
                raise PrerequisiteError("prerequisite authority path is occupied")
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
                    if _stat_identity(os.fstat(temporary_fd)) != _stat_identity(
                        authority_metadata
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
                temporary_payload = _descriptor_bytes(
                    temporary_fd,
                    maximum_bytes=maximum_bytes,
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
        temporary_fd = _open_private_regular_at(
            directory_fd,
            temporary_name,
            mode=0o600,
            allowed_nlinks=frozenset({2}),
        )
        try:
            if (
                _stat_identity(os.fstat(authority_fd))
                != _stat_identity(os.fstat(temporary_fd))
                or _descriptor_bytes(authority_fd, maximum_bytes=maximum_bytes)
                != payload
            ):
                raise PrerequisiteError(
                    "published prerequisite authority identity differs"
                )
        finally:
            os.close(authority_fd)
            os.close(temporary_fd)
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    final_fd = _open_private_regular_at(
        directory_fd,
        name,
        mode=0o600,
    )
    try:
        if _descriptor_bytes(final_fd, maximum_bytes=maximum_bytes) != payload:
            raise PrerequisiteError("published prerequisite authority differs")
    finally:
        os.close(final_fd)


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
            return self._transaction_directory_fd
        if "state" not in self._pinned_directories:
            raise PrerequisiteError("prerequisite state directory is not pinned")
        state_fd = self._pinned_directories["state"][0]
        try:
            os.mkdir(TRANSACTION_DIRECTORY.name, mode=0o700, dir_fd=state_fd)
            os.fsync(state_fd)
        except FileExistsError:
            pass
        transaction_fd = _open_private_directory(
            Path(TRANSACTION_DIRECTORY.name), parent_fd=state_fd
        )
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
            document.get("schema_version") != 1
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
            or plan.get("schema_version") != 1
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
        if not (
            _entry_exists_at(config_fd, temporary_name)
            or _entry_exists_at(config_fd, quarantine_name)
        ):
            return
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
        if state_fd is not None:
            return _load_json_at(state_fd, self.authority_path.name)
        return _load_json(self.authority_path)

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
                authority = self._authority(transaction)
                self._publish_authority(authority, operation_id)
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_transaction(transaction)
                return authority
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
            if transaction["status"] == "aborted":
                return transaction
            if transaction["status"] == "completed" or transaction["phase"] in {
                "authority-commit-intent",
                "completed",
            }:
                raise PrerequisiteError("prerequisite authority commit cannot be aborted")
            plan = dict(transaction["plan"])
            self._validate_plan_context(
                plan, source_sha, operation_id, durable=True
            )
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
            return _open_private_directory(
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
            os.fsync(state_fd)
        except FileExistsError:
            pass
        return _open_private_directory(
            Path(PERMISSION_TRANSACTION_DIRECTORY.name),
            parent_fd=state_fd,
        )

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
            or document.get("schema_version") != 1
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
        if state_fd is not None:
            return _load_json_at(
                state_fd,
                PERMISSION_AUTHORITY_PATH.name,
                require_single_link=require_single_link,
                maximum_bytes=PERMISSION_JSON_MAX_BYTES,
            )
        return _load_json(
            self.permission_authority_path,
            require_single_link=require_single_link,
            maximum_bytes=PERMISSION_JSON_MAX_BYTES,
        )

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
                    document.get("schema_version") != 1
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

    def _adoption_permission_context(self) -> dict[str, object]:
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
        if prepared_root.exists() or prepared_root.is_symlink():
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
            adopted.get("schema_version") != 1
            or adopted.get("status") != "adopted"
            or adopted.get("authority_kind") != ADOPTION_AUTHORITY_KIND
            or not isinstance(production_sha, str)
            or SHA_RE.fullmatch(production_sha) is None
            or not isinstance(production_tree, str)
            or SHA_RE.fullmatch(production_tree) is None
            or bootstrap.get("schema_version") != 3
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
            or base.get("schema_version") != 1
            or base.get("status") != "completed"
            or base.get("authority_kind") != AUTHORITY_KIND
            or OPERATION_RE.fullmatch(base_operation) is None
            or SHA_RE.fullmatch(base_source_sha) is None
            or SHA_RE.fullmatch(base_source_tree) is None
            or not isinstance(base_plan, dict)
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
            or plan.get("schema_version") != 1
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
                return authority
            if transaction["phase"] == "authority-commit-intent":
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
            if transaction["status"] == "aborted":
                return transaction
            if transaction["phase"] != "intent":
                raise PrerequisiteError(
                    "permission change intent is forward-only and cannot be aborted"
                )
            plan = dict(transaction["plan"])
            self._validate_permission_plan_context(
                plan, source_sha, operation_id, durable=True
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
            transaction["status"] = "aborted"
            transaction["phase"] = "aborted"
            transaction["aborted_at"] = _utc_now()
            self._write_permission_transaction(transaction)
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
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        if args.command.startswith("permission-"):
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
