#!/usr/bin/python3 -I
"""Publish the one-time adopted Git source-successor authority.

This program is deliberately self-contained and standard-library-only.  It
must be able to authorize reviewed changes to ``bootstrap_pull_deploy.py`` and
``git_source_trust.py`` without importing or executing either candidate blob.
Only verifier bytes named by the immutable predecessor authority are executed.

The apply transaction writes its private journal and one create-once runtime
authority.  It never changes Git content or refs, file permissions, units,
services, containers, credentials, or PostgreSQL.
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
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import types
from typing import Any, Callable, Mapping


sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"

SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
OPERATION_RE = re.compile(
    r"adopt-git-successor-[a-z0-9][a-z0-9._-]{7,95}\Z"
)
UTC_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)

AUTHORITY_KIND = "manual-runtime-adoption-git-permission-source-successor"
POLICY = "nexpoly-adopted-git-permission-source-successor-v1"
IMPACT_POLICY = "nexpoly-adopted-git-permission-source-successor-impact-v1"
VERIFIER_POLICY = "nexpoly-frozen-predecessor-verifier-agreement-v1"
PUBLICATION_POLICY = "nexpoly-source-successor-authority-publication-v1"
REPOSITORY_TRANSITION_POLICY = (
    "nexpoly-production-repository-materialization-transition-v1"
)
DEPLOY_REMOTE_REF = "refs/remotes/nexpoly-deploy/main"
PREPARED_REF_PREFIX = "refs/nexpoly/prepared/"
GIT_AUXILIARY_POLICY = (
    "baseline-exact-fetch-head-and-transition-reflogs-only-v1"
)
GIT_OBJECT_STORAGE_POLICY = (
    "canonical-loose-pack-index-rev-commit-graph-no-locks-v1"
)

AUTHORITY_RELATIVE_PATH = Path(
    "state/adopted-git-permission-source-successor.json"
)
TRANSACTION_RELATIVE_DIRECTORY = Path(
    "state/adopted-git-permission-source-successor-transactions"
)
TRANSACTION_STAGING_PREFIX = (
    f".{TRANSACTION_RELATIVE_DIRECTORY.name}.create-"
)
PREDECESSOR_AUTHORITY_RELATIVE_PATH = Path(
    "state/adopted-git-permissions.json"
)
PREDECESSOR_TRANSACTION_RELATIVE_DIRECTORY = Path(
    "state/adopted-git-permission-transactions"
)
ADOPTED_DEPLOYMENT_RELATIVE_PATH = Path("state/adopted-deployment.json")
BOOTSTRAP_CONTROL_RELATIVE_PATH = Path("state/bootstrap-control.json")
ADOPTED_PREREQUISITES_RELATIVE_PATH = Path(
    "state/adopted-prerequisites.json"
)
PERMISSION_MARKER_RELATIVE_PATH = Path(
    "state/legacy-git-permission-takeover.json"
)

JSON_MAX_BYTES = 32 * 1024 * 1024
EXCHANGE_EVIDENCE_MAX_BYTES = JSON_MAX_BYTES * 2
PREDECESSOR_MAX_BYTES = 256 * 1024 * 1024
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
AT_EMPTY_PATH = 0x1000
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400

EXCHANGE_EVIDENCE_POLICY = (
    "nexpoly-source-successor-journal-exchange-evidence-v1"
)
EXCHANGE_IDENTITY_FIELDS = {
    "raw_sha256",
    "device",
    "inode",
    "mode",
    "uid",
    "gid",
    "size",
}
EXCHANGE_EVIDENCE_FIELDS = {
    "schema_version",
    "policy",
    "operation_id",
    "journal_name",
    "temporary_name",
    "directory_device",
    "directory_inode",
    "current",
    "staging",
    "staging_document",
}

TRANSACTION_PHASES = frozenset(
    {
        "intent",
        "predecessor-verified",
        "source-verified",
        "authority-commit-intent",
        "completed",
        "aborted",
    }
)

TRACKED_SOURCE_FILES = (
    "ops/config/bootstrap-quiesce.example",
    "ops/config/bootstrap-status.example",
    "ops/config/bootstrap-resume-unchanged.example",
    "ops/config/bootstrap-rollback.example",
    "ops/config/bootstrap-active-jobs-probe.example",
    "ops/config/bootstrap-legacy-runtime-status.example",
    "ops/config/bootstrap-legacy-runtime-resume-unchanged.example",
    "ops/config/bootstrap-legacy-runtime-restore.example",
    "ops/config/deployment-mutable-data-audit.example",
    "ops/config/mutable-data-audit.pg_service.conf.example",
    "scripts/bootstrap_pull_deploy.py",
    "scripts/git_source_trust.py",
    "scripts/bridge_deploy_core.py",
)
CHANGED_PATHS = (
    "scripts/bootstrap_pull_deploy.py",
    "scripts/git_source_trust.py",
)
BOOTSTRAP_PATH = "scripts/bootstrap_pull_deploy.py"
GIT_TRUST_PATH = "scripts/git_source_trust.py"
CI_CONTRACT_PATH = "scripts/bridge_deploy_core.py"

# This is intentionally a one-shot transition, not a generic signer.  The
# successor implementation commit may advance, but these candidate blobs must
# remain the exact T2-reviewed bytes.
EXPECTED_CHANGED_TRANSITIONS: Mapping[str, Mapping[str, str]] = {
    BOOTSTRAP_PATH: {
        "predecessor_blob_sha": "f2b0c71d6792c75f8dd85254b46676fcc801961a",
        "predecessor_sha256": (
            "sha256:7ec0a724716fd1df5b640f8ea3ca01d882ed42f7e368c0f3ffa259ea41b3a826"
        ),
        "target_blob_sha": "24fe7a933ed59944e9012ab9c458178ec385cbe3",
        "target_sha256": (
            "sha256:26c66582aec47bef9fc9d68e8abca136fb14cb9ca1659797b9f84fb5f0ce6367"
        ),
    },
    GIT_TRUST_PATH: {
        "predecessor_blob_sha": "b967f4aa0f092f763694fad667f3bc95eafdf132",
        "predecessor_sha256": (
            "sha256:91b511138795d780434f4862b018b845ba01992c894ad9ddbd28471cb15a3bbe"
        ),
        "target_blob_sha": "87711d349454aa9fca6ca3086ea1238e69542c1c",
        "target_sha256": (
            "sha256:4c1320a0d240fb1134cda11b9db775b104df1869bb7fb0c72c5ec73c13b944f9"
        ),
    },
}

EXPECTED_PREDECESSOR_PROVENANCE: Mapping[str, str] = {
    "predecessor_source_sha": "78d752f2377b10a434a01f2b18511d552ac7fe0b",
    "predecessor_source_tree": "0975ded2752f2e800f1a37331ad8ae0be0c3e473",
    "predecessor_authority_sha256": (
        "sha256:5ec0983614ba071987dec6708b8a69ebf2c12d2a835aea57053118c2b9747561"
    ),
    "predecessor_marker_sha256": (
        "sha256:ac80d137872e3a8b1f187595ec5f8de52b5ff727389ca67bbdd4bd5c8365d2ed"
    ),
    "predecessor_journal_sha256": (
        "sha256:a10dc810916cde39a4754d7ebcc7c294f28258009d17578f8429a4793fc0f951"
    ),
    "adopted_deployment_sha256": (
        "sha256:03205fe74bccfc5d122386d2d58d84996cf21e061ce26285e405a1e243560d1b"
    ),
    "bootstrap_control_sha256": (
        "sha256:01f861239fdb4843541f4c17ef5e01cf678eb8046696075236bcd43e9500d214"
    ),
    "adopted_prerequisites_sha256": (
        "sha256:95864e13e5a413d0a1659a389362d39b765d8a96d1eeb5887292e53c703b3474"
    ),
    "production_source_sha": "fc05ad3c6d930eb329889e18dd546fda0cc10429",
    "production_source_tree": "f13d161713a99c2b6ea60a2ece8caaada6d40e43",
    "predecessor_source_trust_sha256": (
        "sha256:dd8c493199fd02daf621e7ffbcd51ca35ebf7da0e6f77fefd0759137c7a408d4"
    ),
    "production_source_trust_sha256": (
        "sha256:ba12709eb87ebc3ca51ac6ebcaca425be50487420c3529b80ec8696cb8602a3b"
    ),
}

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

PLAN_FIELDS = {
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

AUTHORITY_FIELDS = {
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

TRANSACTION_FIELDS = {
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

MUTATIONS = {
    "services": False,
    "source": False,
    "source_refs": False,
    "database": False,
    "credentials": False,
    "git_permissions": False,
    "units": False,
    "runtime_authority": True,
}


class SuccessorError(RuntimeError):
    """The source-successor authority cannot be proven safely."""


def _require_sha(value: object, label: str = "SHA") -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise SuccessorError(f"{label} is invalid")
    return value


def _require_digest(value: object, label: str = "digest") -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise SuccessorError(f"{label} is invalid")
    return value


def _require_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_RE.fullmatch(value) is None:
        raise SuccessorError("source successor operation ID is invalid")
    return value


def _has_exact_schema(value: Mapping[str, object], version: int) -> bool:
    observed = value.get("schema_version")
    return type(observed) is int and observed == version


def _utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _valid_ref_name(value: str) -> bool:
    """Apply the refname restrictions needed by the sealed raw inventory."""

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


def _valid_ref_directory(value: str) -> bool:
    if value == "refs":
        return True
    return _valid_ref_name(f"{value}/sentinel")


def _logical_ref_inventory(
    trust: types.ModuleType,
    root: Path,
) -> list[dict[str, object]]:
    try:
        raw = trust.run_git(
            root,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(symref)",
            ambient={},
        ).stdout
    except BaseException as exc:
        raise SuccessorError("production logical refs cannot be enumerated") from exc
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > JSON_MAX_BYTES:
        raise SuccessorError("production logical ref inventory is invalid")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        fields = line.split("\0")
        if len(fields) != 4:
            raise SuccessorError("production logical ref inventory is malformed")
        name, object_sha, object_type, symbolic_target = fields
        if (
            not _valid_ref_name(name)
            or name.startswith("refs/replace/")
            or name in seen
            or object_type not in {"blob", "tree", "commit", "tag"}
            or symbolic_target
            and not _valid_ref_name(symbolic_target)
        ):
            raise SuccessorError("production logical ref name is unsafe")
        seen.add(name)
        records.append(
            {
                "name": name,
                "object_sha": _require_sha(object_sha, "ref object"),
                "object_type": object_type,
                "symbolic_target": symbolic_target or None,
            }
        )
    records.sort(key=lambda item: item["name"])
    if len(records) > 10000:
        raise SuccessorError("production logical ref inventory is oversized")
    return records


def _target_reachable_objects(root: Path, target_sha: str) -> list[str]:
    try:
        raw = str(
            _run_git(
                root,
                "rev-list",
                "--objects",
                "--no-object-names",
                target_sha,
                text=True,
            )
        )
    except BaseException as exc:
        raise SuccessorError("target reachable objects cannot be enumerated") from exc
    objects = sorted({line.strip() for line in raw.splitlines() if line.strip()})
    if not objects or target_sha not in objects or len(objects) > 10_000_000:
        raise SuccessorError("target reachable object inventory is invalid")
    for object_sha in objects:
        _require_sha(object_sha, "target reachable object")
    return objects


def _parse_semantic_object_inventory(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > PREDECESSOR_MAX_BYTES:
        raise SuccessorError("Git semantic object inventory is invalid")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        fields = line.split(" ")
        if len(fields) != 3:
            raise SuccessorError("Git semantic object inventory is malformed")
        object_sha, object_type, raw_size = fields
        object_sha = _require_sha(object_sha, "Git semantic object")
        if object_sha in seen or object_type not in {"blob", "tree", "commit", "tag"}:
            raise SuccessorError("Git semantic object identity is invalid")
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise SuccessorError("Git semantic object size is invalid") from exc
        if size < 0 or size > 1024**4:
            raise SuccessorError("Git semantic object size is invalid")
        seen.add(object_sha)
        records.append(
            {"oid": object_sha, "type": object_type, "size": size}
        )
    records.sort(key=lambda record: str(record["oid"]))
    if len(records) > 10_000_000:
        raise SuccessorError("Git semantic object inventory is oversized")
    return records


def _all_semantic_objects(
    root: Path,
    *,
    trust: types.ModuleType | None = None,
) -> list[dict[str, object]]:
    arguments = (
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
    )
    try:
        raw = (
            trust.run_git(root, *arguments, ambient={}, timeout=600).stdout
            if trust is not None
            else _run_git(root, *arguments, text=True, timeout=600)
        )
    except BaseException as exc:
        raise SuccessorError("Git semantic objects cannot be enumerated") from exc
    return _parse_semantic_object_inventory(raw)


def _target_reachable_semantic_objects(
    root: Path,
    target_sha: str,
) -> list[dict[str, object]]:
    reachable = set(_target_reachable_objects(root, target_sha))
    all_objects = {
        str(record["oid"]): record for record in _all_semantic_objects(root)
    }
    if not reachable.issubset(all_objects):
        raise SuccessorError("target reachable semantic objects are incomplete")
    return [all_objects[object_sha] for object_sha in sorted(reachable)]


def _raw_ref_inventory(
    root: Path,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Read every raw ref entry without following links or accepting residue."""

    git_dir = root / ".git"
    refs_root = git_dir / "refs"
    records: list[dict[str, object]] = []
    trust_order_loose_records: list[dict[str, str]] = []
    refs_fd = _open_private_directory(refs_root)
    try:
        expected_directories = {
            refs_root: _directory_identity(os.fstat(refs_fd))
        }
    finally:
        os.close(refs_fd)
    for directory, directory_names, file_names in os.walk(
        refs_root,
        topdown=True,
        followlinks=False,
    ):
        file_names.sort()
        current = Path(directory)
        relative_directory = current.relative_to(git_dir).as_posix()
        if not _valid_ref_directory(relative_directory):
            raise SuccessorError("production raw ref directory is unsafe")
        directory_fd = _open_private_directory(current)
        try:
            before = os.fstat(directory_fd)
            expected_identity = expected_directories.pop(current, None)
            if (
                expected_identity is None
                or _directory_identity(before) != expected_identity
            ):
                raise SuccessorError(
                    "production raw ref directory escaped its parent"
                )
            child_identities = _private_child_directory_identities_at(
                directory_fd,
                directory_names,
                label="production raw ref",
            )
            for name, identity in child_identities.items():
                expected_directories[current / name] = identity
            expected_entries = sorted([*directory_names, *file_names])
            if sorted(os.listdir(directory_fd)) != expected_entries:
                raise SuccessorError("production raw ref directory changed")
            records.append(
                {
                    "path": relative_directory,
                    "kind": "directory",
                    "mode": format(stat.S_IMODE(before.st_mode), "04o"),
                }
            )
            for name in file_names:
                relative = (current / name).relative_to(git_dir).as_posix()
                if not _valid_ref_name(relative):
                    raise SuccessorError("production raw ref file is unsafe")
                descriptor = _open_private_regular_at(directory_fd, name)
                try:
                    payload, metadata = _read_descriptor(
                        descriptor,
                        maximum_bytes=1024 * 1024,
                        label=relative,
                    )
                    observed = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if _stable_identity(observed) != _stable_identity(metadata):
                        raise SuccessorError("production raw ref changed")
                finally:
                    os.close(descriptor)
                records.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                        "size": len(payload),
                        "raw_sha256": _digest_bytes(payload),
                    }
                )
                trust_order_loose_records.append(
                    {
                        "path": relative,
                        "mode": format(
                            stat.S_IMODE(metadata.st_mode), "04o"
                        ),
                        "sha256": _digest_bytes(payload),
                    }
                )
            if (
                _directory_identity(os.fstat(directory_fd))
                != _directory_identity(before)
                or sorted(os.listdir(directory_fd)) != expected_entries
                or _private_child_directory_identities_at(
                    directory_fd,
                    directory_names,
                    label="production raw ref",
                )
                != child_identities
            ):
                raise SuccessorError("production raw ref directory changed")
        finally:
            os.close(directory_fd)
    if expected_directories:
        raise SuccessorError(
            "production raw ref traversal is not self-contained"
        )
    git_fd = _open_private_directory(git_dir)
    try:
        if _entry_exists_at(git_fd, "packed-refs"):
            descriptor = _open_private_regular_at(git_fd, "packed-refs")
            try:
                payload, metadata = _read_descriptor(
                    descriptor,
                    maximum_bytes=JSON_MAX_BYTES,
                    label="packed-refs",
                )
                observed = os.stat(
                    "packed-refs", dir_fd=git_fd, follow_symlinks=False
                )
                if _stable_identity(observed) != _stable_identity(metadata):
                    raise SuccessorError("packed refs changed while reading")
            finally:
                os.close(descriptor)
            records.append(
                {
                    "path": "packed-refs",
                    "kind": "file",
                    "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                    "size": len(payload),
                    "raw_sha256": _digest_bytes(payload),
                }
            )
    finally:
        os.close(git_fd)
    return (
        sorted(records, key=lambda item: str(item["path"])),
        trust_order_loose_records,
    )


def _owner_private_metadata(path: Path, *, directory: bool) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SuccessorError(f"Git metadata is unavailable: {path}") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_type(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or not directory
        and metadata.st_nlink != 1
    ):
        raise SuccessorError(f"Git metadata is not owner-private: {path}")
    return metadata


def _owner_private_payload(path: Path, *, maximum_bytes: int) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SuccessorError(f"Git metadata cannot be opened: {path}") from exc
    try:
        payload, metadata = _read_descriptor(
            descriptor,
            maximum_bytes=maximum_bytes,
            label=str(path),
        )
    finally:
        os.close(descriptor)
    if (
        metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_nlink != 1
    ):
        raise SuccessorError(f"Git metadata is not owner-private: {path}")
    return payload, metadata


def _git_auxiliary_inventory(root: Path) -> list[dict[str, object]]:
    """Seal Git metadata outside refs/objects and reject transaction locks."""

    git_dir = root / ".git"
    _owner_private_metadata(git_dir, directory=True)
    excluded_files = {"HEAD", "config", "index", "packed-refs"}
    records: list[dict[str, object]] = []
    total_bytes = 0
    git_fd = _open_private_directory(git_dir)
    try:
        expected_directories = {
            git_dir: _directory_identity(os.fstat(git_fd))
        }
    finally:
        os.close(git_fd)
    for directory, directory_names, file_names in os.walk(
        git_dir, topdown=True, followlinks=False
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        relative_directory = current.relative_to(git_dir).as_posix()
        directory_fd = _open_private_directory(current)
        try:
            before = os.fstat(directory_fd)
            expected_identity = expected_directories.pop(current, None)
            if (
                expected_identity is None
                or _directory_identity(before) != expected_identity
            ):
                raise SuccessorError(
                    "Git auxiliary traversal escaped its parent"
                )
            all_child_names = list(directory_names)
            child_identities = _private_child_directory_identities_at(
                directory_fd,
                all_child_names,
                label="Git auxiliary",
            )
            expected_entries = sorted([*all_child_names, *file_names])
            if sorted(os.listdir(directory_fd)) != expected_entries:
                raise SuccessorError(
                    "Git auxiliary directory changed while reading"
                )
            if current == git_dir:
                for excluded in ("objects", "refs"):
                    if excluded in directory_names:
                        directory_names.remove(excluded)
            for name in directory_names:
                expected_directories[current / name] = child_identities[name]
            if current != git_dir:
                records.append(
                    {
                        "path": relative_directory,
                        "kind": "directory",
                        "mode": format(
                            stat.S_IMODE(before.st_mode), "04o"
                        ),
                    }
                )
            for name in file_names:
                relative = (current / name).relative_to(git_dir).as_posix()
                if name.endswith(".lock"):
                    raise SuccessorError(
                        f"Git transaction lock remains: {relative}"
                    )
                if current == git_dir and name in excluded_files:
                    _owner_private_payload(
                        current / name,
                        maximum_bytes=PREDECESSOR_MAX_BYTES,
                    )
                    continue
                payload, metadata = _owner_private_payload(
                    current / name,
                    maximum_bytes=PREDECESSOR_MAX_BYTES,
                )
                total_bytes += len(payload)
                if total_bytes > PREDECESSOR_MAX_BYTES:
                    raise SuccessorError(
                        "Git auxiliary inventory is oversized"
                    )
                records.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "mode": format(
                            stat.S_IMODE(metadata.st_mode), "04o"
                        ),
                        "size": len(payload),
                        "raw_sha256": _digest_bytes(payload),
                    }
                )
            if (
                _directory_identity(os.fstat(directory_fd))
                != _directory_identity(before)
                or sorted(os.listdir(directory_fd)) != expected_entries
                or _private_child_directory_identities_at(
                    directory_fd,
                    all_child_names,
                    label="Git auxiliary",
                )
                != child_identities
            ):
                raise SuccessorError(
                    "Git auxiliary directory changed while reading"
                )
        finally:
            os.close(directory_fd)
    if expected_directories:
        raise SuccessorError("Git auxiliary traversal is not self-contained")
    records.sort(key=lambda record: str(record["path"]))
    paths = [str(record["path"]) for record in records]
    if paths != sorted(set(paths)):
        raise SuccessorError("Git auxiliary inventory is not canonical")
    return records


def _valid_object_storage_directory(path: str) -> bool:
    return bool(
        path in {"info", "pack", "info/commit-graphs"}
        or re.fullmatch(r"[0-9a-f]{2}", path)
    )


def _valid_object_storage_file(path: str) -> bool:
    return bool(
        re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{38}", path)
        or re.fullmatch(
            r"pack/pack-[0-9a-f]{40}\.(?:pack|idx|rev|bitmap|mtimes)",
            path,
        )
        or path in {
            "pack/multi-pack-index",
            "info/commit-graph",
            "info/packs",
            "info/commit-graphs/commit-graph-chain",
        }
        or re.fullmatch(
            r"pack/multi-pack-index-[0-9a-f]{40}\.bitmap", path
        )
        or re.fullmatch(
            r"info/commit-graphs/graph-[0-9a-f]{40}\.graph", path
        )
    )


def _git_object_storage_inventory(root: Path) -> list[dict[str, object]]:
    """Reject interrupted pack/lock residue and seal canonical storage paths."""

    objects = root / ".git/objects"
    _owner_private_metadata(objects, directory=True)
    records: list[dict[str, object]] = []
    objects_fd = _open_private_directory(objects)
    try:
        expected_directories = {
            objects: _directory_identity(os.fstat(objects_fd))
        }
    finally:
        os.close(objects_fd)
    for directory, directory_names, file_names in os.walk(
        objects, topdown=True, followlinks=False
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        relative_directory = current.relative_to(objects).as_posix()
        directory_fd = _open_private_directory(current)
        try:
            before = os.fstat(directory_fd)
            expected_identity = expected_directories.pop(current, None)
            if (
                expected_identity is None
                or _directory_identity(before) != expected_identity
            ):
                raise SuccessorError(
                    "Git object traversal escaped its parent"
                )
            child_identities = _private_child_directory_identities_at(
                directory_fd,
                directory_names,
                label="Git object storage",
            )
            expected_entries = sorted([*directory_names, *file_names])
            if sorted(os.listdir(directory_fd)) != expected_entries:
                raise SuccessorError(
                    "Git object directory changed while reading"
                )
            for name, identity in child_identities.items():
                expected_directories[current / name] = identity
            if current != objects:
                if not _valid_object_storage_directory(relative_directory):
                    raise SuccessorError(
                        "Git object directory is not canonical: "
                        + relative_directory
                    )
                records.append(
                    {
                        "path": relative_directory,
                        "kind": "directory",
                        "mode": format(
                            stat.S_IMODE(before.st_mode), "04o"
                        ),
                    }
                )
            for name in file_names:
                path = current / name
                relative = path.relative_to(objects).as_posix()
                if name.endswith(".lock") or not _valid_object_storage_file(
                    relative
                ):
                    raise SuccessorError(
                        f"Git object file is not canonical: {relative}"
                    )
                metadata = _owner_private_metadata(path, directory=False)
                records.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "mode": format(
                            stat.S_IMODE(metadata.st_mode), "04o"
                        ),
                        "size": metadata.st_size,
                    }
                )
            if (
                _directory_identity(os.fstat(directory_fd))
                != _directory_identity(before)
                or sorted(os.listdir(directory_fd)) != expected_entries
                or _private_child_directory_identities_at(
                    directory_fd,
                    directory_names,
                    label="Git object storage",
                )
                != child_identities
            ):
                raise SuccessorError(
                    "Git object directory changed while reading"
                )
        finally:
            os.close(directory_fd)
    if expected_directories:
        raise SuccessorError("Git object traversal is not self-contained")
    records.sort(key=lambda record: str(record["path"]))
    paths = [str(record["path"]) for record in records]
    if paths != sorted(set(paths)):
        raise SuccessorError("Git object storage inventory is not canonical")
    files = {
        str(record["path"])
        for record in records
        if record["kind"] == "file"
    }
    pack_bases = {
        path[: -len(".pack")]
        for path in files
        if path.endswith(".pack")
    }
    if any(f"{base}.idx" not in files for base in pack_bases) or any(
        path.rsplit(".", 1)[0] not in pack_bases
        for path in files
        if re.fullmatch(
            r"pack/pack-[0-9a-f]{40}\.(?:idx|rev|bitmap|mtimes)", path
        )
    ):
        raise SuccessorError("Git pack sidecar set is incomplete")
    return records


def _repository_stable_projection(
    evidence: Mapping[str, object],
    *,
    root: Path,
    source_sha: str,
    source_tree: str,
) -> dict[str, object]:
    expected_fields = {
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
        "refs",
        "objects",
        "forbidden_markers_absent",
        "execution_environment",
        "trust_surface_sha256",
        "evidence_sha256",
    }
    if set(evidence) != expected_fields:
        raise SuccessorError("production trust evidence shape differs")
    digest = _require_digest(
        evidence.get("evidence_sha256"), "production trust evidence"
    )
    unhashed = dict(evidence)
    unhashed.pop("evidence_sha256", None)
    if digest != _canonical_digest(unhashed):
        raise SuccessorError("production trust evidence digest differs")
    source = {
        "sha": source_sha,
        "tree": source_tree,
        "branch": "refs/heads/main",
        "origin": None,
    }
    if (
        evidence.get("repository_root") != str(root)
        or evidence.get("git_dir") != str(root / ".git")
        or evidence.get("object_dir") != str(root / ".git/objects")
        or evidence.get("index_path") != str(root / ".git/index")
        or evidence.get("source") != source
    ):
        raise SuccessorError("production trust source projection differs")
    return {
        key: value
        for key, value in evidence.items()
        if key
        not in {
            "refs",
            "objects",
            "trust_surface_sha256",
            "evidence_sha256",
        }
    }


def _validate_repository_transition(
    document: object,
    *,
    production_root: Path,
    production_sha: str,
    production_tree: str,
    target_sha: str,
    target_tree: str,
    evidence: Mapping[str, object] | None = None,
    trust_order_loose_records: list[dict[str, str]] | None = None,
) -> dict[str, object]:
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
        not isinstance(document, dict)
        or set(document) != fields
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("policy") != REPOSITORY_TRANSITION_POLICY
        or document.get("source")
        != {"sha": production_sha, "tree": production_tree}
        or document.get("target")
        != {"sha": target_sha, "tree": target_tree}
        or document.get("mutable_refs")
        != {
            "deploy_remote": DEPLOY_REMOTE_REF,
            "prepared_prefix": PREPARED_REF_PREFIX,
        }
        or document.get("storage_policy")
        != {
            "standalone": True,
            "promisor": False,
            "alternates": False,
            "replace_refs": 0,
        }
        or document.get("object_materialization_policy")
        != "strict-fsck-owner-private-content-addressed-target-closure-v1"
        or document.get("auxiliary_policy") != GIT_AUXILIARY_POLICY
        or document.get("object_storage_policy")
        != GIT_OBJECT_STORAGE_POLICY
    ):
        raise SuccessorError("production repository transition is invalid")
    counts: dict[str, int] = {}
    for field, allow_zero in (
        ("baseline_semantic_object_count", True),
        ("baseline_only_object_count", True),
        ("target_reachable_object_count", False),
        ("expected_materialized_object_count", False),
    ):
        value = document.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < (0 if allow_zero else 1)
            or value > 10_000_000
        ):
            raise SuccessorError(f"{field} is invalid")
        counts[field] = value
    for field in (
        "baseline_semantic_objects_sha256",
        "baseline_only_objects_sha256",
        "target_reachable_objects_sha256",
        "expected_materialized_objects_sha256",
    ):
        _require_digest(document.get(field), field)
    if (
        counts["expected_materialized_object_count"]
        != counts["baseline_only_object_count"]
        + counts["target_reachable_object_count"]
        or counts["expected_materialized_object_count"]
        < counts["baseline_semantic_object_count"]
    ):
        raise SuccessorError("materialized semantic object counts differ")
    baseline_digest = _require_digest(
        document.get("baseline_evidence_sha256"),
        "production transition baseline evidence",
    )
    stable = document.get("stable_projection")
    if (
        not isinstance(stable, dict)
        or document.get("stable_projection_sha256")
        != _canonical_digest(stable)
    ):
        raise SuccessorError("production stable projection digest differs")
    if evidence is not None:
        objects = evidence.get("objects")
        refs_evidence = evidence.get("refs")
        if (
            not isinstance(objects, dict)
            or not isinstance(refs_evidence, dict)
            or {
                "standalone": objects.get("standalone"),
                "promisor": objects.get("promisor"),
                "alternates": objects.get("alternates"),
                "replace_refs": refs_evidence.get("replace_refs"),
            }
            != document["storage_policy"]
        ):
            raise SuccessorError("production storage policy differs")
        expected_stable = _repository_stable_projection(
            evidence,
            root=production_root,
            source_sha=production_sha,
            source_tree=production_tree,
        )
        if (
            stable != expected_stable
            or baseline_digest != evidence.get("evidence_sha256")
        ):
            raise SuccessorError("production stable projection differs")
    logical = document.get("logical_refs")
    if not isinstance(logical, list) or document.get(
        "logical_refs_sha256"
    ) != _canonical_digest(logical):
        raise SuccessorError("production logical ref digest differs")
    logical_names: list[str] = []
    for record in logical:
        if (
            not isinstance(record, dict)
            or set(record)
            != {"name", "object_sha", "object_type", "symbolic_target"}
            or not isinstance(record.get("name"), str)
            or not _valid_ref_name(record["name"])
            or record["name"].startswith("refs/replace/")
            or record.get("object_type")
            not in {"blob", "tree", "commit", "tag"}
            or record.get("symbolic_target") is not None
            and (
                not isinstance(record.get("symbolic_target"), str)
                or not _valid_ref_name(str(record["symbolic_target"]))
            )
        ):
            raise SuccessorError("production logical ref record is invalid")
        _require_sha(record.get("object_sha"), "production logical ref object")
        logical_names.append(record["name"])
    logical_by_name = {
        str(record["name"]): record for record in logical
    }
    main_ref = logical_by_name.get("refs/heads/main")
    deploy_ref = logical_by_name.get(DEPLOY_REMOTE_REF)
    if (
        logical_names != sorted(set(logical_names))
        or len(logical_names) > 10000
        or any(name.startswith(PREPARED_REF_PREFIX) for name in logical_names)
        or not isinstance(main_ref, dict)
        or main_ref.get("object_sha") != production_sha
        or main_ref.get("object_type") != "commit"
        or main_ref.get("symbolic_target") is not None
        or not isinstance(deploy_ref, dict)
        or deploy_ref.get("object_type") != "commit"
        or deploy_ref.get("symbolic_target") is not None
    ):
        raise SuccessorError("production logical ref baseline is invalid")
    raw = document.get("raw_ref_inventory")
    if not isinstance(raw, list) or document.get(
        "raw_ref_inventory_sha256"
    ) != _canonical_digest(raw):
        raise SuccessorError("production raw ref inventory digest differs")
    paths: list[str] = []
    packed_digest: str | None = None
    for record in raw:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise SuccessorError("production raw ref record is invalid")
        path = record["path"]
        paths.append(path)
        if record.get("kind") == "directory":
            if (
                set(record) != {"path", "kind", "mode"}
                or not _valid_ref_directory(path)
                or record.get("mode") != "0700"
            ):
                raise SuccessorError("production raw ref directory is invalid")
        elif record.get("kind") == "file":
            if (
                set(record)
                != {"path", "kind", "mode", "size", "raw_sha256"}
                or record.get("mode") != "0600"
                or isinstance(record.get("size"), bool)
                or not isinstance(record.get("size"), int)
                or record["size"] < 0
                or record["size"] > JSON_MAX_BYTES
            ):
                raise SuccessorError("production raw ref file is invalid")
            digest = _require_digest(
                record.get("raw_sha256"), "production raw ref payload"
            )
            if path == "packed-refs":
                packed_digest = digest
            elif _valid_ref_name(path) and path in logical_names:
                pass
            else:
                raise SuccessorError("production raw ref file has no logical ref")
        else:
            raise SuccessorError("production raw ref kind is invalid")
    if paths != sorted(set(paths)) or "refs" not in paths:
        raise SuccessorError("production raw ref inventory is not canonical")
    if evidence is not None:
        if trust_order_loose_records is None:
            raise SuccessorError("production trust-order refs are missing")
        refs = evidence.get("refs")
        expected_refs = {
            "loose_count": len(trust_order_loose_records),
            "loose_sha256": _canonical_digest(trust_order_loose_records),
            "packed_refs_sha256": packed_digest,
            "replace_refs": 0,
        }
        if refs != expected_refs:
            raise SuccessorError("production raw refs differ from trust evidence")
    auxiliary = document.get("baseline_auxiliary_inventory")
    if (
        not isinstance(auxiliary, list)
        or document.get("baseline_auxiliary_inventory_sha256")
        != _canonical_digest(auxiliary)
    ):
        raise SuccessorError("production Git auxiliary inventory differs")
    auxiliary_paths: list[str] = []
    for record in auxiliary:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise SuccessorError("production Git auxiliary record is invalid")
        path = str(record["path"])
        auxiliary_paths.append(path)
        if (
            path.startswith(("objects/", "refs/"))
            or path in {"objects", "refs", "HEAD", "config", "index", "packed-refs"}
            or path.endswith(".lock")
            or path.startswith(f"logs/{PREPARED_REF_PREFIX}")
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or Path(path).as_posix() != path
        ):
            raise SuccessorError("production Git auxiliary path is invalid")
        if record.get("kind") == "directory":
            if (
                set(record) != {"path", "kind", "mode"}
                or not isinstance(record.get("mode"), str)
                or re.fullmatch(r"0[4-7]00", str(record["mode"])) is None
            ):
                raise SuccessorError("production Git auxiliary directory is invalid")
        elif record.get("kind") == "file":
            if (
                set(record) != {"path", "kind", "mode", "size", "raw_sha256"}
                or not isinstance(record.get("mode"), str)
                or re.fullmatch(r"0[4-7]00", str(record["mode"])) is None
                or isinstance(record.get("size"), bool)
                or not isinstance(record.get("size"), int)
                or record["size"] < 0
                or record["size"] > PREDECESSOR_MAX_BYTES
            ):
                raise SuccessorError("production Git auxiliary file is invalid")
            _require_digest(record.get("raw_sha256"), "Git auxiliary payload")
        else:
            raise SuccessorError("production Git auxiliary record is invalid")
    if auxiliary_paths != sorted(set(auxiliary_paths)):
        raise SuccessorError("production Git auxiliary inventory is not canonical")
    return dict(document)


def _stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
    )


def _rename_stable_inode_identity(
    metadata: os.stat_result,
) -> tuple[int, ...]:
    """Identity fields unchanged by a legitimate same-directory rename."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
    )


def _entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _open_private_directory(
    path: Path,
    *,
    parent_fd: int | None = None,
) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise SuccessorError(f"private directory is unavailable: {path}") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise SuccessorError(f"private directory is unsafe: {path}")
    return descriptor


def _private_child_directory_identities_at(
    directory_fd: int,
    names: list[str],
    *,
    label: str,
) -> dict[str, tuple[int, ...]]:
    """Pin every direct child directory without following a link."""

    identities: dict[str, tuple[int, ...]] = {}
    for name in names:
        if not name or name in {".", ".."} or "/" in name or "\0" in name:
            raise SuccessorError(f"{label} child directory name is unsafe")
        child_fd = _open_private_directory(
            Path(name),
            parent_fd=directory_fd,
        )
        try:
            held = os.fstat(child_fd)
            observed = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            identity = _directory_identity(held)
            if (
                not stat.S_ISDIR(observed.st_mode)
                or _directory_identity(observed) != identity
            ):
                raise SuccessorError(
                    f"{label} child directory identity differs: {name}"
                )
            identities[name] = identity
        finally:
            os.close(child_fd)
    return identities


def _open_private_regular_at(
    directory_fd: int,
    name: str,
    *,
    mode: int = 0o600,
    allowed_nlinks: frozenset[int] = frozenset({1}),
    writable: bool = False,
) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise SuccessorError(f"private file is unavailable: {name}") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_nlink not in allowed_nlinks
    ):
        os.close(descriptor)
        raise SuccessorError(f"private file is unsafe: {name}")
    return descriptor


def _read_descriptor(
    descriptor: int,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[bytes, os.stat_result]:
    before = os.fstat(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = bytearray()
    while True:
        block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1))
        if not block:
            break
        payload.extend(block)
        if len(payload) > maximum_bytes:
            raise SuccessorError(f"{label} is oversized")
    after = os.fstat(descriptor)
    if _stable_identity(before) != _stable_identity(after):
        raise SuccessorError(f"{label} changed while reading")
    return bytes(payload), after


def _load_json_at(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int = JSON_MAX_BYTES,
    allowed_nlinks: frozenset[int] = frozenset({1}),
) -> tuple[dict[str, object], str, os.stat_result]:
    descriptor = _open_private_regular_at(
        directory_fd,
        name,
        allowed_nlinks=allowed_nlinks,
    )
    try:
        payload, metadata = _read_descriptor(
            descriptor,
            maximum_bytes=maximum_bytes,
            label=name,
        )
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _stable_identity(observed) != _stable_identity(metadata):
            raise SuccessorError(f"private JSON path changed: {name}")
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SuccessorError(f"private JSON is invalid: {name}") from exc
        if not isinstance(document, dict):
            raise SuccessorError(f"private JSON is not an object: {name}")
        return document, _digest_bytes(payload), metadata
    finally:
        os.close(descriptor)


def _load_json_path(
    path: Path,
    *,
    maximum_bytes: int = JSON_MAX_BYTES,
) -> tuple[dict[str, object], str]:
    parent = _open_private_directory(path.parent)
    try:
        document, digest, _metadata = _load_json_at(
            parent,
            path.name,
            maximum_bytes=maximum_bytes,
        )
        return document, digest
    finally:
        os.close(parent)


def _load_canonical_json_at(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int = JSON_MAX_BYTES,
) -> tuple[dict[str, object], str, os.stat_result]:
    descriptor, document, digest, _payload, metadata = (
        _open_canonical_json_at(
            directory_fd,
            name,
            maximum_bytes=maximum_bytes,
        )
    )
    os.close(descriptor)
    return document, digest, metadata


def _open_canonical_json_at(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int = JSON_MAX_BYTES,
) -> tuple[int, dict[str, object], str, bytes, os.stat_result]:
    """Open and validate canonical JSON while retaining its trusted inode."""

    descriptor = _open_private_regular_at(directory_fd, name)
    try:
        payload, metadata = _read_descriptor(
            descriptor,
            maximum_bytes=maximum_bytes,
            label=name,
        )
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SuccessorError(f"private JSON is invalid: {name}") from exc
        if (
            not isinstance(document, dict)
            or payload != _canonical_bytes(document) + b"\n"
        ):
            raise SuccessorError(f"private JSON is not canonical: {name}")
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _stable_identity(observed) != _stable_identity(metadata):
            raise SuccessorError(f"private JSON path changed: {name}")
        return descriptor, document, _digest_bytes(payload), payload, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _revalidate_open_path_at(
    directory_fd: int,
    name: str,
    descriptor: int,
    expected_payload: bytes,
    expected_metadata: os.stat_result,
    *,
    label: str,
    allowed_nlinks: frozenset[int] = frozenset({1}),
    maximum_bytes: int = JSON_MAX_BYTES,
) -> None:
    """Re-prove an open inode and its path immediately before mutation."""

    held_payload, held_metadata = _read_descriptor(
        descriptor,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    if (
        held_payload != expected_payload
        or _stable_identity(held_metadata) != _stable_identity(expected_metadata)
    ):
        raise SuccessorError(f"{label} open inode changed before mutation")
    path_descriptor = _open_private_regular_at(
        directory_fd,
        name,
        allowed_nlinks=allowed_nlinks,
    )
    try:
        path_payload, path_metadata = _read_descriptor(
            path_descriptor,
            maximum_bytes=maximum_bytes,
            label=label,
        )
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            path_payload != expected_payload
            or _stable_identity(path_metadata) != _stable_identity(held_metadata)
            or _stable_identity(observed) != _stable_identity(held_metadata)
        ):
            raise SuccessorError(f"{label} path CAS changed before mutation")
    finally:
        os.close(path_descriptor)


def _revalidate_moved_open_path_at(
    directory_fd: int,
    name: str,
    descriptor: int,
    expected_payload: bytes,
    expected_metadata: os.stat_result,
    *,
    label: str,
    allowed_nlinks: frozenset[int] = frozenset({1}),
    maximum_bytes: int = JSON_MAX_BYTES,
) -> None:
    """Re-prove an inode at its post-rename name without timestamp assumptions."""

    held_payload, held_metadata = _read_descriptor(
        descriptor,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    if (
        held_payload != expected_payload
        or _rename_stable_inode_identity(held_metadata)
        != _rename_stable_inode_identity(expected_metadata)
    ):
        raise SuccessorError(f"{label} open inode changed across rename")
    path_descriptor = _open_private_regular_at(
        directory_fd,
        name,
        allowed_nlinks=allowed_nlinks,
    )
    try:
        path_payload, path_metadata = _read_descriptor(
            path_descriptor,
            maximum_bytes=maximum_bytes,
            label=label,
        )
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        expected_identity = _rename_stable_inode_identity(held_metadata)
        if (
            path_payload != expected_payload
            or _rename_stable_inode_identity(path_metadata) != expected_identity
            or _rename_stable_inode_identity(observed) != expected_identity
        ):
            raise SuccessorError(f"{label} path CAS changed across rename")
    finally:
        os.close(path_descriptor)


def _load_canonical_json_path(
    path: Path,
    *,
    maximum_bytes: int = JSON_MAX_BYTES,
) -> tuple[dict[str, object], str]:
    parent = _open_private_directory(path.parent)
    try:
        document, digest, _metadata = _load_canonical_json_at(
            parent,
            path.name,
            maximum_bytes=maximum_bytes,
        )
        return document, digest
    finally:
        os.close(parent)


def _rename_noreplace(
    source_directory_fd: int,
    source: str,
    target_directory_fd: int,
    target: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SuccessorError("renameat2 no-replace is unavailable")
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
            source_directory_fd,
            os.fsencode(source),
            target_directory_fd,
            os.fsencode(target),
            RENAME_NOREPLACE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source)


def _rename_exchange(
    directory_fd: int,
    first: str,
    second: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SuccessorError("renameat2 exchange is unavailable")
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
            os.fsencode(first),
            directory_fd,
            os.fsencode(second),
            RENAME_EXCHANGE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), first)


def _link_anonymous_noreplace(
    source_fd: int,
    target_directory_fd: int,
    target: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = getattr(libc, "linkat", None)
    if linkat is None:
        raise SuccessorError("linkat empty-path publication is unavailable")
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    result = linkat(
        source_fd,
        b"",
        target_directory_fd,
        os.fsencode(target),
        AT_EMPTY_PATH,
    )
    if result != 0:
        first_error = ctypes.get_errno()
        if first_error in {errno.ENOENT, errno.EPERM}:
            result = linkat(
                AT_FDCWD,
                os.fsencode(f"/proc/self/fd/{source_fd}"),
                target_directory_fd,
                os.fsencode(target),
                AT_SYMLINK_FOLLOW,
            )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _seal_anonymous_payload_at(
    directory_fd: int,
    payload: bytes,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[int, os.stat_result]:
    """Create a complete durable inode without exposing a partial pathname."""

    temporary_flag = getattr(os, "O_TMPFILE", 0)
    if temporary_flag == 0:
        raise SuccessorError(f"anonymous {label} is unavailable")
    try:
        descriptor = os.open(
            ".",
            os.O_RDWR | os.O_CLOEXEC | temporary_flag,
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise SuccessorError(f"anonymous {label} is unavailable") from exc
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        sealed_payload, metadata = _read_descriptor(
            descriptor,
            maximum_bytes=maximum_bytes,
            label=f"anonymous {label}",
        )
        if (
            sealed_payload != payload
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 0
        ):
            raise SuccessorError(f"anonymous {label} differs")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _revalidate_anonymous_payload(
    descriptor: int,
    expected_payload: bytes,
    expected_metadata: os.stat_result,
    *,
    maximum_bytes: int,
    label: str,
    allowed_nlinks: frozenset[int] = frozenset({0}),
) -> os.stat_result:
    payload, metadata = _read_descriptor(
        descriptor,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    if (
        payload != expected_payload
        or metadata.st_dev != expected_metadata.st_dev
        or metadata.st_ino != expected_metadata.st_ino
        or metadata.st_mode != expected_metadata.st_mode
        or metadata.st_uid != expected_metadata.st_uid
        or metadata.st_gid != expected_metadata.st_gid
        or metadata.st_size != expected_metadata.st_size
        or metadata.st_nlink not in allowed_nlinks
    ):
        raise SuccessorError(f"{label} open inode changed")
    return metadata


def _exchange_evidence_name(temporary: str) -> str:
    return f"{temporary}.exchange-evidence"


def _exchange_evidence_quarantine(temporary: str) -> str:
    return f"{_exchange_evidence_name(temporary)}.quarantine"


def _exchange_identity(
    payload: bytes,
    metadata: os.stat_result,
) -> dict[str, object]:
    return {
        "raw_sha256": _digest_bytes(payload),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "size": metadata.st_size,
    }


def _exchange_identity_matches(
    identity: Mapping[str, object],
    payload: bytes,
    metadata: os.stat_result,
) -> bool:
    return identity == _exchange_identity(payload, metadata)


def _exchange_staging_payload_matches(
    identity: Mapping[str, object],
    payload: bytes,
    metadata: os.stat_result,
) -> bool:
    """Match the exact staging inode sealed by durable evidence."""

    return _exchange_identity_matches(identity, payload, metadata)


def _build_exchange_evidence(
    directory_fd: int,
    operation_id: str,
    name: str,
    temporary: str,
    current_payload: bytes,
    current_metadata: os.stat_result,
    staging_payload: bytes,
    staging_metadata: os.stat_result,
    staging_document: Mapping[str, object],
) -> dict[str, object]:
    if current_payload == staging_payload:
        raise SuccessorError("journal exchange generations are identical")
    directory_metadata = os.fstat(directory_fd)
    return {
        "schema_version": 1,
        "policy": EXCHANGE_EVIDENCE_POLICY,
        "operation_id": operation_id,
        "journal_name": name,
        "temporary_name": temporary,
        "directory_device": directory_metadata.st_dev,
        "directory_inode": directory_metadata.st_ino,
        "current": _exchange_identity(current_payload, current_metadata),
        "staging": _exchange_identity(staging_payload, staging_metadata),
        "staging_document": dict(staging_document),
    }


def _validate_exchange_evidence(
    value: object,
    *,
    directory_fd: int,
    operation_id: str,
    name: str,
    temporary: str,
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != EXCHANGE_EVIDENCE_FIELDS
        or not _has_exact_schema(value, 1)
        or value.get("policy") != EXCHANGE_EVIDENCE_POLICY
        or value.get("operation_id") != operation_id
        or value.get("journal_name") != name
        or value.get("temporary_name") != temporary
    ):
        raise SuccessorError("journal exchange evidence is invalid")
    directory_metadata = os.fstat(directory_fd)
    if (
        value.get("directory_device") != directory_metadata.st_dev
        or value.get("directory_inode") != directory_metadata.st_ino
    ):
        raise SuccessorError("journal exchange evidence directory differs")
    current = value.get("current")
    staging = value.get("staging")
    if (
        not isinstance(current, dict)
        or set(current) != EXCHANGE_IDENTITY_FIELDS
        or not isinstance(staging, dict)
        or set(staging) != EXCHANGE_IDENTITY_FIELDS
    ):
        raise SuccessorError("journal exchange evidence identity is invalid")
    for label, identity in (("current", current), ("staging", staging)):
        _require_digest(identity.get("raw_sha256"), f"exchange {label} raw digest")
        for field in ("device", "inode", "mode", "uid", "gid", "size"):
            observed = identity.get(field)
            if type(observed) is not int or observed < 0:
                raise SuccessorError(
                    "journal exchange evidence identity is invalid"
                )
    staging_document = value.get("staging_document")
    if not isinstance(staging, dict) or not isinstance(staging_document, dict):
        raise SuccessorError("journal exchange evidence staging is invalid")
    try:
        staging_payload = _canonical_bytes(staging_document) + b"\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise SuccessorError(
            "journal exchange evidence staging is invalid"
        ) from exc
    if (
        staging.get("raw_sha256") != _digest_bytes(staging_payload)
        or staging.get("size") != len(staging_payload)
        or len(staging_payload) > JSON_MAX_BYTES
        or current.get("raw_sha256") == staging.get("raw_sha256")
    ):
        raise SuccessorError("journal exchange evidence staging differs")
    return value


def _publish_exchange_evidence_at(
    directory_fd: int,
    operation_id: str,
    name: str,
    temporary: str,
    document: Mapping[str, object],
    *,
    current_fd: int,
    current_payload: bytes,
    current_metadata: os.stat_result,
    staging_fd: int,
    staging_payload: bytes,
    staging_metadata: os.stat_result,
    checkpoint: Callable[[str], None],
) -> tuple[bytes, os.stat_result]:
    evidence_name = _exchange_evidence_name(temporary)
    evidence_quarantine = _exchange_evidence_quarantine(temporary)
    if _entry_exists_at(directory_fd, evidence_quarantine):
        raise SuccessorError("journal exchange evidence is quarantined")
    if _entry_exists_at(directory_fd, evidence_name):
        raise SuccessorError("journal exchange evidence requires recovery")
    if _entry_exists_at(directory_fd, temporary):
        raise SuccessorError(
            "named journal staging lacks prior exchange evidence"
        )
    payload = _canonical_bytes(document) + b"\n"
    anonymous_fd, sealed_metadata = _seal_anonymous_payload_at(
        directory_fd,
        payload,
        maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
        label="journal exchange evidence",
    )
    try:
        checkpoint("source-successor-journal-exchange-evidence-sealed")
        _revalidate_open_path_at(
            directory_fd,
            name,
            current_fd,
            current_payload,
            current_metadata,
            label="journal current before evidence publication CAS",
        )
        _revalidate_anonymous_payload(
            staging_fd,
            staging_payload,
            staging_metadata,
            maximum_bytes=JSON_MAX_BYTES,
            label="journal staging before evidence publication CAS",
        )
        _revalidate_anonymous_payload(
            anonymous_fd,
            payload,
            sealed_metadata,
            maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
            label="anonymous journal exchange evidence publication CAS",
        )
        try:
            _link_anonymous_noreplace(
                anonymous_fd,
                directory_fd,
                evidence_name,
            )
        except OSError as exc:
            raise SuccessorError(
                "journal exchange evidence publication target exists "
                "or link failed"
            ) from exc
        os.fsync(directory_fd)
        checkpoint("source-successor-journal-exchange-evidence-published")
        linked_metadata = os.fstat(anonymous_fd)
        _revalidate_moved_open_path_at(
            directory_fd,
            evidence_name,
            anonymous_fd,
            payload,
            linked_metadata,
            label="journal exchange evidence linked inode CAS",
            maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
        )
    finally:
        os.close(anonymous_fd)
    evidence_fd, observed, _digest, observed_payload, metadata = (
        _open_canonical_json_at(
            directory_fd,
            evidence_name,
            maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
        )
    )
    try:
        if observed != document or observed_payload != payload:
            raise SuccessorError("journal exchange evidence differs")
        _revalidate_open_path_at(
            directory_fd,
            evidence_name,
            evidence_fd,
            observed_payload,
            metadata,
            label="journal exchange evidence publication CAS",
            maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
        )
        _revalidate_open_path_at(
            directory_fd,
            name,
            current_fd,
            current_payload,
            current_metadata,
            label="journal current after evidence publication CAS",
        )
        _revalidate_anonymous_payload(
            staging_fd,
            staging_payload,
            staging_metadata,
            maximum_bytes=JSON_MAX_BYTES,
            label="journal staging after evidence publication CAS",
        )
        return observed_payload, metadata
    finally:
        os.close(evidence_fd)


def _quarantine_unlink_at(
    directory_fd: int,
    name: str,
    quarantine_name: str,
    *,
    allowed_nlinks: frozenset[int] = frozenset({1}),
    expected_payload: bytes | None = None,
    after_quarantine: Callable[[], None] | None = None,
    maximum_bytes: int = JSON_MAX_BYTES,
) -> None:
    source_exists = _entry_exists_at(directory_fd, name)
    quarantine_exists = _entry_exists_at(directory_fd, quarantine_name)
    if source_exists and quarantine_exists:
        raise SuccessorError("source and quarantine both exist")
    if not source_exists and not quarantine_exists:
        os.fsync(directory_fd)
        return
    current = quarantine_name if quarantine_exists else name
    descriptor = _open_private_regular_at(
        directory_fd,
        current,
        allowed_nlinks=allowed_nlinks,
    )
    try:
        payload, opened = _read_descriptor(
            descriptor,
            maximum_bytes=maximum_bytes,
            label=current,
        )
        if expected_payload is not None and payload != expected_payload:
            raise SuccessorError("operation-owned publication residue differs")
        if source_exists:
            try:
                _rename_noreplace(
                    directory_fd,
                    name,
                    directory_fd,
                    quarantine_name,
                )
            except OSError as exc:
                raise SuccessorError("publication residue raced") from exc
            observed = os.stat(
                quarantine_name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if _rename_stable_inode_identity(
                observed
            ) != _rename_stable_inode_identity(opened):
                raise SuccessorError("publication residue identity changed")
            os.fsync(directory_fd)
            if after_quarantine is not None:
                after_quarantine()
        current_metadata = os.fstat(descriptor)
        _revalidate_open_path_at(
            directory_fd,
            quarantine_name,
            descriptor,
            payload,
            current_metadata,
            label="publication residue unlink CAS",
            allowed_nlinks=allowed_nlinks,
            maximum_bytes=maximum_bytes,
        )
        os.unlink(quarantine_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)


def _rollback_exchanged_paths_at(
    directory_fd: int,
    name: str,
    temporary: str,
) -> None:
    final_fd = _open_private_regular_at(directory_fd, name)
    temporary_fd = _open_private_regular_at(directory_fd, temporary)
    try:
        final_payload, final_metadata = _read_descriptor(
            final_fd,
            maximum_bytes=JSON_MAX_BYTES,
            label="journal unexpected exchanged final",
        )
        temporary_payload, temporary_metadata = _read_descriptor(
            temporary_fd,
            maximum_bytes=JSON_MAX_BYTES,
            label="journal unexpected exchanged temporary",
        )
        _rename_exchange(directory_fd, temporary, name)
        os.fsync(directory_fd)
        _revalidate_moved_open_path_at(
            directory_fd,
            name,
            temporary_fd,
            temporary_payload,
            temporary_metadata,
            label="journal rollback restored final CAS",
        )
        _revalidate_moved_open_path_at(
            directory_fd,
            temporary,
            final_fd,
            final_payload,
            final_metadata,
            label="journal rollback restored temporary CAS",
        )
    finally:
        os.close(temporary_fd)
        os.close(final_fd)


def _restore_evidenced_current_at(
    directory_fd: int,
    name: str,
    temporary: str,
    *,
    current_fd: int,
    current_payload: bytes,
    current_metadata: os.stat_result,
    unexpected_final_fd: int,
    unexpected_final_payload: bytes,
    unexpected_final_metadata: os.stat_result,
) -> None:
    """Restore only the exact current inode named by durable evidence."""

    _revalidate_open_path_at(
        directory_fd,
        name,
        unexpected_final_fd,
        unexpected_final_payload,
        unexpected_final_metadata,
        label="unexpected journal final before evidenced restore CAS",
    )
    _revalidate_open_path_at(
        directory_fd,
        temporary,
        current_fd,
        current_payload,
        current_metadata,
        label="journal current before evidenced restore CAS",
    )
    _rename_exchange(directory_fd, temporary, name)
    os.fsync(directory_fd)
    try:
        _revalidate_moved_open_path_at(
            directory_fd,
            name,
            current_fd,
            current_payload,
            current_metadata,
            label="journal current after evidenced restore CAS",
        )
        _revalidate_moved_open_path_at(
            directory_fd,
            temporary,
            unexpected_final_fd,
            unexpected_final_payload,
            unexpected_final_metadata,
            label="unexpected journal final after evidenced restore CAS",
        )
    except SuccessorError as mismatch:
        try:
            _rollback_exchanged_paths_at(directory_fd, name, temporary)
        except (OSError, SuccessorError) as rollback_error:
            raise SuccessorError(
                "evidenced journal restore raced and rollback failed"
            ) from rollback_error
        raise SuccessorError(
            "evidenced journal restore raced; observed paths restored"
        ) from mismatch


def _remove_exchange_evidence_at(
    directory_fd: int,
    temporary: str,
    evidence_payload: bytes,
    checkpoint: Callable[[str], None],
) -> None:
    evidence_name = _exchange_evidence_name(temporary)
    evidence_quarantine = _exchange_evidence_quarantine(temporary)
    _quarantine_unlink_at(
        directory_fd,
        evidence_name,
        evidence_quarantine,
        expected_payload=evidence_payload,
        after_quarantine=lambda: checkpoint(
            "source-successor-journal-exchange-evidence-quarantined"
        ),
        maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
    )
    checkpoint("source-successor-journal-exchange-evidence-removed")


def _complete_evidenced_exchange_at(
    directory_fd: int,
    name: str,
    temporary: str,
    *,
    current_fd: int,
    current_payload: bytes,
    current_metadata: os.stat_result,
    staging_fd: int,
    staging_payload: bytes,
    staging_metadata: os.stat_result,
    evidence_fd: int,
    evidence_payload: bytes,
    evidence_metadata: os.stat_result,
    checkpoint: Callable[[str], None],
    after_exchange: Callable[[], None] | None = None,
    after_retired_quarantine: Callable[[], None] | None = None,
) -> None:
    evidence_name = _exchange_evidence_name(temporary)
    evidence_quarantine = _exchange_evidence_quarantine(temporary)
    retired_quarantine = f"{temporary}.quarantine"
    if _entry_exists_at(
        directory_fd, evidence_quarantine
    ) or _entry_exists_at(directory_fd, retired_quarantine):
        raise SuccessorError("journal exchange quarantine namespace changed")
    _revalidate_open_path_at(
        directory_fd,
        evidence_name,
        evidence_fd,
        evidence_payload,
        evidence_metadata,
        label="journal exchange evidence immediately before syscall CAS",
        maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
    )
    _revalidate_open_path_at(
        directory_fd,
        name,
        current_fd,
        current_payload,
        current_metadata,
        label="journal current immediately before exchange syscall CAS",
    )
    _revalidate_open_path_at(
        directory_fd,
        temporary,
        staging_fd,
        staging_payload,
        staging_metadata,
        label="journal staging immediately before exchange syscall CAS",
    )
    _rename_exchange(directory_fd, temporary, name)
    os.fsync(directory_fd)
    if after_exchange is not None:
        after_exchange()
    try:
        if _entry_exists_at(
            directory_fd, evidence_quarantine
        ) or _entry_exists_at(directory_fd, retired_quarantine):
            raise SuccessorError(
                "journal exchange quarantine namespace changed"
            )
        _revalidate_open_path_at(
            directory_fd,
            evidence_name,
            evidence_fd,
            evidence_payload,
            evidence_metadata,
            label="journal exchange evidence after syscall CAS",
            maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
        )
        _revalidate_moved_open_path_at(
            directory_fd,
            name,
            staging_fd,
            staging_payload,
            staging_metadata,
            label="journal published generation evidence CAS",
        )
        _revalidate_moved_open_path_at(
            directory_fd,
            temporary,
            current_fd,
            current_payload,
            current_metadata,
            label="journal displaced generation evidence CAS",
        )
    except SuccessorError as mismatch:
        try:
            _rollback_exchanged_paths_at(directory_fd, name, temporary)
        except (OSError, SuccessorError) as rollback_error:
            raise SuccessorError(
                "journal exchange CAS changed and rollback failed"
            ) from rollback_error
        raise SuccessorError(
            "journal exchange CAS changed; evidence retained after rollback"
        ) from mismatch
    _quarantine_unlink_at(
        directory_fd,
        temporary,
        retired_quarantine,
        expected_payload=current_payload,
        after_quarantine=after_retired_quarantine,
    )
    checkpoint("source-successor-journal-retired-generation-removed")
    _remove_exchange_evidence_at(
        directory_fd,
        temporary,
        evidence_payload,
        checkpoint,
    )


def _exchange_replace_at(
    directory_fd: int,
    name: str,
    temporary: str,
    *,
    current_fd: int,
    current_payload: bytes,
    current_metadata: os.stat_result,
    staging_fd: int,
    staging_payload: bytes,
    staging_metadata: os.stat_result,
    staging_document: Mapping[str, object],
    checkpoint: Callable[[str], None],
    before_exchange_cas: Callable[[], None] | None = None,
    after_exchange: Callable[[], None] | None = None,
    after_retired_quarantine: Callable[[], None] | None = None,
) -> None:
    """Exchange journal generations under durable inode/digest evidence."""

    operation_id = _require_operation_id(name.removesuffix(".json"))
    evidence = _build_exchange_evidence(
        directory_fd,
        operation_id,
        name,
        temporary,
        current_payload,
        current_metadata,
        staging_payload,
        staging_metadata,
        staging_document,
    )
    evidence_payload, published_evidence_metadata = (
        _publish_exchange_evidence_at(
            directory_fd,
            operation_id,
            name,
            temporary,
            evidence,
            current_fd=current_fd,
            current_payload=current_payload,
            current_metadata=current_metadata,
            staging_fd=staging_fd,
            staging_payload=staging_payload,
            staging_metadata=staging_metadata,
            checkpoint=checkpoint,
        )
    )
    try:
        _link_anonymous_noreplace(staging_fd, directory_fd, temporary)
    except OSError as exc:
        raise SuccessorError(
            "journal staging publication target exists or link failed"
        ) from exc
    os.fsync(directory_fd)
    checkpoint("source-successor-journal-staging-published")
    linked_staging_metadata = os.fstat(staging_fd)
    _revalidate_moved_open_path_at(
        directory_fd,
        temporary,
        staging_fd,
        staging_payload,
        linked_staging_metadata,
        label="journal staging linked inode CAS",
    )
    (
        evidence_fd,
        observed_evidence,
        _evidence_digest,
        observed_evidence_payload,
        evidence_metadata,
    ) = _open_canonical_json_at(
        directory_fd,
        _exchange_evidence_name(temporary),
        maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
    )
    try:
        if (
            observed_evidence != evidence
            or observed_evidence_payload != evidence_payload
            or _stable_identity(evidence_metadata)
            != _stable_identity(published_evidence_metadata)
        ):
            raise SuccessorError(
                "journal exchange evidence published inode differs"
            )
        if before_exchange_cas is not None:
            before_exchange_cas()
        _complete_evidenced_exchange_at(
            directory_fd,
            name,
            temporary,
            current_fd=current_fd,
            current_payload=current_payload,
            current_metadata=current_metadata,
            staging_fd=staging_fd,
            staging_payload=staging_payload,
            staging_metadata=linked_staging_metadata,
            evidence_fd=evidence_fd,
            evidence_payload=evidence_payload,
            evidence_metadata=evidence_metadata,
            checkpoint=checkpoint,
            after_exchange=after_exchange,
            after_retired_quarantine=after_retired_quarantine,
        )
    finally:
        os.close(evidence_fd)


def _quarantine_for_unlink_cas_at(
    directory_fd: int,
    name: str,
    quarantine: str,
    *,
    current_fd: int,
    current_payload: bytes,
    current_metadata: os.stat_result,
    after_quarantine: Callable[[], None] | None = None,
) -> None:
    """Move a validated file aside without deleting a raced replacement."""

    _revalidate_open_path_at(
        directory_fd,
        name,
        current_fd,
        current_payload,
        current_metadata,
        label="journal unlink pre-quarantine CAS",
    )
    try:
        _rename_noreplace(
            directory_fd,
            name,
            directory_fd,
            quarantine,
        )
    except OSError as exc:
        raise SuccessorError("journal unlink quarantine raced") from exc
    os.fsync(directory_fd)
    if after_quarantine is not None:
        after_quarantine()
    try:
        _revalidate_moved_open_path_at(
            directory_fd,
            quarantine,
            current_fd,
            current_payload,
            current_metadata,
            label="journal unlink quarantined generation CAS",
        )
    except SuccessorError as mismatch:
        displaced_fd: int | None = None
        try:
            displaced_fd = _open_private_regular_at(directory_fd, quarantine)
            displaced_payload, displaced_metadata = _read_descriptor(
                displaced_fd,
                maximum_bytes=JSON_MAX_BYTES,
                label="journal unexpected unlink generation",
            )
            _rename_noreplace(
                directory_fd,
                quarantine,
                directory_fd,
                name,
            )
            os.fsync(directory_fd)
            _revalidate_moved_open_path_at(
                directory_fd,
                name,
                displaced_fd,
                displaced_payload,
                displaced_metadata,
                label="journal unlink rollback restored generation CAS",
            )
        except (OSError, SuccessorError) as rollback_error:
            raise SuccessorError(
                "journal unlink CAS changed and rollback failed"
            ) from rollback_error
        finally:
            if displaced_fd is not None:
                os.close(displaced_fd)
        raise SuccessorError(
            "journal unlink CAS changed; displaced generation restored"
        ) from mismatch


def _atomic_json_at(
    directory_fd: int,
    name: str,
    document: object,
    *,
    expected_current: tuple[int, bytes, os.stat_result] | None = None,
    checkpoint: Callable[[str], None] = lambda _label: None,
    before_replace_cas: Callable[[], None] | None = None,
    after_exchange: Callable[[], None] | None = None,
    after_retired_quarantine: Callable[[], None] | None = None,
) -> None:
    temporary = f".{name}.tmp"
    quarantine = f"{temporary}.quarantine"
    evidence_name = _exchange_evidence_name(temporary)
    evidence_quarantine = _exchange_evidence_quarantine(temporary)
    payload = _canonical_bytes(document) + b"\n"
    if _entry_exists_at(directory_fd, quarantine):
        raise SuccessorError("journal quarantine is occupied")
    if _entry_exists_at(directory_fd, evidence_quarantine):
        raise SuccessorError("journal exchange evidence is quarantined")
    if _entry_exists_at(directory_fd, evidence_name):
        raise SuccessorError("journal exchange evidence requires recovery")
    if _entry_exists_at(directory_fd, temporary):
        raise SuccessorError(
            "named journal staging lacks durable exchange evidence"
        )
    staging_fd, staging_metadata = _seal_anonymous_payload_at(
        directory_fd,
        payload,
        maximum_bytes=JSON_MAX_BYTES,
        label="journal staging",
    )
    try:
        checkpoint("source-successor-journal-staging-sealed")
        if expected_current is None:
            try:
                _link_anonymous_noreplace(staging_fd, directory_fd, name)
            except OSError as exc:
                raise SuccessorError(
                    "journal target appeared before publication"
                ) from exc
            os.fsync(directory_fd)
            linked_metadata = os.fstat(staging_fd)
            _revalidate_moved_open_path_at(
                directory_fd,
                name,
                staging_fd,
                payload,
                linked_metadata,
                label="journal initial linked inode CAS",
            )
        else:
            current_fd, current_payload, current_metadata = expected_current
            _exchange_replace_at(
                directory_fd,
                name,
                temporary,
                current_fd=current_fd,
                current_payload=current_payload,
                current_metadata=current_metadata,
                staging_fd=staging_fd,
                staging_payload=payload,
                staging_metadata=staging_metadata,
                staging_document=document
                if isinstance(document, Mapping)
                else {},
                checkpoint=checkpoint,
                before_exchange_cas=before_replace_cas,
                after_exchange=after_exchange,
                after_retired_quarantine=after_retired_quarantine,
            )
    finally:
        os.close(staging_fd)
    os.fsync(directory_fd)


def _strict_directory_realpath(path: Path, *, label: str) -> tuple[Path, Path]:
    lexical = path.absolute()
    try:
        canonical = lexical.resolve(strict=True)
        metadata = canonical.stat()
    except (OSError, RuntimeError) as exc:
        raise SuccessorError(f"{label} canonical path is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise SuccessorError(f"{label} canonical path is not a directory")
    return lexical, canonical


def _assert_private_directory_chain(root: Path, target: Path) -> None:
    """Require an owner-private, symlink-free chain below a private anchor."""

    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise SuccessorError("test trust seam escaped its private root") from exc
    current = root
    for component in (Path("."), *relative.parts):
        if component != Path("."):
            current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise SuccessorError("test trust seam path is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise SuccessorError(
                "test trust seams require owner-private symlink-free paths"
            )


PREDECESSOR_AUTHORITY_FIELDS = {
    "schema_version",
    "status",
    "authority_kind",
    "operation_id",
    "source_sha",
    "source_tree",
    "production_source_sha",
    "production_source_tree",
    "adopted_deployment_sha256",
    "bootstrap_control_sha256",
    "adopted_prerequisites_sha256",
    "plan_sha256",
    "permission_impact_sha256",
    "permission_marker_sha256",
    "permission_evidence_sha256",
    "permission_inventory_sha256",
    "original_permissions_sha256",
    "hardened_permissions_sha256",
    "plan",
    "completed_at",
}
PREDECESSOR_AUTHORITY_KIND = "manual-runtime-adoption-permission-hardening"
ADOPTION_AUTHORITY_KIND = "manual-runtime-adoption"
PREREQUISITE_AUTHORITY_KIND = "manual-runtime-adoption-prerequisites"


def _require_exact_keys(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SuccessorError(f"{label} has an invalid shape")
    return value


def _validate_delivery_gate(
    value: object,
    *,
    source_sha: str,
    required_jobs: list[str] | None = None,
) -> dict[str, object]:
    delivery = _require_exact_keys(
        value, {"remote_main", "ci"}, "delivery gate"
    )
    ci = _require_exact_keys(
        delivery.get("ci"),
        {
            "workflow_run_id",
            "run_attempt",
            "head_sha",
            "head_branch",
            "event",
            "path",
            "conclusion",
            "required_jobs",
        },
        "delivery CI evidence",
    )
    jobs = ci.get("required_jobs")
    if (
        delivery.get("remote_main") != source_sha
        or ci.get("head_sha") != source_sha
        or ci.get("head_branch") != "main"
        or ci.get("event") != "push"
        or ci.get("path") != ".github/workflows/ci.yml"
        or ci.get("conclusion") != "success"
        or not isinstance(ci.get("workflow_run_id"), int)
        or isinstance(ci.get("workflow_run_id"), bool)
        or ci["workflow_run_id"] <= 0
        or not isinstance(ci.get("run_attempt"), int)
        or isinstance(ci.get("run_attempt"), bool)
        or ci["run_attempt"] <= 0
        or not isinstance(jobs, list)
        or not jobs
        or jobs != sorted(set(jobs))
        or len(jobs) > 32
        or any(not isinstance(job, str) or not job for job in jobs)
        or required_jobs is not None
        and jobs != required_jobs
    ):
        raise SuccessorError("delivery gate differs from protected main/CI")
    return {
        "remote_main": source_sha,
        "ci": {
            "workflow_run_id": ci["workflow_run_id"],
            "run_attempt": ci["run_attempt"],
            "head_sha": source_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": list(jobs),
        },
    }


def _safe_git_environment(root: Path) -> dict[str, str]:
    git_dir = root / ".git"
    return {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(root),
        "GIT_INDEX_FILE": str(git_dir / "index"),
        "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _assert_safe_git_root(root: Path) -> None:
    if not root.is_absolute():
        raise SuccessorError("source root must be absolute")
    try:
        root_metadata = root.lstat()
        git_metadata = (root / ".git").lstat()
        config_metadata = (root / ".git/config").lstat()
        config_payload = (root / ".git/config").read_bytes()
    except OSError as exc:
        raise SuccessorError("source Git trust surface is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or root_metadata.st_mode & 0o022
        or not stat.S_ISDIR(git_metadata.st_mode)
        or (root / ".git").is_symlink()
        or git_metadata.st_uid != os.geteuid()
        or git_metadata.st_mode & 0o022
        or not stat.S_ISREG(config_metadata.st_mode)
        or (root / ".git/config").is_symlink()
        or config_metadata.st_uid != os.geteuid()
        or config_metadata.st_mode & 0o022
    ):
        raise SuccessorError("source Git trust surface is not owner-private")
    for relative in (
        "commondir",
        "info/grafts",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "shallow",
    ):
        candidate = root / ".git" / relative
        if candidate.exists() or candidate.is_symlink():
            raise SuccessorError(f"forbidden Git marker exists: {relative}")
    for directory, directories, files in os.walk(
        root / ".git", followlinks=False
    ):
        for name in (*directories, *files):
            if name.endswith(".lock"):
                raise SuccessorError("Git lock file exists")
    try:
        text = config_payload.decode("utf-8")
        parser = configparser.RawConfigParser(
            interpolation=None,
            strict=True,
            delimiters=("=",),
        )
        parser.optionxform = str.lower
        parser.read_string(text)
    except (UnicodeError, configparser.Error) as exc:
        raise SuccessorError("local Git config is invalid") from exc
    forbidden = {
        "core.worktree",
        "core.hookspath",
        "core.fsmonitor",
        "core.sparsecheckout",
        "extensions.worktreeconfig",
        "extensions.objectformat",
    }
    for section in parser.sections():
        lowered = section.lower()
        if lowered == "include" or lowered.startswith("includeif "):
            raise SuccessorError("local Git config contains includes")
        for option, raw in parser.items(section, raw=True):
            key = f"{lowered.split(' ', 1)[0]}.{option.lower()}"
            if key in forbidden or (
                lowered.startswith("remote ")
                and option.lower() in {"promisor", "partialclonefilter"}
            ):
                if key == "extensions.objectformat" and raw.lower() == "sha1":
                    continue
                raise SuccessorError("local Git config redirects trust")


def _run_git(
    root: Path,
    *arguments: str,
    text: bool = False,
    timeout: int = 600,
) -> bytes | str:
    command = [
        "/usr/bin/git",
        "--git-dir",
        str(root / ".git"),
        "--work-tree",
        str(root),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.sparseCheckout=false",
        "-c",
        "maintenance.auto=false",
        "-c",
        "gc.auto=0",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=_safe_git_environment(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=text,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SuccessorError("trusted Git command failed") from exc
    return result.stdout


def _git_commit_tree(root: Path, source_sha: str) -> str:
    observed = str(
        _run_git(root, "rev-parse", "--verify", f"{source_sha}^{{tree}}", text=True)
    ).strip()
    return _require_sha(observed, "source tree")


def _git_blob_identity(
    root: Path,
    commit: str,
    relative: str,
) -> tuple[dict[str, str], bytes]:
    raw = bytes(
        _run_git(
            root,
            "ls-tree",
            "-z",
            "--full-tree",
            commit,
            "--",
            relative,
        )
    )
    match = re.fullmatch(
        rb"([0-7]{6}) ([a-z]+) ([0-9a-f]{40})\t([^\x00]+)\x00",
        raw,
    )
    if match is None:
        raise SuccessorError(f"fixed Git entry is missing or ambiguous: {relative}")
    mode = match.group(1).decode()
    object_type = match.group(2).decode()
    blob_sha = match.group(3).decode()
    path = match.group(4).decode("utf-8", "strict")
    if (
        path != relative
        or object_type != "blob"
        or mode not in {"100644", "100755"}
    ):
        raise SuccessorError(f"fixed Git entry has an unsafe type/mode: {relative}")
    payload = bytes(_run_git(root, "cat-file", "blob", blob_sha))
    return (
        {
            "object_type": "blob",
            "mode": mode,
            "blob_sha": blob_sha,
            "sha256": _digest_bytes(payload),
        },
        payload,
    )


def _assert_target_worktree_file(
    root: Path,
    relative: str,
    identity: Mapping[str, str],
    payload: bytes,
) -> None:
    path = root / relative
    try:
        metadata = path.lstat()
        observed = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise SuccessorError(f"target worktree file is unavailable: {relative}") from exc
    expected_executable = identity["mode"] == "100755"
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or _stable_identity(metadata) != _stable_identity(after)
        or observed != payload
        or bool(metadata.st_mode & 0o111) != expected_executable
    ):
        raise SuccessorError(f"target worktree file differs from Git: {relative}")


def _independent_source_readiness(
    root: Path,
    expected_sha: str,
) -> dict[str, object]:
    _assert_safe_git_root(root)
    branch = str(_run_git(root, "symbolic-ref", "--short", "HEAD", text=True)).strip()
    source_sha = str(_run_git(root, "rev-parse", "HEAD", text=True)).strip()
    source_tree = str(_run_git(root, "rev-parse", "HEAD^{tree}", text=True)).strip()
    local_main = str(_run_git(root, "rev-parse", "refs/heads/main", text=True)).strip()
    origin_main = str(
        _run_git(root, "rev-parse", "refs/remotes/origin/main", text=True)
    ).strip()
    remote_names = str(_run_git(root, "remote", text=True)).splitlines()
    fetch_urls = str(
        _run_git(root, "remote", "get-url", "--all", "origin", text=True)
    ).splitlines()
    push_urls = str(
        _run_git(root, "remote", "get-url", "--push", "--all", "origin", text=True)
    ).splitlines()
    dirty = str(
        _run_git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
            text=True,
        )
    )
    ignored = bytes(
        _run_git(
            root,
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        )
    )
    replace_refs = str(
        _run_git(root, "for-each-ref", "--format=%(refname)", "refs/replace/", text=True)
    ).splitlines()
    index_entries = bytes(
        _run_git(root, "ls-files", "--sparse", "-v", "-z")
    ).split(b"\0")
    _run_git(root, "fsck", "--full", "--strict", "--no-reflogs", "--no-dangling")
    try:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "--git-dir",
                str(root / ".git"),
                "--work-tree",
                str(root),
                "fsck",
                "--full",
                "--strict",
                "--no-reflogs",
                "--unreachable",
            ],
            cwd=root,
            env=_safe_git_environment(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SuccessorError("Git unreachable-object proof failed") from exc
    special = [entry for entry in index_entries if entry and not entry.startswith(b"H ")]
    if (
        branch != "main"
        or source_sha != expected_sha
        or local_main != source_sha
        or origin_main != source_sha
        or remote_names != ["origin"]
        or fetch_urls != [REPOSITORY_SSH_URL]
        or push_urls != [REPOSITORY_SSH_URL]
        or dirty
        or ignored
        or replace_refs
        or special
        or (result.stdout + result.stderr).strip()
    ):
        raise SuccessorError("independent source readiness failed")
    return {
        "schema_version": 2,
        "ready": True,
        "source_root": str(root.absolute()),
        "source_sha": source_sha,
        "source_tree": _require_sha(source_tree, "source tree"),
        "branch": "main",
        "origin": REPOSITORY_SSH_URL,
        "remote_names": ["origin"],
        "origin_fetch_urls": [REPOSITORY_SSH_URL],
        "origin_push_urls": [REPOSITORY_SSH_URL],
        "origin_main_sha": source_sha,
        "standalone_object_database": True,
        "shallow": False,
        "dirty_entries": 0,
        "ignored_entries": 0,
        "unreachable_objects": 0,
        "replace_refs": 0,
        "special_index_entries": 0,
        "sparse_index": False,
        "owner_private": True,
        "group_or_world_writable": False,
    }


def _validate_readiness(
    value: object,
    *,
    root: Path,
    source_sha: str,
    source_tree: str,
) -> dict[str, object]:
    readiness = _require_exact_keys(value, SOURCE_READINESS_FIELDS, "source readiness")
    if readiness != {
        **readiness,
        "schema_version": 2,
        "ready": True,
        "source_root": str(root.absolute()),
        "source_sha": source_sha,
        "source_tree": source_tree,
        "branch": "main",
        "origin": REPOSITORY_SSH_URL,
        "remote_names": ["origin"],
        "origin_fetch_urls": [REPOSITORY_SSH_URL],
        "origin_push_urls": [REPOSITORY_SSH_URL],
        "origin_main_sha": source_sha,
        "standalone_object_database": True,
        "shallow": False,
        "dirty_entries": 0,
        "ignored_entries": 0,
        "unreachable_objects": 0,
        "replace_refs": 0,
        "special_index_entries": 0,
        "sparse_index": False,
        "owner_private": True,
        "group_or_world_writable": False,
    }:
        raise SuccessorError("frozen predecessor source readiness differs")
    return dict(readiness)


def _load_frozen_module(
    name: str,
    payload: bytes,
    path: Path,
) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    try:
        exec(compile(payload, f"git-predecessor:{path}", "exec"), module.__dict__)
    except BaseException as exc:
        raise SuccessorError(f"frozen predecessor verifier failed to load: {path}") from exc
    return module


def _publication_plan(
    runtime_root: Path,
    operation_id: str,
) -> dict[str, object]:
    state = runtime_root / "state"
    final = AUTHORITY_RELATIVE_PATH.name
    staging = f".{final}.create-{operation_id}"
    quarantine = f"{staging}.quarantine"
    return {
        "schema_version": 1,
        "policy": PUBLICATION_POLICY,
        "directory": str(state),
        "entries": [
            {
                "role": "final",
                "name": final,
                "path": str(state / final),
                "initially_absent": True,
            },
            {
                "role": "staging",
                "name": staging,
                "path": str(state / staging),
                "initially_absent": True,
            },
            {
                "role": "staging-quarantine",
                "name": quarantine,
                "path": str(state / quarantine),
                "initially_absent": True,
            },
        ],
    }


def _reseal_exact_at(
    directory_fd: int,
    name: str,
    descriptor: int,
    expected_payload: bytes,
) -> os.stat_result:
    payload, before = _read_descriptor(
        descriptor,
        maximum_bytes=JSON_MAX_BYTES,
        label=name,
    )
    if payload != expected_payload:
        raise SuccessorError("published source successor authority differs")
    os.fsync(descriptor)
    sealed = os.fstat(descriptor)
    observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        _stable_identity(before) != _stable_identity(sealed)
        or _stable_identity(observed) != _stable_identity(sealed)
    ):
        raise SuccessorError("published source successor authority changed")
    os.fsync(directory_fd)
    return sealed


def _create_json_once_at(
    directory_fd: int,
    name: str,
    document: object,
    *,
    operation_id: str,
    checkpoint: Callable[[str], None],
    before_link: Callable[[], None] | None = None,
) -> None:
    payload = _canonical_bytes(document) + b"\n"
    if len(payload) > JSON_MAX_BYTES:
        raise SuccessorError("source successor authority is oversized")
    staging = f".{name}.create-{operation_id}"
    quarantine = f"{staging}.quarantine"
    final_exists = _entry_exists_at(directory_fd, name)
    staging_exists = _entry_exists_at(directory_fd, staging)
    quarantine_exists = _entry_exists_at(directory_fd, quarantine)
    if staging_exists and quarantine_exists:
        raise SuccessorError("authority staging and quarantine both exist")

    if final_exists:
        final_fd = _open_private_regular_at(
            directory_fd,
            name,
            allowed_nlinks=frozenset({1, 2}),
        )
        try:
            final_metadata = _reseal_exact_at(
                directory_fd, name, final_fd, payload
            )
            if final_metadata.st_nlink == 2:
                companion = (
                    staging
                    if staging_exists
                    else quarantine if quarantine_exists else None
                )
                if companion is None:
                    raise SuccessorError("authority has an unowned hard link")
                companion_fd = _open_private_regular_at(
                    directory_fd,
                    companion,
                    allowed_nlinks=frozenset({2}),
                )
                try:
                    companion_metadata = _reseal_exact_at(
                        directory_fd, companion, companion_fd, payload
                    )
                    if _stable_identity(companion_metadata) != _stable_identity(
                        final_metadata
                    ):
                        raise SuccessorError("authority hard links differ")
                finally:
                    os.close(companion_fd)
                _quarantine_unlink_at(
                    directory_fd,
                    staging,
                    quarantine,
                    allowed_nlinks=frozenset({2}),
                    expected_payload=payload,
                )
            elif staging_exists or quarantine_exists:
                raise SuccessorError("authority has unowned publication residue")
        finally:
            os.close(final_fd)
    else:
        if staging_exists:
            staging_fd = _open_private_regular_at(directory_fd, staging)
            try:
                observed, _metadata = _read_descriptor(
                    staging_fd,
                    maximum_bytes=JSON_MAX_BYTES,
                    label=staging,
                )
            finally:
                os.close(staging_fd)
            if observed != payload:
                _quarantine_unlink_at(
                    directory_fd,
                    staging,
                    quarantine,
                )
                checkpoint("source-successor-partial-authority-staging-removed")
                staging_exists = False
        elif quarantine_exists:
            _quarantine_unlink_at(directory_fd, staging, quarantine)
            checkpoint("source-successor-authority-quarantine-removed")
            quarantine_exists = False
        if not staging_exists:
            descriptor = os.open(
                staging,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory_fd)
            checkpoint("source-successor-authority-staged")
        staging_fd = _open_private_regular_at(directory_fd, staging)
        try:
            _reseal_exact_at(directory_fd, staging, staging_fd, payload)
            # Staging is recoverable residue; the final hard link is the
            # permanent authority publication boundary. Re-prove all sealed
            # repository content in the last possible window before it.
            if before_link is not None:
                before_link()
            try:
                os.link(
                    staging,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            os.fsync(directory_fd)
            checkpoint("source-successor-authority-linked")
            final_fd = _open_private_regular_at(
                directory_fd,
                name,
                allowed_nlinks=frozenset({2}),
            )
            try:
                final_metadata = _reseal_exact_at(
                    directory_fd, name, final_fd, payload
                )
                staging_metadata = os.fstat(staging_fd)
                if _stable_identity(final_metadata) != _stable_identity(
                    staging_metadata
                ):
                    raise SuccessorError("authority publication inode differs")
            finally:
                os.close(final_fd)
            os.unlink(staging, dir_fd=directory_fd)
            os.fsync(directory_fd)
            checkpoint("source-successor-authority-staging-unlinked")
        finally:
            os.close(staging_fd)

    final_fd = _open_private_regular_at(directory_fd, name)
    try:
        _reseal_exact_at(directory_fd, name, final_fd, payload)
    finally:
        os.close(final_fd)
    os.fsync(directory_fd)


class SourceSuccessorPublisher:
    """Plan and publish the fixed old-root to reviewed-target authority."""

    def __init__(
        self,
        *,
        source_root: Path = REPOSITORY_ROOT,
        production_root: Path = PRODUCTION_ROOT,
        runtime_root: Path = RUNTIME_ROOT,
        checkpoint: Callable[[str], None] | None = None,
        expected_transitions: Mapping[
            str, Mapping[str, str]
        ] = EXPECTED_CHANGED_TRANSITIONS,
        expected_predecessor_provenance: Mapping[
            str, str
        ] = EXPECTED_PREDECESSOR_PROVENANCE,
        delivery_gate_probe: Callable[
            [types.ModuleType, Path, Path, str, list[str], object | None],
            object,
        ]
        | None = None,
    ) -> None:
        self.source_root, canonical_source_root = _strict_directory_realpath(
            source_root,
            label="source root",
        )
        self.production_root, canonical_production_root = (
            _strict_directory_realpath(
                production_root,
                label="production root",
            )
        )
        self.runtime_root, canonical_runtime_root = _strict_directory_realpath(
            runtime_root,
            label="runtime root",
        )
        production_lexical = PRODUCTION_ROOT.absolute()
        runtime_lexical = RUNTIME_ROOT.absolute()
        try:
            canonical_expected_production = production_lexical.resolve(
                strict=False
            )
            canonical_expected_runtime = runtime_lexical.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SuccessorError(
                "fixed production paths cannot be canonicalized"
            ) from exc
        self.checkpoint = checkpoint or (lambda _label: None)
        self.expected_transitions = {
            path: dict(value) for path, value in expected_transitions.items()
        }
        self.expected_predecessor_provenance = dict(
            expected_predecessor_provenance
        )
        expected_provenance_fields = {
            "predecessor_source_sha",
            "predecessor_source_tree",
            "predecessor_authority_sha256",
            "predecessor_marker_sha256",
            "predecessor_journal_sha256",
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "production_source_sha",
            "production_source_tree",
            "predecessor_source_trust_sha256",
            "production_source_trust_sha256",
        }
        if set(self.expected_predecessor_provenance) != expected_provenance_fields:
            raise SuccessorError("fixed predecessor provenance contract is invalid")
        for field in (
            "predecessor_source_sha",
            "predecessor_source_tree",
            "production_source_sha",
            "production_source_tree",
        ):
            _require_sha(self.expected_predecessor_provenance[field], field)
        for field in expected_provenance_fields - {
            "predecessor_source_sha",
            "predecessor_source_tree",
            "production_source_sha",
            "production_source_tree",
        }:
            _require_digest(self.expected_predecessor_provenance[field], field)
        nondefault_seam = (
            self.expected_transitions
            != {
                path: dict(value)
                for path, value in EXPECTED_CHANGED_TRANSITIONS.items()
            }
            or self.expected_predecessor_provenance
            != dict(EXPECTED_PREDECESSOR_PROVENANCE)
            or delivery_gate_probe is not None
            or checkpoint is not None
        )
        production_scope = (
            canonical_runtime_root == canonical_expected_runtime
            or canonical_production_root == canonical_expected_production
        )
        if production_scope and nondefault_seam:
            raise SuccessorError(
                "production source successor forbids injected trust seams"
            )
        if (
            (
                canonical_runtime_root == canonical_expected_runtime
                and self.runtime_root != runtime_lexical
            )
            or (
                canonical_production_root == canonical_expected_production
                and self.production_root != production_lexical
            )
        ):
            raise SuccessorError(
                "production source successor requires fixed canonical paths"
            )
        if nondefault_seam:
            try:
                common = Path(
                    os.path.commonpath(
                        (
                            self.source_root,
                            self.production_root,
                            self.runtime_root,
                        )
                    )
                )
            except (OSError, ValueError) as exc:
                raise SuccessorError("test trust seam root is unavailable") from exc
            if common == Path("/"):
                raise SuccessorError("test trust seams require one private root")
            for lexical, canonical in (
                (self.source_root, canonical_source_root),
                (self.production_root, canonical_production_root),
                (self.runtime_root, canonical_runtime_root),
            ):
                if lexical != canonical:
                    raise SuccessorError(
                        "test trust seams require canonical symlink-free paths"
                    )
                _assert_private_directory_chain(common, lexical)
        self.delivery_gate_probe = delivery_gate_probe

    @property
    def state_root(self) -> Path:
        return self.runtime_root / "state"

    @property
    def authority_path(self) -> Path:
        return self.runtime_root / AUTHORITY_RELATIVE_PATH

    @property
    def transaction_root(self) -> Path:
        return self.runtime_root / TRANSACTION_RELATIVE_DIRECTORY

    def _load_state_json(
        self,
        relative: Path,
        *,
        maximum_bytes: int = JSON_MAX_BYTES,
        state_fd: int | None = None,
    ) -> tuple[dict[str, object], str]:
        path = self.runtime_root / relative
        if relative.parent != Path("state"):
            raise SuccessorError("runtime authority path is outside state")
        if state_fd is None:
            first = _load_canonical_json_path(
                path, maximum_bytes=maximum_bytes
            )
            second = _load_canonical_json_path(
                path, maximum_bytes=maximum_bytes
            )
        else:
            first_document, first_digest, _first_metadata = _load_canonical_json_at(
                state_fd,
                relative.name,
                maximum_bytes=maximum_bytes,
            )
            second_document, second_digest, _second_metadata = _load_canonical_json_at(
                state_fd,
                relative.name,
                maximum_bytes=maximum_bytes,
            )
            first = (first_document, first_digest)
            second = (second_document, second_digest)
        if first != second:
            raise SuccessorError(f"runtime authority changed while reading: {path}")
        return first

    def _assert_state_pinned(self, state_fd: int) -> None:
        held = os.fstat(state_fd)
        try:
            observed = self.state_root.lstat()
        except OSError as exc:
            raise SuccessorError("runtime state directory path changed") from exc
        if (
            not stat.S_ISDIR(observed.st_mode)
            or self.state_root.is_symlink()
            or _directory_identity(held) != _directory_identity(observed)
        ):
            raise SuccessorError("runtime state directory path changed")

    def _adoption_context(
        self, state_fd: int | None = None
    ) -> dict[str, object]:
        adopted, adopted_digest = self._load_state_json(
            ADOPTED_DEPLOYMENT_RELATIVE_PATH,
            state_fd=state_fd,
        )
        bootstrap, bootstrap_digest = self._load_state_json(
            BOOTSTRAP_CONTROL_RELATIVE_PATH,
            state_fd=state_fd,
        )
        prerequisites, prerequisites_digest = self._load_state_json(
            ADOPTED_PREREQUISITES_RELATIVE_PATH,
            maximum_bytes=PREDECESSOR_MAX_BYTES,
            state_fd=state_fd,
        )
        prerequisite_plan = prerequisites.get("plan")
        production_sha = adopted.get("source_sha")
        production_tree = adopted.get("source_tree")
        if (
            not _has_exact_schema(adopted, 1)
            or adopted.get("status") != "adopted"
            or adopted.get("authority_kind") != ADOPTION_AUTHORITY_KIND
            or _require_sha(production_sha, "production source SHA")
            != production_sha
            or _require_sha(production_tree, "production source tree")
            != production_tree
            or not _has_exact_schema(bootstrap, 3)
            or bootstrap.get("status") != "completed"
            or bootstrap.get("authority_kind") != ADOPTION_AUTHORITY_KIND
            or bootstrap.get("adopted_deployment") != adopted
            or bootstrap.get("adopted_deployment_sha256")
            != _canonical_digest(adopted)
            or not _has_exact_schema(prerequisites, 1)
            or prerequisites.get("status") != "completed"
            or prerequisites.get("authority_kind")
            != PREREQUISITE_AUTHORITY_KIND
            or not isinstance(prerequisite_plan, dict)
            or prerequisites.get("plan_sha256")
            != _canonical_digest(prerequisite_plan)
            or prerequisites.get("adopted_deployment_sha256")
            != adopted_digest
            or prerequisite_plan.get("adopted_deployment_sha256")
            != adopted_digest
        ):
            raise SuccessorError("manual adoption authority chain is invalid")
        expected = self.expected_predecessor_provenance
        if (
            adopted_digest != expected["adopted_deployment_sha256"]
            or bootstrap_digest != expected["bootstrap_control_sha256"]
            or prerequisites_digest
            != expected["adopted_prerequisites_sha256"]
            or production_sha != expected["production_source_sha"]
            or production_tree != expected["production_source_tree"]
        ):
            raise SuccessorError("manual adoption differs from fixed provenance")
        return {
            "adopted_deployment_sha256": adopted_digest,
            "bootstrap_control_sha256": bootstrap_digest,
            "adopted_prerequisites_sha256": prerequisites_digest,
            "adopted_prerequisites_plan_sha256": prerequisites[
                "plan_sha256"
            ],
            "production_source": {
                "source_sha": production_sha,
                "source_tree": production_tree,
            },
        }

    def _predecessor_authority(
        self, state_fd: int | None = None,
    ) -> tuple[dict[str, object], str, list[str]]:
        authority, raw_digest = self._load_state_json(
            PREDECESSOR_AUTHORITY_RELATIVE_PATH,
            maximum_bytes=PREDECESSOR_MAX_BYTES,
            state_fd=state_fd,
        )
        plan = _require_exact_keys(
            authority.get("plan"),
            {
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
            },
            "predecessor permission plan",
        )
        for field in (
            "source_sha",
            "source_tree",
            "production_source_sha",
            "production_source_tree",
        ):
            _require_sha(authority.get(field), f"predecessor {field}")
        for field in (
            "adopted_deployment_sha256",
            "bootstrap_control_sha256",
            "adopted_prerequisites_sha256",
            "plan_sha256",
            "permission_impact_sha256",
            "permission_marker_sha256",
            "permission_evidence_sha256",
            "permission_inventory_sha256",
            "original_permissions_sha256",
            "hardened_permissions_sha256",
        ):
            _require_digest(authority.get(field), f"predecessor {field}")
        if (
            set(authority) != PREDECESSOR_AUTHORITY_FIELDS
            or not _has_exact_schema(authority, 1)
            or authority.get("status") != "completed"
            or authority.get("authority_kind") != PREDECESSOR_AUTHORITY_KIND
            or authority.get("plan_sha256") != _canonical_digest(plan)
            or not _has_exact_schema(plan, 1)
            or plan.get("authority_kind") != PREDECESSOR_AUTHORITY_KIND
            or plan.get("operation_id") != authority.get("operation_id")
            or plan.get("source_sha") != authority.get("source_sha")
            or plan.get("source_tree") != authority.get("source_tree")
            or plan.get("delivery_gate_sha256")
            != _canonical_digest(plan.get("delivery_gate"))
        ):
            raise SuccessorError("predecessor permission authority is invalid")
        expected = self.expected_predecessor_provenance
        if (
            authority.get("source_sha") != expected["predecessor_source_sha"]
            or authority.get("source_tree")
            != expected["predecessor_source_tree"]
            or raw_digest != expected["predecessor_authority_sha256"]
        ):
            raise SuccessorError("predecessor authority differs from fixed provenance")
        production = plan.get("production_source")
        takeover = plan.get("permission_takeover")
        readiness = plan.get("source_readiness")
        if (
            not isinstance(production, dict)
            or set(production) != {"source_sha", "source_tree"}
            or production
            != {
                "source_sha": authority["production_source_sha"],
                "source_tree": authority["production_source_tree"],
            }
            or plan.get("adopted_deployment_sha256")
            != authority.get("adopted_deployment_sha256")
            or plan.get("bootstrap_control_sha256")
            != authority.get("bootstrap_control_sha256")
            or plan.get("adopted_prerequisites_sha256")
            != authority.get("adopted_prerequisites_sha256")
            or plan.get("permission_impact_sha256")
            != authority.get("permission_impact_sha256")
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
            or not isinstance(takeover, dict)
            or takeover.get("inventory_sha256")
            != authority.get("permission_inventory_sha256")
            or takeover.get("original_permissions_sha256")
            != authority.get("original_permissions_sha256")
            or takeover.get("hardened_permissions_sha256")
            != authority.get("hardened_permissions_sha256")
            or not isinstance(readiness, dict)
            or set(readiness) != SOURCE_READINESS_FIELDS
            or not _has_exact_schema(readiness, 2)
            or readiness.get("ready") is not True
            or readiness.get("source_sha") != authority.get("source_sha")
            or readiness.get("source_tree") != authority.get("source_tree")
            or readiness.get("branch") != "main"
            or readiness.get("origin") != REPOSITORY_SSH_URL
            or readiness.get("origin_main_sha") != authority.get("source_sha")
            or plan.get("source_readiness_sha256")
            != _canonical_digest(readiness)
        ):
            raise SuccessorError("predecessor outer and plan provenance differ")
        delivery = _validate_delivery_gate(
            plan.get("delivery_gate"),
            source_sha=str(authority["source_sha"]),
        )
        jobs = list(delivery["ci"]["required_jobs"])  # type: ignore[index]
        self._validate_predecessor_completed_journal(
            authority,
            raw_digest,
            state_fd=state_fd,
        )
        return authority, raw_digest, jobs

    def _validate_predecessor_completed_journal(
        self,
        authority: Mapping[str, object],
        authority_digest: str,
        *,
        state_fd: int | None,
    ) -> None:
        close_state = False
        if state_fd is None:
            state_fd = _open_private_directory(self.state_root)
            close_state = True
        try:
            directory_fd = _open_private_directory(
                Path(PREDECESSOR_TRANSACTION_RELATIVE_DIRECTORY.name),
                parent_fd=state_fd,
            )
            try:
                name = f"{authority['operation_id']}.json"
                if os.listdir(directory_fd) != [name]:
                    raise SuccessorError(
                        "predecessor permission journal lineage differs"
                    )
                journal, raw_digest, _metadata = _load_canonical_json_at(
                    directory_fd,
                    name,
                    maximum_bytes=PREDECESSOR_MAX_BYTES,
                )
            finally:
                os.close(directory_fd)
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
            "permission_checkpoint",
            "permission_evidence_sha256",
            "permission_impact_sha256",
            "permission_marker_sha256",
            "source_trust_sha256",
            "created_at",
            "completed_at",
            "aborted_at",
        }
        expected = self.expected_predecessor_provenance
        if (
            set(journal) != fields
            or not _has_exact_schema(journal, 1)
            or journal.get("status") != "completed"
            or journal.get("phase") != "completed"
            or journal.get("operation_id") != authority.get("operation_id")
            or journal.get("plan") != authority.get("plan")
            or journal.get("plan_sha256") != authority.get("plan_sha256")
            or journal.get("permission_evidence_sha256")
            != authority.get("permission_evidence_sha256")
            or journal.get("permission_impact_sha256")
            != authority.get("permission_impact_sha256")
            or journal.get("permission_marker_sha256")
            != authority.get("permission_marker_sha256")
            or journal.get("source_trust_sha256")
            != expected["predecessor_source_trust_sha256"]
            or journal.get("permission_checkpoint") != "permission:hardened"
            or not isinstance(journal.get("created_at"), str)
            or UTC_RE.fullmatch(str(journal["created_at"])) is None
            or journal.get("completed_at") != authority.get("completed_at")
            or not isinstance(authority.get("completed_at"), str)
            or UTC_RE.fullmatch(str(authority["completed_at"])) is None
            or journal.get("aborted_at") is not None
            or raw_digest != expected["predecessor_journal_sha256"]
            or authority_digest
            != expected["predecessor_authority_sha256"]
        ):
            raise SuccessorError("predecessor permission journal is invalid")

    def _manifest(
        self,
        predecessor_sha: str,
        target_sha: str,
    ) -> tuple[list[dict[str, object]], dict[str, bytes]]:
        try:
            _run_git(
                self.source_root,
                "merge-base",
                "--is-ancestor",
                predecessor_sha,
                target_sha,
            )
        except SuccessorError as exc:
            raise SuccessorError("target is not a predecessor descendant") from exc
        records: list[dict[str, object]] = []
        predecessor_payloads: dict[str, bytes] = {}
        changed: list[str] = []
        for relative in TRACKED_SOURCE_FILES:
            old_identity, old_payload = _git_blob_identity(
                self.source_root, predecessor_sha, relative
            )
            new_identity, new_payload = _git_blob_identity(
                self.source_root, target_sha, relative
            )
            _assert_target_worktree_file(
                self.source_root, relative, new_identity, new_payload
            )
            expected_mode = (
                "100644"
                if relative
                == "ops/config/mutable-data-audit.pg_service.conf.example"
                else "100755"
            )
            if (
                old_identity["mode"] != expected_mode
                or new_identity["mode"] != expected_mode
            ):
                raise SuccessorError(f"fixed Git file mode drifted: {relative}")
            relation = (
                "byte-identical"
                if old_identity == new_identity
                else "changed"
            )
            if relation == "changed":
                changed.append(relative)
            records.append(
                {
                    "path": relative,
                    "relation": relation,
                    "predecessor": old_identity,
                    "target": new_identity,
                }
            )
            predecessor_payloads[relative] = old_payload
        if tuple(changed) != CHANGED_PATHS:
            raise SuccessorError("changed fixed paths differ from authorization")
        if set(self.expected_transitions) != set(CHANGED_PATHS):
            raise SuccessorError("reviewed source transition contract is invalid")
        by_path = {str(record["path"]): record for record in records}
        for relative in CHANGED_PATHS:
            expected = self.expected_transitions[relative]
            if set(expected) != {
                "predecessor_blob_sha",
                "predecessor_sha256",
                "target_blob_sha",
                "target_sha256",
            }:
                raise SuccessorError("reviewed source transition fields are invalid")
            record = by_path[relative]
            old_identity = record["predecessor"]
            new_identity = record["target"]
            assert isinstance(old_identity, dict) and isinstance(new_identity, dict)
            if (
                old_identity["blob_sha"] != expected["predecessor_blob_sha"]
                or old_identity["sha256"] != expected["predecessor_sha256"]
                or new_identity["blob_sha"] != expected["target_blob_sha"]
                or new_identity["sha256"] != expected["target_sha256"]
            ):
                raise SuccessorError(f"reviewed source transition differs: {relative}")
        return records, predecessor_payloads

    def _frozen_verifiers(
        self,
        predecessor_sha: str,
        predecessor_payloads: Mapping[str, bytes],
        required_jobs: list[str],
    ) -> tuple[types.ModuleType, types.ModuleType]:
        bootstrap = _load_frozen_module(
            "nexpoly_frozen_predecessor_bootstrap",
            predecessor_payloads[BOOTSTRAP_PATH],
            self.source_root / BOOTSTRAP_PATH,
        )
        trust = _load_frozen_module(
            "nexpoly_frozen_predecessor_git_source_trust",
            predecessor_payloads[GIT_TRUST_PATH],
            self.source_root / GIT_TRUST_PATH,
        )
        for module, names in (
            (bootstrap, ("bootstrap_source_readiness", "_delivery_gate")),
            (
                trust,
                (
                    "verify_repository_permission_takeover",
                    "repository_preflight_evidence",
                    "run_git",
                    "repository_trust_evidence",
                    "require_stable_trust_surface",
                ),
            ),
        ):
            if any(not callable(getattr(module, name, None)) for name in names):
                raise SuccessorError("frozen predecessor verifier API differs")
        # The old authority's exact successful job set is the only executable
        # CI contract.  In particular, the candidate bridge is never loaded.
        bootstrap._required_ci_jobs = (  # type: ignore[attr-defined]
            lambda *, source_sha, allow_test: tuple(required_jobs)
        )
        if predecessor_sha != _require_sha(predecessor_sha):
            raise SuccessorError("predecessor verifier identity differs")
        return bootstrap, trust

    def _marker_projection(
        self,
        trust: types.ModuleType,
        authority: Mapping[str, object],
        state_fd: int | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        marker_path = self.runtime_root / PERMISSION_MARKER_RELATIVE_PATH
        marker_document, marker_digest = self._load_state_json(
            PERMISSION_MARKER_RELATIVE_PATH,
            maximum_bytes=PREDECESSOR_MAX_BYTES,
            state_fd=state_fd,
        )
        if state_fd is not None:
            self._assert_state_pinned(state_fd)
        try:
            verified = trust.verify_repository_permission_takeover(
                self.production_root,
                marker_path,
            )
        except BaseException as exc:
            raise SuccessorError("frozen predecessor marker verification failed") from exc
        if state_fd is not None:
            self._assert_state_pinned(state_fd)
        if not isinstance(verified, dict) or verified != marker_document:
            raise SuccessorError("frozen predecessor marker evidence differs")
        fields = (
            "evidence_sha256",
            "inventory_sha256",
            "original_permissions_sha256",
            "hardened_permissions_sha256",
        )
        for field in fields:
            _require_digest(verified.get(field), f"marker {field}")
        if (
            marker_digest != authority.get("permission_marker_sha256")
            or verified.get("evidence_sha256")
            != authority.get("permission_evidence_sha256")
            or verified.get("inventory_sha256")
            != authority.get("permission_inventory_sha256")
            or verified.get("original_permissions_sha256")
            != authority.get("original_permissions_sha256")
            or verified.get("hardened_permissions_sha256")
            != authority.get("hardened_permissions_sha256")
        ):
            raise SuccessorError("predecessor permission marker drifted")
        if marker_digest != self.expected_predecessor_provenance[
            "predecessor_marker_sha256"
        ]:
            raise SuccessorError("permission marker differs from fixed provenance")
        return (
            {
                "path": str(marker_path),
                "raw_sha256": marker_digest,
                "evidence_sha256": verified["evidence_sha256"],
                "inventory_sha256": verified["inventory_sha256"],
                "original_permissions_sha256": verified[
                    "original_permissions_sha256"
                ],
                "hardened_permissions_sha256": verified[
                    "hardened_permissions_sha256"
                ],
            },
            verified,
        )

    def _production_source_trust(
        self,
        trust: types.ModuleType,
        production: Mapping[str, object],
        *,
        target_sha: str,
        target_tree: str,
    ) -> tuple[str, dict[str, object]]:
        try:
            before = trust.repository_preflight_evidence(
                self.production_root,
                ambient={},
            )
            status = trust.run_git(
                self.production_root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                ambient={},
            ).stdout
            if status:
                raise SuccessorError("production checkout is dirty")
            evidence = trust.repository_trust_evidence(
                self.production_root,
                source_sha=str(production["source_sha"]),
                source_tree=str(production["source_tree"]),
                branch="refs/heads/main",
                origin=None,
                ambient={},
            )
            trust.require_stable_trust_surface(before, evidence)
            logical_refs = _logical_ref_inventory(
                trust, self.production_root
            )
            raw_refs, raw_ref_projection = _raw_ref_inventory(
                self.production_root
            )
            auxiliary = _git_auxiliary_inventory(self.production_root)
            object_storage = _git_object_storage_inventory(
                self.production_root
            )
            baseline_semantic_objects = _all_semantic_objects(
                self.production_root, trust=trust
            )
            middle = trust.repository_preflight_evidence(
                self.production_root,
                ambient={},
            )
            trust.require_stable_trust_surface(evidence, middle)
            logical_refs_after = _logical_ref_inventory(
                trust, self.production_root
            )
            raw_refs_after, raw_ref_projection_after = _raw_ref_inventory(
                self.production_root
            )
            auxiliary_after = _git_auxiliary_inventory(
                self.production_root
            )
            object_storage_after = _git_object_storage_inventory(
                self.production_root
            )
            baseline_semantic_objects_after = _all_semantic_objects(
                self.production_root, trust=trust
            )
            after = trust.repository_preflight_evidence(
                self.production_root,
                ambient={},
            )
            trust.require_stable_trust_surface(middle, after)
        except SuccessorError:
            raise
        except BaseException as exc:
            raise SuccessorError("frozen predecessor production trust failed") from exc
        if not isinstance(evidence, dict):
            raise SuccessorError("production source trust evidence is invalid")
        digest = _require_digest(
            evidence.get("evidence_sha256"),
            "production source trust evidence",
        )
        if digest != self.expected_predecessor_provenance[
            "production_source_trust_sha256"
        ]:
            raise SuccessorError("current production source trust drifted")
        if (
            logical_refs_after != logical_refs
            or raw_refs_after != raw_refs
            or raw_ref_projection_after != raw_ref_projection
            or auxiliary_after != auxiliary
            or object_storage_after != object_storage
            or baseline_semantic_objects_after != baseline_semantic_objects
        ):
            raise SuccessorError(
                "production repository transition baseline changed"
            )
        stable = _repository_stable_projection(
            evidence,
            root=self.production_root,
            source_sha=str(production["source_sha"]),
            source_tree=str(production["source_tree"]),
        )
        target_reachable_objects = _target_reachable_semantic_objects(
            self.source_root, target_sha
        )
        target_objects_by_oid = {
            str(record["oid"]): record for record in target_reachable_objects
        }
        baseline_objects_by_oid = {
            str(record["oid"]): record for record in baseline_semantic_objects
        }
        for object_sha in set(target_objects_by_oid) & set(
            baseline_objects_by_oid
        ):
            if target_objects_by_oid[object_sha] != baseline_objects_by_oid[
                object_sha
            ]:
                raise SuccessorError("shared Git semantic object differs")
        baseline_only_objects = [
            baseline_objects_by_oid[object_sha]
            for object_sha in sorted(
                set(baseline_objects_by_oid) - set(target_objects_by_oid)
            )
        ]
        materialized_objects = [
            (baseline_objects_by_oid | target_objects_by_oid)[object_sha]
            for object_sha in sorted(
                set(baseline_objects_by_oid) | set(target_objects_by_oid)
            )
        ]
        transition: dict[str, object] = {
            "schema_version": 1,
            "policy": REPOSITORY_TRANSITION_POLICY,
            "source": {
                "sha": production["source_sha"],
                "tree": production["source_tree"],
            },
            "target": {"sha": target_sha, "tree": target_tree},
            "baseline_evidence_sha256": digest,
            "stable_projection": stable,
            "stable_projection_sha256": _canonical_digest(stable),
            "logical_refs": logical_refs,
            "logical_refs_sha256": _canonical_digest(logical_refs),
            "raw_ref_inventory": raw_refs,
            "raw_ref_inventory_sha256": _canonical_digest(raw_refs),
            "baseline_auxiliary_inventory": auxiliary,
            "baseline_auxiliary_inventory_sha256": _canonical_digest(
                auxiliary
            ),
            "baseline_semantic_object_count": len(
                baseline_semantic_objects
            ),
            "baseline_semantic_objects_sha256": _canonical_digest(
                baseline_semantic_objects
            ),
            "baseline_only_object_count": len(baseline_only_objects),
            "baseline_only_objects_sha256": _canonical_digest(
                baseline_only_objects
            ),
            "target_reachable_object_count": len(target_reachable_objects),
            "target_reachable_objects_sha256": _canonical_digest(
                target_reachable_objects
            ),
            "expected_materialized_object_count": len(
                materialized_objects
            ),
            "expected_materialized_objects_sha256": _canonical_digest(
                materialized_objects
            ),
            "mutable_refs": {
                "deploy_remote": DEPLOY_REMOTE_REF,
                "prepared_prefix": PREPARED_REF_PREFIX,
            },
            "storage_policy": {
                "standalone": evidence["objects"]["standalone"],
                "promisor": evidence["objects"]["promisor"],
                "alternates": evidence["objects"]["alternates"],
                "replace_refs": evidence["refs"]["replace_refs"],
            },
            "auxiliary_policy": GIT_AUXILIARY_POLICY,
            "object_storage_policy": GIT_OBJECT_STORAGE_POLICY,
            "object_materialization_policy": (
                "strict-fsck-owner-private-content-addressed-target-closure-v1"
            ),
        }
        return digest, _validate_repository_transition(
            transition,
            production_root=self.production_root,
            production_sha=str(production["source_sha"]),
            production_tree=str(production["source_tree"]),
            target_sha=target_sha,
            target_tree=target_tree,
            evidence=evidence,
            trust_order_loose_records=raw_ref_projection,
        )

    def _delivery_gate(
        self,
        bootstrap: types.ModuleType,
        source_sha: str,
        required_jobs: list[str],
        *,
        sealed: object | None,
    ) -> dict[str, object]:
        try:
            if self.delivery_gate_probe is not None:
                raw = self.delivery_gate_probe(
                    bootstrap,
                    self.production_root,
                    self.runtime_root,
                    source_sha,
                    required_jobs,
                    sealed,
                )
            else:
                raw = bootstrap._delivery_gate(
                    self.production_root,
                    self.runtime_root,
                    source_sha,
                    allow_test=False,
                    sealed=sealed,
                )
        except BaseException as exc:
            raise SuccessorError("frozen predecessor delivery gate failed") from exc
        delivery = _validate_delivery_gate(
            raw,
            source_sha=source_sha,
            required_jobs=required_jobs,
        )
        if sealed is not None and delivery != sealed:
            raise SuccessorError("sealed delivery gate changed")
        return delivery

    def _build_plan(
        self,
        source_sha: str,
        operation_id: str,
        *,
        live_delivery: bool,
        sealed_plan: Mapping[str, object] | None = None,
        state_fd: int | None = None,
    ) -> tuple[dict[str, object], str]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_operation_id(operation_id)
        _assert_safe_git_root(self.source_root)
        source_tree = _git_commit_tree(self.source_root, source_sha)
        head = str(_run_git(self.source_root, "rev-parse", "HEAD", text=True)).strip()
        if head != source_sha:
            raise SuccessorError("target source is not the checked-out HEAD")
        if state_fd is not None:
            self._assert_state_pinned(state_fd)
        authority, authority_digest, jobs = self._predecessor_authority(
            state_fd
        )
        predecessor_sha = str(authority["source_sha"])
        predecessor_tree = _git_commit_tree(self.source_root, predecessor_sha)
        if predecessor_tree != authority.get("source_tree"):
            raise SuccessorError("predecessor Git tree differs from authority")
        files, old_payloads = self._manifest(predecessor_sha, source_sha)
        bootstrap, trust = self._frozen_verifiers(
            predecessor_sha, old_payloads, jobs
        )
        try:
            frozen_raw = bootstrap.bootstrap_source_readiness(
                self.source_root,
                expected_sha=source_sha,
            )
        except BaseException as exc:
            raise SuccessorError("frozen predecessor source verifier failed") from exc
        frozen = _validate_readiness(
            frozen_raw,
            root=self.source_root,
            source_sha=source_sha,
            source_tree=source_tree,
        )
        independent = _independent_source_readiness(
            self.source_root, source_sha
        )
        if frozen != independent:
            raise SuccessorError("independent and frozen source verifiers disagree")
        sealed_delivery = (
            sealed_plan.get("delivery_gate")
            if sealed_plan is not None
            else None
        )
        if live_delivery:
            delivery = self._delivery_gate(
                bootstrap,
                source_sha,
                jobs,
                sealed=sealed_delivery,
            )
        else:
            delivery = _validate_delivery_gate(
                sealed_delivery,
                source_sha=source_sha,
                required_jobs=jobs,
            )
        adoption = self._adoption_context(state_fd)
        if (
            authority.get("adopted_deployment_sha256")
            != adoption["adopted_deployment_sha256"]
            or authority.get("bootstrap_control_sha256")
            != adoption["bootstrap_control_sha256"]
            or authority.get("adopted_prerequisites_sha256")
            != adoption["adopted_prerequisites_sha256"]
            or authority.get("production_source_sha")
            != adoption["production_source"]["source_sha"]  # type: ignore[index]
            or authority.get("production_source_tree")
            != adoption["production_source"]["source_tree"]  # type: ignore[index]
            or authority["plan"].get(
                "adopted_prerequisites_plan_sha256"
            )
            != adoption["adopted_prerequisites_plan_sha256"]
        ):
            raise SuccessorError("predecessor authority differs from adoption")
        marker, _marker_document = self._marker_projection(
            trust, authority, state_fd
        )
        production_trust_digest, repository_transition = self._production_source_trust(
            trust,
            adoption["production_source"],  # type: ignore[arg-type]
            target_sha=source_sha,
            target_tree=source_tree,
        )
        transition_refs = {
            str(record["name"]): str(record["object_sha"])
            for record in repository_transition["logical_refs"]  # type: ignore[index]
        }
        if transition_refs.get(DEPLOY_REMOTE_REF) != predecessor_sha:
            raise SuccessorError(
                "production deployment remote does not name the predecessor"
            )
        if state_fd is not None:
            self._assert_state_pinned(state_fd)
        predecessor = {
            "authority_kind": PREDECESSOR_AUTHORITY_KIND,
            "operation_id": authority["operation_id"],
            "source_sha": predecessor_sha,
            "source_tree": predecessor_tree,
            "authority_sha256": authority_digest,
            "completed_journal_sha256": self.expected_predecessor_provenance[
                "predecessor_journal_sha256"
            ],
            "source_trust_sha256": self.expected_predecessor_provenance[
                "predecessor_source_trust_sha256"
            ],
            "plan_sha256": authority["plan_sha256"],
            "permission_marker_sha256": authority[
                "permission_marker_sha256"
            ],
            "permission_evidence_sha256": authority[
                "permission_evidence_sha256"
            ],
            "permission_inventory_sha256": authority[
                "permission_inventory_sha256"
            ],
            "original_permissions_sha256": authority[
                "original_permissions_sha256"
            ],
            "hardened_permissions_sha256": authority[
                "hardened_permissions_sha256"
            ],
        }
        by_path = {str(record["path"]): record for record in files}
        verifier_agreement = {
            "schema_version": 1,
            "policy": VERIFIER_POLICY,
            "candidate_execution": "forbidden-before-authority",
            "predecessor_source_sha": predecessor_sha,
            "predecessor_source_tree": predecessor_tree,
            "bootstrap": by_path[BOOTSTRAP_PATH],
            "git_source_trust": by_path[GIT_TRUST_PATH],
            "ci_contract": by_path[CI_CONTRACT_PATH],
            "required_jobs": jobs,
            "required_jobs_sha256": _canonical_digest(jobs),
        }
        publication = _publication_plan(self.runtime_root, operation_id)
        files_digest = _canonical_digest(files)
        changed_paths = list(CHANGED_PATHS)
        changed_digest = _canonical_digest(changed_paths)
        impact = {
            "schema_version": 1,
            "policy": IMPACT_POLICY,
            "predecessor_authority_sha256": authority_digest,
            "predecessor_marker_sha256": marker["raw_sha256"],
            "production_source_trust_sha256": production_trust_digest,
            "production_repository_transition_sha256": _canonical_digest(
                repository_transition
            ),
            "target": {
                "source_sha": source_sha,
                "source_tree": source_tree,
            },
            "files": files,
            "files_sha256": files_digest,
            "changed_paths": changed_paths,
            "changed_paths_sha256": changed_digest,
            "authority_publication": publication,
            "mutations": dict(MUTATIONS),
        }
        plan = {
            "schema_version": 1,
            "authority_kind": AUTHORITY_KIND,
            "policy": POLICY,
            "operation_id": operation_id,
            "source_sha": source_sha,
            "source_tree": source_tree,
            "source_readiness": frozen,
            "source_readiness_sha256": _canonical_digest(frozen),
            "delivery_gate": delivery,
            "delivery_gate_sha256": _canonical_digest(delivery),
            "adopted_deployment_sha256": adoption[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": adoption[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": adoption[
                "adopted_prerequisites_sha256"
            ],
            "production_source_trust_sha256": production_trust_digest,
            "production_repository_transition": repository_transition,
            "production_repository_transition_sha256": _canonical_digest(
                repository_transition
            ),
            "production_source": adoption["production_source"],
            "predecessor": predecessor,
            "marker": marker,
            "verifier_agreement": verifier_agreement,
            "files": files,
            "files_sha256": files_digest,
            "changed_paths": changed_paths,
            "changed_paths_sha256": changed_digest,
            "authority_publication": publication,
            "source_successor_impact": impact,
            "source_successor_impact_sha256": _canonical_digest(impact),
            "mutations": dict(MUTATIONS),
        }
        if set(plan) != PLAN_FIELDS:
            raise SuccessorError("internal source successor plan shape differs")
        if sealed_plan is not None and plan != sealed_plan:
            raise SuccessorError("durable source successor plan changed")
        return plan, production_trust_digest

    def plan(
        self,
        *,
        source_sha: str,
        operation_id: str,
    ) -> dict[str, object]:
        self._assert_initial_namespace_absent()
        plan, _production_trust_digest = self._build_plan(
            source_sha,
            operation_id,
            live_delivery=True,
        )
        self._assert_initial_namespace_absent()
        return {
            "action": "adopt-git-permission-source-successor-plan",
            "apply": False,
            "logical_zero_write": True,
            "plan": plan,
            "plan_sha256": _canonical_digest(plan),
            "source_successor_impact_sha256": plan[
                "source_successor_impact_sha256"
            ],
        }

    def _assert_initial_namespace_absent(self) -> None:
        state_fd = _open_private_directory(self.state_root)
        try:
            self._assert_state_pinned(state_fd)

            def snapshot() -> tuple[list[str], list[str]]:
                return (
                    self._publication_entries(state_fd),
                    sorted(
                        name
                        for name in os.listdir(state_fd)
                        if name == TRANSACTION_RELATIVE_DIRECTORY.name
                        or name.startswith(TRANSACTION_STAGING_PREFIX)
                    ),
                )

            first = snapshot()
            second = snapshot()
            self._assert_state_pinned(state_fd)
            if first != ([], []) or second != first:
                raise SuccessorError(
                    "source successor initial namespace is occupied"
                )
        finally:
            os.close(state_fd)

    def _apply_preliminary_plan(
        self,
        source_sha: str,
        operation_id: str,
    ) -> dict[str, object]:
        allowed = {
            TRANSACTION_RELATIVE_DIRECTORY.name,
            self._initial_transaction_staging(operation_id),
            f"{self._initial_transaction_staging(operation_id)}.quarantine",
        }

        def assert_recoverable_namespace() -> None:
            state_fd = _open_private_directory(self.state_root)
            try:
                self._assert_state_pinned(state_fd)
                if self._publication_entries(state_fd):
                    raise SuccessorError(
                        "source successor publication namespace is occupied"
                    )
                lineage = {
                    name
                    for name in os.listdir(state_fd)
                    if name == TRANSACTION_RELATIVE_DIRECTORY.name
                    or name.startswith(TRANSACTION_STAGING_PREFIX)
                }
                if not lineage.issubset(allowed):
                    raise SuccessorError(
                        "source successor recovery namespace differs"
                    )
                transaction_fd = self._transaction_directory_fd(
                    state_fd, create=False
                )
                if transaction_fd is not None:
                    try:
                        if os.listdir(transaction_fd):
                            raise SuccessorError(
                                "durable journal exists during preliminary plan"
                            )
                    finally:
                        os.close(transaction_fd)
                self._assert_state_pinned(state_fd)
            finally:
                os.close(state_fd)

        assert_recoverable_namespace()
        plan, _production_trust = self._build_plan(
            source_sha,
            operation_id,
            live_delivery=True,
        )
        assert_recoverable_namespace()
        return {
            "action": "adopt-git-permission-source-successor-plan",
            "apply": False,
            "logical_zero_write": True,
            "plan": plan,
            "plan_sha256": _canonical_digest(plan),
            "source_successor_impact_sha256": plan[
                "source_successor_impact_sha256"
            ],
        }

    @contextlib.contextmanager
    def _deployment_lock(self) -> Any:
        runtime_fd = _open_private_directory(self.runtime_root)
        state_fd = _open_private_directory(Path("state"), parent_fd=runtime_fd)
        lock_fd = _open_private_regular_at(state_fd, "deploy.lock", writable=True)
        source_fd = _open_private_directory(self.source_root)
        source_git_fd = _open_private_directory(
            Path(".git"), parent_fd=source_fd
        )
        production_fd = _open_private_directory(self.production_root)
        production_git_fd = _open_private_directory(
            Path(".git"), parent_fd=production_fd
        )
        original_checkpoint = self.checkpoint
        content_guard: Callable[[], None] | None = None

        def inode_identity(metadata: os.stat_result) -> tuple[int, ...]:
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_nlink,
            )

        def assert_directory_path(
            descriptor: int, path: Path, *, label: str
        ) -> None:
            held = os.fstat(descriptor)
            try:
                observed = path.lstat()
            except OSError as exc:
                raise SuccessorError(f"{label} path changed") from exc
            if (
                not stat.S_ISDIR(observed.st_mode)
                or path.is_symlink()
                or inode_identity(held) != inode_identity(observed)
            ):
                raise SuccessorError(f"{label} path changed")

        def assert_child_directory(
            parent_fd: int,
            name: str,
            descriptor: int,
            *,
            label: str,
        ) -> None:
            held = os.fstat(descriptor)
            try:
                observed = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise SuccessorError(f"{label} path changed") from exc
            if (
                not stat.S_ISDIR(observed.st_mode)
                or inode_identity(held) != inode_identity(observed)
            ):
                raise SuccessorError(f"{label} path changed")

        lock_identity = inode_identity(os.fstat(lock_fd))

        def assert_guard() -> None:
            assert_directory_path(
                runtime_fd, self.runtime_root, label="runtime root"
            )
            self._assert_state_pinned(state_fd)
            assert_child_directory(
                runtime_fd,
                "state",
                state_fd,
                label="runtime state directory",
            )
            assert_directory_path(
                source_fd, self.source_root, label="source root"
            )
            assert_child_directory(
                source_fd,
                ".git",
                source_git_fd,
                label="source Git directory",
            )
            assert_directory_path(
                production_fd,
                self.production_root,
                label="production root",
            )
            assert_child_directory(
                production_fd,
                ".git",
                production_git_fd,
                label="production Git directory",
            )
            try:
                observed_lock = os.stat(
                    "deploy.lock",
                    dir_fd=state_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise SuccessorError("deployment lock path changed") from exc
            if (
                not stat.S_ISREG(observed_lock.st_mode)
                or inode_identity(os.fstat(lock_fd)) != lock_identity
                or inode_identity(observed_lock) != lock_identity
            ):
                raise SuccessorError("deployment lock path changed")

        def set_content_guard(callback: Callable[[], None]) -> None:
            nonlocal content_guard
            if content_guard is not None:
                raise SuccessorError("deployment content guard is already active")
            content_guard = callback

        def assert_all() -> None:
            assert_guard()
            if content_guard is not None:
                content_guard()
                assert_guard()

        def guarded_checkpoint(label: str) -> None:
            # Low-level crash checkpoints can fire dozens of times while one
            # journal generation is exchanged.  They need the cheap pinned
            # path/lock guard; complete Git reproof is reserved for the small,
            # fixed set of irreversible publication boundaries in apply().
            assert_guard()
            try:
                original_checkpoint(label)
            finally:
                assert_guard()

        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            assert_guard()
            self.checkpoint = guarded_checkpoint
            try:
                yield state_fd, assert_guard, set_content_guard, assert_all
            finally:
                assert_guard()
        finally:
            self.checkpoint = original_checkpoint
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(production_git_fd)
                os.close(production_fd)
                os.close(source_git_fd)
                os.close(source_fd)
                os.close(lock_fd)
                os.close(state_fd)
                os.close(runtime_fd)

    def _transaction_directory_fd(
        self,
        state_fd: int,
        *,
        create: bool,
    ) -> int | None:
        name = TRANSACTION_RELATIVE_DIRECTORY.name
        try:
            return _open_private_directory(Path(name), parent_fd=state_fd)
        except SuccessorError:
            try:
                os.stat(name, dir_fd=state_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    return None
            else:
                raise
            os.mkdir(name, 0o700, dir_fd=state_fd)
            os.fsync(state_fd)
            return _open_private_directory(Path(name), parent_fd=state_fd)

    @staticmethod
    def _initial_transaction_staging(operation_id: str) -> str:
        return f"{TRANSACTION_STAGING_PREFIX}{operation_id}.json"

    def _publish_initial_transaction(
        self,
        state_fd: int,
        document: dict[str, object],
    ) -> None:
        operation_id = str(document["operation_id"])
        staging = self._initial_transaction_staging(operation_id)
        quarantine = f"{staging}.quarantine"
        if _entry_exists_at(state_fd, staging) or _entry_exists_at(
            state_fd, quarantine
        ):
            raise SuccessorError(
                "legacy initial journal staging requires recovery"
            )
        if not _entry_exists_at(state_fd, TRANSACTION_RELATIVE_DIRECTORY.name):
            os.mkdir(
                TRANSACTION_RELATIVE_DIRECTORY.name,
                0o700,
                dir_fd=state_fd,
            )
            os.fsync(state_fd)
            self.checkpoint("source-successor-transaction-directory-ready")
        transaction_fd = self._transaction_directory_fd(
            state_fd, create=False
        )
        assert transaction_fd is not None
        try:
            name = f"{operation_id}.json"
            if os.listdir(transaction_fd):
                raise SuccessorError("initial transaction directory is occupied")
            _atomic_json_at(
                transaction_fd,
                name,
                document,
                checkpoint=self.checkpoint,
            )
            os.fsync(transaction_fd)
            os.fsync(state_fd)
            self.checkpoint("source-successor-initial-journal-staged")
            loaded, _digest, _metadata = _load_canonical_json_at(
                transaction_fd,
                name,
                maximum_bytes=JSON_MAX_BYTES,
            )
            if loaded != document:
                raise SuccessorError(
                    "initial transaction publication differs"
                )
            self.checkpoint("source-successor-initial-journal-linked")
        finally:
            os.close(transaction_fd)

    def _validate_transaction_document(
        self,
        document: object,
        operation_id: str,
    ) -> dict[str, object]:
        if (
            not isinstance(document, dict)
            or
            set(document) != TRANSACTION_FIELDS
            or not _has_exact_schema(document, 1)
            or document.get("operation_id") != operation_id
            or document.get("status") not in {"applying", "completed", "aborted"}
            or document.get("phase") not in TRANSACTION_PHASES
            or not isinstance(document.get("plan"), dict)
            or set(document["plan"]) != PLAN_FIELDS
            or document.get("plan_sha256")
            != _canonical_digest(document["plan"])
            or document.get("source_successor_impact_sha256")
            != document["plan"].get("source_successor_impact_sha256")
            or document.get("production_source_trust_sha256")
            != document["plan"].get("production_source_trust_sha256")
            or document.get("status") == "completed"
            and document.get("phase") != "completed"
            or document.get("status") == "aborted"
            and document.get("phase") != "aborted"
            or document.get("status") == "applying"
            and document.get("phase") in {"completed", "aborted"}
        ):
            raise SuccessorError("source successor transaction is invalid")
        trust_digest = document.get("production_source_trust_sha256")
        _require_digest(trust_digest, "transaction production trust")
        created_at = document.get("created_at")
        completed_at = document.get("completed_at")
        aborted_at = document.get("aborted_at")
        if (
            not isinstance(created_at, str)
            or UTC_RE.fullmatch(created_at) is None
            or completed_at is not None
            and (
                not isinstance(completed_at, str)
                or UTC_RE.fullmatch(completed_at) is None
            )
            or aborted_at is not None
            and (
                not isinstance(aborted_at, str)
                or UTC_RE.fullmatch(aborted_at) is None
            )
            or document.get("phase") == "authority-commit-intent"
            and completed_at is None
            or document.get("status") == "completed"
            and (completed_at is None or aborted_at is not None)
            or document.get("status") == "aborted"
            and (aborted_at is None or completed_at is not None)
            or document.get("status") == "applying"
            and document.get("phase") != "authority-commit-intent"
            and (completed_at is not None or aborted_at is not None)
        ):
            raise SuccessorError("source successor journal timestamps are invalid")
        assert isinstance(created_at, str)
        if (
            (
                isinstance(completed_at, str)
                and created_at > completed_at
            )
            or (
                isinstance(aborted_at, str)
                and created_at > aborted_at
            )
        ):
            raise SuccessorError(
                "source successor journal timestamps are not monotonic"
            )
        return document

    @staticmethod
    def _transaction_rank(document: Mapping[str, object]) -> int:
        phase = document.get("phase")
        order = {
            "intent": 0,
            "predecessor-verified": 1,
            "source-verified": 2,
            "authority-commit-intent": 3,
            "completed": 4,
            "aborted": 4,
        }
        return order[str(phase)]

    def _staged_transaction_follows(
        self,
        current: Mapping[str, object],
        staged: Mapping[str, object],
    ) -> bool:
        if any(
            current.get(field) != staged.get(field)
            for field in (
                "schema_version",
                "operation_id",
                "plan",
                "plan_sha256",
                "source_successor_impact_sha256",
                "production_source_trust_sha256",
                "created_at",
            )
        ):
            return False
        current_rank = self._transaction_rank(current)
        staged_rank = self._transaction_rank(staged)
        if staged.get("phase") == "aborted":
            return current.get("phase") in {
                "intent",
                "predecessor-verified",
                "source-verified",
                "aborted",
            }
        if staged_rank not in {current_rank, current_rank + 1}:
            return False
        if staged.get("phase") == "completed":
            return current.get("phase") in {
                "authority-commit-intent",
                "completed",
            }
        return staged.get("status") == "applying"

    def _exchange_transaction_from_payload(
        self,
        payload: bytes,
        operation_id: str,
        *,
        expected_plan_sha256: str,
        expected_impact_sha256: str,
        expected_source_sha: str,
        label: str,
    ) -> dict[str, object]:
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SuccessorError(f"{label} is not canonical JSON") from exc
        if not isinstance(raw, dict) or payload != _canonical_bytes(raw) + b"\n":
            raise SuccessorError(f"{label} is not canonical JSON")
        document = self._validate_transaction_document(raw, operation_id)
        if (
            document.get("plan_sha256") != expected_plan_sha256
            or document.get("source_successor_impact_sha256")
            != expected_impact_sha256
            or document["plan"].get("source_sha") != expected_source_sha
        ):
            raise SuccessorError(f"{label} confirmation differs")
        return document

    def _recover_exchange_evidence(
        self,
        transaction_fd: int,
        operation_id: str,
        *,
        expected_plan_sha256: str,
        expected_impact_sha256: str,
        expected_source_sha: str,
    ) -> dict[str, object]:
        name = f"{operation_id}.json"
        temporary = f".{name}.tmp"
        quarantine = f"{temporary}.quarantine"
        evidence_name = _exchange_evidence_name(temporary)
        evidence_quarantine = _exchange_evidence_quarantine(temporary)
        evidence_exists = _entry_exists_at(transaction_fd, evidence_name)
        evidence_quarantined = _entry_exists_at(
            transaction_fd, evidence_quarantine
        )
        if evidence_exists == evidence_quarantined:
            raise SuccessorError("journal exchange evidence namespace differs")
        evidence_path = (
            evidence_name if evidence_exists else evidence_quarantine
        )
        evidence_fd, raw_evidence, _digest, evidence_payload, evidence_metadata = (
            _open_canonical_json_at(
                transaction_fd,
                evidence_path,
                maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
            )
        )
        final_fd: int | None = None
        residue_fd: int | None = None
        try:
            evidence = _validate_exchange_evidence(
                raw_evidence,
                directory_fd=transaction_fd,
                operation_id=operation_id,
                name=name,
                temporary=temporary,
            )
            _revalidate_open_path_at(
                transaction_fd,
                evidence_path,
                evidence_fd,
                evidence_payload,
                evidence_metadata,
                label="durable journal exchange evidence CAS",
                maximum_bytes=EXCHANGE_EVIDENCE_MAX_BYTES,
            )
            staging_identity = evidence["staging"]
            staging_raw_document = evidence["staging_document"]
            assert isinstance(staging_identity, dict)
            assert isinstance(staging_raw_document, dict)
            staging_payload = _canonical_bytes(staging_raw_document) + b"\n"
            staging_document = self._exchange_transaction_from_payload(
                staging_payload,
                operation_id,
                expected_plan_sha256=expected_plan_sha256,
                expected_impact_sha256=expected_impact_sha256,
                expected_source_sha=expected_source_sha,
                label="evidenced intended staging journal",
            )
            (
                final_fd,
                _raw_final,
                _final_digest,
                final_payload,
                final_metadata,
            ) = _open_canonical_json_at(
                transaction_fd,
                name,
                maximum_bytes=JSON_MAX_BYTES,
            )
            final_document = self._exchange_transaction_from_payload(
                final_payload,
                operation_id,
                expected_plan_sha256=expected_plan_sha256,
                expected_impact_sha256=expected_impact_sha256,
                expected_source_sha=expected_source_sha,
                label="evidenced final journal",
            )
            current_identity = evidence["current"]
            assert isinstance(current_identity, dict)
            final_is_current = _exchange_identity_matches(
                current_identity, final_payload, final_metadata
            )
            final_is_staging = (
                final_payload == staging_payload
                and _exchange_staging_payload_matches(
                    staging_identity, final_payload, final_metadata
                )
            )
            temporary_exists = _entry_exists_at(transaction_fd, temporary)
            quarantine_exists = _entry_exists_at(transaction_fd, quarantine)
            if temporary_exists and quarantine_exists:
                raise SuccessorError(
                    "evidenced journal staging and quarantine both exist"
                )
            residue_name: str | None = None
            if temporary_exists:
                residue_name = temporary
            elif quarantine_exists:
                residue_name = quarantine
            residue_payload: bytes | None = None
            residue_metadata: os.stat_result | None = None
            residue_document: dict[str, object] | None = None
            residue_is_current = False
            residue_is_staging = False
            if residue_name is not None:
                residue_fd = _open_private_regular_at(
                    transaction_fd, residue_name
                )
                residue_payload, residue_metadata = _read_descriptor(
                    residue_fd,
                    maximum_bytes=JSON_MAX_BYTES,
                    label="evidenced journal residue",
                )
                _revalidate_open_path_at(
                    transaction_fd,
                    residue_name,
                    residue_fd,
                    residue_payload,
                    residue_metadata,
                    label="evidenced journal residue CAS",
                )
                residue_document = self._exchange_transaction_from_payload(
                    residue_payload,
                    operation_id,
                    expected_plan_sha256=expected_plan_sha256,
                    expected_impact_sha256=expected_impact_sha256,
                    expected_source_sha=expected_source_sha,
                    label="evidenced journal residue",
                )
                residue_is_current = _exchange_identity_matches(
                    current_identity, residue_payload, residue_metadata
                )
                residue_is_staging = (
                    residue_payload == staging_payload
                    and _exchange_staging_payload_matches(
                        staging_identity, residue_payload, residue_metadata
                    )
                )
            if final_is_current and not self._staged_transaction_follows(
                final_document, staging_document
            ):
                raise SuccessorError(
                    "evidenced journal transition is invalid"
                )
            if (
                residue_is_current
                and residue_document is not None
                and not self._staged_transaction_follows(
                    residue_document, staging_document
                )
            ):
                raise SuccessorError(
                    "evidenced journal transition is invalid"
                )
            if evidence_quarantined:
                if (
                    residue_name is not None
                    or not (final_is_current or final_is_staging)
                ):
                    raise SuccessorError(
                        "quarantined journal exchange evidence state differs"
                    )
                _remove_exchange_evidence_at(
                    transaction_fd,
                    temporary,
                    evidence_payload,
                    self.checkpoint,
                )
                return final_document
            if final_is_current and residue_name is None:
                _remove_exchange_evidence_at(
                    transaction_fd,
                    temporary,
                    evidence_payload,
                    self.checkpoint,
                )
                return final_document
            if final_is_current and temporary_exists and residue_is_staging:
                assert (
                    residue_fd is not None
                    and residue_payload is not None
                    and residue_metadata is not None
                    and residue_document is not None
                )
                if not self._staged_transaction_follows(
                    final_document, staging_document
                ):
                    raise SuccessorError(
                        "evidenced journal transition is invalid"
                    )
                _complete_evidenced_exchange_at(
                    transaction_fd,
                    name,
                    temporary,
                    current_fd=final_fd,
                    current_payload=final_payload,
                    current_metadata=final_metadata,
                    staging_fd=residue_fd,
                    staging_payload=residue_payload,
                    staging_metadata=residue_metadata,
                    evidence_fd=evidence_fd,
                    evidence_payload=evidence_payload,
                    evidence_metadata=evidence_metadata,
                    checkpoint=self.checkpoint,
                    after_exchange=lambda: self.checkpoint(
                        "source-successor-journal-recovery-exchanged"
                    ),
                    after_retired_quarantine=lambda: self.checkpoint(
                        "source-successor-journal-retired-quarantined"
                    ),
                )
                return staging_document
            if (
                final_is_staging
                and residue_name is not None
                and residue_is_current
            ):
                assert residue_document is not None
                if not self._staged_transaction_follows(
                    residue_document, staging_document
                ):
                    raise SuccessorError(
                        "evidenced exchanged journal transition is invalid"
                    )
                _quarantine_unlink_at(
                    transaction_fd,
                    temporary,
                    quarantine,
                    expected_payload=residue_payload,
                    after_quarantine=lambda: self.checkpoint(
                        "source-successor-journal-retired-quarantined"
                    ),
                )
                self.checkpoint(
                    "source-successor-journal-retired-generation-removed"
                )
                _remove_exchange_evidence_at(
                    transaction_fd,
                    temporary,
                    evidence_payload,
                    self.checkpoint,
                )
                return final_document
            if final_is_staging and residue_name is None:
                _remove_exchange_evidence_at(
                    transaction_fd,
                    temporary,
                    evidence_payload,
                    self.checkpoint,
                )
                return final_document
            if temporary_exists and residue_is_current:
                assert (
                    residue_fd is not None
                    and residue_payload is not None
                    and residue_metadata is not None
                )
                try:
                    _restore_evidenced_current_at(
                        transaction_fd,
                        name,
                        temporary,
                        current_fd=residue_fd,
                        current_payload=residue_payload,
                        current_metadata=residue_metadata,
                        unexpected_final_fd=final_fd,
                        unexpected_final_payload=final_payload,
                        unexpected_final_metadata=final_metadata,
                    )
                except (OSError, SuccessorError) as exc:
                    raise SuccessorError(
                        "evidenced journal mismatch rollback failed"
                    ) from exc
                raise SuccessorError(
                    "evidenced journal CAS mismatch was rolled back"
                )
            raise SuccessorError(
                "journal paths do not match durable exchange evidence"
            )
        finally:
            if residue_fd is not None:
                os.close(residue_fd)
            if final_fd is not None:
                os.close(final_fd)
            os.close(evidence_fd)

    def _load_transaction(
        self,
        state_fd: int,
        operation_id: str,
        *,
        expected_plan_sha256: str,
        expected_impact_sha256: str,
        expected_source_sha: str,
        allow_partial_initial_cleanup: bool = False,
        allow_empty_transaction: bool = False,
    ) -> dict[str, object] | None:
        initial_staging = self._initial_transaction_staging(operation_id)
        initial_quarantine = f"{initial_staging}.quarantine"
        initial_exists = _entry_exists_at(state_fd, initial_staging)
        quarantine_exists = _entry_exists_at(state_fd, initial_quarantine)
        if initial_exists and quarantine_exists:
            raise SuccessorError("initial journal staging and quarantine both exist")
        if initial_exists or quarantine_exists:
            current = initial_staging if initial_exists else initial_quarantine
            descriptor = _open_private_regular_at(state_fd, current)
            try:
                staged_payload, _metadata = _read_descriptor(
                    descriptor,
                    maximum_bytes=JSON_MAX_BYTES,
                    label=current,
                )
            finally:
                os.close(descriptor)
            try:
                staged_raw = json.loads(staged_payload.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                staged_raw = None
            canonical = (
                isinstance(staged_raw, dict)
                and staged_payload == _canonical_bytes(staged_raw) + b"\n"
            )
            if not canonical:
                if not allow_partial_initial_cleanup:
                    raise SuccessorError("initial journal staging is partial")
                _quarantine_unlink_at(
                    state_fd,
                    initial_staging,
                    initial_quarantine,
                )
                transaction_fd = self._transaction_directory_fd(
                    state_fd, create=False
                )
                if transaction_fd is not None:
                    try:
                        if os.listdir(transaction_fd):
                            raise SuccessorError(
                                "partial initial journal has durable descendants"
                            )
                    finally:
                        os.close(transaction_fd)
                    os.rmdir(
                        TRANSACTION_RELATIVE_DIRECTORY.name,
                        dir_fd=state_fd,
                    )
                    os.fsync(state_fd)
                self.checkpoint(
                    "source-successor-partial-initial-journal-removed"
                )
                return None
            assert isinstance(staged_raw, dict)
            staged_initial = self._validate_transaction_document(
                staged_raw, operation_id
            )
            if (
                staged_initial.get("status") != "applying"
                or staged_initial.get("phase") != "intent"
                or staged_initial.get("plan_sha256")
                != expected_plan_sha256
                or staged_initial.get("source_successor_impact_sha256")
                != expected_impact_sha256
                or staged_initial["plan"].get("source_sha")
                != expected_source_sha
            ):
                raise SuccessorError("initial journal staging is not exact intent")
            if quarantine_exists:
                raise SuccessorError("complete initial journal is quarantined")
            raise SuccessorError(
                "legacy complete initial journal staging requires reviewed recovery"
            )
        transaction_fd = self._transaction_directory_fd(
            state_fd, create=False
        )
        if transaction_fd is None:
            return None
        final_fd: int | None = None
        try:
            name = f"{operation_id}.json"
            temporary = f".{name}.tmp"
            quarantine = f"{temporary}.quarantine"
            evidence_name = _exchange_evidence_name(temporary)
            evidence_quarantine = _exchange_evidence_quarantine(temporary)
            if _entry_exists_at(
                transaction_fd, evidence_name
            ) or _entry_exists_at(transaction_fd, evidence_quarantine):
                return self._recover_exchange_evidence(
                    transaction_fd,
                    operation_id,
                    expected_plan_sha256=expected_plan_sha256,
                    expected_impact_sha256=expected_impact_sha256,
                    expected_source_sha=expected_source_sha,
                )
            quarantine_exists = _entry_exists_at(
                transaction_fd, quarantine
            )
            final_exists = _entry_exists_at(transaction_fd, name)
            temporary_exists = _entry_exists_at(transaction_fd, temporary)
            if temporary_exists and quarantine_exists:
                raise SuccessorError(
                    "journal staging and quarantine both exist"
                )
            if not final_exists and (temporary_exists or quarantine_exists):
                if (
                    allow_empty_transaction
                    and quarantine_exists
                    and not temporary_exists
                ):
                    raw_aborted, _digest, _metadata = _load_canonical_json_at(
                        transaction_fd,
                        quarantine,
                        maximum_bytes=JSON_MAX_BYTES,
                    )
                    aborted = self._validate_transaction_document(
                        raw_aborted, operation_id
                    )
                    if (
                        aborted.get("status") != "aborted"
                        or aborted.get("phase") != "aborted"
                        or aborted.get("plan_sha256")
                        != expected_plan_sha256
                        or aborted.get("source_successor_impact_sha256")
                        != expected_impact_sha256
                        or aborted["plan"].get("source_sha")
                        != expected_source_sha
                    ):
                        raise SuccessorError(
                            "abort quarantine is not exact durable authority"
                        )
                    return aborted
                raise SuccessorError(
                    "journal staging has no durable final"
                )
            if not final_exists and not temporary_exists and not quarantine_exists:
                if allow_partial_initial_cleanup:
                    os.rmdir(
                        TRANSACTION_RELATIVE_DIRECTORY.name,
                        dir_fd=state_fd,
                    )
                    os.fsync(state_fd)
                    self.checkpoint(
                        "source-successor-empty-transaction-directory-removed"
                    )
                    return None
                if allow_empty_transaction:
                    return None
                raise SuccessorError("transaction directory lacks its journal")
            final_document: dict[str, object] | None = None
            final_payload: bytes | None = None
            final_metadata: os.stat_result | None = None
            if final_exists:
                (
                    final_fd,
                    raw_final,
                    _digest,
                    final_payload,
                    final_metadata,
                ) = _open_canonical_json_at(
                    transaction_fd, name, maximum_bytes=JSON_MAX_BYTES
                )
                final_document = self._validate_transaction_document(
                    raw_final, operation_id
                )
                if (
                    final_document.get("plan_sha256")
                    != expected_plan_sha256
                    or final_document.get("source_successor_impact_sha256")
                    != expected_impact_sha256
                    or final_document["plan"].get("source_sha")
                    != expected_source_sha
                ):
                    raise SuccessorError(
                        "durable source successor confirmation differs"
                    )
            if quarantine_exists:
                assert final_document is not None
                quarantine_fd = _open_private_regular_at(
                    transaction_fd, quarantine
                )
                try:
                    quarantine_payload, _quarantine_metadata = (
                        _read_descriptor(
                            quarantine_fd,
                            maximum_bytes=JSON_MAX_BYTES,
                            label=quarantine,
                        )
                    )
                finally:
                    os.close(quarantine_fd)
                try:
                    quarantine_raw = json.loads(
                        quarantine_payload.decode("utf-8")
                    )
                except (UnicodeError, json.JSONDecodeError, RecursionError):
                    quarantine_raw = None
                if (
                    isinstance(quarantine_raw, dict)
                    and quarantine_payload
                    == _canonical_bytes(quarantine_raw) + b"\n"
                ):
                    raise SuccessorError(
                        "canonical journal quarantine lacks durable "
                        "exchange evidence"
                    )
                _quarantine_unlink_at(
                    transaction_fd,
                    temporary,
                    quarantine,
                    expected_payload=quarantine_payload,
                )
                self.checkpoint(
                    "source-successor-journal-quarantine-removed"
                )
                return final_document
            if temporary_exists:
                staged_fd = _open_private_regular_at(
                    transaction_fd, temporary
                )
                try:
                    staged_payload, _metadata = _read_descriptor(
                        staged_fd,
                        maximum_bytes=JSON_MAX_BYTES,
                        label=temporary,
                    )
                finally:
                    os.close(staged_fd)
                try:
                    raw_staged = json.loads(staged_payload.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError, RecursionError):
                    raw_staged = None
                if (
                    not isinstance(raw_staged, dict)
                    or staged_payload != _canonical_bytes(raw_staged) + b"\n"
                ):
                    if final_document is None:
                        raise SuccessorError(
                            "partial journal staging has no durable final"
                        )
                    _quarantine_unlink_at(
                        transaction_fd,
                        temporary,
                        quarantine,
                    )
                    self.checkpoint(
                        "source-successor-partial-journal-staging-removed"
                    )
                    return final_document
                raise SuccessorError(
                    "named journal staging lacks durable exchange evidence"
                )
            return final_document
        finally:
            if final_fd is not None:
                os.close(final_fd)
            os.close(transaction_fd)

    def _write_transaction(
        self,
        state_fd: int,
        document: dict[str, object],
    ) -> None:
        if not _entry_exists_at(
            state_fd, TRANSACTION_RELATIVE_DIRECTORY.name
        ):
            if (
                document.get("status") != "applying"
                or document.get("phase") != "intent"
            ):
                raise SuccessorError("initial journal is not exact intent")
            self._publish_initial_transaction(state_fd, document)
            return
        transaction_fd = self._transaction_directory_fd(
            state_fd, create=False
        )
        assert transaction_fd is not None
        current_fd: int | None = None
        try:
            name = f"{document['operation_id']}.json"
            expected_current: tuple[int, bytes, os.stat_result] | None = None
            if _entry_exists_at(transaction_fd, name):
                (
                    current_fd,
                    current_raw,
                    _digest,
                    current_payload,
                    current_metadata,
                ) = _open_canonical_json_at(
                    transaction_fd, name, maximum_bytes=JSON_MAX_BYTES
                )
                current = self._validate_transaction_document(
                    current_raw, str(document["operation_id"])
                )
                if not self._staged_transaction_follows(current, document):
                    raise SuccessorError("journal CAS transition differs")
                if current_payload == _canonical_bytes(document) + b"\n":
                    return
                expected_current = (
                    current_fd,
                    current_payload,
                    current_metadata,
                )
            elif document.get("phase") != "intent":
                raise SuccessorError("journal initial phase differs")
            _atomic_json_at(
                transaction_fd,
                name,
                document,
                expected_current=expected_current,
                checkpoint=self.checkpoint,
                before_replace_cas=lambda: self.checkpoint(
                    "source-successor-journal-before-replace-cas"
                ),
                after_exchange=lambda: self.checkpoint(
                    "source-successor-journal-exchanged"
                ),
                after_retired_quarantine=lambda: self.checkpoint(
                    "source-successor-journal-retired-quarantined"
                ),
            )
        finally:
            if current_fd is not None:
                os.close(current_fd)
            os.close(transaction_fd)

    def _assert_exclusive(
        self,
        state_fd: int,
        operation_id: str,
    ) -> None:
        allowed_state_entries = {
            TRANSACTION_RELATIVE_DIRECTORY.name,
            self._initial_transaction_staging(operation_id),
            f"{self._initial_transaction_staging(operation_id)}.quarantine",
        }
        lineage_entries = {
            name
            for name in os.listdir(state_fd)
            if name == TRANSACTION_RELATIVE_DIRECTORY.name
            or name.startswith(TRANSACTION_STAGING_PREFIX)
        }
        if not lineage_entries.issubset(allowed_state_entries):
            raise SuccessorError("another source successor operation exists")
        transaction_fd = self._transaction_directory_fd(
            state_fd, create=False
        )
        if transaction_fd is None:
            return
        try:
            temporary = f".{operation_id}.json.tmp"
            quarantine = f"{temporary}.quarantine"
            evidence_name = _exchange_evidence_name(temporary)
            evidence_quarantine = _exchange_evidence_quarantine(temporary)
            expected = {
                f"{operation_id}.json",
                temporary,
                quarantine,
                evidence_name,
                evidence_quarantine,
            }
            entries = os.listdir(transaction_fd)
            for name in entries:
                if name not in expected:
                    raise SuccessorError("another source successor operation exists")
            if temporary in entries and quarantine in entries:
                raise SuccessorError(
                    "journal staging and quarantine both exist"
                )
            if evidence_name in entries and evidence_quarantine in entries:
                raise SuccessorError(
                    "journal exchange evidence and quarantine both exist"
                )
        finally:
            os.close(transaction_fd)

    def _publication_entries(
        self,
        state_fd: int,
    ) -> list[str]:
        final = AUTHORITY_RELATIVE_PATH.name
        prefix = f".{final}.create-"
        return sorted(
            name
            for name in os.listdir(state_fd)
            if name == final or name.startswith(prefix)
        )

    def _assert_publication_absent(self, state_fd: int) -> None:
        first = self._publication_entries(state_fd)
        os.fsync(state_fd)
        second = self._publication_entries(state_fd)
        if first or second or first != second:
            raise SuccessorError("source successor publication namespace is occupied")

    def _assert_commit_namespace(
        self,
        state_fd: int,
        operation_id: str,
    ) -> None:
        final = AUTHORITY_RELATIVE_PATH.name
        staging = f".{final}.create-{operation_id}"
        quarantine = f"{staging}.quarantine"
        entries = self._publication_entries(state_fd)
        if any(name not in {final, staging, quarantine} for name in entries):
            raise SuccessorError("unowned source successor publication exists")
        if staging in entries and quarantine in entries:
            raise SuccessorError("staging and quarantine both exist")

    def _assert_completed_namespace(
        self,
        state_fd: int,
    ) -> None:
        final = AUTHORITY_RELATIVE_PATH.name
        if any(
            name.startswith(TRANSACTION_STAGING_PREFIX)
            for name in os.listdir(state_fd)
        ):
            raise SuccessorError("completed transaction has top-level residue")
        if self._publication_entries(state_fd) != [final]:
            raise SuccessorError("completed authority namespace has residue")
        descriptor = _open_private_regular_at(state_fd, final)
        os.close(descriptor)

    def _cleanup_aborted_transaction(
        self,
        state_fd: int,
        operation_id: str,
        transaction: Mapping[str, object],
    ) -> None:
        transaction_fd = self._transaction_directory_fd(
            state_fd, create=False
        )
        if transaction_fd is None:
            return
        name = f"{operation_id}.json"
        temporary = f".{name}.tmp"
        quarantine = f"{temporary}.quarantine"
        journal_fd: int | None = None
        try:
            temporary_exists = _entry_exists_at(transaction_fd, temporary)
            quarantine_exists = _entry_exists_at(transaction_fd, quarantine)
            final_exists = _entry_exists_at(transaction_fd, name)
            if temporary_exists or (
                final_exists and quarantine_exists
            ):
                raise SuccessorError("aborted journal has publication residue")
            if quarantine_exists:
                if final_exists:
                    raise SuccessorError("aborted journal quarantine differs")
                (
                    journal_fd,
                    observed,
                    _digest,
                    journal_payload,
                    _journal_metadata,
                ) = _open_canonical_json_at(
                    transaction_fd,
                    quarantine,
                    maximum_bytes=JSON_MAX_BYTES,
                )
            else:
                if not final_exists:
                    raise SuccessorError("aborted journal is missing")
                (
                    journal_fd,
                    observed,
                    _digest,
                    journal_payload,
                    journal_metadata,
                ) = _open_canonical_json_at(
                    transaction_fd, name, maximum_bytes=JSON_MAX_BYTES
                )
            if observed != transaction or observed.get("status") != "aborted":
                raise SuccessorError("aborted journal CAS differs")
            if not quarantine_exists:
                self.checkpoint(
                    "source-successor-abort-before-unlink-cas"
                )
                _quarantine_for_unlink_cas_at(
                    transaction_fd,
                    name,
                    quarantine,
                    current_fd=journal_fd,
                    current_payload=journal_payload,
                    current_metadata=journal_metadata,
                    after_quarantine=lambda: self.checkpoint(
                        "source-successor-abort-journal-quarantined"
                    ),
                )
            _quarantine_unlink_at(
                transaction_fd,
                name,
                quarantine,
                expected_payload=journal_payload,
            )
            self.checkpoint("source-successor-abort-journal-unlinked")
            if os.listdir(transaction_fd):
                raise SuccessorError("aborted transaction directory is not empty")
        finally:
            if journal_fd is not None:
                os.close(journal_fd)
            os.close(transaction_fd)
        try:
            os.rmdir(TRANSACTION_RELATIVE_DIRECTORY.name, dir_fd=state_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SuccessorError("aborted transaction directory changed") from exc
        os.fsync(state_fd)
        self.checkpoint("source-successor-abort-directory-removed")

    def _validate_durable_plan(
        self,
        transaction: Mapping[str, object],
        source_sha: str,
        operation_id: str,
        state_fd: int,
    ) -> str:
        plan = transaction.get("plan")
        if not isinstance(plan, dict):
            raise SuccessorError("durable source successor plan is missing")
        _rebuilt, production_trust = self._build_plan(
            source_sha,
            operation_id,
            live_delivery=False,
            sealed_plan=plan,
            state_fd=state_fd,
        )
        return production_trust

    def _authority(
        self,
        transaction: Mapping[str, object],
    ) -> dict[str, object]:
        plan = transaction["plan"]
        assert isinstance(plan, dict)
        predecessor = plan["predecessor"]
        marker = plan["marker"]
        assert isinstance(predecessor, dict) and isinstance(marker, dict)
        authority = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": AUTHORITY_KIND,
            "policy": POLICY,
            "operation_id": transaction["operation_id"],
            "source_sha": plan["source_sha"],
            "source_tree": plan["source_tree"],
            "predecessor_source_sha": predecessor["source_sha"],
            "predecessor_source_tree": predecessor["source_tree"],
            "predecessor_authority_sha256": predecessor[
                "authority_sha256"
            ],
            "predecessor_marker_sha256": marker["raw_sha256"],
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
            "source_successor_impact_sha256": transaction[
                "source_successor_impact_sha256"
            ],
            "files_sha256": plan["files_sha256"],
            "changed_paths": plan["changed_paths"],
            "changed_paths_sha256": plan["changed_paths_sha256"],
            "delivery_gate": plan["delivery_gate"],
            "delivery_gate_sha256": plan["delivery_gate_sha256"],
            "verifier_agreement_sha256": _canonical_digest(
                plan["verifier_agreement"]
            ),
            "production_source_trust_sha256": transaction[
                "production_source_trust_sha256"
            ],
            "production_repository_transition_sha256": plan[
                "production_repository_transition_sha256"
            ],
            "plan": plan,
            "completed_at": transaction["completed_at"],
        }
        if set(authority) != AUTHORITY_FIELDS:
            raise SuccessorError("internal source successor authority shape differs")
        return authority

    def _load_authority(self, state_fd: int) -> dict[str, object]:
        document, _digest, _metadata = _load_canonical_json_at(
            state_fd,
            AUTHORITY_RELATIVE_PATH.name,
            maximum_bytes=JSON_MAX_BYTES,
        )
        if set(document) != AUTHORITY_FIELDS:
            raise SuccessorError("published source successor authority is invalid")
        return document

    def _advance(
        self,
        state_fd: int,
        transaction: dict[str, object],
        phase: str,
        checkpoint: str,
    ) -> None:
        transaction["phase"] = phase
        self._write_transaction(state_fd, transaction)
        self.checkpoint(checkpoint)

    def apply(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_source_successor_impact_sha256: str,
    ) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_operation_id(operation_id)
        _require_digest(confirm_plan_sha256, "confirmed plan digest")
        _require_digest(
            confirm_source_successor_impact_sha256,
            "confirmed successor impact digest",
        )
        # A first invocation must match an independently reviewed zero-write
        # plan.  Recovery deliberately skips this moving remote/CI query and
        # instead consumes only its already durable sealed gate.
        # Only the final journal name is durable recovery authority. Initial
        # publication links an anonymous, fsynced inode directly at that name;
        # a crash before the link leaves at most an empty removable directory,
        # never a named payload that can suppress the live delivery-gate check.
        durable_transaction_exists = (
            self.transaction_root / f"{operation_id}.json"
        ).exists()
        preliminary: dict[str, object] | None = None
        if not durable_transaction_exists:
            preliminary = self._apply_preliminary_plan(
                source_sha, operation_id
            )
            if (
                preliminary["plan_sha256"] != confirm_plan_sha256
                or preliminary["source_successor_impact_sha256"]
                != confirm_source_successor_impact_sha256
            ):
                raise SuccessorError("source successor confirmations differ")
        with self._deployment_lock() as (
            state_fd,
            assert_guard,
            set_content_guard,
            assert_content_guard,
        ):
            self.checkpoint("source-successor-apply-lock-acquired")
            assert_guard()
            self._assert_exclusive(state_fd, operation_id)
            transaction = self._load_transaction(
                state_fd,
                operation_id,
                expected_plan_sha256=confirm_plan_sha256,
                expected_impact_sha256=(
                    confirm_source_successor_impact_sha256
                ),
                expected_source_sha=source_sha,
                allow_partial_initial_cleanup=preliminary is not None,
            )
            recovered = transaction is not None
            if transaction is None:
                if _entry_exists_at(
                    state_fd, TRANSACTION_RELATIVE_DIRECTORY.name
                ):
                    raise SuccessorError(
                        "source successor transaction namespace is preplanted"
                    )
                self._assert_publication_absent(state_fd)
                locked_plan, production_trust = self._build_plan(
                    source_sha,
                    operation_id,
                    live_delivery=True,
                    state_fd=state_fd,
                )
                locked_digest = _canonical_digest(locked_plan)
                if (
                    preliminary is None
                    or locked_plan != preliminary["plan"]
                    or locked_digest != confirm_plan_sha256
                    or locked_plan["source_successor_impact_sha256"]
                    != confirm_source_successor_impact_sha256
                ):
                    raise SuccessorError("source successor plan changed before intent")
                transaction = {
                    "schema_version": 1,
                    "status": "applying",
                    "phase": "intent",
                    "operation_id": operation_id,
                    "plan": locked_plan,
                    "plan_sha256": locked_digest,
                    "source_successor_impact_sha256": (
                        confirm_source_successor_impact_sha256
                    ),
                    "production_source_trust_sha256": production_trust,
                    "created_at": _utc_now(),
                    "completed_at": None,
                    "aborted_at": None,
                }
                self._write_transaction(state_fd, transaction)
                self.checkpoint("source-successor-intent")
            if transaction["status"] == "aborted":
                raise SuccessorError("source successor operation was aborted")
            if (
                transaction["plan_sha256"] != confirm_plan_sha256
                or transaction["source_successor_impact_sha256"]
                != confirm_source_successor_impact_sha256
                or transaction["plan"].get("source_sha") != source_sha
            ):
                raise SuccessorError("durable source successor confirmation differs")

            def activate_content_reproof() -> None:
                expected_trust = transaction[
                    "production_source_trust_sha256"
                ]

                def reproof() -> None:
                    observed_trust = self._validate_durable_plan(
                        transaction,
                        source_sha,
                        operation_id,
                        state_fd,
                    )
                    if observed_trust != expected_trust:
                        raise SuccessorError(
                            "guarded production trust changed"
                        )

                set_content_guard(reproof)
                assert_guard()

            if transaction["status"] == "completed":
                activate_content_reproof()
                self._assert_completed_namespace(state_fd)
                expected = self._authority(transaction)
                if self._load_authority(state_fd) != expected:
                    raise SuccessorError("completed source successor authority differs")
                trust_digest = self._validate_durable_plan(
                    transaction, source_sha, operation_id, state_fd
                )
                if trust_digest != transaction["production_source_trust_sha256"]:
                    raise SuccessorError("completed production trust changed")
                self._write_transaction(state_fd, transaction)
                # The completed authority is returned only while the exact
                # sealed source and production repository still re-prove.
                assert_content_guard()
                return expected
            if transaction["phase"] == "authority-commit-intent":
                activate_content_reproof()
                self._assert_commit_namespace(state_fd, operation_id)
                trust_digest = self._validate_durable_plan(
                    transaction, source_sha, operation_id, state_fd
                )
                if trust_digest != transaction["production_source_trust_sha256"]:
                    raise SuccessorError("commit production trust changed")
                if recovered:
                    self._write_transaction(state_fd, transaction)
                    self.checkpoint("source-successor-journal-resealed")
                # Boundary 1: commit intent is durable; re-prove all sealed
                # content immediately before create-once authority publication.
                assert_content_guard()
                authority = self._authority(transaction)
                _create_json_once_at(
                    state_fd,
                    AUTHORITY_RELATIVE_PATH.name,
                    authority,
                    operation_id=operation_id,
                    checkpoint=self.checkpoint,
                    before_link=assert_content_guard,
                )
                # Boundary 2: authority publication is durable; do not publish
                # a completed journal if Git content changed during create-once.
                assert_content_guard()
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_transaction(state_fd, transaction)
                self.checkpoint("source-successor-completed")
                # Boundary 3: completed journal is durable; successful return
                # still requires the exact sealed content.
                assert_content_guard()
                return authority
            if recovered:
                self._write_transaction(state_fd, transaction)
                self.checkpoint("source-successor-journal-resealed")
            self._assert_publication_absent(state_fd)
            trust_digest = self._validate_durable_plan(
                transaction, source_sha, operation_id, state_fd
            )
            if trust_digest != transaction["production_source_trust_sha256"]:
                raise SuccessorError("durable production trust changed")
            if transaction["phase"] == "intent":
                self._advance(
                    state_fd,
                    transaction,
                    "predecessor-verified",
                    "source-successor-predecessor-verified",
                )
            if transaction["phase"] == "predecessor-verified":
                self._assert_publication_absent(state_fd)
                trust_digest = self._validate_durable_plan(
                    transaction, source_sha, operation_id, state_fd
                )
                if trust_digest != transaction["production_source_trust_sha256"]:
                    raise SuccessorError("source production trust changed")
                self._advance(
                    state_fd,
                    transaction,
                    "source-verified",
                    "source-successor-source-verified",
                )
            if transaction["phase"] == "source-verified":
                self._assert_publication_absent(state_fd)
                trust_digest = self._validate_durable_plan(
                    transaction, source_sha, operation_id, state_fd
                )
                if trust_digest != transaction["production_source_trust_sha256"]:
                    raise SuccessorError("pre-commit production trust changed")
                activate_content_reproof()
                transaction["phase"] = "authority-commit-intent"
                transaction["completed_at"] = _utc_now()
                self._write_transaction(state_fd, transaction)
                self.checkpoint("source-successor-authority-commit-intent")
                # Boundary 1: commit intent is durable; re-prove all sealed
                # content immediately before create-once authority publication.
                assert_content_guard()
                authority = self._authority(transaction)
                _create_json_once_at(
                    state_fd,
                    AUTHORITY_RELATIVE_PATH.name,
                    authority,
                    operation_id=operation_id,
                    checkpoint=self.checkpoint,
                    before_link=assert_content_guard,
                )
                # Boundary 2: authority publication is durable; do not publish
                # a completed journal if Git content changed during create-once.
                assert_content_guard()
                transaction["phase"] = "completed"
                transaction["status"] = "completed"
                self._write_transaction(state_fd, transaction)
                self.checkpoint("source-successor-completed")
                # Boundary 3: completed journal is durable; successful return
                # still requires the exact sealed content.
                assert_content_guard()
                return authority
            raise SuccessorError("source successor transaction phase is invalid")

    def abort(
        self,
        *,
        source_sha: str,
        operation_id: str,
        confirm_plan_sha256: str,
        confirm_source_successor_impact_sha256: str,
    ) -> dict[str, object]:
        source_sha = _require_sha(source_sha)
        operation_id = _require_operation_id(operation_id)
        _require_digest(confirm_plan_sha256, "confirmed plan digest")
        _require_digest(
            confirm_source_successor_impact_sha256,
            "confirmed successor impact digest",
        )
        with self._deployment_lock() as (
            state_fd,
            assert_guard,
            _set_content_guard,
            _assert_content_guard,
        ):
            assert_guard()
            self._assert_exclusive(state_fd, operation_id)
            transaction = self._load_transaction(
                state_fd,
                operation_id,
                expected_plan_sha256=confirm_plan_sha256,
                expected_impact_sha256=(
                    confirm_source_successor_impact_sha256
                ),
                expected_source_sha=source_sha,
                allow_partial_initial_cleanup=True,
                allow_empty_transaction=True,
            )
            if transaction is None:
                self._assert_publication_absent(state_fd)
                transaction_fd = self._transaction_directory_fd(
                    state_fd, create=False
                )
                if transaction_fd is not None:
                    try:
                        if os.listdir(transaction_fd):
                            raise SuccessorError(
                                "abort transaction namespace differs"
                            )
                    finally:
                        os.close(transaction_fd)
                    os.rmdir(
                        TRANSACTION_RELATIVE_DIRECTORY.name,
                        dir_fd=state_fd,
                    )
                    os.fsync(state_fd)
                result = {
                    "schema_version": 1,
                    "status": "aborted",
                    "operation_id": operation_id,
                }
                assert_guard()
                return result
            if (
                transaction["plan_sha256"] != confirm_plan_sha256
                or transaction["source_successor_impact_sha256"]
                != confirm_source_successor_impact_sha256
                or transaction["plan"].get("source_sha") != source_sha
            ):
                raise SuccessorError("abort confirmation differs")
            if transaction["status"] == "aborted":
                self._assert_publication_absent(state_fd)
                self._cleanup_aborted_transaction(
                    state_fd, operation_id, transaction
                )
                assert_guard()
                return transaction
            if (
                transaction["status"] == "completed"
                or transaction["phase"]
                in {"authority-commit-intent", "completed"}
            ):
                raise SuccessorError("source successor commit is forward-only")
            self._assert_publication_absent(state_fd)
            self._validate_durable_plan(
                transaction, source_sha, operation_id, state_fd
            )
            transaction["phase"] = "aborted"
            transaction["status"] = "aborted"
            transaction["aborted_at"] = _utc_now()
            self._write_transaction(state_fd, transaction)
            self.checkpoint("source-successor-aborted")
            self._cleanup_aborted_transaction(
                state_fd, operation_id, transaction
            )
            assert_guard()
            return transaction


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish the fixed Git permission source successor authority"
    )
    parser.add_argument("action", choices=("plan", "apply", "abort"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--confirm-source-successor-impact-sha256")
    return parser


def main(arguments: list[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    publisher = SourceSuccessorPublisher()
    try:
        if parsed.action == "plan":
            if (
                parsed.confirm_plan_sha256 is not None
                or parsed.confirm_source_successor_impact_sha256 is not None
            ):
                raise SuccessorError("plan does not accept apply confirmations")
            result = publisher.plan(
                source_sha=parsed.sha,
                operation_id=parsed.operation_id,
            )
        else:
            if (
                parsed.confirm_plan_sha256 is None
                or parsed.confirm_source_successor_impact_sha256 is None
            ):
                raise SuccessorError("apply/abort require both confirmations")
            method = publisher.apply if parsed.action == "apply" else publisher.abort
            result = method(
                source_sha=parsed.sha,
                operation_id=parsed.operation_id,
                confirm_plan_sha256=parsed.confirm_plan_sha256,
                confirm_source_successor_impact_sha256=(
                    parsed.confirm_source_successor_impact_sha256
                ),
            )
    except SuccessorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
