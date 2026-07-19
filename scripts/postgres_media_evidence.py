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
from dataclasses import dataclass, field, replace
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
from typing import Callable, Mapping, Sequence


DOCKER = "/usr/bin/docker"
PSQL = "/usr/bin/psql"
POSTGRES_MAJOR = 16
POSTGRES_UID = 70
POSTGRES_GID = 70
POSTGRES_AUDIT_IMAGES = {
    14: (
        "docker.io/library/postgres@"
        "sha256:f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579"
    ),
    15: (
        "docker.io/library/postgres@"
        "sha256:3d0f7584ed7d04e27fa050d6683a74746608faf21f202be78460d679cc56461f"
    ),
    16: (
        "docker.io/library/postgres@"
        "sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
    ),
    18: (
        "docker.io/library/postgres@"
        "sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15"
    ),
}
# Online SQL is executed by the existing server process, so pinning only the
# external client is insufficient.  This source-controlled allowlist binds
# every currently approved live server to both its local content ID and OCI
# repository digest.  A different active major/image must be reviewed and
# added in F before it can participate in migration evidence.
TRUSTED_POSTGRES_SERVER_IMAGES = {
    14: {
        (
            "sha256:"
            "f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579"
        ): (
            "postgres@sha256:"
            "f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579"
        ),
        (
            "sha256:"
            "c55d7e7deac05dde62139e0ded4fcf4f58363656cbc382dbea82fbed995aa767"
        ): (
            "pgvector/pgvector@sha256:"
            "c55d7e7deac05dde62139e0ded4fcf4f58363656cbc382dbea82fbed995aa767"
        ),
    },
    15: {
        (
            "sha256:"
            "3d0f7584ed7d04e27fa050d6683a74746608faf21f202be78460d679cc56461f"
        ): (
            "postgres@sha256:"
            "3d0f7584ed7d04e27fa050d6683a74746608faf21f202be78460d679cc56461f"
        ),
    },
    16: {
        (
            "sha256:"
            "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
        ): (
            "postgres@sha256:"
            "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
        ),
        (
            "sha256:"
            "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
        ): (
            "postgres@sha256:"
            "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
        ),
    },
    18: {
        (
            "sha256:"
            "9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15"
        ): (
            "postgres@sha256:"
            "9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15"
        ),
    },
}
SUPPORTED_POSTGRES_AUDIT_MAJORS = tuple(sorted(POSTGRES_AUDIT_IMAGES))
LOGICAL_MEDIA_POLICY = {
    "schema_version": 1,
    "named_stacks": [
        {
            "stack": "nexpoly_dev",
            "volume_name_pattern": (
                r"^nexpoly_dev(?:_nexpoly_dev)?_postgres_data$"
            ),
            "database": "nexpoly_dev",
            "audit_role": "nexpoly_dev_auditor",
            "online_service": "nexpoly_dev_audit",
            "allowed_state": "online-or-retained-isolated",
        },
        {
            "stack": "nexpoly_md_health_opt",
            "volume_name_pattern": (
                r"^nexpoly_md_health_opt(?:_app)?_postgres_data$"
            ),
            "database": "nexpoly_md_health_opt",
            "audit_role": "nexpoly_health_auditor",
            "online_service": "nexpoly_md_health_opt_audit",
            "allowed_state": "online-or-retained-isolated",
        },
    ],
    "additional_postgres": {
        "allowed_majors": list(SUPPORTED_POSTGRES_AUDIT_MAJORS),
        "active": (
            "live-read-only-adjacent-with-source-epoch-role-provisioning"
        ),
        "inactive": "full-isolated-ledger-audit",
        "database_inventory": "runtime-observed-all-nontemplate",
    },
    "non_postgres": {
        "active": (
            "pinned-read-only-volume-marker-probe-after-"
            "complete-local-view-proof"
        ),
        "active_pgdata_candidate": "fail-closed",
        "postgres_named_without_signature": "fail-closed",
        "inactive": "private-reviewed-content-inventory-v1",
        "reject_symlink_or_special": True,
        "reject_postgres_or_backup_signatures": True,
        "maximum_single_file_bytes": 17_179_869_184,
        "maximum_files": 10_000_000,
        "maximum_bytes": 1_099_511_627_776,
    },
    "future_takeover_backup": {
        "media_id": (
            "postgres-backup:/data/lzq/gith/nexpoly-runtime/"
            "legacy-takeover/preserved-postgres-backups/"
            "nexpoly-b875829c3f00.dump"
        ),
        "pre_takeover": "media-evidence-blocked-until-takeover",
        "post_takeover": "completed-operation-and-exact-seal-required",
    },
}
ADJACENT_POSTGRES_MAJOR_MIN = 9
ADJACENT_POSTGRES_MAJOR_MAX = 18
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_DATABASE_JSON_BYTES = 64 * 1024 * 1024
MAX_DURABLE_CHECKPOINT_BYTES = 128 * 1024 * 1024
MAX_SCRATCH_SEQUENCE_FILES = 512
MAX_SCRATCH_SEQUENCE_BYTES = 256 * 1024 * 1024
MAX_POSTGRES_MARKER_PROBE_ENTRIES = 100_000
MAX_POSTGRES_MARKER_RESULTS = 4_096
POSTGRES_MARKER_PROBE_TIMEOUT_SECONDS = 30
PSQL_AUDIT_PGOPTIONS = (
    "-c default_transaction_read_only=on "
    "-c statement_timeout=5min "
    "-c lock_timeout=5s "
    "-c search_path=pg_catalog "
    "-c row_security=on "
    "-c jit=off "
    "-c session_preload_libraries= "
    "-c local_preload_libraries="
)
PSQL_PROVISION_PGOPTIONS = (
    "-c statement_timeout=5min "
    "-c lock_timeout=5s "
    "-c search_path=pg_catalog "
    "-c row_security=on "
    "-c jit=off "
    "-c session_preload_libraries= "
    "-c local_preload_libraries="
)


def _psql_audit_pgoptions(major: int) -> str:
    if major not in SUPPORTED_POSTGRES_AUDIT_MAJORS:
        raise MediaEvidenceError("PostgreSQL audit major is unsupported")
    if major >= 17:
        return PSQL_AUDIT_PGOPTIONS + " -c event_triggers=false"
    return PSQL_AUDIT_PGOPTIONS


def _psql_provision_pgoptions(major: int) -> str:
    if major not in SUPPORTED_POSTGRES_AUDIT_MAJORS:
        raise MediaEvidenceError("PostgreSQL provision major is unsupported")
    if major >= 17:
        return PSQL_PROVISION_PGOPTIONS + " -c event_triggers=false"
    return PSQL_PROVISION_PGOPTIONS
DEFAULT_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
DEFAULT_AUTHORITY_RULES = (
    DEFAULT_RUNTIME_ROOT / "config/postgres-media-authority-rules.json"
)
DEFAULT_REGISTRY = DEFAULT_RUNTIME_ROOT / "config/postgres-media-registry.json"
AUDIT_ROLE_SQL_DIGEST_ENV = "NEXPOLY_MEDIA_AUDIT_ROLE_SQL_SHA256"
DATABASE_CREDENTIALS_FD_ENV = (
    "NEXPOLY_MEDIA_DATABASE_CREDENTIALS_FD"
)
DATABASE_CREDENTIALS_DIGEST_ENV = (
    "NEXPOLY_MEDIA_DATABASE_CREDENTIALS_SHA256"
)
MAX_DATABASE_CREDENTIALS_BYTES = 1024 * 1024
MAX_DATABASE_PASSWORD_BYTES = 4096
DEFAULT_REVIEWED_CONTENT_ROOT = (
    DEFAULT_RUNTIME_ROOT / "config/postgres-media-reviewed-content"
)
DEFAULT_EVIDENCE_ROOT = DEFAULT_RUNTIME_ROOT / "audit/postgres-media"
FORBIDDEN_BACKUP_ROOT = DEFAULT_RUNTIME_ROOT / "backups"
LEGACY_PRE_TAKEOVER_BACKUP_ROOT = Path(
    "/data/lzq/gith/nexpoly/backups"
)
TAKEOVER_STATE_DIRECTORY = DEFAULT_RUNTIME_ROOT / "state/legacy-takeover"
TAKEOVER_ACTIVE_RECORD = TAKEOVER_STATE_DIRECTORY / "active.json"
TAKEOVER_OPERATIONS_DIRECTORY = TAKEOVER_STATE_DIRECTORY / "operations"
REQUIRED_TAKEOVER_BACKUP_NAME = "nexpoly-b875829c3f00.dump"
TAKEOVER_OPERATION_RE = re.compile(
    r"^takeover-[a-z0-9][a-z0-9-]{7,79}$"
)
MAX_TAKEOVER_STATE_BYTES = 64 * 1024 * 1024
EXPECTED_LEGACY_GIT_IDENTITY = {
    "branch": "refs/heads/main",
    "head_sha": "b875829c3f008b5ee733d8ffced3093e4cbb07c5",
    "head_tree": "4f68c10a39c6943f7ff13af33d547ebb8f5d7a00",
    "local_main_sha": "b875829c3f008b5ee733d8ffced3093e4cbb07c5",
}
BOOTSTRAP_IMMUTABLE_FILES = {
    "control_runtime_selector.py",
    "nexpoly-pull-deploy",
    "nexpoly-postgres-media-evidence",
    "nexpoly-production-readiness",
    "nexpoly-pull-contract-0012",
    "nexpoly-reconcile-production-0005-polytao-alias",
}
TAKEOVER_EXECUTION_CLOSURE = {
    "legacy_takeover.py",
    "site_helper_contracts.py",
    "git_source_trust.py",
    "postgres_media_evidence.py",
}
APPROVED_BACKUP_ROOTS = (
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
ROLE_CONTRACT_POLICY = "nexpoly-postgres-media-audit-role-v1"
MANAGED_ROLE_MATRIX_SCHEMA_VERSION = 1
MAX_MANAGED_ROLE_MATRIX_ROLES = 1024
MAX_MANAGED_ROLE_MATRIX_DATABASES = 1024
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
SCRATCH_SCHEMA_VERSION = 2
SCRATCH_JOURNAL_ROOT_NAME = ".scratch-operations"
SCRATCH_WORKSPACE_ROOT_NAME = ".scratch-workspaces"
DURABLE_CHECKPOINT_ROOT_NAME = ".audit-checkpoints"
DURABLE_CHECKPOINT_SCHEMA_VERSION = 2
DURABLE_CHECKPOINT_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
DURABLE_CHECKPOINT_TEMP_RE = re.compile(
    r"^\.[0-9a-f]{64}\.json\.tmp-[0-9a-f]{32}$"
)
SCRATCH_LOCK_NAME = "LOCK"
SCRATCH_WORKSPACE_OWNER_NAME = ".nexpoly-audit-owner.json"
SCRATCH_LABEL_PREFIX = "io.nexpoly.audit."
SCRATCH_TERMINAL_PHASES = frozenset(
    {"completed", "aborted", "recovered"}
)


def _valid_pg_identifier(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 63
    except UnicodeError:
        return False


def media_id_for_locator(kind: str, locator: str) -> str:
    prefixes = {
        "docker_volume": "docker-volume",
        "container_bind": "container-bind",
        "postgres_backup": "postgres-backup",
        "reviewed_file": "reviewed-file",
    }
    prefix = prefixes.get(kind)
    if prefix is None or not isinstance(locator, str) or not locator:
        raise MediaEvidenceError("external media locator is invalid")
    candidate = f"{prefix}:{locator}"
    if MEDIA_ID_RE.fullmatch(candidate) is not None:
        return candidate
    return (
        f"{prefix}-sha256:"
        + hashlib.sha256(locator.encode("utf-8")).hexdigest()
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
class OnlineDatabaseCredential:
    """One secret record sealed by the fixed runtime credential envelope."""

    envelope_sha256: str
    container_id: str
    cluster_system_identifier: str
    online_admin_role: str
    postgres_major: int
    password: str


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    backup_roots: tuple[Path, ...] = APPROVED_BACKUP_ROOTS
    backup_formats: tuple[tuple[str, tuple[str, ...]], ...] = (
        APPROVED_BACKUP_FORMATS
    )
    discovery_methods: tuple[str, ...] = DISCOVERY_METHODS

    def document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": 1,
            "docker_scope": "all-local-containers-volumes-and-pgdata-binds",
            "backup_roots": [str(value) for value in self.backup_roots],
            "backup_formats": [
                {"name": name, "suffixes": list(suffixes)}
                for name, suffixes in self.backup_formats
            ],
            "discovery_methods": list(self.discovery_methods),
        }
        if self.backup_roots == APPROVED_BACKUP_ROOTS:
            document["takeover_backup_transition"] = {
                "legacy_root": str(LEGACY_PRE_TAKEOVER_BACKUP_ROOT),
                "preserved_root": str(APPROVED_BACKUP_ROOTS[0]),
                "state_directory": str(TAKEOVER_STATE_DIRECTORY),
                "required_relative_path": REQUIRED_TAKEOVER_BACKUP_NAME,
                "pre_takeover": "blocked",
                "post_takeover": "completed-operation-required",
            }
        return document


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    media_id: str
    kind: str
    database: str
    database_user: str
    disposition: str
    audit_method: str
    online_admin_role: str | None = None
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
            "online_admin_role": self.online_admin_role,
            "classification": self.classification,
            "source_postgres_major": self.source_postgres_major,
            "databases": [dict(value) for value in self.databases],
        }


@dataclass(frozen=True, slots=True)
class MediaAuthorityRules:
    payload: bytes
    digest: str
    audit_image: str
    auditor_sha256: str
    descriptors: tuple[MediaDescriptor, ...]
    required_online_databases: tuple[dict[str, str], ...]
    policy: DiscoveryPolicy
    allow_unmatched_non_postgres: bool
    production_identity: dict[str, object]
    audit_images: tuple[tuple[int, str], ...] = ()
    logical_media: dict[str, object] | None = None
    postgres_uid: int = POSTGRES_UID
    postgres_gid: int = POSTGRES_GID


@dataclass(frozen=True, slots=True)
class Registry:
    payload: bytes
    digest: str
    audit_image: str
    auditor_sha256: str
    descriptors: tuple[MediaDescriptor, ...]
    required_online_databases: tuple[dict[str, str], ...]
    boundary: dict[str, object]
    authority_rules_sha256: str = (
        "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    )
    audit_images: tuple[tuple[int, str], ...] = ()
    audit_image_ids: tuple[tuple[int, str], ...] = ()
    reviewed_content_inventory_sha256: str | None = None
    postgres_uid: int = POSTGRES_UID
    postgres_gid: int = POSTGRES_GID
    production_identity: dict[str, object] | None = None


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
    audit_locator: str | None = None
    takeover_redirect: dict[str, object] | None = None
    takeover_seal: dict[str, object] | None = None
    classification_probe: dict[str, object] | None = None
    docker_inspect_sha256: str | None = None

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
            "audit_locator": self.audit_locator,
            "takeover_redirect": (
                dict(self.takeover_redirect)
                if self.takeover_redirect is not None
                else None
            ),
            "classification_probe": (
                dict(self.classification_probe)
                if self.classification_probe is not None
                else None
            ),
            "docker_inspect_sha256": self.docker_inspect_sha256,
        }


@dataclass(frozen=True, slots=True)
class Discovery:
    media: Mapping[str, DiscoveredMedia]
    docker_inventory_sha256: str
    backup_inventory_sha256: str
    scanned_volume_names: tuple[str, ...]
    scanned_bind_sources: tuple[str, ...]
    scanned_container_ids: tuple[str, ...]
    audit_checkpoints: Mapping[str, dict[str, object]] = field(
        default_factory=dict
    )


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


def _audit_images_map(
    value: MediaAuthorityRules | Registry,
) -> dict[int, str]:
    images = dict(value.audit_images)
    if not images:
        images = {POSTGRES_MAJOR: value.audit_image}
    return images


def _audit_image_for_major(
    value: MediaAuthorityRules | Registry,
    major: int,
) -> str:
    image = _audit_images_map(value).get(major)
    if image is None:
        raise MediaEvidenceError(
            f"PostgreSQL {major} media lacks a source-pinned audit image"
        )
    return image


def _scratch_journal_root(evidence_root: Path) -> Path:
    return evidence_root / SCRATCH_JOURNAL_ROOT_NAME


def _scratch_workspace_root(evidence_root: Path) -> Path:
    return evidence_root / SCRATCH_WORKSPACE_ROOT_NAME


def _durable_checkpoint_root(evidence_root: Path) -> Path:
    return evidence_root / DURABLE_CHECKPOINT_ROOT_NAME


def _durable_checkpoint_path(
    evidence_root: Path,
    media_id: str,
) -> Path:
    return _durable_checkpoint_root(evidence_root) / (
        hashlib.sha256(media_id.encode("utf-8")).hexdigest() + ".json"
    )


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


def _normalize_scratch_authority(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Return one journal authority with an explicit immutable image set."""

    authority = dict(value)
    if "postgres_images" not in authority:
        image = authority.get("postgres_image")
        matches = [
            major
            for major, expected in POSTGRES_AUDIT_IMAGES.items()
            if image == expected
        ]
        if len(matches) != 1:
            raise MediaEvidenceError(
                "scratch authority image is not one supported pinned digest"
            )
        authority["postgres_images"] = {
            str(matches[0]): {
                "digest_ref": image,
                "image_id": authority.get("postgres_image_id"),
            }
        }
    return authority


def _scratch_allowed_images(
    authority: Mapping[str, object],
) -> dict[str, str]:
    raw = authority.get("postgres_images")
    if (
        not isinstance(raw, dict)
        or not raw
        or any(
            not isinstance(raw_major, str) or not raw_major.isdigit()
            for raw_major in raw
        )
        or list(raw) != sorted(raw, key=lambda value: int(str(value)))
    ):
        raise MediaEvidenceError("scratch journal image authority is invalid")
    result: dict[str, str] = {}
    for raw_major, record in raw.items():
        if (
            not isinstance(raw_major, str)
            or not raw_major.isdigit()
            or int(raw_major) not in SUPPORTED_POSTGRES_AUDIT_MAJORS
            or not isinstance(record, dict)
            or set(record) != {"digest_ref", "image_id"}
            or record.get("digest_ref")
            != POSTGRES_AUDIT_IMAGES[int(raw_major)]
            or not isinstance(record.get("image_id"), str)
            or DIGEST_RE.fullmatch(record["image_id"]) is None
        ):
            raise MediaEvidenceError(
                "scratch journal image authority is invalid"
            )
        result[str(record["digest_ref"])] = str(record["image_id"])
    if len(result) != len(raw):
        raise MediaEvidenceError(
            "scratch journal image authority contains duplicate images"
        )
    return result


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
            "auditor_sha256",
            "postgres_image",
            "postgres_image_id",
            "postgres_images",
        }
        or any(
            not isinstance(authority.get(key), str)
            or DIGEST_RE.fullmatch(str(authority[key])) is None
            for key in (
                "registry_sha256",
                "auditor_sha256",
                "postgres_image_id",
            )
        )
        or not isinstance(authority.get("postgres_image"), str)
        or IMAGE_RE.fullmatch(str(authority["postgres_image"])) is None
    ):
        raise MediaEvidenceError("scratch journal authority is invalid")
    allowed_images = _scratch_allowed_images(authority)
    if (
        allowed_images.get(str(authority["postgres_image"]))
        != authority["postgres_image_id"]
    ):
        raise MediaEvidenceError(
            "scratch journal primary image is outside its authority set"
        )
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
                or allowed_images.get(str(spec.get("postgres_image")))
                != spec.get("postgres_image_id")
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
    head: dict[str, object] | None
    try:
        head = _load_scratch_document(
            path,
            evidence_root=evidence_root,
            operation_id=operation_id,
        )
    except FileNotFoundError:
        head = None
    sequence_names = [
        name
        for name in _scratch_journal_entries(evidence_root)
        if (
            (match := SCRATCH_SEQUENCE_RE.fullmatch(name)) is not None
            and match.group(1) == operation_id
        )
    ]
    if len(sequence_names) > MAX_SCRATCH_SEQUENCE_FILES:
        raise MediaEvidenceError(
            "scratch immutable journal exceeds its sequence bound"
        )
    sequence_bytes = 0
    for name in sequence_names:
        metadata = os.lstat(journal_root / name)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise MediaEvidenceError(
                "scratch immutable journal entry is unsafe"
            )
        sequence_bytes += metadata.st_size
    if sequence_bytes > MAX_SCRATCH_SEQUENCE_BYTES:
        raise MediaEvidenceError(
            "scratch immutable journal exceeds its byte bound"
        )
    if not sequence_names:
        if head is None:
            raise FileNotFoundError(path)
        return head
    if head is not None and head["phase"] in SCRATCH_TERMINAL_PHASES:
        final_name = _scratch_sequence_name(head)
        if final_name not in sequence_names:
            raise MediaEvidenceError(
                "terminal scratch HEAD lacks its immutable final state"
            )
        final = _load_scratch_document(
            journal_root / final_name,
            evidence_root=evidence_root,
            operation_id=operation_id,
        )
        if final != head:
            raise MediaEvidenceError(
                "terminal scratch HEAD differs from its immutable final state"
            )
        return head
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
    if head is None:
        head = sequence[-1]
    if head["journal_sha256"] not in {
        value["journal_sha256"] for value in sequence
    }:
        raise MediaEvidenceError(
            "scratch mutable HEAD is not anchored in its immutable chain"
        )
    return sequence[-1]


def _load_current_scratch_head(
    path: Path,
    *,
    evidence_root: Path,
    expected: Mapping[str, object],
) -> dict[str, object]:
    """CAS only HEAD and its current immutable state during normal updates."""

    operation_id = path.name.removesuffix(".json")
    head = _load_scratch_document(
        path,
        evidence_root=evidence_root,
        operation_id=operation_id,
    )
    expected_immutable = _load_scratch_document(
        _scratch_journal_root(evidence_root)
        / _scratch_sequence_name(expected),
        evidence_root=evidence_root,
        operation_id=operation_id,
    )
    if expected_immutable != expected:
        raise MediaEvidenceError(
            "scratch current state differs from its immutable journal"
        )
    if head != expected:
        _write_private_replace(
            path,
            canonical_json_bytes(expected_immutable) + b"\n",
            root=_scratch_journal_root(evidence_root),
        )
        head = expected_immutable
    immutable = _load_scratch_document(
        _scratch_journal_root(evidence_root)
        / _scratch_sequence_name(head),
        evidence_root=evidence_root,
        operation_id=operation_id,
    )
    if (
        head != immutable
        or head.get("journal_sha256") != expected.get("journal_sha256")
        or head != expected
    ):
        raise MediaEvidenceError(
            "scratch journal changed outside the held operation"
        )
    return head


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


def _online_container_admin_role(
    runner: CommandRunner,
    container_id: str,
) -> str:
    """Return the exact initdb administrator declared by one live container.

    The official PostgreSQL image does not create a role named ``postgres``
    when ``POSTGRES_USER`` names a different bootstrap administrator.  Parse
    only that one non-secret variable from the exact inspected container and
    fail closed for an absent, duplicate, or unsafe value.
    """

    if CONTAINER_RE.fullmatch(container_id) is None:
        raise MediaEvidenceError(
            "online PostgreSQL container identity is invalid"
        )
    container = _optional_docker_inspect(
        runner,
        "container",
        container_id,
    )
    if (
        container is None
        or container.get("Id") != container_id
        or not isinstance(container.get("Config"), dict)
    ):
        raise MediaEvidenceError(
            "online PostgreSQL container inspect identity differs"
        )
    raw_environment = container["Config"].get("Env")
    if not isinstance(raw_environment, list) or any(
        not isinstance(value, str) for value in raw_environment
    ):
        raise MediaEvidenceError(
            "online PostgreSQL container environment is malformed"
        )
    values = [
        value.removeprefix("POSTGRES_USER=")
        for value in raw_environment
        if value.startswith("POSTGRES_USER=")
    ]
    if (
        len(values) != 1
        or not _valid_pg_identifier(values[0])
    ):
        raise MediaEvidenceError(
            "online PostgreSQL container must declare one safe POSTGRES_USER"
        )
    return values[0]


def _strict_credential_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential JSON member")
        result[key] = value
    return result


def _inherited_database_credentials(
) -> tuple[OnlineDatabaseCredential, ...] | None:
    """Read and authenticate the launcher's inherited secret descriptor.

    The descriptor, not a caller-selected path, is the sole secret transport.
    The raw envelope digest is supplied by the fixed manifest-pinned launcher
    and is checked together with stable file metadata on every SQL invocation.
    """

    raw_descriptor = os.environ.get(DATABASE_CREDENTIALS_FD_ENV)
    expected_digest = os.environ.get(DATABASE_CREDENTIALS_DIGEST_ENV)
    if raw_descriptor is None and expected_digest is None:
        return None
    if (
        not isinstance(raw_descriptor, str)
        or not raw_descriptor.isdigit()
        or int(raw_descriptor) < 3
        or not isinstance(expected_digest, str)
        or DIGEST_RE.fullmatch(expected_digest) is None
    ):
        raise MediaEvidenceError(
            "database credential envelope descriptor authority is invalid"
        )
    descriptor = int(raw_descriptor)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_DATABASE_CREDENTIALS_BYTES
        ):
            raise MediaEvidenceError(
                "database credential envelope descriptor is unsafe"
            )
        chunks: list[bytes] = []
        offset = 0
        while offset < before.st_size:
            chunk = os.pread(
                descriptor,
                min(1024 * 1024, before.st_size - offset),
                offset,
            )
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise MediaEvidenceError(
            "database credential envelope descriptor is unavailable"
        ) from exc
    if (
        len(payload) != before.st_size
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_mode,
            before.st_uid,
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
            after.st_nlink,
        )
        or sha256_bytes(payload) != expected_digest
    ):
        raise MediaEvidenceError(
            "database credential envelope descriptor differs"
        )
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_credential_json_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise MediaEvidenceError(
            "database credential envelope is malformed"
        ) from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "records"}
        or document.get("schema_version") != 1
        or not isinstance(document.get("records"), list)
        or not document["records"]
    ):
        raise MediaEvidenceError(
            "database credential envelope contract differs"
        )
    credentials: list[OnlineDatabaseCredential] = []
    for record in document["records"]:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "container_id",
                "cluster_system_identifier",
                "online_admin_role",
                "postgres_major",
                "password",
            }
        ):
            raise MediaEvidenceError(
                "database credential envelope record contract differs"
            )
        container_id = record.get("container_id")
        system_identifier = record.get("cluster_system_identifier")
        admin_role = record.get("online_admin_role")
        postgres_major = record.get("postgres_major")
        password = record.get("password")
        try:
            password_bytes = (
                password.encode("utf-8")
                if isinstance(password, str)
                else b""
            )
        except UnicodeEncodeError as exc:
            raise MediaEvidenceError(
                "database credential envelope password is unsafe"
            ) from exc
        if (
            not isinstance(container_id, str)
            or CONTAINER_RE.fullmatch(container_id) is None
            or not isinstance(system_identifier, str)
            or PG_SYSTEM_ID_RE.fullmatch(system_identifier) is None
            or not isinstance(admin_role, str)
            or not _valid_pg_identifier(admin_role)
            or type(postgres_major) is not int
            or postgres_major not in SUPPORTED_POSTGRES_AUDIT_MAJORS
            or not isinstance(password, str)
            or not password_bytes
            or len(password_bytes) > MAX_DATABASE_PASSWORD_BYTES
            or "\x00" in password
            or "\n" in password
            or "\r" in password
        ):
            raise MediaEvidenceError(
                "database credential envelope record is unsafe"
            )
        credentials.append(
            OnlineDatabaseCredential(
                envelope_sha256=expected_digest,
                container_id=container_id,
                cluster_system_identifier=system_identifier,
                online_admin_role=admin_role,
                postgres_major=postgres_major,
                password=password,
            )
        )
    container_ids = [value.container_id for value in credentials]
    if (
        container_ids != sorted(container_ids)
        or len(container_ids) != len(set(container_ids))
    ):
        raise MediaEvidenceError(
            "database credential envelope records are ambiguous"
        )
    return tuple(credentials)


def _inherited_database_credential(
    *,
    container_id: str,
    postgres_major: int,
    online_admin_role: str,
) -> OnlineDatabaseCredential | None:
    credentials = _inherited_database_credentials()
    if credentials is None:
        return None
    matches = [
        value
        for value in credentials
        if value.container_id == container_id
    ]
    if len(matches) != 1:
        raise MediaEvidenceError(
            "database credential envelope lacks the exact container"
        )
    credential = matches[0]
    if (
        credential.postgres_major != postgres_major
        or credential.online_admin_role != online_admin_role
    ):
        raise MediaEvidenceError(
            "database credential envelope target identity differs"
        )
    return credential


def _online_container_connection(
    runner: CommandRunner,
    container_id: str,
) -> tuple[str, str, bool]:
    """Return non-secret inspected bootstrap identity and explicit trust mode."""

    container = _optional_docker_inspect(
        runner,
        "container",
        container_id,
    )
    if (
        container is None
        or container.get("Id") != container_id
        or not isinstance(container.get("Config"), dict)
    ):
        raise MediaEvidenceError(
            "online PostgreSQL connection authority differs"
        )
    raw_environment = container["Config"].get("Env")
    if not isinstance(raw_environment, list) or any(
        not isinstance(value, str) for value in raw_environment
    ):
        raise MediaEvidenceError(
            "online PostgreSQL connection environment is malformed"
        )

    def one(name: str, *, required: bool) -> str | None:
        values = [
            value[len(name) + 1 :]
            for value in raw_environment
            if value.startswith(name + "=")
        ]
        if len(values) > 1 or required and len(values) != 1:
            raise MediaEvidenceError(
                f"online PostgreSQL {name} authority is ambiguous"
            )
        if not values:
            return None
        if "\x00" in values[0] or "\n" in values[0]:
            raise MediaEvidenceError(
                f"online PostgreSQL {name} authority is unsafe"
            )
        return values[0]

    user = one("POSTGRES_USER", required=True)
    assert user is not None
    if not _valid_pg_identifier(user):
        raise MediaEvidenceError(
            "online PostgreSQL POSTGRES_USER is invalid"
        )
    database = one("POSTGRES_DB", required=False) or user
    if not _valid_pg_identifier(database):
        raise MediaEvidenceError(
            "online PostgreSQL POSTGRES_DB is invalid"
        )
    password_file = one("POSTGRES_PASSWORD_FILE", required=False)
    trust = one("POSTGRES_HOST_AUTH_METHOD", required=False) == "trust"
    if password_file is not None:
        raise MediaEvidenceError(
            "online PostgreSQL password-file authority is not externally sealed"
        )
    return user, database, trust


TRUSTED_PSQL_PGPASS_SCRIPT = (
    "umask 077\n"
    "unset PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD "
    "PGSERVICE PGSERVICEFILE\n"
    "IFS= read -r nexp_credentials || exit 70\n"
    "printf '%s\\n' \"$nexp_credentials\" > /tmp/nexpoly-pgpass\n"
    "unset nexp_credentials\n"
    "export PGPASSFILE=/tmp/nexpoly-pgpass\n"
    "exec psql \"$@\""
)


def _trusted_psql_input(
    password: str,
    sql_input: bytes | None,
) -> bytes:
    """Frame one libpq passfile line ahead of the psql stdin stream."""

    password_bytes = password.encode("utf-8")
    if (
        len(password_bytes) > MAX_DATABASE_PASSWORD_BYTES
        or b"\x00" in password_bytes
        or b"\n" in password_bytes
        or b"\r" in password_bytes
    ):
        raise MediaEvidenceError(
            "database credential cannot be framed safely"
        )
    escaped = password.replace("\\", "\\\\").replace(":", "\\:")
    pgpass = f"127.0.0.1:*:*:*:{escaped}\n".encode("utf-8")
    return pgpass + (sql_input or b"")


def _trusted_psql_command(
    *,
    container_id: str,
    postgres_major: int,
    pgoptions: str,
    arguments: Sequence[str],
) -> list[str]:
    if (
        CONTAINER_RE.fullmatch(container_id) is None
        or postgres_major not in SUPPORTED_POSTGRES_AUDIT_MAJORS
    ):
        raise MediaEvidenceError("trusted PostgreSQL client target is invalid")
    return [
        DOCKER,
        "run",
        "--rm",
        "-i",
        "--network",
        f"container:{container_id}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m,mode=0700",
        "--env",
        f"PGOPTIONS={pgoptions}",
        "--entrypoint",
        "/bin/sh",
        POSTGRES_AUDIT_IMAGES[postgres_major],
        "-ceu",
        TRUSTED_PSQL_PGPASS_SCRIPT,
        "psql",
        "-X",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        "127.0.0.1",
        *arguments,
    ]


TRUSTED_SERVER_BINARY_ROOTS = tuple(
    PurePosixPath(value)
    for value in (
        "/bin",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/ld.so.preload",
        "/lib",
        "/lib64",
        "/sbin",
        "/usr/bin",
        "/usr/lib",
        "/usr/lib64",
        "/usr/local/bin",
        "/usr/local/lib",
        "/usr/local/sbin",
        "/usr/sbin",
    )
)


def _path_overlaps_trusted_binary_root(value: str) -> bool:
    path = PurePosixPath(value)
    return any(
        path == root
        or root in path.parents
        or path in root.parents
        for root in TRUSTED_SERVER_BINARY_ROOTS
    )


def _process_namespace_epoch(pid: int) -> dict[str, object]:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise MediaEvidenceError(
            "online PostgreSQL container PID is invalid"
        )
    process_root = Path("/proc") / str(pid)
    stat_path = process_root / "stat"
    descriptor = os.open(
        stat_path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor, 64 * 1024)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        )
        or len(payload) == 0
    ):
        raise MediaEvidenceError(
            "online PostgreSQL process identity changed"
        )
    closing = payload.rfind(b") ")
    if closing < 0:
        raise MediaEvidenceError(
            "online PostgreSQL process stat is malformed"
        )
    fields = payload[closing + 2 :].split()
    # /proc/<pid>/stat field 22 is process start time.  The suffix starts at
    # field 3, so the zero-based suffix index is 19.
    if len(fields) <= 19 or not fields[19].isdigit():
        raise MediaEvidenceError(
            "online PostgreSQL process start time is malformed"
        )
    mountinfo_descriptor = os.open(
        process_root / "mountinfo",
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        mountinfo_before = os.fstat(mountinfo_descriptor)
        mountinfo = _read_fd(
            mountinfo_descriptor,
            16 * 1024 * 1024,
        )
        mountinfo_after = os.fstat(mountinfo_descriptor)
    finally:
        os.close(mountinfo_descriptor)
    if (
        not stat.S_ISREG(mountinfo_before.st_mode)
        or mountinfo_before.st_dev != mountinfo_after.st_dev
        or mountinfo_before.st_ino != mountinfo_after.st_ino
        or not mountinfo
    ):
        raise MediaEvidenceError(
            "online PostgreSQL mount namespace changed"
        )
    return {
        "pid": pid,
        "start_time_ticks": fields[19].decode("ascii"),
        "mountinfo_sha256": sha256_bytes(mountinfo),
    }


def _trusted_server_diff_projection(
    runner: CommandRunner,
    container_id: str,
) -> list[dict[str, str]]:
    completed = runner.run(
        [DOCKER, "diff", "--", container_id],
        timeout=60,
    )
    result: list[dict[str, str]] = []
    for raw_line in completed.stdout.decode("utf-8", "strict").splitlines():
        kind, separator, raw_path = raw_line.partition(" ")
        if (
            not separator
            or kind not in {"A", "C", "D"}
            or not raw_path.startswith("/")
            or "\x00" in raw_path
        ):
            raise MediaEvidenceError(
                "online PostgreSQL Docker diff is malformed"
            )
        path = PurePosixPath(raw_path).as_posix()
        if _path_overlaps_trusted_binary_root(path):
            raise MediaEvidenceError(
                "online PostgreSQL image binaries differ from the exact "
                "server image"
            )
        result.append({"kind": kind, "path": path})
    return sorted(result, key=canonical_json_bytes)


def _environment_map(
    value: object,
    *,
    context: str,
) -> dict[str, str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str)
        or "=" not in item
        or "\x00" in item
        or "\n" in item
        for item in value
    ):
        raise MediaEvidenceError(f"{context} environment is malformed")
    result: dict[str, str] = {}
    for item in value:
        name, setting = item.split("=", 1)
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name) is None
            or name in result
        ):
            raise MediaEvidenceError(
                f"{context} environment is ambiguous"
            )
        result[name] = setting
    return result


def _validate_trusted_server_launch(
    container: Mapping[str, object],
    image: Mapping[str, object],
) -> None:
    config = container["Config"]
    image_config = image.get("Config")
    if not isinstance(config, dict) or not isinstance(image_config, dict):
        raise MediaEvidenceError(
            "online PostgreSQL launch configuration is unavailable"
        )
    entrypoint = image_config.get("Entrypoint")
    command = image_config.get("Cmd")
    if (
        entrypoint != ["docker-entrypoint.sh"]
        or command != ["postgres"]
        or config.get("Entrypoint") != entrypoint
        or config.get("Cmd") != command
        or config.get("User") not in {None, ""}
        or image_config.get("User") not in {None, ""}
        or (config.get("WorkingDir") or "")
        != (image_config.get("WorkingDir") or "")
        or container.get("Path") != "docker-entrypoint.sh"
        or container.get("Args") != ["postgres"]
    ):
        raise MediaEvidenceError(
            "online PostgreSQL launch vector differs from its exact image"
        )
    base_environment = _environment_map(
        image_config.get("Env"),
        context="online PostgreSQL server image",
    )
    runtime_environment = _environment_map(
        config.get("Env"),
        context="online PostgreSQL server",
    )
    approved_overrides = {
        "POSTGRES_USER",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST_AUTH_METHOD",
        "PGDATA",
    }
    if (
        set(base_environment) - set(runtime_environment)
        or set(runtime_environment)
        - (set(base_environment) | approved_overrides)
        or any(
            runtime_environment[name] != setting
            for name, setting in base_environment.items()
            if name != "PGDATA"
        )
        or any(
            name.startswith("LD_")
            or name
            in {
                "DYLD_INSERT_LIBRARIES",
                "PYTHONPATH",
                "PERL5LIB",
                "RUBYLIB",
            }
            for name in runtime_environment
        )
        or runtime_environment.get("PATH") != base_environment.get("PATH")
    ):
        raise MediaEvidenceError(
            "online PostgreSQL environment differs from the exact server "
            "image allowlist"
        )


def _trusted_server_runtime_epoch(
    runner: CommandRunner,
    container_id: str,
    *,
    postgres_major: int,
) -> dict[str, object]:
    """Seal the server process and every namespace used by an external client."""

    container = _optional_docker_inspect(
        runner,
        "container",
        container_id,
    )
    if (
        container is None
        or container.get("Id") != container_id
        or not isinstance(container.get("Config"), dict)
        or not isinstance(container.get("HostConfig"), dict)
        or not isinstance(container.get("State"), dict)
        or not isinstance(container.get("Mounts"), list)
        or not isinstance(container.get("NetworkSettings"), dict)
    ):
        raise MediaEvidenceError(
            "online PostgreSQL trusted-client container identity differs"
        )
    config = container["Config"]
    host = container["HostConfig"]
    state = container["State"]
    mounts = container["Mounts"]
    network = container["NetworkSettings"]
    image_id = container.get("Image")
    pid = state.get("Pid")
    trusted_images = TRUSTED_POSTGRES_SERVER_IMAGES.get(
        postgres_major,
        {},
    )
    expected_repo_digest = (
        trusted_images.get(image_id)
        if isinstance(image_id, str)
        else None
    )
    if (
        not isinstance(image_id, str)
        or DIGEST_RE.fullmatch(image_id) is None
        or not isinstance(expected_repo_digest, str)
        or state.get("Status") not in ACTIVE_CONTAINER_STATES
        or not isinstance(state.get("StartedAt"), str)
        or not state["StartedAt"]
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(container.get("RestartCount"), bool)
        or not isinstance(container.get("RestartCount"), int)
        or container["RestartCount"] < 0
        or any(not isinstance(mount, dict) for mount in mounts)
    ):
        raise MediaEvidenceError(
            "online PostgreSQL trusted-client runtime is incomplete"
        )
    image = _optional_docker_inspect(
        runner,
        "image",
        image_id,
    )
    if (
        image is None
        or image.get("Id") != image_id
        or not isinstance(image.get("RepoDigests"), list)
        or expected_repo_digest not in image["RepoDigests"]
    ):
        raise MediaEvidenceError(
            "online PostgreSQL server image differs from static authority"
        )
    _validate_trusted_server_launch(container, image)
    empty_namespace_modes = (
        host.get("PidMode") in {None, ""}
        and host.get("UsernsMode") in {None, ""}
        and host.get("UTSMode") in {None, ""}
        and host.get("IpcMode") in {None, "", "private"}
    )
    network_mode = host.get("NetworkMode")
    sandbox_id = network.get("SandboxID")
    sandbox_key = network.get("SandboxKey")
    tmpfs = host.get("Tmpfs")
    if (
        host.get("Privileged") not in {None, False}
        or host.get("CapAdd") not in (None, [])
        or not empty_namespace_modes
        or not isinstance(network_mode, str)
        or not network_mode
        or network_mode == "host"
        or (
            isinstance(network_mode, str)
            and network_mode.startswith("container:")
        )
        or host.get("Devices") not in (None, [])
        or host.get("DeviceRequests") not in (None, [])
        or host.get("SecurityOpt") not in (None, [], ["no-new-privileges"])
        or host.get("Runtime") not in {None, "", "runc"}
        or host.get("CgroupnsMode") not in {None, "", "private"}
        or tmpfs is not None
        and not isinstance(tmpfs, dict)
        or not isinstance(sandbox_id, str)
        or DIGEST_RE.fullmatch("sha256:" + sandbox_id) is None
        or not isinstance(sandbox_key, str)
        or not sandbox_key.startswith("/var/run/docker/netns/")
        or PurePosixPath(sandbox_key).name != sandbox_id[:12]
    ):
        raise MediaEvidenceError(
            "online PostgreSQL HostConfig exceeds the trusted-client "
            "allowlist"
        )
    if isinstance(tmpfs, dict):
        for destination, options in tmpfs.items():
            if (
                not isinstance(destination, str)
                or not PurePosixPath(destination).is_absolute()
                or not isinstance(options, str)
                or _path_overlaps_trusted_binary_root(destination)
            ):
                raise MediaEvidenceError(
                    "online PostgreSQL tmpfs masks a trusted binary path"
                )
    volume_epochs: list[dict[str, object]] = []
    for mount in mounts:
        destination = mount.get("Destination")
        if (
            not isinstance(destination, str)
            or not PurePosixPath(destination).is_absolute()
            or _path_overlaps_trusted_binary_root(destination)
        ):
            raise MediaEvidenceError(
                "online PostgreSQL mount masks a trusted binary path"
            )
        propagation = mount.get("Propagation")
        if propagation not in {None, "", "rprivate", "private"}:
            raise MediaEvidenceError(
                "online PostgreSQL mount propagation is not private"
            )
        if mount.get("Type") != "volume":
            continue
        name = mount.get("Name")
        if not isinstance(name, str) or VOLUME_RE.fullmatch(name) is None:
            raise MediaEvidenceError(
                "online PostgreSQL volume mount identity is invalid"
            )
        volume = _optional_docker_inspect(
            runner,
            "volume",
            name,
        )
        if (
            volume is None
            or volume.get("Name") != name
            or volume.get("Driver") != "local"
            or volume.get("Scope") not in {None, "local"}
            or volume.get("Options") not in (None, {})
        ):
            raise MediaEvidenceError(
                "online PostgreSQL volume is not an exact local volume"
            )
        volume_epochs.append(
            {
                "name": name,
                "inspect_sha256": sha256_bytes(
                    canonical_json_bytes(volume)
                ),
            }
        )
    peer_ids = [
        value
        for value in runner.run(
            [DOCKER, "ps", "-aq", "--no-trunc"],
            timeout=60,
        )
        .stdout.decode("ascii", "strict")
        .splitlines()
        if value
    ]
    if any(CONTAINER_RE.fullmatch(value) is None for value in peer_ids):
        raise MediaEvidenceError(
            "Docker returned an invalid network-peer container identity"
        )
    for peer_id in sorted(set(peer_ids)):
        if peer_id == container_id:
            continue
        peer = _optional_docker_inspect(
            runner,
            "container",
            peer_id,
        )
        if peer is None:
            raise MediaEvidenceError(
                "Docker network-peer inventory changed"
            )
        peer_state = peer.get("State")
        peer_network = peer.get("NetworkSettings")
        if (
            isinstance(peer_state, dict)
            and peer_state.get("Status") in ACTIVE_CONTAINER_STATES
            and isinstance(peer_network, dict)
            and peer_network.get("SandboxID") == sandbox_id
        ):
            raise MediaEvidenceError(
                "online PostgreSQL network namespace has an existing peer"
            )
    process = _process_namespace_epoch(int(pid))
    return {
        "container_id": container_id,
        "image_id": image_id,
        "started_at": state["StartedAt"],
        "restart_count": container["RestartCount"],
        "process": process,
        "network_sandbox_id": sandbox_id,
        "network_sandbox_key": sandbox_key,
        "server_repo_digest": expected_repo_digest,
        "server_image_inspect_sha256": sha256_bytes(
            canonical_json_bytes(image)
        ),
        "config_sha256": sha256_bytes(canonical_json_bytes(config)),
        "host_config_sha256": sha256_bytes(canonical_json_bytes(host)),
        "tmpfs": (
            dict(sorted(tmpfs.items()))
            if isinstance(tmpfs, dict)
            else {}
        ),
        "mounts_sha256": sha256_bytes(
            canonical_json_bytes(
                sorted(mounts, key=canonical_json_bytes)
            )
        ),
        "volumes": sorted(
            volume_epochs,
            key=lambda value: str(value["name"]),
        ),
        "critical_layer_diff": _trusted_server_diff_projection(
            runner,
            container_id,
        ),
    }


TRUSTED_SERVER_STARTUP_SETTINGS = (
    "shared_preload_libraries",
    "session_preload_libraries",
    "local_preload_libraries",
    "dynamic_library_path",
    "archive_mode",
    "archive_command",
    "archive_cleanup_command",
    "restore_command",
    "recovery_end_command",
    "ssl_passphrase_command",
    "ssl_passphrase_command_supports_reload",
    "jit_provider",
    "config_file",
    "hba_file",
    "ident_file",
    "data_directory",
)


def _trusted_server_startup_projection(
    runner: CommandRunner,
    *,
    container_id: str,
    postgres_major: int,
) -> dict[str, object]:
    """Independently parse startup configuration with the pinned clean binary."""

    container = _optional_docker_inspect(
        runner,
        "container",
        container_id,
    )
    if container is None:
        raise MediaEvidenceError(
            "online PostgreSQL startup configuration container disappeared"
        )
    pgdata = _container_pgdata(container)
    mounts = container.get("Mounts")
    if (
        pgdata is None
        or not isinstance(mounts, list)
        or postgres_major not in SUPPORTED_POSTGRES_AUDIT_MAJORS
    ):
        raise MediaEvidenceError(
            "online PostgreSQL startup configuration is unavailable"
        )
    matches: list[tuple[dict[str, object], str]] = []
    for mount in mounts:
        if (
            not isinstance(mount, dict)
            or mount.get("Type") != "volume"
            or not isinstance(mount.get("Destination"), str)
            or not isinstance(mount.get("Name"), str)
            or VOLUME_RE.fullmatch(str(mount["Name"])) is None
        ):
            continue
        subpath = _mount_pg_subpath(str(mount["Destination"]), pgdata)
        if subpath is not None:
            matches.append((mount, subpath))
    if len(matches) != 1:
        raise MediaEvidenceError(
            "trusted startup parser requires one exact whole-volume PGDATA"
        )
    mount, _data_subpath = matches[0]
    volume_name = str(mount["Name"])
    mount_destination = str(mount["Destination"])
    quoted_pgdata = shlex.quote(pgdata)
    setting_words = " ".join(TRUSTED_SERVER_STARTUP_SETTINGS)
    completed = runner.run(
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            f"{POSTGRES_UID}:{POSTGRES_GID}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m,mode=0700",
            "--mount",
            (
                f"type=volume,src={volume_name},"
                f"dst={mount_destination},readonly"
            ),
            "--entrypoint",
            "/bin/sh",
            POSTGRES_AUDIT_IMAGES[postgres_major],
            "-ceu",
            (
                f"root={quoted_pgdata}; "
                "test -d \"$root\"; test ! -L \"$root\"; "
                "for key in "
                + setting_words
                + "; do printf 'S\\t%s\\t' \"$key\"; "
                "postgres -D \"$root\" -C \"$key\"; done; "
                "for file in postgresql.conf pg_hba.conf pg_ident.conf; do "
                "test -f \"$root/$file\"; test ! -L \"$root/$file\"; done; "
                "test ! -L \"$root/postgresql.auto.conf\"; "
                "find \"$root\" -xdev -type l "
                "\\( -name '*.conf' -o -name 'postgresql.auto.conf' \\) "
                "-print -quit | grep -q . && exit 97 || true; "
                "find \"$root\" -xdev -type f "
                "\\( -name '*.conf' -o -name 'postgresql.auto.conf' \\) "
                "-exec /bin/sh -ceu '"
                "root=$1; shift; "
                "for file do "
                "relative=${file#\"$root\"/}; "
                "case \"$relative\" in "
                "*[!A-Za-z0-9._/-]*) exit 99;; esac; "
                "digest=$(sha256sum \"$file\" | cut -d \" \" -f 1); "
                "printf \"C\\t%s\\t%s\\n\" \"$relative\" \"$digest\"; "
                "done' nexpoly-config \"$root\" {} +"
            ),
        ],
        timeout=120,
    )
    settings: dict[str, str] = {}
    configuration_files: list[dict[str, str]] = []
    for line in completed.stdout.decode("utf-8", "strict").splitlines():
        fields = line.split("\t")
        if (
            len(fields) == 3
            and fields[0] == "S"
            and fields[1] in TRUSTED_SERVER_STARTUP_SETTINGS
            and fields[1] not in settings
        ):
            settings[fields[1]] = fields[2]
        elif (
            len(fields) == 3
            and fields[0] == "C"
            and re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", fields[1])
            is not None
            and not PurePosixPath(fields[1]).is_absolute()
            and ".." not in PurePosixPath(fields[1]).parts
            and re.fullmatch(r"[0-9a-f]{64}", fields[2]) is not None
        ):
            configuration_files.append(
                {
                    "path": f"{pgdata}/{fields[1]}",
                    "sha256": "sha256:" + fields[2],
                }
            )
        else:
            raise MediaEvidenceError(
                "trusted PostgreSQL startup parser output is malformed"
            )
    configuration_files.sort(key=canonical_json_bytes)
    if (
        not configuration_files
        or len(configuration_files) > 1024
        or len(
            {
                str(value["path"])
                for value in configuration_files
            }
        )
        != len(configuration_files)
    ):
        raise MediaEvidenceError(
            "trusted PostgreSQL startup configuration inventory is invalid"
        )
    configuration_digest = sha256_bytes(
        canonical_json_bytes(configuration_files)
    )
    expected_paths = {
        "config_file": f"{pgdata}/postgresql.conf",
        "hba_file": f"{pgdata}/pg_hba.conf",
        "ident_file": f"{pgdata}/pg_ident.conf",
        "data_directory": pgdata,
    }
    if (
        set(settings) != set(TRUSTED_SERVER_STARTUP_SETTINGS)
        or settings["shared_preload_libraries"] != ""
        or settings["session_preload_libraries"] != ""
        or settings["local_preload_libraries"] != ""
        or settings["dynamic_library_path"] != "$libdir"
        or settings["archive_mode"] != "off"
        or settings["archive_command"] != ""
        or settings["archive_cleanup_command"] != ""
        or settings["restore_command"] != ""
        or settings["recovery_end_command"] != ""
        or settings["ssl_passphrase_command"] != ""
        or settings["ssl_passphrase_command_supports_reload"] != "off"
        or settings["jit_provider"] != "llvmjit"
        or any(settings[key] != value for key, value in expected_paths.items())
    ):
        raise MediaEvidenceError(
            "online PostgreSQL startup configuration can load untrusted code"
        )
    return {
        "settings": settings,
        "configuration_tree_sha256": configuration_digest,
        "configuration_files": configuration_files,
        "volume_name": volume_name,
        "mount_destination": mount_destination,
        "pgdata": pgdata,
    }


def _run_trusted_psql(
    runner: CommandRunner,
    *,
    container_id: str,
    postgres_major: int,
    pgoptions: str,
    arguments: Sequence[str],
    input_bytes: bytes | None = None,
    timeout: int = 600,
    expected_image_id: str,
    startup_sink: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a source-pinned client with a server/image/process CAS fence."""

    before_server = _trusted_server_runtime_epoch(
        runner,
        container_id,
        postgres_major=postgres_major,
    )
    before_image = _local_audit_image_id(
        runner,
        POSTGRES_AUDIT_IMAGES[postgres_major],
    )
    if (
        DIGEST_RE.fullmatch(expected_image_id) is None
        or before_image != expected_image_id
    ):
        raise MediaEvidenceError(
            "trusted PostgreSQL client image differs from authority"
        )
    before_startup = _trusted_server_startup_projection(
        runner,
        container_id=container_id,
        postgres_major=postgres_major,
    )
    inspected_user, _bootstrap_database, trust_mode = (
        _online_container_connection(runner, container_id)
    )
    credential = _inherited_database_credential(
        container_id=container_id,
        postgres_major=postgres_major,
        online_admin_role=inspected_user,
    )
    if credential is not None and trust_mode:
        raise MediaEvidenceError(
            "installed PostgreSQL media launcher rejects trust mode"
        )
    if credential is None and not trust_mode:
        raise MediaEvidenceError(
            "online PostgreSQL TCP audit lacks the inherited sealed credential"
        )
    password = credential.password if credential is not None else ""
    selected_users = [
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == "-U"
    ]
    selected_databases = [
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == "-d"
    ]
    if (
        len(selected_users) != 1
        or selected_users[0] != inspected_user
        or len(selected_databases) != 1
        or not _valid_pg_identifier(selected_databases[0])
        or "=" in selected_databases[0]
        or selected_databases[0].startswith(
            ("postgresql://", "postgres://")
        )
    ):
        raise MediaEvidenceError(
            "trusted PostgreSQL client identity differs from container authority"
        )
    if any(
        value
        in {
            "-h",
            "--host",
            "-p",
            "--port",
            "-W",
            "--password",
            "--dbname",
            "--service",
        }
        or value.startswith(
            (
                "--host=",
                "--port=",
                "--dbname=",
                "--service=",
            )
        )
        for value in arguments
    ):
        raise MediaEvidenceError(
            "trusted PostgreSQL client arguments override connection authority"
        )

    def run_client(
        client_arguments: Sequence[str],
        client_input: bytes | None,
    ) -> subprocess.CompletedProcess[bytes]:
        return runner.run(
            _trusted_psql_command(
                container_id=container_id,
                postgres_major=postgres_major,
                pgoptions=pgoptions,
                arguments=client_arguments,
            ),
            input_bytes=_trusted_psql_input(password, client_input),
            timeout=timeout,
            env=fixed_environment(),
        )

    if credential is not None:
        system_identity = run_client(
            [
                "-U",
                inspected_user,
                "-d",
                selected_databases[0],
                "-c",
                (
                    "SELECT system_identifier::text "
                    "FROM pg_catalog.pg_control_system();"
                ),
            ],
            None,
        ).stdout
        try:
            identity_lines = system_identity.decode(
                "ascii",
                "strict",
            ).splitlines()
        except UnicodeError as exc:
            raise MediaEvidenceError(
                "sealed PostgreSQL system identity is unavailable"
            ) from exc
        if (
            len(identity_lines) != 1
            or PG_SYSTEM_ID_RE.fullmatch(identity_lines[0]) is None
            or identity_lines[0]
            != credential.cluster_system_identifier
        ):
            raise MediaEvidenceError(
                "sealed PostgreSQL system identity differs"
            )
        if (
            _inherited_database_credential(
                container_id=container_id,
                postgres_major=postgres_major,
                online_admin_role=inspected_user,
            )
            != credential
        ):
            raise MediaEvidenceError(
                "database credential envelope changed before SQL"
            )

    completed = run_client(
        arguments,
        input_bytes,
    )
    if credential is not None and (
        _inherited_database_credential(
            container_id=container_id,
            postgres_major=postgres_major,
            online_admin_role=inspected_user,
        )
        != credential
    ):
        raise MediaEvidenceError(
            "database credential envelope changed during SQL"
        )
    after_image = _local_audit_image_id(
        runner,
        POSTGRES_AUDIT_IMAGES[postgres_major],
    )
    after_startup = _trusted_server_startup_projection(
        runner,
        container_id=container_id,
        postgres_major=postgres_major,
    )
    after_server = _trusted_server_runtime_epoch(
        runner,
        container_id,
        postgres_major=postgres_major,
    )
    if (
        before_image != after_image
        or before_server != after_server
        or before_startup != after_startup
    ):
        raise MediaEvidenceError(
            "trusted PostgreSQL client execution epoch changed"
        )
    if startup_sink is not None:
        if startup_sink:
            raise MediaEvidenceError(
                "trusted PostgreSQL startup evidence sink is not empty"
            )
        startup_sink.update(before_startup)
    return completed


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
    def authority(self) -> dict[str, object]:
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
        authority: Mapping[str, object],
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
        normalized_authority = _normalize_scratch_authority(authority)
        initial = _seal_scratch_journal(
            {
                "schema_version": SCRATCH_SCHEMA_VERSION,
                "operation_id": operation_id,
                "owner_token": owner_token,
                "uid": os.geteuid(),
                "authority": normalized_authority,
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
        sequence_name = _scratch_sequence_name(validated)
        existing = [
            name
            for name in _scratch_journal_entries(self.evidence_root)
            if name.startswith(f"{self.operation_id}.seq-")
        ]
        if (
            sequence_name not in existing
            and len(existing) >= MAX_SCRATCH_SEQUENCE_FILES
        ):
            raise MediaEvidenceError(
                "scratch immutable journal sequence bound reached"
            )
        existing_bytes = sum(
            os.lstat(
                _scratch_journal_root(self.evidence_root) / name
            ).st_size
            for name in existing
        )
        if (
            sequence_name not in existing
            and existing_bytes + len(payload)
            > MAX_SCRATCH_SEQUENCE_BYTES
        ):
            raise MediaEvidenceError(
                "scratch immutable journal byte bound reached"
            )
        _write_private_atomic(
            _scratch_journal_root(self.evidence_root),
            sequence_name,
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
        durable = _load_current_scratch_head(
            self.journal_path,
            evidence_root=self.evidence_root,
            expected=current,
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

    def _compact_terminal_journal(self) -> None:
        if self.journal["phase"] not in SCRATCH_TERMINAL_PHASES:
            raise MediaEvidenceError(
                "non-terminal scratch journal cannot be compacted"
            )
        keep = _scratch_sequence_name(self.journal)
        root = _scratch_journal_root(self.evidence_root)
        directory = _open_directory_chain(root, private_from=root)
        try:
            prefix = f"{self.operation_id}.seq-"
            for name in sorted(os.listdir(directory)):
                if name.startswith(prefix) and name != keep:
                    if SCRATCH_SEQUENCE_RE.fullmatch(name) is None:
                        raise MediaEvidenceError(
                            "scratch immutable sequence name is invalid"
                        )
                    os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)

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
        allowed_images = _scratch_allowed_images(self.authority)
        image_indexes = [
            index
            for index, argument in enumerate(arguments[2:], start=2)
            if argument in allowed_images
        ]
        if len(image_indexes) != 1:
            raise MediaEvidenceError(
                "owned scratch command does not use exactly one authority image"
            )
        image_index = image_indexes[0]
        selected_image = str(arguments[image_index])
        selected_image_id = allowed_images[selected_image]
        selected_major = next(
            major
            for major, image in POSTGRES_AUDIT_IMAGES.items()
            if image == selected_image
        )
        postgres_volume_root = (
            "/var/lib/postgresql"
            if selected_major == 18
            else "/var/lib/postgresql/data"
        )
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
            value.get("destination") == postgres_volume_root
            for value in effective_mounts
        )
        if needs_pgdata_tmpfs:
            effective_mounts.append(
                {
                    "kind": "tmpfs",
                    "source": None,
                    "destination": postgres_volume_root,
                    "read_only": False,
                }
            )
            expected_tmpfs[postgres_volume_root] = (
                "rw,noexec,nosuid,size=1m,mode=0700"
            )
        resource = self._plan_resource(
            resource_key,
            kind="container",
            spec={
                "postgres_image": selected_image,
                "postgres_image_id": selected_image_id,
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
                        f"{postgres_volume_root}:"
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
            self._compact_terminal_journal()
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
            self._compact_terminal_journal()
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


def _open_bind_source_without_symlinks(path: Path) -> int:
    """Open one absolute bind source, allowing a regular file or directory."""

    parts = _absolute_parts(path)
    if not parts:
        raise MediaEvidenceError("the filesystem root cannot be a bind source")
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for component in parts[:-1]:
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
        child = os.open(
            parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=descriptor,
        )
        os.close(descriptor)
        return child
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


def _takeover_seal_digest(value: object) -> tuple[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "records", "digest"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("records"), list)
        or not value["records"]
        or not isinstance(value.get("digest"), str)
        or DIGEST_RE.fullmatch(value["digest"]) is None
    ):
        raise MediaEvidenceError(
            "completed takeover backup seal is invalid"
        )
    unsigned = {
        "schema_version": 1,
        "records": value["records"],
    }
    if sha256_bytes(canonical_json_bytes(unsigned)) != value["digest"]:
        raise MediaEvidenceError(
            "completed takeover backup seal digest differs"
        )
    seen: dict[str, str] = {}
    ordered_paths: list[str] = []
    required_sha256: str | None = None
    private_records: list[dict[str, object]] = []
    for record in value["records"]:
        if not isinstance(record, dict):
            raise MediaEvidenceError(
                "completed takeover backup seal record is invalid"
            )
        kind = record.get("type")
        common = {"path", "type", "mode", "uid", "gid"}
        expected_fields = {
            "file": common | {"size", "sha256"},
            "directory": common,
        }.get(kind)
        path = record.get("path")
        if (
            expected_fields is None
            or set(record) != expected_fields
            or not isinstance(path, str)
            or path in seen
            or not isinstance(record.get("mode"), str)
            or re.fullmatch(r"[0-7]{4}", record["mode"]) is None
            or isinstance(record.get("uid"), bool)
            or not isinstance(record.get("uid"), int)
            or record["uid"] < 0
            or isinstance(record.get("gid"), bool)
            or not isinstance(record.get("gid"), int)
            or record["gid"] < 0
        ):
            raise MediaEvidenceError(
                "completed takeover backup seal record is invalid"
            )
        if kind == "file" and (
            isinstance(record.get("size"), bool)
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
            or not isinstance(record.get("sha256"), str)
            or DIGEST_RE.fullmatch(record["sha256"]) is None
        ):
            raise MediaEvidenceError(
                "completed takeover backup file seal is invalid"
            )
        pure = PurePosixPath(path)
        if (
            path != "."
            and (
                pure.is_absolute()
                or str(pure) != path
                or any(part in {"", ".", ".."} for part in pure.parts)
            )
        ):
            raise MediaEvidenceError(
                "completed takeover backup seal path is invalid"
            )
        if path != ".":
            parent = str(pure.parent)
            if parent not in seen or seen[parent] != "directory":
                raise MediaEvidenceError(
                    "completed takeover backup seal path overlaps a file"
                )
        if path == REQUIRED_TAKEOVER_BACKUP_NAME:
            if kind != "file" or required_sha256 is not None:
                raise MediaEvidenceError(
                    "completed takeover required dump seal is ambiguous"
                )
            required_sha256 = str(record["sha256"])
        private_records.append(
            {
                **record,
                "mode": "0700" if kind == "directory" else "0600",
            }
        )
        seen[path] = str(kind)
        ordered_paths.append(path)
    if (
        value["records"][0].get("path") != "."
        or value["records"][0].get("type") != "directory"
        or ordered_paths
        != [".", *sorted(ordered_paths[1:], key=os.fsencode)]
        or required_sha256 is None
    ):
        raise MediaEvidenceError(
            "completed takeover required dump seal is absent"
        )
    private_unsigned = {
        "schema_version": 1,
        "records": private_records,
    }
    return (
        sha256_bytes(canonical_json_bytes(private_unsigned)),
        required_sha256,
    )


def _read_takeover_operation(path: Path) -> tuple[dict[str, object], str]:
    descriptor = open_private_regular(path, root=DEFAULT_RUNTIME_ROOT)
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(descriptor, MAX_TAKEOVER_STATE_BYTES)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    reopened = open_private_regular(path, root=DEFAULT_RUNTIME_ROOT)
    try:
        reopened_metadata = os.fstat(reopened)
    finally:
        os.close(reopened)

    def stable(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_nlink,
        )

    if (
        stable(before) != stable(after)
        or stable(after) != stable(reopened_metadata)
    ):
        raise MediaEvidenceError(
            "completed takeover operation changed while being read"
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError(
            "completed takeover operation is not JSON"
        ) from exc
    if (
        not isinstance(value, dict)
        or canonical_json_bytes(value) + b"\n" != payload
    ):
        raise MediaEvidenceError(
            "completed takeover operation is not canonical"
        )
    return value, sha256_bytes(payload)


def _read_installed_private_file(
    path: Path,
    *,
    mode: int,
    maximum_bytes: int = MAX_TAKEOVER_STATE_BYTES,
) -> bytes:
    _absolute_parts(path)

    def open_once() -> int:
        parent = _open_directory_chain(
            path.parent,
            private_from=DEFAULT_RUNTIME_ROOT,
        )
        try:
            return os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        finally:
            os.close(parent)

    descriptor = open_once()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_nlink != 1
        ):
            raise MediaEvidenceError(
                f"installed takeover file is unsafe: {path}"
            )
        payload = _read_fd(descriptor, maximum_bytes)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    reopened = open_once()
    try:
        reopened_metadata = os.fstat(reopened)
    finally:
        os.close(reopened)

    def stable(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_nlink,
        )

    if (
        stable(before) != stable(after)
        or stable(after) != stable(reopened_metadata)
    ):
        raise MediaEvidenceError(
            f"installed takeover file changed while read: {path}"
        )
    return payload


def _load_bootstrap_takeover_binding() -> tuple[
    dict[str, object],
    dict[str, object],
]:
    payload = _read_installed_private_file(
        DEFAULT_RUNTIME_ROOT / "state/bootstrap-control.json",
        mode=0o600,
        maximum_bytes=MAX_REGISTRY_BYTES,
    )
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError(
            "completed bootstrap authority is not JSON"
        ) from exc
    fields = {
        "schema_version",
        "status",
        "source_sha",
        "source_tree",
        "source_readiness",
        "source_readiness_sha256",
        "legacy_takeover",
        "delivery_gate",
        "production_repository",
        "immutable_files",
        "worker_unit_takeover",
        "candidate_control",
        "active_control",
    }
    source_sha = record.get("source_sha") if isinstance(record, dict) else None
    source_tree = (
        record.get("source_tree") if isinstance(record, dict) else None
    )
    readiness = (
        record.get("source_readiness")
        if isinstance(record, dict)
        else None
    )
    if (
        not isinstance(record, dict)
        or set(record) != fields
        or record.get("schema_version") != 2
        or record.get("status") != "completed"
        or not isinstance(source_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or not isinstance(source_tree, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_tree) is None
        or not isinstance(readiness, dict)
        or readiness.get("ready") is not True
        or readiness.get("source_sha") != source_sha
        or readiness.get("source_tree") != source_tree
        or record.get("source_readiness_sha256")
        != sha256_bytes(canonical_json_bytes(readiness))
    ):
        raise MediaEvidenceError(
            "completed bootstrap authority is invalid"
        )
    for name in ("candidate_control", "active_control"):
        control = record.get(name)
        if (
            not isinstance(control, dict)
            or control.get("source_sha") != source_sha
            or control.get("source_tree") != source_tree
        ):
            raise MediaEvidenceError(
                "bootstrap control release authority differs"
            )
    immutable = record.get("immutable_files")
    if (
        not isinstance(immutable, dict)
        or set(immutable) != BOOTSTRAP_IMMUTABLE_FILES
        or any(
            not isinstance(digest, str)
            or DIGEST_RE.fullmatch(digest) is None
            for digest in immutable.values()
        )
    ):
        raise MediaEvidenceError(
            "bootstrap immutable router authority differs"
        )
    bin_root = DEFAULT_RUNTIME_ROOT / "bin"
    descriptor = _open_directory_chain(
        bin_root,
        private_from=DEFAULT_RUNTIME_ROOT,
    )
    try:
        names = set(os.listdir(descriptor))
    finally:
        os.close(descriptor)
    if names != BOOTSTRAP_IMMUTABLE_FILES:
        raise MediaEvidenceError(
            "bootstrap immutable router inventory differs"
        )
    for name, digest in immutable.items():
        installed = _read_installed_private_file(
            bin_root / name,
            mode=0o700,
            maximum_bytes=MAX_REGISTRY_BYTES,
        )
        if sha256_bytes(installed) != digest:
            raise MediaEvidenceError(
                f"bootstrap immutable router changed: {name}"
            )
    takeover = record.get("legacy_takeover")
    takeover_fields = {
        "schema_version",
        "operation_id",
        "authority_sha",
        "authority_tree",
        "install_manifest_sha256",
        "classification_sha256",
        "runtime_identity_sha256",
        "git_identity",
        "pre_stopped_fence_sha256",
        "control_layout_sha256",
        "checkout_permissions_sha256",
        "applied_record_sha256",
        "binding_sha256",
    }
    if (
        not isinstance(takeover, dict)
        or set(takeover) != takeover_fields
        or takeover.get("schema_version") != 1
        or takeover.get("authority_sha") != source_sha
        or takeover.get("authority_tree") != source_tree
        or takeover.get("git_identity") != EXPECTED_LEGACY_GIT_IDENTITY
        or any(
            not isinstance(takeover.get(name), str)
            or DIGEST_RE.fullmatch(takeover[name]) is None
            for name in (
                "install_manifest_sha256",
                "classification_sha256",
                "runtime_identity_sha256",
                "pre_stopped_fence_sha256",
                "control_layout_sha256",
                "checkout_permissions_sha256",
                "applied_record_sha256",
                "binding_sha256",
            )
        )
        or takeover.get("binding_sha256")
        != sha256_bytes(
            canonical_json_bytes(
                {
                    name: value
                    for name, value in takeover.items()
                    if name != "binding_sha256"
                }
            )
        )
    ):
        raise MediaEvidenceError(
            "completed bootstrap takeover binding differs"
        )
    return dict(record), dict(takeover)


def _verified_takeover_installation(
    takeover_binding: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, bytes]]:
    manifest_path = (
        DEFAULT_RUNTIME_ROOT / "legacy-takeover/INSTALL-MANIFEST.json"
    )
    manifest_payload = _read_installed_private_file(
        manifest_path,
        mode=0o600,
        maximum_bytes=MAX_REGISTRY_BYTES,
    )
    if (
        sha256_bytes(manifest_payload)
        != takeover_binding.get("install_manifest_sha256")
    ):
        raise MediaEvidenceError(
            "takeover install manifest differs from bootstrap authority"
        )
    try:
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError(
            "takeover install manifest is not JSON"
        ) from exc
    fields = {
        "schema_version",
        "authority_sha",
        "authority_tree",
        "source_hashes",
        "installed",
        "helper_report_sha256",
        "classification_sha256",
        "production_source_trust_sha256",
        "production_permission_takeover_sha256",
        "production_permission_inventory_sha256",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != fields
        or manifest.get("schema_version") != 1
        or manifest.get("authority_sha")
        != takeover_binding.get("authority_sha")
        or manifest.get("authority_tree")
        != takeover_binding.get("authority_tree")
        or manifest.get("classification_sha256")
        != takeover_binding.get("classification_sha256")
        or not isinstance(manifest.get("installed"), dict)
        or not isinstance(manifest.get("source_hashes"), dict)
        or any(
            not isinstance(value, str)
            or DIGEST_RE.fullmatch(value) is None
            for value in manifest["source_hashes"].values()
        )
    ):
        raise MediaEvidenceError(
            "takeover install manifest authority differs"
        )
    installed_payloads: dict[str, bytes] = {}
    for name, raw in manifest["installed"].items():
        if (
            not isinstance(name, str)
            or not isinstance(raw, dict)
            or set(raw) != {"path", "mode", "sha256"}
            or not isinstance(raw.get("path"), str)
            or not Path(raw["path"]).is_absolute()
            or raw.get("mode") not in {"0600", "0700"}
            or not isinstance(raw.get("sha256"), str)
            or DIGEST_RE.fullmatch(raw["sha256"]) is None
        ):
            raise MediaEvidenceError(
                "takeover installed-file manifest is invalid"
            )
        path = Path(raw["path"])
        allowed_parents = {
            DEFAULT_RUNTIME_ROOT / "config",
            DEFAULT_RUNTIME_ROOT / "legacy-takeover/bin",
        }
        if path.parent not in allowed_parents or path.name != name:
            raise MediaEvidenceError(
                "takeover installed-file path escapes its fixed root"
            )
        installed = _read_installed_private_file(
            path,
            mode=int(raw["mode"], 8),
        )
        if sha256_bytes(installed) != raw["sha256"]:
            raise MediaEvidenceError(
                f"takeover installed file changed: {name}"
            )
        installed_payloads[name] = installed
    if not TAKEOVER_EXECUTION_CLOSURE.issubset(installed_payloads):
        raise MediaEvidenceError(
            "takeover execution dependency closure is incomplete"
        )
    if (
        sha256_bytes(installed_payloads["postgres_media_evidence.py"])
        != _auditor_digest()
    ):
        raise MediaEvidenceError(
            "running media auditor differs from bootstrap F authority"
        )
    return dict(manifest), installed_payloads


def _exec_verified_module(
    name: str,
    payload: bytes,
    filename: Path,
) -> object:
    module = types.ModuleType(name)
    module.__file__ = str(filename)
    module.__package__ = ""
    sys.modules[name] = module
    try:
        code = compile(payload, f"verified:{filename}", "exec")
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _validate_completed_takeover_operation(
    operation_id: str,
    operation: dict[str, object],
) -> dict[str, object]:
    bootstrap, takeover_binding = _load_bootstrap_takeover_binding()
    manifest, installed = _verified_takeover_installation(
        takeover_binding
    )
    recovery_root = DEFAULT_RUNTIME_ROOT / "legacy-takeover/bin"
    prior_site = sys.modules.get("site_helper_contracts")
    prior_git = sys.modules.get("nexpoly_legacy_git_source_trust")
    try:
        _exec_verified_module(
            "site_helper_contracts",
            installed["site_helper_contracts.py"],
            recovery_root / "site_helper_contracts.py",
        )
        _exec_verified_module(
            "nexpoly_legacy_git_source_trust",
            installed["git_source_trust.py"],
            recovery_root / "git_source_trust.py",
        )
        controller_module = _exec_verified_module(
            "nexpoly_verified_takeover_controller_for_media",
            installed["legacy_takeover.py"],
            recovery_root / "legacy_takeover.py",
        )
        controller = controller_module.LegacyTakeover(
            repository=LEGACY_PRE_TAKEOVER_BACKUP_ROOT.parent,
            runtime_root=DEFAULT_RUNTIME_ROOT,
            external_roots=controller_module.EXTERNAL_ROOTS,
            system=object(),
        )
        validated_state = controller._load(operation_id)
        status = controller.status(operation_id)
    except Exception as exc:
        raise MediaEvidenceError(
            "completed takeover operation failed source-pinned validation"
        ) from exc
    finally:
        if prior_site is None:
            sys.modules.pop("site_helper_contracts", None)
        else:
            sys.modules["site_helper_contracts"] = prior_site
        if prior_git is None:
            sys.modules.pop("nexpoly_legacy_git_source_trust", None)
        else:
            sys.modules["nexpoly_legacy_git_source_trust"] = prior_git
    if validated_state != operation:
        raise MediaEvidenceError(
            "completed takeover operation changed across validators"
        )
    if (
        status.get("apply_phase") != "complete"
        or status.get("restore_phase") is not None
        or status.get("active") is not False
        or status.get("git_identity") != EXPECTED_LEGACY_GIT_IDENTITY
        or status.get("classification_sha256")
        != manifest.get("classification_sha256")
        or status.get("applied_record_sha256")
        != takeover_binding.get("applied_record_sha256")
        or status.get("pre_stopped_fence_sha256")
        != takeover_binding.get("pre_stopped_fence_sha256")
        or status.get("runtime_identity_sha256")
        != takeover_binding.get("runtime_identity_sha256")
        or status.get("control_layout_sha256")
        != takeover_binding.get("control_layout_sha256")
        or status.get("checkout_permissions_sha256")
        != takeover_binding.get("checkout_permissions_sha256")
        or any(
            move.get("status") != "externalized"
            or move.get("restore_status") != "pending"
            for move in status.get("moves", [])
            if isinstance(move, dict)
        )
    ):
        raise MediaEvidenceError(
            "completed takeover state differs from bootstrap authority"
        )
    binding = {
        "schema_version": 1,
        "operation_id": operation_id,
        "authority_sha": bootstrap["source_sha"],
        "authority_tree": bootstrap["source_tree"],
        "install_manifest_sha256": takeover_binding[
            "install_manifest_sha256"
        ],
        "classification_sha256": status["classification_sha256"],
        "runtime_identity_sha256": status["runtime_identity_sha256"],
        "git_identity": status["git_identity"],
        "pre_stopped_fence_sha256": status[
            "pre_stopped_fence_sha256"
        ],
        "control_layout_sha256": status["control_layout_sha256"],
        "checkout_permissions_sha256": status[
            "checkout_permissions_sha256"
        ],
        "applied_record_sha256": status["applied_record_sha256"],
    }
    binding["binding_sha256"] = sha256_bytes(
        canonical_json_bytes(binding)
    )
    if (
        binding != takeover_binding
    ):
        raise MediaEvidenceError(
            "completed takeover authority binding differs"
        )
    return dict(binding)


def _completed_takeover_backup_stage() -> dict[str, object]:
    if (
        TAKEOVER_ACTIVE_RECORD.exists()
        or TAKEOVER_ACTIVE_RECORD.is_symlink()
    ):
        raise MediaEvidenceError(
            "legacy takeover is active; media discovery is blocked"
        )
    legacy_exists = (
        LEGACY_PRE_TAKEOVER_BACKUP_ROOT.exists()
        or LEGACY_PRE_TAKEOVER_BACKUP_ROOT.is_symlink()
    )
    preserved_root = APPROVED_BACKUP_ROOTS[0]
    preserved_exists = (
        preserved_root.exists() or preserved_root.is_symlink()
    )
    if legacy_exists and not preserved_exists:
        raise MediaEvidenceError(
            "legacy takeover must complete before media evidence is built"
        )
    if legacy_exists or not preserved_exists:
        raise MediaEvidenceError(
            "takeover backup roots are not in the exact post-takeover state"
        )
    try:
        directory = _open_directory_chain(
            TAKEOVER_OPERATIONS_DIRECTORY,
            private_from=DEFAULT_RUNTIME_ROOT,
        )
    except OSError as exc:
        raise MediaEvidenceError(
            "completed takeover operation directory is unavailable"
        ) from exc
    try:
        directory_before = os.fstat(directory)
        names = sorted(os.listdir(directory))
    finally:
        os.close(directory)
    candidates: list[dict[str, object]] = []
    for name in names:
        if (
            not name.endswith(".json")
            or TAKEOVER_OPERATION_RE.fullmatch(name.removesuffix(".json"))
            is None
        ):
            raise MediaEvidenceError(
                "takeover operation directory has an unknown entry"
            )
        operation, operation_sha256 = _read_takeover_operation(
            TAKEOVER_OPERATIONS_DIRECTORY / name
        )
        if (
            operation.get("repository")
            != str(LEGACY_PRE_TAKEOVER_BACKUP_ROOT.parent)
            or operation.get("apply_phase") != "complete"
            or operation.get("restore_phase") is not None
        ):
            continue
        moves = operation.get("moves")
        if not isinstance(moves, list):
            raise MediaEvidenceError(
                "completed takeover operation has no move inventory"
            )
        matches = [
            move
            for move in moves
            if isinstance(move, dict) and move.get("path") == "backups"
        ]
        if len(matches) != 1:
            raise MediaEvidenceError(
                "completed takeover backup move is ambiguous"
            )
        move = matches[0]
        applied = operation.get("applied_record_sha256")
        operation_id = operation.get("operation_id")
        if (
            move.get("destination") != str(preserved_root)
            or move.get("status") != "externalized"
            or move.get("restore_status") != "pending"
            or not isinstance(operation_id, str)
            or TAKEOVER_OPERATION_RE.fullmatch(operation_id) is None
            or name != f"{operation_id}.json"
            or not isinstance(applied, str)
            or DIGEST_RE.fullmatch(applied) is None
        ):
            raise MediaEvidenceError(
                "completed takeover backup move binding differs"
            )
        binding = _validate_completed_takeover_operation(
            operation_id,
            operation,
        )
        runtime = operation.get("runtime")
        if (
            not isinstance(runtime, dict)
            or any(
                not isinstance(runtime.get(name), str)
                or CONTAINER_RE.fullmatch(runtime[name]) is None
                for name in (
                    "backend_container_id",
                    "web_container_id",
                )
            )
        ):
            raise MediaEvidenceError(
                "completed takeover stopped-reader identity differs"
            )
        externalized_moves = [
            {
                "index": current["index"],
                "path": current["path"],
                "class": current["class"],
                "source": str(
                    LEGACY_PRE_TAKEOVER_BACKUP_ROOT.parent
                    / current["path"]
                ),
                "destination": current["destination"],
                "seal_sha256": current["seal"]["digest"],
            }
            for current in operation["moves"]
        ]
        private_seal, required_sha256 = _takeover_seal_digest(
            move.get("seal")
        )
        candidates.append(
            {
                "stage": "post-takeover",
                "operation_id": operation_id,
                "operation_state_relative_path": (
                    f"operations/{name}"
                ),
                "operation_state_sha256": operation_sha256,
                "takeover_authority_sha": binding["authority_sha"],
                "takeover_authority_tree": binding["authority_tree"],
                "takeover_binding_sha256": binding["binding_sha256"],
                "legacy_stopped_container_ids": sorted(
                    [
                        runtime["backend_container_id"],
                        runtime["web_container_id"],
                    ]
                ),
                "externalized_moves": externalized_moves,
                "applied_record_sha256": applied,
                "original_backup_seal_sha256": move["seal"]["digest"],
                "private_backup_seal_sha256": private_seal,
                "required_media_id": (
                    media_id_for_locator(
                        "postgres_backup",
                        str(
                            preserved_root
                            / REQUIRED_TAKEOVER_BACKUP_NAME
                        ),
                    )
                ),
                "required_backup_sha256": required_sha256,
            }
        )
    if len(candidates) != 1:
        raise MediaEvidenceError(
            "exactly one completed takeover backup authority is required"
        )
    directory = _open_directory_chain(
        TAKEOVER_OPERATIONS_DIRECTORY,
        private_from=DEFAULT_RUNTIME_ROOT,
    )
    try:
        directory_after = os.fstat(directory)
        names_after = sorted(os.listdir(directory))
    finally:
        os.close(directory)
    if (
        directory_before.st_dev,
        directory_before.st_ino,
        directory_before.st_mtime_ns,
        directory_before.st_ctime_ns,
    ) != (
        directory_after.st_dev,
        directory_after.st_ino,
        directory_after.st_mtime_ns,
        directory_after.st_ctime_ns,
    ) or names != names_after or (
        TAKEOVER_ACTIVE_RECORD.exists()
        or TAKEOVER_ACTIVE_RECORD.is_symlink()
    ):
        raise MediaEvidenceError(
            "takeover authority changed during media discovery"
        )
    return candidates[0]


def _validated_takeover_operation_for_registry(
    registry: Registry,
) -> dict[str, object] | None:
    stage = registry.boundary.get("takeover_backup_stage")
    if not isinstance(stage, dict):
        return None
    operation_id = stage.get("operation_id")
    relative = stage.get("operation_state_relative_path")
    if (
        not isinstance(operation_id, str)
        or TAKEOVER_OPERATION_RE.fullmatch(operation_id) is None
        or relative != f"operations/{operation_id}.json"
    ):
        raise MediaEvidenceError(
            "runtime registry takeover operation binding is invalid"
        )
    operation, payload_sha256 = _read_takeover_operation(
        TAKEOVER_STATE_DIRECTORY / relative
    )
    binding = _validate_completed_takeover_operation(
        operation_id,
        operation,
    )
    summaries = [
        {
            "index": move["index"],
            "path": move["path"],
            "class": move["class"],
            "source": str(
                LEGACY_PRE_TAKEOVER_BACKUP_ROOT.parent / move["path"]
            ),
            "destination": move["destination"],
            "seal_sha256": move["seal"]["digest"],
        }
        for move in operation["moves"]
    ]
    runtime = operation["runtime"]
    if (
        payload_sha256 != stage.get("operation_state_sha256")
        or binding.get("binding_sha256")
        != stage.get("takeover_binding_sha256")
        or summaries != stage.get("externalized_moves")
        or sorted(
            [
                runtime["backend_container_id"],
                runtime["web_container_id"],
            ]
        )
        != stage.get("legacy_stopped_container_ids")
    ):
        raise MediaEvidenceError(
            "runtime registry takeover move authority changed"
        )
    return operation


def _project_takeover_move_seal(
    seal: Mapping[str, object],
    relative: PurePosixPath,
) -> dict[str, object]:
    records = seal.get("records")
    if not isinstance(records, list):
        raise MediaEvidenceError(
            "takeover redirect move has no sealed inventory"
        )
    prefix = "." if not relative.parts else relative.as_posix()
    selected: list[dict[str, object]] = []
    for raw in records:
        if not isinstance(raw, dict) or not isinstance(
            raw.get("path"), str
        ):
            raise MediaEvidenceError(
                "takeover redirect move seal is invalid"
            )
        current = str(raw["path"])
        if current == prefix:
            projected = "."
        elif prefix != "." and current.startswith(prefix + "/"):
            projected = current[len(prefix) + 1 :]
        elif prefix == ".":
            projected = current
        else:
            continue
        selected.append({**raw, "path": projected})
    if not selected or selected[0].get("path") != ".":
        raise MediaEvidenceError(
            "takeover redirect source is absent from its move seal"
        )
    unsigned = {"schema_version": 1, "records": selected}
    return {
        **unsigned,
        "digest": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def _takeover_bind_redirect(
    source: str,
    attachments: Sequence[Mapping[str, object]],
    operation: Mapping[str, object] | None,
    stage: Mapping[str, object] | None,
) -> tuple[Path, dict[str, object], dict[str, object]] | None:
    if operation is None or stage is None:
        return None
    original = Path(source)
    source_parts = _absolute_parts(original)
    matches: list[tuple[int, Mapping[str, object], Path]] = []
    for move in operation["moves"]:  # type: ignore[index]
        if not isinstance(move, dict):
            raise MediaEvidenceError(
                "takeover redirect move inventory is invalid"
            )
        move_source = (
            LEGACY_PRE_TAKEOVER_BACKUP_ROOT.parent
            / str(move["path"])
        )
        move_parts = _absolute_parts(move_source)
        if source_parts[: len(move_parts)] == move_parts:
            matches.append((len(move_parts), move, move_source))
    if not matches:
        return None
    matches.sort(key=lambda value: value[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        raise MediaEvidenceError(
            "takeover redirect bind matches multiple move roots"
        )
    if original.exists() or original.is_symlink():
        raise MediaEvidenceError(
            "externalized legacy bind source reappeared"
        )
    stopped = stage.get("legacy_stopped_container_ids")
    attachment_ids = {
        str(value.get("container_id")) for value in attachments
    }
    if (
        not isinstance(stopped, list)
        or not attachments
        or not attachment_ids.issubset(set(stopped))
        or any(
            str(value.get("state")) in ACTIVE_CONTAINER_STATES
            for value in attachments
        )
    ):
        raise MediaEvidenceError(
            "missing bind is not owned only by exact stopped legacy readers"
        )
    _length, move, move_source = matches[0]
    relative = PurePosixPath(
        *source_parts[len(_absolute_parts(move_source)) :]
    )
    destination = Path(str(move["destination"]))
    audit_path = (
        destination
        if not relative.parts
        else destination.joinpath(*relative.parts)
    )
    seal = _project_takeover_move_seal(
        move["seal"],  # type: ignore[arg-type]
        relative,
    )
    redirect = {
        "schema_version": 1,
        "operation_id": stage["operation_id"],
        "operation_state_sha256": stage["operation_state_sha256"],
        "move_index": move["index"],
        "move_seal_sha256": move["seal"]["digest"],  # type: ignore[index]
        "source_root": str(move_source),
        "destination_root": str(destination),
        "relative_path": "." if not relative.parts else relative.as_posix(),
        "audit_path": str(audit_path),
        "legacy_stopped_container_ids": list(
            stage["legacy_stopped_container_ids"]  # type: ignore[index]
        ),
    }
    return audit_path, seal, redirect


def seal_discovery_boundary(
    policy: DiscoveryPolicy,
) -> dict[str, object]:
    result = {
        **policy.document(),
        "backup_root_identities": [
            capture_backup_root_identity(root)
            for root in policy.backup_roots
        ],
    }
    if policy.backup_roots == APPROVED_BACKUP_ROOTS:
        result["takeover_backup_stage"] = (
            _completed_takeover_backup_stage()
        )
    return result


def _load_discovery_boundary(
    value: object,
    *,
    policy: DiscoveryPolicy,
) -> dict[str, object]:
    static = policy.document()
    stage_fields = (
        {"takeover_backup_stage"}
        if policy.backup_roots == APPROVED_BACKUP_ROOTS
        else set()
    )
    if (
        not isinstance(value, dict)
        or set(value)
        != {*static, "backup_root_identities", *stage_fields}
        or {name: value[name] for name in static} != static
        or not isinstance(value.get("backup_root_identities"), list)
        or len(value["backup_root_identities"]) != len(policy.backup_roots)
    ):
        raise MediaEvidenceError(
            "media registry narrowed or changed the fixed discovery boundary"
        )
    if stage_fields and value.get(
        "takeover_backup_stage"
    ) != _completed_takeover_backup_stage():
        raise MediaEvidenceError(
            "media registry takeover backup authority changed"
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
    result = {
        **static,
        "backup_root_identities": identities,
    }
    if stage_fields:
        result["takeover_backup_stage"] = value[
            "takeover_backup_stage"
        ]
    return result


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


def _fd_identity(
    descriptor: int,
    path: Path,
    *,
    include_digest: bool,
    maximum_bytes: int | None = None,
) -> dict[str, object]:
    metadata = os.fstat(descriptor)
    result: dict[str, object] = {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size_bytes": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }
    if include_digest:
        digest = hashlib.sha256()
        observed_size = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            observed_size += len(chunk)
            if (
                maximum_bytes is not None
                and observed_size > maximum_bytes
            ):
                raise MediaEvidenceError(
                    "private file exceeds its reviewed-content size limit"
                )
            digest.update(chunk)
        result["sha256"] = "sha256:" + digest.hexdigest()
    after = os.fstat(descriptor)
    if (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_nlink,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        stat.S_IMODE(after.st_mode),
        after.st_uid,
        after.st_nlink,
    ):
        raise MediaEvidenceError(
            "private file changed while its identity was streamed"
        )
    return result


def load_registry(
    path: Path,
    *,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    private_root: Path | None = None,
    authority_rules: bool = False,
) -> Registry | MediaAuthorityRules:
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
    authority_fields = {
        "schema_version",
        "discovery_boundary",
        "audit_runtime",
        "logical_media",
        "unmatched_media",
        "production_identity",
    }
    registry_fields = {
        "schema_version",
        "media_authority_rules_sha256",
        "reviewed_content_inventory_sha256",
        "discovery_boundary",
        "audit_runtime",
        "expected_media",
        "required_online_databases",
        "production_identity",
    }
    if (
        not isinstance(value, dict)
        or set(value)
        != (authority_fields if authority_rules else registry_fields)
        or value.get("schema_version") != (1 if authority_rules else 5)
    ):
        raise MediaEvidenceError(
            "media authority rules have an invalid shape"
            if authority_rules
            else "runtime media registry v5 has an invalid shape"
        )
    if authority_rules:
        if value.get("discovery_boundary") != policy.document():
            raise MediaEvidenceError(
                "media authority rules narrowed the fixed discovery boundary"
            )
        boundary: dict[str, object] = policy.document()
        unmatched = value.get("unmatched_media")
        if unmatched != {
            "non_postgres": (
                "active-complete-view-metadata-exclusion-or-"
                "inactive-private-content-review"
            ),
            "postgres": (
                "active-live-read-only-adjacent-or-inactive-"
                "isolated-ledger-audit"
            ),
            "postgres_backup": "full-isolated-ledger-audit",
        }:
            raise MediaEvidenceError(
                "media authority unmatched-media policy differs"
            )
        authority_rules_sha256 = sha256_bytes(payload)
    else:
        boundary = _load_discovery_boundary(
            value.get("discovery_boundary"),
            policy=policy,
        )
        authority_rules_sha256 = value.get(
            "media_authority_rules_sha256"
        )
        if (
            not isinstance(authority_rules_sha256, str)
            or DIGEST_RE.fullmatch(authority_rules_sha256) is None
        ):
            raise MediaEvidenceError(
                "runtime media registry lacks its authority-rules digest"
            )
    runtime = value.get("audit_runtime")
    production_identity = value.get("production_identity")
    if (
        not isinstance(production_identity, dict)
        or set(production_identity)
        != {
            "stack",
            "database",
            "kind",
            "media_id",
            "postgres_major",
            "system_identifier",
        }
        or production_identity.get("stack") != "production"
        or production_identity.get("database") != "nexpoly"
        or production_identity.get("kind") != "docker_volume"
        or production_identity.get("media_id")
        != "docker-volume:nexpoly_app_postgres_data"
        or production_identity.get("postgres_major") != POSTGRES_MAJOR
        or not isinstance(
            production_identity.get("system_identifier"),
            str,
        )
        or PG_SYSTEM_ID_RE.fullmatch(
            production_identity["system_identifier"]
        )
        is None
    ):
        raise MediaEvidenceError(
            "media authority production identity is invalid"
        )
    runtime_fields = {
        "postgres_image",
        "postgres_images",
        "postgres_major",
        "postgres_uid",
        "postgres_gid",
    }
    if not authority_rules:
        runtime_fields.update(
            {
                "auditor_sha256",
                "postgres_image_id",
            }
        )
    raw_images = (
        runtime.get("postgres_images")
        if isinstance(runtime, dict)
        else None
    )
    expected_images = {
        str(major): image
        for major, image in POSTGRES_AUDIT_IMAGES.items()
    }
    runtime_image_ids: dict[int, str] = {}
    runtime_images_valid = raw_images == expected_images
    if not authority_rules and isinstance(raw_images, dict):
        runtime_images_valid = set(raw_images) == set(expected_images)
        for raw_major, expected_reference in expected_images.items():
            record = raw_images.get(raw_major)
            if (
                not isinstance(record, dict)
                or set(record) != {"digest_ref", "image_id"}
                or record.get("digest_ref") != expected_reference
                or not isinstance(record.get("image_id"), str)
                or DIGEST_RE.fullmatch(record["image_id"]) is None
            ):
                runtime_images_valid = False
                continue
            runtime_image_ids[int(raw_major)] = str(
                record["image_id"]
            )
    if (
        not isinstance(runtime, dict)
        or set(runtime) != runtime_fields
        or runtime.get("postgres_major") != POSTGRES_MAJOR
        or isinstance(runtime.get("postgres_uid"), bool)
        or not isinstance(runtime.get("postgres_uid"), int)
        or runtime.get("postgres_uid") != POSTGRES_UID
        or isinstance(runtime.get("postgres_gid"), bool)
        or not isinstance(runtime.get("postgres_gid"), int)
        or runtime.get("postgres_gid") != POSTGRES_GID
        or not isinstance(runtime.get("postgres_image"), str)
        or runtime.get("postgres_image")
        != POSTGRES_AUDIT_IMAGES[POSTGRES_MAJOR]
        or not runtime_images_valid
        or not authority_rules
        and (
            not isinstance(runtime.get("auditor_sha256"), str)
            or DIGEST_RE.fullmatch(runtime["auditor_sha256"]) is None
            or not isinstance(runtime.get("postgres_image_id"), str)
            or DIGEST_RE.fullmatch(runtime["postgres_image_id"]) is None
            or runtime.get("postgres_image_id")
            != runtime_image_ids.get(POSTGRES_MAJOR)
        )
    ):
        raise MediaEvidenceError(
            "media registry audit runtime does not pin all supported majors"
        )
    if (
        not authority_rules
        and runtime["auditor_sha256"] != _auditor_digest()
    ):
        raise MediaEvidenceError(
            "media registry does not pin this source-pinned auditor"
        )
    audit_images = tuple(
        (major, POSTGRES_AUDIT_IMAGES[major])
        for major in SUPPORTED_POSTGRES_AUDIT_MAJORS
    )
    audit_image_ids = tuple(sorted(runtime_image_ids.items()))
    if authority_rules:
        logical_media = value.get("logical_media")
        if logical_media != LOGICAL_MEDIA_POLICY:
            raise MediaEvidenceError(
                "media authority logical classification rules differ"
            )
        return MediaAuthorityRules(
            payload=payload,
            digest=authority_rules_sha256,
            audit_image=POSTGRES_AUDIT_IMAGES[POSTGRES_MAJOR],
            auditor_sha256=_auditor_digest(),
            descriptors=(),
            required_online_databases=(),
            policy=policy,
            allow_unmatched_non_postgres=False,
            production_identity=dict(production_identity),
            audit_images=audit_images,
            logical_media=dict(logical_media),
            postgres_uid=runtime["postgres_uid"],
            postgres_gid=runtime["postgres_gid"],
        )
    reviewed_content_inventory_sha256 = value.get(
        "reviewed_content_inventory_sha256"
    )
    if (
        not isinstance(reviewed_content_inventory_sha256, str)
        or DIGEST_RE.fullmatch(reviewed_content_inventory_sha256) is None
    ):
        raise MediaEvidenceError(
            "runtime registry lacks reviewed-content inventory identity"
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
        "online_admin_role",
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
        online_admin_role = raw.get("online_admin_role")
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
            or not _valid_pg_identifier(database)
            or not _valid_pg_identifier(database_user)
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
                "reviewed-content-only",
                "metadata-only-exclusion",
                "live-read-only-adjacent",
                "unsupported-blocking",
            }
            or classification
            not in {
                "nexpoly-db",
                "reviewed-non-pg",
                "excluded-non-pg",
                "adjacent-postgres",
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
                or not _valid_pg_identifier(database_record.get("name"))
                or database_record["name"] in database_names
                or not isinstance(database_record.get("oid"), str)
                or not database_record["oid"].isdigit()
                or not _valid_pg_identifier(database_record.get("owner"))
                or not isinstance(
                    database_record.get("allow_connections"),
                    bool,
                )
                or database_record.get("template") is not False
                or database_record.get("migration_scope")
                not in {
                    "nexpoly-ledger",
                    "adjacent-record-only",
                    "auto-detect-adjacent",
                }
                or (
                    database_record.get("allow_connections") is True
                    and (
                        not _valid_pg_identifier(
                            database_record.get("audit_role")
                        )
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
            and not _valid_pg_identifier(online_admin_role)
            or classification == "nexpoly-db"
            and not live
            and online_admin_role is not None
            or kind == "postgres_backup"
            and method
            not in {
                "isolated-backup-restore-read-only",
                "unsupported-blocking",
            }
            or kind == "container_bind"
            and method
            not in {
                "live-read-only",
                "live-read-only-adjacent",
                "isolated-bind-copy-read-only",
                "reviewed-content-only",
                "metadata-only-exclusion",
                "unsupported-blocking",
            }
            or kind == "docker_volume"
            and method
            not in {
                "live-read-only",
                "live-read-only-adjacent",
                "isolated-volume-copy-read-only",
                "reviewed-content-only",
                "metadata-only-exclusion",
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
            or classification == "nexpoly-db"
            and live
            and source_postgres_major != POSTGRES_MAJOR
            or classification != "nexpoly-db"
            and classification != "adjacent-postgres"
            and (
                disposition != "excluded-from-nexpoly-migration"
                or online_admin_role is not None
                or databases
                or kind == "postgres_backup"
                and source_postgres_major is not None
                or method
                not in {
                    "reviewed-content-only",
                    "metadata-only-exclusion",
                    "unsupported-blocking",
                }
            )
            or classification == "reviewed-non-pg"
            and (
                method != "reviewed-content-only"
                or kind
                not in {
                    "docker_volume",
                    "container_bind",
                    "reviewed_file",
                }
                or source_postgres_major is not None
            )
            or classification == "excluded-non-pg"
            and (
                method != "metadata-only-exclusion"
                or kind not in {"docker_volume", "container_bind"}
                or source_postgres_major is not None
            )
            or classification == "adjacent-postgres"
            and (
                method != "live-read-only-adjacent"
                or kind not in {"docker_volume", "container_bind"}
                or disposition != "excluded-from-nexpoly-migration"
                or not _valid_pg_identifier(online_admin_role)
                or database_user != online_admin_role
                or source_postgres_major
                not in SUPPORTED_POSTGRES_AUDIT_MAJORS
                or not databases
                or any(
                    record["allow_connections"] is True
                    and record["audit_role"] != online_admin_role
                    for record in databases
                )
                or not any(
                    record["name"] == database
                    and record["audit_role"] == database_user
                    for record in databases
                )
            )
            or classification == "unsupported-blocking"
            and method != "unsupported-blocking"
            or classification == "nexpoly-db"
            and (
                not databases
                or (
                    method
                    in {
                        "isolated-volume-copy-read-only",
                        "isolated-bind-copy-read-only",
                    }
                    and any(
                        record["allow_connections"] is True
                        and record["audit_role"] != database_user
                        for record in databases
                    )
                )
                or (
                    method
                    not in {
                        "isolated-volume-copy-read-only",
                        "isolated-bind-copy-read-only",
                    }
                    and (
                        ROLE_RE.fullmatch(database_user) is None
                        or any(
                            record["allow_connections"] is True
                            and (
                                not isinstance(record["audit_role"], str)
                                or ROLE_RE.fullmatch(
                                    record["audit_role"]
                                )
                                is None
                            )
                            for record in databases
                        )
                    )
                )
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
                    and source_postgres_major
                    not in SUPPORTED_POSTGRES_AUDIT_MAJORS
                )
            )
        ):
            raise MediaEvidenceError(
                "media registry descriptor audit method conflicts with disposition"
            )
        prefix = {
            "docker_volume": "docker-volume",
            "container_bind": "container-bind",
            "postgres_backup": "postgres-backup",
            "reviewed_file": "reviewed-file",
        }[kind]
        if not media_id.startswith(
            (prefix + ":", prefix + "-sha256:")
        ):
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
                online_admin_role=online_admin_role,
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
        or writable[0].media_id != production_identity["media_id"]
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
        authority_rules_sha256=authority_rules_sha256,
        descriptors=tuple(descriptors),
        required_online_databases=tuple(normalized_online),
        boundary=boundary,
        audit_images=audit_images,
        audit_image_ids=audit_image_ids,
        reviewed_content_inventory_sha256=(
            reviewed_content_inventory_sha256
        ),
        postgres_uid=runtime["postgres_uid"],
        postgres_gid=runtime["postgres_gid"],
        production_identity=dict(production_identity),
    )


def load_authority_rules(
    path: Path,
    *,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    private_root: Path | None = None,
) -> MediaAuthorityRules:
    value = load_registry(
        path,
        policy=policy,
        private_root=private_root,
        authority_rules=True,
    )
    if not isinstance(value, MediaAuthorityRules):
        raise MediaEvidenceError("media authority rules did not load")
    return value


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


def _container_workload_classification(
    container: Mapping[str, object],
) -> str:
    """Classify an active workload from its real exec vector.

    Image names and OCI labels are mutable metadata.  They may corroborate a
    known entrypoint wrapper, but can never classify a shell or another
    arbitrary process by themselves.
    """

    config = container.get("Config")
    if not isinstance(config, dict):
        return "unknown"
    commands = _container_command_vectors(container)
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
    first_argument = (
        PurePosixPath(actual[1]).name.lower()
        if len(actual) > 1
        else ""
    )
    postgres_metadata = bool(
        re.search(r"(^|[-_.])postgres(?:ql)?($|[-_.])", image_name)
        or isinstance(title, str)
        and title.lower() in {"postgres", "postgresql"}
    )
    if executable in {"postgres", "postmaster"} or (
        executable == "docker-entrypoint.sh"
        and first_argument in {"postgres", "postmaster"}
        and postgres_metadata
    ):
        return "postgres"
    if executable in {
        "minio",
        "redis-server",
        "mysqld",
        "mariadbd",
        "elasticsearch",
        "opensearch",
    }:
        return "non-postgres"
    wrapper_arguments = {
        "minio": {"minio", "server"},
        "redis": {"redis-server"},
        "mysql": {"mysqld"},
        "mariadb": {"mariadbd", "mysqld"},
        "elasticsearch": {"elasticsearch"},
        "opensearch": {"opensearch"},
    }
    if (
        executable == "docker-entrypoint.sh"
        and image_name in wrapper_arguments
        and first_argument in wrapper_arguments[image_name]
    ):
        return "non-postgres"
    return "unknown"


def _container_pgdata(container: Mapping[str, object]) -> str | None:
    config = container.get("Config")
    if (
        not isinstance(config, dict)
        or _container_workload_classification(container) != "postgres"
    ):
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
    return "/var/lib/postgresql/data"


def _reject_volume_subpath_mount(
    container: Mapping[str, object],
    mount: Mapping[str, object],
) -> None:
    """Fail closed when an active reader exposes only part of a volume."""

    if mount.get("Type") != "volume":
        return
    for field in ("SubPath", "Subpath"):
        value = mount.get(field)
        if value is not None and value != "":
            raise MediaEvidenceError(
                "Docker volume subpath prevents complete media discovery"
            )
    host = container.get("HostConfig")
    host_mounts = host.get("Mounts") if isinstance(host, dict) else None
    if host_mounts is None:
        return
    if not isinstance(host_mounts, list):
        raise MediaEvidenceError("Docker HostConfig mounts are invalid")
    source = mount.get("Name")
    destination = mount.get("Destination")
    for raw in host_mounts:
        if not isinstance(raw, dict):
            raise MediaEvidenceError("Docker HostConfig mount is invalid")
        if (
            raw.get("Type") != "volume"
            or raw.get("Source") != source
            or raw.get("Target") != destination
        ):
            continue
        options = raw.get("VolumeOptions")
        if options is None:
            return
        if not isinstance(options, dict):
            raise MediaEvidenceError(
                "Docker volume options are invalid"
            )
        subpath = options.get("Subpath", options.get("SubPath"))
        if subpath is not None and subpath != "":
            raise MediaEvidenceError(
                "Docker volume subpath prevents complete media discovery"
            )
        return


def _reject_active_mount_overlays(
    container: Mapping[str, object],
    mount: Mapping[str, object],
) -> None:
    """Reject active views where another mount can hide persistent content."""

    state = container.get("State")
    status = state.get("Status") if isinstance(state, dict) else None
    if (
        status not in ACTIVE_CONTAINER_STATES
        or mount.get("Type") not in {"volume", "bind"}
    ):
        return
    destination = mount.get("Destination")
    mounts = container.get("Mounts")
    if not isinstance(destination, str) or not isinstance(mounts, list):
        raise MediaEvidenceError(
            "active persistent mount topology is invalid"
        )
    target = PurePosixPath(
        _normalized_container_directory(
            destination,
            source="active persistent mount destination",
        )
    )
    for other in mounts:
        if other is mount:
            continue
        if not isinstance(other, dict) or not isinstance(
            other.get("Destination"),
            str,
        ):
            raise MediaEvidenceError(
                "active persistent mount topology is invalid"
            )
        other_target = PurePosixPath(
            _normalized_container_directory(
                str(other["Destination"]),
                source="active overlapping mount destination",
            )
        )
        if (
            other_target == target
            or target in other_target.parents
            or other_target in target.parents
        ):
            raise MediaEvidenceError(
                "active persistent mount is masked by an overlapping mount"
            )


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


def _revalidate_docker_epoch(
    runner: CommandRunner,
    discovery: Discovery,
) -> None:
    """CAS one frozen Docker inventory without re-probing media content."""

    containers, volumes = _docker_inventory(runner)
    document = _docker_boundary_document(containers, volumes)
    volume_names = tuple(
        sorted(str(value["Name"]) for value in volumes)
    )
    container_ids = tuple(
        sorted(str(value["Id"]) for value in containers)
    )
    bind_sources: set[str] = set()
    for container in containers:
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise MediaEvidenceError(
                "Docker epoch container mounts are invalid"
            )
        for mount in mounts:
            if (
                isinstance(mount, dict)
                and mount.get("Type") == "bind"
            ):
                bind_sources.add(
                    _canonical_bind_source(mount.get("Source"))
                )
    if (
        sha256_bytes(canonical_json_bytes(document))
        != discovery.docker_inventory_sha256
        or volume_names != discovery.scanned_volume_names
        or container_ids != discovery.scanned_container_ids
        or tuple(sorted(bind_sources))
        != discovery.scanned_bind_sources
    ):
        raise MediaEvidenceError(
            "Docker inventory changed during the media-audit epoch"
        )


def _revalidate_backup_epoch(
    registry: Registry,
    discovery: Discovery,
    policy: DiscoveryPolicy,
) -> None:
    scanned: list[dict[str, object]] = []
    media_ids: set[str] = set()
    for root in policy.backup_roots:
        values, records = _walk_backup_root(
            root,
            policy,
            root_authority=_sealed_root_authority(registry, root),
        )
        scanned.extend(records)
        media_ids.update(value.media_id for value in values)
    expected_ids = {
        media_id
        for media_id, source in discovery.media.items()
        if source.kind in {"postgres_backup", "reviewed_file"}
    }
    if (
        sha256_bytes(
            canonical_json_bytes(
                sorted(scanned, key=lambda item: item["path"])
            )
        )
        != discovery.backup_inventory_sha256
        or media_ids != expected_ids
    ):
        raise MediaEvidenceError(
            "private backup inventory changed during the media-audit epoch"
        )


def _bounded_marker_signature(
    payload: bytes,
    *,
    root_prefix: str,
    method: str,
    context: str,
    probe_authority: Mapping[str, object] | None = None,
) -> dict[str, object]:
    prefix = PurePosixPath(root_prefix)
    if not prefix.is_absolute():
        raise MediaEvidenceError("PostgreSQL marker-probe root is invalid")
    roots: dict[str, dict[str, object]] = {}
    for line in payload.decode("utf-8", "strict").splitlines():
        fields = line.split("\t")
        if (
            len(fields) not in {2, 3}
            or fields[0] not in {"V", "C", "B"}
        ):
            raise MediaEvidenceError(
                f"PostgreSQL signature output is invalid: {context}"
            )
        marker = PurePosixPath(fields[1])
        try:
            marker.relative_to(prefix)
        except ValueError as exc:
            raise MediaEvidenceError(
                f"PostgreSQL signature escaped its root: {context}"
            ) from exc
        if fields[0] == "V":
            root = marker.parent
        elif fields[0] == "C":
            if marker.parent.name != "global":
                raise MediaEvidenceError(
                    "PostgreSQL pg_control marker path is invalid"
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
                    "PostgreSQL PG_VERSION marker is invalid"
                )
            record["version"] = int(fields[2])
        elif fields[0] == "C":
            record["pg_control"] = True
        else:
            record["base"] = True
    probe = {
        "method": method,
        "maximum_entries": MAX_POSTGRES_MARKER_PROBE_ENTRIES,
        "maximum_marker_results": MAX_POSTGRES_MARKER_RESULTS,
        "timeout_seconds": POSTGRES_MARKER_PROBE_TIMEOUT_SECONDS,
        "content_read_scope": "PG_VERSION-up-to-32-bytes",
        **dict(probe_authority or {}),
    }
    if not roots:
        return {
            "signature": "non-postgres",
            "data_subpath": ".",
            "postgres_major": None,
            "classification_probe": {
                **probe,
                "result": "no-postgres-markers",
            },
        }
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
            "classification_probe": {
                **probe,
                "result": "ambiguous-or-partial-postgres-markers",
            },
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
            "classification_probe": {
                **probe,
                "result": "ambiguous-or-partial-postgres-markers",
            },
        }
    markers = roots[root_value]
    relative = PurePosixPath(root_value).relative_to(prefix)
    value = "." if not relative.parts else relative.as_posix()
    if (
        value != "."
        and (
            ".." in PurePosixPath(value).parts
            or re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", value) is None
        )
    ):
        raise MediaEvidenceError("PostgreSQL PGDATA subpath is unsafe")
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
            "classification_probe": {
                **probe,
                "result": "ambiguous-or-partial-postgres-markers",
            },
        }
    return {
        "signature": "postgres",
        "data_subpath": value,
        "postgres_major": version,
        "classification_probe": {
            **probe,
            "result": "complete-postgres-signature",
        },
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
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=volume,src={volume_name},dst=/source,readonly",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ceu",
            (
                "set -o pipefail; seen=0; "
                f"timeout -s KILL {POSTGRES_MARKER_PROBE_TIMEOUT_SECONDS} "
                "find /source -xdev -mindepth 1 -print0 | "
                "while IFS= read -r -d '' path; do "
                "seen=$((seen + 1)); "
                f"test \"$seen\" -le {MAX_POSTGRES_MARKER_PROBE_ENTRIES} "
                "|| exit 92; "
                "case \"$path\" in "
                "*/PG_VERSION) test -f \"$path\" || continue; "
                "printf 'V\\t%s\\t' \"$path\"; "
                "head -c 32 \"$path\" | tr -d '\\r\\n'; printf '\\n' ;; "
                "*/global/pg_control) test -f \"$path\" || continue; "
                "printf 'C\\t%s\\n' \"$path\" ;; "
                "*/base) test -d \"$path\" || continue; "
                "printf 'B\\t%s\\n' \"$path\" ;; "
                "esac; done | LC_ALL=C sort | "
                f"awk 'NR > {MAX_POSTGRES_MARKER_RESULTS} "
                "{ exit 93 } { print }'"
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
        source_media_id=media_id_for_locator(
            "docker_volume",
            volume_name,
        ),
    )
    return _bounded_marker_signature(
        completed.stdout,
        root_prefix="/source",
        method="bounded-postgres-marker-probe-v1",
        context=volume_name,
    )


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


def _probe_exact_volume_pgdata(
    runner: CommandRunner,
    image: str,
    volume_name: str,
    data_subpath: str,
    *,
    operation: ScratchOperation,
    resource_key: str,
) -> dict[str, object]:
    """Probe only the three markers at an exact container-declared PGDATA."""

    if (
        data_subpath != "."
        and (
            PurePosixPath(data_subpath).is_absolute()
            or ".." in PurePosixPath(data_subpath).parts
            or re.fullmatch(r"[A-Za-z0-9._/-]{1,512}", data_subpath)
            is None
        )
    ):
        raise MediaEvidenceError("exact Docker PGDATA subpath is unsafe")
    path_checks = ["root=/source; "]
    if data_subpath != ".":
        for component in PurePosixPath(data_subpath).parts:
            quoted = shlex.quote(component)
            path_checks.append(
                f"test ! -L \"$root\"/{quoted}; "
                f"root=\"$root\"/{quoted}; test -d \"$root\"; "
            )
    path_checks.append(
        "test ! -L \"$root/PG_VERSION\"; "
        "test ! -L \"$root/global\"; "
        "test ! -L \"$root/global/pg_control\"; "
        "test ! -L \"$root/base\"; "
    )
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
                "".join(path_checks)
                +
                "if test -f \"$root/PG_VERSION\"; then "
                "printf 'V\\t'; head -c 32 \"$root/PG_VERSION\" | "
                "tr -d '\\r\\n'; printf '\\n'; fi; "
                "test ! -f \"$root/global/pg_control\" || printf 'C\\n'; "
                "test ! -d \"$root/base\" || printf 'B\\n'"
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
        source_media_id=media_id_for_locator(
            "docker_volume",
            volume_name,
        ),
    )
    version: int | None = None
    pg_control = False
    base = False
    for line in completed.stdout.decode("ascii", "strict").splitlines():
        fields = line.split("\t")
        if (
            len(fields) in {2, 3}
            and fields[0] == "V"
            and fields[-1].isdigit()
            and version is None
        ):
            version = int(fields[-1])
        elif fields[0] == "C" and len(fields) in {1, 2} and not pg_control:
            pg_control = True
        elif fields[0] == "B" and len(fields) in {1, 2} and not base:
            base = True
        else:
            raise MediaEvidenceError(
                "exact Docker PGDATA marker output is invalid"
            )
    present = (version is not None, pg_control, base)
    if all(present):
        signature = "postgres"
        result = "complete-postgres-signature"
    elif any(present):
        signature = "damaged-postgres"
        result = "partial-postgres-signature"
    else:
        signature = "non-postgres"
        result = "no-postgres-markers"
    return {
        "signature": signature,
        "data_subpath": data_subpath,
        "postgres_major": version,
        "classification_probe": {
            "method": "exact-pgdata-marker-probe-v1",
            "result": result,
            "content_read_scope": "PG_VERSION-up-to-32-bytes",
        },
    }


def _host_mountinfo_snapshot(source: Path) -> tuple[str, int]:
    """Seal the mount namespace and reject masking at/below a bind source."""

    namespace_before = os.stat("/proc/self/ns/mnt")
    descriptor = os.open(
        "/proc/self/mountinfo",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        payload = _read_fd(descriptor, 16 * 1024 * 1024)
    finally:
        os.close(descriptor)
    namespace_after = os.stat("/proc/self/ns/mnt")
    if (
        namespace_before.st_dev,
        namespace_before.st_ino,
    ) != (
        namespace_after.st_dev,
        namespace_after.st_ino,
    ):
        raise MediaEvidenceError(
            "host mount namespace changed during bind discovery"
        )

    def unescape(value: str) -> str:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    root = PurePosixPath(str(source))
    for raw_line in payload.decode("utf-8", "strict").splitlines():
        left = raw_line.split(" - ", 1)[0].split()
        if len(left) < 6:
            raise MediaEvidenceError("host mountinfo record is invalid")
        mount_point = PurePosixPath(unescape(left[4]))
        if mount_point == root or root in mount_point.parents:
            raise MediaEvidenceError(
                "active bind source is masked by a host mountpoint"
            )
    return sha256_bytes(payload), namespace_before.st_ino


def _probe_active_bind_host_signature(source: str) -> dict[str, object]:
    """Boundedly scan the host bind tree, including nested host mounts.

    An in-container walk can be incomplete when Docker used a non-recursive
    bind option.  Walking the exact host source through no-follow descriptors
    makes the discovery boundary independent of those container mount
    semantics and fails closed on races, links, special entries, permissions,
    and resource-limit exhaustion.
    """

    path = Path(_canonical_bind_source(source))
    mountinfo_sha256, mount_namespace_inode = (
        _host_mountinfo_snapshot(path)
    )
    started = time.monotonic()
    entries = 0
    markers: list[bytes] = []

    def stable(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
        )

    def add_marker(kind: str, relative: tuple[str, ...], value: bytes = b"") -> None:
        if len(markers) >= MAX_POSTGRES_MARKER_RESULTS:
            raise MediaEvidenceError(
                "active bind PostgreSQL marker result limit was exceeded"
            )
        marker = PurePosixPath("/source").joinpath(*relative).as_posix()
        fields = [kind.encode("ascii"), marker.encode("utf-8")]
        if value:
            fields.append(value)
        markers.append(b"\t".join(fields))

    def visit(directory: int, relative: tuple[str, ...]) -> None:
        nonlocal entries
        before = os.fstat(directory)
        if not stat.S_ISDIR(before.st_mode):
            raise MediaEvidenceError("active bind tree entry is not a directory")
        for name in sorted(os.listdir(directory)):
            entries += 1
            if (
                entries > MAX_POSTGRES_MARKER_PROBE_ENTRIES
                or time.monotonic() - started
                > POSTGRES_MARKER_PROBE_TIMEOUT_SECONDS
            ):
                raise MediaEvidenceError(
                    "active bind PostgreSQL marker scan exceeded its bound"
                )
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise MediaEvidenceError("active bind tree name is invalid")
            metadata = os.stat(
                name,
                dir_fd=directory,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode):
                raise MediaEvidenceError(
                    "active bind tree contains a symlink"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory,
            )
            try:
                opened = os.fstat(descriptor)
                if stable(opened) != stable(metadata):
                    raise MediaEvidenceError(
                        "active bind tree changed during marker scan"
                    )
                current = (*relative, name)
                if stat.S_ISDIR(opened.st_mode):
                    if name == "base":
                        add_marker("B", current)
                    visit(descriptor, current)
                elif stat.S_ISREG(opened.st_mode):
                    if name == "PG_VERSION":
                        raw = os.read(descriptor, 33).strip()
                        if not raw.isdigit() or len(raw) > 32:
                            raise MediaEvidenceError(
                                "active bind PG_VERSION is invalid"
                            )
                        add_marker("V", current, raw)
                    elif (
                        len(relative) >= 1
                        and relative[-1] == "global"
                        and name == "pg_control"
                    ):
                        add_marker("C", current)
                else:
                    raise MediaEvidenceError(
                        "active bind tree contains a special entry"
                    )
                if stable(os.fstat(descriptor)) != stable(opened):
                    raise MediaEvidenceError(
                        "active bind tree entry changed during marker scan"
                    )
            finally:
                os.close(descriptor)
        if stable(os.fstat(directory)) != stable(before):
            raise MediaEvidenceError(
                "active bind directory changed during marker scan"
            )

    try:
        root = _open_bind_source_without_symlinks(path)
    except OSError as exc:
        raise MediaEvidenceError(
            "active bind source cannot be opened without symlinks"
        ) from exc
    try:
        root_before = os.fstat(root)
        if stat.S_ISREG(root_before.st_mode):
            header = os.read(root, 262)
            if (
                path.name == "PG_VERSION"
                or header.startswith(b"PGDMP")
                or len(header) >= 262
                and header[257:262] == b"ustar"
            ):
                raise MediaEvidenceError(
                    "active persistent bind contains PostgreSQL or backup material"
                )
        elif stat.S_ISDIR(root_before.st_mode):
            visit(root, ())
        else:
            raise MediaEvidenceError(
                "active persistent bind source is not regular"
            )
        if stable(os.fstat(root)) != stable(root_before):
            raise MediaEvidenceError(
                "active bind root changed during marker scan"
            )
    finally:
        os.close(root)
    after_mountinfo_sha256, after_namespace_inode = (
        _host_mountinfo_snapshot(path)
    )
    if (
        after_mountinfo_sha256 != mountinfo_sha256
        or after_namespace_inode != mount_namespace_inode
    ):
        raise MediaEvidenceError(
            "host mount topology changed during bind discovery"
        )
    return _bounded_marker_signature(
        b"\n".join(sorted(markers)) + (b"\n" if markers else b""),
        root_prefix="/source",
        method="host-openat-bounded-marker-probe-v1",
        context=str(path),
        probe_authority={
            "host_source": str(path),
            "nested_host_mountpoints": "rejected",
            "mountinfo_sha256": mountinfo_sha256,
            "mount_namespace_inode": mount_namespace_inode,
        },
    )


def _scan_reviewed_bind_tree(
    source: Path,
    *,
    expected_takeover_seal: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Safely hash/classify a host bind without giving its path to Docker."""

    _absolute_parts(source)
    try:
        root_descriptor = _open_bind_source_without_symlinks(source)
    except OSError as exc:
        raise MediaEvidenceError(
            f"container bind is not a safe directory: {source}"
        ) from exc
    digest = hashlib.sha256()
    roots: dict[str, dict[str, object]] = {}
    file_count = 0
    size_bytes = 0
    contains_backup_material = False
    expected_records: dict[str, Mapping[str, object]] = {}
    seen_expected: set[str] = set()
    if expected_takeover_seal is not None:
        unsigned = {
            "schema_version": expected_takeover_seal.get(
                "schema_version"
            ),
            "records": expected_takeover_seal.get("records"),
        }
        records = expected_takeover_seal.get("records")
        if (
            unsigned["schema_version"] != 1
            or not isinstance(records, list)
            or not records
            or expected_takeover_seal.get("digest")
            != sha256_bytes(canonical_json_bytes(unsigned))
        ):
            raise MediaEvidenceError(
                "takeover bind redirect seal is invalid"
            )
        for record in records:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("path"), str)
                or record["path"] in expected_records
            ):
                raise MediaEvidenceError(
                    "takeover bind redirect seal inventory is invalid"
                )
            expected_records[record["path"]] = record

    def verify_expected(
        relative_name: str,
        metadata: os.stat_result,
        kind: str,
        *,
        sha256: str | None = None,
        target: str | None = None,
    ) -> None:
        if expected_takeover_seal is None:
            return
        record = expected_records.get(relative_name)
        common = {"path", "type", "mode", "uid", "gid"}
        fields = {
            "directory": common,
            "file": common | {"size", "sha256"},
            "symlink": common | {"target"},
        }.get(kind)
        if (
            record is None
            or fields is None
            or set(record) != fields
            or record.get("type") != kind
            or record.get("mode")
            != f"{stat.S_IMODE(metadata.st_mode):04o}"
            or record.get("uid") != metadata.st_uid
            or record.get("gid") != metadata.st_gid
            or kind == "file"
            and (
                record.get("size") != metadata.st_size
                or record.get("sha256") != sha256
            )
            or kind == "symlink"
            and record.get("target") != target
        ):
            raise MediaEvidenceError(
                "externalized takeover bind differs from its move seal"
            )
        seen_expected.add(relative_name)
    maximum_files = int(
        LOGICAL_MEDIA_POLICY["non_postgres"]["maximum_files"]  # type: ignore[index]
    )
    maximum_bytes = int(
        LOGICAL_MEDIA_POLICY["non_postgres"]["maximum_bytes"]  # type: ignore[index]
    )
    maximum_single = int(
        LOGICAL_MEDIA_POLICY["non_postgres"][  # type: ignore[index]
            "maximum_single_file_bytes"
        ]
    )

    def marker(root: str) -> dict[str, object]:
        return roots.setdefault(
            root,
            {"version": None, "pg_control": False, "base": False},
        )

    def stable_tuple(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_nlink,
        )

    def scan_regular(
        child: int,
        before: os.stat_result,
        *,
        relative_name: str,
        basename: str,
        marker_root: str,
        pg_control: bool = False,
    ) -> None:
        nonlocal file_count, size_bytes, contains_backup_material
        if before.st_size > maximum_single or (
            expected_takeover_seal is None
            and (
                before.st_uid != os.geteuid()
                or before.st_mode & 0o022
                or before.st_nlink != 1
            )
        ):
            raise MediaEvidenceError(
                "container bind file is not a safe owner-controlled "
                "non-writable regular file"
            )
        file_digest = hashlib.sha256()
        version_payload = bytearray()
        header = bytearray()
        observed = 0
        while True:
            chunk = os.read(child, 1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > maximum_single:
                raise MediaEvidenceError(
                    "container bind file exceeds its size limit"
                )
            file_digest.update(chunk)
            if len(header) < 262:
                header.extend(chunk[: 262 - len(header)])
            if basename == "PG_VERSION" and len(version_payload) <= 64:
                version_payload.extend(chunk[: 65 - len(version_payload)])
        after = os.fstat(child)
        if stable_tuple(before) != stable_tuple(after):
            raise MediaEvidenceError(
                "container bind file changed during scan"
            )
        source_sha256 = "sha256:" + file_digest.hexdigest()
        verify_expected(
            relative_name,
            after,
            "file",
            sha256=source_sha256,
        )
        file_count += 1
        size_bytes += observed
        if file_count > maximum_files or size_bytes > maximum_bytes:
            raise MediaEvidenceError(
                "container bind tree exceeds its aggregate limit"
            )
        digest.update(
            canonical_json_bytes(
                [
                    "file",
                    relative_name,
                    before.st_size,
                    stat.S_IMODE(before.st_mode),
                    file_digest.hexdigest(),
                ]
            )
        )
        if (
            basename.endswith((".backup", ".dump", ".tar"))
            or bytes(header).startswith(b"PGDMP")
            or len(header) >= 262
            and bytes(header[257:262]) == b"ustar"
        ):
            contains_backup_material = True
        if basename == "PG_VERSION":
            raw_version = bytes(version_payload).strip()
            record = marker(marker_root)
            if not raw_version.isdigit() or record["version"] is not None:
                raise MediaEvidenceError(
                    "container bind PG_VERSION marker is invalid"
                )
            record["version"] = int(raw_version)
        elif pg_control:
            marker(marker_root)["pg_control"] = True

    def visit(
        directory: int,
        relative: tuple[str, ...],
    ) -> None:
        nonlocal file_count, size_bytes, contains_backup_material
        directory_before = os.fstat(directory)
        if not stat.S_ISDIR(directory_before.st_mode) or (
            expected_takeover_seal is None
            and (
                directory_before.st_uid != os.geteuid()
                or directory_before.st_mode & 0o022
            )
        ):
            raise MediaEvidenceError(
                "container bind tree is not owner-controlled and non-writable"
            )
        verify_expected(
            "/".join(relative) or ".",
            directory_before,
            "directory",
        )
        for name in sorted(os.listdir(directory)):
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise MediaEvidenceError(
                    "container bind entry has an invalid name"
                )
            try:
                entry_metadata = os.stat(
                    name,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise MediaEvidenceError(
                    "container bind entry cannot be inspected safely"
                ) from exc
            parts = (*relative, name)
            relative_name = "/".join(parts)
            if stat.S_ISLNK(entry_metadata.st_mode):
                if expected_takeover_seal is None:
                    raise MediaEvidenceError(
                        "container bind contains a symlink or special entry"
                    )
                try:
                    target = os.readlink(name, dir_fd=directory)
                except OSError as exc:
                    raise MediaEvidenceError(
                        "takeover bind symlink cannot be read safely"
                    ) from exc
                verify_expected(
                    relative_name,
                    entry_metadata,
                    "symlink",
                    target=target,
                )
                digest.update(
                    canonical_json_bytes(
                        [
                            "symlink",
                            relative_name,
                            stat.S_IMODE(entry_metadata.st_mode),
                            target,
                        ]
                    )
                )
                file_count += 1
                size_bytes += len(os.fsencode(target))
                if (
                    file_count > maximum_files
                    or size_bytes > maximum_bytes
                ):
                    raise MediaEvidenceError(
                        "container bind tree exceeds its aggregate limit"
                    )
                continue
            try:
                child = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=directory,
                )
            except OSError as exc:
                raise MediaEvidenceError(
                    "container bind contains a symlink or inaccessible entry"
                ) from exc
            try:
                before = os.fstat(child)
                if (
                    stable_tuple(before) != stable_tuple(entry_metadata)
                    or expected_takeover_seal is None
                    and (
                        before.st_uid != os.geteuid()
                        or before.st_mode & 0o022
                    )
                ):
                    raise MediaEvidenceError(
                        "container bind entry is not owner-controlled "
                        "and non-writable"
                    )
                if stat.S_ISDIR(before.st_mode):
                    digest.update(
                        canonical_json_bytes(
                            [
                                "directory",
                                relative_name,
                                stat.S_IMODE(before.st_mode),
                            ]
                        )
                    )
                    if name == "base":
                        marker("/".join(relative) or ".")[
                            "base"
                        ] = True
                    visit(child, parts)
                elif stat.S_ISREG(before.st_mode):
                    is_pg_control = (
                        len(relative) >= 1
                        and relative[-1] == "global"
                        and name == "pg_control"
                    )
                    scan_regular(
                        child,
                        before,
                        relative_name=relative_name,
                        basename=name,
                        marker_root=(
                            "/".join(relative[:-1]) or "."
                            if is_pg_control
                            else "/".join(relative) or "."
                        ),
                        pg_control=is_pg_control,
                    )
                else:
                    raise MediaEvidenceError(
                        "container bind contains a symlink or special entry"
                    )
            finally:
                os.close(child)
        directory_after = os.fstat(directory)
        if stable_tuple(directory_before) != stable_tuple(directory_after):
            raise MediaEvidenceError(
                "container bind directory changed during scan"
            )

    root_before = os.fstat(root_descriptor)
    try:
        if stat.S_ISDIR(root_before.st_mode):
            visit(root_descriptor, ())
        elif stat.S_ISREG(root_before.st_mode):
            scan_regular(
                root_descriptor,
                root_before,
                relative_name=".",
                basename=source.name,
                marker_root=".",
            )
        else:
            raise MediaEvidenceError(
                "container bind is not a regular file or directory"
            )
        root_after = os.fstat(root_descriptor)
    finally:
        os.close(root_descriptor)
    try:
        reopened = _open_bind_source_without_symlinks(source)
    except OSError as exc:
        raise MediaEvidenceError(
            "container bind disappeared after scan"
        ) from exc
    try:
        root_reopened = os.fstat(reopened)
    finally:
        os.close(reopened)
    if (
        stable_tuple(root_before) != stable_tuple(root_after)
        or stable_tuple(root_after) != stable_tuple(root_reopened)
    ):
        raise MediaEvidenceError(
            "container bind identity changed during scan"
        )
    if (
        expected_takeover_seal is not None
        and seen_expected != set(expected_records)
    ):
        raise MediaEvidenceError(
            "externalized takeover bind seal inventory was not fully scanned"
        )
    if not roots:
        signature = {
            "signature": "non-postgres",
            "data_subpath": ".",
            "postgres_major": None,
        }
    else:
        complete = [
            root
            for root, values in roots.items()
            if isinstance(values["version"], int)
            and values["pg_control"]
            and values["base"]
        ]
        if len(complete) != 1:
            signature = {
                "signature": "damaged-postgres",
                "data_subpath": ".",
                "postgres_major": None,
            }
        else:
            root = complete[0]
            prefix = "" if root == "." else root + "/"
            unexpected = []
            for candidate, values in roots.items():
                if candidate == root:
                    continue
                nested_version = (
                    candidate.startswith(prefix + "base/")
                    and isinstance(values["version"], int)
                    and not values["pg_control"]
                    and not values["base"]
                )
                if not nested_version:
                    unexpected.append(candidate)
            if unexpected:
                signature = {
                    "signature": "damaged-postgres",
                    "data_subpath": ".",
                    "postgres_major": None,
                }
            else:
                signature = {
                    "signature": "postgres",
                    "data_subpath": root,
                    "postgres_major": roots[root]["version"],
                }
    return {
        **signature,
        "source_content_sha256": "sha256:" + digest.hexdigest(),
        "file_count": file_count,
        "size_bytes": size_bytes,
        "contains_backup_material": contains_backup_material,
    }


def _probe_bind_signature(
    runner: CommandRunner,
    image: str,
    source: str,
    *,
    operation: ScratchOperation,
    resource_key: str,
    audit_source: Path | None = None,
    expected_takeover_seal: Mapping[str, object] | None = None,
    exact_data_subpath: str | None = None,
) -> dict[str, object]:
    """Classify a bind by marker metadata without hashing business content."""

    del runner, image, operation, resource_key
    path = audit_source or Path(source)

    def result_from_roots(
        roots: Mapping[str, Mapping[str, object]],
        *,
        method: str,
        content_scope: str,
    ) -> dict[str, object]:
        if not roots:
            signature = "non-postgres"
            data_subpath = "."
            major: int | None = None
            outcome = "no-postgres-markers"
        else:
            complete = [
                root
                for root, markers in roots.items()
                if isinstance(markers.get("version"), int)
                and markers.get("pg_control") is True
                and markers.get("base") is True
            ]
            if len(complete) != 1:
                signature = "damaged-postgres"
                data_subpath = "."
                major = None
                outcome = "ambiguous-or-partial-postgres-markers"
            else:
                root = complete[0]
                prefix = "" if root == "." else root + "/"
                unexpected = [
                    candidate
                    for candidate, markers in roots.items()
                    if candidate != root
                    and not (
                        candidate.startswith(prefix + "base/")
                        and isinstance(markers.get("version"), int)
                        and markers.get("pg_control") is not True
                        and markers.get("base") is not True
                    )
                ]
                if unexpected:
                    signature = "damaged-postgres"
                    data_subpath = "."
                    major = None
                    outcome = "ambiguous-or-partial-postgres-markers"
                else:
                    signature = "postgres"
                    data_subpath = root
                    major = int(roots[root]["version"])
                    outcome = "complete-postgres-signature"
        return {
            "signature": signature,
            "data_subpath": data_subpath,
            "postgres_major": major,
            "classification_probe": {
                "method": method,
                "result": outcome,
                "maximum_entries": MAX_POSTGRES_MARKER_PROBE_ENTRIES,
                "content_read_scope": content_scope,
            },
        }

    if expected_takeover_seal is not None:
        unsigned = {
            "schema_version": expected_takeover_seal.get(
                "schema_version"
            ),
            "records": expected_takeover_seal.get("records"),
        }
        records = expected_takeover_seal.get("records")
        if (
            unsigned["schema_version"] != 1
            or not isinstance(records, list)
            or not records
            or expected_takeover_seal.get("digest")
            != sha256_bytes(canonical_json_bytes(unsigned))
        ):
            raise MediaEvidenceError(
                "takeover bind redirect seal is invalid"
            )
        roots: dict[str, dict[str, object]] = {}

        def marker(root: str) -> dict[str, object]:
            return roots.setdefault(
                root,
                {"version": None, "pg_control": False, "base": False},
            )

        for record in records:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("path"), str)
                or not isinstance(record.get("type"), str)
            ):
                raise MediaEvidenceError(
                    "takeover bind redirect seal inventory is invalid"
                )
            relative = PurePosixPath(str(record["path"]))
            name = relative.name
            parent = (
                relative.parent.as_posix()
                if relative.parent.as_posix() != ""
                else "."
            )
            if name == "PG_VERSION":
                # The seal proves exact bytes by digest but deliberately does
                # not expose them.  A sealed PG marker therefore blocks
                # metadata-only exclusion instead of rereading content.
                marker(parent)["version"] = "sealed-unknown"
            elif (
                name == "pg_control"
                and relative.parent.name == "global"
            ):
                cluster = relative.parent.parent.as_posix() or "."
                marker(cluster)["pg_control"] = True
            elif name == "base" and record["type"] == "directory":
                marker(parent)["base"] = True
        return result_from_roots(
            roots,
            method="takeover-move-seal-marker-proof-v1",
            content_scope="none",
        )

    _absolute_parts(path)
    root_descriptor = _open_bind_source_without_symlinks(path)
    roots: dict[str, dict[str, object]] = {}
    entries = 0

    def marker(root: str) -> dict[str, object]:
        return roots.setdefault(
            root,
            {"version": None, "pg_control": False, "base": False},
        )

    def inspect_directory(
        directory: int,
        relative: tuple[str, ...],
        *,
        recursive: bool,
    ) -> None:
        nonlocal entries
        for name in sorted(os.listdir(directory)):
            entries += 1
            if entries > MAX_POSTGRES_MARKER_PROBE_ENTRIES:
                raise MediaEvidenceError(
                    "container bind marker probe exceeded its entry budget"
                )
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise MediaEvidenceError(
                    "container bind marker probe found an invalid name"
                )
            metadata = os.stat(
                name,
                dir_fd=directory,
                follow_symlinks=False,
            )
            parts = (*relative, name)
            relative_name = "/".join(parts)
            parent = "/".join(relative) or "."
            if stat.S_ISLNK(metadata.st_mode):
                if (
                    name == "PG_VERSION"
                    or name == "pg_control"
                    and relative
                    and relative[-1] == "global"
                    or name == "base"
                ):
                    marker(parent)["symlink_marker"] = True
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if name == "base":
                    marker(parent)["base"] = True
                if recursive:
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory,
                    )
                    try:
                        inspect_directory(child, parts, recursive=True)
                    finally:
                        os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if name == "PG_VERSION":
                child = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
                try:
                    payload = os.read(child, 33)
                finally:
                    os.close(child)
                version = payload.decode("ascii", "strict").strip()
                current = marker(parent)
                if (
                    not version.isdigit()
                    or len(payload) > 32
                    or current["version"] is not None
                ):
                    current["invalid_version"] = True
                else:
                    current["version"] = int(version)
            elif (
                name == "pg_control"
                and relative
                and relative[-1] == "global"
            ):
                cluster = "/".join(relative[:-1]) or "."
                marker(cluster)["pg_control"] = True

    try:
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise MediaEvidenceError(
                "container bind marker probe requires a directory"
            )
        if exact_data_subpath is None or exact_data_subpath == ".":
            target = root_descriptor
            close_target = False
            target_parts: tuple[str, ...] = ()
        else:
            if (
                PurePosixPath(exact_data_subpath).is_absolute()
                or ".." in PurePosixPath(exact_data_subpath).parts
            ):
                raise MediaEvidenceError(
                    "container bind PGDATA subpath is unsafe"
                )
            target = os.dup(root_descriptor)
            close_target = True
            target_parts = tuple(PurePosixPath(exact_data_subpath).parts)
            for component in target_parts:
                child = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=target,
                )
                os.close(target)
                target = child
        try:
            inspect_directory(
                target,
                target_parts,
                recursive=exact_data_subpath is None,
            )
        finally:
            if close_target:
                os.close(target)
    finally:
        os.close(root_descriptor)
    if any(
        markers.get("invalid_version") is True
        or markers.get("symlink_marker") is True
        for markers in roots.values()
    ):
        return {
            "signature": "damaged-postgres",
            "data_subpath": ".",
            "postgres_major": None,
            "classification_probe": {
                "method": "bounded-openat-postgres-marker-probe-v1",
                "result": "ambiguous-or-partial-postgres-markers",
                "maximum_entries": MAX_POSTGRES_MARKER_PROBE_ENTRIES,
                "content_read_scope": "PG_VERSION-up-to-32-bytes",
            },
        }
    result = result_from_roots(
        roots,
        method=(
            "exact-openat-pgdata-marker-probe-v1"
            if exact_data_subpath is not None
            else "bounded-openat-postgres-marker-probe-v1"
        ),
        content_scope="PG_VERSION-up-to-32-bytes",
    )
    if exact_data_subpath not in {None, "."} and result[
        "signature"
    ] == "postgres":
        result["data_subpath"] = exact_data_subpath
    return result


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
    physical_markers: dict[str, set[str]] = {}

    def physical_marker(cluster: tuple[str, ...], kind: str) -> None:
        physical_markers.setdefault("/".join(cluster) or ".", set()).add(
            kind
        )

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
                    if name == "base":
                        physical_marker(relative, "base")
                    visit(child, (*relative, name))
                elif stat.S_ISREG(metadata.st_mode):
                    header = os.read(child, 512)
                    backup_format = _format_for_backup(path, policy, header)
                    if name == "PG_VERSION":
                        physical_marker(relative, "PG_VERSION")
                    elif (
                        name == "pg_control"
                        and relative
                        and relative[-1] == "global"
                    ):
                        physical_marker(
                            relative[:-1],
                            "global/pg_control",
                        )
                    if (
                        backup_format is None
                        and (
                            header.startswith(b"PGDMP")
                            or len(header) >= 262
                            and header[257:262] == b"ustar"
                        )
                    ):
                        raise MediaEvidenceError(
                            "PostgreSQL backup material has an unapproved suffix"
                        )
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
                                media_id=media_id_for_locator(
                                    "postgres_backup",
                                    str(path),
                                ),
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
                                media_id=media_id_for_locator(
                                    "reviewed_file",
                                    str(path),
                                ),
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
    if physical_markers:
        raise MediaEvidenceError(
            "approved backup root contains physical PostgreSQL data "
            f"directory markers: {sorted(physical_markers)!r}"
        )
    return media, scanned


def discover_media(
    registry: Registry,
    *,
    runner: CommandRunner,
    operation: ScratchOperation,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    enforce_registry: bool = True,
) -> Discovery:
    containers, volumes = _docker_inventory(runner)
    takeover_operation = _validated_takeover_operation_for_registry(
        registry
    )
    takeover_stage = registry.boundary.get("takeover_backup_stage")
    if takeover_stage is not None and not isinstance(
        takeover_stage, dict
    ):
        raise MediaEvidenceError(
            "runtime registry takeover stage is invalid"
        )
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
    unknown_volume_attachments: dict[
        str, list[dict[str, object]]
    ] = {}
    unknown_bind_attachments: dict[
        str, list[dict[str, object]]
    ] = {}
    unresolved_pg_candidates: list[str] = []
    for container in containers:
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise MediaEvidenceError("Docker container mounts are invalid")
        workload = _container_workload_classification(container)
        pgdata = _container_pgdata(container)
        matched_pgdata = False
        for raw_mount in mounts:
            if not isinstance(raw_mount, dict):
                raise MediaEvidenceError("Docker mount record is invalid")
            _reject_volume_subpath_mount(container, raw_mount)
            _reject_active_mount_overlays(container, raw_mount)
            mount_type = raw_mount.get("Type")
            source = raw_mount.get("Source")
            name = raw_mount.get("Name")
            destination = raw_mount.get("Destination")
            if not isinstance(destination, str):
                raise MediaEvidenceError("Docker mount destination is invalid")
            attachment = _attached_record(container, raw_mount)
            if mount_type == "volume" and isinstance(name, str):
                volume_attachments.setdefault(name, []).append(attachment)
                if workload == "unknown":
                    unknown_volume_attachments.setdefault(
                        name, []
                    ).append(attachment)
            elif mount_type == "bind":
                canonical_source = _canonical_bind_source(source)
                bind_attachments.setdefault(
                    canonical_source,
                    [],
                ).append(attachment)
                if workload == "unknown":
                    unknown_bind_attachments.setdefault(
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
    volume_inspect_digests = {
        str(value["Name"]): sha256_bytes(canonical_json_bytes(value))
        for value in volumes
        if isinstance(value.get("Name"), str)
    }
    named_volume_patterns = [
        str(rule["volume_name_pattern"])
        for rule in LOGICAL_MEDIA_POLICY["named_stacks"]  # type: ignore[index]
        if isinstance(rule, dict)
        and isinstance(rule.get("volume_name_pattern"), str)
    ]
    production_media_id = (
        str(registry.production_identity.get("media_id"))
        if isinstance(registry.production_identity, dict)
        else None
    )
    for value in volumes:
        name = value.get("Name")
        if not isinstance(name, str):
            raise MediaEvidenceError("Docker volume name is invalid")
        media_id = media_id_for_locator("docker_volume", name)
        attachments = sorted(
            volume_attachments.get(name, []),
            key=lambda item: (str(item["container_id"]), str(item["destination"])),
        )
        known = pg_mounts.get(("docker_volume", name))
        fixed_selector = (
            media_id == production_media_id
            or any(
                re.fullmatch(pattern, name) is not None
                for pattern in named_volume_patterns
            )
            or (
                descriptor_by_id.get(media_id) is not None
                and descriptor_by_id[media_id].classification == "nexpoly-db"
            )
        )
        ambiguous = (
            known is not None
            or name in candidate_extra_volumes
            or name in unknown_volume_attachments
            or fixed_selector
            or not attachments
            or re.search(
                r"(^|[_.-])(postgres|pgdata|pgvector|timescale)([_.-]|$)",
                name,
                flags=re.IGNORECASE,
            )
            is not None
        )
        if known is not None:
            _subpath, pg_attachments = known
            if sorted(
                pg_attachments,
                key=canonical_json_bytes,
            ) != sorted(attachments, key=canonical_json_bytes):
                raise MediaEvidenceError(
                    f"PostgreSQL volume has an unclassified attachment: {name}"
                )
            active_pg_attachments = [
                attachment
                for attachment in pg_attachments
                if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
            ]
            if len(active_pg_attachments) > 1:
                raise MediaEvidenceError(
                    "PostgreSQL volume has multiple active PGDATA readers"
                )
        resource_key = (
            "discover-volume-"
            + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
            + "-probe"
        )
        active_attachments = [
            attachment
            for attachment in attachments
            if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
        ]
        active_unknown = [
            attachment
            for attachment in unknown_volume_attachments.get(name, [])
            if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
        ]
        if known is not None:
            signature = _probe_volume_signature(
                runner,
                registry.audit_image,
                name,
                operation=operation,
                resource_key=resource_key,
            )
        elif active_unknown:
            if len(active_unknown) != 1:
                raise MediaEvidenceError(
                    "opaque volume has multiple active readers"
                )
            signature = _probe_volume_signature(
                runner,
                registry.audit_image,
                name,
                operation=operation,
                resource_key=resource_key,
            )
        elif active_attachments:
            if len(active_attachments) != 1:
                raise MediaEvidenceError(
                    "persistent volume has multiple active readers"
                )
            signature = _probe_volume_signature(
                runner,
                registry.audit_image,
                name,
                operation=operation,
                resource_key=resource_key,
            )
        elif not active_attachments:
            signature = _probe_volume_signature(
                runner,
                registry.audit_image,
                name,
                operation=operation,
                resource_key=resource_key,
            )
        else:
            signature = {
                "signature": "non-postgres",
                "data_subpath": ".",
                "postgres_major": None,
                "classification_probe": {
                    "method": "container-metadata-exclusion-v1",
                    "result": "no-container-pgdata-mapping",
                    "content_read_scope": "none",
                },
            }
        if signature["signature"] == "damaged-postgres":
            raise MediaEvidenceError(
                f"volume has partial or ambiguous PostgreSQL markers: {name}"
            )
        if known is not None:
            subpath, pg_attachments = known
            if (
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
            classification_probe=dict(
                signature["classification_probe"]
            ),
            docker_inspect_sha256=volume_inspect_digests[name],
        )
    scanned_bind_sources = set(bind_attachments)
    for source in sorted(scanned_bind_sources):
        known = bind_mounts.get(source)
        redirect = _takeover_bind_redirect(
            source,
            bind_attachments[source],
            takeover_operation,
            takeover_stage,
        )
        audit_path = redirect[0] if redirect is not None else Path(source)
        takeover_seal = redirect[1] if redirect is not None else None
        redirect_evidence = redirect[2] if redirect is not None else None
        bind_media_id = media_id_for_locator(
            "container_bind",
            source,
        )
        if known is not None and sorted(
            known[1],
            key=canonical_json_bytes,
        ) != sorted(
            bind_attachments.get(source, []),
            key=canonical_json_bytes,
        ):
            raise MediaEvidenceError(
                "PostgreSQL bind has an unclassified attachment: "
                f"{source}"
            )
        active_bind_readers = (
            [
                attachment
                for attachment in known[1]
                if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
            ]
            if known is not None
            else []
        )
        if len(active_bind_readers) > 1:
            raise MediaEvidenceError(
                "PostgreSQL bind has multiple active PGDATA readers"
            )
        ambiguous_bind = (
            known is not None
            or source in candidate_extra_binds
            or source in unknown_bind_attachments
            or redirect is not None
            or not bind_attachments[source]
            or bind_media_id == production_media_id
            or (
                descriptor_by_id.get(bind_media_id) is not None
                and descriptor_by_id[bind_media_id].classification
                == "nexpoly-db"
            )
            or re.search(
                r"(^|[/_.-])(postgres|pgdata|pgvector|timescale)([/_.-]|$)",
                source,
                flags=re.IGNORECASE,
            )
            is not None
        )
        all_active_bind_attachments = [
            attachment
            for attachment in bind_attachments[source]
            if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
        ]
        active_unknown_bind = [
            attachment
            for attachment in unknown_bind_attachments.get(source, [])
            if str(attachment["state"]) in ACTIVE_CONTAINER_STATES
        ]
        if active_bind_readers:
            signature = _probe_active_bind_host_signature(source)
        elif active_unknown_bind:
            if len(active_unknown_bind) != 1:
                raise MediaEvidenceError(
                    "opaque bind has multiple active readers"
                )
            signature = _probe_active_bind_host_signature(source)
        elif all_active_bind_attachments:
            if len(all_active_bind_attachments) != 1:
                raise MediaEvidenceError(
                    "persistent bind has multiple active readers"
                )
            signature = _probe_active_bind_host_signature(source)
        elif not all_active_bind_attachments:
            signature = _probe_bind_signature(
                runner,
                registry.audit_image,
                source,
                operation=operation,
                resource_key=(
                    "discover-bind-"
                    + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
                    + "-probe"
                ),
                audit_source=audit_path,
                expected_takeover_seal=takeover_seal,
                exact_data_subpath=None,
            )
        else:
            signature = {
                "signature": "non-postgres",
                "data_subpath": ".",
                "postgres_major": None,
                "classification_probe": {
                    "method": "container-metadata-exclusion-v1",
                    "result": "no-container-pgdata-mapping",
                    "content_read_scope": "none",
                },
            }
        if signature["signature"] == "damaged-postgres":
            raise MediaEvidenceError(
                "container bind has partial or ambiguous PostgreSQL "
                f"markers: {source}"
            )
        if (
            active_unknown_bind
            and signature["signature"] == "postgres"
        ):
            raise MediaEvidenceError(
                "an opaque active PostgreSQL bind lacks an exact trusted "
                f"PGDATA mapping: {source}"
            )
        if signature["signature"] == "non-postgres":
            if known is not None:
                raise MediaEvidenceError(
                    f"container PGDATA bind lacks PostgreSQL content: {source}"
                )
            path = Path(source)
            _absolute_parts(path)
            media_id = media_id_for_locator(
                "container_bind",
                str(path),
            )
            discovered[media_id] = DiscoveredMedia(
                media_id=media_id,
                kind="container_bind",
                locator=str(path),
                data_subpath=".",
                attached=tuple(
                    sorted(
                        bind_attachments[source],
                        key=lambda item: (
                            str(item["container_id"]),
                            str(item["destination"]),
                        ),
                    )
                ),
                signature="non-postgres",
                postgres_major=None,
                audit_locator=(
                    str(audit_path) if redirect is not None else None
                ),
                takeover_redirect=redirect_evidence,
                takeover_seal=takeover_seal,
                classification_probe=dict(
                    signature["classification_probe"]
                ),
            )
            continue
        if signature["signature"] != "postgres":
            raise MediaEvidenceError(
                f"container bind has a damaged PostgreSQL signature: {source}"
            )
        subpath = str(signature["data_subpath"])
        attachments = (
            known[1]
            if known is not None
            else bind_attachments[source]
        )
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
        if (
            known is not None
            and str(signature["data_subpath"]) != known[0]
        ):
            raise MediaEvidenceError(
                f"container PGDATA conflicts with bind contents: {source}"
            )
        media_id = media_id_for_locator(
            "container_bind",
            str(path),
        )
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
            signature="postgres",
            postgres_major=(
                int(signature["postgres_major"])
                if isinstance(signature["postgres_major"], int)
                else None
            ),
            audit_locator=(
                str(audit_path) if redirect is not None else None
            ),
            takeover_redirect=redirect_evidence,
            takeover_seal=takeover_seal,
            classification_probe=dict(
                signature["classification_probe"]
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

    if enforce_registry:
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
                raise MediaEvidenceError(
                    "discovered media kind differs from registry"
                )
            if descriptor.classification in {
                "nexpoly-db",
                "adjacent-postgres",
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
            if descriptor.classification in {
                "reviewed-non-pg",
                "excluded-non-pg",
            } and (
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
            if descriptor.audit_method in {
                "live-read-only",
                "live-read-only-adjacent",
            } and len(active) != 1:
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
        scanned_bind_sources=tuple(sorted(scanned_bind_sources)),
        scanned_container_ids=tuple(
            sorted(str(value["Id"]) for value in containers)
        ),
    )


def _runtime_registry_document(
    authority: MediaAuthorityRules,
    *,
    boundary: dict[str, object],
    audit_image_ids: Mapping[int, str],
    descriptors: Sequence[MediaDescriptor],
    required_online_databases: Sequence[dict[str, str]],
    reviewed_content_inventory_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 5,
        "media_authority_rules_sha256": authority.digest,
        "reviewed_content_inventory_sha256": (
            reviewed_content_inventory_sha256
        ),
        "production_identity": dict(authority.production_identity),
        "discovery_boundary": boundary,
        "audit_runtime": {
            "postgres_image": _audit_image_for_major(
                authority,
                POSTGRES_MAJOR,
            ),
            "postgres_images": {
                str(major): {
                    "digest_ref": image,
                    "image_id": audit_image_ids[major],
                }
                for major, image in sorted(
                    _audit_images_map(authority).items()
                )
            },
            "postgres_major": POSTGRES_MAJOR,
            "postgres_uid": authority.postgres_uid,
            "postgres_gid": authority.postgres_gid,
            "postgres_image_id": audit_image_ids[POSTGRES_MAJOR],
            "auditor_sha256": _auditor_digest(),
        },
        "expected_media": [
            descriptor.document()
            for descriptor in sorted(
                descriptors,
                key=lambda value: value.media_id,
            )
        ],
        "required_online_databases": [
            dict(value)
            for value in required_online_databases
        ],
    }


def _external_role_contract_document(
    *,
    authority_rules_sha256: str,
    role_sql_sha256: str,
    entry: Mapping[str, object],
) -> dict[str, object]:
    """Return the stable, host-inventory-independent role authority."""

    return {
        "schema_version": 1,
        "policy": ROLE_CONTRACT_POLICY,
        "media_authority_rules_sha256": authority_rules_sha256,
        "role_sql_sha256": role_sql_sha256,
        "cluster_system_identifier": entry.get(
            "cluster_system_identifier"
        ),
        "source_postgres_major": entry.get(
            "source_postgres_major"
        ),
        "database": entry.get("database"),
        "database_oid": entry.get("database_oid"),
        "database_owner": entry.get("database_owner"),
        "audit_role": entry.get("audit_role"),
        "migration_scope": entry.get("migration_scope"),
        "role_settings": [
            "default_transaction_read_only=on",
            "lock_timeout=5s",
            "statement_timeout=5min",
        ],
        "grant_policy": [
            "database:current:CONNECT",
            "function:pg_catalog.pg_control_system():EXECUTE",
            (
                "relation-if-present:"
                "governance.schema_migrations:USAGE+SELECT"
            ),
            (
                "relation-if-present:"
                "generation.polytao_jobs:USAGE+SELECT"
            ),
        ],
    }


def _external_role_contract_sha256(
    *,
    authority_rules_sha256: str,
    role_sql_sha256: str,
    entry: Mapping[str, object],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            _external_role_contract_document(
                authority_rules_sha256=authority_rules_sha256,
                role_sql_sha256=role_sql_sha256,
                entry=entry,
            )
        )
    )


def _expected_role_contracts_for_descriptor(
    registry: Registry,
    descriptor: MediaDescriptor,
    *,
    cluster_system_identifier: str,
) -> dict[str, str]:
    role_sql_sha256 = os.environ.get(
        AUDIT_ROLE_SQL_DIGEST_ENV,
        "",
    )
    if (
        descriptor.audit_method != "live-read-only"
        or DIGEST_RE.fullmatch(role_sql_sha256) is None
        or DIGEST_RE.fullmatch(
            registry.authority_rules_sha256
        )
        is None
        or PG_SYSTEM_ID_RE.fullmatch(
            cluster_system_identifier
        )
        is None
    ):
        raise MediaEvidenceError(
            "steady managed audit-role authority is incomplete"
        )
    contracts: dict[str, str] = {}
    for database in descriptor.databases:
        role = database.get("audit_role")
        if (
            ROLE_RE.fullmatch(str(role or "")) is None
            or role in contracts
        ):
            raise MediaEvidenceError(
                "steady managed audit-role mapping is invalid"
            )
        contracts[str(role)] = _external_role_contract_sha256(
            authority_rules_sha256=(
                registry.authority_rules_sha256
            ),
            role_sql_sha256=role_sql_sha256,
            entry={
                "cluster_system_identifier": (
                    cluster_system_identifier
                ),
                "source_postgres_major": (
                    descriptor.source_postgres_major
                ),
                "database": database.get("name"),
                "database_oid": database.get("oid"),
                "database_owner": database.get("owner"),
                "audit_role": role,
                "migration_scope": database.get(
                    "migration_scope"
                ),
            },
        )
    return contracts


def _managed_role_matrix_sql(
    role_count: int,
    *,
    postgres_major: int,
) -> str:
    if (
        role_count < 1
        or role_count > MAX_MANAGED_ROLE_MATRIX_ROLES
        or postgres_major not in SUPPORTED_POSTGRES_AUDIT_MAJORS
    ):
        raise MediaEvidenceError(
            "managed audit-role matrix scope is invalid"
        )
    role_filter = ", ".join(
        f":'managed_role_{index}'" for index in range(role_count)
    )
    parameter_acl = (
        """
        UNION ALL
        SELECT 'parameter', parameter.parname,
               acl.privilege_type, acl.is_grantable
        FROM pg_parameter_acl AS parameter
        CROSS JOIN LATERAL aclexplode(parameter.paracl) AS acl
        WHERE acl.grantee = role.oid
        """
        if postgres_major >= 15
        else ""
    )
    return (
        r"""
\set ON_ERROR_STOP on
__NEXPOLY_EVENT_TRIGGERS_SESSION_ASSERT__
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
SET LOCAL default_transaction_read_only = on;
SET LOCAL statement_timeout = '5min';
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SET LOCAL row_security = on;
SET LOCAL jit = off;
WITH selected_roles AS (
  SELECT
    role.oid,
    role.rolname,
    role.rolsuper,
    role.rolcreatedb,
    role.rolcreaterole,
    role.rolreplication,
    role.rolbypassrls,
    role.rolinherit,
    role.rolcanlogin,
    role.rolconfig,
    shobj_description(role.oid, 'pg_authid') AS marker
  FROM pg_roles AS role
  WHERE role.rolname IN (__NEXPOLY_MANAGED_ROLE_FILTER__)
     OR COALESCE(
          shobj_description(role.oid, 'pg_authid'),
          ''
        ) LIKE 'nexpoly-postgres-media-audit-role-v1:%'
)
SELECT jsonb_build_object(
  'record_type', 'managed_role_matrix',
  'database', current_database(),
  'database_oid', database_value.oid::text,
  'database_owner', pg_get_userbyid(database_value.datdba),
  'ledger_present',
    to_regclass('governance.schema_migrations') IS NOT NULL,
  'legacy_present',
    to_regclass('generation.polytao_jobs') IS NOT NULL,
  'roles', COALESCE((
    SELECT jsonb_agg(
      jsonb_build_object(
        'name', role.rolname,
        'marker', role.marker,
        'superuser', role.rolsuper,
        'create_db', role.rolcreatedb,
        'create_role', role.rolcreaterole,
        'replication', role.rolreplication,
        'bypass_rls', role.rolbypassrls,
        'inherit', role.rolinherit,
        'can_login', role.rolcanlogin,
        'memberships', COALESCE((
          SELECT jsonb_agg(
            granted.rolname
            ORDER BY granted.rolname COLLATE "C"
          )
          FROM pg_auth_members AS membership
          JOIN pg_roles AS granted
            ON granted.oid = membership.roleid
          WHERE membership.member = role.oid
        ), '[]'::jsonb),
        'incoming_memberships', COALESCE((
          SELECT jsonb_agg(
            member_role.rolname
            ORDER BY member_role.rolname COLLATE "C"
          )
          FROM pg_auth_members AS membership
          JOIN pg_roles AS member_role
            ON member_role.oid = membership.member
          WHERE membership.roleid = role.oid
        ), '[]'::jsonb),
        'settings', COALESCE((
          SELECT jsonb_agg(
            setting
            ORDER BY setting COLLATE "C"
          )
          FROM unnest(
            COALESCE(role.rolconfig, ARRAY[]::text[])
          ) AS setting
        ), '[]'::jsonb),
        'shared_owned_objects', COALESCE((
          SELECT jsonb_agg(
            owned
            ORDER BY owned COLLATE "C"
          )
          FROM (
            SELECT 'database:' || database_owned.datname AS owned
            FROM pg_database AS database_owned
            WHERE database_owned.datdba = role.oid
            UNION
            SELECT 'tablespace:' || tablespace.spcname
            FROM pg_tablespace AS tablespace
            WHERE tablespace.spcowner = role.oid
            UNION
            SELECT
              'shared-dependency:' || dependency.classid::text
              || ':' || dependency.objid::text
            FROM pg_shdepend AS dependency
            WHERE dependency.dbid = 0
              AND dependency.refclassid = 'pg_authid'::regclass
              AND dependency.refobjid = role.oid
              AND dependency.deptype = 'o'
          ) AS owned_values
        ), '[]'::jsonb),
        'shared_direct_acl', COALESCE((
          SELECT jsonb_agg(
            jsonb_build_object(
              'object_kind', acl_value.object_kind,
              'object_name', acl_value.object_name,
              'privilege', acl_value.privilege_type,
              'grantable', acl_value.is_grantable
            )
            ORDER BY acl_value.object_kind COLLATE "C",
                     acl_value.object_name COLLATE "C",
                     acl_value.privilege_type COLLATE "C",
                     acl_value.is_grantable
          )
          FROM (
            SELECT
              'database'::text AS object_kind,
              database_acl.datname::text AS object_name,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_database AS database_acl
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                database_acl.datacl,
                acldefault('d', database_acl.datdba)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'tablespace',
              tablespace.spcname,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_tablespace AS tablespace
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                tablespace.spcacl,
                acldefault('t', tablespace.spcowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
          ) AS acl_value
        ), '[]'::jsonb),
        'local_owned_objects', COALESCE((
          SELECT jsonb_agg(
            owned
            ORDER BY owned COLLATE "C"
          )
          FROM (
            SELECT 'schema:' || namespace.nspname AS owned
            FROM pg_namespace AS namespace
            WHERE namespace.nspowner = role.oid
            UNION
            SELECT
              'relation:' || namespace.nspname || '.'
              || relation.relname
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE relation.relowner = role.oid
            UNION
            SELECT
              'function:' || namespace.nspname || '.'
              || procedure.proname || '('
              || pg_get_function_identity_arguments(procedure.oid)
              || ')'
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE procedure.proowner = role.oid
            UNION
            SELECT
              'local-dependency:' || dependency.classid::text
              || ':' || dependency.objid::text
            FROM pg_shdepend AS dependency
            WHERE dependency.dbid = database_value.oid
              AND dependency.refclassid = 'pg_authid'::regclass
              AND dependency.refobjid = role.oid
              AND dependency.deptype = 'o'
          ) AS owned_values
        ), '[]'::jsonb),
        'local_direct_acl', COALESCE((
          SELECT jsonb_agg(
            jsonb_build_object(
              'object_kind', acl_value.object_kind,
              'object_name', acl_value.object_name,
              'privilege', acl_value.privilege_type,
              'grantable', acl_value.is_grantable
            )
            ORDER BY acl_value.object_kind COLLATE "C",
                     acl_value.object_name COLLATE "C",
                     acl_value.privilege_type COLLATE "C",
                     acl_value.is_grantable
          )
          FROM (
            SELECT
              'schema'::text AS object_kind,
              namespace.nspname::text AS object_name,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_namespace AS namespace
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                namespace.nspacl,
                acldefault('n', namespace.nspowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'relation',
              namespace.nspname || '.' || relation.relname,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                relation.relacl,
                acldefault(
                  CASE
                    WHEN relation.relkind = 'S'
                    THEN 's'::"char"
                    ELSE 'r'::"char"
                  END,
                  relation.relowner
                )
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'column',
              namespace.nspname || '.' || relation.relname
              || '.' || attribute.attname,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_attribute AS attribute
            JOIN pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
            WHERE acl.grantee = role.oid
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            UNION ALL
            SELECT
              'function',
              CASE
                WHEN namespace.nspname = 'pg_catalog'
                 AND procedure.proname = 'pg_control_system'
                THEN 'pg_catalog.pg_control_system()'
                ELSE namespace.nspname || '.' || procedure.proname
                     || '('
                     || pg_get_function_identity_arguments(
                          procedure.oid
                        )
                     || ')'
              END,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                procedure.proacl,
                acldefault('f', procedure.proowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'type',
              namespace.nspname || '.' || type_value.typname,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_type AS type_value
            JOIN pg_namespace AS namespace
              ON namespace.oid = type_value.typnamespace
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                type_value.typacl,
                acldefault('T', type_value.typowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'language',
              language.lanname,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_language AS language
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                language.lanacl,
                acldefault('l', language.lanowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'foreign-data-wrapper',
              wrapper.fdwname,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_foreign_data_wrapper AS wrapper
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                wrapper.fdwacl,
                acldefault('F', wrapper.fdwowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'foreign-server',
              server.srvname,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_foreign_server AS server
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                server.srvacl,
                acldefault('S', server.srvowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
            UNION ALL
            SELECT
              'large-object',
              large_object.oid::text,
              acl.privilege_type,
              acl.is_grantable
            FROM pg_largeobject_metadata AS large_object
            CROSS JOIN LATERAL aclexplode(
              COALESCE(
                large_object.lomacl,
                acldefault('L', large_object.lomowner)
              )
            ) AS acl
            WHERE acl.grantee = role.oid
__NEXPOLY_PARAMETER_ACL_UNION__
          ) AS acl_value
        ), '[]'::jsonb),
        'local_default_acl', COALESCE((
          SELECT jsonb_agg(
            jsonb_build_object(
              'owner', owner_role.rolname,
              'namespace', namespace.nspname,
              'object_type', defaults.defaclobjtype,
              'privilege', acl.privilege_type,
              'grantable', acl.is_grantable
            )
            ORDER BY owner_role.rolname COLLATE "C",
                     COALESCE(namespace.nspname, '') COLLATE "C",
                     defaults.defaclobjtype,
                     acl.privilege_type COLLATE "C",
                     acl.is_grantable
          )
          FROM pg_default_acl AS defaults
          JOIN pg_roles AS owner_role
            ON owner_role.oid = defaults.defaclrole
          LEFT JOIN pg_namespace AS namespace
            ON namespace.oid = defaults.defaclnamespace
          CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS acl
          WHERE acl.grantee = role.oid
        ), '[]'::jsonb),
        'local_effective_persistent_write', COALESCE((
          SELECT jsonb_agg(
            object_name
            ORDER BY object_name COLLATE "C"
          )
          FROM (
            SELECT
              'schema:' || namespace.nspname || ':CREATE'
              AS object_name
            FROM pg_namespace AS namespace
            WHERE namespace.nspname
                  NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname !~ '^pg_toast'
              AND has_schema_privilege(
                    role.oid,
                    namespace.oid,
                    'CREATE'
                  )
            UNION
            SELECT
              'relation:' || namespace.nspname || '.'
              || relation.relname
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname
                  NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname !~ '^pg_toast'
              AND CASE
                WHEN relation.relkind IN ('r', 'p', 'f') THEN
                  has_table_privilege(
                    role.oid,
                    relation.oid,
                    'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                  )
                ELSE false
              END
            UNION
            SELECT
              'sequence:' || namespace.nspname || '.'
              || relation.relname
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname
                  NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname !~ '^pg_toast'
              AND CASE
                WHEN relation.relkind = 'S' THEN
                  has_sequence_privilege(
                    role.oid,
                    relation.oid,
                    'USAGE,UPDATE'
                  )
                ELSE false
              END
            UNION
            SELECT
              'column:' || namespace.nspname || '.'
              || relation.relname
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname
                  NOT IN ('pg_catalog', 'information_schema')
              AND namespace.nspname !~ '^pg_toast'
              AND CASE
                WHEN relation.relkind IN (
                  'r', 'p', 'f', 'v', 'm'
                ) THEN
                  has_any_column_privilege(
                    role.oid,
                    relation.oid,
                    'INSERT,UPDATE,REFERENCES'
                  )
                ELSE false
              END
          ) AS writable
        ), '[]'::jsonb)
      )
      ORDER BY role.rolname COLLATE "C"
    )
    FROM selected_roles AS role
  ), '[]'::jsonb)
)
FROM pg_database AS database_value
WHERE database_value.datname = current_database();
COMMIT;
"""
        .replace(
            "__NEXPOLY_EVENT_TRIGGERS_SESSION_ASSERT__",
            (
                EVENT_TRIGGERS_SESSION_ASSERT_SQL
                if postgres_major >= 17
                else ""
            ),
        )
        .replace("__NEXPOLY_MANAGED_ROLE_FILTER__", role_filter)
        .replace("__NEXPOLY_PARAMETER_ACL_UNION__", parameter_acl)
    )


def _parse_managed_role_matrix_record(
    payload: bytes,
    *,
    expected_database: Mapping[str, object],
) -> dict[str, object]:
    records: list[object] = []
    for line in payload.decode("utf-8", "strict").splitlines():
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise MediaEvidenceError(
                "managed audit-role matrix emitted malformed JSON"
            ) from exc
    if len(records) != 1 or not isinstance(records[0], dict):
        raise MediaEvidenceError(
            "managed audit-role matrix output is incomplete"
        )
    record = records[0]
    if (
        set(record)
        != {
            "record_type",
            "database",
            "database_oid",
            "database_owner",
            "ledger_present",
            "legacy_present",
            "roles",
        }
        or record.get("record_type") != "managed_role_matrix"
        or record.get("database") != expected_database.get("name")
        or record.get("database_oid") != expected_database.get("oid")
        or record.get("database_owner") != expected_database.get("owner")
        or not isinstance(record.get("ledger_present"), bool)
        or not isinstance(record.get("legacy_present"), bool)
        or not isinstance(record.get("roles"), list)
        or len(record["roles"]) > MAX_MANAGED_ROLE_MATRIX_ROLES
    ):
        raise MediaEvidenceError(
            "managed audit-role matrix database epoch differs"
        )
    role_fields = {
        "name",
        "marker",
        "superuser",
        "create_db",
        "create_role",
        "replication",
        "bypass_rls",
        "inherit",
        "can_login",
        "memberships",
        "incoming_memberships",
        "settings",
        "shared_owned_objects",
        "shared_direct_acl",
        "local_owned_objects",
        "local_direct_acl",
        "local_default_acl",
        "local_effective_persistent_write",
    }
    normalized_roles: list[dict[str, object]] = []
    names: set[str] = set()
    for value in record["roles"]:
        if (
            not isinstance(value, dict)
            or set(value) != role_fields
            or not _valid_pg_identifier(value.get("name"))
            or value["name"] in names
            or value.get("marker") is not None
            and not isinstance(value.get("marker"), str)
            or any(
                not isinstance(value.get(field), bool)
                for field in (
                    "superuser",
                    "create_db",
                    "create_role",
                    "replication",
                    "bypass_rls",
                    "inherit",
                    "can_login",
                )
            )
        ):
            raise MediaEvidenceError(
                "managed audit-role matrix role identity is invalid"
            )
        names.add(str(value["name"]))
        normalized = dict(value)
        for field in (
            "memberships",
            "incoming_memberships",
            "settings",
            "shared_owned_objects",
            "local_owned_objects",
            "local_effective_persistent_write",
        ):
            items = normalized.get(field)
            if (
                not isinstance(items, list)
                or any(not isinstance(item, str) for item in items)
                or items != sorted(set(items))
            ):
                raise MediaEvidenceError(
                    "managed audit-role matrix string inventory is invalid"
                )
        for field in (
            "shared_direct_acl",
            "local_direct_acl",
            "local_default_acl",
        ):
            items = normalized.get(field)
            if not isinstance(items, list):
                raise MediaEvidenceError(
                    "managed audit-role matrix ACL inventory is invalid"
                )
            serialized = [canonical_json_bytes(item) for item in items]
            if (
                any(not isinstance(item, dict) for item in items)
                or len(set(serialized)) != len(serialized)
            ):
                raise MediaEvidenceError(
                    "managed audit-role matrix ACL inventory is invalid"
                )
            normalized[field] = sorted(
                (dict(item) for item in items),
                key=canonical_json_bytes,
            )
        normalized_roles.append(normalized)
    normalized_roles.sort(key=lambda value: str(value["name"]))
    return {
        "database": record["database"],
        "database_oid": record["database_oid"],
        "database_owner": record["database_owner"],
        "ledger_present": record["ledger_present"],
        "legacy_present": record["legacy_present"],
        "roles": normalized_roles,
    }


def _managed_role_matrix(
    runner: CommandRunner,
    *,
    container_id: str,
    databases: Sequence[Mapping[str, object]],
    online_admin_role: str,
    postgres_major: int,
    trusted_image_id: str,
    expected_contracts: Mapping[str, str] | None,
    allow_missing: bool,
) -> dict[str, object]:
    if (
        CONTAINER_RE.fullmatch(container_id) is None
        or not _valid_pg_identifier(online_admin_role)
        or postgres_major not in SUPPORTED_POSTGRES_AUDIT_MAJORS
        or DIGEST_RE.fullmatch(trusted_image_id) is None
        or not databases
        or len(databases) > MAX_MANAGED_ROLE_MATRIX_DATABASES
    ):
        raise MediaEvidenceError(
            "managed audit-role matrix authority is invalid"
        )
    expected: dict[str, dict[str, object]] = {}
    ordered_databases: list[dict[str, object]] = []
    database_names: set[str] = set()
    for raw_database in databases:
        database = dict(raw_database)
        name = database.get("name")
        role = database.get("audit_role")
        if (
            not _valid_pg_identifier(name)
            or not isinstance(database.get("oid"), str)
            or not str(database["oid"]).isdigit()
            or not _valid_pg_identifier(database.get("owner"))
            or database.get("allow_connections") is not True
            or database.get("template") is not False
            or ROLE_RE.fullmatch(str(role or "")) is None
            or name in database_names
            or role in expected
        ):
            raise MediaEvidenceError(
                "managed audit-role matrix database authority is invalid"
            )
        database_names.add(str(name))
        expected[str(role)] = {
            "database": str(name),
            "contract_sha256": (
                expected_contracts.get(str(role))
                if expected_contracts is not None
                else None
            ),
        }
        ordered_databases.append(database)
    ordered_databases.sort(key=lambda value: str(value["name"]))
    if (
        expected_contracts is not None
        and (
            set(expected_contracts) != set(expected)
            or any(
                DIGEST_RE.fullmatch(value) is None
                for value in expected_contracts.values()
            )
        )
    ):
        raise MediaEvidenceError(
            "managed audit-role matrix contract map differs"
        )
    role_names = sorted(expected)
    sql = _managed_role_matrix_sql(
        len(role_names),
        postgres_major=postgres_major,
    )
    psql_variables: list[str] = []
    for index, role_name in enumerate(role_names):
        psql_variables.extend(
            ["-v", f"managed_role_{index}={role_name}"]
        )
    records: list[dict[str, object]] = []
    for database in ordered_databases:
        completed = _run_trusted_psql(
            runner,
            container_id=container_id,
            postgres_major=postgres_major,
            pgoptions=_psql_audit_pgoptions(postgres_major),
            arguments=[
                *psql_variables,
                "-U",
                online_admin_role,
                "-d",
                str(database["name"]),
            ],
            input_bytes=sql.encode("utf-8"),
            timeout=600,
            expected_image_id=trusted_image_id,
        )
        records.append(
            _parse_managed_role_matrix_record(
                completed.stdout,
                expected_database=database,
            )
        )
    global_fields = {
        "name",
        "marker",
        "superuser",
        "create_db",
        "create_role",
        "replication",
        "bypass_rls",
        "inherit",
        "can_login",
        "memberships",
        "incoming_memberships",
        "settings",
        "shared_owned_objects",
        "shared_direct_acl",
    }
    global_roles: dict[str, dict[str, object]] = {}
    database_role_maps: dict[
        str, dict[str, dict[str, object]]
    ] = {}
    for record in records:
        role_map = {
            str(value["name"]): value
            for value in record["roles"]  # type: ignore[index]
        }
        database_role_maps[str(record["database"])] = role_map
        for name, value in role_map.items():
            projection = {
                key: value[key] for key in global_fields
            }
            previous = global_roles.setdefault(name, projection)
            if previous != projection:
                raise MediaEvidenceError(
                    "managed audit-role global state changed across "
                    "database snapshots"
                )
    orphaned = sorted(set(global_roles) - set(expected))
    if orphaned:
        raise MediaEvidenceError(
            "orphan managed audit-role marker exists: "
            + ", ".join(orphaned)
        )
    expected_settings = [
        "default_transaction_read_only=on",
        "lock_timeout=5s",
        "statement_timeout=5min",
    ]
    normalized_roles: list[dict[str, object]] = []
    for role_name in role_names:
        global_state = global_roles.get(role_name)
        if global_state is None:
            if allow_missing:
                normalized_roles.append(
                    {
                        "name": role_name,
                        "target_database": expected[role_name][
                            "database"
                        ],
                        "state": "absent",
                    }
                )
                continue
            raise MediaEvidenceError(
                "managed audit-role is absent after provisioning"
            )
        marker = global_state["marker"]
        marker_prefix = ROLE_CONTRACT_POLICY + ":"
        observed_contract = (
            marker.removeprefix(marker_prefix)
            if isinstance(marker, str)
            and marker.startswith(marker_prefix)
            else None
        )
        expected_contract = expected[role_name][
            "contract_sha256"
        ]
        if (
            not isinstance(observed_contract, str)
            or DIGEST_RE.fullmatch(observed_contract) is None
            or expected_contract is not None
            and observed_contract != expected_contract
            or any(
                global_state[field] is not False
                for field in (
                    "superuser",
                    "create_db",
                    "create_role",
                    "replication",
                    "bypass_rls",
                    "inherit",
                    "can_login",
                )
            )
            or global_state["memberships"] != []
            or global_state["incoming_memberships"] != []
            or global_state["settings"] != expected_settings
            or global_state["shared_owned_objects"] != []
            or global_state["shared_direct_acl"]
            != [
                {
                    "object_kind": "database",
                    "object_name": expected[role_name][
                        "database"
                    ],
                    "privilege": "CONNECT",
                    "grantable": False,
                }
            ]
        ):
            raise MediaEvidenceError(
                "managed audit-role global contract differs"
            )
        target_database = str(
            expected[role_name]["database"]
        )
        local_states: list[dict[str, object]] = []
        for database_record in records:
            database_name = str(database_record["database"])
            role_state = database_role_maps[database_name].get(
                role_name
            )
            if role_state is None:
                raise MediaEvidenceError(
                    "managed audit-role disappeared across database "
                    "snapshots"
                )
            expected_acl: list[dict[str, object]] = []
            if database_name == target_database:
                expected_acl.append(
                    {
                        "object_kind": "function",
                        "object_name": (
                            "pg_catalog.pg_control_system()"
                        ),
                        "privilege": "EXECUTE",
                        "grantable": False,
                    }
                )
                if database_record["ledger_present"] is True:
                    expected_acl.extend(
                        [
                            {
                                "object_kind": "relation",
                                "object_name": (
                                    "governance.schema_migrations"
                                ),
                                "privilege": "SELECT",
                                "grantable": False,
                            },
                            {
                                "object_kind": "schema",
                                "object_name": "governance",
                                "privilege": "USAGE",
                                "grantable": False,
                            },
                        ]
                    )
                if database_record["legacy_present"] is True:
                    expected_acl.extend(
                        [
                            {
                                "object_kind": "relation",
                                "object_name": (
                                    "generation.polytao_jobs"
                                ),
                                "privilege": "SELECT",
                                "grantable": False,
                            },
                            {
                                "object_kind": "schema",
                                "object_name": "generation",
                                "privilege": "USAGE",
                                "grantable": False,
                            },
                        ]
                    )
            expected_acl.sort(key=canonical_json_bytes)
            if (
                role_state["local_owned_objects"] != []
                or role_state["local_default_acl"] != []
                or role_state[
                    "local_effective_persistent_write"
                ]
                != []
                or role_state["local_direct_acl"] != expected_acl
            ):
                raise MediaEvidenceError(
                    "managed audit-role cross-database ACL or ownership "
                    "differs"
                )
            local_states.append(
                {
                    "database": database_name,
                    "database_oid": database_record[
                        "database_oid"
                    ],
                    "ledger_present": database_record[
                        "ledger_present"
                    ],
                    "legacy_present": database_record[
                        "legacy_present"
                    ],
                    "local_state_sha256": sha256_bytes(
                        canonical_json_bytes(
                            {
                                key: role_state[key]
                                for key in (
                                    "local_owned_objects",
                                    "local_direct_acl",
                                    "local_default_acl",
                                    (
                                        "local_effective_"
                                        "persistent_write"
                                    ),
                                )
                            }
                        )
                    ),
                }
            )
        normalized_roles.append(
            {
                "name": role_name,
                "target_database": target_database,
                "state": "exact",
                "role_contract_sha256": observed_contract,
                "global_state_sha256": sha256_bytes(
                    canonical_json_bytes(global_state)
                ),
                "databases": local_states,
            }
        )
    document: dict[str, object] = {
        "schema_version": MANAGED_ROLE_MATRIX_SCHEMA_VERSION,
        "policy": ROLE_CONTRACT_POLICY,
        "container_id": container_id,
        "postgres_major": postgres_major,
        "online_admin_role": online_admin_role,
        "roles": normalized_roles,
    }
    return {
        **document,
        "matrix_sha256": sha256_bytes(
            canonical_json_bytes(document)
        ),
    }


def external_database_role_plan(
    registry: Registry,
    discovery: Discovery,
    *,
    role_sql_sha256: str,
    runner: CommandRunner,
) -> dict[str, object]:
    """Emit the exact, source-epoch-bound role provisioning worklist."""

    if DIGEST_RE.fullmatch(role_sql_sha256) is None:
        raise MediaEvidenceError(
            "audit-role plan lacks the exact F role SQL digest"
        )
    entries: list[dict[str, object]] = []
    audit_image_ids = dict(registry.audit_image_ids)
    for descriptor in registry.descriptors:
        if descriptor.audit_method != "live-read-only":
            continue
        descriptor_entries: list[dict[str, object]] = []
        source = discovery.media.get(descriptor.media_id)
        active = (
            _active_media_attachments(source)
            if source is not None
            else []
        )
        if source is None or len(active) != 1:
            raise MediaEvidenceError(
                "audit-role plan live source epoch is incomplete"
            )
        source_digest = sha256_bytes(
            canonical_json_bytes(source.document())
        )
        major = descriptor.source_postgres_major
        audit_image_id = (
            audit_image_ids.get(int(major))
            if isinstance(major, int)
            else None
        )
        if (
            not isinstance(audit_image_id, str)
            or DIGEST_RE.fullmatch(audit_image_id) is None
        ):
            raise MediaEvidenceError(
                "audit-role plan lacks its exact local client image ID"
            )
        cluster_system_identifier = _live_source_system_identifier(
            runner,
            source,
            trusted_image_id=audit_image_id,
        )
        live_inventory = _container_database_inventory(
            runner,
            str(active[0]["container_id"]),
            online_admin_role=str(descriptor.online_admin_role),
            postgres_major=int(major),
            use_trusted_client=True,
            trusted_image_id=audit_image_id,
        )
        expected_inventory = sorted(
            [
                {
                    key: database[key]
                    for key in (
                        "name",
                        "oid",
                        "owner",
                        "allow_connections",
                        "template",
                    )
                }
                for database in descriptor.databases
            ],
            key=lambda value: str(value["name"]),
        )
        if live_inventory != expected_inventory:
            raise MediaEvidenceError(
                "audit-role plan database inventory changed"
            )
        for database in descriptor.databases:
            if database["allow_connections"] is not True:
                raise MediaEvidenceError(
                    "audit-role plan contains a non-connectable database"
                )
            entry: dict[str, object] = {
                "media_id": descriptor.media_id,
                "source_document_sha256": source_digest,
                "container_id": active[0]["container_id"],
                "attachment_sha256": sha256_bytes(
                    canonical_json_bytes(active[0])
                ),
                "source_kind": source.kind,
                "source_locator": source.locator,
                "mount_destination": active[0]["destination"],
                "audit_method": descriptor.audit_method,
                "source_postgres_major": major,
                "audit_image_id": audit_image_id,
                "cluster_system_identifier": (
                    cluster_system_identifier
                ),
                "online_admin_role": descriptor.online_admin_role,
                "database": database["name"],
                "database_oid": database["oid"],
                "database_owner": database["owner"],
                "database_template": database["template"],
                "audit_role": database["audit_role"],
                "migration_scope": database["migration_scope"],
            }
            role_contract_sha256 = _external_role_contract_sha256(
                authority_rules_sha256=(
                    registry.authority_rules_sha256
                ),
                role_sql_sha256=role_sql_sha256,
                entry=entry,
            )
            entry["role_contract_sha256"] = role_contract_sha256
            entry["psql_variables"] = {
                "audit_database": database["name"],
                "audit_role": database["audit_role"],
                "role_contract_sha256": role_contract_sha256,
            }
            entries.append(entry)
            descriptor_entries.append(entry)
        contract_map = {
            str(entry["audit_role"]): str(
                entry["role_contract_sha256"]
            )
            for entry in descriptor_entries
        }
        role_matrix = _managed_role_matrix(
            runner,
            container_id=str(active[0]["container_id"]),
            databases=descriptor.databases,
            online_admin_role=str(descriptor.online_admin_role),
            postgres_major=int(major),
            trusted_image_id=audit_image_id,
            expected_contracts=contract_map,
            allow_missing=True,
        )
        for entry in descriptor_entries:
            entry["preprovision_role_matrix_sha256"] = (
                role_matrix["matrix_sha256"]
            )
    entries.sort(
        key=lambda value: (
            str(value["media_id"]),
            str(value["database"]),
            str(value["database_oid"]),
        )
    )
    if not entries:
        raise MediaEvidenceError(
            "audit-role plan has no online databases"
        )
    role_identities = [
        (
            str(entry["cluster_system_identifier"]),
            str(entry["audit_role"]),
        )
        for entry in entries
    ]
    if len(set(role_identities)) != len(role_identities):
        raise MediaEvidenceError(
            "audit-role plan maps one cluster-global role to multiple "
            "database contracts"
        )
    unsealed: dict[str, object] = {
        "schema_version": 2,
        "phase": "pre-provisioning-inventory",
        "media_authority_rules_sha256": (
            registry.authority_rules_sha256
        ),
        "runtime_registry_sha256": registry.digest,
        "docker_inventory_sha256": discovery.docker_inventory_sha256,
        "discovery_state_sha256": _discovery_state_sha256(discovery),
        "role_sql_sha256": role_sql_sha256,
        "databases": entries,
    }
    return {
        **unsealed,
        "plan_sha256": sha256_bytes(canonical_json_bytes(unsealed)),
    }


def provision_external_database_roles(
    plan: Mapping[str, object],
    *,
    role_sql: bytes,
    runner: CommandRunner,
) -> dict[str, object]:
    """Apply the exact F role SQL to one confirmed, fresh role plan."""

    expected_role_sql = os.environ.get(AUDIT_ROLE_SQL_DIGEST_ENV, "")
    expected_plan_fields = {
        "schema_version",
        "phase",
        "media_authority_rules_sha256",
        "runtime_registry_sha256",
        "docker_inventory_sha256",
        "discovery_state_sha256",
        "role_sql_sha256",
        "databases",
        "plan_sha256",
    }
    unsigned_plan = {
        key: value
        for key, value in plan.items()
        if key != "plan_sha256"
    }
    if (
        DIGEST_RE.fullmatch(expected_role_sql) is None
        or sha256_bytes(role_sql) != expected_role_sql
        or plan.get("role_sql_sha256") != expected_role_sql
        or not isinstance(plan.get("databases"), list)
        or set(plan) != expected_plan_fields
        or plan.get("schema_version") != 2
        or plan.get("phase") != "pre-provisioning-inventory"
        or any(
            not isinstance(plan.get(key), str)
            or DIGEST_RE.fullmatch(str(plan[key])) is None
            for key in (
                "media_authority_rules_sha256",
                "runtime_registry_sha256",
                "docker_inventory_sha256",
                "discovery_state_sha256",
                "plan_sha256",
            )
        )
        or plan.get("plan_sha256")
        != sha256_bytes(canonical_json_bytes(unsigned_plan))
    ):
        raise MediaEvidenceError(
            "role provisioning SQL or confirmed plan differs"
        )
    completed: list[dict[str, object]] = []

    def validate_epoch(entry: Mapping[str, object]) -> None:
        container_id = str(entry["container_id"])
        container = _optional_docker_inspect(
            runner,
            "container",
            container_id,
        )
        if container is None:
            raise MediaEvidenceError(
                "role provisioning container disappeared"
            )
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise MediaEvidenceError(
                "role provisioning container mounts are invalid"
            )
        candidates = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Destination")
            == entry.get("mount_destination")
            and (
                entry.get("source_kind") == "docker_volume"
                and mount.get("Type") == "volume"
                and mount.get("Name") == entry.get("source_locator")
                or entry.get("source_kind") == "container_bind"
                and mount.get("Type") == "bind"
                and _canonical_bind_source(mount.get("Source"))
                == entry.get("source_locator")
            )
        ]
        if (
            len(candidates) != 1
            or sha256_bytes(
                canonical_json_bytes(
                    _attached_record(container, candidates[0])
                )
            )
            != entry.get("attachment_sha256")
            or _online_container_admin_role(runner, container_id)
            != entry.get("online_admin_role")
        ):
            raise MediaEvidenceError(
                "role provisioning source/container epoch changed"
            )
        inventory = _container_database_inventory(
            runner,
            container_id,
            online_admin_role=str(entry["online_admin_role"]),
            postgres_major=int(entry["source_postgres_major"]),
            use_trusted_client=True,
            trusted_image_id=str(entry["audit_image_id"]),
        )
        if not any(
            record["name"] == entry.get("database")
            and record["oid"] == entry.get("database_oid")
            and record["owner"] == entry.get("database_owner")
            and record["allow_connections"] is True
            and record["template"] == entry.get("database_template")
            for record in inventory
        ):
            raise MediaEvidenceError(
                "role provisioning database epoch changed"
            )
        source = DiscoveredMedia(
            media_id=str(entry["media_id"]),
            kind=str(entry["source_kind"]),
            locator=str(entry["source_locator"]),
            data_subpath=".",
            attached=(),
            signature="postgres",
            postgres_major=int(entry["source_postgres_major"]),
        )
        if (
            _live_source_system_identifier(
                runner,
                source,
                trusted_image_id=str(entry["audit_image_id"]),
            )
            != entry.get("cluster_system_identifier")
        ):
            raise MediaEvidenceError(
                "role provisioning cluster system identifier changed"
            )

    role_identities: set[tuple[str, str]] = set()
    validated_entries: list[dict[str, object]] = []
    for entry in plan["databases"]:  # type: ignore[index]
        expected_contract = (
            _external_role_contract_sha256(
                authority_rules_sha256=str(
                    plan["media_authority_rules_sha256"]
                ),
                role_sql_sha256=expected_role_sql,
                entry=entry if isinstance(entry, dict) else {},
            )
        )
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("container_id"), str)
            or CONTAINER_RE.fullmatch(entry["container_id"]) is None
            or not _valid_pg_identifier(entry.get("online_admin_role"))
            or not _valid_pg_identifier(entry.get("database"))
            or ROLE_RE.fullmatch(str(entry.get("audit_role", ""))) is None
            or entry.get("source_postgres_major")
            not in SUPPORTED_POSTGRES_AUDIT_MAJORS
            or not isinstance(entry.get("audit_image_id"), str)
            or DIGEST_RE.fullmatch(entry["audit_image_id"]) is None
            or not isinstance(
                entry.get("cluster_system_identifier"),
                str,
            )
            or PG_SYSTEM_ID_RE.fullmatch(
                entry["cluster_system_identifier"]
            )
            is None
            or not isinstance(entry.get("database_oid"), str)
            or not entry["database_oid"].isdigit()
            or not _valid_pg_identifier(entry.get("database_owner"))
            or not isinstance(entry.get("database_template"), bool)
            or entry.get("database_template") is not False
            or not isinstance(
                entry.get("preprovision_role_matrix_sha256"),
                str,
            )
            or DIGEST_RE.fullmatch(
                entry["preprovision_role_matrix_sha256"]
            )
            is None
            or entry.get("migration_scope")
            not in {
                "nexpoly-ledger",
                "adjacent-record-only",
            }
            or entry.get("role_contract_sha256")
            != expected_contract
            or entry.get("psql_variables")
            != {
                "audit_database": entry.get("database"),
                "audit_role": entry.get("audit_role"),
                "role_contract_sha256": expected_contract,
            }
        ):
            raise MediaEvidenceError(
                "role provisioning plan entry is invalid"
            )
        role_identity = (
            str(entry["cluster_system_identifier"]),
            str(entry["audit_role"]),
        )
        if role_identity in role_identities:
            raise MediaEvidenceError(
                "one cluster-global role maps to multiple database "
                "contracts"
            )
        role_identities.add(role_identity)
        validated_entries.append(entry)

    grouped_entries: dict[str, list[dict[str, object]]] = {}
    for entry in validated_entries:
        grouped_entries.setdefault(
            str(entry["container_id"]),
            [],
        ).append(entry)

    def validate_role_matrices(
        *,
        allow_missing: bool,
        require_planned_digest: bool,
    ) -> list[dict[str, object]]:
        matrices: list[dict[str, object]] = []
        for container_id, group in sorted(grouped_entries.items()):
            first = group[0]
            common_fields = (
                "cluster_system_identifier",
                "online_admin_role",
                "source_postgres_major",
                "audit_image_id",
            )
            if any(
                any(
                    entry[field] != first[field]
                    for field in common_fields
                )
                for entry in group[1:]
            ):
                raise MediaEvidenceError(
                    "managed audit-role matrix spans incompatible "
                    "cluster epochs"
                )
            databases = sorted(
                [
                    {
                        "name": entry["database"],
                        "oid": entry["database_oid"],
                        "owner": entry["database_owner"],
                        "allow_connections": True,
                        "template": entry["database_template"],
                        "audit_role": entry["audit_role"],
                        "migration_scope": entry["migration_scope"],
                    }
                    for entry in group
                ],
                key=lambda value: str(value["name"]),
            )
            current_inventory = _container_database_inventory(
                runner,
                container_id,
                online_admin_role=str(
                    first["online_admin_role"]
                ),
                postgres_major=int(
                    first["source_postgres_major"]
                ),
                use_trusted_client=True,
                trusted_image_id=str(first["audit_image_id"]),
            )
            expected_inventory = [
                {
                    key: database[key]
                    for key in (
                        "name",
                        "oid",
                        "owner",
                        "allow_connections",
                        "template",
                    )
                }
                for database in databases
            ]
            if current_inventory != expected_inventory:
                raise MediaEvidenceError(
                    "managed audit-role matrix database inventory "
                    "changed"
                )
            matrix = _managed_role_matrix(
                runner,
                container_id=container_id,
                databases=databases,
                online_admin_role=str(
                    first["online_admin_role"]
                ),
                postgres_major=int(
                    first["source_postgres_major"]
                ),
                trusted_image_id=str(first["audit_image_id"]),
                expected_contracts={
                    str(entry["audit_role"]): str(
                        entry["role_contract_sha256"]
                    )
                    for entry in group
                },
                allow_missing=allow_missing,
            )
            planned_digests = {
                str(
                    entry[
                        "preprovision_role_matrix_sha256"
                    ]
                )
                for entry in group
            }
            if (
                require_planned_digest
                and (
                    len(planned_digests) != 1
                    or matrix["matrix_sha256"]
                    not in planned_digests
                )
            ):
                raise MediaEvidenceError(
                    "managed audit-role matrix changed after plan"
                )
            matrices.append(matrix)
        return matrices

    # A malformed later entry must never be discovered after an earlier
    # database has already been mutated.  Freeze the complete plan shape and
    # every cluster-global role identity first, then confirm that every live
    # database still belongs to the planned epoch before executing any SQL.
    for entry in validated_entries:
        validate_epoch(entry)
    validate_role_matrices(
        allow_missing=True,
        require_planned_digest=True,
    )

    for entry in validated_entries:
        # Recheck immediately before the mutation as well.  The all-entry
        # preflight above provides plan atomicity, while this check closes the
        # race between preflight and the individual database transaction.
        validate_epoch(entry)
        if (
            _online_container_admin_role(
                runner,
                str(entry["container_id"]),
            )
            != entry["online_admin_role"]
        ):
            raise MediaEvidenceError(
                "role provisioning administrator differs from connection "
                "authority"
            )
        psql_arguments = [
            "-v",
            f"audit_role={entry['audit_role']}",
            "-v",
            f"audit_database={entry['database']}",
            "-v",
            f"expected_database_oid={entry['database_oid']}",
            "-v",
            f"expected_database_owner={entry['database_owner']}",
            "-v",
            f"expected_session_user={entry['online_admin_role']}",
            "-v",
            (
                "expected_event_triggers_disabled="
                + (
                    "true"
                    if int(entry["source_postgres_major"]) >= 17
                    else "false"
                )
            ),
            "-v",
            (
                "role_contract_sha256="
                + str(entry["role_contract_sha256"])
            ),
            "-U",
            str(entry["online_admin_role"]),
            "-d",
            str(entry["database"]),
        ]
        _run_trusted_psql(
            runner,
            container_id=str(entry["container_id"]),
            postgres_major=int(entry["source_postgres_major"]),
            pgoptions=_psql_provision_pgoptions(
                int(entry["source_postgres_major"])
            ),
            arguments=psql_arguments,
            input_bytes=role_sql,
            timeout=600,
            expected_image_id=str(entry["audit_image_id"]),
        )
        validate_epoch(entry)
        database_authority = {
            "name": entry["database"],
            "oid": entry["database_oid"],
            "owner": entry["database_owner"],
            "allow_connections": True,
            "template": entry["database_template"],
            "audit_role": entry["audit_role"],
            "migration_scope": entry["migration_scope"],
        }
        descriptor = MediaDescriptor(
            media_id=str(entry["media_id"]),
            kind=str(entry["source_kind"]),
            database=str(entry["database"]),
            database_user=str(entry["audit_role"]),
            disposition="read-only-online",
            audit_method="live-read-only",
            online_admin_role=str(entry["online_admin_role"]),
            classification="nexpoly-db",
            source_postgres_major=int(
                entry["source_postgres_major"]
            ),
            databases=(database_authority,),
        )
        audit = _audit_container_database(
            runner,
            str(entry["container_id"]),
            descriptor,
            database_authority=database_authority,
            isolated=False,
            trusted_image_id=str(entry["audit_image_id"]),
            expected_role_contract_sha256=str(
                entry["role_contract_sha256"]
            ),
        )
        validate_epoch(entry)
        role_state = {
            key: audit[key]
            for key in (
                "role_superuser",
                "role_create_db",
                "role_create_role",
                "role_replication",
                "role_bypass_rls",
                "role_inherit",
                "role_can_login",
                "role_contract_marker",
                "role_contract_sha256",
                "role_memberships",
                "role_incoming_memberships",
                "role_settings",
                "role_owned_objects",
                "role_direct_acl",
                "role_default_acl",
                "role_effective_persistent_write",
            )
        }
        completed.append(
            {
                "media_id": entry["media_id"],
                "database": entry["database"],
                "database_oid": entry["database_oid"],
                "audit_role": entry["audit_role"],
                "role_contract_sha256": (
                    entry["role_contract_sha256"]
                ),
                "verified_role_state_sha256": sha256_bytes(
                    canonical_json_bytes(role_state)
                ),
            }
        )
    final_role_matrices = validate_role_matrices(
        allow_missing=False,
        require_planned_digest=False,
    )
    unsealed: dict[str, object] = {
        "schema_version": 2,
        "phase": "role-sql-applied-requires-fresh-build",
        "plan_sha256": plan["plan_sha256"],
        "role_sql_sha256": expected_role_sql,
        "databases": completed,
        "managed_role_matrices": [
            {
                "container_id": matrix["container_id"],
                "matrix_sha256": matrix["matrix_sha256"],
            }
            for matrix in final_role_matrices
        ],
    }
    return {
        **unsealed,
        "result_sha256": sha256_bytes(canonical_json_bytes(unsealed)),
    }


def _inherited_audit_role_sql() -> bytes:
    raw_descriptor = os.environ.get("NEXPOLY_MEDIA_AUDIT_ROLE_SQL_FD")
    expected = os.environ.get(AUDIT_ROLE_SQL_DIGEST_ENV, "")
    if (
        not isinstance(raw_descriptor, str)
        or not raw_descriptor.isdigit()
        or DIGEST_RE.fullmatch(expected) is None
    ):
        raise MediaEvidenceError(
            "role provisioning lacks the pinned F SQL descriptor"
        )
    descriptor = int(raw_descriptor)
    try:
        before = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = _read_fd(descriptor, 16 * 1024 * 1024)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise MediaEvidenceError(
            "role provisioning F SQL descriptor is invalid"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or sha256_bytes(payload) != expected
    ):
        raise MediaEvidenceError(
            "role provisioning F SQL descriptor differs"
        )
    return payload


def _active_media_attachments(
    source: DiscoveredMedia,
) -> list[dict[str, object]]:
    return [
        dict(value)
        for value in source.attached
        if str(value["state"]) in ACTIVE_CONTAINER_STATES
    ]


def _runtime_database_records(
    records: object,
) -> tuple[dict[str, object], ...]:
    if not isinstance(records, list) or not records:
        raise MediaEvidenceError(
            "dynamic PostgreSQL audit lacks a database inventory"
        )
    result: list[dict[str, object]] = []
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("audit_state") != "complete"
            or not isinstance(record.get("audit"), dict)
        ):
            raise MediaEvidenceError(
                "dynamic PostgreSQL inventory is not fully audited"
            )
        result.append(
            {
                key: record[key]
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
        )
    return tuple(result)


def _revalidate_live_registry_epoch(
    runner: CommandRunner,
    descriptors: Sequence[MediaDescriptor],
    discovery: Discovery,
    *,
    audit_image_ids: Mapping[int, str],
) -> None:
    """CAS every live cluster's admin and complete database inventory."""

    for descriptor in descriptors:
        if descriptor.audit_method not in {
            "live-read-only",
            "live-read-only-adjacent",
        }:
            continue
        source = discovery.media.get(descriptor.media_id)
        if source is None:
            raise MediaEvidenceError(
                "live registry medium disappeared before publication"
            )
        active = _active_media_attachments(source)
        if len(active) != 1:
            raise MediaEvidenceError(
                "live registry reader changed before publication"
            )
        container_id = str(active[0]["container_id"])
        admin = _online_container_admin_role(runner, container_id)
        if admin != descriptor.online_admin_role:
            raise MediaEvidenceError(
                "live registry administrator changed before publication"
            )
        observed = _container_database_inventory(
            runner,
            container_id,
            online_admin_role=admin,
            postgres_major=int(descriptor.source_postgres_major),
            use_trusted_client=True,
            trusted_image_id=audit_image_ids.get(
                int(descriptor.source_postgres_major)
            ),
        )
        expected = [
            {
                key: database[key]
                for key in (
                    "name",
                    "oid",
                    "owner",
                    "allow_connections",
                    "template",
                )
            }
            for database in descriptor.databases
        ]
        if observed != expected:
            raise MediaEvidenceError(
                "live database inventory changed before registry publication"
            )


AuditCheckpointSink = (
    dict[str, dict[str, object]]
    | Callable[[str, dict[str, object]], None]
)


def _retained_source_admin_role(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> str:
    """Derive the exact bootstrap admin from retained container provenance."""

    container_ids = sorted(
        {
            str(attachment["container_id"])
            for attachment in source.attached
            if isinstance(attachment.get("container_id"), str)
        }
    )
    if not container_ids:
        raise MediaEvidenceError(
            "orphan inactive PostgreSQL media lacks retained POSTGRES_USER "
            "authority"
        )
    roles = {
        _online_container_admin_role(runner, container_id)
        for container_id in container_ids
    }
    if len(roles) != 1:
        raise MediaEvidenceError(
            "inactive PostgreSQL retained containers disagree on POSTGRES_USER"
        )
    return next(iter(roles))


def _emit_audit_checkpoint(
    sink: AuditCheckpointSink | None,
    media_id: str,
    checkpoint: dict[str, object],
) -> None:
    if sink is None:
        return
    if callable(sink):
        sink(media_id, checkpoint)
    else:
        sink[media_id] = checkpoint


def _derive_isolated_volume_descriptor(
    authority: MediaAuthorityRules,
    source: DiscoveredMedia,
    *,
    primary_database: str,
    runner: CommandRunner,
    operation: ScratchOperation,
    checkpoint_sink: AuditCheckpointSink | None = None,
) -> MediaDescriptor:
    major = source.postgres_major
    if (
        source.kind != "docker_volume"
        or source.signature != "postgres"
        or major not in _audit_images_map(authority)
        or _active_media_attachments(source)
    ):
        raise MediaEvidenceError(
            "dynamic PostgreSQL inventory requires an inactive supported volume"
        )
    source_admin_role = _retained_source_admin_role(runner, source)
    provisional_descriptor = MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database=primary_database,
        database_user=source_admin_role,
        disposition="retained-private-isolated",
        audit_method="isolated-volume-copy-read-only",
        classification="nexpoly-db",
        source_postgres_major=major,
        databases=(),
    )
    provisional_registry = Registry(
        payload=b"",
        digest=sha256_bytes(b"dynamic-media-inventory"),
        audit_image=_audit_image_for_major(authority, POSTGRES_MAJOR),
        auditor_sha256=_auditor_digest(),
        descriptors=(provisional_descriptor,),
        required_online_databases=(),
        boundary={},
        authority_rules_sha256=authority.digest,
        audit_images=authority.audit_images,
        postgres_uid=authority.postgres_uid,
        postgres_gid=authority.postgres_gid,
        production_identity=dict(authority.production_identity),
    )
    database, source_digest, before, after, isolation = (
        _isolated_volume_audit(
            runner,
            provisional_registry,
            provisional_descriptor,
            source,
            operation=operation,
            resource_prefix=(
                "inventory-"
                + hashlib.sha256(
                    source.media_id.encode("utf-8")
                ).hexdigest()[:24]
            ),
            derive_inventory=True,
        )
    )
    databases = _runtime_database_records(database.get("databases"))
    if not any(
        record["name"] == primary_database
        and record["allow_connections"] is True
        for record in databases
    ):
        raise MediaEvidenceError(
            "dynamic PostgreSQL medium lacks its logical primary database"
        )
    descriptor = MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database=primary_database,
        database_user=source_admin_role,
        disposition="retained-private-isolated",
        audit_method="isolated-volume-copy-read-only",
        classification="nexpoly-db",
        source_postgres_major=major,
        databases=databases,
    )
    _emit_audit_checkpoint(
        checkpoint_sink,
        source.media_id,
        {
            "schema_version": 1,
            "media_id": source.media_id,
            "source_document_sha256": sha256_bytes(
                canonical_json_bytes(source.document())
            ),
            "descriptor_sha256": sha256_bytes(
                canonical_json_bytes(descriptor.document())
            ),
            "descriptor": descriptor.document(),
            "method": descriptor.audit_method,
            "database": database,
            "source_content_sha256": source_digest,
            "source_identity_before": before,
            "source_identity_after": after,
            "isolation": isolation,
            "scope": "copied-source-cluster",
            "algorithm": "postgres-data-directory-tar-sha256-v1",
        },
    )
    return descriptor


def _derive_isolated_bind_descriptor(
    authority: MediaAuthorityRules,
    source: DiscoveredMedia,
    *,
    primary_database: str,
    runner: CommandRunner,
    operation: ScratchOperation,
    checkpoint_sink: AuditCheckpointSink | None = None,
) -> MediaDescriptor:
    major = source.postgres_major
    if (
        source.kind != "container_bind"
        or source.signature != "postgres"
        or major not in _audit_images_map(authority)
        or _active_media_attachments(source)
    ):
        raise MediaEvidenceError(
            "dynamic PostgreSQL bind inventory requires an inactive "
            "supported private bind"
        )
    source_admin_role = _retained_source_admin_role(runner, source)
    provisional_descriptor = MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database=primary_database,
        database_user=source_admin_role,
        disposition="retained-private-isolated",
        audit_method="isolated-bind-copy-read-only",
        classification="nexpoly-db",
        source_postgres_major=major,
        databases=(),
    )
    provisional_registry = Registry(
        payload=b"",
        digest=sha256_bytes(b"dynamic-bind-inventory"),
        audit_image=_audit_image_for_major(authority, POSTGRES_MAJOR),
        auditor_sha256=_auditor_digest(),
        descriptors=(provisional_descriptor,),
        required_online_databases=(),
        boundary={},
        authority_rules_sha256=authority.digest,
        audit_images=authority.audit_images,
        postgres_uid=authority.postgres_uid,
        postgres_gid=authority.postgres_gid,
        production_identity=dict(authority.production_identity),
    )
    workspace = operation.workspace / (
        "inventory-bind-"
        + hashlib.sha256(source.media_id.encode("utf-8")).hexdigest()[:24]
    )
    workspace.mkdir(mode=0o700)
    database, source_digest, before, after, isolation = (
        _isolated_bind_audit(
            runner,
            provisional_registry,
            provisional_descriptor,
            source,
            workspace,
            operation=operation,
            resource_prefix=workspace.name,
            derive_inventory=True,
        )
    )
    databases = _runtime_database_records(database.get("databases"))
    if not any(
        record["name"] == primary_database
        and record["allow_connections"] is True
        for record in databases
    ):
        raise MediaEvidenceError(
            "dynamic PostgreSQL bind lacks its logical primary database"
        )
    descriptor = MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database=primary_database,
        database_user=source_admin_role,
        disposition="retained-private-isolated",
        audit_method="isolated-bind-copy-read-only",
        classification="nexpoly-db",
        source_postgres_major=major,
        databases=databases,
    )
    _emit_audit_checkpoint(
        checkpoint_sink,
        source.media_id,
        {
            "schema_version": 1,
            "media_id": source.media_id,
            "source_document_sha256": sha256_bytes(
                canonical_json_bytes(source.document())
            ),
            "descriptor_sha256": sha256_bytes(
                canonical_json_bytes(descriptor.document())
            ),
            "descriptor": descriptor.document(),
            "method": descriptor.audit_method,
            "database": database,
            "source_content_sha256": source_digest,
            "source_identity_before": before,
            "source_identity_after": after,
            "isolation": isolation,
            "scope": "copied-source-cluster",
            "algorithm": "postgres-private-tree-sha256-v1",
        },
    )
    return descriptor


def _live_runtime_descriptor(
    authority: MediaAuthorityRules,
    source: DiscoveredMedia,
    *,
    primary_database: str,
    audit_role: str,
    disposition: str,
    runner: CommandRunner,
    audit_image_id: str,
) -> MediaDescriptor:
    active = _active_media_attachments(source)
    if (
        source.kind != "docker_volume"
        or source.signature != "postgres"
        or source.postgres_major != POSTGRES_MAJOR
        or len(active) != 1
    ):
        raise MediaEvidenceError(
            "logical online PostgreSQL medium lacks one exact PG16 reader"
        )
    container_id = str(active[0]["container_id"])
    online_admin_role = _online_container_admin_role(
        runner,
        container_id,
    )
    inventory = _container_database_inventory(
        runner,
        container_id,
        online_admin_role=online_admin_role,
        postgres_major=POSTGRES_MAJOR,
        use_trusted_client=True,
        trusted_image_id=audit_image_id,
    )
    not_connectable = [
        str(record["name"])
        for record in inventory
        if record["allow_connections"] is not True
    ]
    if not_connectable:
        raise MediaEvidenceError(
            "online PostgreSQL medium contains non-connectable databases "
            "that cannot receive a complete ledger audit: "
            f"{not_connectable!r}"
        )
    databases: list[dict[str, object]] = []
    for record in inventory:
        database_audit_role = (
            audit_role
            if record["name"] == primary_database
            else (
                audit_role[:44]
                + "_"
                + hashlib.sha256(
                    str(record["name"]).encode("utf-8")
                ).hexdigest()[:16]
            )
        )
        databases.append(
            {
                **record,
                "audit_role": database_audit_role,
                "migration_scope": (
                    "nexpoly-ledger"
                    if record["name"] == primary_database
                    else "adjacent-record-only"
                ),
            }
        )
    if not any(
        record["name"] == primary_database
        and record["allow_connections"] is True
        for record in databases
    ):
        raise MediaEvidenceError(
            "logical online medium lacks its primary database"
        )
    return MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database=primary_database,
        database_user=audit_role,
        disposition=disposition,
        audit_method="live-read-only",
        online_admin_role=online_admin_role,
        classification="nexpoly-db",
        source_postgres_major=POSTGRES_MAJOR,
        databases=tuple(databases),
    )


def _live_adjacent_runtime_descriptor(
    authority: MediaAuthorityRules,
    source: DiscoveredMedia,
    *,
    runner: CommandRunner,
    audit_image_id: str,
) -> MediaDescriptor:
    """Describe one active adjacent cluster without copying or starting it."""

    active = _active_media_attachments(source)
    major = source.postgres_major
    if (
        source.kind not in {"docker_volume", "container_bind"}
        or source.signature != "postgres"
        or major not in _audit_images_map(authority)
        or len(active) != 1
    ):
        raise MediaEvidenceError(
            "active adjacent PostgreSQL medium lacks one exact supported "
            "PGDATA reader"
        )
    container_id = str(active[0]["container_id"])
    online_admin_role = _online_container_admin_role(
        runner,
        container_id,
    )
    inventory = _container_database_inventory(
        runner,
        container_id,
        online_admin_role=online_admin_role,
        postgres_major=int(major),
        use_trusted_client=True,
        trusted_image_id=audit_image_id,
    )
    if (
        not inventory
        or any(
            record["allow_connections"] is not True
            for record in inventory
        )
    ):
        raise MediaEvidenceError(
            "active adjacent PostgreSQL contains a non-connectable database"
        )
    databases = tuple(
        {
            **record,
            "audit_role": online_admin_role,
            "migration_scope": "auto-detect-adjacent",
        }
        for record in inventory
    )
    primary = next(
        (
            str(record["name"])
            for record in inventory
            if record["name"] == "postgres"
        ),
        str(inventory[0]["name"]),
    )
    return MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database=primary,
        database_user=online_admin_role,
        disposition="excluded-from-nexpoly-migration",
        audit_method="live-read-only-adjacent",
        online_admin_role=online_admin_role,
        classification="adjacent-postgres",
        source_postgres_major=major,
        databases=databases,
    )


def _derive_isolated_backup_descriptor(
    authority: MediaAuthorityRules,
    source: DiscoveredMedia,
    *,
    runner: CommandRunner,
    operation: ScratchOperation,
    checkpoint_sink: AuditCheckpointSink | None = None,
) -> MediaDescriptor:
    if (
        source.kind != "postgres_backup"
        or source.signature != "postgres-backup"
        or source.postgres_major is not None
        or _active_media_attachments(source)
    ):
        raise MediaEvidenceError(
            "dynamic PostgreSQL backup inventory requires one fixed-root "
            "custom/tar backup"
        )
    descriptor = MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database="nexpoly",
        database_user="postgres",
        disposition="retained-private-isolated",
        audit_method="isolated-backup-restore-read-only",
        classification="nexpoly-db",
        source_postgres_major=None,
        databases=(),
    )
    registry = Registry(
        payload=b"",
        digest=sha256_bytes(b"dynamic-backup-inventory"),
        audit_image=_audit_image_for_major(authority, POSTGRES_MAJOR),
        auditor_sha256=_auditor_digest(),
        descriptors=(descriptor,),
        required_online_databases=(),
        boundary=seal_discovery_boundary(authority.policy),
        authority_rules_sha256=authority.digest,
        audit_images=authority.audit_images,
        postgres_uid=authority.postgres_uid,
        postgres_gid=authority.postgres_gid,
        production_identity=dict(authority.production_identity),
    )
    workspace = operation.workspace / (
        "inventory-backup-"
        + hashlib.sha256(source.media_id.encode("utf-8")).hexdigest()[:24]
    )
    workspace.mkdir(mode=0o700)
    database, source_digest, before, after, isolation = (
        _isolated_backup_audit(
            runner,
            registry,
            descriptor,
            source,
            workspace,
            policy=authority.policy,
            operation=operation,
            resource_prefix=workspace.name,
            derive_inventory=True,
        )
    )
    databases = tuple(
        record
        for record in _runtime_database_records(
            database.get("databases")
        )
        if record["name"] == "nexpoly"
    )
    if len(databases) != 1:
        raise MediaEvidenceError(
            "isolated backup audit did not produce one exact Nexpoly database"
        )
    descriptor = MediaDescriptor(
        media_id=source.media_id,
        kind=source.kind,
        database="nexpoly",
        database_user="postgres",
        disposition="retained-private-isolated",
        audit_method="isolated-backup-restore-read-only",
        classification="nexpoly-db",
        source_postgres_major=None,
        databases=databases,
    )
    _emit_audit_checkpoint(
        checkpoint_sink,
        source.media_id,
        {
            "schema_version": 1,
            "media_id": source.media_id,
            "source_document_sha256": sha256_bytes(
                canonical_json_bytes(source.document())
            ),
            "descriptor_sha256": sha256_bytes(
                canonical_json_bytes(descriptor.document())
            ),
            "descriptor": descriptor.document(),
            "method": descriptor.audit_method,
            "database": database,
            "source_content_sha256": source_digest,
            "source_identity_before": before,
            "source_identity_after": after,
            "isolation": isolation,
            "scope": "isolated-restore-cluster",
            "algorithm": "sha256-file-v1",
        },
    )
    return descriptor


def _durable_checkpoint_authority(
    registry: Registry,
) -> dict[str, object]:
    images = _audit_images_map(registry)
    image_ids = dict(registry.audit_image_ids)
    if (
        set(image_ids) != set(images)
        or any(
            not isinstance(image_ids[major], str)
            or DIGEST_RE.fullmatch(image_ids[major]) is None
            for major in images
        )
    ):
        raise MediaEvidenceError(
            "durable checkpoint authority lacks exact local audit images"
        )
    return {
        "schema_version": 1,
        "media_authority_rules_sha256": (
            registry.authority_rules_sha256
        ),
        "auditor_sha256": registry.auditor_sha256,
        "discovery_boundary_sha256": sha256_bytes(
            canonical_json_bytes(registry.boundary)
        ),
        "audit_images": {
            str(major): {
                "digest_ref": images[major],
                "image_id": image_ids[major],
            }
            for major in sorted(images)
        },
    }


def _seal_durable_checkpoint(
    registry: Registry,
    source: DiscoveredMedia,
    checkpoint: Mapping[str, object],
) -> dict[str, object]:
    descriptor = checkpoint.get("descriptor")
    source_document = source.document()
    if (
        checkpoint.get("schema_version") != 1
        or checkpoint.get("media_id") != source.media_id
        or not isinstance(descriptor, dict)
        or checkpoint.get("source_document_sha256")
        != sha256_bytes(canonical_json_bytes(source_document))
        or checkpoint.get("descriptor_sha256")
        != sha256_bytes(canonical_json_bytes(descriptor))
    ):
        raise MediaEvidenceError(
            "isolated audit produced an invalid durable checkpoint"
        )
    unsealed = {
        "schema_version": DURABLE_CHECKPOINT_SCHEMA_VERSION,
        "media_id": source.media_id,
        "kind": source.kind,
        "source_document": source_document,
        "source_document_sha256": checkpoint[
            "source_document_sha256"
        ],
        "descriptor": descriptor,
        "descriptor_sha256": checkpoint["descriptor_sha256"],
        "method": checkpoint.get("method"),
        "database": checkpoint.get("database"),
        "source_content_sha256": checkpoint.get(
            "source_content_sha256"
        ),
        "source_identity_before": checkpoint.get(
            "source_identity_before"
        ),
        "source_identity_after": checkpoint.get(
            "source_identity_after"
        ),
        "isolation": checkpoint.get("isolation"),
        "scope": checkpoint.get("scope"),
        "algorithm": checkpoint.get("algorithm"),
        "authority": _durable_checkpoint_authority(registry),
    }
    return {
        **unsealed,
        "checkpoint_sha256": sha256_bytes(
            canonical_json_bytes(unsealed)
        ),
    }


def _validate_durable_checkpoint_directory(
    evidence_root: Path,
) -> Path:
    root = _open_private_child_directory(
        evidence_root,
        DURABLE_CHECKPOINT_ROOT_NAME,
    )
    descriptor = _open_directory_chain(
        root,
        private_from=root,
    )
    changed = False
    try:
        for name in os.listdir(descriptor):
            metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise MediaEvidenceError(
                    f"durable audit checkpoint entry is unsafe: {name}"
                )
            if DURABLE_CHECKPOINT_TEMP_RE.fullmatch(name) is not None:
                os.unlink(name, dir_fd=descriptor)
                changed = True
            elif DURABLE_CHECKPOINT_NAME_RE.fullmatch(name) is None:
                raise MediaEvidenceError(
                    f"durable audit checkpoint entry is unknown: {name}"
                )
        if changed:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return root


def _read_durable_checkpoint(
    evidence_root: Path,
    media_id: str,
) -> dict[str, object] | None:
    path = _durable_checkpoint_path(evidence_root, media_id)

    def open_once() -> int:
        return open_private_regular(
            path,
            root=_durable_checkpoint_root(evidence_root),
        )

    try:
        descriptor = open_once()
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        payload = _read_fd(
            descriptor,
            MAX_DURABLE_CHECKPOINT_BYTES,
        )
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    reopened = open_once()
    try:
        reopened_metadata = os.fstat(reopened)
    finally:
        os.close(reopened)

    def stable(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_nlink,
        )

    if stable(before) != stable(after) or stable(after) != stable(
        reopened_metadata
    ):
        raise MediaEvidenceError(
            "durable audit checkpoint changed while being read"
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError(
            "durable audit checkpoint is not JSON"
        ) from exc
    if (
        not isinstance(value, dict)
        or canonical_json_bytes(value) + b"\n" != payload
    ):
        raise MediaEvidenceError(
            "durable audit checkpoint is not canonical JSON"
        )
    return value


def _checkpoint_descriptor(
    value: Mapping[str, object],
) -> MediaDescriptor:
    raw = value.get("descriptor")
    fields = {
        "media_id",
        "kind",
        "database",
        "database_user",
        "disposition",
        "audit_method",
        "online_admin_role",
        "classification",
        "source_postgres_major",
        "databases",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != fields
        or not isinstance(raw.get("databases"), list)
        or any(not isinstance(record, dict) for record in raw["databases"])
    ):
        raise MediaEvidenceError(
            "durable audit checkpoint descriptor is malformed"
        )
    descriptor = MediaDescriptor(
        media_id=str(raw["media_id"]),
        kind=str(raw["kind"]),
        database=str(raw["database"]),
        database_user=str(raw["database_user"]),
        disposition=str(raw["disposition"]),
        audit_method=str(raw["audit_method"]),
        online_admin_role=(
            str(raw["online_admin_role"])
            if raw["online_admin_role"] is not None
            else None
        ),
        classification=str(raw["classification"]),
        source_postgres_major=(
            int(raw["source_postgres_major"])
            if raw["source_postgres_major"] is not None
            and not isinstance(raw["source_postgres_major"], bool)
            else None
        ),
        databases=tuple(dict(record) for record in raw["databases"]),
    )
    if descriptor.document() != raw:
        raise MediaEvidenceError(
            "durable audit checkpoint descriptor is not canonical"
        )
    return descriptor


def _validate_durable_checkpoint(
    value: object,
    *,
    registry: Registry,
    source: DiscoveredMedia,
    expected_descriptor: MediaDescriptor,
    allow_descriptor_inventory: bool,
) -> tuple[MediaDescriptor, dict[str, object]]:
    fields = {
        "schema_version",
        "media_id",
        "kind",
        "source_document",
        "source_document_sha256",
        "descriptor",
        "descriptor_sha256",
        "method",
        "database",
        "source_content_sha256",
        "source_identity_before",
        "source_identity_after",
        "isolation",
        "scope",
        "algorithm",
        "authority",
        "checkpoint_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise MediaEvidenceError(
            "durable audit checkpoint has an invalid shape"
        )
    unsealed = {
        key: item
        for key, item in value.items()
        if key != "checkpoint_sha256"
    }
    source_document = source.document()
    if (
        value.get("schema_version")
        != DURABLE_CHECKPOINT_SCHEMA_VERSION
        or value.get("media_id") != source.media_id
        or value.get("kind") != source.kind
        or value.get("source_document") != source_document
        or value.get("source_document_sha256")
        != sha256_bytes(canonical_json_bytes(source_document))
        or value.get("authority")
        != _durable_checkpoint_authority(registry)
        or value.get("checkpoint_sha256")
        != sha256_bytes(canonical_json_bytes(unsealed))
    ):
        raise MediaEvidenceError(
            "durable audit checkpoint authority or source differs"
        )
    descriptor = _checkpoint_descriptor(value)
    if (
        value.get("descriptor_sha256")
        != sha256_bytes(
            canonical_json_bytes(descriptor.document())
        )
        or value.get("method") != descriptor.audit_method
        or (
            descriptor != expected_descriptor
            if not allow_descriptor_inventory
            else replace(descriptor, databases=())
            != replace(expected_descriptor, databases=())
        )
    ):
        raise MediaEvidenceError(
            "durable audit checkpoint descriptor differs"
        )
    expected_methods = {
        "isolated-volume-copy-read-only": (
            "docker_volume",
            "copied-source-cluster",
            "postgres-data-directory-tar-sha256-v1",
        ),
        "isolated-bind-copy-read-only": (
            "container_bind",
            "copied-source-cluster",
            "postgres-private-tree-sha256-v1",
        ),
        "isolated-backup-restore-read-only": (
            "postgres_backup",
            "isolated-restore-cluster",
            "sha256-file-v1",
        ),
    }
    expected = expected_methods.get(descriptor.audit_method)
    if (
        expected is None
        or expected
        != (source.kind, value.get("scope"), value.get("algorithm"))
        or not isinstance(value.get("database"), dict)
        or not isinstance(value.get("source_identity_before"), dict)
        or value.get("source_identity_before")
        != value.get("source_identity_after")
        or not isinstance(value.get("source_content_sha256"), str)
        or DIGEST_RE.fullmatch(value["source_content_sha256"]) is None
        or not isinstance(value.get("isolation"), dict)
    ):
        raise MediaEvidenceError(
            "durable audit checkpoint result is malformed"
        )
    database_records = _runtime_database_records(
        value["database"].get("databases")  # type: ignore[union-attr]
    )
    if descriptor.kind == "postgres_backup":
        database_records = tuple(
            record
            for record in database_records
            if record["name"] == descriptor.database
        )
    if descriptor.databases != database_records:
        raise MediaEvidenceError(
            "durable audit checkpoint database inventory differs"
        )
    return descriptor, dict(value)


def _publish_durable_checkpoint(
    evidence_root: Path,
    registry: Registry,
    source: DiscoveredMedia,
    checkpoint: Mapping[str, object],
) -> dict[str, object]:
    sealed = _seal_durable_checkpoint(
        registry,
        source,
        checkpoint,
    )
    payload = canonical_json_bytes(sealed) + b"\n"
    if len(payload) > MAX_DURABLE_CHECKPOINT_BYTES:
        raise MediaEvidenceError(
            "durable audit checkpoint exceeds its size limit"
        )
    path = _durable_checkpoint_path(evidence_root, source.media_id)
    _replace_private_atomic(
        path.parent,
        path.name,
        payload,
    )
    loaded = _read_durable_checkpoint(evidence_root, source.media_id)
    if loaded != sealed:
        raise MediaEvidenceError(
            "published durable audit checkpoint differs from its stage"
        )
    return sealed


def _revalidate_durable_checkpoint_source(
    checkpoint: Mapping[str, object],
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    *,
    registry: Registry,
    runner: CommandRunner,
    operation: ScratchOperation,
    policy: DiscoveryPolicy,
) -> None:
    expected_identity = checkpoint["source_identity_after"]
    expected_digest = checkpoint["source_content_sha256"]
    if source.kind == "docker_volume":
        before = _docker_volume_identity(runner, source)
        if any(
            str(value["state"]) in ACTIVE_CONTAINER_STATES
            for value in before["attached"]  # type: ignore[index]
        ):
            raise MediaEvidenceError(
                "durable checkpoint source gained an active reader"
            )
        digest = _volume_content_digest(
            runner,
            _audit_image_for_major(
                registry,
                descriptor.source_postgres_major or POSTGRES_MAJOR,
            ),
            source,
            operation=operation,
            resource_key=(
                "checkpoint-cas-"
                + hashlib.sha256(
                    source.media_id.encode("utf-8")
                ).hexdigest()[:24]
            ),
        )
        after = _docker_volume_identity(runner, source)
        observed_identity = after
        if before != after:
            raise MediaEvidenceError(
                "durable checkpoint volume changed during content CAS"
            )
    elif source.kind == "container_bind":
        attachments = _current_attachments(runner, source)
        if any(
            str(value["state"]) in ACTIVE_CONTAINER_STATES
            for value in attachments
        ):
            raise MediaEvidenceError(
                "durable checkpoint bind gained an active reader"
            )
        with tempfile.TemporaryDirectory(
            prefix="checkpoint-bind-cas-",
            dir=operation.workspace,
        ) as temporary:
            os.chmod(temporary, 0o700)
            tree, digest = _bind_tree_snapshot(
                Path(source.locator),
                Path(temporary) / "snapshot",
            )
        observed_identity = {
            **tree,
            "data_subpath": source.data_subpath,
            "attached": attachments,
        }
    elif source.kind == "postgres_backup":
        path = Path(source.locator)
        root = _find_backup_root(path, policy)
        root_authority = _sealed_root_authority(registry, root)
        first = _reviewed_regular_identity(
            path,
            root=root,
            root_authority=root_authority,
            maximum_bytes=int(
                LOGICAL_MEDIA_POLICY["non_postgres"][  # type: ignore[index]
                    "maximum_bytes"
                ]
            ),
        )
        second = _reviewed_regular_identity(
            path,
            root=root,
            root_authority=root_authority,
            maximum_bytes=int(
                LOGICAL_MEDIA_POLICY["non_postgres"][  # type: ignore[index]
                    "maximum_bytes"
                ]
            ),
        )
        if first != second:
            raise MediaEvidenceError(
                "durable checkpoint backup changed during content CAS"
            )
        digest = first["sha256"]
        observed_identity = {
            **first,
            "format": source.backup_format,
        }
    else:
        raise MediaEvidenceError(
            "durable checkpoint source kind is unsupported"
        )
    if (
        observed_identity != expected_identity
        or digest != expected_digest
    ):
        raise MediaEvidenceError(
            "durable checkpoint source content differs"
        )


def _load_durable_checkpoint(
    evidence_root: Path,
    *,
    registry: Registry,
    source: DiscoveredMedia,
    expected_descriptor: MediaDescriptor,
    allow_descriptor_inventory: bool,
    runner: CommandRunner,
    operation: ScratchOperation,
    policy: DiscoveryPolicy,
    revalidate_source: bool,
) -> tuple[MediaDescriptor, dict[str, object]] | None:
    raw = _read_durable_checkpoint(evidence_root, source.media_id)
    if raw is None:
        return None
    descriptor, checkpoint = _validate_durable_checkpoint(
        raw,
        registry=registry,
        source=source,
        expected_descriptor=expected_descriptor,
        allow_descriptor_inventory=allow_descriptor_inventory,
    )
    if revalidate_source:
        _revalidate_durable_checkpoint_source(
            checkpoint,
            descriptor,
            source,
            registry=registry,
            runner=runner,
            operation=operation,
            policy=policy,
        )
    return descriptor, checkpoint


def _validate_required_takeover_backup(
    boundary: Mapping[str, object],
    discovery: Discovery,
    registry: Registry,
) -> None:
    takeover_stage = boundary.get("takeover_backup_stage")
    if not isinstance(takeover_stage, dict):
        return
    required_media_id = takeover_stage.get("required_media_id")
    required_source = (
        discovery.media.get(required_media_id)
        if isinstance(required_media_id, str)
        else None
    )
    required_path = (
        APPROVED_BACKUP_ROOTS[0] / REQUIRED_TAKEOVER_BACKUP_NAME
    )
    if (
        required_source is None
        or required_source.kind != "postgres_backup"
        or required_source.signature != "postgres-backup"
        or required_source.locator != str(required_path)
        or required_source.backup_format != "postgres-custom-v1"
    ):
        raise MediaEvidenceError(
            "completed takeover required backup is absent"
        )
    required_identity = _reviewed_regular_identity(
        required_path,
        root=APPROVED_BACKUP_ROOTS[0],
        root_authority=_sealed_root_authority(
            registry,
            APPROVED_BACKUP_ROOTS[0],
        ),
        maximum_bytes=int(
            LOGICAL_MEDIA_POLICY["non_postgres"]["maximum_bytes"]  # type: ignore[index]
        ),
    )
    if (
        required_identity.get("sha256")
        != takeover_stage.get("required_backup_sha256")
    ):
        raise MediaEvidenceError(
            "completed takeover required backup differs from its seal"
        )


def generate_runtime_registry(
    authority: MediaAuthorityRules,
    *,
    registry_path: Path,
    runner: CommandRunner,
    operation: ScratchOperation,
    reviewed_content_root: Path = DEFAULT_REVIEWED_CONTENT_ROOT,
    evidence_root: Path | None = None,
) -> tuple[Registry, Discovery]:
    """Derive one complete host registry from immutable authority rules."""

    checkpoint_evidence_root = evidence_root or registry_path.parent
    _validate_durable_checkpoint_directory(checkpoint_evidence_root)
    raw_operation_images = operation.authority.get("postgres_images")
    audit_image_ids: dict[int, str] = {}
    if not isinstance(raw_operation_images, dict):
        raise MediaEvidenceError(
            "registry operation lacks local image identities"
        )
    for major, image in _audit_images_map(authority).items():
        record = raw_operation_images.get(str(major))
        if (
            not isinstance(record, dict)
            or record.get("digest_ref") != image
            or not isinstance(record.get("image_id"), str)
            or DIGEST_RE.fullmatch(record["image_id"]) is None
        ):
            raise MediaEvidenceError(
                "registry operation local image identity differs"
            )
        audit_image_ids[major] = str(record["image_id"])
    boundary = seal_discovery_boundary(authority.policy)
    provisional = Registry(
        payload=b"",
        digest=sha256_bytes(b"runtime-registry-provisional"),
        audit_image=_audit_image_for_major(authority, POSTGRES_MAJOR),
        auditor_sha256=_auditor_digest(),
        authority_rules_sha256=authority.digest,
        descriptors=(),
        required_online_databases=(),
        boundary=boundary,
        audit_images=authority.audit_images,
        audit_image_ids=tuple(sorted(audit_image_ids.items())),
        postgres_uid=authority.postgres_uid,
        postgres_gid=authority.postgres_gid,
        production_identity=dict(authority.production_identity),
    )
    discovery = discover_media(
        provisional,
        runner=runner,
        operation=operation,
        policy=authority.policy,
        enforce_registry=False,
    )
    _validate_required_takeover_backup(
        boundary,
        discovery,
        provisional,
    )
    audit_checkpoints: dict[str, dict[str, object]] = {}

    def persist_checkpoint(
        media_id: str,
        checkpoint: dict[str, object],
    ) -> None:
        source = discovery.media.get(media_id)
        if source is None:
            raise MediaEvidenceError(
                "isolated audit checkpoint source disappeared"
            )
        audit_checkpoints[media_id] = _publish_durable_checkpoint(
            checkpoint_evidence_root,
            provisional,
            source,
            checkpoint,
        )

    def resume_checkpoint(
        source: DiscoveredMedia,
        expected: MediaDescriptor,
    ) -> MediaDescriptor | None:
        resumed = _load_durable_checkpoint(
            checkpoint_evidence_root,
            registry=provisional,
            source=source,
            expected_descriptor=expected,
            allow_descriptor_inventory=True,
            runner=runner,
            operation=operation,
            policy=authority.policy,
            revalidate_source=True,
        )
        if resumed is None:
            return None
        descriptor, checkpoint = resumed
        audit_checkpoints[source.media_id] = checkpoint
        return descriptor

    production_media_id = str(authority.production_identity["media_id"])
    production_source = discovery.media.get(production_media_id)
    if production_source is None:
        raise MediaEvidenceError(
            "exact production PostgreSQL medium is absent"
        )
    if (
        _live_source_system_identifier(
            runner,
            production_source,
            trusted_image_id=audit_image_ids[POSTGRES_MAJOR],
        )
        != authority.production_identity["system_identifier"]
    ):
        raise MediaEvidenceError(
            "production PostgreSQL system identifier differs from authority"
        )

    logical = authority.logical_media or LOGICAL_MEDIA_POLICY
    raw_named_stacks = logical.get("named_stacks")
    if not isinstance(raw_named_stacks, list):
        raise MediaEvidenceError("logical media stack rules are invalid")
    named_sources: dict[str, tuple[dict[str, object], DiscoveredMedia]] = {}
    for raw_rule in raw_named_stacks:
        if not isinstance(raw_rule, dict):
            raise MediaEvidenceError("logical media stack rule is invalid")
        pattern = raw_rule.get("volume_name_pattern")
        stack = raw_rule.get("stack")
        if not isinstance(pattern, str) or not isinstance(stack, str):
            raise MediaEvidenceError("logical media selector is invalid")
        matches = [
            source
            for source in discovery.media.values()
            if source.kind == "docker_volume"
            and re.fullmatch(pattern, source.locator) is not None
        ]
        if len(matches) != 1:
            raise MediaEvidenceError(
                f"logical stack {stack} must resolve to one exact medium"
            )
        named_sources[matches[0].media_id] = (raw_rule, matches[0])

    descriptors: list[MediaDescriptor] = []
    required_online: list[dict[str, str]] = []
    reviewed_content: list[dict[str, object]] = []
    for media_id in sorted(discovery.media):
        source = discovery.media[media_id]
        if media_id == production_media_id:
            descriptors.append(
                _live_runtime_descriptor(
                    authority,
                    source,
                    primary_database="nexpoly",
                    audit_role="nexpoly_production_auditor",
                    disposition="writable-target",
                    runner=runner,
                    audit_image_id=audit_image_ids[POSTGRES_MAJOR],
                )
            )
            continue
        named = named_sources.get(media_id)
        if source.signature == "postgres":
            active = _active_media_attachments(source)
            if named is not None:
                rule, _named_source = named
                primary_database = str(rule["database"])
                if active:
                    descriptor = _live_runtime_descriptor(
                        authority,
                        source,
                        primary_database=primary_database,
                        audit_role=str(rule["audit_role"]),
                        disposition="read-only-online",
                        runner=runner,
                        audit_image_id=audit_image_ids[POSTGRES_MAJOR],
                    )
                    required_online.append(
                        {
                            "stack": str(rule["stack"]),
                            "media_id": media_id,
                        }
                    )
                else:
                    retained_admin = _retained_source_admin_role(
                        runner,
                        source,
                    )
                    template = MediaDescriptor(
                        media_id=source.media_id,
                        kind="docker_volume",
                        database=primary_database,
                        database_user=retained_admin,
                        disposition="retained-private-isolated",
                        audit_method="isolated-volume-copy-read-only",
                        classification="nexpoly-db",
                        source_postgres_major=source.postgres_major,
                        databases=(),
                    )
                    descriptor = resume_checkpoint(source, template)
                    if descriptor is None:
                        descriptor = _derive_isolated_volume_descriptor(
                            authority,
                            source,
                            primary_database=primary_database,
                            runner=runner,
                            operation=operation,
                            checkpoint_sink=persist_checkpoint,
                        )
            elif active:
                descriptor = _live_adjacent_runtime_descriptor(
                    authority,
                    source,
                    runner=runner,
                    audit_image_id=audit_image_ids[
                        int(source.postgres_major)
                    ],
                )
            elif source.kind == "docker_volume":
                retained_admin = _retained_source_admin_role(
                    runner,
                    source,
                )
                template = MediaDescriptor(
                    media_id=source.media_id,
                    kind="docker_volume",
                    database="postgres",
                    database_user=retained_admin,
                    disposition="retained-private-isolated",
                    audit_method="isolated-volume-copy-read-only",
                    classification="nexpoly-db",
                    source_postgres_major=source.postgres_major,
                    databases=(),
                )
                descriptor = resume_checkpoint(source, template)
                if descriptor is None:
                    descriptor = _derive_isolated_volume_descriptor(
                        authority,
                        source,
                        primary_database="postgres",
                        runner=runner,
                        operation=operation,
                        checkpoint_sink=persist_checkpoint,
                    )
            elif source.kind == "container_bind":
                retained_admin = _retained_source_admin_role(
                    runner,
                    source,
                )
                template = MediaDescriptor(
                    media_id=source.media_id,
                    kind="container_bind",
                    database="postgres",
                    database_user=retained_admin,
                    disposition="retained-private-isolated",
                    audit_method="isolated-bind-copy-read-only",
                    classification="nexpoly-db",
                    source_postgres_major=source.postgres_major,
                    databases=(),
                )
                descriptor = resume_checkpoint(source, template)
                if descriptor is None:
                    descriptor = _derive_isolated_bind_descriptor(
                        authority,
                        source,
                        primary_database="postgres",
                        runner=runner,
                        operation=operation,
                        checkpoint_sink=persist_checkpoint,
                    )
            else:
                raise MediaEvidenceError(
                    "unsupported physical PostgreSQL medium kind"
                )
            descriptors.append(descriptor)
            continue
        if source.signature == "postgres-backup":
            template = MediaDescriptor(
                media_id=source.media_id,
                kind="postgres_backup",
                database="nexpoly",
                database_user="postgres",
                disposition="retained-private-isolated",
                audit_method="isolated-backup-restore-read-only",
                classification="nexpoly-db",
                source_postgres_major=None,
                databases=(),
            )
            descriptor = resume_checkpoint(source, template)
            if descriptor is None:
                descriptor = _derive_isolated_backup_descriptor(
                    authority,
                    source,
                    runner=runner,
                    operation=operation,
                    checkpoint_sink=persist_checkpoint,
                )
            descriptors.append(descriptor)
            continue
        if source.signature != "non-postgres":
            raise MediaEvidenceError(
                f"unsupported external medium signature: {media_id}"
            )
        if named is not None:
            raise MediaEvidenceError(
                "logical PostgreSQL stack resolved to non-PostgreSQL content"
            )
        active_non_pg = bool(_active_media_attachments(source))
        if source.kind == "docker_volume" and not active_non_pg:
            review = _review_non_postgres_volume(
                provisional,
                source,
                runner=runner,
                operation=operation,
                resource_prefix=(
                    "registry-review-volume-"
                    + hashlib.sha256(media_id.encode("utf-8")).hexdigest()[:24]
                ),
            )
        elif source.kind == "container_bind" and not active_non_pg:
            review = _review_non_postgres_bind(
                provisional,
                source,
                runner=runner,
                operation=operation,
                resource_prefix=(
                    "registry-review-bind-"
                    + hashlib.sha256(media_id.encode("utf-8")).hexdigest()[:24]
                ),
            )
        elif source.kind in {"docker_volume", "container_bind"}:
            identity = _excluded_non_pg_identity(source)
            review = {
                "media_id": source.media_id,
                "metadata_identity": identity,
                "metadata_identity_sha256": sha256_bytes(
                    canonical_json_bytes(identity)
                ),
                "review_algorithm": "metadata-only-exclusion-v1",
            }
        elif source.kind == "reviewed_file":
            review = _review_non_postgres_file(
                provisional,
                source,
                policy=authority.policy,
            )
        else:
            raise MediaEvidenceError(
                "non-PostgreSQL medium requires separate private review"
            )
        reviewed_content.append(review)
        metadata_only = (
            source.kind in {"docker_volume", "container_bind"}
            and active_non_pg
        )
        descriptors.append(
            MediaDescriptor(
                media_id=media_id,
                kind=source.kind,
                database="none",
                database_user="none",
                disposition="excluded-from-nexpoly-migration",
                audit_method=(
                    "metadata-only-exclusion"
                    if metadata_only
                    else "reviewed-content-only"
                ),
                classification=(
                    "excluded-non-pg"
                    if metadata_only
                    else "reviewed-non-pg"
                ),
                source_postgres_major=None,
                databases=(),
            )
        )
    maximum_reviewed_files = int(
        logical["non_postgres"]["maximum_files"]  # type: ignore[index]
    )
    maximum_reviewed_bytes = int(
        logical["non_postgres"]["maximum_bytes"]  # type: ignore[index]
    )
    if (
        sum(
            int(record.get("file_count", 0))
            for record in reviewed_content
        )
        > maximum_reviewed_files
        or sum(
            int(record.get("size_bytes", 0))
            for record in reviewed_content
        )
        > maximum_reviewed_bytes
    ):
        raise MediaEvidenceError(
            "reviewed non-PostgreSQL content exceeds its aggregate limit"
        )
    if set(named_sources) - {
        descriptor.media_id for descriptor in descriptors
    }:
        raise MediaEvidenceError(
            "logical stack selector was not classified"
        )
    discovery = replace(
        discovery,
        audit_checkpoints=dict(audit_checkpoints),
    )
    reviewed_document = {
        "schema_version": 1,
        "media_authority_rules_sha256": authority.digest,
        "discovery_state_sha256": _discovery_state_sha256(discovery),
        "media": reviewed_content,
    }
    reviewed_payload = canonical_json_bytes(reviewed_document) + b"\n"
    reviewed_digest = sha256_bytes(reviewed_payload)

    reviewed_content_path = _write_private_atomic(
        reviewed_content_root,
        reviewed_digest.removeprefix("sha256:") + ".json",
        reviewed_payload,
    )
    if _private_file_digest(reviewed_content_path) != reviewed_digest:
        raise MediaEvidenceError(
            "published reviewed-content inventory differs from its stage"
        )
    payload = (
        canonical_json_bytes(
            _runtime_registry_document(
                authority,
                boundary=boundary,
                audit_image_ids=audit_image_ids,
                descriptors=descriptors,
                required_online_databases=required_online,
                reviewed_content_inventory_sha256=reviewed_digest,
            )
        )
        + b"\n"
    )
    staged_registry: Registry | None = None
    staged_discovery: Discovery | None = None

    def validate_staged(path: Path) -> None:
        nonlocal staged_registry, staged_discovery
        candidate = load_registry(
            path,
            policy=authority.policy,
            private_root=registry_path.parent,
        )
        if (
            not isinstance(candidate, Registry)
            or candidate.authority_rules_sha256 != authority.digest
            or candidate.auditor_sha256 != _auditor_digest()
            or candidate.reviewed_content_inventory_sha256
            != reviewed_digest
            or candidate.payload != payload
            or _private_file_digest(reviewed_content_path)
            != reviewed_digest
        ):
            raise MediaEvidenceError(
                "staged runtime media registry differs from authority"
            )
        _revalidate_docker_epoch(runner, discovery)
        _revalidate_backup_epoch(candidate, discovery, authority.policy)
        _revalidate_live_registry_epoch(
            runner,
            descriptors,
            discovery,
            audit_image_ids=audit_image_ids,
        )
        staged_registry = candidate
        staged_discovery = discovery

    _replace_private_atomic(
        registry_path.parent,
        registry_path.name,
        payload,
        validate_staged=validate_staged,
    )
    published = load_registry(
        registry_path,
        policy=authority.policy,
        private_root=registry_path.parent,
    )
    if (
        staged_registry is None
        or staged_discovery is None
        or not isinstance(published, Registry)
        or published.authority_rules_sha256 != authority.digest
        or published.auditor_sha256 != _auditor_digest()
        or published.reviewed_content_inventory_sha256 != reviewed_digest
        or _private_file_digest(reviewed_content_path)
        != reviewed_digest
        or published.payload != payload
        or published.digest != staged_registry.digest
    ):
        raise MediaEvidenceError(
            "published runtime media registry differs from its validated stage"
        )
    return published, staged_discovery


def load_runtime_registry_for_revalidation(
    authority: MediaAuthorityRules,
    *,
    registry_path: Path,
    evidence_root: Path,
    runner: CommandRunner,
    operation: ScratchOperation,
) -> tuple[Registry, Discovery]:
    """Load one sealed registry and attach reusable offline audit results.

    Discovery and online database inventory are always refreshed.  Offline
    checkpoints are only attached after their full authority/source
    descriptors validate; ``build_evidence`` performs the final content CAS
    before it publishes any fresh evidence.
    """

    _validate_durable_checkpoint_directory(evidence_root)
    loaded = load_registry(
        registry_path,
        policy=authority.policy,
        private_root=registry_path.parent,
    )
    if not isinstance(loaded, Registry):
        raise MediaEvidenceError(
            "runtime media registry is unavailable for revalidation"
        )
    operation_images = operation.authority.get("postgres_images")
    expected_operation_images = {
        str(major): {
            "digest_ref": image,
            "image_id": dict(loaded.audit_image_ids).get(major),
        }
        for major, image in _audit_images_map(loaded).items()
    }
    if (
        loaded.authority_rules_sha256 != authority.digest
        or loaded.auditor_sha256 != authority.auditor_sha256
        or loaded.boundary != seal_discovery_boundary(authority.policy)
        or loaded.production_identity != authority.production_identity
        or loaded.audit_images != authority.audit_images
        or operation_images != expected_operation_images
    ):
        raise MediaEvidenceError(
            "runtime media registry differs from revalidation authority"
        )
    discovery = discover_media(
        loaded,
        runner=runner,
        operation=operation,
        policy=authority.policy,
        enforce_registry=True,
    )
    _validate_required_takeover_backup(
        loaded.boundary,
        discovery,
        loaded,
    )
    checkpoints: dict[str, dict[str, object]] = {}
    isolated_methods = {
        "isolated-volume-copy-read-only",
        "isolated-bind-copy-read-only",
        "isolated-backup-restore-read-only",
    }
    for descriptor in loaded.descriptors:
        if descriptor.audit_method not in isolated_methods:
            continue
        source = discovery.media.get(descriptor.media_id)
        if source is None:
            raise MediaEvidenceError(
                "durable checkpoint source is absent from discovery"
            )
        resumed = _load_durable_checkpoint(
            evidence_root,
            registry=loaded,
            source=source,
            expected_descriptor=descriptor,
            allow_descriptor_inventory=False,
            runner=runner,
            operation=operation,
            policy=authority.policy,
            revalidate_source=False,
        )
        if resumed is None:
            raise MediaEvidenceError(
                "runtime registry lacks a durable offline audit checkpoint"
            )
        _checkpoint_descriptor_value, checkpoint = resumed
        checkpoints[descriptor.media_id] = checkpoint
    _revalidate_docker_epoch(runner, discovery)
    _revalidate_backup_epoch(loaded, discovery, authority.policy)
    _revalidate_live_registry_epoch(
        runner,
        loaded.descriptors,
        discovery,
        audit_image_ids=dict(loaded.audit_image_ids),
    )
    return loaded, replace(
        discovery,
        audit_checkpoints=checkpoints,
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
__NEXPOLY_EVENT_TRIGGERS_SESSION_ASSERT__
SELECT current_setting('data_directory') AS nexpoly_audited_data_directory \gset
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
SET LOCAL default_transaction_read_only = on;
SET LOCAL statement_timeout = '5min';
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = pg_catalog;
SET LOCAL row_security = on;
SET LOCAL jit = off;
SELECT jsonb_build_object(
  'record_type', 'database',
  'database', current_database(),
  'current_user', current_user,
  'transaction_read_only', current_setting('transaction_read_only')::boolean,
  'statement_timeout', current_setting('statement_timeout'),
  'lock_timeout', current_setting('lock_timeout'),
  'search_path', current_setting('search_path'),
  'row_security', current_setting('row_security')::boolean,
__NEXPOLY_STARTUP_SETTINGS_JSON__
  'event_triggers_disabled', __NEXPOLY_EVENT_TRIGGERS_DISABLED_JSON__,
  'role_superuser', role.rolsuper,
  'role_create_db', role.rolcreatedb,
  'role_create_role', role.rolcreaterole,
  'role_replication', role.rolreplication,
  'role_bypass_rls', role.rolbypassrls,
  'role_inherit', role.rolinherit,
  'role_can_login', role.rolcanlogin,
  'role_contract_marker',
    shobj_description(role.oid, 'pg_authid')
) || jsonb_build_object(
  'event_triggers', COALESCE((
    SELECT json_agg(
      json_build_object(
        'name', event_trigger.evtname,
        'event', event_trigger.evtevent,
        'enabled', event_trigger.evtenabled,
        'function', event_trigger.evtfoid::regprocedure::text,
        'tags', to_json(COALESCE(event_trigger.evttags, ARRAY[]::text[]))
      )
      ORDER BY event_trigger.evtname COLLATE "C"
    )
    FROM pg_event_trigger AS event_trigger
  ), '[]'::json),
  'role_memberships', CASE WHEN role.rolsuper THEN '[]'::json ELSE COALESCE((
    SELECT json_agg(granted.rolname ORDER BY granted.rolname COLLATE "C")
    FROM pg_auth_members AS membership
    JOIN pg_roles AS granted ON granted.oid = membership.roleid
    WHERE membership.member = role.oid
  ), '[]'::json) END,
  'role_incoming_memberships', CASE WHEN role.rolsuper THEN '[]'::json ELSE COALESCE((
    SELECT json_agg(member_role.rolname ORDER BY member_role.rolname COLLATE "C")
    FROM pg_auth_members AS membership
    JOIN pg_roles AS member_role ON member_role.oid = membership.member
    WHERE membership.roleid = role.oid
  ), '[]'::json) END,
  'role_settings', CASE WHEN role.rolsuper THEN '[]'::json ELSE COALESCE((
    SELECT json_agg(setting ORDER BY setting COLLATE "C")
    FROM (
      SELECT setting
      FROM unnest(COALESCE(role.rolconfig, ARRAY[]::text[])) AS setting
      UNION ALL
      SELECT database_value.datname || ':' || setting
      FROM pg_db_role_setting AS database_setting
      JOIN pg_database AS database_value
        ON database_value.oid = database_setting.setdatabase
      CROSS JOIN LATERAL unnest(
        COALESCE(database_setting.setconfig, ARRAY[]::text[])
      ) AS setting
      WHERE database_setting.setrole = role.oid
    ) AS role_setting_values
  ), '[]'::json) END,
  'role_owned_objects', CASE WHEN role.rolsuper THEN '[]'::json ELSE COALESCE((
    SELECT json_agg(owned ORDER BY owned COLLATE "C")
    FROM (
      SELECT 'database:' || database_value.datname AS owned
      FROM pg_database AS database_value
      WHERE database_value.datdba = role.oid
      UNION ALL
      SELECT 'schema:' || namespace.nspname
      FROM pg_namespace AS namespace
      WHERE namespace.nspowner = role.oid
      UNION ALL
      SELECT 'relation:' || namespace.nspname || '.' || relation.relname
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE relation.relowner = role.oid
      UNION ALL
      SELECT 'function:' || namespace.nspname || '.' || procedure.proname
             || '(' || pg_get_function_identity_arguments(procedure.oid) || ')'
      FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
      WHERE procedure.proowner = role.oid
      UNION ALL
      SELECT 'ownership-dependency:'
             || COALESCE(database_value.datname, 'shared')
             || ':' || dependency.classid::text
             || ':' || dependency.objid::text
      FROM pg_shdepend AS dependency
      LEFT JOIN pg_database AS database_value
        ON database_value.oid = dependency.dbid
      WHERE dependency.refclassid = 'pg_authid'::regclass
        AND dependency.refobjid = role.oid
        AND dependency.deptype = 'o'
        AND (
          dependency.dbid = 0
          OR dependency.dbid = (
            SELECT oid FROM pg_database WHERE datname = current_database()
          )
        )
    ) AS owned_values
  ), '[]'::json) END,
  'role_direct_acl', CASE WHEN role.rolsuper THEN '[]'::json ELSE COALESCE((
    SELECT json_agg(
      json_build_object(
        'object_kind', acl_value.object_kind,
        'object_name', acl_value.object_name,
        'privilege', acl_value.privilege_type,
        'grantable', acl_value.is_grantable
      )
      ORDER BY acl_value.object_kind COLLATE "C",
               acl_value.object_name COLLATE "C",
               acl_value.privilege_type COLLATE "C",
               acl_value.is_grantable
    )
    FROM (
      SELECT 'database'::text AS object_kind,
             database_value.datname::text AS object_name,
             acl.privilege_type,
             acl.is_grantable
      FROM pg_database AS database_value
      CROSS JOIN LATERAL aclexplode(
        COALESCE(database_value.datacl, acldefault('d', database_value.datdba))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'schema', namespace.nspname::text,
             acl.privilege_type, acl.is_grantable
      FROM pg_namespace AS namespace
      CROSS JOIN LATERAL aclexplode(
        COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'relation', namespace.nspname || '.' || relation.relname,
             acl.privilege_type, acl.is_grantable
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      CROSS JOIN LATERAL aclexplode(
        COALESCE(relation.relacl, acldefault(
          CASE WHEN relation.relkind = 'S' THEN 's'::"char" ELSE 'r'::"char" END,
          relation.relowner
        ))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'column',
             namespace.nspname || '.' || relation.relname || '.'
             || attribute.attname,
             acl.privilege_type, acl.is_grantable
      FROM pg_attribute AS attribute
      JOIN pg_class AS relation ON relation.oid = attribute.attrelid
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
      WHERE acl.grantee = role.oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
      UNION ALL
      SELECT 'function',
             CASE
               WHEN namespace.nspname = 'pg_catalog'
                AND procedure.proname = 'pg_control_system'
               THEN 'pg_catalog.pg_control_system()'
               ELSE namespace.nspname || '.' || procedure.proname || '('
                    || pg_get_function_identity_arguments(procedure.oid)
                    || ')'
             END,
             acl.privilege_type, acl.is_grantable
      FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
      CROSS JOIN LATERAL aclexplode(
        COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'type', namespace.nspname || '.' || type_value.typname,
             acl.privilege_type, acl.is_grantable
      FROM pg_type AS type_value
      JOIN pg_namespace AS namespace
        ON namespace.oid = type_value.typnamespace
      CROSS JOIN LATERAL aclexplode(
        COALESCE(type_value.typacl, acldefault('T', type_value.typowner))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'language', language.lanname,
             acl.privilege_type, acl.is_grantable
      FROM pg_language AS language
      CROSS JOIN LATERAL aclexplode(
        COALESCE(language.lanacl, acldefault('l', language.lanowner))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'foreign-data-wrapper', wrapper.fdwname,
             acl.privilege_type, acl.is_grantable
      FROM pg_foreign_data_wrapper AS wrapper
      CROSS JOIN LATERAL aclexplode(
        COALESCE(wrapper.fdwacl, acldefault('F', wrapper.fdwowner))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'foreign-server', server.srvname,
             acl.privilege_type, acl.is_grantable
      FROM pg_foreign_server AS server
      CROSS JOIN LATERAL aclexplode(
        COALESCE(server.srvacl, acldefault('S', server.srvowner))
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'tablespace', tablespace.spcname,
             acl.privilege_type, acl.is_grantable
      FROM pg_tablespace AS tablespace
      CROSS JOIN LATERAL aclexplode(
        COALESCE(
          tablespace.spcacl,
          acldefault('t', tablespace.spcowner)
        )
      ) AS acl
      WHERE acl.grantee = role.oid
      UNION ALL
      SELECT 'large-object', large_object.oid::text,
             acl.privilege_type, acl.is_grantable
      FROM pg_largeobject_metadata AS large_object
      CROSS JOIN LATERAL aclexplode(
        COALESCE(
          large_object.lomacl,
          acldefault('L', large_object.lomowner)
        )
      ) AS acl
      WHERE acl.grantee = role.oid
__NEXPOLY_PARAMETER_ACL_UNION__
    ) AS acl_value
  ), '[]'::json) END,
  'role_default_acl', CASE WHEN role.rolsuper THEN '[]'::json ELSE COALESCE((
    SELECT json_agg(
      json_build_object(
        'owner', owner_role.rolname,
        'namespace', namespace.nspname,
        'object_type', defaults.defaclobjtype,
        'privilege', acl.privilege_type,
        'grantable', acl.is_grantable
      )
      ORDER BY owner_role.rolname COLLATE "C",
               COALESCE(namespace.nspname, '') COLLATE "C",
               defaults.defaclobjtype,
               acl.privilege_type COLLATE "C",
               acl.is_grantable
    )
    FROM pg_default_acl AS defaults
    JOIN pg_roles AS owner_role ON owner_role.oid = defaults.defaclrole
    LEFT JOIN pg_namespace AS namespace ON namespace.oid = defaults.defaclnamespace
    CROSS JOIN LATERAL aclexplode(defaults.defaclacl) AS acl
    WHERE acl.grantee = role.oid
  ), '[]'::json) END,
  'role_effective_persistent_write', CASE WHEN role.rolsuper THEN '[]'::json ELSE COALESCE((
    SELECT json_agg(object_name ORDER BY object_name COLLATE "C")
    FROM (
      SELECT 'database:' || database_value.datname || ':CREATE' AS object_name
      FROM pg_database AS database_value
      WHERE has_database_privilege(
        role.oid, database_value.oid, 'CREATE'
      )
      UNION
      SELECT 'schema:' || namespace.nspname || ':CREATE'
      FROM pg_namespace AS namespace
      WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname !~ '^pg_toast'
        AND has_schema_privilege(role.oid, namespace.oid, 'CREATE')
      UNION
      SELECT 'relation:' || namespace.nspname || '.' || relation.relname
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname !~ '^pg_toast'
        AND CASE
          WHEN relation.relkind IN ('r', 'p', 'f') THEN
            has_table_privilege(
              role.oid,
              relation.oid,
              'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
            )
          ELSE false
        END
      UNION
      SELECT 'sequence:' || namespace.nspname || '.' || relation.relname
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname !~ '^pg_toast'
        AND CASE
          WHEN relation.relkind = 'S' THEN
            has_sequence_privilege(
              role.oid, relation.oid, 'USAGE,UPDATE'
            )
          ELSE false
        END
      UNION
      SELECT 'column:' || namespace.nspname || '.' || relation.relname
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname !~ '^pg_toast'
        AND relation.relkind IN ('r', 'p', 'f', 'v', 'm')
        AND has_any_column_privilege(
          role.oid,
          relation.oid,
          'INSERT,UPDATE,REFERENCES'
        )
    ) AS writable
  ), '[]'::json) END,
  'system_identifier', control.system_identifier::text,
  'database_oid', database.oid::text,
  'database_owner', pg_get_userbyid(database.datdba),
  'encoding', pg_encoding_to_char(database.encoding),
  'collate', database.datcollate,
  'ctype', database.datctype,
  'server_version_num', current_setting('server_version_num')::integer,
  'data_directory', :'nexpoly_audited_data_directory'
)
FROM pg_roles AS role
CROSS JOIN pg_control_system() AS control
JOIN pg_database AS database ON database.datname = current_database()
WHERE role.rolname = current_user;
SELECT NOT EXISTS (
  SELECT 1 FROM pg_event_trigger
) AS nexpoly_event_trigger_safe \gset
\if :nexpoly_event_trigger_safe
\else
\warn 'Nexpoly audit refused database event triggers'
SELECT 'NEXPOLY_AUDIT_REFUSED_DATABASE_EVENT_TRIGGERS'::integer;
\endif
\set ledger_present false
SELECT (to_regclass('governance.schema_migrations') IS NOT NULL) AS ledger_present \gset
\if :ledger_present
LOCK TABLE governance.schema_migrations IN ACCESS SHARE MODE;
SELECT (
  relation.relkind = 'r'
  AND relation.relpersistence = 'p'
  AND NOT relation.relispartition
  AND NOT relation.relrowsecurity
  AND NOT relation.relforcerowsecurity
  AND COALESCE(relation.reloptions, ARRAY[]::text[]) = ARRAY[]::text[]
  AND relation.relreplident = 'd'
  AND relation.reltablespace = 0
  AND relation.relpartbound IS NULL
  AND (
    SELECT access_method.amname
    FROM pg_am AS access_method
    WHERE access_method.oid = relation.relam
  ) = 'heap'
  AND NOT EXISTS (
    SELECT 1
    FROM aclexplode(relation.relacl) AS acl
    WHERE acl.grantee <> relation.relowner
      AND (
        acl.grantee <> current_user::regrole
        OR acl.privilege_type <> 'SELECT'
        OR acl.is_grantable
      )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM pg_attribute AS attribute
    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
    WHERE attribute.attrelid = relation.oid
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND acl.grantee <> relation.relowner
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_description AS description
    WHERE description.classoid = 'pg_class'::regclass
      AND description.objoid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_init_privs AS initial_privilege
    WHERE initial_privilege.classoid = 'pg_class'::regclass
      AND initial_privilege.objoid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_subscription_rel AS subscription
    WHERE subscription.srrelid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_policy AS policy
    WHERE policy.polrelid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_inherits AS inheritance
    WHERE inheritance.inhrelid = relation.oid
       OR inheritance.inhparent = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_trigger AS trigger_value
    WHERE trigger_value.tgrelid = relation.oid
      AND NOT trigger_value.tgisinternal
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_rewrite AS rule_value
    WHERE rule_value.ev_class = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_constraint AS foreign_key
    WHERE foreign_key.contype = 'f'
      AND foreign_key.confrelid = relation.oid
      AND foreign_key.conrelid <> relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_statistic_ext AS statistics
    WHERE statistics.stxrelid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_seclabel AS security_label
    WHERE security_label.classoid = 'pg_class'::regclass
      AND security_label.objoid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_publication AS publication
    WHERE publication.puballtables
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_publication_rel AS membership
    WHERE membership.prrelid = relation.oid
  )
  AND (
    SELECT COALESCE(
      jsonb_agg(
        jsonb_build_array(
          attribute.attnum,
          attribute.attname,
          format_type(attribute.atttypid, attribute.atttypmod)
        )
        ORDER BY attribute.attnum
      ),
      '[]'::jsonb
    )
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = relation.oid
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) = '[[1,"version","text"],[2,"checksum","text"],[3,"applied_at","timestamp with time zone"]]'::jsonb
) AS nexpoly_ledger_catalog_safe
FROM pg_class AS relation
WHERE relation.oid = 'governance.schema_migrations'::regclass
\gset
\if :nexpoly_ledger_catalog_safe
\else
\warn 'Nexpoly audit refused unsafe migration ledger catalog'
SELECT 'NEXPOLY_AUDIT_REFUSED_UNSAFE_MIGRATION_LEDGER_CATALOG'::integer;
\endif
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
    'persistence', relation.relpersistence,
    'is_partition', relation.relispartition,
    'row_security', relation.relrowsecurity,
    'force_row_security', relation.relforcerowsecurity,
    'reloptions', to_json(COALESCE(relation.reloptions, ARRAY[]::text[])),
    'replica_identity', relation.relreplident,
    'tablespace_oid', relation.reltablespace::text,
    'access_method', COALESCE(access_method.amname, ''),
    'partition_bound', pg_get_expr(
      relation.relpartbound,
      relation.oid,
      true
    ),
    'acl', COALESCE((
      SELECT json_agg(
        json_build_object(
          'grantee', CASE
            WHEN acl.grantee = 0 THEN 'PUBLIC'
            ELSE pg_get_userbyid(acl.grantee)
          END,
          'privilege', acl.privilege_type,
          'grantable', acl.is_grantable
        )
        ORDER BY CASE
                   WHEN acl.grantee = 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
                 END COLLATE "C",
                 acl.privilege_type COLLATE "C",
                 acl.is_grantable
      )
      FROM aclexplode(relation.relacl) AS acl
      WHERE acl.grantee <> relation.relowner
    ), '[]'::json),
    'column_acl', COALESCE((
      SELECT json_agg(
        json_build_object(
          'column', attribute.attname,
          'grantee', CASE
            WHEN acl.grantee = 0 THEN 'PUBLIC'
            ELSE pg_get_userbyid(acl.grantee)
          END,
          'privilege', acl.privilege_type,
          'grantable', acl.is_grantable
        )
        ORDER BY attribute.attnum,
                 CASE
                   WHEN acl.grantee = 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
                 END COLLATE "C",
                 acl.privilege_type COLLATE "C",
                 acl.is_grantable
      )
      FROM pg_attribute AS attribute
      CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
      WHERE attribute.attrelid = relation.oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
        AND acl.grantee <> relation.relowner
    ), '[]'::json),
    'comments', COALESCE((
      SELECT json_agg(
        json_build_object(
          'subobject', description.objsubid,
          'description', description.description
        )
        ORDER BY description.objsubid
      )
      FROM pg_description AS description
      WHERE description.classoid = 'pg_class'::regclass
        AND description.objoid = relation.oid
    ), '[]'::json),
    'initial_privileges', COALESCE((
      SELECT json_agg(
        json_build_object(
          'subobject', initial_privilege.objsubid,
          'privilege_type', initial_privilege.privtype,
          'acl', initial_privilege.initprivs::text
        )
        ORDER BY initial_privilege.objsubid,
                 initial_privilege.privtype
      )
      FROM pg_init_privs AS initial_privilege
      WHERE initial_privilege.classoid = 'pg_class'::regclass
        AND initial_privilege.objoid = relation.oid
    ), '[]'::json),
    'subscriptions', COALESCE((
      SELECT json_agg(
        json_build_object(
          'subscription_oid', subscription.srsubid::text,
          'state', subscription.srsubstate
        )
        ORDER BY subscription.srsubid
      )
      FROM pg_subscription_rel AS subscription
      WHERE subscription.srrelid = relation.oid
    ), '[]'::json),
    'policies', COALESCE((
      SELECT json_agg(policy.polname ORDER BY policy.polname COLLATE "C")
      FROM pg_policy AS policy
      WHERE policy.polrelid = relation.oid
    ), '[]'::json),
    'publications', COALESCE((
      SELECT json_agg(
        publication_membership
        ORDER BY publication_membership COLLATE "C"
      )
      FROM (
        SELECT 'all-tables:' || publication.pubname
               AS publication_membership
        FROM pg_publication AS publication
        WHERE publication.puballtables
        UNION ALL
        SELECT 'table:' || publication.pubname
        FROM pg_publication_rel AS membership
        JOIN pg_publication AS publication
          ON publication.oid = membership.prpubid
        WHERE membership.prrelid = relation.oid
__NEXPOLY_LEDGER_SCHEMA_PUBLICATION_UNION__
      ) AS publication_memberships
    ), '[]'::json),
    'extended_statistics', COALESCE((
      SELECT json_agg(
        statistics_namespace.nspname || '.' || statistics.stxname
        ORDER BY statistics_namespace.nspname COLLATE "C",
                 statistics.stxname COLLATE "C"
      )
      FROM pg_statistic_ext AS statistics
      JOIN pg_namespace AS statistics_namespace
        ON statistics_namespace.oid = statistics.stxnamespace
      WHERE statistics.stxrelid = relation.oid
    ), '[]'::json),
    'security_labels', COALESCE((
      SELECT json_agg(
        json_build_object(
          'provider', security_label.provider,
          'label', security_label.label,
          'subobject', security_label.objsubid
        )
        ORDER BY security_label.provider COLLATE "C",
                 security_label.objsubid,
                 security_label.label COLLATE "C"
      )
      FROM pg_seclabel AS security_label
      WHERE security_label.classoid = 'pg_class'::regclass
        AND security_label.objoid = relation.oid
    ), '[]'::json),
    'parents', COALESCE((
      SELECT json_agg(
        parent_namespace.nspname || '.' || parent.relname
        ORDER BY parent_namespace.nspname COLLATE "C",
                 parent.relname COLLATE "C"
      )
      FROM pg_inherits AS inheritance
      JOIN pg_class AS parent ON parent.oid = inheritance.inhparent
      JOIN pg_namespace AS parent_namespace
        ON parent_namespace.oid = parent.relnamespace
      WHERE inheritance.inhrelid = relation.oid
    ), '[]'::json),
    'children', COALESCE((
      SELECT json_agg(
        child_namespace.nspname || '.' || child.relname
        ORDER BY child_namespace.nspname COLLATE "C",
                 child.relname COLLATE "C"
      )
      FROM pg_inherits AS inheritance
      JOIN pg_class AS child ON child.oid = inheritance.inhrelid
      JOIN pg_namespace AS child_namespace
        ON child_namespace.oid = child.relnamespace
      WHERE inheritance.inhparent = relation.oid
    ), '[]'::json),
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
    ), '[]'::json),
    'triggers', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', trigger_value.tgname,
          'enabled', trigger_value.tgenabled,
          'definition', pg_get_triggerdef(trigger_value.oid, true)
        ) ORDER BY trigger_value.tgname COLLATE "C"
      )
      FROM pg_trigger AS trigger_value
      WHERE trigger_value.tgrelid = relation.oid
        AND NOT trigger_value.tgisinternal
    ), '[]'::json),
    'rewrite_rules', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', rule_value.rulename,
          'event', rule_value.ev_type,
          'enabled', rule_value.ev_enabled,
          'instead', rule_value.is_instead,
          'definition', pg_get_ruledef(rule_value.oid, true)
        ) ORDER BY rule_value.rulename COLLATE "C"
      )
      FROM pg_rewrite AS rule_value
      WHERE rule_value.ev_class = relation.oid
    ), '[]'::json),
    'referencing_foreign_keys', COALESCE((
      SELECT json_agg(
        json_build_object(
          'relation', source_namespace.nspname || '.' || source.relname,
          'name', foreign_key.conname,
          'definition', pg_get_constraintdef(foreign_key.oid, true)
        )
        ORDER BY source_namespace.nspname COLLATE "C",
                 source.relname COLLATE "C",
                 foreign_key.conname COLLATE "C"
      )
      FROM pg_constraint AS foreign_key
      JOIN pg_class AS source ON source.oid = foreign_key.conrelid
      JOIN pg_namespace AS source_namespace
        ON source_namespace.oid = source.relnamespace
      WHERE foreign_key.contype = 'f'
        AND foreign_key.confrelid = relation.oid
        AND foreign_key.conrelid <> relation.oid
    ), '[]'::json),
    'unapproved_drop_dependents', COALESCE((
      SELECT json_agg(
        json_build_object(
          'class', dependency.classid::regclass::text,
          'object', pg_describe_object(
            dependency.classid,
            dependency.objid,
            dependency.objsubid
          ),
          'referenced_subobject', dependency.refobjsubid,
          'dependency_type', dependency.deptype
        )
        ORDER BY dependency.classid::regclass::text COLLATE "C",
                 pg_describe_object(
                   dependency.classid,
                   dependency.objid,
                   dependency.objsubid
                 ) COLLATE "C",
                 dependency.refobjsubid
      )
      FROM pg_depend AS dependency
      WHERE dependency.refclassid = 'pg_class'::regclass
        AND dependency.refobjid = relation.oid
        AND NOT (
          dependency.classid = 'pg_type'::regclass
          AND dependency.objid = relation.reltype
        )
        AND NOT (
          dependency.classid = 'pg_class'::regclass
          AND (
            dependency.objid = relation.reltoastrelid
            OR dependency.objid IN (
              SELECT index_value.indexrelid
              FROM pg_index AS index_value
              WHERE index_value.indrelid = relation.oid
            )
          )
        )
        AND NOT (
          dependency.classid = 'pg_constraint'::regclass
          AND dependency.objid IN (
            SELECT constraint_value.oid
            FROM pg_constraint AS constraint_value
            WHERE constraint_value.conrelid = relation.oid
          )
        )
        AND NOT (
          dependency.classid = 'pg_attrdef'::regclass
          AND dependency.objid IN (
            SELECT default_value.oid
            FROM pg_attrdef AS default_value
            WHERE default_value.adrelid = relation.oid
          )
        )
    ), '[]'::json)
  )
)
FROM pg_class AS relation
LEFT JOIN pg_am AS access_method ON access_method.oid = relation.relam
WHERE relation.oid = 'governance.schema_migrations'::regclass;
\else
SELECT json_build_object(
  'record_type', 'ledger',
  'rows', '[]'::json,
  'relation', null
);
\endif
\set generation_schema_present false
SELECT (
  to_regnamespace('generation') IS NOT NULL
) AS generation_schema_present \gset
\if :generation_schema_present
SELECT (
  namespace.nspowner = database.datdba
  AND NOT EXISTS (
    SELECT 1
    FROM aclexplode(namespace.nspacl) AS acl
    WHERE acl.grantee <> namespace.nspowner
      AND (
        acl.grantee <> current_user::regrole
        OR acl.privilege_type <> 'USAGE'
        OR acl.is_grantable
      )
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_description AS description
    WHERE description.classoid = 'pg_namespace'::regclass
      AND description.objoid = namespace.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_seclabel AS security_label
    WHERE security_label.classoid = 'pg_namespace'::regclass
      AND security_label.objoid = namespace.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_default_acl AS defaults
    WHERE defaults.defaclnamespace = namespace.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_init_privs AS initial_privilege
    WHERE initial_privilege.classoid = 'pg_namespace'::regclass
      AND initial_privilege.objoid = namespace.oid
  )
  AND NOT EXISTS (
    SELECT 1
    FROM pg_depend AS dependency
    WHERE dependency.refclassid = 'pg_namespace'::regclass
      AND dependency.refobjid = namespace.oid
      AND NOT (
        dependency.classid = 'pg_class'::regclass
        AND (
          dependency.objid = COALESCE(
            to_regclass('generation.polytao_jobs')::oid,
            0
          )
          OR dependency.objid IN (
            SELECT index_value.indexrelid
            FROM pg_index AS index_value
            WHERE index_value.indrelid = COALESCE(
              to_regclass('generation.polytao_jobs')::oid,
              0
            )
          )
        )
      )
      AND NOT (
        dependency.classid = 'pg_type'::regclass
        AND dependency.objid IN (
          SELECT allowed_type
          FROM (
            SELECT relation.reltype AS allowed_type
            FROM pg_class AS relation
            WHERE relation.oid = COALESCE(
              to_regclass('generation.polytao_jobs')::oid,
              0
            )
            UNION ALL
            SELECT type_value.typarray
            FROM pg_type AS type_value
            JOIN pg_class AS relation
              ON relation.reltype = type_value.oid
            WHERE relation.oid = COALESCE(
              to_regclass('generation.polytao_jobs')::oid,
              0
            )
              AND type_value.typarray <> 0
          ) AS allowed_types
        )
      )
  )
__NEXPOLY_GENERATION_SCHEMA_PUBLICATION_SAFE__
) AS nexpoly_generation_schema_safe
FROM pg_namespace AS namespace
JOIN pg_database AS database ON database.datname = current_database()
WHERE namespace.oid = to_regnamespace('generation')
\gset
\if :nexpoly_generation_schema_safe
\else
\warn 'Nexpoly audit refused unsafe generation schema catalog'
SELECT 'NEXPOLY_AUDIT_REFUSED_UNSAFE_GENERATION_SCHEMA_CATALOG'::integer;
\endif
\endif
SELECT (to_regclass('generation.polytao_jobs') IS NOT NULL) AS legacy_present \gset
\if :legacy_present
LOCK TABLE generation.polytao_jobs IN ACCESS SHARE MODE;
SELECT (
  relation.relkind = 'r'
  AND relation.relpersistence = 'p'
  AND NOT relation.relispartition
  AND NOT relation.relrowsecurity
  AND NOT relation.relforcerowsecurity
  AND COALESCE(relation.reloptions, ARRAY[]::text[]) = ARRAY[]::text[]
  AND relation.relreplident = 'd'
  AND relation.reltablespace = 0
  AND relation.relpartbound IS NULL
  AND (
    SELECT access_method.amname
    FROM pg_am AS access_method
    WHERE access_method.oid = relation.relam
  ) = 'heap'
  AND NOT EXISTS (
    SELECT 1
    FROM aclexplode(relation.relacl) AS acl
    WHERE acl.grantee <> relation.relowner
      AND (
        acl.grantee <> current_user::regrole
        OR acl.privilege_type <> 'SELECT'
        OR acl.is_grantable
      )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM pg_attribute AS attribute
    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
    WHERE attribute.attrelid = relation.oid
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
      AND acl.grantee <> relation.relowner
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_description AS description
    WHERE description.classoid = 'pg_class'::regclass
      AND description.objoid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_init_privs AS initial_privilege
    WHERE initial_privilege.classoid = 'pg_class'::regclass
      AND initial_privilege.objoid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_subscription_rel AS subscription
    WHERE subscription.srrelid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_policy AS policy
    WHERE policy.polrelid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_inherits AS inheritance
    WHERE inheritance.inhrelid = relation.oid
       OR inheritance.inhparent = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_trigger AS trigger_value
    WHERE trigger_value.tgrelid = relation.oid
      AND NOT trigger_value.tgisinternal
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_rewrite AS rule_value
    WHERE rule_value.ev_class = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_constraint AS foreign_key
    WHERE foreign_key.contype = 'f'
      AND foreign_key.confrelid = relation.oid
      AND foreign_key.conrelid <> relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_statistic_ext AS statistics
    WHERE statistics.stxrelid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_seclabel AS security_label
    WHERE security_label.classoid = 'pg_class'::regclass
      AND security_label.objoid = relation.oid
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_publication AS publication
    WHERE publication.puballtables
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_publication_rel AS membership
    WHERE membership.prrelid = relation.oid
  )
  AND (
    SELECT COALESCE(
      jsonb_agg(
        jsonb_build_array(
          attribute.attnum,
          attribute.attname,
          format_type(attribute.atttypid, attribute.atttypmod)
        )
        ORDER BY attribute.attnum
      ),
      '[]'::jsonb
    )
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = relation.oid
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) = '[[1,"job_id","text"],[2,"status","text"],[3,"input_smiles","text"],[4,"canonical_smiles","text"],[5,"descriptor_prompt","text"],[6,"descriptors","jsonb"],[7,"request_data","jsonb"],[8,"requested_count","integer"],[9,"returned_count","integer"],[10,"attempts","integer"],[11,"progress_percent","integer"],[12,"progress_stage","text"],[13,"progress_message","text"],[14,"worker_id","text"],[15,"worker_job_id","text"],[16,"worker_version","text"],[17,"engine","text"],[18,"result_data","jsonb"],[19,"error_message","text"],[20,"created_at","timestamp with time zone"],[21,"updated_at","timestamp with time zone"],[22,"started_at","timestamp with time zone"],[23,"finished_at","timestamp with time zone"]]'::jsonb
) AS nexpoly_legacy_catalog_safe
FROM pg_class AS relation
WHERE relation.oid = 'generation.polytao_jobs'::regclass
\gset
\if :nexpoly_legacy_catalog_safe
\else
\warn 'Nexpoly audit refused unsafe legacy relation catalog'
SELECT 'NEXPOLY_AUDIT_REFUSED_UNSAFE_LEGACY_RELATION_CATALOG'::integer;
\endif
SELECT json_build_object(
  'record_type', 'legacy_relation',
  'present', true,
  'generation_schema', (
    SELECT json_build_object(
      'oid', namespace.oid::text,
      'owner', pg_get_userbyid(namespace.nspowner),
      'acl', COALESCE((
        SELECT json_agg(
          json_build_object(
            'grantee', CASE
              WHEN acl.grantee = 0 THEN 'PUBLIC'
              ELSE pg_get_userbyid(acl.grantee)
            END,
            'privilege', acl.privilege_type,
            'grantable', acl.is_grantable
          )
          ORDER BY CASE
                     WHEN acl.grantee = 0 THEN 'PUBLIC'
                     ELSE pg_get_userbyid(acl.grantee)
                   END COLLATE "C",
                   acl.privilege_type COLLATE "C",
                   acl.is_grantable
        )
        FROM aclexplode(namespace.nspacl) AS acl
        WHERE acl.grantee <> namespace.nspowner
      ), '[]'::json),
      'comments', COALESCE((
        SELECT json_agg(description.description ORDER BY description.description COLLATE "C")
        FROM pg_description AS description
        WHERE description.classoid = 'pg_namespace'::regclass
          AND description.objoid = namespace.oid
      ), '[]'::json),
      'security_labels', COALESCE((
        SELECT json_agg(
          json_build_object(
            'provider', security_label.provider,
            'label', security_label.label
          )
          ORDER BY security_label.provider COLLATE "C",
                   security_label.label COLLATE "C"
        )
        FROM pg_seclabel AS security_label
        WHERE security_label.classoid = 'pg_namespace'::regclass
          AND security_label.objoid = namespace.oid
      ), '[]'::json),
      'default_acl', COALESCE((
        SELECT json_agg(defaults.oid::text ORDER BY defaults.oid)
        FROM pg_default_acl AS defaults
        WHERE defaults.defaclnamespace = namespace.oid
      ), '[]'::json),
      'initial_privileges', COALESCE((
        SELECT json_agg(
          initial_privilege.initprivs::text
          ORDER BY initial_privilege.objsubid,
                   initial_privilege.privtype
        )
        FROM pg_init_privs AS initial_privilege
        WHERE initial_privilege.classoid = 'pg_namespace'::regclass
          AND initial_privilege.objoid = namespace.oid
      ), '[]'::json),
      'publications', COALESCE((
        SELECT json_agg(
          publication.pubname
          ORDER BY publication.pubname COLLATE "C"
        )
        FROM pg_publication AS publication
        WHERE false
__NEXPOLY_GENERATION_SCHEMA_PUBLICATIONS__
      ), '[]'::json),
      'unapproved_dependents', COALESCE((
        SELECT json_agg(
          json_build_object(
            'class', dependency.classid::regclass::text,
            'object', pg_describe_object(
              dependency.classid,
              dependency.objid,
              dependency.objsubid
            ),
            'dependency_type', dependency.deptype
          )
          ORDER BY dependency.classid::regclass::text COLLATE "C",
                   pg_describe_object(
                     dependency.classid,
                     dependency.objid,
                     dependency.objsubid
                   ) COLLATE "C"
        )
        FROM pg_depend AS dependency
        WHERE dependency.refclassid = 'pg_namespace'::regclass
          AND dependency.refobjid = namespace.oid
          AND NOT (
            dependency.classid = 'pg_class'::regclass
            AND (
              dependency.objid = relation.oid
              OR dependency.objid IN (
                SELECT index_value.indexrelid
                FROM pg_index AS index_value
                WHERE index_value.indrelid = relation.oid
              )
            )
          )
          AND NOT (
            dependency.classid = 'pg_type'::regclass
            AND dependency.objid IN (
              relation.reltype,
              (
                SELECT type_value.typarray
                FROM pg_type AS type_value
                WHERE type_value.oid = relation.reltype
              )
            )
          )
      ), '[]'::json)
    )
    FROM pg_namespace AS namespace
    WHERE namespace.oid = to_regnamespace('generation')
  ),
  'relation', json_build_object(
    'oid', relation.oid::text,
    'kind', relation.relkind,
    'persistence', relation.relpersistence,
    'is_partition', relation.relispartition,
    'row_security', relation.relrowsecurity,
    'force_row_security', relation.relforcerowsecurity,
    'reloptions', to_json(COALESCE(relation.reloptions, ARRAY[]::text[])),
    'replica_identity', relation.relreplident,
    'tablespace_oid', relation.reltablespace::text,
    'access_method', COALESCE(access_method.amname, ''),
    'partition_bound', pg_get_expr(
      relation.relpartbound,
      relation.oid,
      true
    ),
    'acl', COALESCE((
      SELECT json_agg(
        json_build_object(
          'grantee', CASE
            WHEN acl.grantee = 0 THEN 'PUBLIC'
            ELSE pg_get_userbyid(acl.grantee)
          END,
          'privilege', acl.privilege_type,
          'grantable', acl.is_grantable
        )
        ORDER BY CASE
                   WHEN acl.grantee = 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
                 END COLLATE "C",
                 acl.privilege_type COLLATE "C",
                 acl.is_grantable
      )
      FROM aclexplode(relation.relacl) AS acl
      WHERE acl.grantee <> relation.relowner
    ), '[]'::json),
    'column_acl', COALESCE((
      SELECT json_agg(
        json_build_object(
          'column', attribute.attname,
          'grantee', CASE
            WHEN acl.grantee = 0 THEN 'PUBLIC'
            ELSE pg_get_userbyid(acl.grantee)
          END,
          'privilege', acl.privilege_type,
          'grantable', acl.is_grantable
        )
        ORDER BY attribute.attnum,
                 CASE
                   WHEN acl.grantee = 0 THEN 'PUBLIC'
                   ELSE pg_get_userbyid(acl.grantee)
                 END COLLATE "C",
                 acl.privilege_type COLLATE "C",
                 acl.is_grantable
      )
      FROM pg_attribute AS attribute
      CROSS JOIN LATERAL aclexplode(attribute.attacl) AS acl
      WHERE attribute.attrelid = relation.oid
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped
        AND acl.grantee <> relation.relowner
    ), '[]'::json),
    'comments', COALESCE((
      SELECT json_agg(
        json_build_object(
          'subobject', description.objsubid,
          'description', description.description
        )
        ORDER BY description.objsubid
      )
      FROM pg_description AS description
      WHERE description.classoid = 'pg_class'::regclass
        AND description.objoid = relation.oid
    ), '[]'::json),
    'initial_privileges', COALESCE((
      SELECT json_agg(
        json_build_object(
          'subobject', initial_privilege.objsubid,
          'privilege_type', initial_privilege.privtype,
          'acl', initial_privilege.initprivs::text
        )
        ORDER BY initial_privilege.objsubid,
                 initial_privilege.privtype
      )
      FROM pg_init_privs AS initial_privilege
      WHERE initial_privilege.classoid = 'pg_class'::regclass
        AND initial_privilege.objoid = relation.oid
    ), '[]'::json),
    'subscriptions', COALESCE((
      SELECT json_agg(
        json_build_object(
          'subscription_oid', subscription.srsubid::text,
          'state', subscription.srsubstate
        )
        ORDER BY subscription.srsubid
      )
      FROM pg_subscription_rel AS subscription
      WHERE subscription.srrelid = relation.oid
    ), '[]'::json),
    'policies', COALESCE((
      SELECT json_agg(policy.polname ORDER BY policy.polname COLLATE "C")
      FROM pg_policy AS policy
      WHERE policy.polrelid = relation.oid
    ), '[]'::json),
    'publications', COALESCE((
      SELECT json_agg(
        publication_membership
        ORDER BY publication_membership COLLATE "C"
      )
      FROM (
        SELECT 'all-tables:' || publication.pubname
               AS publication_membership
        FROM pg_publication AS publication
        WHERE publication.puballtables
        UNION ALL
        SELECT 'table:' || publication.pubname
        FROM pg_publication_rel AS membership
        JOIN pg_publication AS publication
          ON publication.oid = membership.prpubid
        WHERE membership.prrelid = relation.oid
__NEXPOLY_LEGACY_SCHEMA_PUBLICATION_UNION__
      ) AS publication_memberships
    ), '[]'::json),
    'extended_statistics', COALESCE((
      SELECT json_agg(
        statistics_namespace.nspname || '.' || statistics.stxname
        ORDER BY statistics_namespace.nspname COLLATE "C",
                 statistics.stxname COLLATE "C"
      )
      FROM pg_statistic_ext AS statistics
      JOIN pg_namespace AS statistics_namespace
        ON statistics_namespace.oid = statistics.stxnamespace
      WHERE statistics.stxrelid = relation.oid
    ), '[]'::json),
    'security_labels', COALESCE((
      SELECT json_agg(
        json_build_object(
          'provider', security_label.provider,
          'label', security_label.label,
          'subobject', security_label.objsubid
        )
        ORDER BY security_label.provider COLLATE "C",
                 security_label.objsubid,
                 security_label.label COLLATE "C"
      )
      FROM pg_seclabel AS security_label
      WHERE security_label.classoid = 'pg_class'::regclass
        AND security_label.objoid = relation.oid
    ), '[]'::json),
    'parents', COALESCE((
      SELECT json_agg(
        parent_namespace.nspname || '.' || parent.relname
        ORDER BY parent_namespace.nspname COLLATE "C",
                 parent.relname COLLATE "C"
      )
      FROM pg_inherits AS inheritance
      JOIN pg_class AS parent ON parent.oid = inheritance.inhparent
      JOIN pg_namespace AS parent_namespace
        ON parent_namespace.oid = parent.relnamespace
      WHERE inheritance.inhrelid = relation.oid
    ), '[]'::json),
    'children', COALESCE((
      SELECT json_agg(
        child_namespace.nspname || '.' || child.relname
        ORDER BY child_namespace.nspname COLLATE "C",
                 child.relname COLLATE "C"
      )
      FROM pg_inherits AS inheritance
      JOIN pg_class AS child ON child.oid = inheritance.inhrelid
      JOIN pg_namespace AS child_namespace
        ON child_namespace.oid = child.relnamespace
      WHERE inheritance.inhparent = relation.oid
    ), '[]'::json),
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
    ), '[]'::json),
    'triggers', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', trigger_value.tgname,
          'enabled', trigger_value.tgenabled,
          'definition', pg_get_triggerdef(trigger_value.oid, true)
        ) ORDER BY trigger_value.tgname COLLATE "C"
      )
      FROM pg_trigger AS trigger_value
      WHERE trigger_value.tgrelid = relation.oid
        AND NOT trigger_value.tgisinternal
    ), '[]'::json),
    'rewrite_rules', COALESCE((
      SELECT json_agg(
        json_build_object(
          'name', rule_value.rulename,
          'event', rule_value.ev_type,
          'enabled', rule_value.ev_enabled,
          'instead', rule_value.is_instead,
          'definition', pg_get_ruledef(rule_value.oid, true)
        ) ORDER BY rule_value.rulename COLLATE "C"
      )
      FROM pg_rewrite AS rule_value
      WHERE rule_value.ev_class = relation.oid
    ), '[]'::json),
    'referencing_foreign_keys', COALESCE((
      SELECT json_agg(
        json_build_object(
          'relation', source_namespace.nspname || '.' || source.relname,
          'name', foreign_key.conname,
          'definition', pg_get_constraintdef(foreign_key.oid, true)
        )
        ORDER BY source_namespace.nspname COLLATE "C",
                 source.relname COLLATE "C",
                 foreign_key.conname COLLATE "C"
      )
      FROM pg_constraint AS foreign_key
      JOIN pg_class AS source ON source.oid = foreign_key.conrelid
      JOIN pg_namespace AS source_namespace
        ON source_namespace.oid = source.relnamespace
      WHERE foreign_key.contype = 'f'
        AND foreign_key.confrelid = relation.oid
        AND foreign_key.conrelid <> relation.oid
    ), '[]'::json),
    'unapproved_drop_dependents', COALESCE((
      SELECT json_agg(
        json_build_object(
          'class', dependency.classid::regclass::text,
          'object', pg_describe_object(
            dependency.classid,
            dependency.objid,
            dependency.objsubid
          ),
          'referenced_subobject', dependency.refobjsubid,
          'dependency_type', dependency.deptype
        )
        ORDER BY dependency.classid::regclass::text COLLATE "C",
                 pg_describe_object(
                   dependency.classid,
                   dependency.objid,
                   dependency.objsubid
                 ) COLLATE "C",
                 dependency.refobjsubid
      )
      FROM pg_depend AS dependency
      WHERE dependency.refclassid = 'pg_class'::regclass
        AND dependency.refobjid = relation.oid
        AND NOT (
          dependency.classid = 'pg_type'::regclass
          AND dependency.objid = relation.reltype
        )
        AND NOT (
          dependency.classid = 'pg_class'::regclass
          AND (
            dependency.objid = relation.reltoastrelid
            OR dependency.objid IN (
              SELECT index_value.indexrelid
              FROM pg_index AS index_value
              WHERE index_value.indrelid = relation.oid
            )
          )
        )
        AND NOT (
          dependency.classid = 'pg_constraint'::regclass
          AND dependency.objid IN (
            SELECT constraint_value.oid
            FROM pg_constraint AS constraint_value
            WHERE constraint_value.conrelid = relation.oid
          )
        )
        AND NOT (
          dependency.classid = 'pg_attrdef'::regclass
          AND dependency.objid IN (
            SELECT default_value.oid
            FROM pg_attrdef AS default_value
            WHERE default_value.adrelid = relation.oid
          )
        )
    ), '[]'::json)
  ),
  'rows', COALESCE((
    SELECT json_agg(to_jsonb(value) ORDER BY to_jsonb(value)::text COLLATE "C")
    FROM generation.polytao_jobs AS value
  ), '[]'::json)
)
FROM pg_class AS relation
LEFT JOIN pg_am AS access_method ON access_method.oid = relation.relam
WHERE relation.oid = 'generation.polytao_jobs'::regclass;
\else
SELECT json_build_object(
  'record_type', 'legacy_relation',
  'present', false,
  'generation_schema', (
    SELECT json_build_object(
      'oid', namespace.oid::text,
      'owner', pg_get_userbyid(namespace.nspowner),
      'acl', COALESCE((
        SELECT json_agg(
          json_build_object(
            'grantee', CASE
              WHEN acl.grantee = 0 THEN 'PUBLIC'
              ELSE pg_get_userbyid(acl.grantee)
            END,
            'privilege', acl.privilege_type,
            'grantable', acl.is_grantable
          )
          ORDER BY CASE
                     WHEN acl.grantee = 0 THEN 'PUBLIC'
                     ELSE pg_get_userbyid(acl.grantee)
                   END COLLATE "C",
                   acl.privilege_type COLLATE "C",
                   acl.is_grantable
        )
        FROM aclexplode(namespace.nspacl) AS acl
        WHERE acl.grantee <> namespace.nspowner
      ), '[]'::json),
      'comments', COALESCE((
        SELECT json_agg(description.description ORDER BY description.description COLLATE "C")
        FROM pg_description AS description
        WHERE description.classoid = 'pg_namespace'::regclass
          AND description.objoid = namespace.oid
      ), '[]'::json),
      'security_labels', COALESCE((
        SELECT json_agg(
          json_build_object(
            'provider', security_label.provider,
            'label', security_label.label
          )
          ORDER BY security_label.provider COLLATE "C",
                   security_label.label COLLATE "C"
        )
        FROM pg_seclabel AS security_label
        WHERE security_label.classoid = 'pg_namespace'::regclass
          AND security_label.objoid = namespace.oid
      ), '[]'::json),
      'default_acl', COALESCE((
        SELECT json_agg(defaults.oid::text ORDER BY defaults.oid)
        FROM pg_default_acl AS defaults
        WHERE defaults.defaclnamespace = namespace.oid
      ), '[]'::json),
      'initial_privileges', COALESCE((
        SELECT json_agg(
          initial_privilege.initprivs::text
          ORDER BY initial_privilege.objsubid,
                   initial_privilege.privtype
        )
        FROM pg_init_privs AS initial_privilege
        WHERE initial_privilege.classoid = 'pg_namespace'::regclass
          AND initial_privilege.objoid = namespace.oid
      ), '[]'::json),
      'publications', COALESCE((
        SELECT json_agg(
          publication.pubname
          ORDER BY publication.pubname COLLATE "C"
        )
        FROM pg_publication AS publication
        WHERE false
__NEXPOLY_GENERATION_SCHEMA_PUBLICATIONS__
      ), '[]'::json),
      'unapproved_dependents', COALESCE((
        SELECT json_agg(
          json_build_object(
            'class', dependency.classid::regclass::text,
            'object', pg_describe_object(
              dependency.classid,
              dependency.objid,
              dependency.objsubid
            ),
            'dependency_type', dependency.deptype
          )
          ORDER BY dependency.classid::regclass::text COLLATE "C",
                   pg_describe_object(
                     dependency.classid,
                     dependency.objid,
                     dependency.objsubid
                   ) COLLATE "C"
        )
        FROM pg_depend AS dependency
        WHERE dependency.refclassid = 'pg_namespace'::regclass
          AND dependency.refobjid = namespace.oid
      ), '[]'::json)
    )
    FROM pg_namespace AS namespace
    WHERE namespace.oid = to_regnamespace('generation')
  ),
  'relation', null,
  'rows', '[]'::json
);
\endif
COMMIT;
"""

DATABASE_INVENTORY_SQL = r"""
\set ON_ERROR_STOP on
__NEXPOLY_EVENT_TRIGGERS_SESSION_ASSERT__
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
\if :{?online_admin_expected}
SELECT json_build_object(
  'record_type', 'online_admin',
  'session_user', session_user,
  'current_user', current_user,
  'role_superuser', role.rolsuper,
  'role_can_login', role.rolcanlogin
)
FROM pg_roles AS role
WHERE role.rolname = session_user;
\endif
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

PARAMETER_ACL_UNION_SQL = r"""
      UNION ALL
      SELECT 'parameter', parameter.parname,
             acl.privilege_type, acl.is_grantable
      FROM pg_parameter_acl AS parameter
      CROSS JOIN LATERAL aclexplode(parameter.paracl) AS acl
      WHERE acl.grantee = role.oid
"""

SCHEMA_PUBLICATION_UNION_SQL = r"""
        UNION ALL
        SELECT 'schema:' || publication.pubname || ':'
               || publication_namespace.nspname
        FROM pg_publication_namespace AS membership
        JOIN pg_publication AS publication
          ON publication.oid = membership.pnpubid
        JOIN pg_namespace AS publication_namespace
          ON publication_namespace.oid = membership.pnnspid
        WHERE membership.pnnspid = relation.relnamespace
"""

GENERATION_SCHEMA_PUBLICATION_SAFE_SQL = r"""
  AND NOT EXISTS (
    SELECT 1
    FROM pg_publication_namespace AS membership
    WHERE membership.pnnspid = namespace.oid
  )
"""

GENERATION_SCHEMA_PUBLICATIONS_SQL = r"""
           OR publication.oid IN (
             SELECT membership.pnpubid
             FROM pg_publication_namespace AS membership
             WHERE membership.pnnspid = namespace.oid
           )
"""

EVENT_TRIGGERS_SESSION_ASSERT_SQL = r"""
SELECT (
  current_setting('event_triggers') = 'false'
) AS nexpoly_event_triggers_session_safe \gset
\if :nexpoly_event_triggers_session_safe
\else
\warn 'Nexpoly audit refused enabled login event triggers'
SELECT 'NEXPOLY_AUDIT_REFUSED_ENABLED_LOGIN_EVENT_TRIGGERS'::integer;
\endif
"""


DATABASE_STARTUP_SETTINGS_SQL = r"""
  'jit', current_setting('jit')::boolean,
  'shared_preload_libraries',
    current_setting('shared_preload_libraries'),
  'session_preload_libraries',
    current_setting('session_preload_libraries'),
  'local_preload_libraries',
    current_setting('local_preload_libraries'),
  'dynamic_library_path', current_setting('dynamic_library_path'),
  'archive_mode', current_setting('archive_mode'),
  'archive_command', current_setting('archive_command'),
  'archive_cleanup_command', current_setting('archive_cleanup_command'),
  'restore_command', current_setting('restore_command'),
  'recovery_end_command', current_setting('recovery_end_command'),
  'ssl_passphrase_command', current_setting('ssl_passphrase_command'),
  'ssl_passphrase_command_supports_reload',
    current_setting('ssl_passphrase_command_supports_reload'),
  'jit_provider', current_setting('jit_provider'),
  'config_file', current_setting('config_file'),
  'hba_file', current_setting('hba_file'),
  'ident_file', current_setting('ident_file'),
  'config_source_files',
    json_build_array(current_setting('config_file')),
  'config_errors', '[]'::json,
"""


DATABASE_EXTERNAL_STARTUP_PLACEHOLDER_SQL = r"""
  'jit', current_setting('jit')::boolean,
  'shared_preload_libraries', NULL,
  'session_preload_libraries', NULL,
  'local_preload_libraries', NULL,
  'dynamic_library_path', NULL,
  'archive_mode', NULL,
  'archive_command', NULL,
  'archive_cleanup_command', NULL,
  'restore_command', NULL,
  'recovery_end_command', NULL,
  'ssl_passphrase_command', NULL,
  'ssl_passphrase_command_supports_reload', NULL,
  'jit_provider', NULL,
  'config_file', NULL,
  'hba_file', NULL,
  'ident_file', NULL,
  'config_source_files', '[]'::json,
  'config_errors', '[]'::json,
"""


def _database_audit_sql_for_major(
    major: int,
    *,
    external_startup_projection: bool = False,
) -> str:
    if major not in SUPPORTED_POSTGRES_AUDIT_MAJORS:
        raise MediaEvidenceError(
            "database audit SQL major is unsupported"
        )
    fragment = PARAMETER_ACL_UNION_SQL if major >= 15 else ""
    event_assertion = (
        EVENT_TRIGGERS_SESSION_ASSERT_SQL if major >= 17 else ""
    )
    sql = DATABASE_AUDIT_SQL.replace(
        "__NEXPOLY_EVENT_TRIGGERS_SESSION_ASSERT__",
        event_assertion,
    ).replace(
        "__NEXPOLY_STARTUP_SETTINGS_JSON__",
        (
            DATABASE_EXTERNAL_STARTUP_PLACEHOLDER_SQL
            if external_startup_projection
            else DATABASE_STARTUP_SETTINGS_SQL
        ),
    ).replace(
        "__NEXPOLY_EVENT_TRIGGERS_DISABLED_JSON__",
        (
            "true"
            if major >= 17
            else "null"
        ),
    ).replace(
        "__NEXPOLY_PARAMETER_ACL_UNION__",
        fragment,
    )
    schema_publication_fragment = (
        SCHEMA_PUBLICATION_UNION_SQL if major >= 15 else ""
    )
    sql = sql.replace(
        "__NEXPOLY_LEDGER_SCHEMA_PUBLICATION_UNION__",
        schema_publication_fragment,
    ).replace(
        "__NEXPOLY_LEGACY_SCHEMA_PUBLICATION_UNION__",
        schema_publication_fragment,
    )
    generation_publication_safe = (
        GENERATION_SCHEMA_PUBLICATION_SAFE_SQL if major >= 15 else ""
    )
    generation_publications = (
        GENERATION_SCHEMA_PUBLICATIONS_SQL if major >= 15 else ""
    )
    sql = sql.replace(
        "__NEXPOLY_GENERATION_SCHEMA_PUBLICATION_SAFE__",
        generation_publication_safe,
    ).replace(
        "__NEXPOLY_GENERATION_SCHEMA_PUBLICATIONS__",
        generation_publications,
    )
    if "__NEXPOLY_" in sql:
        raise AssertionError("database audit SQL fragment was not resolved")
    return sql


def _database_inventory_sql_for_major(major: int) -> str:
    if major not in SUPPORTED_POSTGRES_AUDIT_MAJORS:
        raise MediaEvidenceError(
            "database inventory SQL major is unsupported"
        )
    return DATABASE_INVENTORY_SQL.replace(
        "__NEXPOLY_EVENT_TRIGGERS_SESSION_ASSERT__",
        EVENT_TRIGGERS_SESSION_ASSERT_SQL if major >= 17 else "",
    )


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
    expected_reader: str | None,
    legacy_relation: bool,
) -> dict[str, object]:
    if not isinstance(relation, dict) or set(relation) != {
        "oid",
        "kind",
        "persistence",
        "is_partition",
        "row_security",
        "force_row_security",
        "reloptions",
        "replica_identity",
        "tablespace_oid",
        "access_method",
        "partition_bound",
        "acl",
        "column_acl",
        "comments",
        "initial_privileges",
        "subscriptions",
        "policies",
        "publications",
        "extended_statistics",
        "security_labels",
        "parents",
        "children",
        "owner",
        "columns",
        "indexes",
        "constraints",
        "triggers",
        "rewrite_rules",
        "referencing_foreign_keys",
        "unapproved_drop_dependents",
    }:
        raise MediaEvidenceError("database relation authority shape is invalid")
    if (
        relation.get("kind") != "r"
        or relation.get("persistence") != "p"
        or relation.get("is_partition") is not False
        or relation.get("row_security") is not False
        or relation.get("force_row_security") is not False
        or relation.get("reloptions") != []
        or relation.get("replica_identity") != "d"
        or relation.get("tablespace_oid") != "0"
        or relation.get("access_method") != "heap"
        or relation.get("partition_bound") is not None
        or relation.get("acl")
        != (
            []
            if expected_reader is None or expected_reader == expected_owner
            else [
                {
                    "grantee": expected_reader,
                    "privilege": "SELECT",
                    "grantable": False,
                }
            ]
        )
        or relation.get("column_acl") != []
        or relation.get("comments") != []
        or relation.get("initial_privileges") != []
        or relation.get("subscriptions") != []
        or relation.get("policies") != []
        or relation.get("publications") != []
        or relation.get("extended_statistics") != []
        or relation.get("security_labels") != []
        or relation.get("parents") != []
        or relation.get("children") != []
        or relation.get("triggers") != []
        or relation.get("rewrite_rules") != []
        or relation.get("referencing_foreign_keys") != []
        or relation.get("unapproved_drop_dependents") != []
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


def _validated_generation_schema_authority(
    schema: object,
    *,
    expected_owner: str,
    expected_reader: str | None,
) -> dict[str, object]:
    expected_acl = (
        []
        if expected_reader is None or expected_reader == expected_owner
        else [
            {
                "grantee": expected_reader,
                "privilege": "USAGE",
                "grantable": False,
            }
        ]
    )
    if (
        not isinstance(schema, dict)
        or set(schema)
        != {
            "oid",
            "owner",
            "acl",
            "comments",
            "security_labels",
            "default_acl",
            "initial_privileges",
            "publications",
            "unapproved_dependents",
        }
        or not isinstance(schema.get("oid"), str)
        or not schema["oid"].isdigit()
        or schema.get("owner") != expected_owner
        or schema.get("acl") != expected_acl
        or schema.get("comments") != []
        or schema.get("security_labels") != []
        or schema.get("default_acl") != []
        or schema.get("initial_privileges") != []
        or schema.get("publications") != []
        or schema.get("unapproved_dependents") != []
    ):
        raise MediaEvidenceError(
            "generation schema is outside the exact 0012 authority"
        )
    authority = {
        "owner": expected_owner,
        "acl": expected_acl,
        "comments": [],
        "security_labels": [],
        "default_acl": [],
        "initial_privileges": [],
        "publications": [],
        "unapproved_dependents": [],
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
    expected_postgres_major: int = POSTGRES_MAJOR,
    allow_online_admin: bool = False,
    trusted_startup: Mapping[str, object] | None = None,
    expected_role_contract_sha256: str | None = None,
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
    role_memberships = database.get("role_memberships")
    incoming_memberships = database.get("role_incoming_memberships")
    role_settings = database.get("role_settings")
    role_owned_objects = database.get("role_owned_objects")
    role_direct_acl = database.get("role_direct_acl")
    role_default_acl = database.get("role_default_acl")
    event_triggers = database.get("event_triggers")
    effective_write = database.get(
        "role_effective_persistent_write"
    )
    role_contract_marker = database.get("role_contract_marker")
    role_contract_sha256: str | None = None
    if not isolated and not allow_online_admin:
        prefix = ROLE_CONTRACT_POLICY + ":"
        if (
            not isinstance(role_contract_marker, str)
            or not role_contract_marker.startswith(prefix)
            or DIGEST_RE.fullmatch(
                role_contract_marker.removeprefix(prefix)
            )
            is None
            or expected_role_contract_sha256 is not None
            and role_contract_marker
            != prefix + expected_role_contract_sha256
        ):
            raise MediaEvidenceError(
                "online database audit role contract marker differs"
            )
        role_contract_sha256 = role_contract_marker.removeprefix(
            prefix
        )
    else:
        role_contract_marker = None
    config_errors = database.get("config_errors")
    data_directory = database.get("data_directory")
    startup_settings: Mapping[str, object] = database
    configuration_files: list[dict[str, str]] | None = None
    if trusted_startup is not None:
        raw_startup_settings = trusted_startup.get("settings")
        raw_configuration_files = trusted_startup.get(
            "configuration_files"
        )
        if (
            not isinstance(raw_startup_settings, dict)
            or set(raw_startup_settings)
            != set(TRUSTED_SERVER_STARTUP_SETTINGS)
            or any(
                not isinstance(value, str)
                for value in raw_startup_settings.values()
            )
            or not isinstance(
                trusted_startup.get("configuration_tree_sha256"),
                str,
            )
            or DIGEST_RE.fullmatch(
                str(trusted_startup["configuration_tree_sha256"])
            )
            is None
            or trusted_startup.get("pgdata") != data_directory
            or not isinstance(raw_configuration_files, list)
            or any(
                not isinstance(value, dict)
                or set(value) != {"path", "sha256"}
                or not isinstance(value["path"], str)
                or not isinstance(value["sha256"], str)
                or DIGEST_RE.fullmatch(value["sha256"]) is None
                for value in raw_configuration_files
            )
        ):
            raise MediaEvidenceError(
                "database startup settings differ from the independent "
                "read-only parser"
            )
        startup_settings = raw_startup_settings
        configuration_files = [
            {
                "path": str(value["path"]),
                "sha256": str(value["sha256"]),
            }
            for value in raw_configuration_files
        ]
        if (
            configuration_files
            != sorted(configuration_files, key=canonical_json_bytes)
            or len(
                {
                    value["path"]
                    for value in configuration_files
                }
            )
            != len(configuration_files)
        ):
            raise MediaEvidenceError(
                "database startup configuration inventory is ambiguous"
            )
        config_source_files: object = [
            value["path"] for value in configuration_files
        ]
    else:
        config_source_files = database.get("config_source_files")
    config_paths = {
        key: startup_settings.get(key)
        for key in ("config_file", "hba_file", "ident_file")
    }
    command_execution_settings = {
        key: startup_settings.get(key)
        for key in (
            "archive_mode",
            "archive_command",
            "archive_cleanup_command",
            "restore_command",
            "recovery_end_command",
            "ssl_passphrase_command",
            "ssl_passphrase_command_supports_reload",
            "jit_provider",
        )
    }
    if (
        database.get("jit") is not False
        or startup_settings.get("shared_preload_libraries") != ""
        or startup_settings.get("session_preload_libraries") != ""
        or startup_settings.get("local_preload_libraries") != ""
        or startup_settings.get("dynamic_library_path") != "$libdir"
        or command_execution_settings
        != {
            "archive_mode": "off",
            "archive_command": "",
            "archive_cleanup_command": "",
            "restore_command": "",
            "recovery_end_command": "",
            "ssl_passphrase_command": "",
            "ssl_passphrase_command_supports_reload": "off",
            "jit_provider": "llvmjit",
        }
        or not isinstance(data_directory, str)
        or not PurePosixPath(data_directory).is_absolute()
        or any(
            not isinstance(value, str)
            or not PurePosixPath(value).is_absolute()
            for value in config_paths.values()
        )
        or not isinstance(config_source_files, list)
        or config_source_files != sorted(set(config_source_files))
        or any(
            not isinstance(value, str)
            or not PurePosixPath(value).is_absolute()
            or PurePosixPath(data_directory)
            not in PurePosixPath(value).parents
            or (
                not value.endswith(".conf")
                and not value.endswith("/postgresql.auto.conf")
            )
            for value in config_source_files
        )
        or config_paths["config_file"] not in config_source_files
        or config_errors != []
    ):
        raise MediaEvidenceError(
            "database startup configuration is unsafe"
        )
    canonical_string_lists = (
        role_memberships,
        incoming_memberships,
        role_settings,
        role_owned_objects,
        effective_write,
    )
    if (
        database.get("database") != expected_database
        or database.get("current_user") != expected_user
        or database.get("transaction_read_only") is not True
        or database.get("statement_timeout") != "5min"
        or database.get("lock_timeout") != "5s"
        or database.get("search_path") != "pg_catalog"
        or database.get("row_security") is not True
        or database.get("event_triggers_disabled")
        is not (True if expected_postgres_major >= 17 else None)
        or database.get("role_superuser")
        is not (isolated or allow_online_admin)
        or not isinstance(database.get("role_create_db"), bool)
        or not isinstance(database.get("role_create_role"), bool)
        or not isinstance(database.get("role_replication"), bool)
        or not isinstance(database.get("role_bypass_rls"), bool)
        or not isinstance(database.get("role_inherit"), bool)
        or not isinstance(database.get("role_can_login"), bool)
        or not isinstance(role_memberships, list)
        or any(
            not isinstance(current, list)
            or current != sorted(set(current))
            or any(not isinstance(item, str) for item in current)
            for current in canonical_string_lists
        )
        or any(
            not _valid_pg_identifier(role)
            for role in role_memberships
        )
        or any(
            not _valid_pg_identifier(role)
            for role in incoming_memberships
        )
        or not isinstance(role_direct_acl, list)
        or not isinstance(role_default_acl, list)
        or event_triggers != []
        or not isinstance(database.get("system_identifier"), str)
        or PG_SYSTEM_ID_RE.fullmatch(database["system_identifier"]) is None
        or isinstance(server_version_num, bool)
        or not isinstance(server_version_num, int)
        or server_version_num // 10000 != expected_postgres_major
        or not isinstance(database.get("data_directory"), str)
        or not PurePosixPath(database["data_directory"]).is_absolute()
    ):
        raise MediaEvidenceError("database audit identity or read-only role differs")
    normalized_direct_acl: list[dict[str, object]] = []
    for acl in role_direct_acl:
        if (
            not isinstance(acl, dict)
            or set(acl)
            != {
                "object_kind",
                "object_name",
                "privilege",
                "grantable",
            }
            or acl.get("object_kind")
            not in {
                "database",
                "schema",
                "relation",
                "column",
                "function",
                "type",
                "language",
                "foreign-data-wrapper",
                "foreign-server",
                "tablespace",
                "large-object",
                "parameter",
            }
            or not isinstance(acl.get("object_name"), str)
            or not acl["object_name"]
            or not isinstance(acl.get("privilege"), str)
            or not acl["privilege"]
            or not isinstance(acl.get("grantable"), bool)
        ):
            raise MediaEvidenceError(
                "database audit role direct ACL is malformed"
            )
        normalized_direct_acl.append(dict(acl))
    if normalized_direct_acl != sorted(
        normalized_direct_acl,
        key=canonical_json_bytes,
    ):
        raise MediaEvidenceError(
            "database audit role direct ACL is not canonical"
        )
    normalized_default_acl: list[dict[str, object]] = []
    for acl in role_default_acl:
        if (
            not isinstance(acl, dict)
            or set(acl)
            != {
                "owner",
                "namespace",
                "object_type",
                "privilege",
                "grantable",
            }
            or not _valid_pg_identifier(acl.get("owner"))
            or acl.get("namespace") is not None
            and not _valid_pg_identifier(acl.get("namespace"))
            or not isinstance(acl.get("object_type"), str)
            or not isinstance(acl.get("privilege"), str)
            or not isinstance(acl.get("grantable"), bool)
        ):
            raise MediaEvidenceError(
                "database audit role default ACL is malformed"
            )
        normalized_default_acl.append(dict(acl))
    if normalized_default_acl != sorted(
        normalized_default_acl,
        key=canonical_json_bytes,
    ):
        raise MediaEvidenceError(
            "database audit role default ACL is not canonical"
        )
    if not isolated and not allow_online_admin and (
        database["role_superuser"]
        or database["role_create_db"]
        or database["role_create_role"]
        or database["role_replication"]
        or database["role_bypass_rls"]
        or database["role_inherit"]
        or database["role_can_login"]
        or database["role_memberships"]
        or incoming_memberships
        or role_owned_objects
        or role_default_acl
        or effective_write
        or role_settings
        != [
            "default_transaction_read_only=on",
            "lock_timeout=5s",
            "statement_timeout=5min",
        ]
    ):
        raise MediaEvidenceError("online database audit role is privileged")
    ledger_record = values["ledger"]
    ledger = ledger_record.get("rows")
    relation = ledger_record.get("relation")
    legacy = values["legacy_relation"]
    generation_schema = legacy.get("generation_schema")
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
    if legacy["present"] != (generation_schema is not None):
        raise MediaEvidenceError(
            "generation schema and legacy relation evidence differ"
        )
    if not isolated and not allow_online_admin:
        allowed_acl = [
            {
                "object_kind": "database",
                "object_name": expected_database,
                "privilege": "CONNECT",
                "grantable": False,
            },
            {
                "object_kind": "function",
                "object_name": "pg_catalog.pg_control_system()",
                "privilege": "EXECUTE",
                "grantable": False,
            },
        ]
        if relation is not None:
            allowed_acl.extend(
                [
                    {
                        "object_kind": "relation",
                        "object_name": "governance.schema_migrations",
                        "privilege": "SELECT",
                        "grantable": False,
                    },
                    {
                        "object_kind": "schema",
                        "object_name": "governance",
                        "privilege": "USAGE",
                        "grantable": False,
                    },
                ]
            )
        if legacy["present"]:
            allowed_acl.extend(
                [
                    {
                        "object_kind": "relation",
                        "object_name": "generation.polytao_jobs",
                        "privilege": "SELECT",
                        "grantable": False,
                    },
                    {
                        "object_kind": "schema",
                        "object_name": "generation",
                        "privilege": "USAGE",
                        "grantable": False,
                    },
                ]
            )
        if normalized_direct_acl != sorted(
            allowed_acl,
            key=canonical_json_bytes,
        ):
            raise MediaEvidenceError(
                "online database audit role direct ACL differs "
                f"(observed={normalized_direct_acl!r}, "
                f"expected={sorted(allowed_acl, key=canonical_json_bytes)!r})"
            )
    ledger_authority = (
        _validated_relation_authority(
            relation,
            expected_owner=str(database["database_owner"]),
            expected_reader=(
                None
                if isolated or allow_online_admin
                else str(database["current_user"])
            ),
            legacy_relation=False,
        )
        if relation is not None
        else None
    )
    legacy_authority = (
        _validated_relation_authority(
            legacy_relation,
            expected_owner=str(database["database_owner"]),
            expected_reader=(
                None
                if isolated or allow_online_admin
                else str(database["current_user"])
            ),
            legacy_relation=True,
        )
        if legacy_relation is not None
        else None
    )
    generation_schema_authority = (
        _validated_generation_schema_authority(
            generation_schema,
            expected_owner=str(database["database_owner"]),
            expected_reader=(
                None
                if isolated or allow_online_admin
                else str(database["current_user"])
            ),
        )
        if generation_schema is not None
        else None
    )
    if migration_scope == "auto-detect-adjacent":
        migration_scope = (
            "nexpoly-ledger"
            if relation is not None
            or ledger != []
            or legacy["present"]
            else "adjacent-record-only"
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
        "server_startup": {
            "jit": False,
            "shared_preload_libraries": "",
            "session_preload_libraries": "",
            "local_preload_libraries": "",
            "dynamic_library_path": "$libdir",
            **command_execution_settings,
            "config_file": config_paths["config_file"],
            "hba_file": config_paths["hba_file"],
            "ident_file": config_paths["ident_file"],
            "config_source_files": list(config_source_files),
            "config_errors": [],
            "independent_configuration_tree_sha256": (
                trusted_startup["configuration_tree_sha256"]
                if trusted_startup is not None
                else None
            ),
            "verification": (
                "pinned-read-only-config-parse-v1"
                if trusted_startup is not None
                else "owned-isolated-cluster-v1"
            ),
        },
        "event_triggers_disabled": database[
            "event_triggers_disabled"
        ],
        "role_superuser": database["role_superuser"],
        "role_create_db": database["role_create_db"],
        "role_create_role": database["role_create_role"],
        "role_replication": database["role_replication"],
        "role_bypass_rls": database["role_bypass_rls"],
        "role_inherit": database["role_inherit"],
        "role_can_login": database["role_can_login"],
        "role_contract_marker": role_contract_marker,
        "role_contract_sha256": role_contract_sha256,
        "role_memberships": list(database["role_memberships"]),
        "role_incoming_memberships": list(incoming_memberships),
        "role_settings": list(role_settings),
        "role_owned_objects": list(role_owned_objects),
        "role_direct_acl": normalized_direct_acl,
        "role_default_acl": normalized_default_acl,
        "event_triggers": [],
        "role_effective_persistent_write": list(effective_write),
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
        "generation_schema": {
            "state": "present" if generation_schema is not None else "absent",
            "schema_sha256": (
                generation_schema_authority["authority_sha256"]
                if generation_schema_authority is not None
                else None
            ),
            "schema_authority": (
                generation_schema_authority["authority"]
                if generation_schema_authority is not None
                else None
            ),
        },
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


def _parse_database_inventory(
    payload: bytes,
    *,
    expected_admin_role: str | None = None,
) -> list[dict[str, object]]:
    if len(payload) > MAX_DATABASE_JSON_BYTES:
        raise MediaEvidenceError("database inventory output exceeds its limit")
    values: list[dict[str, object]] | None = None
    online_admin: dict[str, object] | None = None
    for raw_line in payload.decode("utf-8", "strict").splitlines():
        if not raw_line.startswith("{"):
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise MediaEvidenceError(
                "database inventory emitted malformed JSON"
            ) from exc
        if not isinstance(value, dict):
            raise MediaEvidenceError("database inventory record is invalid")
        if value.get("record_type") == "online_admin":
            if (
                online_admin is not None
                or set(value)
                != {
                    "record_type",
                    "session_user",
                    "current_user",
                    "role_superuser",
                    "role_can_login",
                }
            ):
                raise MediaEvidenceError(
                    "database inventory administrator record is invalid"
                )
            online_admin = value
            continue
        if (
            value.get("record_type") != "database_inventory"
            or values is not None
            or set(value) != {"record_type", "databases"}
            or not isinstance(value.get("databases"), list)
        ):
            raise MediaEvidenceError("database inventory record is invalid")
        values = value["databases"]
    if values is None:
        raise MediaEvidenceError("database inventory output is incomplete")
    if expected_admin_role is None:
        if online_admin is not None:
            raise MediaEvidenceError(
                "isolated database inventory unexpectedly emitted online "
                "administrator evidence"
            )
    elif (
        not _valid_pg_identifier(expected_admin_role)
        or online_admin is None
        or online_admin.get("session_user") != expected_admin_role
        or online_admin.get("current_user") != expected_admin_role
        or online_admin.get("role_superuser") is not True
        or online_admin.get("role_can_login") is not True
    ):
        raise MediaEvidenceError(
            "database inventory administrator differs from POSTGRES_USER"
        )
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
            or not _valid_pg_identifier(record.get("name"))
            or record["name"] in names
            or not isinstance(record.get("oid"), str)
            or not record["oid"].isdigit()
            or not _valid_pg_identifier(record.get("owner"))
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
    *,
    online_admin_role: str | None = None,
    postgres_major: int = POSTGRES_MAJOR,
    use_trusted_client: bool = False,
    trusted_image_id: str | None = None,
) -> list[dict[str, object]]:
    connect_user = online_admin_role or "postgres"
    if not _valid_pg_identifier(connect_user):
        raise MediaEvidenceError(
            "database inventory connection role is invalid"
        )
    psql_arguments: list[str] = []
    if online_admin_role is not None:
        psql_arguments.extend(["-v", "online_admin_expected=1"])
    if use_trusted_client:
        if (
            not isinstance(trusted_image_id, str)
            or DIGEST_RE.fullmatch(trusted_image_id) is None
        ):
            raise MediaEvidenceError(
                "trusted PostgreSQL inventory lacks its exact client image ID"
            )
        inspected_user = _online_container_admin_role(
            runner,
            container_id,
        )
        if inspected_user != connect_user:
            raise MediaEvidenceError(
                "trusted PostgreSQL client user differs from container authority"
            )
        psql_arguments.extend(["-U", connect_user, "-d", "postgres"])
        completed = _run_trusted_psql(
            runner,
            container_id=container_id,
            postgres_major=postgres_major,
            pgoptions=_psql_audit_pgoptions(postgres_major),
            arguments=psql_arguments,
            input_bytes=_database_inventory_sql_for_major(
                postgres_major
            ).encode("utf-8"),
            timeout=600,
            expected_image_id=trusted_image_id,
        )
    else:
        command = [
            DOCKER,
            "exec",
            "--user",
            "postgres",
            "--env",
            f"PGOPTIONS={_psql_audit_pgoptions(postgres_major)}",
            "-i",
            container_id,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            *psql_arguments,
            "-h",
            "/var/run/postgresql",
            "-U",
            connect_user,
            "-d",
            "postgres",
        ]
        completed = runner.run(
            command,
            input_bytes=_database_inventory_sql_for_major(
                postgres_major
            ).encode("utf-8"),
            timeout=600,
        )
    return _parse_database_inventory(
        completed.stdout,
        expected_admin_role=online_admin_role,
    )


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
        raise MediaEvidenceError(
            "every non-template database requires a complete audit"
        )
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
    derive_inventory: bool = False,
    trusted_image_id: str | None = None,
    expected_role_contracts: Mapping[str, str] | None = None,
) -> dict[str, object]:
    # Direct unit-level orchestration tests construct descriptors without the
    # v3 authority. Registry-loaded production calls can never take this
    # compatibility branch because load_registry requires a full inventory.
    if not descriptor.databases and not derive_inventory:
        primary = _audit_container_database(
            runner,
            container_id,
            descriptor,
            isolated=isolated,
            trusted_image_id=trusted_image_id,
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
    if not descriptor.databases:
        observed = _container_database_inventory(
            runner,
            container_id,
            online_admin_role=descriptor.database_user,
            postgres_major=int(
                descriptor.source_postgres_major or POSTGRES_MAJOR
            ),
        )
        if (
            not isolated
            or not observed
            or any(record["allow_connections"] is not True for record in observed)
        ):
            raise MediaEvidenceError(
                "dynamic database inventory requires an isolated, fully "
                "connectable source copy"
            )
        dynamic_authority = [
            {
                **record,
                "audit_role": descriptor.database_user,
                "migration_scope": "nexpoly-ledger",
            }
            for record in observed
        ]
        audited: list[dict[str, object]] = []
        primary: dict[str, object] | None = None
        for authority in dynamic_authority:
            current = _audit_container_database(
                runner,
                container_id,
                descriptor,
                database_authority=authority,
                isolated=True,
            )
            audited.append(_database_inventory_record(authority, current))
            if authority["name"] == descriptor.database:
                primary = current
        if primary is None:
            raise MediaEvidenceError(
                "dynamic database inventory lacks its primary database"
            )
        return {
            **primary,
            "database_inventory": observed,
            "database_inventory_sha256": sha256_bytes(
                canonical_json_bytes(observed)
            ),
            "databases": audited,
        }
    observed = _container_database_inventory(
        runner,
        container_id,
        online_admin_role=(
            descriptor.online_admin_role
            if not isolated
            else descriptor.database_user
        ),
        postgres_major=int(
            descriptor.source_postgres_major or POSTGRES_MAJOR
        ),
        use_trusted_client=not isolated,
        trusted_image_id=trusted_image_id,
    )
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
        if authority["allow_connections"] is not True:
            raise MediaEvidenceError(
                "non-template database does not allow a complete audit: "
                f"{authority['name']}"
            )
        database_audit = _audit_container_database(
            runner,
            container_id,
            descriptor,
            database_authority=authority,
            isolated=isolated,
            trusted_image_id=trusted_image_id,
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
            primary = database_audit
    if primary is None:
        raise MediaEvidenceError("primary database audit is absent")
    if (
        not isolated
        and descriptor.audit_method == "live-read-only"
    ):
        if (
            not isinstance(trusted_image_id, str)
            or DIGEST_RE.fullmatch(trusted_image_id) is None
            or not _valid_pg_identifier(
                descriptor.online_admin_role
            )
        ):
            raise MediaEvidenceError(
                "online managed audit-role matrix lacks exact authority"
            )
        _managed_role_matrix(
            runner,
            container_id=container_id,
            databases=descriptor.databases,
            online_admin_role=str(descriptor.online_admin_role),
            postgres_major=int(
                descriptor.source_postgres_major or POSTGRES_MAJOR
            ),
            trusted_image_id=trusted_image_id,
            expected_contracts=expected_role_contracts,
            allow_missing=False,
        )
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
    *,
    trusted_image_id: str,
    expected_role_contracts: Mapping[str, str] | None = None,
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
    container_id = str(attachment["container_id"])
    online_admin_role = _online_container_admin_role(
        runner,
        container_id,
    )
    if online_admin_role != descriptor.online_admin_role:
        raise MediaEvidenceError(
            "online PostgreSQL POSTGRES_USER changed after registry derivation"
        )
    destination = PurePosixPath(str(attachment["destination"]))
    expected_pgdata = (
        destination
        if source.data_subpath == "."
        else destination.joinpath(*PurePosixPath(source.data_subpath).parts)
    )
    result = _audit_container_medium(
        runner,
        container_id,
        descriptor,
        isolated=False,
        expected_data_directory=str(expected_pgdata),
        trusted_image_id=trusted_image_id,
        expected_role_contracts=expected_role_contracts,
    )
    return result


def _current_attachments(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> list[dict[str, object]]:
    # Attachments belong to the immutable discovery epoch.  Re-inspecting all
    # containers once per medium is O(media * containers) and can splice
    # states from different moments.  The caller performs one complete Docker
    # snapshot CAS at the end of the epoch; all per-medium work consumes this
    # frozen projection.
    del runner
    return sorted(
        (dict(value) for value in source.attached),
        key=lambda item: (
            str(item["container_id"]),
            str(item["destination"]),
        ),
    )


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
    *,
    trusted_image_id: str,
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
    container_id = str(attachment["container_id"])
    postgres_major = source.postgres_major
    if postgres_major not in SUPPORTED_POSTGRES_AUDIT_MAJORS:
        raise MediaEvidenceError(
            "online source PostgreSQL major is unsupported"
        )
    connect_user = _online_container_admin_role(
        runner,
        container_id,
    )
    completed = _run_trusted_psql(
        runner,
        container_id=container_id,
        postgres_major=postgres_major,
        pgoptions=_psql_audit_pgoptions(postgres_major),
        arguments=[
            "-U",
            connect_user,
            "-d",
            "postgres",
            "-c",
            (
                "SELECT system_identifier::text "
                "FROM pg_catalog.pg_control_system();"
            ),
        ],
        timeout=60,
        expected_image_id=trusted_image_id,
    )
    match = re.search(
        rb"(?m)^([0-9]{1,20})\s*$",
        completed.stdout,
    )
    if match is None:
        raise MediaEvidenceError(
            "online PostgreSQL control-system identity is unavailable"
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


def _reviewed_bind_identity(
    runner: CommandRunner,
    source: DiscoveredMedia,
) -> dict[str, object]:
    if source.kind != "container_bind":
        raise MediaEvidenceError(
            "reviewed bind identity requires a container bind"
        )
    original = Path(source.locator)
    path = Path(source.audit_locator or source.locator)
    try:
        descriptor = _open_bind_source_without_symlinks(path)
    except OSError as exc:
        raise MediaEvidenceError(
            "reviewed container bind is unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not (
            stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISREG(metadata.st_mode)
        )
        or source.takeover_redirect is None
        and (
            metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink != 1
        )
    ):
        raise MediaEvidenceError(
            "reviewed container bind must be an owner-controlled "
            "non-writable regular file or directory"
        )
    identity = {
        "path": str(original),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "data_subpath": ".",
        "attached": _current_attachments(runner, source),
    }
    if source.takeover_redirect is not None:
        identity["audit_path"] = str(path)
        identity["takeover_redirect"] = dict(
            source.takeover_redirect
        )
    return identity


def _review_non_postgres_bind(
    registry: Registry,
    source: DiscoveredMedia,
    *,
    runner: CommandRunner,
    operation: ScratchOperation,
    resource_prefix: str,
) -> dict[str, object]:
    del registry, operation, resource_prefix
    if (
        source.kind != "container_bind"
        or source.signature != "non-postgres"
        or source.postgres_major is not None
    ):
        raise MediaEvidenceError(
            "non-PostgreSQL bind review has a conflicting signature"
        )
    before = _reviewed_bind_identity(runner, source)
    audit_path = Path(source.audit_locator or source.locator)
    first = _scan_reviewed_bind_tree(
        audit_path,
        expected_takeover_seal=source.takeover_seal,
    )
    second = _scan_reviewed_bind_tree(
        audit_path,
        expected_takeover_seal=source.takeover_seal,
    )
    after = _reviewed_bind_identity(runner, source)
    if (
        before != after
        or first != second
    ):
        raise MediaEvidenceError(
            "non-PostgreSQL bind changed during reviewed-content audit"
        )
    if (
        first["signature"] != "non-postgres"
        or first["postgres_major"] is not None
        or first["contains_backup_material"] is not False
    ):
        raise MediaEvidenceError(
            "non-PostgreSQL bind contains PostgreSQL or backup material"
        )
    return {
        "media_id": source.media_id,
        "source_identity_sha256": sha256_bytes(
            canonical_json_bytes(before)
        ),
        "source_content_sha256": first["source_content_sha256"],
        "file_count": first["file_count"],
        "size_bytes": first["size_bytes"],
        "review_algorithm": "private-reviewed-content-inventory-v1",
    }


def _excluded_non_pg_identity(
    source: DiscoveredMedia,
) -> dict[str, object]:
    """Build a metadata-only exclusion identity without reading content."""

    probe = source.classification_probe
    if (
        source.signature != "non-postgres"
        or source.postgres_major is not None
        or not isinstance(probe, dict)
        or probe.get("result")
        not in {
            "no-postgres-markers",
            "no-container-pgdata-mapping",
            "trusted-non-postgres-active-reader",
        }
        or probe.get("content_read_scope")
        not in {"none", "PG_VERSION-up-to-32-bytes"}
    ):
        raise MediaEvidenceError(
            "metadata-only exclusion lacks a bounded non-PG probe"
        )
    common = {
        "locator": source.locator,
        "data_subpath": source.data_subpath,
        "attached": [dict(value) for value in source.attached],
        "classification_probe": dict(probe),
    }
    if source.kind == "docker_volume":
        if (
            not isinstance(source.docker_inspect_sha256, str)
            or DIGEST_RE.fullmatch(source.docker_inspect_sha256) is None
        ):
            raise MediaEvidenceError(
                "excluded Docker volume lacks its inspect digest"
            )
        return {
            **common,
            "docker_inspect_sha256": source.docker_inspect_sha256,
        }
    if source.kind != "container_bind":
        raise MediaEvidenceError(
            "metadata-only exclusion requires a Docker volume or bind"
        )
    path = Path(source.audit_locator or source.locator)
    if not path.is_absolute() or path == Path("/"):
        raise MediaEvidenceError(
            "excluded container bind cannot be the filesystem root"
        )
    try:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            metadata = None
        resolved_parent = path.parent.resolve(strict=True)
        parent_metadata = os.stat(resolved_parent)
    except FileNotFoundError:
        metadata = None
        resolved_parent = path.parent.resolve(strict=False)
        parent_metadata = None
    if metadata is None:
        if source.takeover_redirect is not None:
            raise MediaEvidenceError(
                "takeover redirect destination disappeared"
            )
        return {
            **common,
            "audit_locator": str(path),
            "resolved_parent": str(resolved_parent),
            "root_state": "absent",
            "transient_runtime": True,
        }
    entry_type = (
        "directory"
        if stat.S_ISDIR(metadata.st_mode)
        else "file"
        if stat.S_ISREG(metadata.st_mode)
        else "unix-socket"
        if stat.S_ISSOCK(metadata.st_mode)
        else "fifo"
        if stat.S_ISFIFO(metadata.st_mode)
        else "block-device"
        if stat.S_ISBLK(metadata.st_mode)
        else "character-device"
        if stat.S_ISCHR(metadata.st_mode)
        else "symlink"
        if stat.S_ISLNK(metadata.st_mode)
        else "unknown-special"
    )
    identity = {
        **common,
        "audit_locator": str(path),
        "resolved_parent": str(resolved_parent),
        "parent_device": (
            parent_metadata.st_dev
            if parent_metadata is not None
            else None
        ),
        "parent_inode": (
            parent_metadata.st_ino
            if parent_metadata is not None
            else None
        ),
        "entry_type": entry_type,
        "transient_runtime": entry_type not in {"directory", "file"},
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }
    if entry_type == "symlink":
        identity["symlink_target"] = os.readlink(path)
    if source.takeover_redirect is not None:
        seal = source.takeover_seal
        if (
            not isinstance(seal, dict)
            or not isinstance(seal.get("records"), list)
            or not seal["records"]
            or seal["records"][0].get("path") != "."
        ):
            raise MediaEvidenceError(
                "takeover metadata exclusion lacks its move seal"
            )
        root = seal["records"][0]
        expected_type = (
            "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "file"
        )
        if (
            root.get("type") != expected_type
            or root.get("mode")
            != f"{stat.S_IMODE(metadata.st_mode):04o}"
            or root.get("uid") != metadata.st_uid
            or root.get("gid") != metadata.st_gid
        ):
            raise MediaEvidenceError(
                "takeover bind root metadata differs from its move seal"
            )
        identity["takeover_redirect"] = dict(
            source.takeover_redirect
        )
        identity["takeover_move_seal_sha256"] = seal.get("digest")
    return identity


def _non_postgres_volume_screen(
    runner: CommandRunner,
    image: str,
    source: DiscoveredMedia,
    *,
    operation: ScratchOperation,
    resource_key: str,
) -> dict[str, int]:
    """Reject database/backup signatures and bound a non-PG content walk."""

    if (
        source.kind != "docker_volume"
        or source.signature != "non-postgres"
        or source.postgres_major is not None
        or any(
            str(value["state"]) in ACTIVE_CONTAINER_STATES
            for value in source.attached
        )
        or "postgres" in source.locator.lower()
    ):
        raise MediaEvidenceError(
            "non-PostgreSQL content review requires one inactive, "
            "non-Postgres-named Docker volume"
        )
    maximum_files = int(
        LOGICAL_MEDIA_POLICY["non_postgres"]["maximum_files"]  # type: ignore[index]
    )
    maximum_bytes = int(
        LOGICAL_MEDIA_POLICY["non_postgres"]["maximum_bytes"]  # type: ignore[index]
    )
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
                "test -z \"$(find /source -xdev "
                "! -type d ! -type f -print -quit)\"; "
                "find /source -xdev -type f -print0 | "
                "while IFS= read -r -d '' path; do "
                "case \"$path\" in "
                "*/PG_VERSION|*/global/pg_control|*.backup|*.dump|*.tar) "
                "exit 91;; esac; "
                "magic=\"$(head -c 5 \"$path\" | od -An -tx1 | "
                "tr -d ' \\n')\"; "
                "test \"$magic\" != 5047444d50; "
                "tar_magic=\"$(dd if=\"$path\" bs=1 skip=257 count=5 "
                "2>/dev/null | od -An -tx1 | tr -d ' \\n')\"; "
                "test \"$tar_magic\" != 7573746172; "
                "done; "
                "files=\"$(find /source -xdev -type f -print0 | "
                "tr -cd '\\000' | wc -c)\"; "
                "bytes=\"$(find /source -xdev -type f -exec stat -c '%s' "
                "{} + | awk '{total += $1} END {print total + 0}')\"; "
                f"test \"$files\" -le {maximum_files}; "
                f"test \"$bytes\" -le {maximum_bytes}; "
                "printf '%s\\n%s\\n' \"$files\" \"$bytes\""
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
    lines = completed.stdout.decode("ascii", "strict").splitlines()
    if (
        len(lines) != 2
        or any(not value.isdigit() for value in lines)
        or int(lines[0]) > maximum_files
        or int(lines[1]) > maximum_bytes
    ):
        raise MediaEvidenceError(
            "non-PostgreSQL content review summary is invalid"
        )
    return {"file_count": int(lines[0]), "size_bytes": int(lines[1])}


def _review_non_postgres_volume(
    registry: Registry,
    source: DiscoveredMedia,
    *,
    runner: CommandRunner,
    operation: ScratchOperation,
    resource_prefix: str,
) -> dict[str, object]:
    image = _audit_image_for_major(registry, POSTGRES_MAJOR)
    before = _docker_volume_identity(runner, source)
    first_screen = _non_postgres_volume_screen(
        runner,
        image,
        source,
        operation=operation,
        resource_key=f"{resource_prefix}-screen-before",
    )
    first_digest = _volume_content_digest(
        runner,
        image,
        source,
        operation=operation,
        resource_key=f"{resource_prefix}-digest-before",
    )
    second_screen = _non_postgres_volume_screen(
        runner,
        image,
        source,
        operation=operation,
        resource_key=f"{resource_prefix}-screen-after",
    )
    second_digest = _volume_content_digest(
        runner,
        image,
        source,
        operation=operation,
        resource_key=f"{resource_prefix}-digest-after",
    )
    after = _docker_volume_identity(runner, source)
    if (
        before != after
        or first_screen != second_screen
        or first_digest != second_digest
    ):
        raise MediaEvidenceError(
            "non-PostgreSQL volume changed during reviewed-content audit"
        )
    return {
        "media_id": source.media_id,
        "source_identity_sha256": sha256_bytes(
            canonical_json_bytes(before)
        ),
        "source_content_sha256": first_digest,
        **first_screen,
        "review_algorithm": "private-reviewed-content-inventory-v1",
    }


def _reviewed_regular_identity(
    path: Path,
    *,
    root: Path,
    root_authority: object,
    maximum_bytes: int,
) -> dict[str, object]:
    """Stream one sealed regular file and CAS all identity fields."""

    descriptor = open_sealed_backup_regular(
        path,
        root=root,
        root_authority=root_authority,
    )
    try:
        before = os.fstat(descriptor)
        if before.st_size > maximum_bytes:
            raise MediaEvidenceError(
                "private file exceeds its reviewed-content size limit"
            )
        identity = _fd_identity(
            descriptor,
            path,
            include_digest=True,
            maximum_bytes=maximum_bytes,
        )
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_tuple = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        stat.S_IMODE(before.st_mode),
        before.st_uid,
        before.st_nlink,
    )
    after_tuple = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        stat.S_IMODE(after.st_mode),
        after.st_uid,
        after.st_nlink,
    )
    if before_tuple != after_tuple:
        raise MediaEvidenceError(
            "reviewed non-PG file changed during streaming content audit"
        )
    reopened = open_sealed_backup_regular(
        path,
        root=root,
        root_authority=root_authority,
    )
    try:
        reopened_metadata = os.fstat(reopened)
    finally:
        os.close(reopened)
    reopened_tuple = (
        reopened_metadata.st_dev,
        reopened_metadata.st_ino,
        reopened_metadata.st_size,
        reopened_metadata.st_mtime_ns,
        reopened_metadata.st_ctime_ns,
        stat.S_IMODE(reopened_metadata.st_mode),
        reopened_metadata.st_uid,
        reopened_metadata.st_nlink,
    )
    if reopened_tuple != after_tuple:
        raise MediaEvidenceError(
            "reviewed non-PG file was replaced after content audit"
        )
    return identity


def _review_non_postgres_file(
    registry: Registry,
    source: DiscoveredMedia,
    *,
    policy: DiscoveryPolicy,
) -> dict[str, object]:
    """Double-open and stream-CAS one private, non-database regular file."""

    if (
        source.kind != "reviewed_file"
        or source.signature != "non-postgres"
        or source.postgres_major is not None
        or source.attached
    ):
        raise MediaEvidenceError(
            "reviewed regular file has a conflicting media identity"
        )
    path = Path(source.locator)
    backup_root = _find_backup_root(path, policy)
    root_authority = _sealed_root_authority(registry, backup_root)
    maximum_bytes = int(
        LOGICAL_MEDIA_POLICY["non_postgres"][  # type: ignore[index]
            "maximum_single_file_bytes"
        ]
    )
    header_descriptor = open_sealed_backup_regular(
        path,
        root=backup_root,
        root_authority=root_authority,
    )
    try:
        header = os.read(header_descriptor, 512)
    finally:
        os.close(header_descriptor)
    if (
        path.name in {"PG_VERSION", "pg_control"}
        or header.startswith(b"PGDMP")
        or len(header) >= 262
        and header[257:262] == b"ustar"
    ):
        raise MediaEvidenceError(
            "reviewed file contains PostgreSQL or backup material"
        )
    first = _reviewed_regular_identity(
        path,
        root=backup_root,
        root_authority=root_authority,
        maximum_bytes=maximum_bytes,
    )
    second = _reviewed_regular_identity(
        path,
        root=backup_root,
        root_authority=root_authority,
        maximum_bytes=maximum_bytes,
    )
    if first != second:
        raise MediaEvidenceError(
            "reviewed non-PG file changed between content CAS passes"
        )
    content_digest = str(first["sha256"])
    identity = {
        key: value
        for key, value in first.items()
        if key != "sha256"
    }
    return {
        "media_id": source.media_id,
        "source_identity_sha256": sha256_bytes(
            canonical_json_bytes(identity)
        ),
        "source_content_sha256": content_digest,
        "file_count": 1,
        "size_bytes": int(first["size_bytes"]),
        "review_algorithm": "private-reviewed-content-inventory-v1",
    }


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
    postgres_major: int,
    hba_file: str | None = None,
) -> list[str]:
    if postgres_major not in SUPPORTED_POSTGRES_AUDIT_MAJORS:
        raise MediaEvidenceError("isolated PostgreSQL major is unsupported")
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
    if postgres_major >= 17:
        values.extend(["-c", "event_triggers=false"])
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
    trusted_image_id: str | None = None,
    expected_role_contract_sha256: str | None = None,
) -> dict[str, object]:
    trusted_startup: dict[str, object] | None = (
        {} if not isolated else None
    )
    adjacent_admin = (
        descriptor.audit_method == "live-read-only-adjacent"
        and descriptor.classification == "adjacent-postgres"
    )
    if database_authority is None:
        database_name = descriptor.database
        audit_role = descriptor.database_user
        migration_scope = "nexpoly-ledger"
    else:
        if (
            not isinstance(trusted_image_id, str)
            or DIGEST_RE.fullmatch(trusted_image_id) is None
        ):
            raise MediaEvidenceError(
                "online database audit lacks its exact client image ID"
            )
        database_name = str(database_authority["name"])
        audit_role = database_authority.get("audit_role")
        migration_scope = str(database_authority["migration_scope"])
        if (
            not isinstance(audit_role, str)
            or (
                adjacent_admin
                and audit_role != descriptor.online_admin_role
                or not adjacent_admin
                and ROLE_RE.fullmatch(audit_role) is None
            )
        ):
            raise MediaEvidenceError(
                "connectable database lacks an approved audit role"
            )
    connect_user = (
        audit_role
        if isolated
        else descriptor.online_admin_role
    )
    if (
        not _valid_pg_identifier(connect_user)
    ):
        raise MediaEvidenceError(
            "online database audit lacks its inspected administrator role"
        )
    audit_major = descriptor.source_postgres_major or POSTGRES_MAJOR
    sql = _database_audit_sql_for_major(
        audit_major,
        external_startup_projection=not isolated,
    )
    if not isolated and not adjacent_admin:
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
    psql_arguments = [
        "-U",
        connect_user,
        "-d",
        database_name,
    ]
    if isolated:
        command = [
            DOCKER,
            "exec",
            "--user",
            "postgres",
            "--env",
            f"PGOPTIONS={_psql_audit_pgoptions(audit_major)}",
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
            *psql_arguments,
        ]
        command_environment = None
    else:
        inspected_user = _online_container_admin_role(
            runner,
            container_id,
        )
        if inspected_user != connect_user:
            raise MediaEvidenceError(
                "online database administrator differs from connection "
                "authority"
            )
        completed = _run_trusted_psql(
            runner,
            container_id=container_id,
            postgres_major=audit_major,
            pgoptions=_psql_audit_pgoptions(audit_major),
            arguments=psql_arguments,
            input_bytes=sql.encode("utf-8"),
            timeout=600,
            expected_image_id=trusted_image_id,
            startup_sink=trusted_startup,
        )
    if isolated:
        completed = runner.run(
            command,
            input_bytes=sql.encode("utf-8"),
            timeout=600,
        )
    return _parse_database_audit(
        completed.stdout,
        expected_database=database_name,
        expected_user=audit_role,
        isolated=isolated,
        migration_scope=migration_scope,
        expected_postgres_major=(
            audit_major
        ),
        allow_online_admin=adjacent_admin,
        trusted_startup=trusted_startup,
        expected_role_contract_sha256=(
            expected_role_contract_sha256
        ),
    )


def _isolated_volume_audit(
    runner: CommandRunner,
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    *,
    operation: ScratchOperation,
    resource_prefix: str,
    derive_inventory: bool = False,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    source_major = descriptor.source_postgres_major
    if source_major is None:
        raise MediaEvidenceError(
            "physical PostgreSQL volume lacks its source major"
        )
    audit_image = _audit_image_for_major(registry, source_major)
    postgres_volume_root = (
        "/var/lib/postgresql"
        if source_major == 18
        else "/var/lib/postgresql/data"
    )
    data_path = (
        postgres_volume_root
        + (
            ""
            if source.data_subpath == "."
            else f"/{source.data_subpath}"
        )
    )
    hba_path = f"{data_path}/.nexpoly-audit-pg_hba.conf"
    before = _docker_volume_identity(runner, source)
    if any(
        str(value["state"]) in ACTIVE_CONTAINER_STATES
        for value in before["attached"]
    ):
        raise MediaEvidenceError("active volume cannot enter isolated audit")
    before_digest = _volume_content_digest(
        runner,
        audit_image,
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
                audit_image,
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
            media_id=media_id_for_locator(
                "docker_volume",
                clone,
            ),
            kind="docker_volume",
            locator=clone,
            data_subpath=source.data_subpath,
            attached=(),
            postgres_major=source_major,
        )
        if (
            _volume_content_digest(
                runner,
                audit_image,
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
                audit_image,
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
        operation.run_container(
            f"{resource_prefix}-hba",
            [
                DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={clone},dst={postgres_volume_root}",
                "--entrypoint",
                "/bin/sh",
                audit_image,
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
                    "destination": postgres_volume_root,
                    "read_only": False,
                },
            ),
            dependencies=(volume_key,),
            source_media_id=source.media_id,
            read_only_rootfs=False,
        )
        pgdata = data_path
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
                f"type=volume,src={clone},dst={postgres_volume_root}",
                "--env",
                f"PGDATA={pgdata}",
                audit_image,
                *_isolated_postgres_arguments(
                    pgdata=pgdata,
                    postgres_major=int(
                        descriptor.source_postgres_major
                        or POSTGRES_MAJOR
                    ),
                    hba_file=hba_path,
                ),
            ],
            mounts=(
                {
                    "kind": "volume",
                    "source": clone,
                    "destination": postgres_volume_root,
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
            derive_inventory=derive_inventory,
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
        audit_image,
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
    derive_inventory: bool = False,
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
            derive_inventory=derive_inventory,
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

    def stable_tuple(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_nlink,
        )

    def reopen_relative(
        relative: tuple[str, ...],
        *,
        directory: bool,
    ) -> os.stat_result:
        descriptor = os.dup(source_descriptor)
        try:
            for index, component in enumerate(relative):
                final = index == len(relative) - 1
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                if not final or directory:
                    flags |= os.O_DIRECTORY
                else:
                    flags |= getattr(os, "O_NONBLOCK", 0)
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)

    def copy_directory(
        input_descriptor: int,
        output: Path,
        relative: tuple[str, ...],
        *,
        expected_before: os.stat_result | None = None,
    ) -> None:
        directory_before = (
            expected_before
            if expected_before is not None
            else os.fstat(input_descriptor)
        )
        if (
            not stat.S_ISDIR(directory_before.st_mode)
            or directory_before.st_uid != os.geteuid()
            or directory_before.st_mode & 0o077
        ):
            raise MediaEvidenceError("bind PGDATA is not deploy-user-owned")
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
                    copy_directory(
                        child,
                        target,
                        (*relative, name),
                        expected_before=metadata,
                    )
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
                    after = os.fstat(child)
                    reopened = reopen_relative(
                        (*relative, name),
                        directory=False,
                    )
                    if (
                        stable_tuple(metadata) != stable_tuple(after)
                        or stable_tuple(after) != stable_tuple(reopened)
                    ):
                        raise MediaEvidenceError(
                            "bind PGDATA file changed while it was copied"
                        )
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
        directory_after = os.fstat(input_descriptor)
        directory_reopened = reopen_relative(
            relative,
            directory=True,
        )
        if (
            stable_tuple(directory_before) != stable_tuple(directory_after)
            or stable_tuple(directory_after)
            != stable_tuple(directory_reopened)
        ):
            raise MediaEvidenceError(
                "bind PGDATA directory changed while it was copied"
            )

    try:
        before = os.fstat(source_descriptor)
        copy_directory(
            source_descriptor,
            destination,
            (),
            expected_before=before,
        )
        after = os.fstat(source_descriptor)
    finally:
        os.close(source_descriptor)
    try:
        reopened_descriptor = _open_bind_source_without_symlinks(source)
    except OSError as exc:
        raise MediaEvidenceError(
            "bind PGDATA disappeared after it was copied"
        ) from exc
    try:
        reopened = os.fstat(reopened_descriptor)
    finally:
        os.close(reopened_descriptor)
    identity = {
        "path": str(source),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mtime_ns": before.st_mtime_ns,
        "ctime_ns": before.st_ctime_ns,
        "mode": stat.S_IMODE(before.st_mode),
        "uid": before.st_uid,
    }
    if (
        stable_tuple(before) != stable_tuple(after)
        or stable_tuple(after) != stable_tuple(reopened)
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
    derive_inventory: bool = False,
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
                    postgres_major=int(
                        descriptor.source_postgres_major
                        or POSTGRES_MAJOR
                    ),
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
            derive_inventory=derive_inventory,
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
        raise MediaEvidenceError(
            "pinned PostgreSQL audit image is not preloaded"
        )
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
    expected_major: int = POSTGRES_MAJOR,
    expected_image_id: str | None = None,
) -> str:
    value = _json_command(runner, [DOCKER, "image", "inspect", "--", image])
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise MediaEvidenceError("pinned PostgreSQL audit image is not preloaded")
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
        raise MediaEvidenceError(
            "local image does not expose the pinned PostgreSQL digest"
        )
    image_id = value[0].get("Id")
    if (
        not isinstance(image_id, str)
        or DIGEST_RE.fullmatch(image_id) is None
        or image_id
        != (
            expected_image_id
            if expected_image_id is not None
            else operation.authority["postgres_image_id"]
        )
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
    if re.search(
        rf"\b{expected_major}(?:\.[0-9]+)?\b",
        output,
    ) is None:
        raise MediaEvidenceError(
            "pinned audit image has the wrong PostgreSQL major"
        )
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
                "pg_controldata pg_isready psql awk dd head od tr wc timeout; do "
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
        _absolute_parts(path)
        before = os.lstat(path)
        attachments = _current_attachments(runner, source)
        after = os.lstat(path)
        stable = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_mtime_ns,
            value.st_ctime_ns,
            stat.S_IMODE(value.st_mode),
            value.st_uid,
            value.st_gid,
        )
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_mode & 0o077
            or stable(before) != stable(after)
        ):
            raise MediaEvidenceError(
                "active PostgreSQL bind is not one stable private directory"
            )
        return {
            "path": str(path),
            "device": before.st_dev,
            "inode": before.st_ino,
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
            "mode": stat.S_IMODE(before.st_mode),
            "uid": before.st_uid,
            "data_subpath": source.data_subpath,
            "attached": attachments,
        }
    raise MediaEvidenceError("live source must be a Docker volume or bind")


def _auditor_digest() -> str:
    path = Path(__file__)
    inherited = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(path))
    expected_inherited = os.environ.get("NEXPOLY_MEDIA_AUDITOR_SHA256")
    if inherited is not None:
        descriptor_number = int(inherited.group(1))
        if (
            descriptor_number < 3
            or not isinstance(expected_inherited, str)
            or DIGEST_RE.fullmatch(expected_inherited) is None
            or re.fullmatch(
                r"[0-9a-f]{64}",
                os.environ.get(
                    "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID",
                    "",
                ),
            )
            is None
        ):
            raise MediaEvidenceError(
                "auditor inherited control identity is unavailable"
            )
        try:
            descriptor = os.dup(descriptor_number)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            raise MediaEvidenceError(
                "auditor inherited control descriptor is unavailable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            payload = _read_fd(descriptor)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700
            or before.st_nlink != 1
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or sha256_bytes(payload) != expected_inherited
        ):
            raise MediaEvidenceError(
                "auditor inherited implementation identity differs"
            )
        return expected_inherited
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


def _metadata_only_exclusion_record(
    registry: Registry,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
    *,
    auditor_sha256: str,
    audited_at: str,
) -> dict[str, object]:
    if (
        descriptor.classification != "excluded-non-pg"
        or descriptor.audit_method != "metadata-only-exclusion"
        or source.kind not in {"docker_volume", "container_bind"}
    ):
        raise MediaEvidenceError(
            "metadata-only exclusion descriptor is invalid"
        )
    before = _excluded_non_pg_identity(source)
    after = _excluded_non_pg_identity(source)
    if before != after:
        raise MediaEvidenceError(
            "excluded non-PG metadata changed during evidence capture"
        )
    record = {
        "record_type": "excluded-non-pg-media",
        "media_id": descriptor.media_id,
        "kind": descriptor.kind,
        "classification": descriptor.classification,
        "disposition": descriptor.disposition,
        "source_identity_before": before,
        "source_identity_after": after,
        "postgres_signature": {
            "state": source.signature,
            "major": source.postgres_major,
            "data_subpath": source.data_subpath,
        },
        "readers": [dict(value) for value in source.attached],
        "excluded_from_nexpoly_migration": True,
        "audit": {
            "method": "metadata-only-exclusion",
            "complete": True,
            "auditor_sha256": auditor_sha256,
            "audited_at": audited_at,
            "classification_content_read_scope": (
                source.classification_probe or {}
            ).get("content_read_scope"),
            "content_digest_computed": False,
        },
    }
    # Do not add size/count/content-digest fields to this record.  The shape
    # itself is the enforceable proof that unrelated business content was not
    # inventoried or hashed.
    return _seal_media_record(record)


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
    audited_at: str,
    audit_image: str | None = None,
    audit_major: int = POSTGRES_MAJOR,
) -> dict[str, object]:
    if descriptor.classification not in {
        "adjacent-record-only",
        "reviewed-non-pg",
    }:
        raise MediaEvidenceError("record-only media classification is invalid")
    selected_image = audit_image or registry.audit_image
    if source.kind == "docker_volume":
        before = _docker_volume_identity(runner, source)
        before_digest = _volume_content_digest(
            runner,
            selected_image,
            source,
            operation=operation,
            resource_key=f"{resource_prefix}-digest-before",
        )
        after_digest = _volume_content_digest(
            runner,
            selected_image,
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
        if descriptor.classification == "reviewed-non-pg":
            before = _reviewed_bind_identity(runner, source)
            audit_path = Path(source.audit_locator or source.locator)
            first_scan = _scan_reviewed_bind_tree(
                audit_path,
                expected_takeover_seal=source.takeover_seal,
            )
            second_scan = _scan_reviewed_bind_tree(
                audit_path,
                expected_takeover_seal=source.takeover_seal,
            )
            after = _reviewed_bind_identity(runner, source)
            if (
                first_scan["signature"] != "non-postgres"
                or first_scan["postgres_major"] is not None
                or first_scan["contains_backup_material"] is not False
                or first_scan != second_scan
            ):
                raise MediaEvidenceError(
                    "reviewed container bind changed classification "
                    "during content audit"
                )
            before_digest = str(first_scan["source_content_sha256"])
            after_digest = str(second_scan["source_content_sha256"])
        else:
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
            "source_passed_to_docker": False,
            "source_started_as_postgres": False,
            "content_cas_verified": True,
        }
    elif source.kind == "reviewed_file":
        path = Path(source.locator)
        backup_root = _find_backup_root(path, policy)
        root_authority = _sealed_root_authority(registry, backup_root)
        maximum_bytes = int(
            LOGICAL_MEDIA_POLICY["non_postgres"][  # type: ignore[index]
                "maximum_single_file_bytes"
            ]
        )
        first = _reviewed_regular_identity(
            path,
            root=backup_root,
            root_authority=root_authority,
            maximum_bytes=maximum_bytes,
        )
        second = _reviewed_regular_identity(
            path,
            root=backup_root,
            root_authority=root_authority,
            maximum_bytes=maximum_bytes,
        )
        if first != second:
            raise MediaEvidenceError(
                "reviewed non-PG file changed between content CAS passes"
            )
        identity = {
            key: value
            for key, value in first.items()
            if key != "sha256"
        }
        before = identity
        after = dict(identity)
        before_digest = str(first["sha256"])
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
            "postgres_major": audit_major,
            "postgres_uid": registry.postgres_uid,
            "postgres_gid": registry.postgres_gid,
            "postgres_image": selected_image,
            "postgres_image_id": audit_image_id,
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
                "scanned_bind_sources": list(
                    discovery.scanned_bind_sources
                ),
                "scanned_container_ids": list(
                    discovery.scanned_container_ids
                ),
            }
        )
    )


def _revalidate_final_source_content(
    registry: Registry,
    discovery: Discovery,
    media_records: Mapping[str, Mapping[str, object]],
    *,
    runner: CommandRunner,
    operation: ScratchOperation,
    policy: DiscoveryPolicy,
) -> None:
    """Recheck every source's content immediately before evidence publish."""

    descriptor_by_id = {
        descriptor.media_id: descriptor
        for descriptor in registry.descriptors
    }
    if set(descriptor_by_id) != set(media_records):
        raise MediaEvidenceError(
            "final source CAS lacks the complete registry projection"
        )
    with tempfile.TemporaryDirectory(
        prefix="nexpoly-postgres-media-final-cas-",
        dir=operation.workspace,
    ) as temporary:
        os.chmod(temporary, 0o700)
        temporary_root = Path(temporary)
        for index, media_id in enumerate(sorted(descriptor_by_id)):
            descriptor = descriptor_by_id[media_id]
            source = discovery.media.get(media_id)
            record = media_records[media_id]
            if source is None:
                raise MediaEvidenceError(
                    "final source CAS medium disappeared"
                )
            expected_identity = record.get("source_identity_after")
            expected_digest = record.get("source_content_sha256")
            if (
                not isinstance(expected_identity, dict)
                or descriptor.audit_method != "metadata-only-exclusion"
                and (
                    not isinstance(expected_digest, str)
                    or DIGEST_RE.fullmatch(expected_digest) is None
                )
            ):
                raise MediaEvidenceError(
                    "final source CAS evidence is malformed"
                )

            if descriptor.audit_method == "metadata-only-exclusion":
                observed_identity = _excluded_non_pg_identity(source)
                if observed_identity != expected_identity:
                    raise MediaEvidenceError(
                        "excluded non-PG metadata changed before evidence publish"
                    )
                continue

            checkpoint = discovery.audit_checkpoints.get(
                descriptor.media_id
            )
            if (
                descriptor.audit_method
                in {
                    "isolated-volume-copy-read-only",
                    "isolated-bind-copy-read-only",
                    "isolated-backup-restore-read-only",
                }
                and isinstance(checkpoint, dict)
            ):
                if (
                    checkpoint.get("schema_version")
                    == DURABLE_CHECKPOINT_SCHEMA_VERSION
                ):
                    _checkpoint_descriptor_value, checkpoint = (
                        _validate_durable_checkpoint(
                            checkpoint,
                            registry=registry,
                            source=source,
                            expected_descriptor=descriptor,
                            allow_descriptor_inventory=False,
                        )
                    )
                if (
                    checkpoint.get("schema_version")
                    not in {
                        1,
                        DURABLE_CHECKPOINT_SCHEMA_VERSION,
                    }
                    or checkpoint.get("media_id")
                    != descriptor.media_id
                    or checkpoint.get("source_document_sha256")
                    != sha256_bytes(
                        canonical_json_bytes(source.document())
                    )
                    or checkpoint.get("descriptor_sha256")
                    != sha256_bytes(
                        canonical_json_bytes(descriptor.document())
                    )
                    or checkpoint.get("method")
                    != descriptor.audit_method
                    or not isinstance(checkpoint.get("database"), dict)
                    or not isinstance(
                        checkpoint.get("source_identity_before"),
                        dict,
                    )
                    or checkpoint.get("source_identity_before")
                    != checkpoint.get("source_identity_after")
                    or not isinstance(
                        checkpoint.get("source_content_sha256"),
                        str,
                    )
                    or DIGEST_RE.fullmatch(
                        checkpoint["source_content_sha256"]
                    )
                    is None
                    or not isinstance(checkpoint.get("isolation"), dict)
                    or checkpoint.get("scope")
                    not in {
                        "copied-source-cluster",
                        "isolated-restore-cluster",
                    }
                    or not isinstance(checkpoint.get("algorithm"), str)
                ):
                    raise MediaEvidenceError(
                        "in-epoch media audit checkpoint is invalid"
                    )
                database = _scope_database_bundle(
                    dict(checkpoint["database"]),
                    str(checkpoint["scope"]),
                )
                source_digest = str(
                    checkpoint["source_content_sha256"]
                )
                before = dict(
                    checkpoint["source_identity_before"]
                )
                after = dict(
                    checkpoint["source_identity_after"]
                )
                isolation = dict(checkpoint["isolation"])
                algorithm = str(checkpoint["algorithm"])
                source_system_identifier = (
                    None
                    if descriptor.kind == "postgres_backup"
                    else str(
                        database["database_identity"][
                            "system_identifier"
                        ]
                    )
                )
            elif descriptor.audit_method in {
                "live-read-only",
                "live-read-only-adjacent",
            }:
                audit_major = int(descriptor.source_postgres_major)
                audit_image_id = dict(registry.audit_image_ids).get(
                    audit_major
                )
                if (
                    not isinstance(audit_image_id, str)
                    or DIGEST_RE.fullmatch(audit_image_id) is None
                ):
                    raise MediaEvidenceError(
                        "final online CAS lacks its exact client image ID"
                    )
                before = _live_source_identity(runner, source)
                system_identifier_before = (
                    _live_source_system_identifier(
                        runner,
                        source,
                        trusted_image_id=audit_image_id,
                    )
                )
                database = _scope_database_bundle(
                    _run_live_audit(
                        runner,
                        descriptor,
                        source,
                        trusted_image_id=audit_image_id,
                        expected_role_contracts=(
                            _expected_role_contracts_for_descriptor(
                                registry,
                                descriptor,
                                cluster_system_identifier=(
                                    system_identifier_before
                                ),
                            )
                            if descriptor.audit_method
                            == "live-read-only"
                            else None
                        ),
                    ),
                    "source-cluster",
                )
                system_identifier_after = (
                    _live_source_system_identifier(
                        runner,
                        source,
                        trusted_image_id=audit_image_id,
                    )
                )
                after = _live_source_identity(runner, source)
                observed_digest = sha256_bytes(
                    canonical_json_bytes(
                        {
                            "database_inventory": database[
                                "database_inventory"
                            ],
                            "databases": database["databases"],
                        }
                    )
                )
                if (
                    before != after
                    or after != expected_identity
                    or observed_digest != expected_digest
                    or system_identifier_before
                    != system_identifier_after
                    or system_identifier_after
                    != record.get("source_system_identifier")
                ):
                    raise MediaEvidenceError(
                        "online PostgreSQL source changed before evidence publish"
                    )
                continue

            if source.kind == "docker_volume":
                before = _docker_volume_identity(runner, source)
                if any(
                    str(value["state"]) in ACTIVE_CONTAINER_STATES
                    for value in before["attached"]
                ):
                    raise MediaEvidenceError(
                        "inactive volume gained an active reader before publish"
                    )
                observed_digest = _volume_content_digest(
                    runner,
                    _audit_image_for_major(
                        registry,
                        descriptor.source_postgres_major
                        or POSTGRES_MAJOR,
                    ),
                    source,
                    operation=operation,
                    resource_key=(
                        f"final-cas-{index:04d}-volume-digest"
                    ),
                )
                after = _docker_volume_identity(runner, source)
                if (
                    before != after
                    or after != expected_identity
                    or observed_digest != expected_digest
                ):
                    raise MediaEvidenceError(
                        "Docker volume content changed before evidence publish"
                    )
                continue

            if source.kind == "container_bind":
                if descriptor.classification == "reviewed-non-pg":
                    before = _reviewed_bind_identity(runner, source)
                    audit_path = Path(
                        source.audit_locator or source.locator
                    )
                    first_scan = _scan_reviewed_bind_tree(
                        audit_path,
                        expected_takeover_seal=source.takeover_seal,
                    )
                    second_scan = _scan_reviewed_bind_tree(
                        audit_path,
                        expected_takeover_seal=source.takeover_seal,
                    )
                    after = _reviewed_bind_identity(runner, source)
                    if (
                        first_scan != second_scan
                        or first_scan["signature"] != "non-postgres"
                        or first_scan["postgres_major"] is not None
                        or first_scan[
                            "contains_backup_material"
                        ] is not False
                    ):
                        raise MediaEvidenceError(
                            "reviewed container bind changed classification "
                            "before evidence publish"
                        )
                    observed_digest = str(
                        first_scan["source_content_sha256"]
                    )
                else:
                    before = _live_source_identity(runner, source)
                    destination = temporary_root / f"bind-{index:04d}"
                    _tree, observed_digest = _bind_tree_snapshot(
                        Path(source.locator),
                        destination,
                    )
                    after = _live_source_identity(runner, source)
                if (
                    before != after
                    or after != expected_identity
                    or observed_digest != expected_digest
                ):
                    raise MediaEvidenceError(
                        "PostgreSQL bind content changed before evidence publish"
                    )
                continue

            if source.kind in {"reviewed_file", "postgres_backup"}:
                path = Path(source.locator)
                root = _find_backup_root(path, policy)
                root_authority = _sealed_root_authority(registry, root)
                maximum_bytes = int(
                    LOGICAL_MEDIA_POLICY["non_postgres"][  # type: ignore[index]
                        (
                            "maximum_single_file_bytes"
                            if source.kind == "reviewed_file"
                            else "maximum_bytes"
                        )
                    ]
                )
                first = _reviewed_regular_identity(
                    path,
                    root=root,
                    root_authority=root_authority,
                    maximum_bytes=maximum_bytes,
                )
                second = _reviewed_regular_identity(
                    path,
                    root=root,
                    root_authority=root_authority,
                    maximum_bytes=maximum_bytes,
                )
                if first != second:
                    raise MediaEvidenceError(
                        "private file changed during final content CAS"
                    )
                if source.kind == "postgres_backup":
                    observed_identity = {
                        **first,
                        "format": source.backup_format,
                    }
                else:
                    observed_identity = {
                        key: value
                        for key, value in first.items()
                        if key != "sha256"
                    }
                if (
                    observed_identity != expected_identity
                    or first["sha256"] != expected_digest
                ):
                    raise MediaEvidenceError(
                        "private file content changed before evidence publish"
                    )
                continue

            raise MediaEvidenceError(
                "final source CAS encountered an unsupported medium"
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
            or record.get("audit_state") != "complete"
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
            raise MediaEvidenceError(
                "complete database inventory audit is absent"
            )
        scoped_databases.append({**record, "audit": scoped_audit})
    return {
        **primary,
        "databases": scoped_databases,
    }


def _durable_checkpoint_audit_material(
    registry: Registry,
    discovery: Discovery,
    descriptor: MediaDescriptor,
    source: DiscoveredMedia,
) -> tuple[
    dict[str, object],
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    str,
    str | None,
] | None:
    """Return one sealed offline audit without repeating copy/restore work."""

    checkpoint = discovery.audit_checkpoints.get(descriptor.media_id)
    if checkpoint is None:
        return None
    if descriptor.audit_method not in {
        "isolated-volume-copy-read-only",
        "isolated-bind-copy-read-only",
        "isolated-backup-restore-read-only",
    }:
        raise MediaEvidenceError(
            "durable checkpoint is attached to a non-isolated medium"
        )
    _checkpoint_descriptor_value, validated = (
        _validate_durable_checkpoint(
            checkpoint,
            registry=registry,
            source=source,
            expected_descriptor=descriptor,
            allow_descriptor_inventory=False,
        )
    )
    scope = str(validated["scope"])
    database = _scope_database_bundle(
        dict(validated["database"]),
        scope,
    )
    source_system_identifier = (
        None
        if descriptor.kind == "postgres_backup"
        else str(
            database["database_identity"]["system_identifier"]  # type: ignore[index]
        )
    )
    return (
        database,
        str(validated["source_content_sha256"]),
        dict(validated["source_identity_before"]),
        dict(validated["source_identity_after"]),
        dict(validated["isolation"]),
        str(validated["algorithm"]),
        source_system_identifier,
    )


def _write_private_atomic(directory: Path, name: str, payload: bytes) -> Path:
    directory_descriptor = _open_directory_chain(
        directory,
        private_from=directory,
        create_leaf=True,
    )
    temporary = f".{name}.tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        temporary_prefix = f".{name}.tmp-"
        for candidate in os.listdir(directory_descriptor):
            if not candidate.startswith(temporary_prefix):
                continue
            metadata = os.stat(
                candidate,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise MediaEvidenceError(
                    f"stale immutable evidence temporary is unsafe: {candidate}"
                )
            os.unlink(candidate, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
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
        except FileNotFoundError:
            # ScratchLock serializes publishers. rename gives one crash-safe
            # name transition without the hard-link nlink=2 failure window.
            os.rename(
                temporary,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
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


def _replace_private_atomic(
    directory: Path,
    name: str,
    payload: bytes,
    *,
    validate_staged: Callable[[Path], None] | None = None,
) -> Path:
    """Atomically publish one replaceable, owner-private generated input."""

    directory_descriptor = _open_directory_chain(
        directory,
        private_from=directory,
        create_leaf=True,
    )
    temporary = f".{name}.tmp-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        try:
            existing = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
            or existing.st_nlink != 1
        ):
            raise MediaEvidenceError(
                f"existing generated registry is unsafe: {name}"
            )
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
        if validate_staged is not None:
            validate_staged(directory / temporary)
        os.rename(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
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
    evidence_root: Path,
    operation: ScratchOperation,
    policy: DiscoveryPolicy = DiscoveryPolicy(),
    now: Callable[[], str] = utc_now,
    reviewed_content_file: Path | None = None,
) -> dict[str, object]:
    isolated_media_ids = {
        descriptor.media_id
        for descriptor in registry.descriptors
        if descriptor.audit_method
        in {
            "isolated-volume-copy-read-only",
            "isolated-bind-copy-read-only",
            "isolated-backup-restore-read-only",
        }
    }
    if not set(discovery.audit_checkpoints).issubset(
        isolated_media_ids
    ):
        raise MediaEvidenceError(
            "durable checkpoint inventory contains a non-isolated medium"
        )
    if (
        reviewed_content_file is None
        and registry.reviewed_content_inventory_sha256 is not None
    ):
        reviewed_content_file = DEFAULT_REVIEWED_CONTENT_ROOT / (
            registry.reviewed_content_inventory_sha256.removeprefix(
                "sha256:"
            )
            + ".json"
        )
    required_majors = {
        (
            descriptor.source_postgres_major
            if descriptor.source_postgres_major is not None
            else POSTGRES_MAJOR
        )
        for descriptor in registry.descriptors
    }
    required_majors.add(POSTGRES_MAJOR)
    audit_image_ids: dict[int, str] = {}
    pinned_image_ids = dict(registry.audit_image_ids)
    for major in sorted(required_majors):
        image = _audit_image_for_major(registry, major)
        expected_image_id = pinned_image_ids.get(major)
        if (
            not isinstance(expected_image_id, str)
            or DIGEST_RE.fullmatch(expected_image_id) is None
        ):
            raise MediaEvidenceError(
                "runtime registry lacks an exact local audit image ID"
            )
        audit_image_ids[major] = _validate_audit_image(
            runner,
            image,
            postgres_uid=registry.postgres_uid,
            postgres_gid=registry.postgres_gid,
            operation=operation,
            resource_prefix=f"build-image-pg{major}",
            expected_major=major,
            expected_image_id=expected_image_id,
        )
    auditor_sha256 = _auditor_digest()
    if auditor_sha256 != registry.auditor_sha256:
        raise MediaEvidenceError("auditor drifted after registry validation")
    if (
        registry.reviewed_content_inventory_sha256 is not None
        and (
            reviewed_content_file is None
            or _private_file_digest(reviewed_content_file)
            != registry.reviewed_content_inventory_sha256
        )
    ):
        raise MediaEvidenceError(
            "reviewed-content inventory differs from runtime registry"
        )
    discovery_state_before = _discovery_state_sha256(discovery)
    reviewed_content_records = (
        _load_reviewed_content_inventory(
            reviewed_content_file,
            expected_digest=(
                registry.reviewed_content_inventory_sha256
            ),
            expected_authority_digest=registry.authority_rules_sha256,
            expected_discovery_state=discovery_state_before,
        )
        if registry.reviewed_content_inventory_sha256 is not None
        else {}
    )
    media_records: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(
        prefix="nexpoly-postgres-media-",
        dir=operation.workspace,
    ) as temporary:
        os.chmod(temporary, 0o700)
        temporary_root = Path(temporary)
        for index, descriptor in enumerate(registry.descriptors):
            source = discovery.media[descriptor.media_id]
            audit_major = (
                descriptor.source_postgres_major
                if descriptor.source_postgres_major is not None
                else POSTGRES_MAJOR
            )
            audit_image = _audit_image_for_major(
                registry,
                audit_major,
            )
            audit_image_id = audit_image_ids[audit_major]
            workspace = temporary_root / f"medium-{index:04d}"
            workspace.mkdir(mode=0o700)
            checkpoint_material = _durable_checkpoint_audit_material(
                registry,
                discovery,
                descriptor,
                source,
            )
            audited_at = now()
            if RFC3339_UTC_RE.fullmatch(audited_at) is None:
                raise MediaEvidenceError("audit clock did not return UTC RFC3339")
            if descriptor.audit_method == "metadata-only-exclusion":
                exclusion = _metadata_only_exclusion_record(
                    registry,
                    descriptor,
                    source,
                    auditor_sha256=auditor_sha256,
                    audited_at=audited_at,
                )
                reviewed = reviewed_content_records.get(
                    descriptor.media_id
                )
                if (
                    reviewed is None
                    or reviewed.get("review_algorithm")
                    != "metadata-only-exclusion-v1"
                    or reviewed.get("metadata_identity_sha256")
                    != sha256_bytes(
                        canonical_json_bytes(
                            exclusion["source_identity_before"]
                        )
                    )
                    or reviewed.get("metadata_identity")
                    != exclusion["source_identity_before"]
                ):
                    raise MediaEvidenceError(
                        "excluded non-PG evidence differs from its "
                        "metadata-only inventory"
                    )
                media_records[descriptor.media_id] = exclusion
                continue
            if descriptor.audit_method in {
                "adjacent-record-only",
                "reviewed-content-only",
            }:
                record_only = _record_only_medium(
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
                    audited_at=audited_at,
                    audit_image=audit_image,
                    audit_major=audit_major,
                )
                if descriptor.classification == "reviewed-non-pg":
                    reviewed = reviewed_content_records.get(
                        descriptor.media_id
                    )
                    if (
                        reviewed is None
                        or record_only.get("source_content_sha256")
                        != reviewed["source_content_sha256"]
                        or sha256_bytes(
                            canonical_json_bytes(
                                record_only["source_identity_before"]
                            )
                        )
                        != reviewed["source_identity_sha256"]
                    ):
                        raise MediaEvidenceError(
                            "reviewed non-PG evidence differs from its "
                            "private content inventory"
                        )
                media_records[descriptor.media_id] = record_only
                continue
            if checkpoint_material is not None:
                (
                    database,
                    source_digest,
                    before,
                    after,
                    isolation,
                    algorithm,
                    source_system_identifier,
                ) = checkpoint_material
            elif descriptor.audit_method in {
                "live-read-only",
                "live-read-only-adjacent",
            }:
                before = _live_source_identity(runner, source)
                source_system_identifier_before = (
                    _live_source_system_identifier(
                        runner,
                        source,
                        trusted_image_id=audit_image_id,
                    )
                )
                database = _run_live_audit(
                    runner,
                    descriptor,
                    source,
                    trusted_image_id=audit_image_id,
                    expected_role_contracts=(
                        _expected_role_contracts_for_descriptor(
                            registry,
                            descriptor,
                            cluster_system_identifier=(
                                source_system_identifier_before
                            ),
                        )
                        if descriptor.audit_method
                        == "live-read-only"
                        else None
                    ),
                )
                database = _scope_database_bundle(
                    database,
                    "source-cluster",
                )
                source_system_identifier_after = (
                    _live_source_system_identifier(
                        runner,
                        source,
                        trusted_image_id=audit_image_id,
                    )
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
                "record_type": descriptor.classification,
                "media_id": descriptor.media_id,
                "kind": descriptor.kind,
                "classification": descriptor.classification,
                "database": descriptor.database,
                "disposition": descriptor.disposition,
                "online_admin_role": descriptor.online_admin_role,
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
                "server_startup": database["server_startup"],
                "event_triggers_disabled": database[
                    "event_triggers_disabled"
                ],
                "role_superuser": database["role_superuser"],
                "role_create_db": database["role_create_db"],
                "role_create_role": database["role_create_role"],
                "role_replication": database["role_replication"],
                "role_bypass_rls": database["role_bypass_rls"],
                "role_inherit": database["role_inherit"],
                "role_can_login": database["role_can_login"],
                "role_memberships": database["role_memberships"],
                "role_incoming_memberships": database[
                    "role_incoming_memberships"
                ],
                "role_settings": database["role_settings"],
                "role_owned_objects": database[
                    "role_owned_objects"
                ],
                "role_direct_acl": database["role_direct_acl"],
                "role_default_acl": database["role_default_acl"],
                "event_triggers": database["event_triggers"],
                "role_effective_persistent_write": database[
                    "role_effective_persistent_write"
                ],
                "ledger": database["ledger"],
                "ledger_sha256": database["ledger_sha256"],
                "ledger_relation": database["ledger_relation"],
                "ledger_analysis": database["ledger_analysis"],
                "legacy_relation_present": database["legacy_relation_present"],
                "generation_schema": database["generation_schema"],
                "legacy_relation": database["legacy_relation"],
                "migration_0013": database["migration_0013"],
                "audit": {
                    "method": descriptor.audit_method,
                    "complete": True,
                    "auditor_sha256": auditor_sha256,
                    "postgres_major": audit_major,
                    "postgres_uid": registry.postgres_uid,
                    "postgres_gid": registry.postgres_gid,
                    "postgres_image": audit_image,
                    "postgres_image_id": audit_image_id,
                    "audited_at": audited_at,
                    "isolation": isolation,
                },
            }
            sealed = _seal_media_record(record)
            media_records[descriptor.media_id] = sealed
    reviewed_media_ids = {
        descriptor.media_id
        for descriptor in registry.descriptors
        if descriptor.classification
        in {"reviewed-non-pg", "excluded-non-pg"}
    }
    if set(reviewed_content_records) != reviewed_media_ids:
        raise MediaEvidenceError(
            "reviewed-content inventory differs from runtime classifications"
        )
    if (
        registry.reviewed_content_inventory_sha256 is not None
        and (
            reviewed_content_file is None
            or _private_file_digest(reviewed_content_file)
            != registry.reviewed_content_inventory_sha256
        )
    ):
        raise MediaEvidenceError(
            "reviewed-content inventory changed during audit"
        )
    _revalidate_docker_epoch(runner, discovery)
    _revalidate_backup_epoch(registry, discovery, policy)
    final_discovery = discovery
    discovery_state_after = _discovery_state_sha256(discovery)
    if discovery_state_after != discovery_state_before:
        raise MediaEvidenceError(
            "external PostgreSQL discovery boundary changed during audit"
        )
    _revalidate_final_source_content(
        registry,
        final_discovery,
        media_records,
        runner=runner,
        operation=operation,
        policy=policy,
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
    production_identity = registry.production_identity
    writable_records = [
        record
        for record in media_records.values()
        if record.get("disposition") == "writable-target"
    ]
    if production_identity is not None and (
        len(writable_records) != 1
        or writable_records[0].get("media_id")
        != production_identity.get("media_id")
        or writable_records[0].get("database")
        != production_identity.get("database")
        or writable_records[0].get("kind")
        != production_identity.get("kind")
        or writable_records[0].get("source_system_identifier")
        != production_identity.get("system_identifier")
    ):
        raise MediaEvidenceError(
            "production PostgreSQL identity differs from authority rules"
        )
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
                "role_replication": record["role_replication"],
                "role_bypass_rls": record["role_bypass_rls"],
                "role_inherit": record["role_inherit"],
                "role_can_login": record["role_can_login"],
                "role_memberships": record["role_memberships"],
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
        if record.get("record_type")
        in {"nexpoly-db", "adjacent-postgres"}
        for database_record in record["databases"]
    )
    envelope = {
        "schema_version": 5,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": {
            "schema_version": 5,
            "media_authority_rules_sha256": (
                registry.authority_rules_sha256
            ),
            "runtime_registry_sha256": registry.digest,
            "reviewed_content_inventory_sha256": (
                registry.reviewed_content_inventory_sha256
            ),
            "audit_images": {
                str(major): {
                    "digest_ref": dict(registry.audit_images)[major],
                    "image_id": image_id,
                }
                for major, image_id in registry.audit_image_ids
            },
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
            "scanned_bind_sources": list(
                discovery.scanned_bind_sources
            ),
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


def _private_file_digest(path: Path) -> str:
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
            "private file changed while being read"
        )
    return sha256_bytes(payload)


def _load_reviewed_content_inventory(
    path: Path,
    *,
    expected_digest: str,
    expected_authority_digest: str,
    expected_discovery_state: str,
) -> dict[str, dict[str, object]]:
    descriptor = open_private_regular(path, root=path.parent)
    try:
        payload = _read_fd(descriptor, MAX_REGISTRY_BYTES)
    finally:
        os.close(descriptor)
    if sha256_bytes(payload) != expected_digest:
        raise MediaEvidenceError(
            "reviewed-content inventory digest differs"
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MediaEvidenceError(
            "reviewed-content inventory is not JSON"
        ) from exc
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "media_authority_rules_sha256",
            "discovery_state_sha256",
            "media",
        }
        or document.get("schema_version") != 1
        or document.get("media_authority_rules_sha256")
        != expected_authority_digest
        or document.get("discovery_state_sha256")
        != expected_discovery_state
        or canonical_json_bytes(document) + b"\n" != payload
        or not isinstance(document.get("media"), list)
    ):
        raise MediaEvidenceError(
            "reviewed-content inventory authority differs"
        )
    result: dict[str, dict[str, object]] = {}
    for record in document["media"]:
        if (
            isinstance(record, dict)
            and record.get("review_algorithm")
            == "metadata-only-exclusion-v1"
        ):
            identity = record.get("metadata_identity")
            if (
                set(record)
                != {
                    "media_id",
                    "metadata_identity",
                    "metadata_identity_sha256",
                    "review_algorithm",
                }
                or not isinstance(record.get("media_id"), str)
                or MEDIA_ID_RE.fullmatch(record["media_id"]) is None
                or record["media_id"] in result
                or not isinstance(identity, dict)
                or record.get("metadata_identity_sha256")
                != sha256_bytes(canonical_json_bytes(identity))
                or any(
                    forbidden in record
                    for forbidden in (
                        "source_content_sha256",
                        "file_count",
                        "size_bytes",
                    )
                )
            ):
                raise MediaEvidenceError(
                    "metadata-only exclusion inventory is invalid"
                )
            result[record["media_id"]] = dict(record)
            continue
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "media_id",
                "source_identity_sha256",
                "source_content_sha256",
                "file_count",
                "size_bytes",
                "review_algorithm",
            }
            or not isinstance(record.get("media_id"), str)
            or MEDIA_ID_RE.fullmatch(record["media_id"]) is None
            or record["media_id"] in result
            or not isinstance(record.get("source_identity_sha256"), str)
            or DIGEST_RE.fullmatch(record["source_identity_sha256"]) is None
            or not isinstance(record.get("source_content_sha256"), str)
            or DIGEST_RE.fullmatch(record["source_content_sha256"]) is None
            or isinstance(record.get("file_count"), bool)
            or not isinstance(record.get("file_count"), int)
            or not 0
            <= record["file_count"]
            <= int(
                LOGICAL_MEDIA_POLICY["non_postgres"]["maximum_files"]  # type: ignore[index]
            )
            or isinstance(record.get("size_bytes"), bool)
            or not isinstance(record.get("size_bytes"), int)
            or not 0
            <= record["size_bytes"]
            <= int(
                LOGICAL_MEDIA_POLICY["non_postgres"]["maximum_bytes"]  # type: ignore[index]
            )
            or (
                str(record.get("media_id", "")).startswith(
                    ("reviewed-file:", "reviewed-file-sha256:")
                )
                and (
                    record.get("file_count") != 1
                    or record["size_bytes"]
                    > int(
                        LOGICAL_MEDIA_POLICY["non_postgres"][  # type: ignore[index]
                            "maximum_single_file_bytes"
                        ]
                    )
                )
            )
            or record.get("review_algorithm")
            != "private-reviewed-content-inventory-v1"
        ):
            raise MediaEvidenceError(
                "reviewed-content inventory record is invalid"
            )
        result[record["media_id"]] = dict(record)
    if list(result) != sorted(result):
        raise MediaEvidenceError(
            "reviewed-content inventory is not canonical"
        )
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subparsers = value.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build",
        help="discover all fixed media and build fresh isolated evidence",
    )
    revalidate = subparsers.add_parser(
        "revalidate",
        help=(
            "refresh discovery and online audits while reusing source-CAS "
            "offline checkpoints"
        ),
    )
    subparsers.add_parser(
        "role-plan",
        help=(
            "publish the private source-epoch registry and emit the exact "
            "per-database NOLOGIN role provisioning plan without auditing "
            "through those roles"
        ),
    )
    provision = subparsers.add_parser(
        "provision-roles",
        help=(
            "apply exact F NOLOGIN role SQL only after a separately reviewed "
            "fresh role-plan digest is supplied"
        ),
    )
    provision.add_argument(
        "--confirm-plan-sha256",
        required=True,
        help="exact sha256 digest printed by role-plan",
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
            "--operation-id",
            help="exact audit-<64hex> operation journal; default is all",
        )
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    evidence_root = DEFAULT_EVIDENCE_ROOT
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
        authority = load_authority_rules(DEFAULT_AUTHORITY_RULES)
        expected_authority = os.environ.get(
            "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256"
        )
        if (
            not isinstance(expected_authority, str)
            or DIGEST_RE.fullmatch(expected_authority) is None
            or authority.digest != expected_authority
        ):
            raise MediaEvidenceError(
                "media authority rules differ from bridge authority"
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
            image_ids = {
                major: _local_audit_image_id(runner, image)
                for major, image in _audit_images_map(authority).items()
            }
            image_id = image_ids[POSTGRES_MAJOR]
            operation_images = {
                str(major): {
                    "digest_ref": image,
                    "image_id": image_ids[major],
                }
                for major, image in sorted(
                    _audit_images_map(authority).items()
                )
            }
            if arguments.command == "revalidate":
                preliminary = load_registry(
                    DEFAULT_REGISTRY,
                    policy=authority.policy,
                    private_root=DEFAULT_REGISTRY.parent,
                )
                if not isinstance(preliminary, Registry):
                    raise MediaEvidenceError(
                        "runtime registry is unavailable for revalidation"
                    )
                operation = ScratchOperation.begin(
                    evidence_root,
                    runner=runner,
                    authority={
                        "registry_sha256": preliminary.digest,
                        "auditor_sha256": preliminary.auditor_sha256,
                        "postgres_image": preliminary.audit_image,
                        "postgres_image_id": image_id,
                        "postgres_images": operation_images,
                    },
                )
                try:
                    for major, image in sorted(
                        _audit_images_map(authority).items()
                    ):
                        _validate_audit_image(
                            runner,
                            image,
                            postgres_uid=authority.postgres_uid,
                            postgres_gid=authority.postgres_gid,
                            operation=operation,
                            resource_prefix=(
                                f"revalidate-image-pg{major}"
                            ),
                            expected_major=major,
                            expected_image_id=image_ids[major],
                        )
                    registry, registry_discovery = (
                        load_runtime_registry_for_revalidation(
                            authority,
                            registry_path=DEFAULT_REGISTRY,
                            evidence_root=evidence_root,
                            runner=runner,
                            operation=operation,
                        )
                    )
                    envelope = build_evidence(
                        registry,
                        registry_discovery,
                        runner=runner,
                        evidence_root=evidence_root,
                        operation=operation,
                    )
                    operation.complete(
                        {
                            "runtime_registry_sha256": registry.digest,
                            "external_database_audit_sha256": (
                                sha256_bytes(
                                    canonical_json_bytes(envelope) + b"\n"
                                )
                            ),
                            "checkpoint_mode": "durable-revalidation",
                        }
                    )
                except BaseException:
                    try:
                        operation.abort()
                    except BaseException as cleanup_error:
                        raise MediaEvidenceError(
                            "checkpoint revalidation failed and owned "
                            "recovery did not complete"
                        ) from cleanup_error
                    raise
                print(
                    json.dumps(
                        envelope,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                return 0
            registry_operation = ScratchOperation.begin(
                evidence_root,
                runner=runner,
                authority={
                    "registry_sha256": authority.digest,
                    "auditor_sha256": authority.auditor_sha256,
                    "postgres_image": authority.audit_image,
                    "postgres_image_id": image_id,
                    "postgres_images": operation_images,
                },
            )
            try:
                for major, image in sorted(
                    _audit_images_map(authority).items()
                ):
                    _validate_audit_image(
                        runner,
                        image,
                        postgres_uid=authority.postgres_uid,
                        postgres_gid=authority.postgres_gid,
                        operation=registry_operation,
                        resource_prefix=f"registry-image-pg{major}",
                        expected_major=major,
                        expected_image_id=image_ids[major],
                    )
                registry, registry_discovery = generate_runtime_registry(
                    authority,
                    registry_path=DEFAULT_REGISTRY,
                    runner=runner,
                    operation=registry_operation,
                    evidence_root=evidence_root,
                )
                registry_operation.complete(
                    {"runtime_registry_sha256": registry.digest}
                )
            except BaseException:
                try:
                    registry_operation.abort()
                except BaseException as cleanup_error:
                    raise MediaEvidenceError(
                        "runtime registry generation failed and owned "
                        "recovery did not complete"
                    ) from cleanup_error
                raise
            if arguments.command == "role-plan":
                role_plan = external_database_role_plan(
                    registry,
                    registry_discovery,
                    role_sql_sha256=os.environ.get(
                        AUDIT_ROLE_SQL_DIGEST_ENV,
                        "",
                    ),
                    runner=runner,
                )
                print(
                    json.dumps(
                        role_plan,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                return 0
            if arguments.command == "provision-roles":
                role_plan = external_database_role_plan(
                    registry,
                    registry_discovery,
                    role_sql_sha256=os.environ.get(
                        AUDIT_ROLE_SQL_DIGEST_ENV,
                        "",
                    ),
                    runner=runner,
                )
                if (
                    DIGEST_RE.fullmatch(
                        arguments.confirm_plan_sha256
                    )
                    is None
                    or role_plan["plan_sha256"]
                    != arguments.confirm_plan_sha256
                ):
                    raise MediaEvidenceError(
                        "fresh role plan differs from explicit confirmation"
                    )
                result = provision_external_database_roles(
                    role_plan,
                    role_sql=_inherited_audit_role_sql(),
                    runner=runner,
                )
                _revalidate_docker_epoch(runner, registry_discovery)
                _revalidate_live_registry_epoch(
                    runner,
                    registry.descriptors,
                    registry_discovery,
                    audit_image_ids=dict(registry.audit_image_ids),
                )
                print(
                    json.dumps(
                        result,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                return 0
            operation = ScratchOperation.begin(
                evidence_root,
                runner=runner,
                authority={
                    "registry_sha256": registry.digest,
                    "auditor_sha256": registry.auditor_sha256,
                    "postgres_image": registry.audit_image,
                    "postgres_image_id": image_id,
                    "postgres_images": operation_images,
                },
            )
            try:
                envelope = build_evidence(
                    registry,
                    registry_discovery,
                    runner=runner,
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
