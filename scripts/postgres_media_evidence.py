#!/usr/bin/python3 -I
"""Build complete, source-CAS PostgreSQL external-media evidence.

This module is deliberately fail closed.  It discovers the entire reviewed
Docker/backup boundary, copies dormant PostgreSQL media through read-only
mounts, restores backups into a private PostgreSQL 16 scratch cluster, and
emits fresh evidence.  It never starts PostgreSQL against a source volume and
never writes to a source backup or bind mount.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence


DOCKER = "/usr/bin/docker"
PSQL = "/usr/bin/psql"
POSTGRES_MAJOR = 16
POSTGRES_UID = 70
POSTGRES_GID = 70
ADJACENT_POSTGRES_MAJOR_MIN = 9
ADJACENT_POSTGRES_MAJOR_MAX = 18
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_DATABASE_JSON_BYTES = 64 * 1024 * 1024
DEFAULT_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
DEFAULT_REGISTRY = DEFAULT_RUNTIME_ROOT / "config/postgres-media-registry.json"
DEFAULT_SERVICE_FILE = DEFAULT_RUNTIME_ROOT / "config/pg_service.conf"
DEFAULT_EVIDENCE_ROOT = DEFAULT_RUNTIME_ROOT / "audit/postgres-media"
FORBIDDEN_BACKUP_ROOT = DEFAULT_RUNTIME_ROOT / "backups"
APPROVED_BACKUP_ROOTS = (
    Path("/data/lzq/gith/nexpoly/backups"),
    Path(
        "/data/lzq/gith/nexpoly-runtime/legacy-takeover/"
        "preserved-postgres-backups"
    ),
    Path("/data/lzq/recovery/nexpoly-postgres-media"),
    Path(
        "/data/lzq/recovery/"
        "nexpoly-pre-merge-20260717T090623Z/dev-0009-quarantine"
    ),
)
APPROVED_BACKUP_FORMATS = (
    ("postgres-custom-v1", (".backup", ".dump")),
    ("postgres-tar-v1", (".tar",)),
)
DISCOVERY_METHODS = (
    "all-docker-containers-and-volumes-v1",
    "container-pgdata-mounts-v1",
    "all-volume-pg-version-readonly-recursive-probe-v1",
    "fixed-private-backup-roots-v1",
)
ACTIVE_CONTAINER_STATES = frozenset(
    {"created", "running", "paused", "restarting", "removing"}
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ROLE_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,62}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MEDIA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,1023}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{64}$")
PG_SYSTEM_ID_RE = re.compile(r"^[0-9]{1,20}$")
MIGRATION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
RFC3339_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
SCRATCH_OPERATION_RE = re.compile(r"^audit-[0-9a-f]{64}$")
SCRATCH_RESOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SCRATCH_JOURNAL_TEMP_RE = re.compile(
    r"^\.(audit-[0-9a-f]{64})\.json\.tmp-[0-9a-f]{32}$"
)
SCRATCH_SEQUENCE_RE = re.compile(
    r"^(audit-[0-9a-f]{64})\.seq-([0-9]{20})-([0-9a-f]{64})\.json$"
)
SCRATCH_SCHEMA_VERSION = 1
SCRATCH_JOURNAL_ROOT_NAME = ".scratch-operations"
SCRATCH_WORKSPACE_ROOT_NAME = ".scratch-workspaces"
SCRATCH_LOCK_NAME = "LOCK"
SCRATCH_WORKSPACE_OWNER_NAME = ".nexpoly-audit-owner.json"
SCRATCH_LABEL_PREFIX = "io.nexpoly.audit."
SCRATCH_TERMINAL_PHASES = frozenset(
    {"completed", "aborted", "recovered"}
)
SCRATCH_PHASES = frozenset(
    {
        "starting",
        "running",
        "blocked-foreign-identity",
        "blocked-recovery-error",
        "awaiting-create-resolution",
        *SCRATCH_TERMINAL_PHASES,
    }
)

LEGACY_0005_ALIAS_VERSION = "0005_polytao_jobs"
LEGACY_0005_ALIAS_CHECKSUM = (
    "b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5"
)
KNOWN_DIRTY_0009_CHECKSUM = (
    "79a6956fc934794d61bc003f02a6b5280e9e8bd77a217b61a28d3dbdb8b7be0b"
)
CANONICAL_0013_CHECKSUM = (
    "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
)
SUPERSEDED_0013_CHECKSUM = (
    "a60cbf66c70981ba6eb7cf545b5bd89df96fa399fff48ef7f6f21d3682c64cab"
)
CANONICAL_MIGRATION_LEDGER = (
    (
        "0001_app_data_governance",
        "d5fc9f3d063f1cba476834f3530519b7970cd54f3c3711d05aba1f1cb2fd34f9",
    ),
    (
        "0002_lab_identity_defaults",
        "580ed6dc7c34970aabd662bc47765e9d02446c28aea1c4fa8fb2a99f05b1ac2f",
    ),
    (
        "0003_runtime_postgres_cutover",
        "0888ac9abd1b6b642f0addd42274b5408981a26c27f1140b7b656ff34ad73ce3",
    ),
    (
        "0004_monomer_md_jobs",
        "b3ad64728f399f42b2bf9edb47ad035ac70f09fce6ced48e7b422ea74d5a7e8e",
    ),
    (
        "0005_byteff2_formal_monomer_md",
        "c9ec808c50915b82a696ab482ed676c62bc75f00a9af21baf9e7f66b185bacb5",
    ),
    (
        "0006_property_filter_records",
        "57b103dc656334cf5e52bdc9512576a303ae0044ec5fb64eb7cba802021eceaa",
    ),
    (
        "0007_polytao_jobs",
        LEGACY_0005_ALIAS_CHECKSUM,
    ),
    (
        "0008_polytao_backend_runtime",
        "d0d8b2187aad8657269600873d3d2630e30c7d72da2f6662e18ab22031deff90",
    ),
    (
        "0009_monomer_md_job_leases",
        "ef1757a81976f351459e8257bd492aa6267cbf507c4ea85506fefa2d465d2db8",
    ),
    (
        "0010_deployment_control",
        "f7fad29bcf1da1c6903a688a7312a67216bc11002ac558209ff56e25f69cf7cd",
    ),
    (
        "0011_monomer_md_demo_steps",
        "9a03f38329199aa707818c2099b9811d46366bafe0ddaeb39ae53bc20d0a68ed",
    ),
    (
        "0012_drop_polytao_jobs",
        "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728",
    ),
    (
        "0013_monomer_dft_jobs",
        CANONICAL_0013_CHECKSUM,
    ),
)


class MediaEvidenceError(RuntimeError):
    """External-media discovery or evidence construction failed closed."""


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    backup_roots: tuple[Path, ...] = APPROVED_BACKUP_ROOTS
    backup_formats: tuple[tuple[str, tuple[str, ...]], ...] = (
        APPROVED_BACKUP_FORMATS
    )
    discovery_methods: tuple[str, ...] = DISCOVERY_METHODS

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "docker_scope": "all-local-containers-volumes-and-pgdata-binds",
            "backup_roots": [str(value) for value in self.backup_roots],
            "backup_formats": [
                {"name": name, "suffixes": list(suffixes)}
                for name, suffixes in self.backup_formats
            ],
            "discovery_methods": list(self.discovery_methods),
        }


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    media_id: str
    kind: str
    database: str
    database_user: str
    disposition: str
    audit_method: str
    pg_service: str | None
    classification: str = "nexpoly-db"
    source_postgres_major: int | None = POSTGRES_MAJOR
    databases: tuple[dict[str, object], ...] = ()

    def document(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "kind": self.kind,
            "database": self.database,
            "database_user": self.database_user,
            "disposition": self.disposition,
            "audit_method": self.audit_method,
            "pg_service": self.pg_service,
            "classification": self.classification,
            "source_postgres_major": self.source_postgres_major,
            "databases": [dict(value) for value in self.databases],
        }


@dataclass(frozen=True, slots=True)
class Registry:
    payload: bytes
    digest: str
    audit_image: str
    auditor_sha256: str
    service_file_sha256: str
    descriptors: tuple[MediaDescriptor, ...]
    required_online_databases: tuple[dict[str, str], ...]
    boundary: dict[str, object]
    postgres_uid: int = POSTGRES_UID
    postgres_gid: int = POSTGRES_GID


@dataclass(frozen=True, slots=True)
class DiscoveredMedia:
    media_id: str
    kind: str
    locator: str
    data_subpath: str
    attached: tuple[dict[str, object], ...]
    backup_format: str | None = None
    signature: str = "postgres"
    postgres_major: int | None = POSTGRES_MAJOR

    def document(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "kind": self.kind,
            "locator": self.locator,
            "data_subpath": self.data_subpath,
            "attached": [dict(value) for value in self.attached],
            "backup_format": self.backup_format,
            "signature": self.signature,
            "postgres_major": self.postgres_major,
        }


@dataclass(frozen=True, slots=True)
class Discovery:
    media: Mapping[str, DiscoveredMedia]
    docker_inventory_sha256: str
    backup_inventory_sha256: str
    scanned_volume_names: tuple[str, ...]
    scanned_container_ids: tuple[str, ...]


@dataclass(slots=True)
class CommandRunner:
    """Small injectable subprocess boundary used by hostile and integration tests."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: int = 120,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if not arguments or not Path(arguments[0]).is_absolute():
            raise MediaEvidenceError("command binary must be an absolute path")
        completed = subprocess.run(
            list(arguments),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else fixed_environment(),
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", "replace")[-4096:]
            raise MediaEvidenceError(
                f"command failed ({arguments[0]}): {stderr.strip()}"
            )
        return completed


def _scratch_journal_root(evidence_root: Path) -> Path:
    return evidence_root / SCRATCH_JOURNAL_ROOT_NAME


def _scratch_workspace_root(evidence_root: Path) -> Path:
    return evidence_root / SCRATCH_WORKSPACE_ROOT_NAME


def _directory_identity(path: Path) -> dict[str, int]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MediaEvidenceError(f"scratch directory is unsafe: {path}")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    """Atomically publish one private directory without replacing a collision."""

    if (
        source.parent != target.parent
        or source.name in {"", ".", ".."}
        or target.name in {"", ".", ".."}
        or "/" in source.name
        or "/" in target.name
    ):
        raise MediaEvidenceError(
            "scratch workspace rename does not share one safe parent"
        )
    directory = _open_directory_chain(
        source.parent,
        private_from=source.parent,
    )
    try:
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(library, "renameat2", None)
        if renameat2 is None:
            raise MediaEvidenceError(
                "atomic no-replace directory rename is unavailable"
            )
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            directory,
            os.fsencode(source.name),
            directory,
            os.fsencode(target.name),
            1,  # RENAME_NOREPLACE
        )
        if result != 0:
            error = ctypes.get_errno()
            if error in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(error, os.strerror(error), target)
            raise OSError(error, os.strerror(error), target)
        os.fsync(directory)
    finally:
        os.close(directory)


def _open_private_child_directory(root: Path, name: str) -> Path:
    if not name or "/" in name or "\x00" in name:
        raise MediaEvidenceError("private child directory name is invalid")
    child = root / name
    descriptor = _open_directory_chain(
        child,
        private_from=root,
        create_leaf=True,
    )
    os.close(descriptor)
    return child


def _scratch_journal_digest(value: Mapping[str, object]) -> str:
    unsealed = {
        key: item for key, item in value.items() if key != "journal_sha256"
    }
    return sha256_bytes(canonical_json_bytes(unsealed))


def _seal_scratch_journal(value: Mapping[str, object]) -> dict[str, object]:
    unsealed = {
        key: item for key, item in value.items() if key != "journal_sha256"
    }
    return {
        **unsealed,
        "journal_sha256": sha256_bytes(canonical_json_bytes(unsealed)),
    }


def _validate_scratch_journal(
    value: object,
    *,
    operation_id: str | None = None,
    evidence_root: Path | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MediaEvidenceError("scratch journal is not an object")
    required = {
        "schema_version",
        "operation_id",
        "owner_token",
        "uid",
        "authority",
        "sequence",
        "previous_state_sha256",
        "phase",
        "workspace",
        "resources",
        "result",
        "blocked_reason",
        "journal_sha256",
    }
    if set(value) != required:
        raise MediaEvidenceError("scratch journal has an invalid shape")
    current_id = value.get("operation_id")
    owner_token = value.get("owner_token")
    sequence = value.get("sequence")
    previous = value.get("previous_state_sha256")
    digest = value.get("journal_sha256")
    if (
        value.get("schema_version") != SCRATCH_SCHEMA_VERSION
        or not isinstance(current_id, str)
        or SCRATCH_OPERATION_RE.fullmatch(current_id) is None
        or operation_id is not None
        and current_id != operation_id
        or not isinstance(owner_token, str)
        or re.fullmatch(r"[0-9a-f]{64}", owner_token) is None
        or value.get("uid") != os.geteuid()
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or sequence == 0
        and previous is not None
        or sequence > 0
        and (
            not isinstance(previous, str)
            or DIGEST_RE.fullmatch(previous) is None
        )
        or not isinstance(digest, str)
        or DIGEST_RE.fullmatch(digest) is None
        or digest != _scratch_journal_digest(value)
        or value.get("phase") not in SCRATCH_PHASES
        or value.get("blocked_reason") is not None
        and not isinstance(value["blocked_reason"], str)
        or not isinstance(value.get("result"), (dict, type(None)))
    ):
        raise MediaEvidenceError("scratch journal identity or seal is invalid")
    authority = value.get("authority")
    if (
        not isinstance(authority, dict)
        or set(authority)
        != {
            "registry_sha256",
            "service_file_sha256",
            "auditor_sha256",
            "postgres_image",
            "postgres_image_id",
        }
        or any(
            not isinstance(authority.get(key), str)
            or DIGEST_RE.fullmatch(str(authority[key])) is None
            for key in (
                "registry_sha256",
                "service_file_sha256",
                "auditor_sha256",
                "postgres_image_id",
            )
        )
        or not isinstance(authority.get("postgres_image"), str)
        or IMAGE_RE.fullmatch(str(authority["postgres_image"])) is None
    ):
        raise MediaEvidenceError("scratch journal authority is invalid")
    workspace = value.get("workspace")
    if (
        not isinstance(workspace, dict)
        or set(workspace)
        != {"path", "state", "identity", "owner_marker_sha256"}
        or workspace.get("state")
        not in {
            "create-intent",
            "owner-marker-create-intent",
            "created",
            "remove-intent",
            "absent",
            "blocked-foreign",
        }
        or not isinstance(workspace.get("path"), str)
        or not isinstance(workspace.get("identity"), (dict, type(None)))
        or workspace.get("owner_marker_sha256") is not None
        and (
            not isinstance(workspace["owner_marker_sha256"], str)
            or DIGEST_RE.fullmatch(workspace["owner_marker_sha256"]) is None
        )
    ):
        raise MediaEvidenceError("scratch journal workspace is invalid")
    if evidence_root is not None:
        owner_suffix = hashlib.sha256(
            str(owner_token).encode("ascii")
        ).hexdigest()[:16]
        expected_workspace = (
            _scratch_workspace_root(evidence_root)
            / f"{current_id}-{owner_suffix}"
        )
        if workspace["path"] != str(expected_workspace):
            raise MediaEvidenceError("scratch journal workspace escapes its root")
    workspace_identity = workspace.get("identity")
    if workspace_identity is not None and (
        not isinstance(workspace_identity, dict)
        or set(workspace_identity) != {"device", "inode", "uid", "mode"}
        or any(
            isinstance(workspace_identity.get(key), bool)
            or not isinstance(workspace_identity.get(key), int)
            or int(workspace_identity[key]) < 0
            for key in ("device", "inode", "uid", "mode")
        )
    ):
        raise MediaEvidenceError("scratch workspace identity is invalid")
    if (
        workspace["state"] in {
            "owner-marker-create-intent",
            "created",
            "remove-intent",
        }
        and workspace_identity is None
        or workspace["state"] in {"created", "remove-intent"}
        and workspace["owner_marker_sha256"] is None
        or workspace["state"] == "create-intent"
        and (
            workspace_identity is not None
            or workspace["owner_marker_sha256"] is not None
        )
    ):
        raise MediaEvidenceError("scratch workspace lifecycle is invalid")
    resources = value.get("resources")
    if not isinstance(resources, list):
        raise MediaEvidenceError("scratch journal resources are invalid")
    keys: set[str] = set()
    names: set[str] = set()
    for resource in resources:
        if (
            not isinstance(resource, dict)
            or set(resource)
            != {
                "resource_key",
                "kind",
                "name",
                "labels",
                "dependencies",
                "state",
                "spec",
                "container_id",
                "inspect_sha256",
            }
        ):
            raise MediaEvidenceError("scratch resource has an invalid shape")
        key = resource.get("resource_key")
        name = resource.get("name")
        labels = resource.get("labels")
        dependencies = resource.get("dependencies")
        expected_name = "nexpoly-audit-" + hashlib.sha256(
            (
                f"{current_id}\0{owner_token}\0{key}"
                if isinstance(key, str)
                else ""
            ).encode("utf-8")
        ).hexdigest()[:40]
        mandatory_labels = (
            {
                "io.nexpoly.audit": "true",
                "io.nexpoly.audit.schema": str(SCRATCH_SCHEMA_VERSION),
                "io.nexpoly.audit.operation": str(current_id),
                "io.nexpoly.audit.owner": sha256_bytes(
                    str(owner_token).encode("ascii")
                ),
                "io.nexpoly.audit.resource": str(key),
                "io.nexpoly.audit.registry": str(
                    authority["registry_sha256"]
                ),
            }
            if isinstance(labels, dict)
            else {}
        )
        if (
            not isinstance(key, str)
            or SCRATCH_RESOURCE_RE.fullmatch(key) is None
            or key in keys
            or not isinstance(name, str)
            or VOLUME_RE.fullmatch(name) is None
            or name != expected_name
            or name in names
            or resource.get("kind") not in {"container", "volume"}
            or resource.get("state")
            not in {
                "create-intent",
                "create-ambiguous",
                "create-absent-confirmation",
                "created",
                "remove-intent",
                "absent",
            }
            or not isinstance(labels, dict)
            or any(
                not isinstance(label, str) or not isinstance(item, str)
                for label, item in labels.items()
            )
            or any(
                labels.get(label) != item
                for label, item in mandatory_labels.items()
            )
            or any(
                label.startswith("io.nexpoly.audit")
                and label not in mandatory_labels
                for label in labels
            )
            or not isinstance(dependencies, list)
            or any(
                not isinstance(item, str)
                or SCRATCH_RESOURCE_RE.fullmatch(item) is None
                for item in dependencies
            )
            or not isinstance(resource.get("spec"), dict)
            or resource.get("container_id") is not None
            and (
                not isinstance(resource["container_id"], str)
                or CONTAINER_RE.fullmatch(resource["container_id"]) is None
            )
            or resource.get("inspect_sha256") is not None
            and (
                not isinstance(resource["inspect_sha256"], str)
                or DIGEST_RE.fullmatch(resource["inspect_sha256"]) is None
            )
        ):
            raise MediaEvidenceError("scratch resource identity is invalid")
        if (
            resource["kind"] == "volume"
            and resource["container_id"] is not None
            or resource["state"] in {"created", "remove-intent"}
            and resource["inspect_sha256"] is None
            or resource["kind"] == "container"
            and resource["state"] in {"created", "remove-intent"}
            and resource["container_id"] is None
        ):
            raise MediaEvidenceError("scratch resource lifecycle is invalid")
        spec = resource["spec"]
        if resource["kind"] == "volume":
            if spec != {
                "driver": "local",
                "scope": "local",
                "options": None,
            }:
                raise MediaEvidenceError("scratch volume spec is invalid")
        else:
            required_spec = {
                "postgres_image",
                "postgres_image_id",
                "mounts",
                "network",
                "read_only_rootfs",
                "detached",
                "command",
                "entrypoint",
                "environment",
                "tmpfs",
                "arguments_sha256",
            }
            mounts = spec.get("mounts")
            tmpfs = spec.get("tmpfs")
            if (
                set(spec) != required_spec
                or spec.get("postgres_image")
                != authority["postgres_image"]
                or spec.get("postgres_image_id")
                != authority["postgres_image_id"]
                or spec.get("network") != "none"
                or not isinstance(spec.get("read_only_rootfs"), bool)
                or not isinstance(spec.get("detached"), bool)
                or not isinstance(spec.get("command"), list)
                or any(
                    not isinstance(item, str)
                    for item in spec.get("command", [])
                )
                or not isinstance(
                    spec.get("entrypoint"),
                    (str, type(None)),
                )
                or not isinstance(spec.get("environment"), list)
                or any(
                    not isinstance(item, str) or "=" not in item
                    for item in spec.get("environment", [])
                )
                or not isinstance(tmpfs, dict)
                or any(
                    not isinstance(destination, str)
                    or not PurePosixPath(destination).is_absolute()
                    or not isinstance(options, str)
                    or not options
                    for destination, options in (
                        tmpfs.items() if isinstance(tmpfs, dict) else ()
                    )
                )
                or not isinstance(spec.get("arguments_sha256"), str)
                or DIGEST_RE.fullmatch(spec["arguments_sha256"]) is None
                or not isinstance(mounts, list)
                or mounts != sorted(mounts, key=canonical_json_bytes)
            ):
                raise MediaEvidenceError("scratch container spec is invalid")
            destinations: set[str] = set()
            tmpfs_destinations: set[str] = set()
            for mount in mounts:
                if (
                    not isinstance(mount, dict)
                    or set(mount)
                    != {
                        "kind",
                        "source",
                        "destination",
                        "read_only",
                    }
                    or mount.get("kind")
                    not in {"volume", "bind", "tmpfs"}
                    or not isinstance(mount.get("destination"), str)
                    or not PurePosixPath(
                        mount["destination"]
                    ).is_absolute()
                    or mount["destination"] in destinations
                    or not isinstance(mount.get("read_only"), bool)
                    or mount["kind"] == "tmpfs"
                    and mount.get("source") is not None
                    or mount["kind"] != "tmpfs"
                    and not isinstance(mount.get("source"), str)
                ):
                    raise MediaEvidenceError(
                        "scratch container mount spec is invalid"
                    )
                destinations.add(mount["destination"])
                if mount["kind"] == "tmpfs":
                    tmpfs_destinations.add(mount["destination"])
            if tmpfs_destinations != set(tmpfs):
                raise MediaEvidenceError(
                    "scratch container tmpfs spec is inconsistent"
                )
        keys.add(key)
        names.add(name)
    if any(
        dependency not in keys
        for resource in resources
        for dependency in resource["dependencies"]
    ):
        raise MediaEvidenceError("scratch resource dependency is unknown")
    return value


def _write_private_replace(path: Path, payload: bytes, *, root: Path) -> None:
    if path.parent != root:
        raise MediaEvidenceError("mutable private file escapes its root")
    directory = _open_directory_chain(root, private_from=root)
    temporary = f".{path.name}.tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _load_scratch_document(
    path: Path,
    *,
    evidence_root: Path,
    operation_id: str,
) -> dict[str, object]:
    descriptor = open_private_regular(
        path,
        root=_scratch_journal_root(evidence_root),
    )
    try:
        payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError("scratch journal is not JSON") from exc
    if payload != canonical_json_bytes(value) + b"\n":
        raise MediaEvidenceError("scratch journal is not canonical JSON")
    return _validate_scratch_journal(
        value,
        operation_id=operation_id,
        evidence_root=evidence_root,
    )


def _scratch_sequence_name(journal: Mapping[str, object]) -> str:
    return (
        f"{journal['operation_id']}.seq-"
        f"{int(journal['sequence']):020d}-"
        f"{str(journal['journal_sha256']).removeprefix('sha256:')}.json"
    )


def _load_scratch_journal(path: Path, *, evidence_root: Path) -> dict[str, object]:
    operation_id = path.name.removesuffix(".json")
    if (
        path.suffix != ".json"
        or SCRATCH_OPERATION_RE.fullmatch(operation_id) is None
    ):
        raise MediaEvidenceError("scratch journal name is invalid")
    journal_root = _scratch_journal_root(evidence_root)
    sequence_names = [
        name
        for name in _scratch_journal_entries(evidence_root)
        if (
            (match := SCRATCH_SEQUENCE_RE.fullmatch(name)) is not None
            and match.group(1) == operation_id
        )
    ]
    if not sequence_names:
        return _load_scratch_document(
            path,
            evidence_root=evidence_root,
            operation_id=operation_id,
        )
    sequence: list[dict[str, object]] = []
    for name in sorted(sequence_names):
        match = SCRATCH_SEQUENCE_RE.fullmatch(name)
        if match is None:
            raise AssertionError(name)
        journal = _load_scratch_document(
            journal_root / name,
            evidence_root=evidence_root,
            operation_id=operation_id,
        )
        if (
            int(match.group(2)) != journal["sequence"]
            or match.group(3)
            != str(journal["journal_sha256"]).removeprefix("sha256:")
            or name != _scratch_sequence_name(journal)
        ):
            raise MediaEvidenceError(
                "scratch immutable sequence name differs from its journal"
            )
        sequence.append(journal)
    if [int(value["sequence"]) for value in sequence] != list(
        range(len(sequence))
    ):
        raise MediaEvidenceError(
            "scratch immutable journal sequence is not a complete genesis chain"
        )
    immutable_fields = (
        "schema_version",
        "operation_id",
        "owner_token",
        "uid",
        "authority",
    )
    genesis = sequence[0]
    workspace_path = genesis["workspace"]["path"]  # type: ignore[index]
    for index, journal in enumerate(sequence):
        if (
            any(journal[field] != genesis[field] for field in immutable_fields)
            or journal["workspace"]["path"] != workspace_path  # type: ignore[index]
            or (
                index == 0
                and journal["previous_state_sha256"] is not None
            )
            or (
                index > 0
                and journal["previous_state_sha256"]
                != sequence[index - 1]["journal_sha256"]
            )
        ):
            raise MediaEvidenceError(
                "scratch immutable journal genesis chain is invalid"
            )
    try:
        head = _load_scratch_document(
            path,
            evidence_root=evidence_root,
            operation_id=operation_id,
        )
    except FileNotFoundError:
        head = sequence[-1]
    if head["journal_sha256"] not in {
        value["journal_sha256"] for value in sequence
    }:
        raise MediaEvidenceError(
            "scratch mutable HEAD is not anchored in its immutable chain"
        )
    return sequence[-1]


@dataclass(slots=True)
class ScratchLock:
    evidence_root: Path
    create: bool = True
    descriptor: int | None = None

    def __enter__(self) -> ScratchLock:
        journal_root = _scratch_journal_root(self.evidence_root)
        if self.create:
            _open_private_child_directory(
                self.evidence_root,
                SCRATCH_JOURNAL_ROOT_NAME,
            )
        elif not journal_root.exists():
            raise MediaEvidenceError("scratch journal root does not exist")
        directory = _open_directory_chain(
            journal_root,
            private_from=journal_root,
        )
        try:
            flags = (
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if self.create:
                flags |= os.O_CREAT
            self.descriptor = os.open(
                SCRATCH_LOCK_NAME,
                flags,
                0o600,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
        metadata = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            os.close(self.descriptor)
            self.descriptor = None
            raise MediaEvidenceError("scratch operation lock is unsafe")
        try:
            fcntl.flock(
                self.descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            os.close(self.descriptor)
            self.descriptor = None
            raise MediaEvidenceError(
                "another scratch operation holds the private lock"
            ) from exc
        return self

    def __exit__(self, *_args: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


def _optional_docker_inspect(
    runner: CommandRunner,
    kind: str,
    identity: str,
) -> dict[str, object] | None:
    completed = runner.run(
        [DOCKER, kind, "inspect", "--", identity],
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        if b"no such" not in completed.stderr.lower():
            raise MediaEvidenceError(
                f"Docker {kind} inspect failed while resolving scratch state"
            )
        return None
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError(
            f"Docker {kind} inspect returned invalid JSON"
        ) from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise MediaEvidenceError(f"Docker {kind} inspect identity is malformed")
    return value[0]


def _scratch_audit_labels(
    *,
    operation_id: str,
    owner_token: str,
    resource_key: str,
    registry_sha256: str,
    source_media_id: str | None,
) -> dict[str, str]:
    values = {
        "io.nexpoly.audit": "true",
        "io.nexpoly.audit.schema": str(SCRATCH_SCHEMA_VERSION),
        "io.nexpoly.audit.operation": operation_id,
        "io.nexpoly.audit.owner": sha256_bytes(owner_token.encode("ascii")),
        "io.nexpoly.audit.resource": resource_key,
        "io.nexpoly.audit.registry": registry_sha256,
    }
    if source_media_id is not None:
        values["io.nexpoly.source"] = sha256_bytes(
            source_media_id.encode("utf-8")
        )
    return values


def _labels_match(
    actual: object,
    expected: Mapping[str, str],
) -> bool:
    if not isinstance(actual, dict):
        return False
    if any(actual.get(key) != value for key, value in expected.items()):
        return False
    return all(
        not key.startswith("io.nexpoly.audit")
        or key in expected
        for key in actual
        if isinstance(key, str)
    )


def _audit_labeled_operation_ids(runner: CommandRunner) -> set[str]:
    """Enumerate every local Docker object claiming scratch-audit ownership."""

    operations: set[str] = set()

    def record(labels: object) -> None:
        if not isinstance(labels, dict) or labels.get(
            "io.nexpoly.audit"
        ) != "true":
            return
        operation = labels.get("io.nexpoly.audit.operation")
        owner = labels.get("io.nexpoly.audit.owner")
        resource = labels.get("io.nexpoly.audit.resource")
        registry = labels.get("io.nexpoly.audit.registry")
        if (
            not isinstance(operation, str)
            or SCRATCH_OPERATION_RE.fullmatch(operation) is None
            or not isinstance(owner, str)
            or DIGEST_RE.fullmatch(owner) is None
            or not isinstance(resource, str)
            or SCRATCH_RESOURCE_RE.fullmatch(resource) is None
            or not isinstance(registry, str)
            or DIGEST_RE.fullmatch(registry) is None
        ):
            raise MediaEvidenceError(
                "Docker object has malformed scratch-audit ownership labels"
            )
        operations.add(operation)

    volume_names = [
        value
        for value in runner.run(
            [DOCKER, "volume", "ls", "--format", "{{.Name}}"],
            timeout=60,
        )
        .stdout.decode("utf-8", "strict")
        .splitlines()
        if value
    ]
    if any(VOLUME_RE.fullmatch(value) is None for value in volume_names):
        raise MediaEvidenceError(
            "Docker returned malformed volume names during global audit fence"
        )
    for name in sorted(set(volume_names)):
        value = _optional_docker_inspect(runner, "volume", name)
        if value is None:
            raise MediaEvidenceError(
                "Docker volume inventory changed during global audit fence"
            )
        record(value.get("Labels"))
    identifiers = [
        value
        for value in runner.run(
            [DOCKER, "ps", "-aq", "--no-trunc"],
            timeout=60,
        )
        .stdout.decode("ascii", "strict")
        .splitlines()
        if value
    ]
    if any(CONTAINER_RE.fullmatch(value) is None for value in identifiers):
        raise MediaEvidenceError(
            "Docker returned malformed container IDs during global audit fence"
        )
    for identifier in sorted(set(identifiers)):
        value = _optional_docker_inspect(
            runner,
            "container",
            identifier,
        )
        if value is None:
            raise MediaEvidenceError(
                "Docker container inventory changed during global audit fence"
            )
        config = value.get("Config")
        record(config.get("Labels") if isinstance(config, dict) else None)
    return operations


def _container_mount_projection(value: Mapping[str, object]) -> list[dict[str, object]]:
    mounts = value.get("Mounts")
    if not isinstance(mounts, list):
        raise MediaEvidenceError("scratch container mounts are invalid")
    projected: list[dict[str, object]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            raise MediaEvidenceError("scratch container mount is invalid")
        kind = mount.get("Type")
        destination = mount.get("Destination")
        if kind not in {"volume", "bind", "tmpfs"} or not isinstance(
            destination, str
        ):
            raise MediaEvidenceError("scratch container mount identity is invalid")
        projected.append(
            {
                "kind": kind,
                "source": (
                    mount.get("Name")
                    if kind == "volume"
                    else mount.get("Source")
                    if kind == "bind"
                    else None
                ),
                "destination": destination,
                "read_only": mount.get("RW") is False,
            }
        )
    host = value.get("HostConfig")
    if not isinstance(host, dict):
        raise MediaEvidenceError("scratch container host config is invalid")
    tmpfs = host.get("Tmpfs")
    if tmpfs is None:
        tmpfs = {}
    if (
        not isinstance(tmpfs, dict)
        or any(
            not isinstance(destination, str)
            or not isinstance(options, str)
            for destination, options in tmpfs.items()
        )
    ):
        raise MediaEvidenceError("scratch container tmpfs config is invalid")
    existing_tmpfs = {
        str(value["destination"])
        for value in projected
        if value["kind"] == "tmpfs"
    }
    for destination in tmpfs:
        if destination not in existing_tmpfs:
            projected.append(
                {
                    "kind": "tmpfs",
                    "source": None,
                    "destination": destination,
                    "read_only": False,
                }
            )
    return sorted(projected, key=canonical_json_bytes)


def _container_immutable_identity(value: Mapping[str, object]) -> dict[str, object]:
    config = value.get("Config")
    host = value.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise MediaEvidenceError("scratch container config is invalid")
    return {
        "id": value.get("Id"),
        "name": value.get("Name"),
        "image_id": value.get("Image"),
        "created": value.get("Created"),
        "path": value.get("Path"),
        "args": value.get("Args"),
        "config_image": config.get("Image"),
        "labels": config.get("Labels"),
        "environment": config.get("Env"),
        "entrypoint": config.get("Entrypoint"),
        "command": config.get("Cmd"),
        "network_mode": host.get("NetworkMode"),
        "read_only_rootfs": host.get("ReadonlyRootfs"),
        "privileged": host.get("Privileged"),
        "restart_policy": host.get("RestartPolicy"),
        "port_bindings": host.get("PortBindings"),
        "devices": host.get("Devices"),
        "tmpfs": host.get("Tmpfs"),
        "mounts": _container_mount_projection(value),
    }


@dataclass(slots=True)
class ScratchOperation:
    evidence_root: Path
    runner: CommandRunner
    journal: dict[str, object]

    @property
    def operation_id(self) -> str:
        return str(self.journal["operation_id"])

    @property
    def owner_token(self) -> str:
        return str(self.journal["owner_token"])

    @property
    def authority(self) -> dict[str, str]:
        return dict(self.journal["authority"])  # type: ignore[arg-type]

    @property
    def workspace(self) -> Path:
        return Path(str(self.journal["workspace"]["path"]))  # type: ignore[index]

    @property
    def journal_path(self) -> Path:
        return (
            _scratch_journal_root(self.evidence_root)
            / f"{self.operation_id}.json"
        )

    @classmethod
    def begin(
        cls,
        evidence_root: Path,
        *,
        runner: CommandRunner,
        authority: Mapping[str, str],
    ) -> ScratchOperation:
        _open_private_child_directory(
            evidence_root,
            SCRATCH_JOURNAL_ROOT_NAME,
        )
        workspace_root = _open_private_child_directory(
            evidence_root,
            SCRATCH_WORKSPACE_ROOT_NAME,
        )
        operation_id = f"audit-{secrets.token_hex(32)}"
        owner_token = secrets.token_hex(32)
        owner_suffix = hashlib.sha256(
            owner_token.encode("ascii")
        ).hexdigest()[:16]
        workspace = workspace_root / f"{operation_id}-{owner_suffix}"
        initial = _seal_scratch_journal(
            {
                "schema_version": SCRATCH_SCHEMA_VERSION,
                "operation_id": operation_id,
                "owner_token": owner_token,
                "uid": os.geteuid(),
                "authority": dict(authority),
                "sequence": 0,
                "previous_state_sha256": None,
                "phase": "starting",
                "workspace": {
                    "path": str(workspace),
                    "state": "create-intent",
                    "identity": None,
                    "owner_marker_sha256": None,
                },
                "resources": [],
                "result": None,
                "blocked_reason": None,
            }
        )
        operation = cls(evidence_root, runner, initial)
        operation._persist(initial, initial=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{workspace.name}.stage-",
                dir=workspace_root,
            )
        )
        os.chmod(staging, 0o700)
        _fsync_directory(workspace_root)
        identity = _directory_identity(staging)
        marker_payload = operation._workspace_owner_marker_payload(identity)
        _write_private_atomic(
            staging,
            SCRATCH_WORKSPACE_OWNER_NAME,
            marker_payload,
        )
        marker_sha256 = sha256_bytes(marker_payload)
        try:
            _rename_directory_noreplace(staging, workspace)
        except FileExistsError as exc:
            operation._update(
                lambda value: (
                    value["workspace"].update(  # type: ignore[union-attr]
                        {"state": "blocked-foreign"}
                    ),
                    value.update(
                        {
                            "phase": "blocked-foreign-identity",
                            "blocked_reason": (
                                "scratch workspace already exists"
                            ),
                        }
                    ),
                )
            )
            raise MediaEvidenceError(
                "scratch workspace collides with an existing path"
            ) from exc
        if _directory_identity(workspace) != identity:
            raise MediaEvidenceError(
                "scratch workspace identity changed during atomic publish"
            )
        operation._update(
            lambda value: (
                value["workspace"].update(  # type: ignore[union-attr]
                    {
                        "state": "created",
                        "identity": identity,
                        "owner_marker_sha256": marker_sha256,
                    }
                ),
                value.update({"phase": "running"}),
            )
        )
        return operation

    @classmethod
    def load(
        cls,
        evidence_root: Path,
        operation_id: str,
        *,
        runner: CommandRunner,
    ) -> ScratchOperation:
        path = (
            _scratch_journal_root(evidence_root)
            / f"{operation_id}.json"
        )
        return cls(
            evidence_root,
            runner,
            _load_scratch_journal(path, evidence_root=evidence_root),
        )

    def _persist(
        self,
        value: Mapping[str, object],
        *,
        initial: bool = False,
    ) -> None:
        validated = _validate_scratch_journal(
            dict(value),
            operation_id=self.operation_id,
            evidence_root=self.evidence_root,
        )
        if initial:
            try:
                os.lstat(self.journal_path)
            except FileNotFoundError:
                pass
            else:
                raise MediaEvidenceError(
                    "scratch operation journal already exists"
                )
        payload = canonical_json_bytes(validated) + b"\n"
        if len(payload) > MAX_REGISTRY_BYTES:
            raise MediaEvidenceError("scratch operation journal exceeds its limit")
        _write_private_atomic(
            _scratch_journal_root(self.evidence_root),
            _scratch_sequence_name(validated),
            payload,
        )
        _write_private_replace(
            self.journal_path,
            payload,
            root=_scratch_journal_root(self.evidence_root),
        )
        self.journal = validated

    def _update(self, mutate: Callable[[dict[str, object]], object]) -> None:
        current = self.journal
        durable = _load_scratch_journal(
            self.journal_path,
            evidence_root=self.evidence_root,
        )
        if durable["journal_sha256"] != current["journal_sha256"]:
            raise MediaEvidenceError(
                "scratch journal changed outside the held operation"
            )
        updated = json.loads(canonical_json_bytes(current).decode("ascii"))
        mutate(updated)
        updated.pop("journal_sha256", None)
        updated["sequence"] = int(current["sequence"]) + 1
        updated["previous_state_sha256"] = current["journal_sha256"]
        sealed = _seal_scratch_journal(updated)
        self._persist(sealed)

    def _blocked(self, reason: str) -> None:
        self._update(
            lambda value: value.update(
                {"phase": "blocked-foreign-identity", "blocked_reason": reason}
            )
        )

    def _resource(self, resource_key: str) -> dict[str, object]:
        matches = [
            value
            for value in self.journal["resources"]  # type: ignore[union-attr]
            if value["resource_key"] == resource_key
        ]
        if len(matches) != 1:
            raise MediaEvidenceError("scratch resource is not uniquely journaled")
        return matches[0]

    def _resource_name(self, resource_key: str) -> str:
        payload = (
            f"{self.operation_id}\0{self.owner_token}\0{resource_key}"
        ).encode("ascii")
        return "nexpoly-audit-" + hashlib.sha256(payload).hexdigest()[:40]

    def _plan_resource(
        self,
        resource_key: str,
        *,
        kind: str,
        spec: Mapping[str, object],
        dependencies: Sequence[str] = (),
        source_media_id: str | None = None,
    ) -> dict[str, object]:
        if (
            SCRATCH_RESOURCE_RE.fullmatch(resource_key) is None
            or any(
                value["resource_key"] == resource_key
                for value in self.journal["resources"]  # type: ignore[union-attr]
            )
        ):
            raise MediaEvidenceError("scratch resource key is invalid or reused")
        labels = _scratch_audit_labels(
            operation_id=self.operation_id,
            owner_token=self.owner_token,
            resource_key=resource_key,
            registry_sha256=self.authority["registry_sha256"],
            source_media_id=source_media_id,
        )
        resource: dict[str, object] = {
            "resource_key": resource_key,
            "kind": kind,
            "name": self._resource_name(resource_key),
            "labels": labels,
            "dependencies": list(dependencies),
            "state": "create-intent",
            "spec": dict(spec),
            "container_id": None,
            "inspect_sha256": None,
        }
        self._update(
            lambda value: value["resources"].append(resource)  # type: ignore[union-attr]
        )
        return self._resource(resource_key)

    def _update_resource(
        self,
        resource_key: str,
        changes: Mapping[str, object],
    ) -> None:
        def mutate(value: dict[str, object]) -> None:
            matches = [
                item
                for item in value["resources"]  # type: ignore[union-attr]
                if item["resource_key"] == resource_key
            ]
            if len(matches) != 1:
                raise MediaEvidenceError(
                    "scratch resource disappeared from its journal"
                )
            matches[0].update(changes)

        self._update(mutate)

    def create_volume(
        self,
        resource_key: str,
        *,
        source_media_id: str | None = None,
    ) -> str:
        resource = self._plan_resource(
            resource_key,
            kind="volume",
            spec={
                "driver": "local",
                "scope": "local",
                "options": None,
            },
            source_media_id=source_media_id,
        )
        arguments = [DOCKER, "volume", "create"]
        for key, value in sorted(resource["labels"].items()):  # type: ignore[union-attr]
            arguments.extend(["--label", f"{key}={value}"])
        arguments.extend(["--", str(resource["name"])])
        try:
            completed = self.runner.run(arguments, timeout=60)
            returned = completed.stdout.decode("utf-8", "strict").strip()
            if returned and returned != resource["name"]:
                raise MediaEvidenceError(
                    "Docker created an unexpected scratch volume"
                )
            self._adopt_volume(resource_key)
        except BaseException:
            self._resolve_create_intent(
                resource_key,
                absent_state="create-ambiguous",
            )
            raise
        return str(resource["name"])

    def _adopt_volume(self, resource_key: str) -> None:
        resource = self._resource(resource_key)
        value = _optional_docker_inspect(
            self.runner,
            "volume",
            str(resource["name"]),
        )
        if value is None:
            raise MediaEvidenceError("scratch volume disappeared after creation")
        labels = value.get("Labels")
        spec = resource["spec"]
        if (
            value.get("Name") != resource["name"]
            or not _labels_match(labels, resource["labels"])
            or value.get("Driver") != spec.get("driver")
            or value.get("Scope") not in {None, spec.get("scope")}
            or value.get("Options") not in (None, spec.get("options"))
        ):
            self._blocked("scratch volume has a foreign identity")
            raise MediaEvidenceError("scratch volume identity is foreign")
        digest = sha256_bytes(canonical_json_bytes(value))
        recorded = resource.get("inspect_sha256")
        if recorded is not None and recorded != digest:
            self._blocked("scratch volume inspect identity changed")
            raise MediaEvidenceError("scratch volume identity changed")
        self._update_resource(
            resource_key,
            {"state": "created", "inspect_sha256": digest},
        )

    def run_container(
        self,
        resource_key: str,
        arguments: Sequence[str],
        *,
        mounts: Sequence[Mapping[str, object]] = (),
        dependencies: Sequence[str] = (),
        source_media_id: str | None = None,
        detached: bool = False,
        read_only_rootfs: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        if len(arguments) < 3 or list(arguments[:2]) != [DOCKER, "run"]:
            raise MediaEvidenceError("owned scratch command is not docker run")
        if "--name" in arguments or "--label" in arguments:
            raise MediaEvidenceError(
                "owned scratch command supplied unmanaged identity flags"
            )
        try:
            image_index = list(arguments).index(
                self.authority["postgres_image"],
                2,
            )
        except ValueError as exc:
            raise MediaEvidenceError(
                "owned scratch command does not use the authority image"
            ) from exc
        expected_entrypoint: str | None = None
        expected_environment: list[str] = []
        expected_tmpfs: dict[str, str] = {}
        for index, value in enumerate(arguments[2:image_index]):
            if value == "--entrypoint":
                expected_entrypoint = str(arguments[index + 3])
            elif value == "--env":
                expected_environment.append(str(arguments[index + 3]))
            elif value == "--tmpfs":
                raw_tmpfs = str(arguments[index + 3])
                destination, separator, options = raw_tmpfs.partition(":")
                if (
                    not separator
                    or not PurePosixPath(destination).is_absolute()
                    or not options
                    or destination in expected_tmpfs
                ):
                    raise MediaEvidenceError(
                        "owned scratch tmpfs specification is invalid"
                    )
                expected_tmpfs[destination] = options
        if any("=" not in value for value in expected_environment):
            raise MediaEvidenceError(
                "owned scratch environment must use explicit key=value"
            )
        effective_mounts = [dict(value) for value in mounts]
        needs_pgdata_tmpfs = not any(
            value.get("destination") == "/var/lib/postgresql/data"
            for value in effective_mounts
        )
        if needs_pgdata_tmpfs:
            effective_mounts.append(
                {
                    "kind": "tmpfs",
                    "source": None,
                    "destination": "/var/lib/postgresql/data",
                    "read_only": False,
                }
            )
            expected_tmpfs["/var/lib/postgresql/data"] = (
                "rw,noexec,nosuid,size=1m,mode=0700"
            )
        resource = self._plan_resource(
            resource_key,
            kind="container",
            spec={
                "postgres_image": self.authority["postgres_image"],
                "postgres_image_id": self.authority["postgres_image_id"],
                "mounts": sorted(
                    effective_mounts,
                    key=canonical_json_bytes,
                ),
                "network": "none",
                "read_only_rootfs": read_only_rootfs,
                "detached": detached,
                "command": list(arguments[image_index + 1 :]),
                "entrypoint": expected_entrypoint,
                "environment": expected_environment,
                "tmpfs": expected_tmpfs,
                "arguments_sha256": sha256_bytes(
                    canonical_json_bytes(list(arguments))
                ),
            },
            dependencies=dependencies,
            source_media_id=source_media_id,
        )
        managed = [DOCKER, "run", "--name", str(resource["name"])]
        for key, value in sorted(resource["labels"].items()):  # type: ignore[union-attr]
            managed.extend(["--label", f"{key}={value}"])
        if needs_pgdata_tmpfs:
            managed.extend(
                [
                    "--tmpfs",
                    (
                        "/var/lib/postgresql/data:"
                        "rw,noexec,nosuid,size=1m,mode=0700"
                    ),
                ]
            )
        # Do not delegate lifecycle ownership to Docker's --rm.  A named
        # exited container must remain inspectable long enough to CAS its
        # immutable identity before journaled container-first removal.
        managed.extend(value for value in arguments[2:] if value != "--rm")
        try:
            completed = self.runner.run(managed, timeout=3600)
            if detached:
                returned_id = completed.stdout.decode("ascii", "strict").strip()
                if CONTAINER_RE.fullmatch(returned_id) is None:
                    raise MediaEvidenceError(
                        "Docker returned an invalid scratch container ID"
                    )
            self._resolve_created_container(
                resource_key,
                allow_absent=False,
            )
            if (
                detached
                and self._resource(resource_key)["container_id"]
                != returned_id
            ):
                self._blocked(
                    "Docker detached response ID differs from inspect"
                )
                raise MediaEvidenceError(
                    "scratch container response identity is foreign"
                )
            if not detached:
                self.remove_resource(resource_key)
            return completed
        except BaseException:
            if self.journal["phase"] == "blocked-foreign-identity":
                raise
            self._resolve_create_intent(
                resource_key,
                absent_state="create-ambiguous",
            )
            if self._resource(resource_key)["state"] != "create-ambiguous":
                try:
                    self.remove_resource(resource_key)
                except BaseException:
                    pass
            raise

    def _validate_container(
        self,
        resource_key: str,
        value: Mapping[str, object],
    ) -> tuple[str, str]:
        resource = self._resource(resource_key)
        config = value.get("Config")
        host = value.get("HostConfig")
        container_id = value.get("Id")
        name = value.get("Name")
        spec = resource["spec"]
        if not isinstance(config, dict) or not isinstance(host, dict):
            self._blocked("scratch container config is invalid")
            raise MediaEvidenceError("scratch container identity is foreign")
        actual_entrypoint = config.get("Entrypoint")
        expected_entrypoint = spec.get("entrypoint")
        if expected_entrypoint is not None and actual_entrypoint not in (
            expected_entrypoint,
            [expected_entrypoint],
        ):
            self._blocked("scratch container entrypoint changed")
            raise MediaEvidenceError("scratch container entrypoint is foreign")
        actual_environment = config.get("Env")
        if (
            not isinstance(container_id, str)
            or CONTAINER_RE.fullmatch(container_id) is None
            or name != f"/{resource['name']}"
            or value.get("Image") != spec.get("postgres_image_id")
            or not isinstance(config.get("Image"), str)
            or config.get("Cmd") != spec.get("command")
            or not isinstance(actual_environment, list)
            or any(
                not isinstance(value, str)
                for value in actual_environment
            )
            or any(
                value not in actual_environment
                for value in spec.get("environment", [])
            )
            or not _labels_match(config.get("Labels"), resource["labels"])
            or host.get("NetworkMode") != "none"
            or host.get("Privileged") not in {None, False}
            or host.get("PortBindings") not in (None, {})
            or host.get("Devices") not in (None, [])
            or (host.get("Tmpfs") or {}) != spec.get("tmpfs")
            or bool(host.get("ReadonlyRootfs"))
            is not bool(spec.get("read_only_rootfs"))
            or _container_mount_projection(value) != spec.get("mounts")
        ):
            self._blocked("scratch container has a foreign identity")
            raise MediaEvidenceError("scratch container identity is foreign")
        restart = host.get("RestartPolicy")
        if isinstance(restart, dict) and restart.get("Name") not in {"", "no"}:
            self._blocked("scratch container has a restart policy")
            raise MediaEvidenceError("scratch container restart policy is unsafe")
        digest = sha256_bytes(
            canonical_json_bytes(_container_immutable_identity(value))
        )
        if (
            resource.get("container_id") is not None
            and resource["container_id"] != container_id
            or resource.get("inspect_sha256") is not None
            and resource["inspect_sha256"] != digest
        ):
            self._blocked("scratch container immutable identity changed")
            raise MediaEvidenceError("scratch container identity changed")
        return container_id, digest

    def _resolve_created_container(
        self,
        resource_key: str,
        *,
        allow_absent: bool,
        absent_state: str = "absent",
    ) -> None:
        resource = self._resource(resource_key)
        value = _optional_docker_inspect(
            self.runner,
            "container",
            str(resource["name"]),
        )
        if value is None:
            if allow_absent:
                self._update_resource(
                    resource_key,
                    {"state": absent_state},
                )
                if absent_state in {
                    "create-ambiguous",
                    "create-absent-confirmation",
                }:
                    self._mark_create_ambiguous()
                return
            raise MediaEvidenceError(
                "detached scratch container disappeared after creation"
            )
        container_id, digest = self._validate_container(resource_key, value)
        self._update_resource(
            resource_key,
            {
                "state": "created",
                "container_id": container_id,
                "inspect_sha256": digest,
            },
        )

    def _mark_create_ambiguous(self) -> None:
        self._update(
            lambda value: value.update(
                {
                    "phase": "awaiting-create-resolution",
                    "blocked_reason": (
                        "Docker creation outcome requires a later "
                        "recovery pass"
                    ),
                }
            )
        )

    def _resolve_create_intent(
        self,
        resource_key: str,
        *,
        absent_state: str = "absent",
    ) -> None:
        resource = self._resource(resource_key)
        if resource["kind"] == "container":
            self._resolve_created_container(
                resource_key,
                allow_absent=True,
                absent_state=absent_state,
            )
            return
        value = _optional_docker_inspect(
            self.runner,
            "volume",
            str(resource["name"]),
        )
        if value is None:
            self._update_resource(
                resource_key,
                {"state": absent_state},
            )
            if absent_state in {
                "create-ambiguous",
                "create-absent-confirmation",
            }:
                self._mark_create_ambiguous()
        else:
            self._adopt_volume(resource_key)

    def _volume_attachments(self, name: str) -> list[str]:
        completed = self.runner.run(
            [DOCKER, "ps", "-aq", "--no-trunc"],
            timeout=60,
        )
        identifiers = [
            value
            for value in completed.stdout.decode("ascii", "strict").splitlines()
            if value
        ]
        if any(CONTAINER_RE.fullmatch(value) is None for value in identifiers):
            raise MediaEvidenceError("Docker returned malformed container IDs")
        attached: list[str] = []
        for identifier in sorted(set(identifiers)):
            container = _optional_docker_inspect(
                self.runner,
                "container",
                identifier,
            )
            if container is None:
                continue
            mounts = container.get("Mounts")
            if not isinstance(mounts, list):
                raise MediaEvidenceError("Docker container mounts are invalid")
            if any(
                isinstance(mount, dict)
                and mount.get("Type") == "volume"
                and mount.get("Name") == name
                for mount in mounts
            ):
                attached.append(identifier)
        return attached

    def _operation_labeled_resources(self) -> list[dict[str, object]]:
        expected_owner = sha256_bytes(self.owner_token.encode("ascii"))
        result: list[dict[str, object]] = []
        volume_names = [
            value
            for value in self.runner.run(
                [DOCKER, "volume", "ls", "--format", "{{.Name}}"],
                timeout=60,
            )
            .stdout.decode("utf-8", "strict")
            .splitlines()
            if value
        ]
        if any(VOLUME_RE.fullmatch(value) is None for value in volume_names):
            raise MediaEvidenceError(
                "Docker returned malformed volume names during orphan fence"
            )
        for name in sorted(set(volume_names)):
            value = _optional_docker_inspect(
                self.runner,
                "volume",
                name,
            )
            if value is None:
                raise MediaEvidenceError(
                    "Docker volume inventory changed during orphan fence"
                )
            labels = value.get("Labels")
            if (
                isinstance(labels, dict)
                and labels.get("io.nexpoly.audit.operation")
                == self.operation_id
            ):
                result.append(
                    {
                        "kind": "volume",
                        "name": name,
                        "owner_matches": (
                            labels.get("io.nexpoly.audit.owner")
                            == expected_owner
                        ),
                        "inspect_sha256": sha256_bytes(
                            canonical_json_bytes(value)
                        ),
                    }
                )
        identifiers = [
            value
            for value in self.runner.run(
                [DOCKER, "ps", "-aq", "--no-trunc"],
                timeout=60,
            )
            .stdout.decode("ascii", "strict")
            .splitlines()
            if value
        ]
        if any(CONTAINER_RE.fullmatch(value) is None for value in identifiers):
            raise MediaEvidenceError(
                "Docker returned malformed container IDs during orphan fence"
            )
        for identifier in sorted(set(identifiers)):
            value = _optional_docker_inspect(
                self.runner,
                "container",
                identifier,
            )
            if value is None:
                raise MediaEvidenceError(
                    "Docker container inventory changed during orphan fence"
                )
            config = value.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if (
                isinstance(labels, dict)
                and labels.get("io.nexpoly.audit.operation")
                == self.operation_id
            ):
                result.append(
                    {
                        "kind": "container",
                        "name": value.get("Name"),
                        "container_id": identifier,
                        "owner_matches": (
                            labels.get("io.nexpoly.audit.owner")
                            == expected_owner
                        ),
                        "inspect_sha256": sha256_bytes(
                            canonical_json_bytes(
                                _container_immutable_identity(value)
                            )
                        ),
                    }
                )
        return sorted(result, key=canonical_json_bytes)

    def remove_resource(
        self,
        resource_key: str,
        *,
        resolve_ambiguous: bool = False,
    ) -> None:
        resource = self._resource(resource_key)
        if (
            resource["state"]
            in {"create-ambiguous", "create-absent-confirmation"}
            and not resolve_ambiguous
        ):
            raise MediaEvidenceError(
                "scratch creation outcome awaits a later recovery pass"
            )
        if resource["state"] == "absent":
            present = _optional_docker_inspect(
                self.runner,
                str(resource["kind"]),
                str(resource["name"]),
            )
            if present is not None:
                self._blocked(
                    "an absent scratch tombstone name was reused"
                )
                raise MediaEvidenceError(
                    "scratch tombstone name was reused by a foreign resource"
                )
            return
        if resource["state"] == "create-ambiguous":
            self._resolve_create_intent(
                resource_key,
                absent_state="create-absent-confirmation",
            )
            resource = self._resource(resource_key)
            if resource["state"] == "create-absent-confirmation":
                raise MediaEvidenceError(
                    "scratch absence requires one additional recovery pass"
                )
        elif resource["state"] == "create-absent-confirmation":
            self._resolve_create_intent(
                resource_key,
                absent_state="absent",
            )
        else:
            self._resolve_create_intent(resource_key)
        resource = self._resource(resource_key)
        if resource["state"] == "absent":
            return
        self._update_resource(resource_key, {"state": "remove-intent"})
        resource = self._resource(resource_key)
        if resource["kind"] == "container":
            value = _optional_docker_inspect(
                self.runner,
                "container",
                str(resource["name"]),
            )
            if value is None:
                self._update_resource(resource_key, {"state": "absent"})
                return
            container_id, _digest = self._validate_container(
                resource_key,
                value,
            )
            removed = self.runner.run(
                [
                    DOCKER,
                    "container",
                    "rm",
                    "-f",
                    "-v",
                    "--",
                    container_id,
                ],
                timeout=120,
                check=False,
            )
            if (
                removed.returncode != 0
                and b"no such" not in removed.stderr.lower()
            ):
                raise MediaEvidenceError(
                    "owned scratch container removal failed"
                )
            if _optional_docker_inspect(
                self.runner,
                "container",
                str(resource["name"]),
            ) is not None:
                raise MediaEvidenceError(
                    "owned scratch container remains after removal"
                )
        else:
            self._adopt_volume(resource_key)
            resource = self._resource(resource_key)
            attachments = self._volume_attachments(str(resource["name"]))
            if attachments:
                self._blocked(
                    "scratch volume has a foreign or live attachment"
                )
                raise MediaEvidenceError(
                    "scratch volume still has container attachments"
                )
            # Docker volumes have no immutable object ID.  Repeat the complete
            # label/spec/digest and attachment CAS immediately before the
            # name-based rm so a same-name swap in the earlier inspection
            # window is never deleted.
            self._adopt_volume(resource_key)
            resource = self._resource(resource_key)
            attachments = self._volume_attachments(str(resource["name"]))
            if attachments:
                self._blocked(
                    "scratch volume gained an attachment before removal"
                )
                raise MediaEvidenceError(
                    "scratch volume gained an attachment before removal"
                )
            removed = self.runner.run(
                [DOCKER, "volume", "rm", "--", str(resource["name"])],
                timeout=120,
                check=False,
            )
            if (
                removed.returncode != 0
                and b"no such" not in removed.stderr.lower()
            ):
                raise MediaEvidenceError("owned scratch volume removal failed")
            if _optional_docker_inspect(
                self.runner,
                "volume",
                str(resource["name"]),
            ) is not None:
                raise MediaEvidenceError(
                    "owned scratch volume remains after removal"
                )
        self._update_resource(resource_key, {"state": "absent"})

    def recover(self, *, terminal_phase: str = "recovered") -> None:
        if self.journal["phase"] in SCRATCH_TERMINAL_PHASES:
            remaining = self._operation_labeled_resources()
            if remaining:
                raise MediaEvidenceError(
                    "terminal scratch journal still has operation-labeled "
                    "Docker resources"
                )
            return
        try:
            for resource in list(self.journal["resources"]):  # type: ignore[union-attr]
                if resource["kind"] == "container":
                    self.remove_resource(
                        str(resource["resource_key"]),
                        resolve_ambiguous=True,
                    )
            for resource in list(self.journal["resources"]):  # type: ignore[union-attr]
                if resource["kind"] == "volume":
                    self.remove_resource(
                        str(resource["resource_key"]),
                        resolve_ambiguous=True,
                    )
            self._remove_workspace()
            remaining = self._operation_labeled_resources()
            if remaining:
                self._blocked(
                    "operation-labeled Docker resources remain outside "
                    "the durable journal"
                )
                raise MediaEvidenceError(
                    "operation-labeled Docker resources remain after recovery"
                )
            self._update(
                lambda value: value.update(
                    {
                        "phase": terminal_phase,
                        "blocked_reason": None,
                    }
                )
            )
        except BaseException as error:
            if self.journal["phase"] == "awaiting-create-resolution":
                raise
            if self.journal["phase"] != "blocked-foreign-identity":
                reason = str(error)
                self._update(
                    lambda value: value.update(
                        {
                            "phase": "blocked-recovery-error",
                            "blocked_reason": reason,
                        }
                    )
                )
            raise

    def _remove_workspace(self) -> None:
        workspace = self.journal["workspace"]  # type: ignore[assignment]
        path = Path(str(workspace["path"]))
        if workspace["state"] == "blocked-foreign":
            raise MediaEvidenceError(
                "foreign scratch workspace collision cannot be removed"
            )
        if workspace["state"] == "absent":
            if path.exists() or path.is_symlink():
                self._blocked("absent scratch workspace path was reused")
                raise MediaEvidenceError("scratch workspace tombstone was reused")
            return
        if workspace["state"] == "create-intent":
            if path.exists() or path.is_symlink():
                try:
                    identity, marker_sha256 = (
                        self._unsealed_workspace_owner(path)
                    )
                except (MediaEvidenceError, OSError):
                    self._blocked(
                        "workspace collision lacks the exact durable owner marker"
                    )
                    raise MediaEvidenceError(
                        "unowned scratch workspace cannot be recovered"
                    )
                self._update(
                    lambda value: value["workspace"].update(  # type: ignore[union-attr]
                        {
                            "state": "created",
                            "identity": identity,
                            "owner_marker_sha256": marker_sha256,
                        }
                    )
                )
                workspace = self.journal["workspace"]  # type: ignore[assignment]
            else:
                self._remove_owned_staging_workspaces()
                self._update(
                    lambda value: value["workspace"].update(  # type: ignore[union-attr]
                        {"state": "absent"}
                    )
                )
                return
        if not path.exists() and not path.is_symlink():
            self._update(
                lambda value: value["workspace"].update(  # type: ignore[union-attr]
                    {"state": "absent"}
                )
            )
            return
        identity = _directory_identity(path)
        expected = workspace.get("identity")
        if expected is None or identity != expected:
            self._blocked("scratch workspace identity changed")
            raise MediaEvidenceError("scratch workspace identity is foreign")
        if workspace["state"] == "owner-marker-create-intent":
            marker_payload = self._workspace_owner_marker_payload(
                workspace["identity"]
            )
            _write_private_atomic(
                path,
                SCRATCH_WORKSPACE_OWNER_NAME,
                marker_payload,
            )
            marker_sha256 = sha256_bytes(marker_payload)
            self._update(
                lambda value: value["workspace"].update(  # type: ignore[union-attr]
                    {
                        "state": "created",
                        "owner_marker_sha256": marker_sha256,
                    }
                )
            )
            workspace = self.journal["workspace"]  # type: ignore[assignment]
        if workspace["state"] in {"created", "remove-intent"}:
            self._validate_workspace_owner_marker(path, workspace)
        self._update(
            lambda value: value["workspace"].update(  # type: ignore[union-attr]
                {"state": "remove-intent"}
            )
        )
        shutil.rmtree(path)
        _fsync_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise MediaEvidenceError("scratch workspace removal failed")
        self._update(
            lambda value: value["workspace"].update(  # type: ignore[union-attr]
                {"state": "absent"}
            )
        )

    def _staging_workspace_paths(self) -> list[Path]:
        workspace_root = _scratch_workspace_root(self.evidence_root)
        prefix = f".{self.workspace.name}.stage-"
        descriptor = _open_directory_chain(
            workspace_root,
            private_from=workspace_root,
        )
        try:
            names = sorted(os.listdir(descriptor))
        finally:
            os.close(descriptor)
        return [
            workspace_root / name
            for name in names
            if name.startswith(prefix)
            and re.fullmatch(
                re.escape(prefix) + r"[A-Za-z0-9_-]{6,64}",
                name,
            )
            is not None
        ]

    def _unsealed_workspace_owner(
        self,
        path: Path,
    ) -> tuple[dict[str, int], str]:
        identity = _directory_identity(path)
        marker = path / SCRATCH_WORKSPACE_OWNER_NAME
        descriptor = open_private_regular(marker, root=path)
        try:
            payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
        finally:
            os.close(descriptor)
        expected = self._workspace_owner_marker_payload(identity)
        if payload != expected:
            raise MediaEvidenceError(
                "scratch workspace owner marker is foreign"
            )
        return identity, sha256_bytes(payload)

    def _remove_owned_staging_workspaces(self) -> None:
        for staging in self._staging_workspace_paths():
            try:
                self._unsealed_workspace_owner(staging)
            except (FileNotFoundError, MediaEvidenceError, OSError):
                # An unsealed staging collision is never promoted or removed.
                continue
            shutil.rmtree(staging)
            _fsync_directory(staging.parent)

    def _validate_workspace_owner_marker(
        self,
        path: Path,
        workspace: Mapping[str, object],
    ) -> None:
        marker_sha256 = workspace.get("owner_marker_sha256")
        if (
            not isinstance(marker_sha256, str)
            or DIGEST_RE.fullmatch(marker_sha256) is None
        ):
            self._blocked("scratch workspace owner marker is missing")
            raise MediaEvidenceError(
                "scratch workspace owner marker is missing"
            )
        marker = path / SCRATCH_WORKSPACE_OWNER_NAME
        descriptor = open_private_regular(marker, root=path)
        try:
            payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
        finally:
            os.close(descriptor)
        expected = self._workspace_owner_marker_payload(
            workspace["identity"]
        )
        if payload != expected or sha256_bytes(payload) != marker_sha256:
            self._blocked("scratch workspace owner marker changed")
            raise MediaEvidenceError(
                "scratch workspace owner marker is foreign"
            )

    def _workspace_owner_marker_payload(
        self,
        identity: object,
    ) -> bytes:
        return canonical_json_bytes(
            {
                "schema_version": SCRATCH_SCHEMA_VERSION,
                "operation_id": self.operation_id,
                "owner_sha256": sha256_bytes(
                    self.owner_token.encode("ascii")
                ),
                "authority_sha256": sha256_bytes(
                    canonical_json_bytes(self.authority)
                ),
                "workspace_identity": identity,
            }
        ) + b"\n"

    def complete(self, result: Mapping[str, object]) -> None:
        self._update(lambda value: value.update({"result": dict(result)}))
        self.recover(terminal_phase="completed")

    def abort(self) -> None:
        if any(
            resource["state"] == "create-ambiguous"
            or resource["state"] == "create-absent-confirmation"
            for resource in self.journal["resources"]  # type: ignore[union-attr]
        ):
            raise MediaEvidenceError(
                "scratch creation outcome awaits a later recovery pass"
            )
        self.recover(terminal_phase="aborted")


def _scratch_journal_entries(evidence_root: Path) -> list[str]:
    journal_root = _scratch_journal_root(evidence_root)
    descriptor = _open_directory_chain(
        journal_root,
        private_from=journal_root,
    )
    try:
        names = sorted(os.listdir(descriptor))
    finally:
        os.close(descriptor)
    return names


def _scratch_journal_temp_names(evidence_root: Path) -> list[str]:
    return [
        name
        for name in _scratch_journal_entries(evidence_root)
        if SCRATCH_JOURNAL_TEMP_RE.fullmatch(name) is not None
    ]


def _validate_scratch_journal_temps(evidence_root: Path) -> list[str]:
    journal_root = _scratch_journal_root(evidence_root)
    names = _scratch_journal_temp_names(evidence_root)
    directory = _open_directory_chain(
        journal_root,
        private_from=journal_root,
    )
    try:
        for name in names:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink != 1
                ):
                    raise MediaEvidenceError(
                        "incomplete scratch journal update is unsafe"
                    )
            finally:
                os.close(descriptor)
    finally:
        os.close(directory)
    return names


def _cleanup_scratch_journal_temps(evidence_root: Path) -> None:
    journal_root = _scratch_journal_root(evidence_root)
    directory = _open_directory_chain(
        journal_root,
        private_from=journal_root,
    )
    try:
        for name in _validate_scratch_journal_temps(evidence_root):
            os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def _list_scratch_operation_ids(evidence_root: Path) -> list[str]:
    result: set[str] = set()
    for name in _scratch_journal_entries(evidence_root):
        if name == SCRATCH_LOCK_NAME:
            continue
        if SCRATCH_JOURNAL_TEMP_RE.fullmatch(name) is not None:
            continue
        sequence = SCRATCH_SEQUENCE_RE.fullmatch(name)
        if sequence is not None:
            result.add(sequence.group(1))
            continue
        if not name.endswith(".json"):
            raise MediaEvidenceError("scratch journal root has an unknown entry")
        operation_id = name.removesuffix(".json")
        if SCRATCH_OPERATION_RE.fullmatch(operation_id) is None:
            raise MediaEvidenceError("scratch journal name is invalid")
        result.add(operation_id)
    return sorted(result)


def recover_scratch_operations(
    evidence_root: Path,
    *,
    runner: CommandRunner,
    operation_id: str | None = None,
) -> dict[str, object]:
    _cleanup_scratch_journal_temps(evidence_root)
    identifiers = _list_scratch_operation_ids(evidence_root)
    if operation_id is not None:
        if SCRATCH_OPERATION_RE.fullmatch(operation_id) is None:
            raise MediaEvidenceError("scratch operation ID is invalid")
        if operation_id not in identifiers:
            raise MediaEvidenceError("scratch operation journal does not exist")
        identifiers = [operation_id]
    labeled_operations = _audit_labeled_operation_ids(runner)
    known_operations = set(_list_scratch_operation_ids(evidence_root))
    unknown_operations = sorted(labeled_operations - known_operations)
    if unknown_operations:
        raise MediaEvidenceError(
            "Docker scratch-audit resources lack immutable operation "
            f"journals: {unknown_operations!r}"
        )
    recovered: list[str] = []
    terminal: list[str] = []
    for identifier in identifiers:
        operation = ScratchOperation.load(
            evidence_root,
            identifier,
            runner=runner,
        )
        if operation.journal["phase"] in SCRATCH_TERMINAL_PHASES:
            operation.recover()
            terminal.append(identifier)
            continue
        operation.recover()
        recovered.append(identifier)
    return {
        "schema_version": SCRATCH_SCHEMA_VERSION,
        "recovered_operation_ids": recovered,
        "terminal_operation_ids": terminal,
    }


def scratch_status(
    evidence_root: Path,
    *,
    operation_id: str | None = None,
) -> dict[str, object]:
    identifiers = _list_scratch_operation_ids(evidence_root)
    if operation_id is not None:
        if SCRATCH_OPERATION_RE.fullmatch(operation_id) is None:
            raise MediaEvidenceError("scratch operation ID is invalid")
        if operation_id not in identifiers:
            raise MediaEvidenceError("scratch operation journal does not exist")
        identifiers = [operation_id]
    operations: list[dict[str, object]] = []
    for identifier in identifiers:
        journal = _load_scratch_journal(
            _scratch_journal_root(evidence_root) / f"{identifier}.json",
            evidence_root=evidence_root,
        )
        operations.append(
            {
                "operation_id": identifier,
                "phase": journal["phase"],
                "sequence": journal["sequence"],
                "journal_sha256": journal["journal_sha256"],
                "workspace_state": journal["workspace"]["state"],  # type: ignore[index]
                "resources": [
                    {
                        "resource_key": resource["resource_key"],
                        "kind": resource["kind"],
                        "name": resource["name"],
                        "state": resource["state"],
                    }
                    for resource in journal["resources"]  # type: ignore[union-attr]
                ],
                "blocked_reason": journal["blocked_reason"],
                "result": journal["result"],
            }
        )
    return {
        "schema_version": SCRATCH_SCHEMA_VERSION,
        "incomplete_journal_update_count": len(
            _validate_scratch_journal_temps(evidence_root)
        ),
        "operations": operations,
    }


def fixed_environment(**extra: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        **extra,
    }


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
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _absolute_parts(path: Path) -> tuple[str, ...]:
    raw = os.fspath(path)
    if not raw.startswith("/") or "\x00" in raw:
        raise MediaEvidenceError(f"path is not absolute: {path}")
    pure = PurePosixPath(raw)
    if ".." in pure.parts or "." in pure.parts:
        raise MediaEvidenceError(f"path is not normalized: {path}")
    return tuple(part for part in pure.parts if part != "/")


def _open_directory_chain(
    path: Path,
    *,
    private_from: Path,
    create_leaf: bool = False,
) -> int:
    """Open a directory without following any component.

    Components at and below ``private_from`` must be deploy-user-owned 0700.
    Earlier ancestors need only be non-group/world-writable.  The returned
    descriptor owns the final directory and must be closed by the caller.
    """

    parts = _absolute_parts(path)
    anchor_parts = _absolute_parts(private_from)
    if parts[: len(anchor_parts)] != anchor_parts:
        raise MediaEvidenceError(f"path escapes its private anchor: {path}")
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for index, component in enumerate(parts):
            flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create_leaf or index != len(parts) - 1:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            private = index + 1 >= len(anchor_parts)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or private
                and (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                )
                or not private
                and metadata.st_mode & 0o022
                and not (
                    metadata.st_uid == 0
                    and metadata.st_mode & stat.S_ISVTX
                )
            ):
                os.close(child)
                raise MediaEvidenceError(f"unsafe directory chain: {path}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


BACKUP_ROOT_IDENTITY_FIELDS = {
    "path",
    "device",
    "inode",
    "uid",
    "mode",
}


def _open_directory_without_symlinks(path: Path) -> int:
    """Open an absolute directory path without trusting ancestor permissions."""

    parts = _absolute_parts(path)
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for component in parts:
            child = os.open(
                component,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _backup_root_identity(
    descriptor: int,
    path: Path,
) -> dict[str, object]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MediaEvidenceError(
            f"backup root is not deploy-user-owned mode 0700: {path}"
        )
    return {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def capture_backup_root_identity(path: Path) -> dict[str, object]:
    """CAS-capture one private root while tolerating shared ancestors."""

    first = _open_directory_without_symlinks(path)
    try:
        before = _backup_root_identity(first, path)
    finally:
        os.close(first)
    second = _open_directory_without_symlinks(path)
    try:
        after = _backup_root_identity(second, path)
    finally:
        os.close(second)
    if before != after:
        raise MediaEvidenceError(
            f"backup root changed while its identity was captured: {path}"
        )
    return before


def _validate_backup_root_authority(
    value: object,
    *,
    path: Path,
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != BACKUP_ROOT_IDENTITY_FIELDS
        or value.get("path") != str(path)
        or isinstance(value.get("device"), bool)
        or not isinstance(value.get("device"), int)
        or value["device"] < 0
        or isinstance(value.get("inode"), bool)
        or not isinstance(value.get("inode"), int)
        or value["inode"] <= 0
        or value.get("uid") != os.geteuid()
        or value.get("mode") != 0o700
    ):
        raise MediaEvidenceError(
            f"backup root identity authority is invalid: {path}"
        )
    return dict(value)


def _open_sealed_backup_root(
    path: Path,
    authority: object,
) -> int:
    expected = _validate_backup_root_authority(authority, path=path)
    try:
        descriptor = _open_directory_without_symlinks(path)
    except OSError as exc:
        raise MediaEvidenceError(
            f"sealed backup root is missing, replaced, or symlinked: {path}"
        ) from exc
    try:
        observed = _backup_root_identity(descriptor, path)
        if observed != expected:
            raise MediaEvidenceError(
                f"sealed backup root identity differs: {path}"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _relative_to_backup_root(path: Path, root: Path) -> tuple[str, ...]:
    parts = _absolute_parts(path)
    root_parts = _absolute_parts(root)
    if len(parts) <= len(root_parts) or parts[: len(root_parts)] != root_parts:
        raise MediaEvidenceError(f"private file escapes approved root: {path}")
    return parts[len(root_parts) :]


def open_sealed_backup_regular(
    path: Path,
    *,
    root: Path,
    root_authority: object,
) -> int:
    """Open a private backup relative to an exact sealed root descriptor."""

    relative = _relative_to_backup_root(path, root)
    descriptor = _open_sealed_backup_root(root, root_authority)
    try:
        for component in relative[:-1]:
            child = os.open(
                component,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                os.close(child)
                raise MediaEvidenceError(
                    f"backup private parent chain is unsafe: {path}"
                )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            relative[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)
    metadata = os.fstat(file_descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(file_descriptor)
        raise MediaEvidenceError(f"private backup file is unsafe: {path}")
    return file_descriptor


def seal_discovery_boundary(
    policy: DiscoveryPolicy,
) -> dict[str, object]:
    return {
        **policy.document(),
        "backup_root_identities": [
            capture_backup_root_identity(root)
            for root in policy.backup_roots
        ],
    }


def _load_discovery_boundary(
    value: object,
    *,
    policy: DiscoveryPolicy,
) -> dict[str, object]:
    static = policy.document()
    if (
        not isinstance(value, dict)
        or set(value) != {*static, "backup_root_identities"}
        or {name: value[name] for name in static} != static
        or not isinstance(value.get("backup_root_identities"), list)
        or len(value["backup_root_identities"]) != len(policy.backup_roots)
    ):
        raise MediaEvidenceError(
            "media registry narrowed or changed the fixed discovery boundary"
        )
    identities: list[dict[str, object]] = []
    for root, raw in zip(
        policy.backup_roots,
        value["backup_root_identities"],
        strict=True,
    ):
        identity = _validate_backup_root_authority(raw, path=root)
        descriptor = _open_sealed_backup_root(root, identity)
        os.close(descriptor)
        identities.append(identity)
    return {
        **static,
        "backup_root_identities": identities,
    }


def _sealed_root_authority(
    registry: Registry,
    root: Path,
) -> dict[str, object]:
    identities = registry.boundary.get("backup_root_identities")
    if not isinstance(identities, list):
        raise MediaEvidenceError(
            "media registry lacks sealed backup root identities"
        )
    matches = [
        value
        for value in identities
        if isinstance(value, dict) and value.get("path") == str(root)
    ]
    if len(matches) != 1:
        raise MediaEvidenceError(
            f"media registry backup root identity is ambiguous: {root}"
        )
    return _validate_backup_root_authority(matches[0], path=root)


def open_private_regular(path: Path, *, root: Path) -> int:
    """Open a private regular file using openat/O_NOFOLLOW for every parent."""

    parts = _absolute_parts(path)
    root_parts = _absolute_parts(root)
    if len(parts) <= len(root_parts) or parts[: len(root_parts)] != root_parts:
        raise MediaEvidenceError(f"private file escapes approved root: {path}")
    parent = Path("/").joinpath(*parts[:-1])
    descriptor = _open_directory_chain(parent, private_from=root)
    try:
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)
    metadata = os.fstat(file_descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        os.close(file_descriptor)
        raise MediaEvidenceError(f"private file is unsafe: {path}")
    return file_descriptor


def _read_fd(descriptor: int, maximum: int | None = None) -> bytes:
    chunks: list[bytes] = []
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if maximum is not None and size > maximum:
            raise MediaEvidenceError("private file exceeds its size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise MediaEvidenceError("private file write made no progress")
        view = view[written:]


def _fd_identity(descriptor: int, path: Path, *, include_digest: bool) -> dict[str, object]:
    metadata = os.fstat(descriptor)
    result: dict[str, object] = {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size_bytes": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }
    if include_digest:
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
        result["sha256"] = "sha256:" + digest.hexdigest()
    return result


def load_registry(
    path: Path,
    *,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    private_root: Path | None = None,
) -> Registry:
    root = private_root or path.parent
    descriptor = open_private_regular(path, root=root)
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
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
        raise MediaEvidenceError("media registry changed while being read")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError("media registry is not canonical JSON data") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "discovery_boundary",
        "audit_runtime",
        "expected_media",
        "required_online_databases",
    }:
        raise MediaEvidenceError("media registry v3 has an invalid shape")
    if value.get("schema_version") != 3:
        raise MediaEvidenceError("media registry schema is not v3")
    boundary = _load_discovery_boundary(
        value.get("discovery_boundary"),
        policy=policy,
    )
    runtime = value.get("audit_runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {
            "postgres_image",
            "postgres_major",
            "postgres_uid",
            "postgres_gid",
            "auditor_sha256",
            "pg_service_file_sha256",
        }
        or runtime.get("postgres_major") != POSTGRES_MAJOR
        or isinstance(runtime.get("postgres_uid"), bool)
        or not isinstance(runtime.get("postgres_uid"), int)
        or runtime.get("postgres_uid") != POSTGRES_UID
        or isinstance(runtime.get("postgres_gid"), bool)
        or not isinstance(runtime.get("postgres_gid"), int)
        or runtime.get("postgres_gid") != POSTGRES_GID
        or not isinstance(runtime.get("postgres_image"), str)
        or IMAGE_RE.fullmatch(runtime["postgres_image"]) is None
        or not isinstance(runtime.get("auditor_sha256"), str)
        or DIGEST_RE.fullmatch(runtime["auditor_sha256"]) is None
        or not isinstance(runtime.get("pg_service_file_sha256"), str)
        or DIGEST_RE.fullmatch(runtime["pg_service_file_sha256"]) is None
    ):
        raise MediaEvidenceError("media registry audit runtime is not pinned PG16")
    if runtime["auditor_sha256"] != _auditor_digest():
        raise MediaEvidenceError(
            "media registry does not pin this source-pinned auditor"
        )
    raw_descriptors = value.get("expected_media")
    if not isinstance(raw_descriptors, list) or not raw_descriptors:
        raise MediaEvidenceError("media registry expected-media set is empty")
    descriptors: list[MediaDescriptor] = []
    seen: set[str] = set()
    fields = {
        "media_id",
        "kind",
        "database",
        "database_user",
        "disposition",
        "audit_method",
        "pg_service",
        "classification",
        "source_postgres_major",
        "databases",
    }
    for raw in raw_descriptors:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise MediaEvidenceError("media registry descriptor is malformed")
        media_id = raw.get("media_id")
        kind = raw.get("kind")
        database = raw.get("database")
        database_user = raw.get("database_user")
        disposition = raw.get("disposition")
        method = raw.get("audit_method")
        service = raw.get("pg_service")
        classification = raw.get("classification")
        source_postgres_major = raw.get("source_postgres_major")
        raw_databases = raw.get("databases")
        if (
            not isinstance(media_id, str)
            or MEDIA_ID_RE.fullmatch(media_id) is None
            or media_id in seen
            or kind
            not in {
                "docker_volume",
                "container_bind",
                "postgres_backup",
                "reviewed_file",
            }
            or not isinstance(database, str)
            or ROLE_RE.fullmatch(database) is None
            or not isinstance(database_user, str)
            or ROLE_RE.fullmatch(database_user) is None
            or disposition
            not in {
                "writable-target",
                "read-only-online",
                "retained-private-isolated",
                "excluded-from-nexpoly-migration",
            }
            or method
            not in {
                "live-read-only",
                "isolated-volume-copy-read-only",
                "isolated-bind-copy-read-only",
                "isolated-backup-restore-read-only",
                "adjacent-record-only",
                "reviewed-content-only",
                "unsupported-blocking",
            }
            or classification
            not in {
                "nexpoly-db",
                "adjacent-record-only",
                "reviewed-non-pg",
                "unsupported-blocking",
            }
            or source_postgres_major is not None
            and (
                isinstance(source_postgres_major, bool)
                or not isinstance(source_postgres_major, int)
                or not ADJACENT_POSTGRES_MAJOR_MIN
                <= source_postgres_major
                <= ADJACENT_POSTGRES_MAJOR_MAX
            )
            or not isinstance(raw_databases, list)
        ):
            raise MediaEvidenceError("media registry descriptor identity is invalid")
        databases: list[dict[str, object]] = []
        database_names: set[str] = set()
        for database_record in raw_databases:
            if (
                not isinstance(database_record, dict)
                or set(database_record)
                != {
                    "name",
                    "oid",
                    "owner",
                    "allow_connections",
                    "template",
                    "audit_role",
                    "migration_scope",
                }
                or not isinstance(database_record.get("name"), str)
                or ROLE_RE.fullmatch(database_record["name"]) is None
                or database_record["name"] in database_names
                or not isinstance(database_record.get("oid"), str)
                or not database_record["oid"].isdigit()
                or not isinstance(database_record.get("owner"), str)
                or ROLE_RE.fullmatch(database_record["owner"]) is None
                or not isinstance(
                    database_record.get("allow_connections"),
                    bool,
                )
                or database_record.get("template") is not False
                or database_record.get("migration_scope")
                not in {"nexpoly-ledger", "adjacent-record-only"}
                or (
                    database_record.get("allow_connections") is True
                    and (
                        not isinstance(
                            database_record.get("audit_role"),
                            str,
                        )
                        or ROLE_RE.fullmatch(
                            database_record["audit_role"]
                        )
                        is None
                    )
                )
                or (
                    database_record.get("allow_connections") is False
                    and database_record.get("audit_role") is not None
                )
                or (
                    database_record.get("migration_scope")
                    == "nexpoly-ledger"
                    and database_record.get("allow_connections") is not True
                )
            ):
                raise MediaEvidenceError(
                    "media registry database inventory is invalid"
                )
            database_names.add(str(database_record["name"]))
            databases.append(dict(database_record))
        if [record["name"] for record in databases] != sorted(database_names):
            raise MediaEvidenceError(
                "media registry database inventory is not canonical"
            )
        live = method == "live-read-only"
        if (
            classification == "nexpoly-db"
            and live
            and (
                not isinstance(service, str)
                or SERVICE_RE.fullmatch(service) is None
            )
            or classification == "nexpoly-db"
            and not live
            and service is not None
            or kind == "postgres_backup"
            and method
            not in {
                "isolated-backup-restore-read-only",
                "adjacent-record-only",
                "unsupported-blocking",
            }
            or kind == "container_bind"
            and method
            not in {
                "live-read-only",
                "isolated-bind-copy-read-only",
                "adjacent-record-only",
                "reviewed-content-only",
                "unsupported-blocking",
            }
            or kind == "docker_volume"
            and method
            not in {
                "live-read-only",
                "isolated-volume-copy-read-only",
                "adjacent-record-only",
                "reviewed-content-only",
                "unsupported-blocking",
            }
            or kind == "reviewed_file"
            and method not in {"reviewed-content-only", "unsupported-blocking"}
            or disposition == "retained-private-isolated"
            and live
            or classification == "nexpoly-db"
            and disposition != "retained-private-isolated"
            and disposition != "writable-target"
            and disposition != "read-only-online"
            or classification == "nexpoly-db"
            and not live
            and disposition != "retained-private-isolated"
            or classification != "nexpoly-db"
            and (
                disposition != "excluded-from-nexpoly-migration"
                or service is not None
                or databases
                or method
                not in {
                    "adjacent-record-only",
                    "reviewed-content-only",
                    "unsupported-blocking",
                }
            )
            or classification == "adjacent-record-only"
            and (
                method != "adjacent-record-only"
                or kind
                not in {
                    "docker_volume",
                    "container_bind",
                    "postgres_backup",
                }
                or kind == "postgres_backup"
                and source_postgres_major is not None
                or kind != "postgres_backup"
                and source_postgres_major is None
            )
            or classification == "reviewed-non-pg"
            and (
                method != "reviewed-content-only"
                or source_postgres_major is not None
            )
            or classification == "unsupported-blocking"
            and method != "unsupported-blocking"
            or classification == "nexpoly-db"
            and (
                not databases
                or not any(
                    record["name"] == database
                    and record["audit_role"] == database_user
                    and record["migration_scope"] == "nexpoly-ledger"
                    for record in databases
                )
                or (
                    kind == "postgres_backup"
                    and source_postgres_major is not None
                )
                or (
                    kind != "postgres_backup"
                    and source_postgres_major != POSTGRES_MAJOR
                )
            )
        ):
            raise MediaEvidenceError(
                "media registry descriptor audit method conflicts with disposition"
            )
        prefix = {
            "docker_volume": "docker-volume:",
            "container_bind": "container-bind:",
            "postgres_backup": "postgres-backup:",
            "reviewed_file": "reviewed-file:",
        }[kind]
        if not media_id.startswith(prefix):
            raise MediaEvidenceError("media registry ID conflicts with its kind")
        seen.add(media_id)
        descriptors.append(
            MediaDescriptor(
                media_id=media_id,
                kind=str(kind),
                database=database,
                database_user=database_user,
                disposition=disposition,
                audit_method=method,
                pg_service=service,
                classification=classification,
                source_postgres_major=source_postgres_major,
                databases=tuple(databases),
            )
        )
    if [value.media_id for value in descriptors] != sorted(seen):
        raise MediaEvidenceError("media registry descriptors are not canonical")
    writable = [value for value in descriptors if value.disposition == "writable-target"]
    if (
        len(writable) != 1
        or writable[0].database != "nexpoly"
        or writable[0].kind not in {"docker_volume", "container_bind"}
    ):
        raise MediaEvidenceError("media registry lacks one production writable target")
    required_online = value.get("required_online_databases")
    if not isinstance(required_online, list):
        raise MediaEvidenceError("required online database set is invalid")
    allowed_stacks = ("nexpoly_dev", "nexpoly_md_health_opt")
    normalized_online: list[dict[str, str]] = []
    descriptor_by_id = {value.media_id: value for value in descriptors}
    for raw in required_online:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"stack", "media_id"}
            or raw.get("stack") not in allowed_stacks
            or not isinstance(raw.get("media_id"), str)
            or raw["media_id"] not in descriptor_by_id
            or descriptor_by_id[raw["media_id"]].database != raw["stack"]
            or descriptor_by_id[raw["media_id"]].classification
            != "nexpoly-db"
            or descriptor_by_id[raw["media_id"]].audit_method != "live-read-only"
        ):
            raise MediaEvidenceError("required online database mapping is invalid")
        normalized_online.append(
            {"stack": raw["stack"], "media_id": raw["media_id"]}
        )
    stacks = [value["stack"] for value in normalized_online]
    if stacks not in (
        [],
        ["nexpoly_dev"],
        ["nexpoly_md_health_opt"],
        ["nexpoly_dev", "nexpoly_md_health_opt"],
    ):
        raise MediaEvidenceError(
            "registry online database projection is not a canonical subset"
        )
    online_by_stack = {
        mapping["stack"]: mapping["media_id"]
        for mapping in normalized_online
    }
    for stack in allowed_stacks:
        stack_descriptors = [
            descriptor
            for descriptor in descriptors
            if descriptor.database == stack
            and descriptor.classification == "nexpoly-db"
        ]
        if not stack_descriptors:
            raise MediaEvidenceError(
                f"{stack} media is absent from the complete registry"
            )
        online_media_id = online_by_stack.get(stack)
        for descriptor in stack_descriptors:
            if descriptor.media_id == online_media_id:
                if (
                    descriptor.disposition != "read-only-online"
                    or descriptor.audit_method != "live-read-only"
                ):
                    raise MediaEvidenceError(
                        f"{stack} online medium is not explicitly live"
                    )
            elif (
                descriptor.disposition != "retained-private-isolated"
                or descriptor.audit_method == "live-read-only"
            ):
                raise MediaEvidenceError(
                    f"{stack} non-projected media is not retained-isolated"
                )
    return Registry(
        payload=payload,
        digest=sha256_bytes(payload),
        audit_image=runtime["postgres_image"],
        auditor_sha256=runtime["auditor_sha256"],
        service_file_sha256=runtime["pg_service_file_sha256"],
        descriptors=tuple(descriptors),
        required_online_databases=tuple(normalized_online),
        boundary=boundary,
        postgres_uid=runtime["postgres_uid"],
        postgres_gid=runtime["postgres_gid"],
    )


def _json_command(
    runner: CommandRunner,
    arguments: Sequence[str],
    *,
    timeout: int = 120,
) -> object:
    payload = runner.run(arguments, timeout=timeout).stdout
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError("command did not return valid JSON") from exc


def _normalized_container_directory(value: str, *, source: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not candidate.is_absolute()
        or ".." in candidate.parts
        or "\x00" in value
    ):
        raise MediaEvidenceError(
            f"container {source} PostgreSQL data directory is unsafe"
        )
    return candidate.as_posix()


def _postgres_command_data_directories(
    command: Sequence[str],
) -> list[str]:
    candidates: list[str] = []

    def record(raw: str, *, source: str) -> None:
        value = raw.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        candidates.append(
            _normalized_container_directory(value, source=source)
        )

    for index, item in enumerate(command):
        if item in {"-D", "--data-directory", "--pgdata"}:
            if index + 1 >= len(command):
                raise MediaEvidenceError(
                    "container PostgreSQL data-directory option lacks a value"
                )
            record(command[index + 1], source="command")
            continue
        for prefix in (
            "--data-directory=",
            "--data_directory=",
            "--pgdata=",
        ):
            if item.startswith(prefix):
                record(item.split("=", 1)[1], source="command")
                break
        else:
            setting: str | None = None
            if item == "-c":
                if index + 1 >= len(command):
                    raise MediaEvidenceError(
                        "container postgres -c option lacks a value"
                    )
                setting = command[index + 1]
            elif item.startswith("-c") and len(item) > 2:
                setting = item[2:]
            if setting is None:
                continue
            match = re.fullmatch(
                r"\s*data_directory\s*=\s*(.+?)\s*",
                setting,
                flags=re.IGNORECASE,
            )
            if match is not None:
                record(match.group(1), source="postgres -c")
    return candidates


def _container_command_vectors(
    container: Mapping[str, object],
) -> list[list[str]]:
    result: list[list[str]] = []
    path = container.get("Path")
    args = container.get("Args")
    if isinstance(path, str) and isinstance(args, list) and all(
        isinstance(value, str) for value in args
    ):
        result.append([path, *args])
    config = container.get("Config")
    if not isinstance(config, dict):
        return result
    entrypoint = config.get("Entrypoint")
    command = config.get("Cmd")
    configured: list[str] = []
    if isinstance(entrypoint, str):
        configured.append(entrypoint)
    elif isinstance(entrypoint, list) and all(
        isinstance(value, str) for value in entrypoint
    ):
        configured.extend(entrypoint)
    if isinstance(command, str):
        configured.append(command)
    elif isinstance(command, list) and all(
        isinstance(value, str) for value in command
    ):
        configured.extend(command)
    if configured and configured not in result:
        result.append(configured)
    return result


def _container_pgdata(container: Mapping[str, object]) -> str | None:
    config = container.get("Config")
    if not isinstance(config, dict):
        return None
    commands = _container_command_vectors(container)
    command_directories = {
        directory
        for command in commands
        for directory in _postgres_command_data_directories(command)
    }
    if len(command_directories) > 1:
        raise MediaEvidenceError(
            "container has conflicting PostgreSQL data-directory options"
        )
    if command_directories:
        return next(iter(command_directories))
    environment = config.get("Env")
    pgdata: str | None = None
    if isinstance(environment, list):
        for item in environment:
            if isinstance(item, str) and item.startswith("PGDATA="):
                candidate = _normalized_container_directory(
                    item.split("=", 1)[1],
                    source="PGDATA",
                )
                if pgdata is not None and pgdata != candidate:
                    raise MediaEvidenceError(
                        "container has conflicting PGDATA values"
                    )
                pgdata = candidate
    if pgdata is not None:
        return pgdata
    image = config.get("Image")
    labels = config.get("Labels")
    title = (
        labels.get("org.opencontainers.image.title", "")
        if isinstance(labels, dict)
        else ""
    )
    image_name = image.split("@", 1)[0] if isinstance(image, str) else ""
    image_name = image_name.rsplit("/", 1)[-1].split(":", 1)[0].lower()
    actual = commands[0] if commands else []
    executable = PurePosixPath(actual[0]).name.lower() if actual else ""
    first_argument = actual[1].lower() if len(actual) > 1 else ""
    if (
        re.search(r"(^|[-_.])postgres($|[-_.])", image_name)
        or executable in {"postgres", "docker-entrypoint.sh"}
        and first_argument == "postgres"
        or isinstance(title, str)
        and title.lower() in {"postgres", "postgresql"}
    ):
        return "/var/lib/postgresql/data"
    mounts = container.get("Mounts")
    if isinstance(mounts, list) and any(
        isinstance(mount, dict)
        and mount.get("Destination") == "/var/lib/postgresql/data"
        for mount in mounts
    ):
        return "/var/lib/postgresql/data"
    return None


def _canonical_bind_source(value: object) -> str:
    if not isinstance(value, str):
        raise MediaEvidenceError("Docker bind source is invalid")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or "\x00" in value
    ):
        raise MediaEvidenceError("Docker bind source is unsafe")
    return path.as_posix()


def _mount_pg_subpath(destination: str, pgdata: str) -> str | None:
    mount = PurePosixPath(destination)
    target = PurePosixPath(pgdata)
    if not mount.is_absolute() or not target.is_absolute():
        return None
    try:
        relative = target.relative_to(mount)
    except ValueError:
        return None
    value = "." if not relative.parts else relative.as_posix()
    if (
        value != "."
        and (
            ".." in PurePosixPath(value).parts
            or re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", value) is None
        )
    ):
        raise MediaEvidenceError("container PGDATA subpath is unsafe")
    return value


def _mount_destination_overlaps_pgdata(
    destination: str,
    pgdata: str,
) -> bool:
    mount = PurePosixPath(
        _normalized_container_directory(
            destination,
            source="mount destination",
        )
    )
    target = PurePosixPath(
        _normalized_container_directory(
            pgdata,
            source="PGDATA",
        )
    )
    try:
        mount.relative_to(target)
        return True
    except ValueError:
        pass
    try:
        target.relative_to(mount)
        return True
    except ValueError:
        return False


def _docker_inventory(
    runner: CommandRunner,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    container_ids = [
        value
        for value in runner.run(
            [DOCKER, "ps", "-aq", "--no-trunc"],
            timeout=60,
        )
        .stdout.decode("ascii", "strict")
        .splitlines()
        if value
    ]
    if any(CONTAINER_RE.fullmatch(value) is None for value in container_ids):
        raise MediaEvidenceError("Docker returned a malformed container ID")
    containers: list[dict[str, object]] = []
    for container_id in sorted(set(container_ids)):
        value = _json_command(
            runner,
            [DOCKER, "container", "inspect", "--", container_id],
        )
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
            or value[0].get("Id") != container_id
        ):
            raise MediaEvidenceError("Docker container inspect identity differs")
        containers.append(value[0])
    volume_names = [
        value
        for value in runner.run(
            [DOCKER, "volume", "ls", "--format", "{{.Name}}"],
            timeout=60,
        )
        .stdout.decode("utf-8", "strict")
        .splitlines()
        if value
    ]
    if any(VOLUME_RE.fullmatch(value) is None for value in volume_names):
        raise MediaEvidenceError("Docker returned a malformed volume name")
    volumes: list[dict[str, object]] = []
    for volume_name in sorted(set(volume_names)):
        value = _json_command(
            runner,
            [DOCKER, "volume", "inspect", "--", volume_name],
        )
        if (
            not isinstance(value, list)
            or len(value) != 1
            or not isinstance(value[0], dict)
            or value[0].get("Name") != volume_name
        ):
            raise MediaEvidenceError("Docker volume inspect identity differs")
        volumes.append(value[0])
    return containers, volumes


def _container_identity(container: Mapping[str, object]) -> dict[str, object]:
    config = container.get("Config")
    host_config = container.get("HostConfig")
    state = container.get("State")
    status = state.get("Status") if isinstance(state, dict) else None
    started_at = state.get("StartedAt") if isinstance(state, dict) else None
    finished_at = state.get("FinishedAt") if isinstance(state, dict) else None
    container_id = container.get("Id")
    name = container.get("Name")
    image_id = container.get("Image")
    created_at = container.get("Created")
    path = container.get("Path")
    arguments = container.get("Args")
    restart_count = container.get("RestartCount")
    if (
        not isinstance(container_id, str)
        or CONTAINER_RE.fullmatch(container_id) is None
        or not isinstance(name, str)
        or not name.startswith("/")
        or len(name) < 2
        or not isinstance(image_id, str)
        or DIGEST_RE.fullmatch(image_id) is None
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(path, str)
        or not isinstance(arguments, list)
        or any(not isinstance(value, str) for value in arguments)
        or not isinstance(config, dict)
        or not isinstance(host_config, dict)
        or not isinstance(status, str)
        or not status
        or not isinstance(started_at, str)
        or not started_at
        or not isinstance(finished_at, str)
        or not finished_at
        or isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 0
    ):
        raise MediaEvidenceError("Docker container identity is incomplete")
    config_identity = {
        "id": container_id,
        "name": name,
        "image_id": image_id,
        "created_at": created_at,
        "path": path,
        "arguments": arguments,
        "config": config,
        "host_config": host_config,
    }
    return {
        "container_id": container_id,
        "container_name": name,
        "container_image_id": image_id,
        "container_config_sha256": sha256_bytes(
            canonical_json_bytes(config_identity)
        ),
        "container_created_at": created_at,
        "container_started_at": started_at,
        "container_finished_at": finished_at,
        "container_restart_count": restart_count,
        "state": status,
    }


def _attached_record(
    container: Mapping[str, object],
    mount: Mapping[str, object],
) -> dict[str, object]:
    identity = _container_identity(container)
    destination = mount.get("Destination")
    if (
        not isinstance(destination, str)
        or not PurePosixPath(destination).is_absolute()
    ):
        raise MediaEvidenceError("Docker attachment identity is incomplete")
    return {
        **identity,
        "destination": destination,
        "read_only": mount.get("RW") is False,
    }


def _docker_boundary_document(
    containers: Sequence[Mapping[str, object]],
    volumes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    container_records: list[dict[str, object]] = []
    for container in containers:
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or any(
            not isinstance(value, dict) for value in mounts
        ):
            raise MediaEvidenceError("Docker container mounts are invalid")
        container_records.append(
            {
                **_container_identity(container),
                "mounts_sha256": sha256_bytes(
                    canonical_json_bytes(
                        sorted(mounts, key=canonical_json_bytes)
                    )
                ),
            }
        )
    volume_records: list[dict[str, object]] = []
    for volume in volumes:
        name = volume.get("Name")
        if not isinstance(name, str) or VOLUME_RE.fullmatch(name) is None:
            raise MediaEvidenceError("Docker volume identity is invalid")
        volume_records.append(
            {
                "name": name,
                "inspect_sha256": sha256_bytes(canonical_json_bytes(volume)),
            }
        )
    return {
        "containers": sorted(
            container_records,
            key=lambda value: str(value["container_id"]),
        ),
        "volumes": sorted(
            volume_records,
            key=lambda value: str(value["name"]),
        ),
    }


def _probe_volume_signature(
    runner: CommandRunner,
    image: str,
    volume_name: str,
    *,
    operation: ScratchOperation,
    resource_key: str,
) -> dict[str, object]:
    """Classify a physical PostgreSQL signature without starting PostgreSQL."""

    completed = operation.run_container(
        resource_key,
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,src={volume_name},dst=/source,readonly",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ceu",
            (
                "find /source -xdev "
                "\\( -type f -name PG_VERSION "
                "-o -type f -path '*/global/pg_control' "
                "-o -type d -name base \\) "
                "-print | LC_ALL=C sort | "
                "while IFS= read -r path; do "
                "case \"$path\" in "
                "*/PG_VERSION) "
                "printf 'V\\t%s\\t' \"$path\"; "
                "tr -d '\\r\\n' < \"$path\"; printf '\\n' ;; "
                "*/global/pg_control) printf 'C\\t%s\\n' \"$path\" ;; "
                "*/base) printf 'B\\t%s\\n' \"$path\" ;; "
                "esac; done"
            ),
        ],
        mounts=(
            {
                "kind": "volume",
                "source": volume_name,
                "destination": "/source",
                "read_only": True,
            },
        ),
        source_media_id=f"docker-volume:{volume_name}",
    )
    roots: dict[str, dict[str, object]] = {}
    for line in completed.stdout.decode("utf-8", "strict").splitlines():
        fields = line.split("\t")
        if (
            len(fields) not in {2, 3}
            or fields[0] not in {"V", "C", "B"}
            or not fields[1].startswith("/source/")
        ):
            raise MediaEvidenceError(
                f"volume PostgreSQL signature output is invalid: {volume_name}"
            )
        marker = PurePosixPath(fields[1])
        if fields[0] == "V":
            root = marker.parent
        elif fields[0] == "C":
            if marker.parent.name != "global":
                raise MediaEvidenceError(
                    "volume pg_control marker path is invalid"
                )
            root = marker.parent.parent
        else:
            root = marker.parent
        record = roots.setdefault(
            root.as_posix(),
            {"version": None, "pg_control": False, "base": False},
        )
        if fields[0] == "V":
            if (
                len(fields) != 3
                or not fields[2].isdigit()
                or record["version"] is not None
            ):
                raise MediaEvidenceError(
                    "volume PG_VERSION marker is invalid"
                )
            record["version"] = int(fields[2])
        elif fields[0] == "C":
            record["pg_control"] = True
        else:
            record["base"] = True
    if not roots:
        return {
            "signature": "non-postgres",
            "data_subpath": ".",
            "postgres_major": None,
        }

    # PostgreSQL 16 stores a PG_VERSION file in each database directory below
    # PGDATA/base in addition to the cluster-level PG_VERSION.  Those nested
    # files are not independent clusters and must not turn every healthy
    # physical cluster into a false "damaged-postgres" result.  Ignore only
    # version-only descendants of the one complete cluster's base directory;
    # any second complete root or any other partial marker still fails closed.
    complete_roots = [
        root
        for root, markers in roots.items()
        if isinstance(markers["version"], int)
        and markers["pg_control"]
        and markers["base"]
    ]
    if len(complete_roots) != 1:
        return {
            "signature": "damaged-postgres",
            "data_subpath": ".",
            "postgres_major": None,
        }
    root_value = complete_roots[0]
    cluster_base = PurePosixPath(root_value) / "base"
    unexpected_roots = []
    for candidate, candidate_markers in roots.items():
        if candidate == root_value:
            continue
        candidate_path = PurePosixPath(candidate)
        try:
            candidate_path.relative_to(cluster_base)
        except ValueError:
            nested_database_version = False
        else:
            nested_database_version = (
                isinstance(candidate_markers["version"], int)
                and not candidate_markers["pg_control"]
                and not candidate_markers["base"]
            )
        if not nested_database_version:
            unexpected_roots.append(candidate)
    if unexpected_roots:
        return {
            "signature": "damaged-postgres",
            "data_subpath": ".",
            "postgres_major": None,
        }
    markers = roots[root_value]
    relative = PurePosixPath(root_value).relative_to("/source")
    value = "." if not relative.parts else relative.as_posix()
    if (
        value != "."
        and (
            ".." in PurePosixPath(value).parts
            or re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", value) is None
        )
    ):
        raise MediaEvidenceError("volume PGDATA subpath is unsafe")
    version = markers["version"]
    if (
        not isinstance(version, int)
        or not markers["pg_control"]
        or not markers["base"]
    ):
        return {
            "signature": "damaged-postgres",
            "data_subpath": value,
            "postgres_major": version if isinstance(version, int) else None,
        }
    return {
        "signature": "postgres",
        "data_subpath": value,
        "postgres_major": version,
    }


def _probe_volume_pgdata(
    runner: CommandRunner,
    image: str,
    volume_name: str,
    *,
    operation: ScratchOperation,
    resource_key: str,
) -> str | None:
    signature = _probe_volume_signature(
        runner,
        image,
        volume_name,
        operation=operation,
        resource_key=resource_key,
    )
    if signature["signature"] == "non-postgres":
        return None
    if signature["signature"] != "postgres":
        raise MediaEvidenceError(
            f"volume has a damaged PostgreSQL signature: {volume_name}"
        )
    return str(signature["data_subpath"])


def _format_for_backup(path: Path, policy: DiscoveryPolicy, header: bytes) -> str | None:
    for name, suffixes in policy.backup_formats:
        if any(path.name.endswith(suffix) for suffix in suffixes):
            if name == "postgres-custom-v1" and not header.startswith(b"PGDMP"):
                raise MediaEvidenceError(f"custom backup magic differs: {path}")
            if name == "postgres-tar-v1" and (
                len(header) < 265 or header[257:262] != b"ustar"
            ):
                raise MediaEvidenceError(f"tar backup magic differs: {path}")
            return name
    return None


def _bind_postgres_signature(
    source: Path,
    data_subpath: str,
) -> dict[str, object]:
    root = (
        source
        if data_subpath == "."
        else source.joinpath(*PurePosixPath(data_subpath).parts)
    )
    markers: dict[str, object] = {
        "version": None,
        "pg_control": False,
        "base": False,
    }
    version_path = root / "PG_VERSION"
    try:
        descriptor = open_private_regular(version_path, root=source)
    except FileNotFoundError:
        pass
    else:
        try:
            payload = _read_fd(descriptor, 32)
        finally:
            os.close(descriptor)
        try:
            value = payload.decode("ascii").strip()
        except UnicodeError as exc:
            raise MediaEvidenceError("bind PG_VERSION is invalid") from exc
        if not value.isdigit():
            raise MediaEvidenceError("bind PG_VERSION is invalid")
        markers["version"] = int(value)
    try:
        descriptor = open_private_regular(
            root / "global/pg_control",
            root=source,
        )
    except FileNotFoundError:
        pass
    else:
        os.close(descriptor)
        markers["pg_control"] = True
    try:
        descriptor = _open_directory_chain(
            root / "base",
            private_from=source,
        )
    except FileNotFoundError:
        pass
    else:
        os.close(descriptor)
        markers["base"] = True
    if not any(
        (
            markers["version"] is not None,
            markers["pg_control"],
            markers["base"],
        )
    ):
        signature = "non-postgres"
    elif all(
        (
            isinstance(markers["version"], int),
            markers["pg_control"],
            markers["base"],
        )
    ):
        signature = "postgres"
    else:
        signature = "damaged-postgres"
    return {
        "signature": signature,
        "data_subpath": data_subpath,
        "postgres_major": markers["version"],
    }


def _walk_backup_root(
    root: Path,
    policy: DiscoveryPolicy,
    *,
    root_authority: object,
) -> tuple[list[DiscoveredMedia], list[dict[str, object]]]:
    try:
        root_descriptor = _open_sealed_backup_root(
            root,
            root_authority,
        )
    except FileNotFoundError as exc:
        raise MediaEvidenceError(
            "approved backup root is missing; expected an existing "
            f"deploy-user-owned mode-0700 directory: {root}"
        ) from exc
    except MediaEvidenceError as exc:
        raise MediaEvidenceError(
            "approved backup root is unsafe; expected a "
            f"deploy-user-owned mode-0700 directory: {root}"
        ) from exc
    except OSError as exc:
        raise MediaEvidenceError(
            "approved backup root is inaccessible; expected an existing "
            f"deploy-user-owned mode-0700 directory: {root}"
        ) from exc
    media: list[DiscoveredMedia] = []
    scanned: list[dict[str, object]] = []

    def visit(descriptor: int, relative: tuple[str, ...]) -> None:
        try:
            names = sorted(os.listdir(descriptor))
        except OSError as exc:
            raise MediaEvidenceError(f"cannot enumerate backup root: {root}") from exc
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise MediaEvidenceError("backup entry has an invalid name")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                metadata = os.fstat(child)
                path = root.joinpath(*relative, name)
                if (
                    metadata.st_uid != os.geteuid()
                    or stat.S_ISDIR(metadata.st_mode)
                    and stat.S_IMODE(metadata.st_mode) != 0o700
                    or stat.S_ISREG(metadata.st_mode)
                    and (
                        stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_nlink != 1
                    )
                ):
                    raise MediaEvidenceError(
                        f"backup private parent chain is unsafe: {path}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    visit(child, (*relative, name))
                elif stat.S_ISREG(metadata.st_mode):
                    header = os.read(child, 512)
                    backup_format = _format_for_backup(path, policy, header)
                    scanned.append(
                        {
                            "path": str(path),
                            "device": metadata.st_dev,
                            "inode": metadata.st_ino,
                            "size_bytes": metadata.st_size,
                            "mtime_ns": metadata.st_mtime_ns,
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "recognized_format": backup_format,
                        }
                    )
                    if backup_format is not None:
                        media.append(
                            DiscoveredMedia(
                                media_id=f"postgres-backup:{path}",
                                kind="postgres_backup",
                                locator=str(path),
                                data_subpath=".",
                                attached=(),
                                backup_format=backup_format,
                                signature="postgres-backup",
                                postgres_major=None,
                            )
                        )
                    else:
                        media.append(
                            DiscoveredMedia(
                                media_id=f"reviewed-file:{path}",
                                kind="reviewed_file",
                                locator=str(path),
                                data_subpath=".",
                                attached=(),
                                signature="non-postgres",
                                postgres_major=None,
                            )
                        )
                else:
                    raise MediaEvidenceError(
                        f"backup tree contains a symlink or special entry: {path}"
                    )
            finally:
                os.close(child)

    try:
        visit(root_descriptor, ())
    finally:
        os.close(root_descriptor)
    return media, scanned


def discover_media(
    registry: Registry,
    *,
    runner: CommandRunner,
    operation: ScratchOperation,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
) -> Discovery:
    containers, volumes = _docker_inventory(runner)
    descriptor_by_id = {
        descriptor.media_id: descriptor
        for descriptor in registry.descriptors
    }
    volume_attachments: dict[str, list[dict[str, object]]] = {}
    bind_attachments: dict[str, list[dict[str, object]]] = {}
    pg_mounts: dict[tuple[str, str], tuple[str, list[dict[str, object]]]] = {}
    bind_mounts: dict[str, tuple[str, list[dict[str, object]]]] = {}
    candidate_extra_volumes: set[str] = set()
    candidate_extra_binds: set[str] = set()
    unresolved_pg_candidates: list[str] = []
    for container in containers:
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise MediaEvidenceError("Docker container mounts are invalid")
        pgdata = _container_pgdata(container)
        matched_pgdata = False
        for raw_mount in mounts:
            if not isinstance(raw_mount, dict):
                raise MediaEvidenceError("Docker mount record is invalid")
            mount_type = raw_mount.get("Type")
            source = raw_mount.get("Source")
            name = raw_mount.get("Name")
            destination = raw_mount.get("Destination")
            if not isinstance(destination, str):
                raise MediaEvidenceError("Docker mount destination is invalid")
            attachment = _attached_record(container, raw_mount)
            if mount_type == "volume" and isinstance(name, str):
                volume_attachments.setdefault(name, []).append(attachment)
            elif mount_type == "bind":
                canonical_source = _canonical_bind_source(source)
                bind_attachments.setdefault(
                    canonical_source,
                    [],
                ).append(attachment)
            if pgdata is None:
                continue
            subpath = _mount_pg_subpath(destination, pgdata)
            if subpath is None:
                if mount_type == "volume" and isinstance(name, str):
                    candidate_extra_volumes.add(name)
                elif mount_type == "bind":
                    canonical_source = _canonical_bind_source(source)
                    if (
                        raw_mount.get("RW") is not False
                        or _mount_destination_overlaps_pgdata(
                            destination,
                            pgdata,
                        )
                    ):
                        candidate_extra_binds.add(canonical_source)
                continue
            matched_pgdata = True
            if mount_type == "volume" and isinstance(name, str):
                key = ("docker_volume", name)
                previous = pg_mounts.get(key)
                if previous is not None and previous[0] != subpath:
                    raise MediaEvidenceError("one volume maps to conflicting PGDATA roots")
                pg_mounts[key] = (
                    subpath,
                    [*(previous[1] if previous else []), attachment],
                )
            elif mount_type == "bind":
                canonical_source = _canonical_bind_source(source)
                previous = bind_mounts.get(canonical_source)
                if previous is not None and previous[0] != subpath:
                    raise MediaEvidenceError("one bind maps to conflicting PGDATA roots")
                bind_mounts[canonical_source] = (
                    subpath,
                    [*(previous[1] if previous else []), attachment],
                )
        if pgdata is not None and not matched_pgdata:
            unresolved_pg_candidates.append(str(container.get("Id")))
    if unresolved_pg_candidates:
        raise MediaEvidenceError(
            "PostgreSQL-candidate containers do not expose a reviewable PGDATA mount: "
            + ", ".join(sorted(unresolved_pg_candidates))
        )
    unclassified_binds = sorted(candidate_extra_binds - set(bind_mounts))
    if unclassified_binds:
        raise MediaEvidenceError(
            "PostgreSQL-candidate container has an unclassified persistent "
            f"bind mount: {unclassified_binds!r}"
        )

    discovered: dict[str, DiscoveredMedia] = {}
    volume_names = {str(value.get("Name")) for value in volumes}
    for value in volumes:
        name = value.get("Name")
        if not isinstance(name, str):
            raise MediaEvidenceError("Docker volume name is invalid")
        media_id = f"docker-volume:{name}"
        attachments = sorted(
            volume_attachments.get(name, []),
            key=lambda item: (str(item["container_id"]), str(item["destination"])),
        )
        known = pg_mounts.get(("docker_volume", name))
        signature = _probe_volume_signature(
            runner,
            registry.audit_image,
            name,
            operation=operation,
            resource_key=(
                "discover-volume-"
                + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
                + "-probe"
            ),
        )
        if known is not None:
            subpath, pg_attachments = known
            if sorted(pg_attachments, key=canonical_json_bytes) != sorted(
                attachments, key=canonical_json_bytes
            ):
                # A PostgreSQL volume mounted a second time outside PGDATA is an
                # ambiguous reader and must be reviewed explicitly.
                raise MediaEvidenceError(
                    f"PostgreSQL volume has an unclassified attachment: {name}"
                )
            inactive_reviewed_non_pg = (
                signature["signature"] == "non-postgres"
                and all(
                    str(attachment["state"])
                    not in ACTIVE_CONTAINER_STATES
                    for attachment in attachments
                )
                and (
                    descriptor_by_id.get(media_id)
                    is not None
                    and descriptor_by_id[media_id].classification
                    == "reviewed-non-pg"
                    and descriptor_by_id[media_id].audit_method
                    == "reviewed-content-only"
                )
            )
            if inactive_reviewed_non_pg:
                subpath = "."
            elif (
                signature["signature"] != "postgres"
                or signature["data_subpath"] != subpath
            ):
                raise MediaEvidenceError(
                    f"container PGDATA conflicts with volume contents: {name}"
                )
        else:
            if signature["signature"] == "non-postgres":
                if name in candidate_extra_volumes:
                    raise MediaEvidenceError(
                        "PostgreSQL-candidate container has an unclassified "
                        f"persistent volume without PG_VERSION: {name}"
                    )
                subpath = "."
            else:
                subpath = str(signature["data_subpath"])
            if signature["signature"] == "postgres" and any(
                str(attachment["state"]) in ACTIVE_CONTAINER_STATES
                for attachment in attachments
            ):
                raise MediaEvidenceError(
                    "an active PostgreSQL volume lacks an exact PGDATA "
                    f"container mapping: {name}"
                )
        discovered[media_id] = DiscoveredMedia(
            media_id=media_id,
            kind="docker_volume",
            locator=name,
            data_subpath=subpath,
            attached=tuple(attachments),
            signature=str(signature["signature"]),
            postgres_major=(
                int(signature["postgres_major"])
                if isinstance(signature["postgres_major"], int)
                else None
            ),
        )
    for source, (subpath, attachments) in sorted(bind_mounts.items()):
        all_attachments = bind_attachments.get(source, [])
        if sorted(attachments, key=canonical_json_bytes) != sorted(
            all_attachments,
            key=canonical_json_bytes,
        ):
            raise MediaEvidenceError(
                "PostgreSQL bind has an unclassified attachment: "
                f"{source}"
            )
        path = Path(source)
        _absolute_parts(path)
        signature = _bind_postgres_signature(path, subpath)
        if signature["signature"] != "postgres":
            raise MediaEvidenceError(
                f"container PGDATA bind has a damaged signature: {source}"
            )
        media_id = f"container-bind:{path}"
        discovered[media_id] = DiscoveredMedia(
            media_id=media_id,
            kind="container_bind",
            locator=str(path),
            data_subpath=subpath,
            attached=tuple(
                sorted(
                    all_attachments,
                    key=lambda item: (
                        str(item["container_id"]),
                        str(item["destination"]),
                    ),
                )
            ),
            signature=str(signature["signature"]),
            postgres_major=(
                int(signature["postgres_major"])
                if isinstance(signature["postgres_major"], int)
                else None
            ),
        )
    backup_scan: list[dict[str, object]] = []
    for root in policy.backup_roots:
        if root == FORBIDDEN_BACKUP_ROOT:
            raise MediaEvidenceError("operation rollback root cannot be scanned as legacy")
        values, scanned = _walk_backup_root(
            root,
            policy,
            root_authority=_sealed_root_authority(registry, root),
        )
        backup_scan.extend(scanned)
        for value in values:
            if value.media_id in discovered:
                raise MediaEvidenceError("duplicate external media identity")
            discovered[value.media_id] = value

    expected = {value.media_id for value in registry.descriptors}
    if set(discovered) != expected:
        missing = sorted(expected - set(discovered))
        additional = sorted(set(discovered) - expected)
        raise MediaEvidenceError(
            "discovered media differs from registry "
            f"(missing={missing!r}, additional={additional!r})"
        )
    for descriptor in registry.descriptors:
        value = discovered[descriptor.media_id]
        if value.kind != descriptor.kind:
            raise MediaEvidenceError("discovered media kind differs from registry")
        if descriptor.classification in {
            "nexpoly-db",
            "adjacent-record-only",
        }:
            if (
                value.kind == "postgres_backup"
                and (
                    value.signature != "postgres-backup"
                    or value.postgres_major is not None
                    or descriptor.source_postgres_major is not None
                )
                or value.kind != "postgres_backup"
                and (
                    value.signature != "postgres"
                    or value.postgres_major
                    != descriptor.source_postgres_major
                )
            ):
                raise MediaEvidenceError(
                    "PostgreSQL media signature or major differs from registry"
                )
        if descriptor.classification == "reviewed-non-pg" and (
            value.signature != "non-postgres"
            or value.postgres_major is not None
        ):
            raise MediaEvidenceError(
                "reviewed non-PG medium has a PostgreSQL signature"
            )
        if descriptor.classification == "unsupported-blocking":
            raise MediaEvidenceError(
                f"registry explicitly blocks unsupported medium: {value.media_id}"
            )
        active = [
            attachment
            for attachment in value.attached
            if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
        ]
        if descriptor.audit_method == "live-read-only" and len(active) != 1:
            raise MediaEvidenceError(
                "online media must have exactly one active PostgreSQL reader: "
                f"{value.media_id}"
            )
        if (
            descriptor.classification == "nexpoly-db"
            and descriptor.audit_method != "live-read-only"
            and active
        ):
            raise MediaEvidenceError(
                f"isolated media has an active reader: {value.media_id}"
            )
    docker_document = _docker_boundary_document(containers, volumes)
    return Discovery(
        media=discovered,
        docker_inventory_sha256=sha256_bytes(canonical_json_bytes(docker_document)),
        backup_inventory_sha256=sha256_bytes(
            canonical_json_bytes(sorted(backup_scan, key=lambda item: item["path"]))
        ),
        scanned_volume_names=tuple(sorted(volume_names)),
        scanned_container_ids=tuple(
            sorted(str(value["Id"]) for value in containers)
        ),
    )


def analyze_ledger(
    ledger: object,
    *,
    legacy_relation_present: bool,
    isolated: bool,
) -> dict[str, object]:
    if not isinstance(ledger, list):
        raise MediaEvidenceError("migration ledger is not a list")
    raw: list[tuple[str, str]] = []
    for row in ledger:
        if (
            not isinstance(row, dict)
            or set(row) != {"version", "checksum"}
            or not isinstance(row.get("version"), str)
            or MIGRATION_RE.fullmatch(row["version"]) is None
            or not isinstance(row.get("checksum"), str)
            or re.fullmatch(r"[0-9a-f]{64}", row["checksum"]) is None
        ):
            raise MediaEvidenceError("migration ledger row is invalid")
        raw.append((row["version"], row["checksum"]))
    if raw != sorted(raw) or len({version for version, _checksum in raw}) != len(raw):
        raise MediaEvidenceError("migration ledger order or uniqueness is invalid")
    alias_rows = [value for value in raw if value[0] == LEGACY_0005_ALIAS_VERSION]
    if alias_rows and alias_rows != [
        (LEGACY_0005_ALIAS_VERSION, LEGACY_0005_ALIAS_CHECKSUM)
    ]:
        raise MediaEvidenceError("historical 0005 alias identity differs")
    stripped = [value for value in raw if value[0] != LEGACY_0005_ALIAS_VERSION]
    if len(stripped) > len(CANONICAL_MIGRATION_LEDGER):
        raise MediaEvidenceError("migration ledger extends beyond known history")
    mismatches: list[dict[str, str]] = []
    normalized: list[tuple[str, str]] = []
    for index, observed in enumerate(stripped):
        expected = CANONICAL_MIGRATION_LEDGER[index]
        if observed[0] != expected[0]:
            raise MediaEvidenceError("migration ledger is not a contiguous prefix")
        checksum = observed[1]
        if checksum == expected[1]:
            normalized.append(observed)
            continue
        if (
            observed[0] == "0009_monomer_md_job_leases"
            and checksum == KNOWN_DIRTY_0009_CHECKSUM
        ):
            status = "known-isolated-dirty"
        elif (
            observed[0] == "0013_monomer_dft_jobs"
            and checksum == SUPERSEDED_0013_CHECKSUM
        ):
            status = "superseded-requires-0014"
        else:
            raise MediaEvidenceError("migration ledger contains an unknown checksum")
        mismatches.append(
            {
                "version": observed[0],
                "expected_checksum": expected[1],
                "observed_checksum": checksum,
                "status": status,
            }
        )
        normalized.append(observed)
    versions = {version for version, _checksum in stripped}
    if alias_rows and (
        "0007_polytao_jobs" not in versions or "0012_drop_polytao_jobs" in versions
    ):
        raise MediaEvidenceError(
            "historical 0005 alias exists outside its exact pre-0012 epoch"
        )
    expected_relation = (
        "0007_polytao_jobs" in versions
        and "0012_drop_polytao_jobs" not in versions
    )
    if legacy_relation_present is not expected_relation:
        raise MediaEvidenceError(
            "legacy relation presence conflicts with the migration prefix"
        )
    if any(value["status"] == "known-isolated-dirty" for value in mismatches):
        if not isolated:
            raise MediaEvidenceError("known dirty 0009 medium is not isolated")
        status = "known-isolated-dirty"
    elif any(
        value["status"] == "superseded-requires-0014"
        for value in mismatches
    ):
        status = "superseded-requires-0014"
    elif not stripped:
        if not isolated:
            raise MediaEvidenceError("empty migration ledger is not isolated")
        status = "empty-isolated"
    elif alias_rows:
        status = "canonical-with-historical-0005-alias"
    else:
        status = "canonical-prefix"
    migration_0013 = next(
        (checksum for version, checksum in stripped if version == "0013_monomer_dft_jobs"),
        None,
    )
    if migration_0013 is None:
        migration_0013_record = {"state": "absent", "checksum": None}
    elif migration_0013 == CANONICAL_0013_CHECKSUM:
        migration_0013_record = {
            "state": "canonical",
            "checksum": migration_0013,
        }
    else:
        migration_0013_record = {
            "state": "superseded-requires-0014",
            "checksum": migration_0013,
        }
    return {
        "status": status,
        "canonical_prefix_length": len(stripped),
        "historical_0005_alias_present": bool(alias_rows),
        "checksum_mismatches": mismatches,
        "migration_0013": migration_0013_record,
        "requires_0014": migration_0013 == SUPERSEDED_0013_CHECKSUM,
    }


DATABASE_AUDIT_SQL = r"""
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
SELECT json_build_object(
  'record_type', 'database',
  'database', current_database(),
  'current_user', current_user,
  'transaction_read_only', current_setting('transaction_read_only')::boolean,
  'role_superuser', role.rolsuper,
  'role_create_db', role.rolcreatedb,
  'role_create_role', role.rolcreaterole,
  'system_identifier', control.system_identifier::text,
  'database_oid', database.oid::text,
  'database_owner', pg_get_userbyid(database.datdba),
  'encoding', pg_encoding_to_char(database.encoding),
  'collate', database.datcollate,
  'ctype', database.datctype,
  'server_version_num', current_setting('server_version_num')::integer,
  'data_directory', current_setting('data_directory')
)
FROM pg_roles AS role
CROSS JOIN pg_control_system() AS control
JOIN pg_database AS database ON database.datname = current_database()
WHERE role.rolname = current_user;
\set ledger_present false
SELECT (to_regclass('governance.schema_migrations') IS NOT NULL) AS ledger_present \gset
\if :ledger_present
SELECT json_build_object(
  'record_type', 'ledger',
  'rows', COALESCE((
    SELECT json_agg(
      json_build_object('version', version, 'checksum', checksum)
      ORDER BY version, checksum
    )
    FROM governance.schema_migrations
  ), '[]'::json),
  'relation', json_build_object(
    'oid', relation.oid::text,
    'kind', relation.relkind,
    'owner', pg_get_userbyid(relation.relowner),
    'columns', COALESCE((
      SELECT json_agg(
        json_build_object(
          'number', attribute.attnum,
          'name', attribute.attname,
          'type', format_type(attribute.atttypid, attribute.atttypmod),
          'not_null', attribute.attnotnull,
          'identity', attribute.attidentity,
          'generated', attribute.attgenerated,
          'default', pg_get_expr(default_value.adbin, default_value.adrelid)
        ) ORDER BY attribute.attnum
      )
      FROM pg_attribute AS attribute
      LEFT JOIN pg_attrdef AS default_value
        ON default_value.adrelid = attribute.attrelid
       AND default_value.adnum = attribute.attnum
      WHERE attribute.attrelid = relation.oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ), '[]'::json),
    'indexes', COALESCE((
      SELECT json_agg(pg_get_indexdef(index_value.indexrelid) ORDER BY index_value.indexrelid)
      FROM pg_index AS index_value
      WHERE index_value.indrelid = relation.oid
    ), '[]'::json),
    'constraints', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', constraint_value.conname,
          'type', constraint_value.contype,
          'definition', pg_get_constraintdef(constraint_value.oid, true)
        ) ORDER BY constraint_value.conname
      )
      FROM pg_constraint AS constraint_value
      WHERE constraint_value.conrelid = relation.oid
    ), '[]'::json)
  )
)
FROM pg_class AS relation
WHERE relation.oid = 'governance.schema_migrations'::regclass;
\else
SELECT json_build_object(
  'record_type', 'ledger',
  'rows', '[]'::json,
  'relation', null
);
\endif
SELECT (to_regclass('generation.polytao_jobs') IS NOT NULL) AS legacy_present \gset
\if :legacy_present
SELECT json_build_object(
  'record_type', 'legacy_relation',
  'present', true,
  'relation', json_build_object(
    'oid', relation.oid::text,
    'kind', relation.relkind,
    'owner', pg_get_userbyid(relation.relowner),
    'columns', COALESCE((
      SELECT json_agg(
        json_build_object(
          'number', attribute.attnum,
          'name', attribute.attname,
          'type', format_type(attribute.atttypid, attribute.atttypmod),
          'not_null', attribute.attnotnull,
          'identity', attribute.attidentity,
          'generated', attribute.attgenerated,
          'default', pg_get_expr(default_value.adbin, default_value.adrelid)
        ) ORDER BY attribute.attnum
      )
      FROM pg_attribute AS attribute
      LEFT JOIN pg_attrdef AS default_value
        ON default_value.adrelid = attribute.attrelid
       AND default_value.adnum = attribute.attnum
      WHERE attribute.attrelid = relation.oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
    ), '[]'::json),
    'indexes', COALESCE((
      SELECT json_agg(pg_get_indexdef(index_value.indexrelid) ORDER BY index_value.indexrelid)
      FROM pg_index AS index_value
      WHERE index_value.indrelid = relation.oid
    ), '[]'::json),
    'constraints', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', constraint_value.conname,
          'type', constraint_value.contype,
          'definition', pg_get_constraintdef(constraint_value.oid, true)
        ) ORDER BY constraint_value.conname
      )
      FROM pg_constraint AS constraint_value
      WHERE constraint_value.conrelid = relation.oid
    ), '[]'::json)
  ),
  'rows', COALESCE((
    SELECT json_agg(to_jsonb(value) ORDER BY to_jsonb(value)::text COLLATE "C")
    FROM generation.polytao_jobs AS value
  ), '[]'::json)
)
FROM pg_class AS relation
WHERE relation.oid = 'generation.polytao_jobs'::regclass;
\else
SELECT json_build_object(
  'record_type', 'legacy_relation',
  'present', false,
  'relation', null,
  'rows', '[]'::json
);
\endif
COMMIT;
"""

DATABASE_INVENTORY_SQL = r"""
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
SELECT json_build_object(
  'record_type', 'database_inventory',
  'databases', COALESCE((
    SELECT json_agg(
      json_build_object(
        'name', database.datname,
        'oid', database.oid::text,
        'owner', pg_get_userbyid(database.datdba),
        'allow_connections', database.datallowconn,
        'template', database.datistemplate
      )
      ORDER BY database.datname COLLATE "C"
    )
    FROM pg_database AS database
    WHERE NOT database.datistemplate
  ), '[]'::json)
);
COMMIT;
"""


LEDGER_RELATION_COLUMNS = (
    ("version", "text", True, None),
    ("checksum", "text", True, None),
    ("applied_at", "timestamp with time zone", True, "now()"),
)
LEDGER_RELATION_INDEXES = {
    "schema_migrations_pkey": (
        "CREATE UNIQUE INDEX schema_migrations_pkey ON "
        "governance.schema_migrations USING btree (version)"
    ),
}
LEDGER_RELATION_CONSTRAINTS = {
    "schema_migrations_pkey": ("p", "PRIMARY KEY (version)"),
}
LEGACY_RELATION_COLUMNS = (
    ("job_id", "text", True, None),
    ("status", "text", True, "'pending'::text"),
    ("input_smiles", "text", False, None),
    ("canonical_smiles", "text", False, None),
    ("descriptor_prompt", "text", True, None),
    ("descriptors", "jsonb", True, "'{}'::jsonb"),
    ("request_data", "jsonb", True, "'{}'::jsonb"),
    ("requested_count", "integer", True, "10"),
    ("returned_count", "integer", True, "0"),
    ("attempts", "integer", True, "0"),
    ("progress_percent", "integer", True, "0"),
    ("progress_stage", "text", True, "'pending'::text"),
    (
        "progress_message",
        "text",
        True,
        "'Waiting for the PolyTAO backend runtime to start.'::text",
    ),
    ("worker_id", "text", False, None),
    ("worker_job_id", "text", False, None),
    ("worker_version", "text", False, None),
    ("engine", "text", True, "'polytao-backend'::text"),
    ("result_data", "jsonb", False, None),
    ("error_message", "text", False, None),
    ("created_at", "timestamp with time zone", True, "now()"),
    ("updated_at", "timestamp with time zone", True, "now()"),
    ("started_at", "timestamp with time zone", False, None),
    ("finished_at", "timestamp with time zone", False, None),
)
LEGACY_RELATION_INDEXES = {
    "polytao_jobs_pkey": (
        "CREATE UNIQUE INDEX polytao_jobs_pkey ON "
        "generation.polytao_jobs USING btree (job_id)"
    ),
    "idx_polytao_jobs_status": (
        "CREATE INDEX idx_polytao_jobs_status ON "
        "generation.polytao_jobs USING btree (status)"
    ),
    "idx_polytao_jobs_created_at": (
        "CREATE INDEX idx_polytao_jobs_created_at ON "
        "generation.polytao_jobs USING btree (created_at DESC)"
    ),
}
LEGACY_RELATION_CONSTRAINTS = {
    "polytao_jobs_pkey": ("p", "PRIMARY KEY (job_id)"),
    "polytao_jobs_status_check": (
        "c",
        (
            "CHECK (status = ANY (ARRAY['pending'::text, "
            "'submitted'::text, 'running'::text, 'completed'::text, "
            "'failed'::text, 'cancelled'::text]))"
        ),
    ),
    "polytao_jobs_requested_count_check": (
        "c",
        "CHECK (requested_count > 0)",
    ),
    "polytao_jobs_returned_count_check": (
        "c",
        "CHECK (returned_count >= 0)",
    ),
    "polytao_jobs_attempts_check": ("c", "CHECK (attempts >= 0)"),
    "polytao_jobs_progress_percent_check": (
        "c",
        "CHECK (progress_percent >= 0 AND progress_percent <= 100)",
    ),
}


def _relation_index_name(definition: object) -> str:
    if not isinstance(definition, str):
        raise MediaEvidenceError("database relation index evidence is invalid")
    match = re.match(
        r"^CREATE (?:UNIQUE )?INDEX ([a-z_][a-z0-9_]*) ON ",
        definition,
    )
    if match is None:
        raise MediaEvidenceError("database relation index definition is invalid")
    return match.group(1)


def _validated_relation_authority(
    relation: object,
    *,
    expected_owner: str,
    legacy_relation: bool,
) -> dict[str, object]:
    if not isinstance(relation, dict) or set(relation) != {
        "oid",
        "kind",
        "owner",
        "columns",
        "indexes",
        "constraints",
    }:
        raise MediaEvidenceError("database relation authority shape is invalid")
    if (
        relation.get("kind") != "r"
        or relation.get("owner") != expected_owner
        or not isinstance(relation.get("oid"), str)
        or not str(relation["oid"]).isdigit()
    ):
        raise MediaEvidenceError(
            "database relation is not an approved owner ordinary table"
        )
    raw_columns = relation.get("columns")
    raw_indexes = relation.get("indexes")
    raw_constraints = relation.get("constraints")
    if (
        not isinstance(raw_columns, list)
        or not isinstance(raw_indexes, list)
        or not isinstance(raw_constraints, list)
    ):
        raise MediaEvidenceError("database relation schema evidence is invalid")
    columns: list[tuple[str, str, bool, object]] = []
    for position, column in enumerate(raw_columns, start=1):
        if (
            not isinstance(column, dict)
            or set(column)
            != {
                "number",
                "name",
                "type",
                "not_null",
                "identity",
                "generated",
                "default",
            }
            or column.get("number") != position
            or column.get("identity") != ""
            or column.get("generated") != ""
            or not isinstance(column.get("name"), str)
            or not isinstance(column.get("type"), str)
            or not isinstance(column.get("not_null"), bool)
            or not isinstance(column.get("default"), (str, type(None)))
        ):
            raise MediaEvidenceError(
                "database relation column authority is invalid"
            )
        columns.append(
            (
                str(column["name"]),
                str(column["type"]),
                bool(column["not_null"]),
                column["default"],
            )
        )
    if legacy_relation:
        if tuple(columns) != LEGACY_RELATION_COLUMNS:
            raise MediaEvidenceError(
                "legacy relation columns differ from migration authority"
            )
        expected_indexes = LEGACY_RELATION_INDEXES
        expected_constraints = LEGACY_RELATION_CONSTRAINTS
    else:
        if tuple(columns) != LEDGER_RELATION_COLUMNS:
            raise MediaEvidenceError(
                "ledger relation columns differ from migration authority"
            )
        expected_indexes = LEDGER_RELATION_INDEXES
        expected_constraints = LEDGER_RELATION_CONSTRAINTS
    indexes = {
        _relation_index_name(value): value
        for value in raw_indexes
    }
    if (
        len(indexes) != len(raw_indexes)
        or indexes != expected_indexes
    ):
        raise MediaEvidenceError(
            "database relation indexes differ from migration authority"
        )
    constraints: dict[str, tuple[str, str]] = {}
    for constraint in raw_constraints:
        if (
            not isinstance(constraint, dict)
            or set(constraint) != {"name", "type", "definition"}
            or not isinstance(constraint.get("name"), str)
            or not isinstance(constraint.get("type"), str)
            or not isinstance(constraint.get("definition"), str)
            or not constraint["definition"]
        ):
            raise MediaEvidenceError(
                "database relation constraint authority is invalid"
            )
        constraints[str(constraint["name"])] = (
            str(constraint["type"]),
            str(constraint["definition"]),
        )
    if constraints != expected_constraints:
        raise MediaEvidenceError(
            "database relation constraints differ from migration authority"
        )
    authority = {
        "relation_kind": "ordinary-table",
        "owner": expected_owner,
        "columns": [
            {
                "name": name,
                "type": data_type,
                "not_null": not_null,
                "default": default_value,
            }
            for name, data_type, not_null, default_value in columns
        ],
        "indexes": [
            {"name": name, "definition": indexes[name]}
            for name in sorted(indexes)
        ],
        "constraints": [
            {
                "name": name,
                "type": constraints[name][0],
                "definition": constraints[name][1],
            }
            for name in sorted(constraints)
        ],
    }
    return {
        "authority": authority,
        "authority_sha256": sha256_bytes(canonical_json_bytes(authority)),
    }


def _parse_database_audit(
    payload: bytes,
    *,
    expected_database: str,
    expected_user: str,
    isolated: bool,
    migration_scope: str = "nexpoly-ledger",
) -> dict[str, object]:
    if len(payload) > MAX_DATABASE_JSON_BYTES:
        raise MediaEvidenceError("database audit output exceeds its limit")
    values: dict[str, dict[str, object]] = {}
    for raw_line in payload.decode("utf-8", "strict").splitlines():
        if not raw_line.startswith("{"):
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise MediaEvidenceError("database audit emitted malformed JSON") from exc
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("record_type"), str)
            or value["record_type"] in values
        ):
            raise MediaEvidenceError("database audit record identity is invalid")
        values[value["record_type"]] = value
    if set(values) != {"database", "ledger", "legacy_relation"}:
        raise MediaEvidenceError("database audit output is incomplete")
    database = values["database"]
    server_version_num = database.get("server_version_num")
    if (
        database.get("database") != expected_database
        or database.get("current_user") != expected_user
        or database.get("transaction_read_only") is not True
        or database.get("role_superuser") is not isolated
        or not isinstance(database.get("role_create_db"), bool)
        or not isinstance(database.get("role_create_role"), bool)
        or not isinstance(database.get("system_identifier"), str)
        or PG_SYSTEM_ID_RE.fullmatch(database["system_identifier"]) is None
        or isinstance(server_version_num, bool)
        or not isinstance(server_version_num, int)
        or server_version_num // 10000 != POSTGRES_MAJOR
        or not isinstance(database.get("data_directory"), str)
        or not PurePosixPath(database["data_directory"]).is_absolute()
    ):
        raise MediaEvidenceError("database audit identity or read-only role differs")
    if not isolated and (
        database["role_superuser"]
        or database["role_create_db"]
        or database["role_create_role"]
    ):
        raise MediaEvidenceError("online database audit role is privileged")
    ledger_record = values["ledger"]
    ledger = ledger_record.get("rows")
    relation = ledger_record.get("relation")
    legacy = values["legacy_relation"]
    if (
        relation is not None
        and not isinstance(relation, dict)
        or not isinstance(legacy.get("present"), bool)
    ):
        raise MediaEvidenceError("database relation evidence is malformed")
    relation_rows = legacy.get("rows")
    if not isinstance(relation_rows, list):
        raise MediaEvidenceError("legacy relation row evidence is malformed")
    legacy_relation = legacy.get("relation")
    if legacy["present"] != (legacy_relation is not None):
        raise MediaEvidenceError("legacy relation evidence is inconsistent")
    ledger_authority = (
        _validated_relation_authority(
            relation,
            expected_owner=str(database["database_owner"]),
            legacy_relation=False,
        )
        if relation is not None
        else None
    )
    legacy_authority = (
        _validated_relation_authority(
            legacy_relation,
            expected_owner=str(database["database_owner"]),
            legacy_relation=True,
        )
        if legacy_relation is not None
        else None
    )
    if migration_scope == "nexpoly-ledger":
        analysis = analyze_ledger(
            ledger,
            legacy_relation_present=legacy["present"],
            isolated=isolated,
        )
    elif migration_scope == "adjacent-record-only":
        if relation is not None or ledger != [] or legacy["present"]:
            raise MediaEvidenceError(
                "adjacent database contains unclassified Nexpoly relations"
            )
        analysis = {
            "status": "adjacent-no-nexpoly-relations",
            "canonical_prefix_length": 0,
            "historical_0005_alias_present": False,
            "checksum_mismatches": [],
            "migration_0013": {"state": "absent", "checksum": None},
            "requires_0014": False,
        }
    else:
        raise MediaEvidenceError("database migration scope is invalid")
    database_identity = {
        key: database[key]
        for key in (
            "database",
            "system_identifier",
            "database_oid",
            "database_owner",
            "encoding",
            "collate",
            "ctype",
            "server_version_num",
            "data_directory",
        )
    }
    return {
        "database_identity": database_identity,
        "database_identity_sha256": sha256_bytes(
            canonical_json_bytes(database_identity)
        ),
        "current_user": database["current_user"],
        "transaction_read_only": True,
        "role_superuser": database["role_superuser"],
        "role_create_db": database["role_create_db"],
        "role_create_role": database["role_create_role"],
        "ledger": ledger,
        "ledger_sha256": sha256_bytes(canonical_json_bytes(ledger)),
        "ledger_relation": {
            "state": "present" if relation is not None else "absent",
            "row_count": len(ledger) if isinstance(ledger, list) else None,
            "schema_sha256": (
                ledger_authority["authority_sha256"]
                if ledger_authority is not None
                else None
            ),
            "schema_authority": (
                ledger_authority["authority"]
                if ledger_authority is not None
                else None
            ),
            "content_sha256": sha256_bytes(canonical_json_bytes(ledger)),
        },
        "ledger_analysis": {
            key: analysis[key]
            for key in (
                "status",
                "canonical_prefix_length",
                "historical_0005_alias_present",
                "checksum_mismatches",
            )
        },
        "legacy_relation_present": legacy["present"],
        "legacy_relation": {
            "state": "present" if legacy["present"] else "absent",
            "row_count": len(relation_rows) if legacy["present"] else None,
            "schema_sha256": (
                legacy_authority["authority_sha256"]
                if legacy_authority is not None
                else None
            ),
            "schema_authority": (
                legacy_authority["authority"]
                if legacy_authority is not None
                else None
            ),
            "content_sha256": (
                sha256_bytes(canonical_json_bytes(relation_rows))
                if legacy["present"]
                else None
            ),
        },
        "migration_0013": analysis["migration_0013"],
        "requires_0014": analysis["requires_0014"],
    }


def _parse_database_inventory(payload: bytes) -> list[dict[str, object]]:
    if len(payload) > MAX_DATABASE_JSON_BYTES:
        raise MediaEvidenceError("database inventory output exceeds its limit")
    values: list[dict[str, object]] | None = None
    for raw_line in payload.decode("utf-8", "strict").splitlines():
        if not raw_line.startswith("{"):
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise MediaEvidenceError(
                "database inventory emitted malformed JSON"
            ) from exc
        if (
            values is not None
            or not isinstance(value, dict)
            or set(value) != {"record_type", "databases"}
            or value.get("record_type") != "database_inventory"
            or not isinstance(value.get("databases"), list)
        ):
            raise MediaEvidenceError("database inventory record is invalid")
        values = value["databases"]
    if values is None:
        raise MediaEvidenceError("database inventory output is incomplete")
    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    for record in values:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "name",
                "oid",
                "owner",
                "allow_connections",
                "template",
            }
            or not isinstance(record.get("name"), str)
            or ROLE_RE.fullmatch(record["name"]) is None
            or record["name"] in names
            or not isinstance(record.get("oid"), str)
            or not record["oid"].isdigit()
            or not isinstance(record.get("owner"), str)
            or ROLE_RE.fullmatch(record["owner"]) is None
            or not isinstance(record.get("allow_connections"), bool)
            or record.get("template") is not False
        ):
            raise MediaEvidenceError("database inventory entry is invalid")
        names.add(str(record["name"]))
        normalized.append(dict(record))
    if [record["name"] for record in normalized] != sorted(names):
        raise MediaEvidenceError("database inventory is not canonical")
    return normalized


def _container_database_inventory(
    runner: CommandRunner,
    container_id: str,
) -> list[dict[str, object]]:
    completed = runner.run(
        [
            DOCKER,
            "exec",
            "--user",
            "postgres",
            "-i",
            container_id,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            "/var/run/postgresql",
            "-U",
            "postgres",
            "-d",
            "postgres",
        ],
        input_bytes=DATABASE_INVENTORY_SQL.encode("utf-8"),
        timeout=600,
    )
    return _parse_database_inventory(completed.stdout)


def _declared_database_inventory(
    descriptor: MediaDescriptor,
) -> list[dict[str, object]]:
    return [
        {
            key: record[key]
            for key in (
                "name",
                "oid",
                "owner",
                "allow_connections",
                "template",
            )
        }
        for record in descriptor.databases
    ]


def _validate_database_inventory(
    descriptor: MediaDescriptor,
    observed: list[dict[str, object]],
) -> list[dict[str, object]]:
    declared = _declared_database_inventory(descriptor)
    if observed != declared:
        raise MediaEvidenceError(
            "complete database inventory differs from registry authority"
        )
    return observed


def _database_authority(
    descriptor: MediaDescriptor,
    name: str,
) -> dict[str, object]:
    matches = [
        dict(record)
        for record in descriptor.databases
        if record["name"] == name
    ]
    if len(matches) != 1:
        raise MediaEvidenceError("database is absent from registry authority")
    return matches[0]


def _database_inventory_record(
    authority: dict[str, object],
    audit: dict[str, object] | None,
) -> dict[str, object]:
    common = {
        key: authority[key]
        for key in (
            "name",
            "oid",
            "owner",
            "allow_connections",
            "template",
            "audit_role",
            "migration_scope",
        )
    }
    if audit is None:
        return {
            **common,
            "audit_state": "not-connectable-record-only",
            "audit": None,
        }
    return {
        **common,
        "audit_state": "complete",
        "audit": audit,
    }


def _audit_container_medium(
    runner: CommandRunner,
    container_id: str,
    descriptor: MediaDescriptor,
    *,
    isolated: bool,
    expected_data_directory: str | None,
    logical_backup: bool = False,
) -> dict[str, object]:
    # Direct unit-level orchestration tests construct descriptors without the
    # v3 authority. Registry-loaded production calls can never take this
    # compatibility branch because load_registry requires a full inventory.
    if not descriptor.databases:
        primary = _audit_container_database(
            runner,
            container_id,
            descriptor,
            isolated=isolated,
        )
        return {
            **primary,
            "database_inventory": [
                {
                    "name": descriptor.database,
                    "oid": primary["database_identity"]["database_oid"],
                    "owner": primary["database_identity"]["database_owner"],
                    "allow_connections": True,
                    "template": False,
                }
            ],
            "databases": [
                {
                    "name": descriptor.database,
                    "oid": primary["database_identity"]["database_oid"],
                    "owner": primary["database_identity"]["database_owner"],
                    "allow_connections": True,
                    "template": False,
                    "audit_role": descriptor.database_user,
                    "migration_scope": "nexpoly-ledger",
                    "audit_state": "complete",
                    "audit": primary,
                }
            ],
        }
    observed = _container_database_inventory(runner, container_id)
    if logical_backup:
        # A logical backup represents only the restored target. The fresh
        # scratch cluster's administrative postgres database is not source
        # media and must not leak into its inventory.
        declared_names = {
            str(record["name"]) for record in descriptor.databases
        }
        observed = [
            record
            for record in observed
            if record["name"] in declared_names
        ]
    inventory = _validate_database_inventory(descriptor, observed)
    audited: list[dict[str, object]] = []
    primary: dict[str, object] | None = None
    for authority in descriptor.databases:
        database_audit: dict[str, object] | None
        if authority["allow_connections"] is not True:
            if authority["migration_scope"] == "nexpoly-ledger":
                raise MediaEvidenceError(
                    "Nexpoly database does not allow a complete audit"
                )
            database_audit = None
        else:
            database_audit = _audit_container_database(
                runner,
                container_id,
                descriptor,
                database_authority=authority,
                isolated=isolated,
            )
            if (
                expected_data_directory is not None
                and database_audit["database_identity"]["data_directory"]
                != expected_data_directory
            ):
                raise MediaEvidenceError(
                    "database audit data_directory differs from exact PGDATA"
                )
        record = _database_inventory_record(authority, database_audit)
        audited.append(record)
        if authority["name"] == descriptor.database:
            if database_audit is None:
                raise MediaEvidenceError(
                    "primary Nexpoly database cannot be record-only"
                )
            primary = database_audit
    if primary is None:
        raise MediaEvidenceError("primary database audit is absent")
    return {
        **primary,
        "database_inventory": inventory,
        "database_inventory_sha256": sha256_bytes(
            canonical_json_bytes(inventory)
        ),
        "databases": audited,
    }


def _run_live_audit(
    runner: CommandRunner,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
) -> dict[str, object]:
    attachments = _current_attachments(runner, source)
    active = [
        value
        for value in attachments
        if str(value["state"]) in ACTIVE_CONTAINER_STATES
    ]
    if len(active) != 1:
        raise MediaEvidenceError(
            "live database audit lacks one exact active PGDATA container"
        )
    attachment = active[0]
    destination = PurePosixPath(str(attachment["destination"]))
    expected_pgdata = (
        destination
        if source.data_subpath == "."
        else destination.joinpath(*PurePosixPath(source.data_subpath).parts)
    )
    result = _audit_container_medium(
        runner,
        str(attachment["container_id"]),
        descriptor,
        isolated=False,
        expected_data_directory=str(expected_pgdata),
    )
    return result


def _current_attachments(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> list[dict[str, object]]:
    current: list[dict[str, object]] = []
    identifiers = [
        value
        for value in runner.run(
            [DOCKER, "ps", "-aq", "--no-trunc"],
            timeout=60,
        )
        .stdout.decode("ascii", "strict")
        .splitlines()
        if value
    ]
    if any(CONTAINER_RE.fullmatch(value) is None for value in identifiers):
        raise MediaEvidenceError(
            "Docker returned malformed container IDs while fencing readers"
        )
    for container_id in sorted(set(identifiers)):
        container = _optional_docker_inspect(
            runner,
            "container",
            container_id,
        )
        if container is None:
            raise MediaEvidenceError(
                "Docker container inventory changed while fencing readers"
            )
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise MediaEvidenceError("attached Docker container mounts are invalid")
        for mount in mounts:
            if not isinstance(mount, dict):
                raise MediaEvidenceError("attached Docker mount is invalid")
            if source.kind == "docker_volume":
                exact_source = (
                    mount.get("Type") == "volume"
                    and mount.get("Name") == source.locator
                )
            elif source.kind == "container_bind":
                exact_source = (
                    mount.get("Type") == "bind"
                    and _canonical_bind_source(mount.get("Source"))
                    == source.locator
                )
            else:
                raise MediaEvidenceError("backup cannot have Docker attachments")
            if not exact_source:
                continue
            current.append(_attached_record(container, mount))
    normalized = sorted(
        current,
        key=lambda item: (
            str(item["container_id"]),
            str(item["destination"]),
        ),
    )
    expected = sorted(
        source.attached,
        key=lambda item: (
            str(item["container_id"]),
            str(item["destination"]),
        ),
    )
    if normalized != expected:
        raise MediaEvidenceError(
            "Docker source attachment set changed or contains an "
            "unclassified reader"
        )
    return normalized


def _docker_volume_identity(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> dict[str, object]:
    value = _json_command(
        runner,
        [DOCKER, "volume", "inspect", "--", source.locator],
    )
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise MediaEvidenceError("Docker volume identity is malformed")
    volume = value[0]
    if volume.get("Name") != source.locator:
        raise MediaEvidenceError("Docker volume identity changed")
    labels = volume.get("Labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        raise MediaEvidenceError("Docker volume labels are invalid")
    return {
        "name": source.locator,
        "driver": volume.get("Driver"),
        "mountpoint": volume.get("Mountpoint"),
        "labels_sha256": sha256_bytes(canonical_json_bytes(labels)),
        "inspect_sha256": sha256_bytes(canonical_json_bytes(volume)),
        "data_subpath": source.data_subpath,
        "attached": _current_attachments(runner, source),
    }


def _live_source_system_identifier(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> str:
    attachments = _current_attachments(runner, source)
    active = [
        value
        for value in attachments
        if str(value["state"]) in ACTIVE_CONTAINER_STATES
    ]
    if len(active) != 1:
        raise MediaEvidenceError(
            "online source lacks one exact active PostgreSQL container"
        )
    attachment = active[0]
    destination = PurePosixPath(str(attachment["destination"]))
    pgdata = (
        destination
        if source.data_subpath == "."
        else destination.joinpath(*PurePosixPath(source.data_subpath).parts)
    )
    completed = runner.run(
        [
            DOCKER,
            "exec",
            "--user",
            "postgres",
            str(attachment["container_id"]),
            "pg_controldata",
            "-D",
            str(pgdata),
        ],
        timeout=60,
    )
    match = re.search(
        rb"(?m)^Database system identifier:\s*([0-9]{1,20})\s*$",
        completed.stdout,
    )
    if match is None:
        raise MediaEvidenceError(
            "online container pg_controldata identity is unavailable"
        )
    return match.group(1).decode("ascii")


def _volume_content_digest(
    runner: CommandRunner,
    image: str,
    source: DiscoveredMedia,
    *,
    operation: ScratchOperation,
    resource_key: str,
) -> str:
    completed = operation.run_container(
        resource_key,
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,src={source.locator},dst=/source,readonly",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ceu",
            (
                "set -o pipefail; "
                "test -z \"$(find /source -xdev -type l -print -quit)\"; "
                "cd /source; "
                "find . -xdev -mindepth 1 -print0 | LC_ALL=C sort -z | "
                "while IFS= read -r -d '' path; do "
                "if test -d \"$path\"; then "
                "printf 'D\\0%s\\0' \"$path\"; "
                "stat -c '%f\\0%u\\0%g\\0' \"$path\"; "
                "elif test -f \"$path\"; then "
                "printf 'F\\0%s\\0' \"$path\"; "
                "stat -c '%f\\0%u\\0%g\\0%s\\0' \"$path\"; "
                "sha256sum \"$path\" | cut -d ' ' -f 1; "
                "else exit 97; fi; done | sha256sum"
            ),
        ],
        mounts=(
            {
                "kind": "volume",
                "source": source.locator,
                "destination": "/source",
                "read_only": True,
            },
        ),
        source_media_id=source.media_id,
    )
    fields = completed.stdout.decode("ascii", "strict").strip().split()
    if len(fields) != 2 or re.fullmatch(r"[0-9a-f]{64}", fields[0]) is None:
        raise MediaEvidenceError("volume content digest output is invalid")
    return "sha256:" + fields[0]


def _temp_name(prefix: str) -> str:
    return f"nexpoly-audit-{prefix}-{secrets.token_hex(12)}"


def _wait_for_postgres(
    runner: CommandRunner,
    container_id: str,
    *,
    database: str,
    user: str,
) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        # On an empty official-image volume, docker-entrypoint.sh starts a
        # temporary server, stops it after initialization, and only then execs
        # the final PostgreSQL process as PID 1.  A plain pg_isready can race
        # that temporary server and let the next command hit the restart gap.
        final_process = runner.run(
            [
                DOCKER,
                "exec",
                "--user",
                "postgres",
                container_id,
                "/bin/sh",
                "-ceu",
                'test "$(cat /proc/1/comm)" = postgres',
            ],
            timeout=10,
            check=False,
        )
        if final_process.returncode != 0:
            time.sleep(1)
            continue
        result = runner.run(
            [
                DOCKER,
                "exec",
                "--user",
                "postgres",
                container_id,
                "pg_isready",
                "-h",
                "/var/run/postgresql",
                "-d",
                database,
                "-U",
                user,
            ],
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise MediaEvidenceError("isolated PostgreSQL 16 audit container did not start")


def _isolated_postgres_arguments(
    *,
    pgdata: str,
    hba_file: str | None = None,
) -> list[str]:
    values = [
        "postgres",
        "-c",
        f"data_directory={pgdata}",
        "-c",
        "listen_addresses=",
        "-c",
        "unix_socket_directories=/var/run/postgresql",
        "-c",
        "ssl=off",
        "-c",
        "logging_collector=off",
        "-c",
        "shared_preload_libraries=",
        "-c",
        "session_preload_libraries=",
        "-c",
        "local_preload_libraries=",
        "-c",
        "archive_command=/bin/false",
        "-c",
        "restore_command=/bin/false",
    ]
    if hba_file is not None:
        values.extend(["-c", f"hba_file={hba_file}"])
    return values


def _audit_container_database(
    runner: CommandRunner,
    container_id: str,
    descriptor: MediaDescriptor,
    *,
    database_authority: dict[str, object] | None = None,
    isolated: bool = True,
) -> dict[str, object]:
    if database_authority is None:
        database_name = descriptor.database
        audit_role = descriptor.database_user
        migration_scope = "nexpoly-ledger"
    else:
        database_name = str(database_authority["name"])
        audit_role = database_authority.get("audit_role")
        migration_scope = str(database_authority["migration_scope"])
        if (
            not isinstance(audit_role, str)
            or ROLE_RE.fullmatch(audit_role) is None
        ):
            raise MediaEvidenceError(
                "connectable database lacks an approved audit role"
            )
    connect_user = audit_role if isolated else "postgres"
    sql = DATABASE_AUDIT_SQL
    if not isolated:
        sql = sql.replace(
            (
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ "
                "READ ONLY DEFERRABLE;"
            ),
            (
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ "
                f'READ ONLY DEFERRABLE; SET LOCAL ROLE "{audit_role}";'
            ),
            1,
        )
    completed = runner.run(
        [
            DOCKER,
            "exec",
            "--user",
            "postgres",
            "-i",
            container_id,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            "/var/run/postgresql",
            "-U",
            connect_user,
            "-d",
            database_name,
        ],
        input_bytes=sql.encode("utf-8"),
        timeout=600,
    )
    return _parse_database_audit(
        completed.stdout,
        expected_database=database_name,
        expected_user=audit_role,
        isolated=isolated,
        migration_scope=migration_scope,
    )


def _isolated_volume_audit(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    *,
    operation: ScratchOperation,
    resource_prefix: str,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    before = _docker_volume_identity(runner, source)
    if any(
        str(value["state"]) in ACTIVE_CONTAINER_STATES
        for value in before["attached"]
    ):
        raise MediaEvidenceError("active volume cannot enter isolated audit")
    before_digest = _volume_content_digest(
        runner,
        registry.audit_image,
        source,
        operation=operation,
        resource_key=f"{resource_prefix}-source-digest-before",
    )
    volume_key = f"{resource_prefix}-volume"
    clone = operation.create_volume(
        volume_key,
        source_media_id=source.media_id,
    )
    container_id: str | None = None
    try:
        operation.run_container(
            f"{resource_prefix}-copy",
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--mount",
                f"type=volume,src={source.locator},dst=/source,readonly",
                "--mount",
                f"type=volume,src={clone},dst=/copy",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                (
                    "set -o pipefail; "
                    "tar -C /source -cpf - . | "
                    "tar -C /copy -xpf -"
                ),
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": source.locator,
                    "destination": "/source",
                    "read_only": True,
                },
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": "/copy",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
        )
        clone_source = DiscoveredMedia(
            media_id=f"docker-volume:{clone}",
            kind="docker_volume",
            locator=clone,
            data_subpath=source.data_subpath,
            attached=(),
        )
        if (
            _volume_content_digest(
                runner,
                registry.audit_image,
                clone_source,
                operation=operation,
                resource_key=f"{resource_prefix}-clone-digest",
            )
            != before_digest
        ):
            raise MediaEvidenceError(
                "disposable physical-volume copy digest differs"
            )
        operation.run_container(
            f"{resource_prefix}-chown",
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={clone},dst=/copy",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                "chown -R postgres:postgres /copy",
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": "/copy",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
            read_only_rootfs=False,
        )
        hba_path = (
            "/var/lib/postgresql/data/"
            + ("" if source.data_subpath == "." else source.data_subpath + "/")
            + ".nexpoly-audit-pg_hba.conf"
        )
        data_path = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        operation.run_container(
            f"{resource_prefix}-hba",
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                (
                    f"rm -f '{data_path}/postmaster.pid'; "
                    f"printf '%s\\n' 'local all all trust' > '{hba_path}'; "
                    f"chown postgres:postgres '{hba_path}'; chmod 0600 '{hba_path}'"
                ),
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": "/var/lib/postgresql/data",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
            read_only_rootfs=False,
        )
        pgdata = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        container_key = f"{resource_prefix}-postgres"
        completed = operation.run_container(
            container_key,
            [
                DOCKER,
                "run",
                "-d",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                _postgres_socket_tmpfs(registry),
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--env",
                f"PGDATA={pgdata}",
                registry.audit_image,
                *_isolated_postgres_arguments(
                    pgdata=pgdata,
                    hba_file=hba_path,
                ),
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": "/var/lib/postgresql/data",
                    "read_only": False,
                },
                {
                    "kind": "tmpfs",
                    "source": None,
                    "destination": "/var/run/postgresql",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
            detached=True,
        )
        container_id = completed.stdout.decode("ascii", "strict").strip()
        if CONTAINER_RE.fullmatch(container_id) is None:
            raise MediaEvidenceError("isolated audit container ID is invalid")
        _wait_for_postgres(
            runner,
            container_id,
            database=descriptor.database,
            user=descriptor.database_user,
        )
        database = _audit_container_medium(
            runner,
            container_id,
            descriptor,
            isolated=True,
            expected_data_directory=pgdata,
        )
    finally:
        if any(
            value["resource_key"] == f"{resource_prefix}-postgres"
            for value in operation.journal["resources"]  # type: ignore[union-attr]
        ):
            operation.remove_resource(f"{resource_prefix}-postgres")
        operation.remove_resource(volume_key)
    after = _docker_volume_identity(runner, source)
    after_digest = _volume_content_digest(
        runner,
        registry.audit_image,
        source,
        operation=operation,
        resource_key=f"{resource_prefix}-source-digest-after",
    )
    if before != after or before_digest != after_digest:
        raise MediaEvidenceError("source volume changed during isolated audit")
    isolation = {
        "source_mounted_read_only": True,
        "source_started_as_postgres": False,
        "scratch_network": "none",
        "scratch_destroyed": True,
        "copy_method": "readonly-tar-copy-to-disposable-volume-v1",
    }
    return database, before_digest, before, after, isolation


def _copy_backup_snapshot(
    source: Path,
    *,
    root: Path,
    root_authority: object,
    destination: Path,
) -> tuple[dict[str, object], str]:
    descriptor = open_sealed_backup_regular(
        source,
        root=root,
        root_authority=root_authority,
    )
    try:
        before = _fd_identity(descriptor, source, include_digest=True)
        digest = before["sha256"]
        target_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        copied_digest = hashlib.sha256()
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                _write_all(target_descriptor, chunk)
                copied_digest.update(chunk)
            os.fsync(target_descriptor)
        finally:
            os.close(target_descriptor)
        after_same_fd = _fd_identity(descriptor, source, include_digest=True)
    finally:
        os.close(descriptor)
    if before != after_same_fd:
        raise MediaEvidenceError("backup changed while creating isolated snapshot")
    if "sha256:" + copied_digest.hexdigest() != digest:
        raise MediaEvidenceError("isolated backup snapshot digest differs")
    return before, str(digest)


def _find_backup_root(path: Path, policy: DiscoveryPolicy) -> Path:
    parts = _absolute_parts(path)
    matches = [
        root
        for root in policy.backup_roots
        if parts[: len(_absolute_parts(root))] == _absolute_parts(root)
    ]
    if len(matches) != 1:
        raise MediaEvidenceError("backup is outside one fixed approved root")
    return matches[0]


def _isolated_backup_audit(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    workspace: Path,
    *,
    policy: DiscoveryPolicy,
    operation: ScratchOperation,
    resource_prefix: str,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    source_path = Path(source.locator)
    backup_root = _find_backup_root(source_path, policy)
    root_authority = _sealed_root_authority(registry, backup_root)
    staged = workspace / "source.backup"
    before, source_digest = _copy_backup_snapshot(
        source_path,
        root=backup_root,
        root_authority=root_authority,
        destination=staged,
    )
    volume_key = f"{resource_prefix}-volume"
    scratch_volume = operation.create_volume(
        volume_key,
        source_media_id=source.media_id,
    )
    container_id: str | None = None
    container_key = f"{resource_prefix}-postgres"
    try:
        completed = operation.run_container(
            container_key,
            [
                DOCKER,
                "run",
                "-d",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                _postgres_socket_tmpfs(registry),
                "--mount",
                f"type=volume,src={scratch_volume},dst=/var/lib/postgresql/data",
                "--mount",
                f"type=bind,src={workspace},dst=/source-audit,readonly",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                f"POSTGRES_USER={descriptor.database_user}",
                registry.audit_image,
                "postgres",
                "-c",
                "listen_addresses=",
                "-c",
                "unix_socket_directories=/var/run/postgresql",
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": scratch_volume,
                    "destination": "/var/lib/postgresql/data",
                    "read_only": False,
                },
                {
                    "kind": "bind",
                    "source": str(workspace),
                    "destination": "/source-audit",
                    "read_only": True,
                },
                {
                    "kind": "tmpfs",
                    "source": None,
                    "destination": "/var/run/postgresql",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
            detached=True,
        )
        container_id = completed.stdout.decode("ascii", "strict").strip()
        if CONTAINER_RE.fullmatch(container_id) is None:
            raise MediaEvidenceError("restore container ID is invalid")
        _wait_for_postgres(
            runner,
            container_id,
            database="postgres",
            user=descriptor.database_user,
        )
        runner.run(
            [
                DOCKER,
                "exec",
                "--user",
                "postgres",
                container_id,
                "createdb",
                "-h",
                "/var/run/postgresql",
                "-U",
                descriptor.database_user,
                "--",
                descriptor.database,
            ],
            timeout=120,
        )
        runner.run(
            [
                DOCKER,
                "exec",
                "--user",
                "root",
                container_id,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--strict-names",
                "-h",
                "/var/run/postgresql",
                "-U",
                descriptor.database_user,
                "-d",
                descriptor.database,
                "/source-audit/source.backup",
            ],
            timeout=3600,
        )
        database = _audit_container_medium(
            runner,
            container_id,
            descriptor,
            isolated=True,
            expected_data_directory="/var/lib/postgresql/data",
            logical_backup=True,
        )
    finally:
        if any(
            value["resource_key"] == container_key
            for value in operation.journal["resources"]  # type: ignore[union-attr]
        ):
            operation.remove_resource(container_key)
        operation.remove_resource(volume_key)
    final_descriptor = open_sealed_backup_regular(
        source_path,
        root=backup_root,
        root_authority=root_authority,
    )
    try:
        after = _fd_identity(final_descriptor, source_path, include_digest=True)
    finally:
        os.close(final_descriptor)
    if before != after or after.get("sha256") != source_digest:
        raise MediaEvidenceError("source backup changed during isolated restore")
    identity = {
        **before,
        "format": source.backup_format,
    }
    isolation = {
        "source_opened_with_openat_no_follow": True,
        "source_passed_to_docker": False,
        "staged_snapshot_mounted_read_only": True,
        "source_started_as_postgres": False,
        "scratch_network": "none",
        "scratch_destroyed": True,
        "restore_method": "pg_restore-no-owner-no-privileges-v1",
    }
    return database, source_digest, identity, {**after, "format": source.backup_format}, isolation


def _bind_tree_snapshot(
    source: Path,
    destination: Path,
) -> tuple[dict[str, object], str]:
    """Copy a private bind tree via openat without following symlinks."""

    source_descriptor = _open_directory_chain(source, private_from=source)
    destination.mkdir(mode=0o700)
    digest = hashlib.sha256()

    def copy_directory(
        input_descriptor: int,
        output: Path,
        relative: tuple[str, ...],
    ) -> None:
        for name in sorted(os.listdir(input_descriptor)):
            child = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=input_descriptor,
            )
            try:
                metadata = os.fstat(child)
                if (
                    metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077
                ):
                    raise MediaEvidenceError("bind PGDATA is not deploy-user-owned")
                relative_name = "/".join((*relative, name))
                if stat.S_ISDIR(metadata.st_mode):
                    target = output / name
                    target.mkdir(mode=0o700)
                    digest.update(
                        canonical_json_bytes(
                            ["directory", relative_name, stat.S_IMODE(metadata.st_mode)]
                        )
                    )
                    copy_directory(child, target, (*relative, name))
                elif stat.S_ISREG(metadata.st_mode):
                    target = output / name
                    target_descriptor = os.open(
                        target,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    file_digest = hashlib.sha256()
                    try:
                        while True:
                            chunk = os.read(child, 1024 * 1024)
                            if not chunk:
                                break
                            _write_all(target_descriptor, chunk)
                            file_digest.update(chunk)
                        os.fsync(target_descriptor)
                    finally:
                        os.close(target_descriptor)
                    digest.update(
                        canonical_json_bytes(
                            [
                                "file",
                                relative_name,
                                metadata.st_size,
                                stat.S_IMODE(metadata.st_mode),
                                file_digest.hexdigest(),
                            ]
                        )
                    )
                else:
                    raise MediaEvidenceError(
                        "bind PGDATA contains a symlink or special entry"
                    )
            finally:
                os.close(child)

    try:
        before = os.fstat(source_descriptor)
        copy_directory(source_descriptor, destination, ())
        after = os.fstat(source_descriptor)
    finally:
        os.close(source_descriptor)
    identity = {
        "path": str(source),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mtime_ns": before.st_mtime_ns,
        "mode": stat.S_IMODE(before.st_mode),
        "uid": before.st_uid,
    }
    if (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
        before.st_uid,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_mode,
        after.st_uid,
    ):
        raise MediaEvidenceError("bind PGDATA changed while it was copied")
    return identity, "sha256:" + digest.hexdigest()


def _isolated_bind_audit(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    workspace: Path,
    *,
    operation: ScratchOperation,
    resource_prefix: str,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    before_attachments = _current_attachments(runner, source)
    if any(
        str(value["state"]) in ACTIVE_CONTAINER_STATES
        for value in before_attachments
    ):
        raise MediaEvidenceError("active bind cannot enter isolated audit")
    snapshot = workspace / "pgdata"
    before_tree, source_digest = _bind_tree_snapshot(
        Path(source.locator),
        snapshot,
    )
    before = {
        **before_tree,
        "data_subpath": source.data_subpath,
        "attached": before_attachments,
    }
    # The copied bind is imported into a disposable Docker volume.  The
    # original host path is never passed to Docker.
    volume_key = f"{resource_prefix}-volume"
    clone = operation.create_volume(
        volume_key,
        source_media_id=source.media_id,
    )
    container_id: str | None = None
    container_key = f"{resource_prefix}-postgres"
    try:
        operation.run_container(
            f"{resource_prefix}-copy",
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--mount",
                f"type=bind,src={snapshot},dst=/source,readonly",
                "--mount",
                f"type=volume,src={clone},dst=/copy",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                (
                    "set -o pipefail; "
                    "tar -C /source -cpf - . | "
                    "tar -C /copy -xpf -; "
                    "chown -R postgres:postgres /copy"
                ),
            ],
            mounts=(
                {
                    "kind": "bind",
                    "source": str(snapshot),
                    "destination": "/source",
                    "read_only": True,
                },
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": "/copy",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
        )
        # Reuse the isolated-volume start/query primitives without recursively
        # copying the disposable clone.
        hba_path = (
            "/var/lib/postgresql/data/"
            + ("" if source.data_subpath == "." else source.data_subpath + "/")
            + ".nexpoly-audit-pg_hba.conf"
        )
        data_path = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        operation.run_container(
            f"{resource_prefix}-hba",
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--entrypoint",
                "/bin/sh",
                registry.audit_image,
                "-ceu",
                (
                    f"rm -f '{data_path}/postmaster.pid'; "
                    f"printf '%s\\n' 'local all all trust' > '{hba_path}'; "
                    f"chown postgres:postgres '{hba_path}'; chmod 0600 '{hba_path}'"
                ),
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": "/var/lib/postgresql/data",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
            read_only_rootfs=False,
        )
        pgdata = (
            "/var/lib/postgresql/data"
            + ("" if source.data_subpath == "." else f"/{source.data_subpath}")
        )
        completed = operation.run_container(
            container_key,
            [
                DOCKER,
                "run",
                "-d",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                _postgres_socket_tmpfs(registry),
                "--mount",
                f"type=volume,src={clone},dst=/var/lib/postgresql/data",
                "--env",
                f"PGDATA={pgdata}",
                registry.audit_image,
                *_isolated_postgres_arguments(
                    pgdata=pgdata,
                    hba_file=hba_path,
                ),
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": "/var/lib/postgresql/data",
                    "read_only": False,
                },
                {
                    "kind": "tmpfs",
                    "source": None,
                    "destination": "/var/run/postgresql",
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
            detached=True,
        )
        container_id = completed.stdout.decode("ascii", "strict").strip()
        if CONTAINER_RE.fullmatch(container_id) is None:
            raise MediaEvidenceError("isolated bind audit container ID is invalid")
        _wait_for_postgres(
            runner,
            container_id,
            database=descriptor.database,
            user=descriptor.database_user,
        )
        database = _audit_container_medium(
            runner,
            container_id,
            descriptor,
            isolated=True,
            expected_data_directory=pgdata,
        )
    finally:
        if any(
            value["resource_key"] == container_key
            for value in operation.journal["resources"]  # type: ignore[union-attr]
        ):
            operation.remove_resource(container_key)
        operation.remove_resource(volume_key)
    after_workspace = workspace / "pgdata-after"
    after_tree, after_digest = _bind_tree_snapshot(
        Path(source.locator),
        after_workspace,
    )
    shutil.rmtree(after_workspace)
    after = {
        **after_tree,
        "data_subpath": source.data_subpath,
        "attached": _current_attachments(runner, source),
    }
    if before != after or source_digest != after_digest:
        raise MediaEvidenceError("source bind changed during isolated audit")
    isolation = {
        "source_mounted_read_only": False,
        "source_opened_with_openat_no_follow": True,
        "source_started_as_postgres": False,
        "scratch_network": "none",
        "scratch_destroyed": True,
        "copy_method": "private-openat-copy-to-disposable-volume-v1",
    }
    return database, source_digest, before, after, isolation


def _local_audit_image_id(runner: CommandRunner, image: str) -> str:
    value = _json_command(runner, [DOCKER, "image", "inspect", "--", image])
    if (
        not isinstance(value, list)
        or len(value) != 1
        or not isinstance(value[0], dict)
        or not isinstance(value[0].get("Id"), str)
        or DIGEST_RE.fullmatch(value[0]["Id"]) is None
    ):
        raise MediaEvidenceError("pinned PG16 image is not preloaded")
    return str(value[0]["Id"])


def _postgres_socket_tmpfs(registry: Registry) -> str:
    if (
        isinstance(registry.postgres_uid, bool)
        or not isinstance(registry.postgres_uid, int)
        or registry.postgres_uid != POSTGRES_UID
        or isinstance(registry.postgres_gid, bool)
        or not isinstance(registry.postgres_gid, int)
        or registry.postgres_gid != POSTGRES_GID
    ):
        raise MediaEvidenceError(
            "registry PostgreSQL runtime user differs from pinned Alpine image"
        )
    return (
        "/var/run/postgresql:rw,noexec,nosuid,size=16m,"
        f"uid={registry.postgres_uid},gid={registry.postgres_gid},mode=0700"
    )


def _validate_audit_image(
    runner: CommandRunner,
    image: str,
    *,
    postgres_uid: int = POSTGRES_UID,
    postgres_gid: int = POSTGRES_GID,
    operation: ScratchOperation,
    resource_prefix: str,
) -> str:
    value = _json_command(runner, [DOCKER, "image", "inspect", "--", image])
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise MediaEvidenceError("pinned PG16 image is not preloaded")
    repo_digests = value[0].get("RepoDigests")

    def normalized_repository(reference: str) -> tuple[str, str] | None:
        if "@" not in reference:
            return None
        repository, digest = reference.rsplit("@", 1)
        if repository.startswith("docker.io/"):
            repository = repository.removeprefix("docker.io/")
        if repository.startswith("library/"):
            repository = repository.removeprefix("library/")
        return repository, digest

    expected_reference = normalized_repository(image)
    if (
        expected_reference is None
        or not isinstance(repo_digests, list)
        or not any(
            isinstance(reference, str)
            and normalized_repository(reference) == expected_reference
            for reference in repo_digests
        )
    ):
        raise MediaEvidenceError("local image does not expose the pinned PG16 digest")
    image_id = value[0].get("Id")
    if (
        not isinstance(image_id, str)
        or DIGEST_RE.fullmatch(image_id) is None
        or image_id != operation.authority["postgres_image_id"]
    ):
        raise MediaEvidenceError("pinned audit image ID is invalid or drifted")
    output = operation.run_container(
        f"{resource_prefix}-version",
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "postgres",
            image,
            "--version",
        ],
        mounts=(),
    ).stdout.decode("utf-8", "strict")
    if re.search(r"\b16(?:\.[0-9]+)?\b", output) is None:
        raise MediaEvidenceError("pinned audit image is not PostgreSQL 16")
    identity = operation.run_container(
        f"{resource_prefix}-postgres-user",
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ceu",
            "printf '%s:%s\\n' \"$(id -u postgres)\" \"$(id -g postgres)\"",
        ],
        mounts=(),
    ).stdout.decode("ascii", "strict").strip()
    if identity != f"{postgres_uid}:{postgres_gid}":
        raise MediaEvidenceError(
            "pinned audit image PostgreSQL UID/GID differs from registry"
        )
    if postgres_uid != POSTGRES_UID or postgres_gid != POSTGRES_GID:
        raise MediaEvidenceError(
            "registry PostgreSQL UID/GID differs from pinned Alpine policy"
        )
    operation.run_container(
        f"{resource_prefix}-toolchain",
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ceu",
            (
                "set -o pipefail; "
                "for tool in find sort stat cut tar sha256sum pg_restore createdb "
                "pg_controldata pg_isready psql; do "
                "command -v \"$tool\" >/dev/null; done; "
                "printf ready | sha256sum >/dev/null; "
                "tar -cf /dev/null -T /dev/null"
            ),
        ],
        mounts=(),
    )
    return image_id


def _live_source_identity(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> dict[str, object]:
    if source.kind == "docker_volume":
        return _docker_volume_identity(runner, source)
    if source.kind == "container_bind":
        path = Path(source.locator)
        descriptor = _open_directory_chain(path, private_from=path)
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        return {
            "path": str(path),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mtime_ns": metadata.st_mtime_ns,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "data_subpath": source.data_subpath,
            "attached": _current_attachments(runner, source),
        }
    raise MediaEvidenceError("live source must be a Docker volume or bind")


def _auditor_digest() -> str:
    path = Path(__file__)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_mode & 0o022
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
    ):
        raise MediaEvidenceError("auditor implementation identity is unsafe")
    return sha256_bytes(payload)


def _seal_media_record(record: dict[str, object]) -> dict[str, object]:
    audit = record.get("audit")
    if not isinstance(audit, dict) or "evidence_sha256" in audit:
        raise MediaEvidenceError("internal audit record is malformed")
    digest = sha256_bytes(canonical_json_bytes(record))
    return {
        **record,
        "audit": {
            **audit,
            "evidence_sha256": digest,
        },
    }


def _record_only_medium(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    workspace: Path,
    *,
    policy: DiscoveryPolicy,
    operation: ScratchOperation,
    resource_prefix: str,
    auditor_sha256: str,
    audit_image_id: str,
    service_file_sha256: str,
    audited_at: str,
) -> dict[str, object]:
    if descriptor.classification not in {
        "adjacent-record-only",
        "reviewed-non-pg",
    }:
        raise MediaEvidenceError("record-only media classification is invalid")
    if source.kind == "docker_volume":
        before = _docker_volume_identity(runner, source)
        before_digest = _volume_content_digest(
            runner,
            registry.audit_image,
            source,
            operation=operation,
            resource_key=f"{resource_prefix}-digest-before",
        )
        after_digest = _volume_content_digest(
            runner,
            registry.audit_image,
            source,
            operation=operation,
            resource_key=f"{resource_prefix}-digest-after",
        )
        after = _docker_volume_identity(runner, source)
        algorithm = "docker-volume-tree-sha256-v1"
        isolation = {
            "source_mounted_read_only": True,
            "source_started_as_postgres": False,
            "content_cas_verified": True,
        }
    elif source.kind == "container_bind":
        before = _live_source_identity(runner, source)
        first = workspace / "record-before"
        second = workspace / "record-after"
        _tree, before_digest = _bind_tree_snapshot(
            Path(source.locator),
            first,
        )
        _tree, after_digest = _bind_tree_snapshot(
            Path(source.locator),
            second,
        )
        shutil.rmtree(first)
        shutil.rmtree(second)
        after = _live_source_identity(runner, source)
        algorithm = "private-bind-tree-sha256-v1"
        isolation = {
            "source_opened_with_openat_no_follow": True,
            "source_started_as_postgres": False,
            "content_cas_verified": True,
        }
    elif source.kind == "reviewed_file":
        path = Path(source.locator)
        backup_root = _find_backup_root(path, policy)
        root_authority = _sealed_root_authority(
            registry,
            backup_root,
        )
        descriptor_fd = open_sealed_backup_regular(
            path,
            root=backup_root,
            root_authority=root_authority,
        )
        try:
            metadata_before = os.fstat(descriptor_fd)
            payload = _read_fd(descriptor_fd)
            metadata_after = os.fstat(descriptor_fd)
        finally:
            os.close(descriptor_fd)
        identity = {
            "path": str(path),
            "device": metadata_before.st_dev,
            "inode": metadata_before.st_ino,
            "size_bytes": metadata_before.st_size,
            "mtime_ns": metadata_before.st_mtime_ns,
            "mode": stat.S_IMODE(metadata_before.st_mode),
            "uid": metadata_before.st_uid,
        }
        if (
            metadata_before.st_dev,
            metadata_before.st_ino,
            metadata_before.st_size,
            metadata_before.st_mtime_ns,
        ) != (
            metadata_after.st_dev,
            metadata_after.st_ino,
            metadata_after.st_size,
            metadata_after.st_mtime_ns,
        ):
            raise MediaEvidenceError(
                "reviewed non-PG file changed during content audit"
            )
        before = identity
        after = dict(identity)
        before_digest = sha256_bytes(payload)
        after_digest = before_digest
        algorithm = "sha256-file-v1"
        isolation = {
            "source_opened_with_openat_no_follow": True,
            "source_passed_to_docker": False,
            "content_cas_verified": True,
        }
    elif source.kind == "postgres_backup":
        if (
            descriptor.classification != "adjacent-record-only"
            or descriptor.audit_method != "adjacent-record-only"
            or source.signature != "postgres-backup"
            or source.postgres_major is not None
            or source.backup_format
            not in {"postgres-custom-v1", "postgres-tar-v1"}
        ):
            raise MediaEvidenceError(
                "adjacent PostgreSQL backup identity is invalid"
            )
        path = Path(source.locator)
        backup_root = _find_backup_root(path, policy)
        root_authority = _sealed_root_authority(
            registry,
            backup_root,
        )
        first_descriptor = open_sealed_backup_regular(
            path,
            root=backup_root,
            root_authority=root_authority,
        )
        try:
            before = _fd_identity(
                first_descriptor,
                path,
                include_digest=True,
            )
        finally:
            os.close(first_descriptor)
        second_descriptor = open_sealed_backup_regular(
            path,
            root=backup_root,
            root_authority=root_authority,
        )
        try:
            after = _fd_identity(
                second_descriptor,
                path,
                include_digest=True,
            )
        finally:
            os.close(second_descriptor)
        before = {**before, "format": source.backup_format}
        after = {**after, "format": source.backup_format}
        before_digest = str(before["sha256"])
        after_digest = str(after["sha256"])
        algorithm = "sha256-file-v1"
        isolation = {
            "source_opened_with_openat_no_follow": True,
            "source_passed_to_docker": False,
            "source_started_as_postgres": False,
            "content_cas_verified": True,
        }
    else:
        raise MediaEvidenceError(
            "record-only media kind is not independently auditable"
        )
    if before != after or before_digest != after_digest:
        raise MediaEvidenceError(
            "record-only medium changed during content audit"
        )
    record = {
        "record_type": descriptor.classification,
        "media_id": descriptor.media_id,
        "kind": descriptor.kind,
        "classification": descriptor.classification,
        "disposition": descriptor.disposition,
        "source_identity_before": before,
        "source_identity_after": after,
        "source_content_sha256": before_digest,
        "content_identity_algorithm": algorithm,
        "postgres_signature": {
            "state": source.signature,
            "major": source.postgres_major,
            "data_subpath": source.data_subpath,
        },
        "readers": [dict(value) for value in source.attached],
        "excluded_from_nexpoly_migration": True,
        "audit": {
            "method": descriptor.audit_method,
            "complete": True,
            "auditor_sha256": auditor_sha256,
            "postgres_major": POSTGRES_MAJOR,
            "postgres_uid": registry.postgres_uid,
            "postgres_gid": registry.postgres_gid,
            "postgres_image": registry.audit_image,
            "postgres_image_id": audit_image_id,
            "pg_service_file_sha256": service_file_sha256,
            "audited_at": audited_at,
            "isolation": isolation,
        },
    }
    return _seal_media_record(record)


def _discovery_state_sha256(discovery: Discovery) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "media": [
                    discovery.media[media_id].document()
                    for media_id in sorted(discovery.media)
                ],
                "docker_inventory_sha256": (
                    discovery.docker_inventory_sha256
                ),
                "backup_inventory_sha256": (
                    discovery.backup_inventory_sha256
                ),
                "scanned_volume_names": list(
                    discovery.scanned_volume_names
                ),
                "scanned_container_ids": list(
                    discovery.scanned_container_ids
                ),
            }
        )
    )


def _scope_database_identity(
    database: dict[str, object],
    scope: str,
) -> dict[str, object]:
    identity = database.get("database_identity")
    if not isinstance(identity, dict):
        raise MediaEvidenceError("internal database identity is malformed")
    scoped = {**identity, "system_identifier_scope": scope}
    return {
        **database,
        "database_identity": scoped,
        "database_identity_sha256": sha256_bytes(
            canonical_json_bytes(scoped)
        ),
    }


def _scope_database_bundle(
    bundle: dict[str, object],
    scope: str,
) -> dict[str, object]:
    primary = _scope_database_identity(bundle, scope)
    raw_databases = bundle.get("databases")
    if not isinstance(raw_databases, list):
        raise MediaEvidenceError("internal database inventory bundle is malformed")
    scoped_databases: list[dict[str, object]] = []
    for record in raw_databases:
        if (
            not isinstance(record, dict)
            or record.get("audit_state")
            not in {"complete", "not-connectable-record-only"}
        ):
            raise MediaEvidenceError(
                "internal database inventory record is malformed"
            )
        audit = record.get("audit")
        if record["audit_state"] == "complete":
            if not isinstance(audit, dict):
                raise MediaEvidenceError(
                    "complete database inventory audit is absent"
                )
            scoped_audit: dict[str, object] | None = (
                _scope_database_identity(audit, scope)
            )
        else:
            if audit is not None:
                raise MediaEvidenceError(
                    "record-only database fabricated an audit"
                )
            scoped_audit = None
        scoped_databases.append({**record, "audit": scoped_audit})
    return {
        **primary,
        "databases": scoped_databases,
    }


def _write_private_atomic(directory: Path, name: str, payload: bytes) -> Path:
    directory_descriptor = _open_directory_chain(
        directory,
        private_from=directory,
        create_leaf=True,
    )
    temporary = f".{name}.tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            try:
                metadata = os.fstat(existing)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_nlink != 1
                ):
                    raise MediaEvidenceError(
                        f"existing immutable evidence is unsafe: {name}"
                    )
                if _read_fd(existing) != payload:
                    raise MediaEvidenceError(
                        f"conflicting immutable evidence already exists: {name}"
                    )
            finally:
                os.close(existing)
        os.unlink(temporary, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        os.close(directory_descriptor)
    return directory / name


def build_evidence(
    registry: Registry,
    discovery: Discovery,
    *,
    runner: CommandRunner,
    service_file: Path,
    evidence_root: Path,
    operation: ScratchOperation,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    now: Callable[[], str] = utc_now,
) -> dict[str, object]:
    audit_image_id = _validate_audit_image(
        runner,
        registry.audit_image,
        postgres_uid=registry.postgres_uid,
        postgres_gid=registry.postgres_gid,
        operation=operation,
        resource_prefix="build-image",
    )
    auditor_sha256 = _auditor_digest()
    if auditor_sha256 != registry.auditor_sha256:
        raise MediaEvidenceError("auditor drifted after registry validation")
    service_file_sha256 = _private_service_file_digest(service_file)
    if service_file_sha256 != registry.service_file_sha256:
        raise MediaEvidenceError(
            "private PostgreSQL service file differs from registry authority"
        )
    discovery_state_before = _discovery_state_sha256(discovery)
    media_records: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(
        prefix="nexpoly-postgres-media-",
        dir=operation.workspace,
    ) as temporary:
        os.chmod(temporary, 0o700)
        temporary_root = Path(temporary)
        for index, descriptor in enumerate(registry.descriptors):
            source = discovery.media[descriptor.media_id]
            workspace = temporary_root / f"medium-{index:04d}"
            workspace.mkdir(mode=0o700)
            audited_at = now()
            if RFC3339_UTC_RE.fullmatch(audited_at) is None:
                raise MediaEvidenceError("audit clock did not return UTC RFC3339")
            if descriptor.audit_method in {
                "adjacent-record-only",
                "reviewed-content-only",
            }:
                media_records[descriptor.media_id] = _record_only_medium(
                    runner,
                    registry,
                    descriptor,
                    source,
                    workspace,
                    policy=policy,
                    operation=operation,
                    resource_prefix=f"medium-{index:04d}-record",
                    auditor_sha256=auditor_sha256,
                    audit_image_id=audit_image_id,
                    service_file_sha256=service_file_sha256,
                    audited_at=audited_at,
                )
                continue
            if descriptor.audit_method == "live-read-only":
                before = _live_source_identity(runner, source)
                source_system_identifier_before = (
                    _live_source_system_identifier(runner, source)
                )
                database = _run_live_audit(runner, descriptor, source)
                database = _scope_database_bundle(
                    database,
                    "source-cluster",
                )
                source_system_identifier_after = (
                    _live_source_system_identifier(runner, source)
                )
                after = _live_source_identity(runner, source)
                if before != after:
                    raise MediaEvidenceError("online source identity changed during audit")
                if (
                    source_system_identifier_before
                    != source_system_identifier_after
                    or source_system_identifier_before
                    != database["database_identity"]["system_identifier"]
                ):
                    raise MediaEvidenceError(
                        "online service is not bound to the exact "
                        "Docker PGDATA cluster"
                    )
                source_system_identifier: str | None = (
                    source_system_identifier_before
                )
                source_digest = sha256_bytes(
                    canonical_json_bytes(
                        {
                            "database_inventory": database[
                                "database_inventory"
                            ],
                            "databases": database["databases"],
                        }
                    )
                )
                algorithm = "logical-cluster-inventory-v3"
                isolation = {
                    "source_mounted_by_auditor": False,
                    "source_started_by_auditor": False,
                    "transaction_read_only": True,
                }
            elif descriptor.audit_method == "isolated-volume-copy-read-only":
                (
                    database,
                    source_digest,
                    before,
                    after,
                    isolation,
                ) = _isolated_volume_audit(
                    runner,
                    registry,
                    descriptor,
                    source,
                    operation=operation,
                    resource_prefix=f"medium-{index:04d}-volume",
                )
                database = _scope_database_bundle(
                    database,
                    "copied-source-cluster",
                )
                source_system_identifier = str(
                    database["database_identity"]["system_identifier"]
                )
                algorithm = "postgres-data-directory-tar-sha256-v1"
            elif descriptor.audit_method == "isolated-bind-copy-read-only":
                (
                    database,
                    source_digest,
                    before,
                    after,
                    isolation,
                ) = _isolated_bind_audit(
                    runner,
                    registry,
                    descriptor,
                    source,
                    workspace,
                    operation=operation,
                    resource_prefix=f"medium-{index:04d}-bind",
                )
                database = _scope_database_bundle(
                    database,
                    "copied-source-cluster",
                )
                source_system_identifier = str(
                    database["database_identity"]["system_identifier"]
                )
                algorithm = "postgres-private-tree-sha256-v1"
            elif descriptor.audit_method == "isolated-backup-restore-read-only":
                (
                    database,
                    source_digest,
                    before,
                    after,
                    isolation,
                ) = _isolated_backup_audit(
                    runner,
                    registry,
                    descriptor,
                    source,
                    workspace,
                    policy=policy,
                    operation=operation,
                    resource_prefix=f"medium-{index:04d}-backup",
                )
                database = _scope_database_bundle(
                    database,
                    "isolated-restore-cluster",
                )
                source_system_identifier = None
                algorithm = "sha256-file-v1"
            else:
                raise MediaEvidenceError(
                    "unsupported media cannot produce deployable evidence"
                )
            record: dict[str, object] = {
                "record_type": "nexpoly-db",
                "media_id": descriptor.media_id,
                "kind": descriptor.kind,
                "classification": descriptor.classification,
                "database": descriptor.database,
                "disposition": descriptor.disposition,
                "source_identity_before": before,
                "source_identity_after": after,
                "source_system_identifier": source_system_identifier,
                "source_content_sha256": source_digest,
                "content_identity_algorithm": algorithm,
                "database_inventory": database["database_inventory"],
                "database_inventory_sha256": database[
                    "database_inventory_sha256"
                ],
                "databases": database["databases"],
                "database_identity": database["database_identity"],
                "database_identity_sha256": database[
                    "database_identity_sha256"
                ],
                "current_user": database["current_user"],
                "transaction_read_only": database["transaction_read_only"],
                "role_superuser": database["role_superuser"],
                "role_create_db": database["role_create_db"],
                "role_create_role": database["role_create_role"],
                "ledger": database["ledger"],
                "ledger_sha256": database["ledger_sha256"],
                "ledger_relation": database["ledger_relation"],
                "ledger_analysis": database["ledger_analysis"],
                "legacy_relation_present": database["legacy_relation_present"],
                "legacy_relation": database["legacy_relation"],
                "migration_0013": database["migration_0013"],
                "audit": {
                    "method": descriptor.audit_method,
                    "complete": True,
                    "auditor_sha256": auditor_sha256,
                    "postgres_major": POSTGRES_MAJOR,
                    "postgres_uid": registry.postgres_uid,
                    "postgres_gid": registry.postgres_gid,
                    "postgres_image": registry.audit_image,
                    "postgres_image_id": audit_image_id,
                    "pg_service_file_sha256": service_file_sha256,
                    "audited_at": audited_at,
                    "isolation": isolation,
                },
            }
            sealed = _seal_media_record(record)
            media_records[descriptor.media_id] = sealed
    if _private_service_file_digest(service_file) != service_file_sha256:
        raise MediaEvidenceError(
            "private PostgreSQL service file changed during audit"
        )
    final_discovery = discover_media(
        registry,
        runner=runner,
        operation=operation,
        policy=policy,
    )
    discovery_state_after = _discovery_state_sha256(final_discovery)
    if discovery_state_after != discovery_state_before:
        raise MediaEvidenceError(
            "external PostgreSQL discovery boundary changed during audit"
        )
    captured_at = now()
    if RFC3339_UTC_RE.fullmatch(captured_at) is None:
        raise MediaEvidenceError("audit clock did not return UTC RFC3339")
    for media_id, sealed in media_records.items():
        name = hashlib.sha256(media_id.encode("utf-8")).hexdigest()
        evidence_suffix = str(
            sealed["audit"]["evidence_sha256"]
        ).removeprefix("sha256:")
        _write_private_atomic(
            evidence_root,
            f"{name}-{evidence_suffix}.json",
            canonical_json_bytes(sealed) + b"\n",
        )
    required_records: list[dict[str, object]] = []
    for mapping in registry.required_online_databases:
        record = media_records[mapping["media_id"]]
        if record.get("record_type") != "nexpoly-db":
            raise MediaEvidenceError(
                "required online database maps to non-Nexpoly media"
            )
        required_records.append(
            {
                "stack": mapping["stack"],
                "media_id": mapping["media_id"],
                "database": record["database"],
                "current_user": record["current_user"],
                "transaction_read_only": record["transaction_read_only"],
                "role_superuser": record["role_superuser"],
                "role_create_db": record["role_create_db"],
                "role_create_role": record["role_create_role"],
                "system_identifier": record["database_identity"]["system_identifier"],
                "database_identity_sha256": record["database_identity_sha256"],
                "ledger": record["ledger"],
                "ledger_sha256": record["ledger_sha256"],
                "legacy_relation_present": record["legacy_relation_present"],
            }
        )
    requires_0014 = any(
        database_record.get("audit_state") == "complete"
        and isinstance(database_record.get("audit"), dict)
        and database_record["audit"]["migration_0013"]["state"]
        == "superseded-requires-0014"
        for record in media_records.values()
        if record.get("record_type") == "nexpoly-db"
        for database_record in record["databases"]
    )
    envelope = {
        "schema_version": 3,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": {
            "schema_version": 3,
            "sha256": registry.digest,
            "discovery_boundary_sha256": sha256_bytes(
                canonical_json_bytes(registry.boundary)
            ),
            "discovery_state_sha256_before": discovery_state_before,
            "discovery_state_sha256_after": discovery_state_after,
            "captured_at": captured_at,
            "expected_media_ids": sorted(media_records),
            "discovered_media_ids": sorted(discovery.media),
            "docker_inventory_sha256": discovery.docker_inventory_sha256,
            "backup_inventory_sha256": discovery.backup_inventory_sha256,
            "scanned_volume_names": list(discovery.scanned_volume_names),
            "scanned_container_ids": list(discovery.scanned_container_ids),
        },
        "databases": required_records,
        "media": [media_records[name] for name in sorted(media_records)],
        "requires_0014": requires_0014,
    }
    envelope_payload = canonical_json_bytes(envelope) + b"\n"
    envelope_digest = hashlib.sha256(envelope_payload).hexdigest()
    _write_private_atomic(
        evidence_root,
        f"external-database-audit-{envelope_digest}.json",
        envelope_payload,
    )
    return envelope


def _private_service_file_digest(path: Path) -> str:
    descriptor = open_private_regular(path, root=path.parent)
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
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
        raise MediaEvidenceError(
            "private PostgreSQL service file changed while being read"
        )
    return sha256_bytes(payload)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subparsers = value.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build",
        help="discover all fixed media and build fresh isolated evidence",
    )
    build.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    build.add_argument("--service-file", type=Path, default=DEFAULT_SERVICE_FILE)
    build.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    build.add_argument(
        "--expected-registry-sha256",
        required=True,
        help="full sha256:<hex> pinned by the prepared bridge descriptor",
    )
    for command in ("status", "recover"):
        current = subparsers.add_parser(
            command,
            help=(
                "inspect private scratch journals without changing Docker"
                if command == "status"
                else "recover only resources owned by private scratch journals"
            ),
        )
        current.add_argument(
            "--evidence-root",
            type=Path,
            default=DEFAULT_EVIDENCE_ROOT,
        )
        current.add_argument(
            "--operation-id",
            help="exact audit-<64hex> operation journal; default is all",
        )
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if (
        arguments.command == "build"
        and DIGEST_RE.fullmatch(arguments.expected_registry_sha256) is None
    ):
        print(
            "postgres-media-evidence: error: expected registry digest is invalid",
            file=sys.stderr,
        )
        return 2
    evidence_root = arguments.evidence_root.absolute()
    try:
        if arguments.command in {"status", "recover"}:
            if not evidence_root.exists() and not evidence_root.is_symlink():
                if arguments.command == "status":
                    print(
                        json.dumps(
                            {
                                "schema_version": SCRATCH_SCHEMA_VERSION,
                                "incomplete_journal_update_count": 0,
                                "operations": [],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    return 0
                raise MediaEvidenceError("scratch evidence root does not exist")
            evidence_descriptor = _open_directory_chain(
                evidence_root,
                private_from=evidence_root,
            )
            os.close(evidence_descriptor)
            journal_root = _scratch_journal_root(evidence_root)
            if not journal_root.exists() and not journal_root.is_symlink():
                if arguments.command == "status":
                    print(
                        json.dumps(
                            {
                                "schema_version": SCRATCH_SCHEMA_VERSION,
                                "incomplete_journal_update_count": 0,
                                "operations": [],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    return 0
                raise MediaEvidenceError("scratch journal root does not exist")
            runner = CommandRunner()
            with ScratchLock(evidence_root, create=False):
                result = (
                    scratch_status(
                        evidence_root,
                        operation_id=arguments.operation_id,
                    )
                    if arguments.command == "status"
                    else recover_scratch_operations(
                        evidence_root,
                        runner=runner,
                        operation_id=arguments.operation_id,
                    )
                )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 0
        registry = load_registry(arguments.registry)
        if registry.digest != arguments.expected_registry_sha256:
            raise MediaEvidenceError("media registry digest differs from authority")
        if (
            _private_service_file_digest(arguments.service_file)
            != registry.service_file_sha256
        ):
            raise MediaEvidenceError(
                "private PostgreSQL service file differs from registry authority"
            )
        evidence_descriptor = _open_directory_chain(
            evidence_root,
            private_from=evidence_root,
            create_leaf=True,
        )
        os.close(evidence_descriptor)
        runner = CommandRunner()
        with ScratchLock(evidence_root):
            # Recovery deliberately precedes image validation and the first
            # all-Docker discovery, so an interrupted owned PGDATA clone
            # cannot make the next build deadlock on its own stale medium.
            recover_scratch_operations(
                evidence_root,
                runner=runner,
            )
            image_id = _local_audit_image_id(runner, registry.audit_image)
            operation = ScratchOperation.begin(
                evidence_root,
                runner=runner,
                authority={
                    "registry_sha256": registry.digest,
                    "service_file_sha256": registry.service_file_sha256,
                    "auditor_sha256": registry.auditor_sha256,
                    "postgres_image": registry.audit_image,
                    "postgres_image_id": image_id,
                },
            )
            try:
                # Validate availability, exact local digest, PG16 and the
                # fixed shell toolchain before dormant media are probed.
                _validate_audit_image(
                    runner,
                    registry.audit_image,
                    postgres_uid=registry.postgres_uid,
                    postgres_gid=registry.postgres_gid,
                    operation=operation,
                    resource_prefix="preflight-image",
                )
                discovery = discover_media(
                    registry,
                    runner=runner,
                    operation=operation,
                )
                envelope = build_evidence(
                    registry,
                    discovery,
                    runner=runner,
                    service_file=arguments.service_file,
                    evidence_root=evidence_root,
                    operation=operation,
                )
                operation.complete(
                    {
                        "external_database_audit_sha256": sha256_bytes(
                            canonical_json_bytes(envelope) + b"\n"
                        )
                    }
                )
            except BaseException:
                try:
                    operation.abort()
                except BaseException as cleanup_error:
                    raise MediaEvidenceError(
                        "scratch audit failed and owned recovery did not complete"
                    ) from cleanup_error
                raise
    except (MediaEvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"postgres-media-evidence: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
