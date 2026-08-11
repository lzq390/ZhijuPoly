#!/usr/bin/env python3
"""Validate fixed production site-helper installation and JSON contracts.

The readiness command is intentionally read-only: it hashes installed helpers
but never executes them.  Evidence validation consumes an already captured
JSON document, so mutating recovery helpers cannot be invoked accidentally by
a health check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Callable


PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
MAX_EVIDENCE_BYTES = 256 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BARE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_IMAGE_DIGEST_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{64}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
MIGRATION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
ROLE_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,62}$")
SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")
VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PG_SYSTEM_ID_RE = re.compile(r"^[0-9]{1,20}$")
MEDIA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,1023}$")
RFC3339_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
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
        raise SiteHelperContractError("external media locator is invalid")
    candidate = f"{prefix}:{locator}"
    if MEDIA_ID_RE.fullmatch(candidate) is not None:
        return candidate
    return (
        f"{prefix}-sha256:"
        + hashlib.sha256(locator.encode("utf-8")).hexdigest()
    )
CANONICAL_0013_CHECKSUM = (
    "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
)
ADJACENT_POSTGRES_MAJOR_MIN = 9
ADJACENT_POSTGRES_MAJOR_MAX = 18
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
SUPERSEDED_0013_CHECKSUM = (
    "a60cbf66c70981ba6eb7cf545b5bd89df96fa399fff48ef7f6f21d3682c64cab"
)
LEGACY_0005_ALIAS_VERSION = "0005_polytao_jobs"
LEGACY_0005_ALIAS_CHECKSUM = (
    "b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5"
)
KNOWN_DIRTY_0009_CHECKSUM = (
    "79a6956fc934794d61bc003f02a6b5280e9e8bd77a217b61a28d3dbdb8b7be0b"
)
LEDGER_SCHEMA_AUTHORITY = {
    "relation_kind": "ordinary-table",
    "columns": [
        {
            "name": "version",
            "type": "text",
            "not_null": True,
            "default": None,
        },
        {
            "name": "checksum",
            "type": "text",
            "not_null": True,
            "default": None,
        },
        {
            "name": "applied_at",
            "type": "timestamp with time zone",
            "not_null": True,
            "default": "now()",
        },
    ],
    "indexes": [
        {
            "name": "schema_migrations_pkey",
            "definition": (
                "CREATE UNIQUE INDEX schema_migrations_pkey ON "
                "governance.schema_migrations USING btree (version)"
            ),
        }
    ],
    "constraints": [
        {
            "name": "schema_migrations_pkey",
            "type": "p",
            "definition": "PRIMARY KEY (version)",
        }
    ],
}
LEGACY_SCHEMA_COLUMNS = [
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
]
LEGACY_SCHEMA_AUTHORITY = {
    "relation_kind": "ordinary-table",
    "columns": [
        {
            "name": name,
            "type": data_type,
            "not_null": not_null,
            "default": default,
        }
        for name, data_type, not_null, default in LEGACY_SCHEMA_COLUMNS
    ],
    "indexes": [
        {
            "name": "idx_polytao_jobs_created_at",
            "definition": (
                "CREATE INDEX idx_polytao_jobs_created_at ON "
                "generation.polytao_jobs USING btree (created_at DESC)"
            ),
        },
        {
            "name": "idx_polytao_jobs_status",
            "definition": (
                "CREATE INDEX idx_polytao_jobs_status ON "
                "generation.polytao_jobs USING btree (status)"
            ),
        },
        {
            "name": "polytao_jobs_pkey",
            "definition": (
                "CREATE UNIQUE INDEX polytao_jobs_pkey ON "
                "generation.polytao_jobs USING btree (job_id)"
            ),
        },
    ],
    "constraints": [
        {
            "name": "polytao_jobs_attempts_check",
            "type": "c",
            "definition": "CHECK (attempts >= 0)",
        },
        {
            "name": "polytao_jobs_pkey",
            "type": "p",
            "definition": "PRIMARY KEY (job_id)",
        },
        {
            "name": "polytao_jobs_progress_percent_check",
            "type": "c",
            "definition": (
                "CHECK (progress_percent >= 0 AND progress_percent <= 100)"
            ),
        },
        {
            "name": "polytao_jobs_requested_count_check",
            "type": "c",
            "definition": "CHECK (requested_count > 0)",
        },
        {
            "name": "polytao_jobs_returned_count_check",
            "type": "c",
            "definition": "CHECK (returned_count >= 0)",
        },
        {
            "name": "polytao_jobs_status_check",
            "type": "c",
            "definition": (
                "CHECK (status = ANY (ARRAY['pending'::text, "
                "'submitted'::text, 'running'::text, 'completed'::text, "
                "'failed'::text, 'cancelled'::text]))"
            ),
        },
    ],
}

ACTIVE_JOB_FIELDS_V1 = frozenset(
    {
        "monomer_md",
        "polytao",
        "online_knowledge",
        "conditional_generation",
        "reverse_design",
        "gpu_inference",
        "gpu_waiting",
        "inflight_api_writes",
    }
)
ACTIVE_JOB_FIELDS_V2 = ACTIVE_JOB_FIELDS_V1 | {"monomer_dft"}
BUSINESS_MUTABLE_TABLES = (
    ("online_knowledge", "history"),
    ("online_knowledge", "jobs"),
    ("lab", "test_projects"),
    ("lab", "sample_measurements"),
    ("md", "monomer_md_jobs"),
)
POST_0013_BUSINESS_MUTABLE_TABLES = (
    ("monomer_dft", "jobs"),
    ("monomer_dft", "job_attempts"),
    ("monomer_dft", "artifacts"),
)
GOVERNED_CONTROL_TABLES = (
    ("governance", "deployment_control"),
    ("governance", "database_analytics_snapshots"),
)
STATIC_IMPORT_TABLES = (
    ("governance", "source_files"),
    ("governance", "import_batches"),
    ("governance", "property_filter_options_snapshots"),
    ("core", "polymers"),
    ("core", "polymer_properties"),
    ("core", "polymer_property_filter_records"),
    ("knowledge", "documents"),
    ("knowledge", "formulation_records"),
    ("pi", "polymers"),
    ("pi", "tg_predictions"),
    ("pi", "monomer_iupac"),
    ("dft", "molecule_final"),
    ("dft", "energy_trace"),
    ("experimental", "process_records"),
    ("experimental", "property_records"),
    ("model_registry", "assets"),
)
MIGRATION_LEDGER_TABLE = ("governance", "schema_migrations")
CONTRACT_0012_EXCEPTION_TABLE = ("generation", "polytao_jobs")
DATA_SEQUENCES = (
    (
        "governance",
        "source_files_source_file_id_seq",
        "governance.source_files.source_file_id",
    ),
    (
        "governance",
        "import_batches_import_batch_id_seq",
        "governance.import_batches.import_batch_id",
    ),
    (
        "knowledge",
        "formulation_records_formulation_id_seq",
        "knowledge.formulation_records.formulation_id",
    ),
    (
        "online_knowledge",
        "history_history_id_seq",
        "online_knowledge.history.history_id",
    ),
    (
        "lab",
        "test_projects_id_seq",
        "lab.test_projects.id",
    ),
    (
        "lab",
        "sample_measurements_id_seq",
        "lab.sample_measurements.id",
    ),
    (
        "experimental",
        "process_records_record_id_seq",
        "experimental.process_records.record_id",
    ),
    (
        "experimental",
        "property_records_record_id_seq",
        "experimental.property_records.record_id",
    ),
    (
        "model_registry",
        "assets_asset_id_seq",
        "model_registry.assets.asset_id",
    ),
    (
        "md",
        "monomer_md_queue_sequence_seq",
        "md.monomer_md_jobs.queue_sequence",
    ),
    (
        "monomer_dft",
        "jobs_enqueue_sequence_seq",
        "monomer_dft.jobs.enqueue_sequence",
    ),
)
DATA_SEQUENCE_OWNERSHIP = (
    ("governance", "source_files", "source_file_id", 1, "a"),
    ("governance", "import_batches", "import_batch_id", 1, "a"),
    ("knowledge", "formulation_records", "formulation_id", 1, "a"),
    ("online_knowledge", "history", "history_id", 1, "a"),
    ("lab", "test_projects", "id", 1, "i"),
    ("lab", "sample_measurements", "id", 1, "i"),
    ("experimental", "process_records", "record_id", 1, "a"),
    ("experimental", "property_records", "record_id", 1, "a"),
    ("model_registry", "assets", "asset_id", 1, "a"),
    ("md", "monomer_md_jobs", "queue_sequence", 37, "a"),
    ("monomer_dft", "jobs", "enqueue_sequence", 2, "i"),
)
MONOMER_DFT_TABLE_SCHEMA_SHA256 = {
    ("monomer_dft", "jobs"): (
        "sha256:"
        "a96507d1fa4575f9d9c9a6b6f1b418c6c4b6d2b4aea2d09f8fa18fefe0a603ef"
    ),
    ("monomer_dft", "job_attempts"): (
        "sha256:"
        "9a79e9e3aa2342de49d0cf0f280bc3524e4f595ce12a85027f332707cf4bac47"
    ),
    ("monomer_dft", "artifacts"): (
        "sha256:"
        "4b1342737e358385c9d94566872861cb3865bb2573d613e325f92aede57dbbf0"
    ),
}
EMPTY_POSTGRES_COPY_SHA256 = (
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
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
        "b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5",
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
        "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc",
    ),
    (
        "0014_monomer_md_task_queue_cancel",
        "7d91b451371eaf10542440c8b947c9ac50b51e3d553cb205a76aca196eaf8df6",
    ),
    (
        "0015_property_filter_performance",
        "e0159576c09d31de8a7da46f728d36553f67aa75adba344f93cdc302cf000732",
    ),
)

HELPERS: dict[str, dict[str, str]] = {
    "bootstrap-quiesce": {
        "authority": "reviewed-wrapper",
        "effect": "isolate-ingress-and-drain",
    },
    "bootstrap-status": {
        "authority": "reviewed-wrapper",
        "effect": "read-only",
    },
    "bootstrap-resume-unchanged": {
        "authority": "reviewed-wrapper",
        "effect": "restore-ingress-only",
    },
    "bootstrap-rollback": {
        "authority": "reviewed-wrapper",
        "effect": "restore-legacy-runtime",
    },
    "bootstrap-active-jobs-probe": {
        "authority": "site-specific",
        "effect": "read-only",
    },
    "bootstrap-legacy-runtime-status": {
        "authority": "site-specific",
        "effect": "read-only",
    },
    "bootstrap-legacy-runtime-resume-unchanged": {
        "authority": "site-specific",
        "effect": "restore-ingress-only",
    },
    "bootstrap-legacy-runtime-restore": {
        "authority": "site-specific",
        "effect": "restore-legacy-runtime",
    },
    "contract-0012-external-database-audit": {
        "authority": "site-specific",
        "effect": "read-only",
    },
    "deployment-mutable-data-audit": {
        "authority": "site-specific",
        "effect": "read-only",
    },
    "production-readiness-collector": {
        "authority": "site-specific",
        "effect": "read-only",
    },
}

EVIDENCE_HELPERS = {
    "bootstrap-active-jobs-probe",
    "bootstrap-legacy-runtime-status",
    "bootstrap-legacy-runtime-resume-unchanged",
    "bootstrap-legacy-runtime-restore",
    "contract-0012-external-database-audit",
    "deployment-mutable-data-audit",
}


class SiteHelperContractError(RuntimeError):
    """A helper installation or evidence contract is unsafe."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require_bool(document: dict[str, Any], name: str, expected: bool) -> None:
    if document.get(name) is not expected:
        raise SiteHelperContractError(f"helper evidence did not prove {name}")


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SiteHelperContractError(f"helper evidence has invalid {label}")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise SiteHelperContractError(f"helper evidence has invalid {label}")
    return value


def legacy_runtime_identity(document: dict[str, Any]) -> str:
    version = document.get("schema_version", 1)
    digest_fields = [
        "backend_image_id",
        "web_image_id",
        "worker_unit_sha256",
    ]
    if version == 2:
        digest_fields.extend(
            [
                "backend_process_spec_sha256",
                "web_process_spec_sha256",
                "worker_manager_environment_sha256",
                "postgres_image_id",
            ]
        )
    identity: dict[str, Any] = {
        name: _require_digest(document.get(name), name) for name in digest_fields
    }
    if version == 2:
        for name in (
            "backend_container_id",
            "web_container_id",
            "postgres_container_id",
        ):
            value = document.get(name)
            if not isinstance(value, str) or CONTAINER_RE.fullmatch(value) is None:
                raise SiteHelperContractError(
                    f"helper evidence has invalid {name}"
                )
            identity[name] = value
        unit = document.get("worker_unit_name")
        if not isinstance(unit, str) or SYSTEMD_UNIT_RE.fullmatch(unit) is None:
            raise SiteHelperContractError(
                "helper evidence has invalid worker_unit_name"
            )
        identity["worker_unit_name"] = unit
        manager_uid = document.get("worker_manager_uid")
        if (
            isinstance(manager_uid, bool)
            or not isinstance(manager_uid, int)
            or manager_uid < 0
        ):
            raise SiteHelperContractError(
                "helper evidence has invalid worker_manager_uid"
            )
        runtime_dir = document.get("worker_manager_runtime_dir")
        if runtime_dir != f"/run/user/{manager_uid}":
            raise SiteHelperContractError(
                "helper evidence has invalid worker_manager_runtime_dir"
            )
        identity["worker_manager_uid"] = manager_uid
        identity["worker_manager_runtime_dir"] = runtime_dir
        unit_path = document.get("worker_unit_path")
        if (
            not isinstance(unit_path, str)
            or not Path(unit_path).is_absolute()
            or ".." in Path(unit_path).parts
            or Path(unit_path).name != unit
        ):
            raise SiteHelperContractError(
                "helper evidence has invalid worker_unit_path"
            )
        unit_mode = document.get("worker_unit_mode")
        if (
            not isinstance(unit_mode, str)
            or re.fullmatch(r"[0-7]{4}", unit_mode) is None
        ):
            raise SiteHelperContractError(
                "helper evidence has invalid worker_unit_mode"
            )
        unit_uid = document.get("worker_unit_uid")
        unit_gid = document.get("worker_unit_gid")
        if (
            isinstance(unit_uid, bool)
            or not isinstance(unit_uid, int)
            or unit_uid != manager_uid
            or isinstance(unit_gid, bool)
            or not isinstance(unit_gid, int)
            or unit_gid < 0
        ):
            raise SiteHelperContractError(
                "helper evidence has invalid Worker unit ownership"
            )
        identity["worker_unit_path"] = unit_path
        identity["worker_unit_mode"] = unit_mode
        identity["worker_unit_uid"] = unit_uid
        identity["worker_unit_gid"] = unit_gid
        volume = document.get("postgres_data_volume")
        if not isinstance(volume, str) or VOLUME_RE.fullmatch(volume) is None:
            raise SiteHelperContractError(
                "helper evidence has invalid postgres_data_volume"
            )
        system_identifier = document.get("postgres_system_identifier")
        if (
            not isinstance(system_identifier, str)
            or PG_SYSTEM_ID_RE.fullmatch(system_identifier) is None
        ):
            raise SiteHelperContractError(
                "helper evidence has invalid postgres_system_identifier"
            )
        identity["postgres_data_volume"] = volume
        identity["postgres_system_identifier"] = system_identifier
    return sha256_bytes(canonical_json_bytes(identity))


def _validate_live_process_fields_v2(document: dict[str, Any]) -> None:
    for name in (
        "backend_pid",
        "web_pid",
        "worker_main_pid",
        "worker_active_enter_monotonic",
    ):
        _require_positive_int(document[name], name)
    for name in ("backend_restart_count", "web_restart_count"):
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SiteHelperContractError(
                f"legacy-status {name} is invalid"
            )
    for name in (
        "backend_started_at",
        "web_started_at",
        "worker_invocation_id",
    ):
        if not isinstance(document[name], str) or not document[name]:
            raise SiteHelperContractError(f"legacy-status {name} is invalid")


def _validate_legacy_status_v2(
    document: dict[str, Any],
    *,
    expected_runtime_digest: str | None,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "legacy_runtime_state",
        "backend_image_id",
        "web_image_id",
        "worker_unit_sha256",
        "backend_container_id",
        "web_container_id",
        "backend_process_spec_sha256",
        "web_process_spec_sha256",
        "worker_unit_name",
        "worker_unit_path",
        "worker_unit_mode",
        "worker_unit_uid",
        "worker_unit_gid",
        "worker_manager_uid",
        "worker_manager_runtime_dir",
        "worker_manager_environment_sha256",
        "postgres_container_id",
        "postgres_image_id",
        "postgres_data_volume",
        "postgres_system_identifier",
        "backend_pid",
        "web_pid",
        "backend_started_at",
        "web_started_at",
        "backend_restart_count",
        "web_restart_count",
        "worker_main_pid",
        "worker_invocation_id",
        "worker_active_enter_monotonic",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_open",
    }
    if set(document) != fields:
        raise SiteHelperContractError("legacy-status v2 evidence has an invalid shape")
    identity = legacy_runtime_identity(document)
    if expected_runtime_digest is not None and identity != _require_digest(
        expected_runtime_digest, "expected runtime digest"
    ):
        raise SiteHelperContractError("legacy-status selected another runtime")
    state = document.get("legacy_runtime_state")
    backend_dynamic_fields = {
        "backend_pid",
        "backend_started_at",
        "backend_restart_count",
    }
    web_dynamic_fields = {
        "web_pid",
        "web_started_at",
        "web_restart_count",
    }
    worker_dynamic_fields = {
        "worker_main_pid",
        "worker_invocation_id",
        "worker_active_enter_monotonic",
    }
    health_fields = {
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_open",
    }
    if state == "stopped":
        dynamic_fields = (
            backend_dynamic_fields | web_dynamic_fields | worker_dynamic_fields
        )
        if any(document[name] is not None for name in dynamic_fields) or any(
            document[name] is not False for name in health_fields
        ):
            raise SiteHelperContractError("legacy-status v2 stopped state is partial")
    elif state == "open":
        expected_health = {
            "backend_healthy": True,
            "web_healthy": True,
            "worker_healthy": True,
            "ingress_open": True,
        }
        if any(document[name] is not value for name, value in expected_health.items()):
            raise SiteHelperContractError(
                "legacy-status v2 health state is inconsistent"
            )
        _validate_live_process_fields_v2(document)
    elif state == "isolated":
        expected_health = {
            "backend_healthy": True,
            "web_healthy": False,
            "worker_healthy": True,
            "ingress_open": False,
        }
        if any(document[name] is not value for name, value in expected_health.items()):
            raise SiteHelperContractError(
                "legacy-status v2 health state is inconsistent"
            )
        for name in (
            "backend_pid",
            "worker_main_pid",
            "worker_active_enter_monotonic",
        ):
            _require_positive_int(document[name], name)
        backend_restarts = document["backend_restart_count"]
        if (
            isinstance(backend_restarts, bool)
            or not isinstance(backend_restarts, int)
            or backend_restarts < 0
        ):
            raise SiteHelperContractError(
                "legacy-status backend_restart_count is invalid"
            )
        for name in (
            "backend_started_at",
            "worker_invocation_id",
        ):
            if not isinstance(document[name], str) or not document[name]:
                raise SiteHelperContractError(f"legacy-status {name} is invalid")
        if any(document[name] is not None for name in web_dynamic_fields):
            raise SiteHelperContractError(
                "legacy-status v2 isolated Web is not stopped"
            )
    else:
        raise SiteHelperContractError("legacy-status runtime state is unsupported")
    return dict(document)


def validate_active_jobs(document: object) -> dict[str, Any]:
    fields = {"ingress_isolated", "active_jobs", "active_total"}
    versioned_fields = fields | {"active_jobs_schema_version"}
    if not isinstance(document, dict) or frozenset(document) not in {
        frozenset(fields),
        frozenset(versioned_fields),
    }:
        raise SiteHelperContractError("active-jobs evidence has an invalid shape")
    version = document.get("active_jobs_schema_version", 1)
    if isinstance(version, bool) or version not in {1, 2}:
        raise SiteHelperContractError("active-jobs evidence schema is unsupported")
    _require_bool(document, "ingress_isolated", True)
    expected = ACTIVE_JOB_FIELDS_V2 if version == 2 else ACTIVE_JOB_FIELDS_V1
    jobs = document.get("active_jobs")
    if not isinstance(jobs, dict) or set(jobs) != set(expected):
        raise SiteHelperContractError("active-jobs evidence categories differ")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in jobs.values()
    ):
        raise SiteHelperContractError("active-jobs evidence count is invalid")
    total = document.get("active_total")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total != sum(jobs.values())
    ):
        raise SiteHelperContractError("active-jobs total is inconsistent")
    return {
        "active_jobs_schema_version": version,
        "ingress_isolated": True,
        "active_jobs": {name: jobs[name] for name in sorted(jobs)},
        "active_total": total,
    }


def validate_legacy_status(
    document: object,
    *,
    expected_runtime_digest: str | None,
) -> dict[str, Any]:
    if isinstance(document, dict) and document.get("schema_version") == 2:
        return _validate_legacy_status_v2(
            document,
            expected_runtime_digest=expected_runtime_digest,
        )
    fields = {
        "schema_version",
        "legacy_runtime_state",
        "backend_image_id",
        "web_image_id",
        "worker_unit_sha256",
        "backend_container_id",
        "backend_pid",
        "backend_started_at",
        "backend_restart_count",
        "worker_main_pid",
        "worker_invocation_id",
        "worker_active_enter_monotonic",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_open",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
    ):
        raise SiteHelperContractError("legacy-status evidence has an invalid shape")
    identity = legacy_runtime_identity(document)
    if expected_runtime_digest is not None and identity != _require_digest(
        expected_runtime_digest, "expected runtime digest"
    ):
        raise SiteHelperContractError("legacy-status selected another runtime")
    state = document.get("legacy_runtime_state")
    process_fields = {
        "backend_container_id",
        "backend_pid",
        "backend_started_at",
        "backend_restart_count",
        "worker_main_pid",
        "worker_invocation_id",
        "worker_active_enter_monotonic",
    }
    health_fields = {
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_open",
    }
    if state == "stopped":
        if any(document[name] is not None for name in process_fields) or any(
            document[name] is not False for name in health_fields
        ):
            raise SiteHelperContractError("legacy-status stopped state is partial")
    elif state in {"open", "isolated"}:
        expected_health = {
            "backend_healthy": True,
            "web_healthy": state == "open",
            "worker_healthy": True,
            "ingress_open": state == "open",
        }
        if any(document[name] is not value for name, value in expected_health.items()):
            raise SiteHelperContractError("legacy-status health state is inconsistent")
        if (
            not isinstance(document["backend_container_id"], str)
            or CONTAINER_RE.fullmatch(document["backend_container_id"]) is None
        ):
            raise SiteHelperContractError("legacy-status container ID is invalid")
        for name in (
            "backend_pid",
            "worker_main_pid",
            "worker_active_enter_monotonic",
        ):
            _require_positive_int(document[name], name)
        restarts = document["backend_restart_count"]
        if isinstance(restarts, bool) or not isinstance(restarts, int) or restarts < 0:
            raise SiteHelperContractError("legacy-status restart count is invalid")
        for name in ("backend_started_at", "worker_invocation_id"):
            if not isinstance(document[name], str) or not document[name]:
                raise SiteHelperContractError(f"legacy-status {name} is invalid")
    else:
        raise SiteHelperContractError("legacy-status runtime state is unsupported")
    return dict(document)


def validate_legacy_resume(
    document: object,
    *,
    expected_runtime_digest: str | None,
) -> dict[str, Any]:
    if isinstance(document, dict) and document.get("schema_version") == 2:
        stable = {
            "backend_image_id",
            "web_image_id",
            "worker_unit_sha256",
            "backend_container_id",
            "web_container_id",
            "backend_process_spec_sha256",
            "web_process_spec_sha256",
            "worker_unit_name",
            "worker_unit_path",
            "worker_unit_mode",
            "worker_unit_uid",
            "worker_unit_gid",
            "worker_manager_uid",
            "worker_manager_runtime_dir",
            "worker_manager_environment_sha256",
            "postgres_container_id",
            "postgres_image_id",
            "postgres_data_volume",
            "postgres_system_identifier",
        }
        paired = {
            f"{name}_{suffix}"
            for name in (
                "backend_pid",
                "backend_started_at",
                "backend_restart_count",
                "worker_main_pid",
                "worker_invocation_id",
                "worker_active_enter_monotonic",
            )
            for suffix in ("before", "after")
        }
        fields_v2 = stable | paired | {
            "schema_version",
            "legacy_runtime_unchanged",
            "web_pid_before",
            "web_started_at_before",
            "web_restart_count_before",
            "web_pid_after",
            "web_started_at_after",
            "web_restart_count_after",
            "backend_healthy",
            "web_healthy",
            "worker_healthy",
            "ingress_restored",
        }
        if set(document) != fields_v2:
            raise SiteHelperContractError(
                "legacy-resume v2 evidence has an invalid shape"
            )
        for name in (
            "legacy_runtime_unchanged",
            "backend_healthy",
            "web_healthy",
            "worker_healthy",
            "ingress_restored",
        ):
            _require_bool(document, name, True)
        identity = legacy_runtime_identity(document)
        if expected_runtime_digest is not None and identity != _require_digest(
            expected_runtime_digest, "expected runtime digest"
        ):
            raise SiteHelperContractError("legacy-resume selected another runtime")
        for prefix in (
            "backend_pid",
            "backend_started_at",
            "backend_restart_count",
            "worker_main_pid",
            "worker_invocation_id",
            "worker_active_enter_monotonic",
        ):
            if document[f"{prefix}_before"] != document[f"{prefix}_after"]:
                raise SiteHelperContractError(f"legacy-resume changed {prefix}")
        for name in (
            "backend_pid_before",
            "worker_main_pid_before",
            "worker_active_enter_monotonic_before",
            "web_pid_after",
        ):
            _require_positive_int(document[name], name)
        for name in (
            "backend_started_at_before",
            "worker_invocation_id_before",
            "web_started_at_after",
        ):
            if not isinstance(document[name], str) or not document[name]:
                raise SiteHelperContractError(f"legacy-resume {name} is invalid")
        for name in ("backend_restart_count_before", "web_restart_count_after"):
            value = document[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SiteHelperContractError(f"legacy-resume {name} is invalid")
        if any(
            document[name] is not None
            for name in (
                "web_pid_before",
                "web_started_at_before",
                "web_restart_count_before",
            )
        ):
            raise SiteHelperContractError(
                "legacy-resume did not start from stopped Web"
            )
        return dict(document)
    fields = {
        "schema_version",
        "legacy_runtime_unchanged",
        "backend_image_id",
        "web_image_id",
        "worker_unit_sha256",
        "backend_container_id_before",
        "backend_container_id_after",
        "backend_pid_before",
        "backend_pid_after",
        "backend_started_at_before",
        "backend_started_at_after",
        "backend_restart_count_before",
        "backend_restart_count_after",
        "worker_main_pid_before",
        "worker_main_pid_after",
        "worker_invocation_id_before",
        "worker_invocation_id_after",
        "worker_active_enter_monotonic_before",
        "worker_active_enter_monotonic_after",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_restored",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
    ):
        raise SiteHelperContractError("legacy-resume evidence has an invalid shape")
    for name in (
        "legacy_runtime_unchanged",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_restored",
    ):
        _require_bool(document, name, True)
    identity = legacy_runtime_identity(document)
    if expected_runtime_digest is not None and identity != _require_digest(
        expected_runtime_digest, "expected runtime digest"
    ):
        raise SiteHelperContractError("legacy-resume selected another runtime")
    before_after = (
        ("backend_container_id", CONTAINER_RE),
        ("backend_pid", None),
        ("backend_started_at", None),
        ("backend_restart_count", None),
        ("worker_main_pid", None),
        ("worker_invocation_id", None),
        ("worker_active_enter_monotonic", None),
    )
    for prefix, expression in before_after:
        before = document[f"{prefix}_before"]
        after = document[f"{prefix}_after"]
        if before != after:
            raise SiteHelperContractError(f"legacy-resume changed {prefix}")
        if expression is not None and (
            not isinstance(before, str) or expression.fullmatch(before) is None
        ):
            raise SiteHelperContractError(f"legacy-resume {prefix} is invalid")
    for name in ("backend_pid_before", "worker_main_pid_before", "worker_active_enter_monotonic_before"):
        _require_positive_int(document[name], name)
    restart_count = document["backend_restart_count_before"]
    if (
        isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 0
    ):
        raise SiteHelperContractError("legacy-resume restart count is invalid")
    for name in ("backend_started_at_before", "worker_invocation_id_before"):
        if not isinstance(document[name], str) or not document[name]:
            raise SiteHelperContractError(f"legacy-resume {name} is invalid")
    return dict(document)


def validate_legacy_restore(
    document: object,
    *,
    expected_runtime_digest: str | None,
) -> dict[str, Any]:
    if isinstance(document, dict) and document.get("schema_version") == 2:
        fields_v2 = {
            "schema_version",
            "legacy_runtime_restored",
            "backend_image_id",
            "web_image_id",
            "worker_unit_sha256",
            "backend_container_id",
            "web_container_id",
            "backend_process_spec_sha256",
            "web_process_spec_sha256",
            "worker_unit_name",
            "worker_unit_path",
            "worker_unit_mode",
            "worker_unit_uid",
            "worker_unit_gid",
            "worker_manager_uid",
            "worker_manager_runtime_dir",
            "worker_manager_environment_sha256",
            "postgres_container_id",
            "postgres_image_id",
            "postgres_data_volume",
            "postgres_system_identifier",
            "backend_pid",
            "web_pid",
            "backend_started_at",
            "web_started_at",
            "backend_restart_count",
            "web_restart_count",
            "worker_main_pid",
            "worker_invocation_id",
            "worker_active_enter_monotonic",
            "backend_healthy",
            "web_healthy",
            "worker_healthy",
            "ingress_restored",
        }
        if set(document) != fields_v2:
            raise SiteHelperContractError(
                "legacy-restore v2 evidence has an invalid shape"
            )
        for name in (
            "legacy_runtime_restored",
            "backend_healthy",
            "web_healthy",
            "worker_healthy",
            "ingress_restored",
        ):
            _require_bool(document, name, True)
        identity = legacy_runtime_identity(document)
        if expected_runtime_digest is not None and identity != _require_digest(
            expected_runtime_digest, "expected runtime digest"
        ):
            raise SiteHelperContractError("legacy-restore selected another runtime")
        _validate_live_process_fields_v2(document)
        return dict(document)
    fields = {
        "schema_version",
        "legacy_runtime_restored",
        "backend_image_id",
        "web_image_id",
        "worker_unit_sha256",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_restored",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
    ):
        raise SiteHelperContractError("legacy-restore evidence has an invalid shape")
    for name in (
        "legacy_runtime_restored",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_restored",
    ):
        _require_bool(document, name, True)
    identity = legacy_runtime_identity(document)
    if expected_runtime_digest is not None and identity != _require_digest(
        expected_runtime_digest, "expected runtime digest"
    ):
        raise SiteHelperContractError("legacy-restore selected another runtime")
    return dict(document)


def _validate_external_database_audit_schema_v1(
    document: object,
    *,
    expected_users: dict[str, str] | None,
    expected_media_registry_digest: str | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "inventory_complete",
        "writable_target",
        "media_registry",
        "databases",
        "media",
        "requires_0014",
    }:
        raise SiteHelperContractError("external-database evidence has an invalid shape")
    if (
        document.get("schema_version") != 2
        or document.get("inventory_complete") is not True
        or document.get("writable_target")
        != {"stack": "production", "database": "nexpoly"}
    ):
        raise SiteHelperContractError("external-database inventory is incomplete")
    registry = document.get("media_registry")
    if not isinstance(registry, dict) or set(registry) != {
        "schema_version",
        "sha256",
        "captured_at",
        "expected_media_ids",
        "discovered_media_ids",
    }:
        raise SiteHelperContractError("external media registry has an invalid shape")
    registry_digest = _require_digest(
        registry.get("sha256"),
        "external media registry digest",
    )
    if (
        registry.get("schema_version") != 1
        or not isinstance(registry.get("captured_at"), str)
        or RFC3339_UTC_RE.fullmatch(registry["captured_at"]) is None
    ):
        raise SiteHelperContractError("external media registry identity is invalid")
    if (
        expected_media_registry_digest is not None
        and registry_digest != _require_digest(
            expected_media_registry_digest,
            "expected external media registry digest",
        )
    ):
        raise SiteHelperContractError("external media registry digest differs")
    expected_media_ids = registry.get("expected_media_ids")
    discovered_media_ids = registry.get("discovered_media_ids")
    if (
        not isinstance(expected_media_ids, list)
        or not expected_media_ids
        or any(
            not isinstance(media_id, str)
            or MEDIA_ID_RE.fullmatch(media_id) is None
            for media_id in expected_media_ids
        )
        or expected_media_ids != sorted(set(expected_media_ids))
        or discovered_media_ids != expected_media_ids
    ):
        raise SiteHelperContractError(
            "external media discovery differs from its complete registry"
        )

    databases = document.get("databases")
    if not isinstance(databases, list) or len(databases) != 2:
        raise SiteHelperContractError("external-database inventory differs")
    fields = {
        "stack",
        "database",
        "current_user",
        "transaction_read_only",
        "role_superuser",
        "role_create_db",
        "role_create_role",
        "ledger",
        "legacy_relation_present",
    }
    expected_stacks = {"nexpoly_dev", "nexpoly_md_health_opt"}
    records: dict[str, dict[str, Any]] = {}
    for record in databases:
        if not isinstance(record, dict) or set(record) != fields:
            raise SiteHelperContractError("external-database record is invalid")
        stack = record.get("stack")
        if (
            stack not in expected_stacks
            or stack in records
            or record.get("database") != stack
        ):
            raise SiteHelperContractError("external-database stack identity differs")
        user = record.get("current_user")
        if (
            not isinstance(user, str)
            or ROLE_RE.fullmatch(user) is None
            or expected_users is not None
            and user != expected_users.get(stack)
        ):
            raise SiteHelperContractError("external-database audit user differs")
        for name in (
            "transaction_read_only",
            "role_superuser",
            "role_create_db",
            "role_create_role",
        ):
            expected = name == "transaction_read_only"
            _require_bool(record, name, expected)
        if not isinstance(record.get("legacy_relation_present"), bool):
            raise SiteHelperContractError("external-database relation state is invalid")
        ledger = record.get("ledger")
        if not isinstance(ledger, list) or not ledger:
            raise SiteHelperContractError("external-database ledger is empty")
        seen: set[str] = set()
        for row in ledger:
            if (
                not isinstance(row, dict)
                or set(row) != {"version", "checksum"}
                or not isinstance(row.get("version"), str)
                or MIGRATION_RE.fullmatch(row["version"]) is None
                or row["version"] in seen
                or not isinstance(row.get("checksum"), str)
                or re.fullmatch(r"[0-9a-f]{64}", row["checksum"]) is None
            ):
                raise SiteHelperContractError("external-database ledger is invalid")
            seen.add(row["version"])
        records[str(stack)] = dict(record)
    if set(records) != expected_stacks:
        raise SiteHelperContractError("external-database inventory is incomplete")

    raw_media = document.get("media")
    if not isinstance(raw_media, list):
        raise SiteHelperContractError("external media inventory is invalid")
    media_records: dict[str, dict[str, Any]] = {}
    requires_0014 = False
    media_fields = {
        "media_id",
        "kind",
        "database",
        "source_identity_before",
        "source_identity_after",
        "source_content_sha256",
        "audit",
        "ledger",
        "ledger_analysis",
        "legacy_relation_present",
        "migration_0013",
        "disposition",
    }
    for record in raw_media:
        if not isinstance(record, dict) or set(record) != media_fields:
            raise SiteHelperContractError("external media record is invalid")
        media_id = record.get("media_id")
        kind = record.get("kind")
        database = record.get("database")
        if (
            not isinstance(media_id, str)
            or MEDIA_ID_RE.fullmatch(media_id) is None
            or media_id in media_records
            or kind not in {"docker_volume", "postgres_backup"}
            or not isinstance(database, str)
            or ROLE_RE.fullmatch(database) is None
        ):
            raise SiteHelperContractError("external media identity is invalid")
        before = record.get("source_identity_before")
        after = record.get("source_identity_after")
        if not isinstance(before, dict) or before != after:
            raise SiteHelperContractError(
                "external media source identity changed while it was audited"
            )
        if kind == "docker_volume":
            if set(before) != {
                "name",
                "driver",
                "mountpoint",
                "labels_sha256",
                "inspect_sha256",
                "attached_container_ids",
            }:
                raise SiteHelperContractError(
                    "external Docker volume identity is invalid"
                )
            attached = before.get("attached_container_ids")
            if (
                not isinstance(before.get("name"), str)
                or VOLUME_RE.fullmatch(before["name"]) is None
                or media_id
                != media_id_for_locator(
                    "docker_volume",
                    before["name"],
                )
                or not isinstance(before.get("driver"), str)
                or not before["driver"]
                or not isinstance(before.get("mountpoint"), str)
                or not Path(before["mountpoint"]).is_absolute()
                or not isinstance(attached, list)
                or any(
                    not isinstance(container, str)
                    or CONTAINER_RE.fullmatch(container) is None
                    for container in attached
                )
                or attached != sorted(set(attached))
            ):
                raise SiteHelperContractError(
                    "external Docker volume identity is invalid"
                )
            _require_digest(before.get("labels_sha256"), "volume labels digest")
            _require_digest(before.get("inspect_sha256"), "volume inspect digest")
            allowed_dispositions = {
                "writable-target",
                "read-only-online",
                "retained-private-isolated",
            }
            if record.get("disposition") not in allowed_dispositions:
                raise SiteHelperContractError(
                    "external Docker volume disposition is invalid"
                )
            if (
                record["disposition"] == "retained-private-isolated"
                and attached
            ):
                raise SiteHelperContractError(
                    "isolated Docker volume is still attached"
                )
        else:
            if set(before) != {
                "path",
                "device",
                "inode",
                "size_bytes",
                "mtime_ns",
                "mode",
                "uid",
                "sha256",
            }:
                raise SiteHelperContractError(
                    "external PostgreSQL backup identity is invalid"
                )
            path = before.get("path")
            if (
                not isinstance(path, str)
                or not Path(path).is_absolute()
                or ".." in Path(path).parts
                or media_id
                != media_id_for_locator("postgres_backup", path)
                or any(
                    isinstance(before.get(name), bool)
                    or not isinstance(before.get(name), int)
                    or before[name] < 0
                    for name in (
                        "device",
                        "inode",
                        "size_bytes",
                        "mtime_ns",
                        "mode",
                        "uid",
                    )
                )
                or before["mode"] & 0o077
                or record.get("disposition") != "retained-private-isolated"
            ):
                raise SiteHelperContractError(
                    "external PostgreSQL backup identity is invalid"
                )
            backup_digest = _require_digest(
                before.get("sha256"),
                "PostgreSQL backup digest",
            )
            if backup_digest != record.get("source_content_sha256"):
                raise SiteHelperContractError(
                    "external PostgreSQL backup content digest differs"
                )
        _require_digest(
            record.get("source_content_sha256"),
            "external media content digest",
        )
        audit = record.get("audit")
        if (
            not isinstance(audit, dict)
            or set(audit)
            != {
                "method",
                "complete",
                "evidence_sha256",
                "auditor_sha256",
                "postgres_major",
                "audited_at",
            }
            or audit.get("method")
            not in {
                "live-read-only",
                "isolated-volume-copy-read-only",
                "isolated-backup-restore-read-only",
            }
            or audit.get("complete") is not True
            or audit.get("postgres_major") != 16
            or not isinstance(audit.get("audited_at"), str)
            or RFC3339_UTC_RE.fullmatch(audit["audited_at"]) is None
        ):
            raise SiteHelperContractError(
                "external media isolated audit is incomplete"
            )
        _require_digest(audit.get("evidence_sha256"), "media audit evidence digest")
        _require_digest(audit.get("auditor_sha256"), "media auditor digest")
        if (
            kind == "postgres_backup"
            and audit["method"] != "isolated-backup-restore-read-only"
            or kind == "docker_volume"
            and record["disposition"] == "retained-private-isolated"
            and audit["method"] != "isolated-volume-copy-read-only"
            or kind == "docker_volume"
            and record["disposition"] != "retained-private-isolated"
            and audit["method"] != "live-read-only"
        ):
            raise SiteHelperContractError(
                "external media audit method conflicts with its source"
            )
        if not isinstance(record.get("legacy_relation_present"), bool):
            raise SiteHelperContractError(
                "external media relation state is invalid"
            )
        ledger = record.get("ledger")
        if not isinstance(ledger, list):
            raise SiteHelperContractError("external media ledger is invalid")
        known_migrations = dict(CANONICAL_MIGRATION_LEDGER)
        known_migrations[LEGACY_0005_ALIAS_VERSION] = LEGACY_0005_ALIAS_CHECKSUM
        seen_versions: set[str] = set()
        checksum_mismatches: list[dict[str, str]] = []
        migration_0013_rows: list[str] = []
        for row in ledger:
            if (
                not isinstance(row, dict)
                or set(row) != {"version", "checksum"}
                or not isinstance(row.get("version"), str)
                or MIGRATION_RE.fullmatch(row["version"]) is None
                or not isinstance(row.get("checksum"), str)
                or re.fullmatch(r"[0-9a-f]{64}", row["checksum"]) is None
            ):
                raise SiteHelperContractError("external media ledger is invalid")
            version = row["version"]
            checksum = row["checksum"]
            if version in seen_versions:
                raise SiteHelperContractError(
                    "external media ledger contains a duplicate migration"
                )
            seen_versions.add(version)
            expected_checksum = known_migrations.get(version)
            if expected_checksum is None:
                raise SiteHelperContractError(
                    "external media ledger contains an unknown migration"
                )
            if checksum != expected_checksum:
                if (
                    version == "0009_monomer_md_job_leases"
                    and checksum == KNOWN_DIRTY_0009_CHECKSUM
                ):
                    mismatch_status = "known-isolated-dirty"
                elif (
                    version == "0013_monomer_dft_jobs"
                    and checksum == SUPERSEDED_0013_CHECKSUM
                ):
                    mismatch_status = "superseded-requires-0014"
                else:
                    raise SiteHelperContractError(
                        "external media ledger contains an unknown checksum"
                    )
                checksum_mismatches.append(
                    {
                        "version": version,
                        "expected_checksum": expected_checksum,
                        "observed_checksum": checksum,
                        "status": mismatch_status,
                    }
                )
            if version == "0013_monomer_dft_jobs":
                migration_0013_rows.append(checksum)
        if ledger != sorted(ledger, key=lambda row: row["version"]):
            raise SiteHelperContractError(
                "external media ledger is not in canonical version order"
            )
        if len(migration_0013_rows) > 1:
            raise SiteHelperContractError(
                "external media contains multiple 0013 ledger rows"
            )
        if not migration_0013_rows:
            expected_0013 = {"state": "absent", "checksum": None}
        elif migration_0013_rows[0] == CANONICAL_0013_CHECKSUM:
            expected_0013 = {
                "state": "canonical",
                "checksum": CANONICAL_0013_CHECKSUM,
            }
        elif migration_0013_rows[0] == SUPERSEDED_0013_CHECKSUM:
            expected_0013 = {
                "state": "superseded-requires-0014",
                "checksum": SUPERSEDED_0013_CHECKSUM,
            }
            requires_0014 = True
        else:
            raise SiteHelperContractError(
                "external media contains an unknown 0013 checksum"
            )
        if record.get("migration_0013") != expected_0013:
            raise SiteHelperContractError(
                "external media 0013 analysis differs from its raw ledger"
            )
        if any(
            mismatch["status"] == "superseded-requires-0014"
            for mismatch in checksum_mismatches
        ):
            ledger_status = "superseded-requires-0014"
        elif checksum_mismatches:
            ledger_status = "known-isolated-dirty"
        else:
            ledger_status = "canonical"
        expected_analysis = {
            "status": ledger_status,
            "checksum_mismatches": checksum_mismatches,
        }
        if record.get("ledger_analysis") != expected_analysis:
            raise SiteHelperContractError(
                "external media ledger analysis differs from its raw ledger"
            )
        if checksum_mismatches and record["disposition"] != "retained-private-isolated":
            raise SiteHelperContractError(
                "non-canonical external media is not isolated"
            )
        media_records[media_id] = dict(record)
    if sorted(media_records) != expected_media_ids:
        raise SiteHelperContractError(
            "external media records differ from the complete registry"
        )
    writable_media = [
        record
        for record in media_records.values()
        if record["disposition"] == "writable-target"
    ]
    if (
        len(writable_media) != 1
        or writable_media[0]["kind"] != "docker_volume"
        or writable_media[0]["database"] != "nexpoly"
    ):
        raise SiteHelperContractError(
            "external media registry does not identify one production writable volume"
        )
    if document.get("requires_0014") is not requires_0014:
        raise SiteHelperContractError(
            "external media 0014 requirement differs from its ledgers"
        )
    return {
        "schema_version": 2,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": {
            "schema_version": 1,
            "sha256": registry_digest,
            "captured_at": registry["captured_at"],
            "expected_media_ids": expected_media_ids,
            "discovered_media_ids": discovered_media_ids,
        },
        "databases": [records[name] for name in sorted(records)],
        "media": [media_records[name] for name in sorted(media_records)],
        "requires_0014": requires_0014,
    }


def _external_media_ledger_v2(
    ledger: object,
    *,
    legacy_relation_present: bool,
    isolated: bool,
) -> tuple[dict[str, Any], dict[str, str | None], bool]:
    if not isinstance(ledger, list):
        raise SiteHelperContractError("external media ledger is invalid")
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
            raise SiteHelperContractError("external media ledger row is invalid")
        raw.append((row["version"], row["checksum"]))
    if (
        raw != sorted(raw)
        or len({version for version, _checksum in raw}) != len(raw)
    ):
        raise SiteHelperContractError(
            "external media ledger is not unique canonical order"
        )
    alias = [
        value for value in raw if value[0] == LEGACY_0005_ALIAS_VERSION
    ]
    if alias and alias != [
        (LEGACY_0005_ALIAS_VERSION, LEGACY_0005_ALIAS_CHECKSUM)
    ]:
        raise SiteHelperContractError("external media 0005 alias differs")
    stripped = [
        value for value in raw if value[0] != LEGACY_0005_ALIAS_VERSION
    ]
    if len(stripped) > len(CANONICAL_MIGRATION_LEDGER):
        raise SiteHelperContractError("external media ledger extends past authority")
    mismatches: list[dict[str, str]] = []
    for index, observed in enumerate(stripped):
        expected = CANONICAL_MIGRATION_LEDGER[index]
        if observed[0] != expected[0]:
            raise SiteHelperContractError(
                "external media ledger is not a contiguous canonical prefix"
            )
        if observed[1] == expected[1]:
            continue
        if (
            observed[0] == "0009_monomer_md_job_leases"
            and observed[1] == KNOWN_DIRTY_0009_CHECKSUM
        ):
            status = "known-isolated-dirty"
        elif (
            observed[0] == "0013_monomer_dft_jobs"
            and observed[1] == SUPERSEDED_0013_CHECKSUM
        ):
            status = "superseded-requires-0014"
        else:
            raise SiteHelperContractError(
                "external media ledger contains an unknown checksum"
            )
        mismatches.append(
            {
                "version": observed[0],
                "expected_checksum": expected[1],
                "observed_checksum": observed[1],
                "status": status,
            }
        )
    versions = {version for version, _checksum in stripped}
    if alias and (
        "0007_polytao_jobs" not in versions
        or "0012_drop_polytao_jobs" in versions
    ):
        raise SiteHelperContractError(
            "external media 0005 alias is outside its pre-0012 epoch"
        )
    relation_expected = (
        "0007_polytao_jobs" in versions
        and "0012_drop_polytao_jobs" not in versions
    )
    if legacy_relation_present is not relation_expected:
        raise SiteHelperContractError(
            "external media relation conflicts with its migration prefix"
        )
    if any(
        mismatch["status"] == "known-isolated-dirty"
        for mismatch in mismatches
    ):
        if not isolated:
            raise SiteHelperContractError(
                "known dirty 0009 external medium is not isolated"
            )
        ledger_status = "known-isolated-dirty"
    elif any(
        mismatch["status"] == "superseded-requires-0014"
        for mismatch in mismatches
    ):
        ledger_status = "superseded-requires-0014"
    elif not stripped:
        if not isolated:
            raise SiteHelperContractError(
                "empty external migration ledger is not isolated"
            )
        ledger_status = "empty-isolated"
    elif alias:
        ledger_status = "canonical-with-historical-0005-alias"
    else:
        ledger_status = "canonical-prefix"
    observed_0013 = next(
        (
            checksum
            for version, checksum in stripped
            if version == "0013_monomer_dft_jobs"
        ),
        None,
    )
    if observed_0013 is None:
        migration_0013: dict[str, str | None] = {
            "state": "absent",
            "checksum": None,
        }
    elif observed_0013 == CANONICAL_0013_CHECKSUM:
        migration_0013 = {
            "state": "canonical",
            "checksum": observed_0013,
        }
    else:
        migration_0013 = {
            "state": "superseded-requires-0014",
            "checksum": observed_0013,
        }
    return (
        {
            "status": ledger_status,
            "canonical_prefix_length": len(stripped),
            "historical_0005_alias_present": bool(alias),
            "checksum_mismatches": mismatches,
        },
        migration_0013,
        observed_0013 == SUPERSEDED_0013_CHECKSUM,
    )


def _external_attachment_v2(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "container_id",
        "container_name",
        "container_image_id",
        "container_config_sha256",
        "container_created_at",
        "container_started_at",
        "container_finished_at",
        "container_restart_count",
        "state",
        "destination",
        "read_only",
    }:
        raise SiteHelperContractError(
            "external media Docker attachment is invalid"
        )
    container_id = value.get("container_id")
    container_name = value.get("container_name")
    destination = value.get("destination")
    state = value.get("state")
    restart_count = value.get("container_restart_count")
    if (
        not isinstance(container_id, str)
        or CONTAINER_RE.fullmatch(container_id) is None
        or not isinstance(container_name, str)
        or not container_name.startswith("/")
        or len(container_name) < 2
        or _require_digest(
            value.get("container_image_id"),
            "attached container image ID",
        )
        != value.get("container_image_id")
        or _require_digest(
            value.get("container_config_sha256"),
            "attached container config digest",
        )
        != value.get("container_config_sha256")
        or any(
            not isinstance(value.get(name), str)
            or not value[name]
            for name in (
                "container_created_at",
                "container_started_at",
                "container_finished_at",
            )
        )
        or isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 0
        or not isinstance(destination, str)
        or not Path(destination).is_absolute()
        or not isinstance(state, str)
        or not state
        or not isinstance(value.get("read_only"), bool)
    ):
        raise SiteHelperContractError(
            "external media Docker attachment identity differs"
        )
    return dict(value)


def _external_source_identity_v2(
    value: object,
    *,
    kind: str,
    media_id: str,
    allow_readonly_bind: bool = False,
    require_bind_ctime: bool = False,
    require_file_ctime: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SiteHelperContractError("external media source identity is invalid")
    if kind == "docker_volume":
        if set(value) != {
            "name",
            "driver",
            "mountpoint",
            "labels_sha256",
            "inspect_sha256",
            "data_subpath",
            "attached",
        }:
            raise SiteHelperContractError(
                "external Docker volume identity is invalid"
            )
        name = value.get("name")
        attached = value.get("attached")
        if (
            not isinstance(name, str)
            or VOLUME_RE.fullmatch(name) is None
            or media_id
            != media_id_for_locator("docker_volume", name)
            or not isinstance(value.get("driver"), str)
            or not value["driver"]
            or not isinstance(value.get("mountpoint"), str)
            or not Path(value["mountpoint"]).is_absolute()
            or not isinstance(value.get("data_subpath"), str)
            or not isinstance(attached, list)
        ):
            raise SiteHelperContractError(
                "external Docker volume identity differs"
            )
        normalized_attached = [
            _external_attachment_v2(record) for record in attached
        ]
        if normalized_attached != sorted(
            normalized_attached,
            key=canonical_json_bytes,
        ):
            raise SiteHelperContractError(
                "external Docker volume attachments are not canonical"
            )
        _require_digest(value.get("labels_sha256"), "volume labels digest")
        _require_digest(value.get("inspect_sha256"), "volume inspect digest")
    elif kind == "container_bind":
        bind_fields = {
            "path",
            "device",
            "inode",
            "mtime_ns",
            "mode",
            "uid",
            "data_subpath",
            "attached",
        }
        if require_bind_ctime:
            bind_fields.add("ctime_ns")
        redirected = (
            "audit_path" in value or "takeover_redirect" in value
        )
        if redirected:
            bind_fields.update({"audit_path", "takeover_redirect"})
        if set(value) != bind_fields:
            raise SiteHelperContractError(
                "external PostgreSQL bind identity is invalid"
            )
        path = value.get("path")
        attached = value.get("attached")
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
            or media_id
            != media_id_for_locator("container_bind", path)
            or not isinstance(attached, list)
            or not isinstance(value.get("data_subpath"), str)
        ):
            raise SiteHelperContractError(
                "external PostgreSQL bind identity differs"
            )
        for name in (
            "device",
            "inode",
            "mtime_ns",
            "mode",
            "uid",
            *(("ctime_ns",) if require_bind_ctime else ()),
        ):
            field = value.get(name)
            if isinstance(field, bool) or not isinstance(field, int) or field < 0:
                raise SiteHelperContractError(
                    "external PostgreSQL bind metadata is invalid"
                )
        if value["mode"] & (0o022 if allow_readonly_bind else 0o077):
            raise SiteHelperContractError(
                "external PostgreSQL bind permissions are unsafe"
            )
        normalized_attached = [
            _external_attachment_v2(record) for record in attached
        ]
        if normalized_attached != sorted(
            normalized_attached,
            key=canonical_json_bytes,
        ):
            raise SiteHelperContractError(
                "external PostgreSQL bind attachments are not canonical"
            )
        if redirected:
            audit_path = value.get("audit_path")
            redirect = value.get("takeover_redirect")
            redirect_fields = {
                "schema_version",
                "operation_id",
                "operation_state_sha256",
                "move_index",
                "move_seal_sha256",
                "source_root",
                "destination_root",
                "relative_path",
                "audit_path",
                "legacy_stopped_container_ids",
            }
            if (
                not require_bind_ctime
                or not isinstance(audit_path, str)
                or not Path(audit_path).is_absolute()
                or not isinstance(redirect, dict)
                or set(redirect) != redirect_fields
                or redirect.get("schema_version") != 1
                or not isinstance(redirect.get("operation_id"), str)
                or OPERATION_ID_RE.fullmatch(
                    redirect["operation_id"]
                )
                is None
                or not isinstance(redirect.get("move_index"), int)
                or isinstance(redirect.get("move_index"), bool)
                or redirect["move_index"] < 0
                or redirect.get("audit_path") != audit_path
            ):
                raise SiteHelperContractError(
                    "takeover bind redirect authority is invalid"
                )
            for name in (
                "operation_state_sha256",
                "move_seal_sha256",
            ):
                _require_digest(
                    redirect.get(name),
                    f"takeover bind {name}",
                )
            source_root = redirect.get("source_root")
            destination_root = redirect.get("destination_root")
            relative_path = redirect.get("relative_path")
            stopped = redirect.get("legacy_stopped_container_ids")
            if (
                not isinstance(source_root, str)
                or not Path(source_root).is_absolute()
                or not isinstance(destination_root, str)
                or not Path(destination_root).is_absolute()
                or not isinstance(relative_path, str)
                or relative_path != "."
                and (
                    PurePosixPath(relative_path).is_absolute()
                    or str(PurePosixPath(relative_path))
                    != relative_path
                    or any(
                        part in {"", ".", ".."}
                        for part in PurePosixPath(relative_path).parts
                    )
                )
                or not isinstance(stopped, list)
                or any(
                    not isinstance(container_id, str)
                    or CONTAINER_RE.fullmatch(container_id) is None
                    for container_id in stopped
                )
                or stopped != sorted(set(stopped))
            ):
                raise SiteHelperContractError(
                    "takeover bind redirect path binding is invalid"
                )
            expected_source = (
                Path(source_root)
                if relative_path == "."
                else Path(source_root).joinpath(
                    *PurePosixPath(relative_path).parts
                )
            )
            expected_audit = (
                Path(destination_root)
                if relative_path == "."
                else Path(destination_root).joinpath(
                    *PurePosixPath(relative_path).parts
                )
            )
            if (
                Path(path) != expected_source
                or Path(audit_path) != expected_audit
                or not {
                    str(record["container_id"])
                    for record in normalized_attached
                }.issubset(set(stopped))
                or any(
                    str(record["state"])
                    in {
                        "created",
                        "running",
                        "paused",
                        "restarting",
                        "removing",
                    }
                    for record in normalized_attached
                )
            ):
                raise SiteHelperContractError(
                    "takeover bind redirect reader binding differs"
                )
    elif kind == "postgres_backup":
        backup_fields = {
            "path",
            "device",
            "inode",
            "size_bytes",
            "mtime_ns",
            "mode",
            "uid",
            "sha256",
            "format",
        }
        if require_file_ctime:
            backup_fields.add("ctime_ns")
        if set(value) != backup_fields:
            raise SiteHelperContractError(
                "external PostgreSQL backup identity is invalid"
            )
        path = value.get("path")
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
            or media_id
            != media_id_for_locator("postgres_backup", path)
            or value.get("format")
            not in {"postgres-custom-v1", "postgres-tar-v1"}
        ):
            raise SiteHelperContractError(
                "external PostgreSQL backup identity differs"
            )
        for name in (
            "device",
            "inode",
            "size_bytes",
            "mtime_ns",
            *(("ctime_ns",) if require_file_ctime else ()),
            "mode",
            "uid",
        ):
            field = value.get(name)
            if isinstance(field, bool) or not isinstance(field, int) or field < 0:
                raise SiteHelperContractError(
                    "external PostgreSQL backup metadata is invalid"
                )
        if value["mode"] != 0o600:
            raise SiteHelperContractError(
                "external PostgreSQL backup is not private"
            )
        _require_digest(value.get("sha256"), "PostgreSQL backup digest")
    else:
        raise SiteHelperContractError("external media kind is unsupported")
    return dict(value)


def _external_relation_v2(
    value: object,
    *,
    present: bool | None,
    ledger: bool,
) -> dict[str, Any]:
    expected_fields = (
        {"state", "row_count", "schema_sha256", "content_sha256"}
        if not ledger
        else {"state", "row_count", "schema_sha256", "content_sha256"}
    )
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise SiteHelperContractError("external media relation evidence is invalid")
    if value.get("state") not in {"present", "absent"}:
        raise SiteHelperContractError("external media relation state differs")
    observed_present = value["state"] == "present"
    if present is not None and observed_present is not present:
        raise SiteHelperContractError("external media relation state differs")
    row_count = value.get("row_count")
    if observed_present or ledger:
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
        ):
            raise SiteHelperContractError(
                "external media relation row count is invalid"
            )
    elif row_count is not None:
        raise SiteHelperContractError(
            "absent external relation has a row count"
        )
    if observed_present:
        _require_digest(value.get("schema_sha256"), "relation schema digest")
        _require_digest(value.get("content_sha256"), "relation content digest")
    elif (
        value.get("schema_sha256") is not None
        or not ledger
        and value.get("content_sha256") is not None
    ):
        raise SiteHelperContractError(
            "absent external relation has fabricated evidence"
        )
    elif ledger:
        _require_digest(value.get("content_sha256"), "empty ledger digest")
    return dict(value)


def _validate_external_database_audit_schema_v2_retired(
    document: object,
    *,
    expected_users: dict[str, str] | None,
    expected_media_registry_digest: str | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "inventory_complete",
        "writable_target",
        "media_registry",
        "databases",
        "media",
        "requires_0014",
    }:
        raise SiteHelperContractError(
            "external-database evidence v2 has an invalid shape"
        )
    if (
        document.get("schema_version") != 2
        or document.get("inventory_complete") is not True
        or document.get("writable_target")
        != {"stack": "production", "database": "nexpoly"}
    ):
        raise SiteHelperContractError(
            "external-database evidence is not complete"
        )
    registry = document.get("media_registry")
    registry_fields = {
        "schema_version",
        "sha256",
        "discovery_boundary_sha256",
        "discovery_state_sha256_before",
        "discovery_state_sha256_after",
        "captured_at",
        "expected_media_ids",
        "discovered_media_ids",
        "docker_inventory_sha256",
        "backup_inventory_sha256",
        "scanned_volume_names",
        "scanned_bind_sources",
        "scanned_container_ids",
    }
    if (
        not isinstance(registry, dict)
        or set(registry) != registry_fields
        or registry.get("schema_version") != 2
        or not isinstance(registry.get("captured_at"), str)
        or RFC3339_UTC_RE.fullmatch(registry["captured_at"]) is None
    ):
        raise SiteHelperContractError(
            "external media registry v2 identity is invalid"
        )
    registry_digest = _require_digest(
        registry.get("sha256"),
        "external media registry digest",
    )
    if (
        expected_media_registry_digest is not None
        and registry_digest
        != _require_digest(
            expected_media_registry_digest,
            "expected external media registry digest",
        )
    ):
        raise SiteHelperContractError("external media registry digest differs")
    for name in (
        "discovery_boundary_sha256",
        "discovery_state_sha256_before",
        "discovery_state_sha256_after",
        "docker_inventory_sha256",
        "backup_inventory_sha256",
    ):
        _require_digest(registry.get(name), name)
    if (
        registry["discovery_state_sha256_before"]
        != registry["discovery_state_sha256_after"]
    ):
        raise SiteHelperContractError(
            "external media discovery boundary changed during audit"
        )
    expected_ids = registry.get("expected_media_ids")
    discovered_ids = registry.get("discovered_media_ids")
    if (
        not isinstance(expected_ids, list)
        or not expected_ids
        or any(
            not isinstance(media_id, str)
            or MEDIA_ID_RE.fullmatch(media_id) is None
            for media_id in expected_ids
        )
        or expected_ids != sorted(set(expected_ids))
        or discovered_ids != expected_ids
    ):
        raise SiteHelperContractError(
            "external media complete discovery differs from registry v2"
        )
    volume_names = registry.get("scanned_volume_names")
    bind_sources = registry.get("scanned_bind_sources")
    container_ids = registry.get("scanned_container_ids")
    if (
        not isinstance(volume_names, list)
        or volume_names != sorted(set(volume_names))
        or any(
            not isinstance(name, str) or VOLUME_RE.fullmatch(name) is None
            for name in volume_names
        )
        or not isinstance(bind_sources, list)
        or bind_sources != sorted(set(bind_sources))
        or any(
            not isinstance(source, str)
            or not Path(source).is_absolute()
            or ".." in Path(source).parts
            for source in bind_sources
        )
        or not isinstance(container_ids, list)
        or container_ids != sorted(set(container_ids))
        or any(
            not isinstance(container, str)
            or CONTAINER_RE.fullmatch(container) is None
            for container in container_ids
        )
    ):
        raise SiteHelperContractError(
            "external media scanned Docker boundary is invalid"
        )

    media_fields = {
        "media_id",
        "kind",
        "database",
        "disposition",
        "source_identity_before",
        "source_identity_after",
        "source_system_identifier",
        "source_content_sha256",
        "content_identity_algorithm",
        "database_identity",
        "database_identity_sha256",
        "current_user",
        "transaction_read_only",
        "role_superuser",
        "role_create_db",
        "role_create_role",
        "ledger",
        "ledger_sha256",
        "ledger_relation",
        "ledger_analysis",
        "legacy_relation_present",
        "legacy_relation",
        "migration_0013",
        "audit",
    }
    raw_media = document.get("media")
    if not isinstance(raw_media, list):
        raise SiteHelperContractError("external media evidence list is invalid")
    media: dict[str, dict[str, Any]] = {}
    requires_0014 = False
    allowed_methods = {
        "docker_volume": {
            "live-read-only",
            "isolated-volume-copy-read-only",
        },
        "container_bind": {
            "live-read-only",
            "isolated-bind-copy-read-only",
        },
        "postgres_backup": {
            "isolated-backup-restore-read-only",
        },
    }
    algorithms = {
        "live-read-only": "logical-database-identity-v2",
        "isolated-volume-copy-read-only": (
            "postgres-data-directory-tar-sha256-v1"
        ),
        "isolated-bind-copy-read-only": "postgres-private-tree-sha256-v1",
        "isolated-backup-restore-read-only": "sha256-file-v1",
    }
    audit_runtime_identity: tuple[str, str, str] | None = None
    for record in raw_media:
        if not isinstance(record, dict) or set(record) != media_fields:
            raise SiteHelperContractError(
                "external media evidence record is invalid"
            )
        media_id = record.get("media_id")
        kind = record.get("kind")
        database_name = record.get("database")
        disposition = record.get("disposition")
        if (
            not isinstance(media_id, str)
            or MEDIA_ID_RE.fullmatch(media_id) is None
            or media_id in media
            or kind not in allowed_methods
            or not isinstance(database_name, str)
            or ROLE_RE.fullmatch(database_name) is None
            or disposition
            not in {
                "writable-target",
                "read-only-online",
                "retained-private-isolated",
            }
        ):
            raise SiteHelperContractError(
                "external media evidence identity is invalid"
            )
        before = _external_source_identity_v2(
            record.get("source_identity_before"),
            kind=kind,
            media_id=media_id,
        )
        after = _external_source_identity_v2(
            record.get("source_identity_after"),
            kind=kind,
            media_id=media_id,
        )
        if before != after:
            raise SiteHelperContractError(
                "external media source identity changed during audit"
            )
        source_digest = _require_digest(
            record.get("source_content_sha256"),
            "external media source content digest",
        )
        audit = record.get("audit")
        if not isinstance(audit, dict) or set(audit) != {
            "method",
            "complete",
            "auditor_sha256",
            "postgres_major",
            "postgres_image",
            "postgres_image_id",
            "audited_at",
            "isolation",
            "evidence_sha256",
        }:
            raise SiteHelperContractError(
                "external media fresh audit identity is invalid"
            )
        method = audit.get("method")
        if (
            method not in allowed_methods[kind]
            or audit.get("complete") is not True
            or audit.get("postgres_major") != 16
            or not isinstance(audit.get("postgres_image"), str)
            or OCI_IMAGE_DIGEST_RE.fullmatch(audit["postgres_image"]) is None
            or not isinstance(audit.get("audited_at"), str)
            or RFC3339_UTC_RE.fullmatch(audit["audited_at"]) is None
            or record.get("content_identity_algorithm") != algorithms[method]
        ):
            raise SiteHelperContractError(
                "external media audit method or PG16 runtime differs"
            )
        auditor_digest = _require_digest(
            audit.get("auditor_sha256"),
            "media auditor digest",
        )
        postgres_image_id = _require_digest(
            audit.get("postgres_image_id"),
            "PG16 image ID",
        )
        observed_runtime = (
            auditor_digest,
            audit["postgres_image"],
            postgres_image_id,
        )
        if audit_runtime_identity is None:
            audit_runtime_identity = observed_runtime
        elif audit_runtime_identity != observed_runtime:
            raise SiteHelperContractError(
                "external media evidence mixes audit runtimes"
            )
        evidence_digest = _require_digest(
            audit.get("evidence_sha256"),
            "media evidence digest",
        )
        isolation = audit.get("isolation")
        expected_isolation: dict[str, object]
        if method == "live-read-only":
            expected_isolation = {
                "source_mounted_by_auditor": False,
                "source_started_by_auditor": False,
                "transaction_read_only": True,
            }
        elif method == "isolated-volume-copy-read-only":
            expected_isolation = {
                "source_mounted_read_only": True,
                "source_started_as_postgres": False,
                "scratch_network": "none",
                "scratch_destroyed": True,
                "copy_method": (
                    "readonly-tar-copy-to-disposable-volume-v1"
                ),
            }
        elif method == "isolated-bind-copy-read-only":
            expected_isolation = {
                "source_mounted_read_only": False,
                "source_opened_with_openat_no_follow": True,
                "source_started_as_postgres": False,
                "scratch_network": "none",
                "scratch_destroyed": True,
                "copy_method": (
                    "private-openat-copy-to-disposable-volume-v1"
                ),
            }
        else:
            expected_isolation = {
                "source_opened_with_openat_no_follow": True,
                "source_passed_to_docker": False,
                "staged_snapshot_mounted_read_only": True,
                "source_started_as_postgres": False,
                "scratch_network": "none",
                "scratch_destroyed": True,
                "restore_method": (
                    "pg_restore-no-owner-no-privileges-v1"
                ),
            }
        if isolation != expected_isolation:
            raise SiteHelperContractError(
                "external media isolation proof is incomplete"
            )
        if (
            method == "live-read-only"
            and disposition == "retained-private-isolated"
            or method != "live-read-only"
            and disposition != "retained-private-isolated"
        ):
            raise SiteHelperContractError(
                "external media method conflicts with disposition"
            )
        attachments = (
            before.get("attached")
            if kind in {"docker_volume", "container_bind"}
            else []
        )
        if not isinstance(attachments, list):
            raise SiteHelperContractError(
                "external media attachment evidence is invalid"
            )
        active_attachments = [
            value
            for value in attachments
            if value["state"]
            in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
            }
        ]
        if (
            method == "live-read-only"
            and len(active_attachments) != 1
            or method != "live-read-only"
            and active_attachments
        ):
            raise SiteHelperContractError(
                "external media active-reader state conflicts with audit method"
            )
        if (
            kind == "docker_volume"
            and before["name"] not in volume_names
            or any(
                value["container_id"] not in container_ids
                for value in attachments
            )
        ):
            raise SiteHelperContractError(
                "external media source was outside the scanned Docker boundary"
            )
        database_identity = record.get("database_identity")
        if not isinstance(database_identity, dict) or set(database_identity) != {
            "database",
            "system_identifier",
            "system_identifier_scope",
            "database_oid",
            "database_owner",
            "encoding",
            "collate",
            "ctype",
            "server_version_num",
        }:
            raise SiteHelperContractError(
                "external media database identity is invalid"
            )
        server_version_num = database_identity.get("server_version_num")
        expected_system_scope = (
            "source-cluster"
            if method == "live-read-only"
            else "isolated-restore-cluster"
            if method == "isolated-backup-restore-read-only"
            else "copied-source-cluster"
        )
        if (
            database_identity.get("database") != database_name
            or not isinstance(database_identity.get("system_identifier"), str)
            or PG_SYSTEM_ID_RE.fullmatch(
                database_identity["system_identifier"]
            )
            is None
            or database_identity.get("system_identifier_scope")
            != expected_system_scope
            or isinstance(server_version_num, bool)
            or not isinstance(server_version_num, int)
            or server_version_num // 10000 != 16
        ):
            raise SiteHelperContractError(
                "external media PostgreSQL identity differs"
            )
        source_system_identifier = record.get("source_system_identifier")
        if method == "isolated-backup-restore-read-only":
            if source_system_identifier is not None:
                raise SiteHelperContractError(
                    "logical backup fabricated a source system identifier"
                )
        elif (
            not isinstance(source_system_identifier, str)
            or PG_SYSTEM_ID_RE.fullmatch(source_system_identifier) is None
            or source_system_identifier
            != database_identity["system_identifier"]
        ):
            raise SiteHelperContractError(
                "external media source system identifier is not bound"
            )
        database_identity_digest = _require_digest(
            record.get("database_identity_sha256"),
            "database identity digest",
        )
        if database_identity_digest != sha256_bytes(
            canonical_json_bytes(database_identity)
        ):
            raise SiteHelperContractError(
                "external media database identity digest differs"
            )
        user = record.get("current_user")
        if (
            not isinstance(user, str)
            or ROLE_RE.fullmatch(user) is None
            or record.get("transaction_read_only") is not True
            or not isinstance(record.get("role_superuser"), bool)
            or not isinstance(record.get("role_create_db"), bool)
            or not isinstance(record.get("role_create_role"), bool)
            or method == "live-read-only"
            and (
                record["role_superuser"]
                or record["role_create_db"]
                or record["role_create_role"]
            )
            or method != "live-read-only"
            and record["role_superuser"] is not True
        ):
            raise SiteHelperContractError(
                "external media database audit role is unsafe"
            )
        ledger = record.get("ledger")
        if _require_digest(
            record.get("ledger_sha256"),
            "external media ledger digest",
        ) != sha256_bytes(canonical_json_bytes(ledger)):
            raise SiteHelperContractError(
                "external media ledger digest differs"
            )
        legacy_present = record.get("legacy_relation_present")
        if not isinstance(legacy_present, bool):
            raise SiteHelperContractError(
                "external media legacy relation state is invalid"
            )
        isolated = disposition == "retained-private-isolated"
        expected_analysis, expected_0013, needs_0014 = (
            _external_media_ledger_v2(
                ledger,
                legacy_relation_present=legacy_present,
                isolated=isolated,
            )
        )
        if (
            record.get("ledger_analysis") != expected_analysis
            or record.get("migration_0013") != expected_0013
        ):
            raise SiteHelperContractError(
                "external media ledger analysis differs"
            )
        ledger_relation = _external_relation_v2(
            record.get("ledger_relation"),
            present=None,
            ledger=True,
        )
        if (
            ledger_relation["row_count"] != len(ledger)
            or not ledger
            and ledger_relation["state"] == "present"
            and ledger_relation["schema_sha256"] is None
            or ledger
            and ledger_relation["state"] != "present"
            or ledger_relation["content_sha256"]
            != sha256_bytes(canonical_json_bytes(ledger))
        ):
            raise SiteHelperContractError(
                "external media ledger relation content differs"
            )
        _external_relation_v2(
            record.get("legacy_relation"),
            present=legacy_present,
            ledger=False,
        )
        if (
            kind == "postgres_backup"
            and source_digest != before.get("sha256")
        ):
            raise SiteHelperContractError(
                "external backup source content digest differs"
            )
        if method == "live-read-only":
            logical_digest = sha256_bytes(
                canonical_json_bytes(
                    {
                        "database_identity": database_identity,
                        "ledger": ledger,
                        "ledger_relation": record["ledger_relation"],
                        "legacy_relation": record["legacy_relation"],
                    }
                )
            )
            if source_digest != logical_digest:
                raise SiteHelperContractError(
                    "online database logical content digest differs"
                )
        unsealed = {
            **record,
            "audit": {
                key: value
                for key, value in audit.items()
                if key != "evidence_sha256"
            },
        }
        if evidence_digest != sha256_bytes(canonical_json_bytes(unsealed)):
            raise SiteHelperContractError(
                "external media self-sealed evidence digest differs"
            )
        requires_0014 = requires_0014 or needs_0014
        media[media_id] = dict(record)
    if sorted(media) != expected_ids:
        raise SiteHelperContractError(
            "external media evidence differs from complete registry v2"
        )
    writable = [
        record
        for record in media.values()
        if record["disposition"] == "writable-target"
    ]
    if (
        len(writable) != 1
        or writable[0]["database"] != "nexpoly"
        or writable[0]["kind"] not in {"docker_volume", "container_bind"}
    ):
        raise SiteHelperContractError(
            "external media evidence lacks one production writable target"
        )

    databases = document.get("databases")
    if not isinstance(databases, list) or len(databases) != 2:
        raise SiteHelperContractError(
            "external online database evidence is incomplete"
        )
    database_fields = {
        "stack",
        "media_id",
        "database",
        "current_user",
        "transaction_read_only",
        "role_superuser",
        "role_create_db",
        "role_create_role",
        "system_identifier",
        "database_identity_sha256",
        "ledger",
        "ledger_sha256",
        "legacy_relation_present",
    }
    expected_stacks = ["nexpoly_dev", "nexpoly_md_health_opt"]
    normalized_databases: list[dict[str, Any]] = []
    for index, database in enumerate(databases):
        if (
            not isinstance(database, dict)
            or set(database) != database_fields
            or database.get("stack") != expected_stacks[index]
            or database.get("database") != expected_stacks[index]
            or not isinstance(database.get("media_id"), str)
            or database["media_id"] not in media
        ):
            raise SiteHelperContractError(
                "external online database identity differs"
            )
        source_record = media[database["media_id"]]
        expected_database = {
            "stack": expected_stacks[index],
            "media_id": database["media_id"],
            "database": source_record["database"],
            "current_user": source_record["current_user"],
            "transaction_read_only": source_record["transaction_read_only"],
            "role_superuser": source_record["role_superuser"],
            "role_create_db": source_record["role_create_db"],
            "role_create_role": source_record["role_create_role"],
            "system_identifier": source_record["database_identity"][
                "system_identifier"
            ],
            "database_identity_sha256": source_record[
                "database_identity_sha256"
            ],
            "ledger": source_record["ledger"],
            "ledger_sha256": source_record["ledger_sha256"],
            "legacy_relation_present": source_record[
                "legacy_relation_present"
            ],
        }
        if database != expected_database:
            raise SiteHelperContractError(
                "external online database evidence was spliced"
            )
        if (
            expected_users is not None
            and database["current_user"]
            != expected_users.get(expected_stacks[index])
        ):
            raise SiteHelperContractError(
                "external online database audit user differs"
            )
        normalized_databases.append(dict(database))
    if document.get("requires_0014") is not requires_0014:
        raise SiteHelperContractError(
            "external media 0014 requirement differs from complete ledgers"
        )
    return {
        "schema_version": 2,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": dict(registry),
        "databases": normalized_databases,
        "media": [media[name] for name in sorted(media)],
        "requires_0014": requires_0014,
    }


def _external_reviewed_file_identity_v3(
    value: object,
    *,
    media_id: str,
) -> dict[str, Any]:
    fields = {
        "path",
        "device",
        "inode",
        "size_bytes",
        "mtime_ns",
        "ctime_ns",
        "mode",
        "uid",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SiteHelperContractError(
            "reviewed non-PostgreSQL file identity is invalid"
        )
    path = value.get("path")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or ".." in Path(path).parts
        or media_id
        != media_id_for_locator("reviewed_file", path)
        or value.get("mode") != 0o600
    ):
        raise SiteHelperContractError(
            "reviewed non-PostgreSQL file identity differs"
        )
    for name in fields - {"path"}:
        field = value.get(name)
        if isinstance(field, bool) or not isinstance(field, int) or field < 0:
            raise SiteHelperContractError(
                "reviewed non-PostgreSQL file metadata is invalid"
            )
    return dict(value)


def _external_source_identity_v3(
    value: object,
    *,
    kind: str,
    media_id: str,
    allow_readonly_bind: bool = False,
) -> dict[str, Any]:
    if kind == "reviewed_file":
        return _external_reviewed_file_identity_v3(
            value,
            media_id=media_id,
        )
    return _external_source_identity_v2(
        value,
        kind=kind,
        media_id=media_id,
        allow_readonly_bind=allow_readonly_bind,
        require_bind_ctime=kind == "container_bind",
        require_file_ctime=kind == "postgres_backup",
    )


def _external_relation_v3(
    value: object,
    *,
    relation: str,
    owner: str,
    rows: object,
    present: bool,
) -> dict[str, Any]:
    fields = {
        "state",
        "row_count",
        "schema_sha256",
        "schema_authority",
        "content_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SiteHelperContractError(
            "external media relation-v3 evidence is invalid"
        )
    is_ledger = relation == "ledger"
    expected_state = "present" if present else "absent"
    expected_row_count = (
        len(rows)
        if is_ledger and isinstance(rows, list)
        else None
    )
    expected_content = (
        sha256_bytes(canonical_json_bytes(rows))
        if is_ledger
        else None
    )
    if (
        value.get("state") != expected_state
        or (
            is_ledger
            and (
                value.get("row_count") != expected_row_count
                or value.get("content_sha256") != expected_content
            )
        )
        or (
            not is_ledger
            and present
            and (
                isinstance(value.get("row_count"), bool)
                or not isinstance(value.get("row_count"), int)
                or value["row_count"] < 0
                or DIGEST_RE.fullmatch(
                    str(value.get("content_sha256"))
                )
                is None
            )
        )
        or (
            not is_ledger
            and not present
            and (
                value.get("row_count") is not None
                or value.get("content_sha256") is not None
            )
        )
    ):
        raise SiteHelperContractError(
            "external media relation-v3 content differs"
        )
    if not present:
        if (
            value.get("schema_sha256") is not None
            or value.get("schema_authority") is not None
        ):
            raise SiteHelperContractError(
                "absent external relation fabricated schema authority"
            )
        return dict(value)
    expected = (
        LEDGER_SCHEMA_AUTHORITY
        if is_ledger
        else LEGACY_SCHEMA_AUTHORITY
    )
    expected_authority = {**expected, "owner": owner}
    authority = value.get("schema_authority")
    if authority != expected_authority:
        raise SiteHelperContractError(
            "external relation differs from canonical migration authority"
        )
    if value.get("schema_sha256") != sha256_bytes(
        canonical_json_bytes(authority)
    ):
        raise SiteHelperContractError(
            "external canonical relation authority digest differs"
        )
    return dict(value)


def _external_server_startup_v3(
    value: object,
    *,
    data_directory: str,
    online: bool,
) -> dict[str, Any]:
    fields = {
        "jit",
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
        "config_source_files",
        "config_errors",
        "independent_configuration_tree_sha256",
        "verification",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SiteHelperContractError(
            "external PostgreSQL startup evidence shape is invalid"
        )
    sources = value.get("config_source_files")
    root = Path(data_directory)
    if (
        value.get("jit") is not False
        or value.get("shared_preload_libraries") != ""
        or value.get("session_preload_libraries") != ""
        or value.get("local_preload_libraries") != ""
        or value.get("dynamic_library_path") != "$libdir"
        or value.get("archive_mode") != "off"
        or value.get("archive_command")
        != ("" if online else "(disabled)")
        or value.get("archive_cleanup_command") != ""
        or value.get("restore_command")
        != ("" if online else "/bin/false")
        or value.get("recovery_end_command") != ""
        or value.get("ssl_passphrase_command") != ""
        or value.get("ssl_passphrase_command_supports_reload") != "off"
        or value.get("jit_provider") != "llvmjit"
        or not isinstance(sources, list)
        or sources != sorted(set(sources))
        or any(
            not isinstance(source, str)
            or not Path(source).is_absolute()
            or root not in Path(source).parents
            or (
                not source.endswith(".conf")
                and not source.endswith("/postgresql.auto.conf")
            )
            for source in sources
        )
        or value.get("config_file") not in sources
        or value.get("config_errors") != []
        or any(
            not isinstance(value.get(key), str)
            or not Path(value[key]).is_absolute()
            for key in ("config_file", "hba_file", "ident_file")
        )
        or (
            online
            and (
                value.get("verification")
                != "pinned-read-only-config-parse-v1"
                or not isinstance(
                    value.get(
                        "independent_configuration_tree_sha256"
                    ),
                    str,
                )
                or DIGEST_RE.fullmatch(
                    value["independent_configuration_tree_sha256"]
                )
                is None
            )
        )
        or (
            not online
            and (
                value.get("verification")
                != "owned-isolated-cluster-v1"
                or value.get(
                    "independent_configuration_tree_sha256"
                )
                is not None
            )
        )
    ):
        raise SiteHelperContractError(
            "external PostgreSQL startup configuration is unsafe"
        )
    return dict(value)


def _external_database_record_v3(
    value: object,
    *,
    authority: dict[str, Any],
    method: str,
    disposition: str,
) -> tuple[dict[str, Any], bool]:
    fields = {
        "database_identity",
        "database_identity_sha256",
        "current_user",
        "transaction_read_only",
        "server_startup",
        "event_triggers_disabled",
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
        "event_triggers",
        "role_effective_persistent_write",
        "ledger",
        "ledger_sha256",
        "ledger_relation",
        "ledger_analysis",
        "legacy_relation_present",
        "generation_schema",
        "legacy_relation",
        "migration_0013",
        "requires_0014",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SiteHelperContractError(
            "external per-database audit-v3 record is invalid"
        )
    identity = value.get("database_identity")
    identity_fields = {
        "database",
        "system_identifier",
        "system_identifier_scope",
        "database_oid",
        "database_owner",
        "encoding",
        "collate",
        "ctype",
        "server_version_num",
        "data_directory",
    }
    expected_scope = (
        "source-cluster"
        if method in {"live-read-only", "live-read-only-adjacent"}
        else "isolated-restore-cluster"
        if method == "isolated-backup-restore-read-only"
        else "copied-source-cluster"
    )
    if (
        not isinstance(identity, dict)
        or set(identity) != identity_fields
        or identity.get("database") != authority["name"]
        or identity.get("database_oid") != authority["oid"]
        or identity.get("database_owner") != authority["owner"]
        or identity.get("system_identifier_scope") != expected_scope
        or not isinstance(identity.get("system_identifier"), str)
        or PG_SYSTEM_ID_RE.fullmatch(identity["system_identifier"]) is None
        or isinstance(identity.get("server_version_num"), bool)
        or not isinstance(identity.get("server_version_num"), int)
        or identity["server_version_num"] // 10000
        not in POSTGRES_AUDIT_IMAGES
        or not isinstance(identity.get("data_directory"), str)
        or not Path(identity["data_directory"]).is_absolute()
    ):
        raise SiteHelperContractError(
            "external per-database PostgreSQL identity differs"
        )
    if value.get("database_identity_sha256") != sha256_bytes(
        canonical_json_bytes(identity)
    ):
        raise SiteHelperContractError(
            "external per-database identity digest differs"
        )
    _external_server_startup_v3(
        value.get("server_startup"),
        data_directory=str(identity["data_directory"]),
        online=method in {"live-read-only", "live-read-only-adjacent"},
    )
    isolated = disposition == "retained-private-isolated"
    adjacent_admin = method == "live-read-only-adjacent"
    role_contract_marker = value.get("role_contract_marker")
    role_contract_sha256 = value.get("role_contract_sha256")
    if (
        method == "live-read-only"
        and (
            not isinstance(role_contract_sha256, str)
            or DIGEST_RE.fullmatch(role_contract_sha256) is None
            or role_contract_marker
            != (
                "nexpoly-postgres-media-audit-role-v1:"
                + role_contract_sha256
            )
        )
        or method != "live-read-only"
        and (
            role_contract_marker is not None
            or role_contract_sha256 is not None
        )
    ):
        raise SiteHelperContractError(
            "external database audit role contract marker differs"
        )
    role_memberships = value.get("role_memberships")
    incoming_memberships = value.get("role_incoming_memberships")
    role_settings = value.get("role_settings")
    role_owned_objects = value.get("role_owned_objects")
    role_direct_acl = value.get("role_direct_acl")
    role_default_acl = value.get("role_default_acl")
    event_triggers = value.get("event_triggers")
    effective_write = value.get("role_effective_persistent_write")
    canonical_string_lists = (
        role_memberships,
        incoming_memberships,
        role_settings,
        role_owned_objects,
        effective_write,
    )
    if (
        value.get("current_user") != authority["audit_role"]
        or value.get("transaction_read_only") is not True
        or value.get("event_triggers_disabled")
        is not (
            True
            if identity["server_version_num"] // 10000 >= 17
            else None
        )
        or value.get("role_superuser")
        is not (isolated or adjacent_admin)
        or not isinstance(value.get("role_create_db"), bool)
        or not isinstance(value.get("role_create_role"), bool)
        or not isinstance(value.get("role_replication"), bool)
        or not isinstance(value.get("role_bypass_rls"), bool)
        or not isinstance(value.get("role_inherit"), bool)
        or not isinstance(value.get("role_can_login"), bool)
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
        or (
            not isolated
            and not adjacent_admin
            and (
                value["role_create_db"]
                or value["role_create_role"]
                or value["role_replication"]
                or value["role_bypass_rls"]
                or value["role_inherit"]
                or value["role_can_login"]
                or role_memberships
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
            )
        )
    ):
        raise SiteHelperContractError(
            "external per-database audit role is unsafe"
        )
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
            raise SiteHelperContractError(
                "external per-database direct ACL is invalid"
            )
        normalized_direct_acl.append(dict(acl))
    if normalized_direct_acl != sorted(
        normalized_direct_acl,
        key=canonical_json_bytes,
    ):
        raise SiteHelperContractError(
            "external per-database direct ACL is not canonical"
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
            or (
                acl.get("namespace") is not None
                and not _valid_pg_identifier(acl.get("namespace"))
            )
            or not isinstance(acl.get("object_type"), str)
            or not acl["object_type"]
            or not isinstance(acl.get("privilege"), str)
            or not acl["privilege"]
            or not isinstance(acl.get("grantable"), bool)
        ):
            raise SiteHelperContractError(
                "external per-database default ACL is invalid"
            )
        normalized_default_acl.append(dict(acl))
    if normalized_default_acl != sorted(
        normalized_default_acl,
        key=canonical_json_bytes,
    ):
        raise SiteHelperContractError(
            "external per-database default ACL is not canonical"
        )
    ledger = value.get("ledger")
    if value.get("ledger_sha256") != sha256_bytes(
        canonical_json_bytes(ledger)
    ):
        raise SiteHelperContractError(
            "external per-database ledger digest differs"
        )
    legacy_present = value.get("legacy_relation_present")
    if not isinstance(legacy_present, bool):
        raise SiteHelperContractError(
            "external per-database legacy state is invalid"
        )
    migration_scope = authority["migration_scope"]
    if migration_scope == "auto-detect-adjacent":
        migration_scope = (
            "nexpoly-ledger"
            if (
                isinstance(value.get("ledger_relation"), dict)
                and value["ledger_relation"].get("state") == "present"
            )
            or ledger != []
            or legacy_present
            else "adjacent-record-only"
        )
    if migration_scope == "nexpoly-ledger":
        expected_analysis, expected_0013, requires_0014 = (
            _external_media_ledger_v2(
                ledger,
                legacy_relation_present=legacy_present,
                isolated=isolated,
            )
        )
    elif migration_scope == "adjacent-record-only":
        if ledger != [] or legacy_present:
            raise SiteHelperContractError(
                "adjacent database contains Nexpoly migration relations"
            )
        expected_analysis = {
            "status": "adjacent-no-nexpoly-relations",
            "canonical_prefix_length": 0,
            "historical_0005_alias_present": False,
            "checksum_mismatches": [],
        }
        expected_0013 = {"state": "absent", "checksum": None}
        requires_0014 = False
    else:
        raise SiteHelperContractError(
            "external database migration scope is invalid"
        )
    if (
        value.get("ledger_analysis") != expected_analysis
        or value.get("migration_0013") != expected_0013
        or value.get("requires_0014") is not requires_0014
    ):
        raise SiteHelperContractError(
            "external per-database migration analysis differs"
        )
    ledger_present = (
        isinstance(value.get("ledger_relation"), dict)
        and value["ledger_relation"].get("state") == "present"
    )
    if migration_scope == "adjacent-record-only" and ledger_present:
        raise SiteHelperContractError(
            "adjacent database fabricated a migration ledger"
        )
    if not isolated and not adjacent_admin:
        allowed_acl = [
            {
                "object_kind": "database",
                "object_name": authority["name"],
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
        if ledger_present:
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
        if legacy_present:
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
            raise SiteHelperContractError(
                "external per-database direct ACL differs"
            )
    _external_relation_v3(
        value.get("ledger_relation"),
        relation="ledger",
        owner=authority["owner"],
        rows=ledger,
        present=ledger_present,
    )
    generation_schema = value.get("generation_schema")
    expected_schema_acl = (
        []
        if (
            isolated
            or adjacent_admin
            or authority["audit_role"] == authority["owner"]
        )
        else [
            {
                "grantee": authority["audit_role"],
                "privilege": "USAGE",
                "grantable": False,
            }
        ]
    )
    expected_schema_authority = {
        "owner": authority["owner"],
        "acl": expected_schema_acl,
        "comments": [],
        "security_labels": [],
        "default_acl": [],
        "initial_privileges": [],
        "publications": [],
        "unapproved_dependents": [],
    }
    if (
        not isinstance(generation_schema, dict)
        or set(generation_schema)
        != {"state", "schema_sha256", "schema_authority"}
        or generation_schema.get("state")
        != ("present" if legacy_present else "absent")
        or (
            legacy_present
            and (
                generation_schema.get("schema_authority")
                != expected_schema_authority
                or generation_schema.get("schema_sha256")
                != sha256_bytes(
                    canonical_json_bytes(expected_schema_authority)
                )
            )
        )
        or (
            not legacy_present
            and (
                generation_schema.get("schema_authority") is not None
                or generation_schema.get("schema_sha256") is not None
            )
        )
    ):
        raise SiteHelperContractError(
            "external generation schema authority differs"
        )
    _external_relation_v3(
        value.get("legacy_relation"),
        relation="legacy",
        owner=authority["owner"],
        rows=None,
        present=legacy_present,
    )
    return dict(value), requires_0014


def _external_audit_runtime_v3(
    audit: object,
    *,
    method: str,
    isolation: dict[str, object],
) -> tuple[
    dict[str, Any],
    tuple[str, int, int, int, str, str],
]:
    fields = {
        "method",
        "complete",
        "auditor_sha256",
        "postgres_major",
        "postgres_uid",
        "postgres_gid",
        "postgres_image",
        "postgres_image_id",
        "audited_at",
        "isolation",
        "evidence_sha256",
    }
    if (
        not isinstance(audit, dict)
        or set(audit) != fields
        or audit.get("method") != method
        or audit.get("complete") is not True
        or audit.get("postgres_major") not in POSTGRES_AUDIT_IMAGES
        or isinstance(audit.get("postgres_uid"), bool)
        or not isinstance(audit.get("postgres_uid"), int)
        or audit.get("postgres_uid") != 70
        or isinstance(audit.get("postgres_gid"), bool)
        or not isinstance(audit.get("postgres_gid"), int)
        or audit.get("postgres_gid") != 70
        or not isinstance(audit.get("postgres_image"), str)
        or audit.get("postgres_image")
        != POSTGRES_AUDIT_IMAGES.get(audit.get("postgres_major"))
        or not isinstance(audit.get("audited_at"), str)
        or RFC3339_UTC_RE.fullmatch(audit["audited_at"]) is None
        or audit.get("isolation") != isolation
    ):
        raise SiteHelperContractError(
            "external media-v3 audit runtime or isolation differs"
        )
    auditor = _require_digest(
        audit.get("auditor_sha256"),
        "media-v3 auditor digest",
    )
    image_id = _require_digest(
        audit.get("postgres_image_id"),
        "media-v3 PostgreSQL image ID",
    )
    _require_digest(
        audit.get("evidence_sha256"),
        "media-v3 evidence digest",
    )
    return dict(audit), (
        auditor,
        audit["postgres_major"],
        audit["postgres_uid"],
        audit["postgres_gid"],
        audit["postgres_image"],
        image_id,
    )


def _external_record_only_medium_v3(
    record: object,
    *,
    volume_names: list[str],
    container_ids: list[str],
) -> tuple[dict[str, Any], tuple[str, int, int, int, str, str]]:
    fields = {
        "record_type",
        "media_id",
        "kind",
        "classification",
        "disposition",
        "source_identity_before",
        "source_identity_after",
        "source_content_sha256",
        "content_identity_algorithm",
        "postgres_signature",
        "readers",
        "excluded_from_nexpoly_migration",
        "audit",
    }
    if not isinstance(record, dict) or set(record) != fields:
        raise SiteHelperContractError(
            "record-only external medium-v3 shape is invalid"
        )
    media_id = record.get("media_id")
    kind = record.get("kind")
    classification = record.get("classification")
    if (
        not isinstance(media_id, str)
        or MEDIA_ID_RE.fullmatch(media_id) is None
        or kind not in {
            "docker_volume",
            "container_bind",
            "postgres_backup",
            "reviewed_file",
        }
        or classification
        not in {"adjacent-record-only", "reviewed-non-pg"}
        or record.get("record_type") != classification
        or record.get("disposition") != "excluded-from-nexpoly-migration"
        or record.get("excluded_from_nexpoly_migration") is not True
    ):
        raise SiteHelperContractError(
            "record-only external medium-v3 identity differs"
        )
    before = _external_source_identity_v3(
        record.get("source_identity_before"),
        kind=kind,
        media_id=media_id,
        allow_readonly_bind=(
            kind == "container_bind"
            and classification == "reviewed-non-pg"
        ),
    )
    after = _external_source_identity_v3(
        record.get("source_identity_after"),
        kind=kind,
        media_id=media_id,
        allow_readonly_bind=(
            kind == "container_bind"
            and classification == "reviewed-non-pg"
        ),
    )
    readers = record.get("readers")
    expected_readers = (
        before.get("attached")
        if kind in {"docker_volume", "container_bind"}
        else []
    )
    if before != after or readers != expected_readers:
        raise SiteHelperContractError(
            "record-only external medium changed or reader set differs"
        )
    if any(
        value["container_id"] not in container_ids
        for value in readers
    ):
        raise SiteHelperContractError(
            "record-only reader is outside Docker inventory"
        )
    if kind == "docker_volume" and before["name"] not in volume_names:
        raise SiteHelperContractError(
            "record-only volume is outside Docker inventory"
        )
    signature = record.get("postgres_signature")
    if (
        not isinstance(signature, dict)
        or set(signature) != {"state", "major", "data_subpath"}
        or not isinstance(signature.get("data_subpath"), str)
    ):
        raise SiteHelperContractError(
            "record-only PostgreSQL signature is invalid"
        )
    if classification == "adjacent-record-only":
        method = "adjacent-record-only"
        algorithm = (
            "docker-volume-tree-sha256-v1"
            if kind == "docker_volume"
            else "sha256-file-v1"
            if kind == "postgres_backup"
            else "private-bind-tree-sha256-v1"
        )
        if (
            kind
            not in {
                "docker_volume",
                "container_bind",
                "postgres_backup",
            }
            or kind == "postgres_backup"
            and (
                signature.get("state") != "postgres-backup"
                or signature.get("major") is not None
            )
            or kind != "postgres_backup"
            and (
                signature.get("state") != "postgres"
                or isinstance(signature.get("major"), bool)
                or not isinstance(signature.get("major"), int)
                or not ADJACENT_POSTGRES_MAJOR_MIN
                <= signature["major"]
                <= ADJACENT_POSTGRES_MAJOR_MAX
            )
        ):
            raise SiteHelperContractError(
                "adjacent PostgreSQL record-only signature differs"
            )
    else:
        method = "reviewed-content-only"
        algorithm = (
            "sha256-file-v1"
            if kind == "reviewed_file"
            else "docker-volume-tree-sha256-v1"
            if kind == "docker_volume"
            else "private-bind-tree-sha256-v1"
        )
        if (
            signature.get("state") != "non-postgres"
            or signature.get("major") is not None
        ):
            raise SiteHelperContractError(
                "reviewed non-PG medium contains a PostgreSQL signature"
            )
    if record.get("content_identity_algorithm") != algorithm:
        raise SiteHelperContractError(
            "record-only content identity algorithm differs"
        )
    source_digest = _require_digest(
        record.get("source_content_sha256"),
        "record-only source content digest",
    )
    if (
        kind == "postgres_backup"
        and before.get("sha256") != source_digest
    ):
        raise SiteHelperContractError(
            "record-only PostgreSQL backup content digest differs"
        )
    isolation = (
        {
            "source_mounted_read_only": True,
            "source_started_as_postgres": False,
            "content_cas_verified": True,
        }
        if kind == "docker_volume"
        else {
            "source_opened_with_openat_no_follow": True,
            "source_passed_to_docker": False,
            "source_started_as_postgres": False,
            "content_cas_verified": True,
        }
        if kind == "container_bind"
        else {
            "source_opened_with_openat_no_follow": True,
            "source_passed_to_docker": False,
            "source_started_as_postgres": False,
            "content_cas_verified": True,
        }
        if kind == "postgres_backup"
        else {
            "source_opened_with_openat_no_follow": True,
            "source_passed_to_docker": False,
            "content_cas_verified": True,
        }
    )
    audit, runtime = _external_audit_runtime_v3(
        record.get("audit"),
        method=method,
        isolation=isolation,
    )
    unsealed = {
        **record,
        "audit": {
            key: value
            for key, value in audit.items()
            if key != "evidence_sha256"
        },
    }
    if audit["evidence_sha256"] != sha256_bytes(
        canonical_json_bytes(unsealed)
    ):
        raise SiteHelperContractError(
            "record-only medium self-seal differs"
        )
    return dict(record), runtime


def _external_nexpoly_medium_v3(
    record: object,
    *,
    volume_names: list[str],
    container_ids: list[str],
) -> tuple[
    dict[str, Any],
    tuple[str, int, int, int, str, str],
    bool,
]:
    primary_fields = {
        "database_identity",
        "database_identity_sha256",
        "current_user",
        "transaction_read_only",
        "server_startup",
        "event_triggers_disabled",
        "role_superuser",
        "role_create_db",
        "role_create_role",
        "role_replication",
        "role_bypass_rls",
        "role_inherit",
        "role_can_login",
        "role_memberships",
        "role_incoming_memberships",
        "role_settings",
        "role_owned_objects",
        "role_direct_acl",
        "role_default_acl",
        "event_triggers",
        "role_effective_persistent_write",
        "ledger",
        "ledger_sha256",
        "ledger_relation",
        "ledger_analysis",
        "legacy_relation_present",
        "generation_schema",
        "legacy_relation",
        "migration_0013",
    }
    fields = {
        "record_type",
        "media_id",
        "kind",
        "classification",
        "database",
        "disposition",
        "online_admin_role",
        "source_identity_before",
        "source_identity_after",
        "source_system_identifier",
        "source_content_sha256",
        "content_identity_algorithm",
        "database_inventory",
        "database_inventory_sha256",
        "databases",
        "audit",
        *primary_fields,
    }
    if not isinstance(record, dict) or set(record) != fields:
        raise SiteHelperContractError(
            "Nexpoly external medium-v3 shape is invalid"
        )
    media_id = record.get("media_id")
    kind = record.get("kind")
    database_name = record.get("database")
    disposition = record.get("disposition")
    online_admin_role = record.get("online_admin_role")
    adjacent = record.get("record_type") == "adjacent-postgres"
    if (
        record.get("record_type")
        not in {"nexpoly-db", "adjacent-postgres"}
        or record.get("classification") != record.get("record_type")
        or not isinstance(media_id, str)
        or MEDIA_ID_RE.fullmatch(media_id) is None
        or kind
        not in {"docker_volume", "container_bind", "postgres_backup"}
        or adjacent
        and kind == "postgres_backup"
        or not _valid_pg_identifier(database_name)
        or disposition
        not in {
            "writable-target",
            "read-only-online",
            "retained-private-isolated",
            "excluded-from-nexpoly-migration",
        }
        or adjacent
        and disposition != "excluded-from-nexpoly-migration"
        or not adjacent
        and disposition == "excluded-from-nexpoly-migration"
        or disposition != "retained-private-isolated"
        and (
            not _valid_pg_identifier(online_admin_role)
        )
        or disposition == "retained-private-isolated"
        and online_admin_role is not None
    ):
        raise SiteHelperContractError(
            "Nexpoly external medium-v3 identity differs"
        )
    before = _external_source_identity_v3(
        record.get("source_identity_before"),
        kind=kind,
        media_id=media_id,
    )
    after = _external_source_identity_v3(
        record.get("source_identity_after"),
        kind=kind,
        media_id=media_id,
    )
    if before != after:
        raise SiteHelperContractError(
            "Nexpoly external medium changed during audit"
        )
    attachments = (
        before.get("attached")
        if kind in {"docker_volume", "container_bind"}
        else []
    )
    if (
        kind == "docker_volume"
        and before["name"] not in volume_names
        or any(
            value["container_id"] not in container_ids
            for value in attachments
        )
    ):
        raise SiteHelperContractError(
            "Nexpoly external medium is outside Docker inventory"
        )
    method = (
        "live-read-only-adjacent"
        if adjacent
        else "live-read-only"
        if disposition != "retained-private-isolated"
        else "isolated-volume-copy-read-only"
        if kind == "docker_volume"
        else "isolated-bind-copy-read-only"
        if kind == "container_bind"
        else "isolated-backup-restore-read-only"
    )
    active = [
        value
        for value in attachments
        if value["state"]
        in {"created", "running", "paused", "restarting", "removing"}
    ]
    if (
        method in {"live-read-only", "live-read-only-adjacent"}
        and len(active) != 1
        or method
        not in {"live-read-only", "live-read-only-adjacent"}
        and active
    ):
        raise SiteHelperContractError(
            "Nexpoly external medium reader state differs"
        )
    expected_algorithm = {
        "live-read-only": "logical-cluster-inventory-v3",
        "live-read-only-adjacent": "logical-cluster-inventory-v3",
        "isolated-volume-copy-read-only": (
            "postgres-data-directory-tar-sha256-v1"
        ),
        "isolated-bind-copy-read-only": (
            "postgres-private-tree-sha256-v1"
        ),
        "isolated-backup-restore-read-only": "sha256-file-v1",
    }[method]
    if record.get("content_identity_algorithm") != expected_algorithm:
        raise SiteHelperContractError(
            "Nexpoly external content algorithm differs"
        )
    source_digest = _require_digest(
        record.get("source_content_sha256"),
        "Nexpoly external source content digest",
    )
    inventory = record.get("database_inventory")
    databases = record.get("databases")
    if (
        not isinstance(inventory, list)
        or not inventory
        or not isinstance(databases, list)
        or len(databases) != len(inventory)
        or record.get("database_inventory_sha256")
        != sha256_bytes(canonical_json_bytes(inventory))
    ):
        raise SiteHelperContractError(
            "Nexpoly complete database inventory is invalid"
        )
    base_fields = {
        "name",
        "oid",
        "owner",
        "allow_connections",
        "template",
    }
    authority_fields = {
        *base_fields,
        "audit_role",
        "migration_scope",
        "audit_state",
        "audit",
    }
    names: list[str] = []
    oids: set[str] = set()
    primary_audit: dict[str, Any] | None = None
    requires_0014 = False
    system_identifiers: set[str] = set()
    server_majors: set[int] = set()
    for observed, database_record in zip(
        inventory,
        databases,
        strict=True,
    ):
        if (
            not isinstance(observed, dict)
            or set(observed) != base_fields
            or not isinstance(database_record, dict)
            or set(database_record) != authority_fields
            or {
                key: database_record[key]
                for key in base_fields
            }
            != observed
            or not _valid_pg_identifier(observed.get("name"))
            or not isinstance(observed.get("oid"), str)
            or not observed["oid"].isdigit()
            or observed["oid"] in oids
            or not _valid_pg_identifier(observed.get("owner"))
            or not isinstance(observed.get("allow_connections"), bool)
            or observed.get("template") is not False
        ):
            raise SiteHelperContractError(
                "Nexpoly database inventory entry differs"
            )
        names.append(observed["name"])
        oids.add(observed["oid"])
        authority = {
            key: database_record[key]
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
        if (
            database_record.get("audit_state") != "complete"
            or observed["allow_connections"] is not True
            or (
                not _valid_pg_identifier(authority["audit_role"])
                if adjacent
                else (
                    not isinstance(authority["audit_role"], str)
                    or ROLE_RE.fullmatch(authority["audit_role"]) is None
                )
            )
        ):
            raise SiteHelperContractError(
                "every non-template database requires a complete audit"
            )
        database_audit, needs_0014 = _external_database_record_v3(
            database_record.get("audit"),
            authority=authority,
            method=method,
            disposition=disposition,
        )
        requires_0014 = requires_0014 or needs_0014
        system_identifiers.add(
            database_audit["database_identity"]["system_identifier"]
        )
        server_majors.add(
            database_audit["database_identity"][
                "server_version_num"
            ]
            // 10000
        )
        if observed["name"] == database_name:
            primary_audit = database_audit
    if names != sorted(set(names)) or primary_audit is None:
        raise SiteHelperContractError(
            "Nexpoly database inventory is not canonical or lacks primary"
        )
    if any(
        record.get(field) != primary_audit[field]
        for field in primary_fields
    ):
        raise SiteHelperContractError(
            "Nexpoly primary database projection was spliced"
        )
    source_system_identifier = record.get("source_system_identifier")
    if kind == "postgres_backup":
        if source_system_identifier is not None:
            raise SiteHelperContractError(
                "logical backup fabricated a source system identifier"
            )
    elif (
        len(system_identifiers) != 1
        or source_system_identifier not in system_identifiers
        or not isinstance(source_system_identifier, str)
    ):
        raise SiteHelperContractError(
            "physical medium database cluster identities differ"
        )
    if kind == "postgres_backup" and source_digest != before["sha256"]:
        raise SiteHelperContractError(
            "logical backup content digest differs"
        )
    if method in {"live-read-only", "live-read-only-adjacent"}:
        logical_digest = sha256_bytes(
            canonical_json_bytes(
                {
                    "database_inventory": inventory,
                    "databases": databases,
                }
            )
        )
        if source_digest != logical_digest:
            raise SiteHelperContractError(
                "online complete-cluster logical digest differs"
            )
    isolation = {
        "live-read-only": {
            "source_mounted_by_auditor": False,
            "source_started_by_auditor": False,
            "transaction_read_only": True,
        },
        "live-read-only-adjacent": {
            "source_mounted_by_auditor": False,
            "source_started_by_auditor": False,
            "transaction_read_only": True,
        },
        "isolated-volume-copy-read-only": {
            "source_mounted_read_only": True,
            "source_started_as_postgres": False,
            "scratch_network": "none",
            "scratch_destroyed": True,
            "copy_method": "readonly-tar-copy-to-disposable-volume-v1",
        },
        "isolated-bind-copy-read-only": {
            "source_mounted_read_only": False,
            "source_opened_with_openat_no_follow": True,
            "source_started_as_postgres": False,
            "scratch_network": "none",
            "scratch_destroyed": True,
            "copy_method": "private-openat-copy-to-disposable-volume-v1",
        },
        "isolated-backup-restore-read-only": {
            "source_opened_with_openat_no_follow": True,
            "source_passed_to_docker": False,
            "staged_snapshot_mounted_read_only": True,
            "source_started_as_postgres": False,
            "scratch_network": "none",
            "scratch_destroyed": True,
            "restore_method": "pg_restore-no-owner-no-privileges-v1",
        },
    }[method]
    audit, runtime = _external_audit_runtime_v3(
        record.get("audit"),
        method=method,
        isolation=isolation,
    )
    if server_majors != {audit["postgres_major"]}:
        raise SiteHelperContractError(
            "external database major differs from its pinned audit image"
        )
    unsealed = {
        **record,
        "audit": {
            key: value
            for key, value in audit.items()
            if key != "evidence_sha256"
        },
    }
    if audit["evidence_sha256"] != sha256_bytes(
        canonical_json_bytes(unsealed)
    ):
        raise SiteHelperContractError(
            "Nexpoly external medium self-seal differs"
        )
    return dict(record), runtime, requires_0014


def _external_excluded_non_pg_medium_v4(
    record: object,
    *,
    volume_names: list[str],
    bind_sources: list[str],
    container_ids: list[str],
) -> tuple[dict[str, Any], str]:
    fields = {
        "record_type",
        "media_id",
        "kind",
        "classification",
        "disposition",
        "source_identity_before",
        "source_identity_after",
        "postgres_signature",
        "readers",
        "excluded_from_nexpoly_migration",
        "audit",
    }
    if not isinstance(record, dict) or set(record) != fields:
        raise SiteHelperContractError(
            "excluded non-PG medium contains content-derived fields"
        )
    media_id = record.get("media_id")
    kind = record.get("kind")
    before = record.get("source_identity_before")
    after = record.get("source_identity_after")
    if (
        record.get("record_type") != "excluded-non-pg-media"
        or record.get("classification") != "excluded-non-pg"
        or record.get("disposition") != "excluded-from-nexpoly-migration"
        or kind not in {"docker_volume", "container_bind"}
        or not isinstance(media_id, str)
        or MEDIA_ID_RE.fullmatch(media_id) is None
        or not isinstance(before, dict)
        or before != after
        or record.get("excluded_from_nexpoly_migration") is not True
        or record.get("postgres_signature")
        != {"state": "non-postgres", "major": None, "data_subpath": "."}
    ):
        raise SiteHelperContractError(
            "excluded non-PG medium identity is invalid"
        )
    identity_fields = {
        "locator",
        "data_subpath",
        "attached",
        "classification_probe",
    }
    if kind == "docker_volume":
        identity_fields.add("docker_inspect_sha256")
    else:
        if before.get("root_state") == "absent":
            identity_fields.update(
                {
                    "audit_locator",
                    "resolved_parent",
                    "root_state",
                    "transient_runtime",
                }
            )
        else:
            identity_fields.update(
                {
                    "audit_locator",
                    "resolved_parent",
                    "parent_device",
                    "parent_inode",
                    "entry_type",
                    "transient_runtime",
                    "device",
                    "inode",
                    "size",
                    "mtime_ns",
                    "ctime_ns",
                    "mode",
                    "uid",
                    "gid",
                }
            )
            if before.get("entry_type") == "symlink":
                identity_fields.add("symlink_target")
            if "takeover_redirect" in before:
                identity_fields.update(
                    {
                        "takeover_redirect",
                        "takeover_move_seal_sha256",
                    }
                )
    if set(before) != identity_fields:
        raise SiteHelperContractError(
            "excluded non-PG metadata identity shape is invalid"
        )
    locator = before.get("locator")
    attachments = before.get("attached")
    probe = before.get("classification_probe")
    expected_probe_fields: set[str] | None = None
    if isinstance(probe, dict):
        method = probe.get("method")
        if method == "container-metadata-exclusion-v1":
            expected_probe_fields = {
                "method",
                "result",
                "content_read_scope",
            }
            valid_result = probe.get("result") in {
                "no-container-pgdata-mapping",
                "trusted-non-postgres-active-reader",
            }
        elif method in {
            "bounded-postgres-marker-probe-v1",
            "bounded-openat-postgres-marker-probe-v1",
            "exact-openat-pgdata-marker-probe-v1",
            "takeover-move-seal-marker-proof-v1",
        }:
            expected_probe_fields = {
                "method",
                "result",
                "maximum_entries",
                "content_read_scope",
            }
            if method == "bounded-postgres-marker-probe-v1":
                expected_probe_fields.update(
                    {
                        "maximum_marker_results",
                        "timeout_seconds",
                    }
                )
            valid_result = probe.get("result") == "no-postgres-markers"
        elif method == "existing-reader-bounded-marker-probe-v1":
            expected_probe_fields = {
                "method",
                "result",
                "maximum_entries",
                "maximum_marker_results",
                "timeout_seconds",
                "content_read_scope",
                "container_id",
                "mount_destination",
            }
            valid_result = (
                probe.get("result") == "no-postgres-markers"
                and isinstance(probe.get("container_id"), str)
                and probe["container_id"] in container_ids
                and isinstance(probe.get("mount_destination"), str)
                and Path(probe["mount_destination"]).is_absolute()
            )
        else:
            valid_result = False
    else:
        valid_result = False
    if (
        not isinstance(locator, str)
        or before.get("data_subpath") != "."
        or media_id != media_id_for_locator(kind, locator)
        or kind == "docker_volume"
        and locator not in volume_names
        or kind == "container_bind"
        and locator not in bind_sources
        or not isinstance(attachments, list)
        or attachments != record.get("readers")
        or any(
            not isinstance(value, dict)
            or value.get("container_id") not in container_ids
            for value in attachments
        )
        or expected_probe_fields is None
        or set(probe) != expected_probe_fields
        or not valid_result
        or probe.get("content_read_scope")
        not in {"none", "PG_VERSION-up-to-32-bytes"}
    ):
        raise SiteHelperContractError(
            "excluded non-PG probe or Docker binding is invalid"
        )
    if kind == "docker_volume":
        _require_digest(
            before.get("docker_inspect_sha256"),
            "excluded Docker-volume inspect digest",
        )
    else:
        if before.get("root_state") == "absent":
            if (
                before.get("transient_runtime") is not True
                or not isinstance(before.get("audit_locator"), str)
                or not Path(before["audit_locator"]).is_absolute()
                or not isinstance(before.get("resolved_parent"), str)
                or not Path(before["resolved_parent"]).is_absolute()
            ):
                raise SiteHelperContractError(
                    "absent transient bind identity is invalid"
                )
            audit = record.get("audit")
        else:
            audit = None
        if (
            before.get("root_state") != "absent"
            and (
            not isinstance(before.get("audit_locator"), str)
            or not Path(before["audit_locator"]).is_absolute()
            or before.get("entry_type")
            not in {
                "directory",
                "file",
                "unix-socket",
                "fifo",
                "block-device",
                "character-device",
                "unknown-special",
                "symlink",
            }
            or before.get("transient_runtime")
            is not (
                before.get("entry_type") not in {"directory", "file"}
            )
            or any(
                isinstance(before.get(name), bool)
                or not isinstance(before.get(name), int)
                or before[name] < 0
                for name in (
                    "device",
                    "inode",
                    "size",
                    "mtime_ns",
                    "ctime_ns",
                    "mode",
                    "uid",
                    "gid",
                    "parent_device",
                    "parent_inode",
                )
            )
            )
        ):
            raise SiteHelperContractError(
                "excluded container-bind root metadata is invalid"
            )
        if (
            before.get("root_state") != "absent"
            and "takeover_redirect" in before
        ):
            _require_digest(
                before.get("takeover_move_seal_sha256"),
                "excluded takeover move-seal digest",
            )
            if not isinstance(before.get("takeover_redirect"), dict):
                raise SiteHelperContractError(
                    "excluded takeover redirect binding is invalid"
                )
    audit = record.get("audit")
    if (
        not isinstance(audit, dict)
        or set(audit)
        != {
            "method",
            "complete",
            "auditor_sha256",
            "audited_at",
            "classification_content_read_scope",
            "content_digest_computed",
            "evidence_sha256",
        }
        or audit.get("method") != "metadata-only-exclusion"
        or audit.get("complete") is not True
        or audit.get("classification_content_read_scope")
        != probe.get("content_read_scope")
        or audit.get("content_digest_computed") is not False
        or not isinstance(audit.get("audited_at"), str)
        or RFC3339_UTC_RE.fullmatch(audit["audited_at"]) is None
    ):
        raise SiteHelperContractError(
            "excluded non-PG audit authority is invalid"
        )
    auditor = _require_digest(
        audit.get("auditor_sha256"),
        "excluded non-PG auditor digest",
    )
    evidence_digest = _require_digest(
        audit.get("evidence_sha256"),
        "excluded non-PG evidence digest",
    )
    unsealed = {
        **record,
        "audit": {
            key: value
            for key, value in audit.items()
            if key != "evidence_sha256"
        },
    }
    if evidence_digest != sha256_bytes(canonical_json_bytes(unsealed)):
        raise SiteHelperContractError(
            "excluded non-PG evidence self-seal differs"
        )
    return dict(record), auditor


def validate_external_database_audit(
    document: object,
    *,
    expected_users: dict[str, str] | None,
    expected_media_authority_rules_digest: str | None = None,
    expected_runtime_registry_digest: str | None = None,
    expected_media_registry_digest: str | None = None,
) -> dict[str, Any]:
    if expected_media_registry_digest is not None:
        if (
            expected_runtime_registry_digest is not None
            and expected_runtime_registry_digest
            != expected_media_registry_digest
        ):
            raise SiteHelperContractError(
                "conflicting expected runtime media-registry digests"
            )
        expected_runtime_registry_digest = (
            expected_media_registry_digest
        )
    fields = {
        "schema_version",
        "inventory_complete",
        "writable_target",
        "media_registry",
        "databases",
        "media",
        "requires_0014",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 5
        or document.get("inventory_complete") is not True
        or document.get("writable_target")
        != {"stack": "production", "database": "nexpoly"}
    ):
        raise SiteHelperContractError(
            "external-database evidence is not explicit schema v5"
        )
    registry = document.get("media_registry")
    registry_fields = {
        "schema_version",
        "media_authority_rules_sha256",
        "runtime_registry_sha256",
        "reviewed_content_inventory_sha256",
        "audit_images",
        "discovery_boundary_sha256",
        "discovery_state_sha256_before",
        "discovery_state_sha256_after",
        "captured_at",
        "expected_media_ids",
        "discovered_media_ids",
        "docker_inventory_sha256",
        "backup_inventory_sha256",
        "scanned_volume_names",
        "scanned_bind_sources",
        "scanned_container_ids",
    }
    if (
        not isinstance(registry, dict)
        or set(registry) != registry_fields
        or registry.get("schema_version") != 5
        or not isinstance(registry.get("captured_at"), str)
        or RFC3339_UTC_RE.fullmatch(registry["captured_at"]) is None
    ):
        raise SiteHelperContractError(
            "external media registry-v5 binding is invalid"
        )
    authority_rules_digest = _require_digest(
        registry.get("media_authority_rules_sha256"),
        "external media authority-rules digest",
    )
    runtime_registry_digest = _require_digest(
        registry.get("runtime_registry_sha256"),
        "external runtime media-registry digest",
    )
    _require_digest(
        registry.get("reviewed_content_inventory_sha256"),
        "external reviewed-content inventory digest",
    )
    raw_audit_images = registry.get("audit_images")
    if (
        not isinstance(raw_audit_images, dict)
        or set(raw_audit_images)
        != {str(major) for major in POSTGRES_AUDIT_IMAGES}
    ):
        raise SiteHelperContractError(
            "external media-v5 audit-image authority is incomplete"
        )
    audit_images: dict[int, tuple[str, str]] = {}
    for major, digest_ref in POSTGRES_AUDIT_IMAGES.items():
        image = raw_audit_images.get(str(major))
        if (
            not isinstance(image, dict)
            or set(image) != {"digest_ref", "image_id"}
            or image.get("digest_ref") != digest_ref
        ):
            raise SiteHelperContractError(
                "external media-v5 audit-image reference differs"
            )
        image_id = _require_digest(
            image.get("image_id"),
            f"external PostgreSQL {major} local image ID",
        )
        audit_images[major] = (digest_ref, image_id)
    if (
        expected_media_authority_rules_digest is not None
        and authority_rules_digest
        != _require_digest(
            expected_media_authority_rules_digest,
            "expected external media authority-rules digest",
        )
    ):
        raise SiteHelperContractError(
            "external media authority-rules digest differs"
        )
    if (
        expected_runtime_registry_digest is not None
        and runtime_registry_digest
        != _require_digest(
            expected_runtime_registry_digest,
            "expected external runtime media-registry digest",
        )
    ):
        raise SiteHelperContractError(
            "external runtime media-registry digest differs"
        )
    for name in (
        "discovery_boundary_sha256",
        "discovery_state_sha256_before",
        "discovery_state_sha256_after",
        "docker_inventory_sha256",
        "backup_inventory_sha256",
    ):
        _require_digest(registry.get(name), name)
    if (
        registry["discovery_state_sha256_before"]
        != registry["discovery_state_sha256_after"]
    ):
        raise SiteHelperContractError(
            "external media discovery boundary changed during audit"
        )
    expected_ids = registry.get("expected_media_ids")
    discovered_ids = registry.get("discovered_media_ids")
    volume_names = registry.get("scanned_volume_names")
    bind_sources = registry.get("scanned_bind_sources")
    container_ids = registry.get("scanned_container_ids")
    if (
        not isinstance(expected_ids, list)
        or not expected_ids
        or expected_ids != sorted(set(expected_ids))
        or any(
            not isinstance(media_id, str)
            or MEDIA_ID_RE.fullmatch(media_id) is None
            for media_id in expected_ids
        )
        or discovered_ids != expected_ids
        or not isinstance(volume_names, list)
        or volume_names != sorted(set(volume_names))
        or any(
            not isinstance(name, str)
            or VOLUME_RE.fullmatch(name) is None
            for name in volume_names
        )
        or not isinstance(bind_sources, list)
        or bind_sources != sorted(set(bind_sources))
        or any(
            not isinstance(source, str)
            or not Path(source).is_absolute()
            or ".." in Path(source).parts
            for source in bind_sources
        )
        or not isinstance(container_ids, list)
        or container_ids != sorted(set(container_ids))
        or any(
            not isinstance(container_id, str)
            or CONTAINER_RE.fullmatch(container_id) is None
            for container_id in container_ids
        )
    ):
        raise SiteHelperContractError(
            "external media registry-v3 complete inventory differs"
        )
    expected_volume_ids = {
        media_id_for_locator("docker_volume", name)
        for name in volume_names
    }
    if {
        media_id
        for media_id in expected_ids
        if media_id.startswith(
            ("docker-volume:", "docker-volume-sha256:")
        )
    } != expected_volume_ids:
        raise SiteHelperContractError(
            "external media-v3 did not classify every local volume"
        )
    expected_bind_ids = {
        media_id_for_locator("container_bind", source)
        for source in bind_sources
    }
    if {
        media_id
        for media_id in expected_ids
        if media_id.startswith(
            ("container-bind:", "container-bind-sha256:")
        )
    } != expected_bind_ids:
        raise SiteHelperContractError(
            "external media-v3 did not classify every scanned bind"
        )
    raw_media = document.get("media")
    if not isinstance(raw_media, list):
        raise SiteHelperContractError(
            "external media-v3 record list is invalid"
        )
    normalized: dict[str, dict[str, Any]] = {}
    runtimes_by_major: dict[
        int,
        tuple[str, int, int, int, str, str],
    ] = {}
    common_auditor: str | None = None
    requires_0014 = False
    for raw in raw_media:
        if not isinstance(raw, dict):
            raise SiteHelperContractError(
                "external media-v3 record is invalid"
            )
        if raw.get("record_type") in {
            "nexpoly-db",
            "adjacent-postgres",
        }:
            record, runtime, needs_0014 = _external_nexpoly_medium_v3(
                raw,
                volume_names=volume_names,
                container_ids=container_ids,
            )
            requires_0014 = requires_0014 or needs_0014
            auditor = runtime[0]
        elif raw.get("record_type") == "excluded-non-pg-media":
            record, auditor = _external_excluded_non_pg_medium_v4(
                raw,
                volume_names=volume_names,
                bind_sources=bind_sources,
                container_ids=container_ids,
            )
            runtime = None
        else:
            if raw.get("record_type") != "reviewed-non-pg":
                raise SiteHelperContractError(
                    "schema-v5 PostgreSQL media requires a full logical audit"
                )
            record, runtime = _external_record_only_medium_v3(
                raw,
                volume_names=volume_names,
                container_ids=container_ids,
            )
            auditor = runtime[0]
        media_id = record["media_id"]
        if media_id in normalized:
            raise SiteHelperContractError(
                "external media-v3 contains duplicate identities"
            )
        if common_auditor is None:
            common_auditor = auditor
        elif common_auditor != auditor:
            raise SiteHelperContractError(
                "external media-v3 mixes auditor implementations"
            )
        if runtime is not None:
            major = runtime[1]
            if (
                audit_images.get(major)
                != (runtime[4], runtime[5])
            ):
                raise SiteHelperContractError(
                    "external media-v5 runtime differs from registry image"
                )
            previous_runtime = runtimes_by_major.get(major)
            if (
                previous_runtime is not None
                and previous_runtime != runtime
            ):
                raise SiteHelperContractError(
                    "external media-v5 mixes one-major audit runtimes"
                )
            runtimes_by_major[major] = runtime
        normalized[media_id] = record
    if sorted(normalized) != expected_ids:
        raise SiteHelperContractError(
            "external media-v3 records differ from complete registry"
        )
    writable = [
        record
        for record in normalized.values()
        if record.get("disposition") == "writable-target"
    ]
    if (
        len(writable) != 1
        or writable[0].get("record_type") != "nexpoly-db"
        or writable[0].get("database") != "nexpoly"
        or writable[0].get("kind")
        not in {"docker_volume", "container_bind"}
    ):
        raise SiteHelperContractError(
            "external media-v3 lacks one production writable target"
        )
    databases = document.get("databases")
    database_fields = {
        "stack",
        "media_id",
        "database",
        "current_user",
        "transaction_read_only",
        "role_superuser",
        "role_create_db",
        "role_create_role",
        "role_replication",
        "role_bypass_rls",
        "role_inherit",
        "role_can_login",
        "role_memberships",
        "system_identifier",
        "database_identity_sha256",
        "ledger",
        "ledger_sha256",
        "legacy_relation_present",
    }
    if not isinstance(databases, list) or len(databases) > 2:
        raise SiteHelperContractError(
            "external online database-v3 evidence is invalid"
        )
    observed_stacks = [
        database.get("stack")
        if isinstance(database, dict)
        else None
        for database in databases
    ]
    allowed_stacks = ["nexpoly_dev", "nexpoly_md_health_opt"]
    expected_stacks = [
        stack for stack in allowed_stacks if stack in observed_stacks
    ]
    if observed_stacks != expected_stacks:
        raise SiteHelperContractError(
            "external online database-v3 stack order differs"
        )
    normalized_databases: list[dict[str, Any]] = []
    for database, stack in zip(
        databases,
        expected_stacks,
        strict=True,
    ):
        if (
            not isinstance(database, dict)
            or set(database) != database_fields
            or database.get("stack") != stack
            or database.get("database") != stack
            or database.get("media_id") not in normalized
        ):
            raise SiteHelperContractError(
                "external online database-v3 identity differs"
            )
        source = normalized[database["media_id"]]
        if (
            source.get("record_type") != "nexpoly-db"
            or source.get("database") != stack
        ):
            raise SiteHelperContractError(
                "external online database-v3 maps to another medium"
            )
        expected_projection = {
            "stack": stack,
            "media_id": database["media_id"],
            "database": source["database"],
            "current_user": source["current_user"],
            "transaction_read_only": source["transaction_read_only"],
            "role_superuser": source["role_superuser"],
            "role_create_db": source["role_create_db"],
            "role_create_role": source["role_create_role"],
            "role_replication": source["role_replication"],
            "role_bypass_rls": source["role_bypass_rls"],
            "role_inherit": source["role_inherit"],
            "role_can_login": source["role_can_login"],
            "role_memberships": source["role_memberships"],
            "system_identifier": source["database_identity"][
                "system_identifier"
            ],
            "database_identity_sha256": source[
                "database_identity_sha256"
            ],
            "ledger": source["ledger"],
            "ledger_sha256": source["ledger_sha256"],
            "legacy_relation_present": source[
                "legacy_relation_present"
            ],
        }
        if database != expected_projection:
            raise SiteHelperContractError(
                "external online database-v3 projection was spliced"
            )
        if (
            expected_users is not None
            and database["current_user"] != expected_users.get(stack)
        ):
            raise SiteHelperContractError(
                "external online database-v3 audit user differs"
            )
        normalized_databases.append(dict(database))
    projected_by_stack = {
        database["stack"]: database["media_id"]
        for database in normalized_databases
    }
    for stack in allowed_stacks:
        stack_records = [
            record
            for record in normalized.values()
            if record.get("record_type") == "nexpoly-db"
            and record.get("database") == stack
        ]
        if not stack_records:
            raise SiteHelperContractError(
                f"external media-v3 omits retained {stack} media"
            )
        projected_media_id = projected_by_stack.get(stack)
        for record in stack_records:
            if record["media_id"] == projected_media_id:
                if (
                    record.get("disposition") != "read-only-online"
                    or record["audit"].get("method") != "live-read-only"
                ):
                    raise SiteHelperContractError(
                        f"external media-v3 online {stack} projection is not live"
                    )
            elif (
                record.get("disposition") != "retained-private-isolated"
                or record["audit"].get("method") == "live-read-only"
            ):
                raise SiteHelperContractError(
                    f"external media-v3 offline {stack} medium is not retained-isolated"
                )
    if document.get("requires_0014") is not requires_0014:
        raise SiteHelperContractError(
            "external media-v3 0014 requirement differs from every database"
        )
    return {
        "schema_version": 5,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": dict(registry),
        "databases": normalized_databases,
        "media": [normalized[name] for name in sorted(normalized)],
        "requires_0014": requires_0014,
    }


def _validate_table_inventory(
    records: object,
    expected_relations: tuple[tuple[str, str], ...],
    *,
    absent_relations: frozenset[tuple[str, str]] = frozenset(),
) -> list[dict[str, Any]]:
    if not isinstance(records, list) or len(records) != len(expected_relations):
        raise SiteHelperContractError("mutable-data audit table inventory differs")
    normalized: list[dict[str, Any]] = []
    fields = {
        "schema",
        "table",
        "state",
        "row_count",
        "schema_sha256",
        "content_sha256",
    }
    for record, expected in zip(records, expected_relations, strict=True):
        expected_state = "absent" if expected in absent_relations else "present"
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or (record.get("schema"), record.get("table")) != expected
            or record.get("state") != expected_state
        ):
            raise SiteHelperContractError(
                "mutable-data audit table record is invalid"
            )
        if expected_state == "absent":
            if any(
                record.get(name) is not None
                for name in ("row_count", "schema_sha256", "content_sha256")
            ):
                raise SiteHelperContractError(
                    "absent mutable-data relation contains fabricated evidence"
                )
            normalized.append(dict(record))
            continue
        if (
            isinstance(record.get("row_count"), bool)
            or not isinstance(record.get("row_count"), int)
            or record["row_count"] < 0
        ):
            raise SiteHelperContractError(
                "mutable-data audit table row count is invalid"
            )
        normalized.append(
            {
                "schema": expected[0],
                "table": expected[1],
                "state": "present",
                "row_count": record["row_count"],
                "schema_sha256": _require_digest(
                    record.get("schema_sha256"),
                    "mutable-data schema digest",
                ),
                "content_sha256": _require_digest(
                    record.get("content_sha256"),
                    "mutable-data content digest",
                ),
            }
        )
    return normalized


def _validate_polytao_archive_evidence(
    document: object,
    *,
    migration_exception: dict[str, Any],
) -> dict[str, Any] | None:
    if migration_exception["state"] == "absent":
        if document is not None:
            raise SiteHelperContractError(
                "post-0012 mutable-data audit fabricated a PolyTAO archive seal"
            )
        return None
    fields = {
        "schema_version",
        "row_count",
        "status_counts",
        "rows_sha256",
        "schema_sha256",
        "structure_counts",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise SiteHelperContractError(
            "mutable-data PolyTAO archive seal has an invalid shape"
        )
    row_count = document.get("row_count")
    status_counts = document.get("status_counts")
    business_statuses = {
        "pending",
        "submitted",
        "running",
        "completed",
        "failed",
        "cancelled",
    }
    if (
        document.get("schema_version") != 2
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count != migration_exception["row_count"]
        or not isinstance(status_counts, dict)
        or any(
            not isinstance(status, str)
            or status not in business_statuses
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            for status, count in status_counts.items()
        )
        or sum(status_counts.values()) != row_count
        or not isinstance(document.get("rows_sha256"), str)
        or BARE_SHA256_RE.fullmatch(document["rows_sha256"]) is None
        or not isinstance(document.get("schema_sha256"), str)
        or BARE_SHA256_RE.fullmatch(document["schema_sha256"]) is None
    ):
        raise SiteHelperContractError(
            "mutable-data PolyTAO archive seal is incomplete"
        )
    structure_counts = document.get("structure_counts")
    if (
        not isinstance(structure_counts, dict)
        or set(structure_counts)
        != {"columns", "indexes", "constraints", "triggers"}
        or any(
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for count in structure_counts.values()
        )
    ):
        raise SiteHelperContractError(
            "mutable-data PolyTAO archive structure seal is invalid"
        )
    return {
        "schema_version": 2,
        "row_count": row_count,
        "status_counts": dict(status_counts),
        "rows_sha256": document["rows_sha256"],
        "schema_sha256": document["schema_sha256"],
        "structure_counts": dict(structure_counts),
    }


def _validate_mutable_ledger(records: object) -> list[dict[str, str]]:
    if not isinstance(records, list) or len(records) not in {
        8,
        11,
        12,
        13,
        14,
        15,
    }:
        raise SiteHelperContractError(
            "mutable-data audit migration ledger is not a governed B/F state"
        )
    expected = [
        {"version": version, "checksum": checksum}
        for version, checksum in CANONICAL_MIGRATION_LEDGER[: len(records)]
    ]
    if records != expected:
        raise SiteHelperContractError(
            "mutable-data audit migration ledger is non-canonical"
        )
    return [dict(record) for record in expected]


def _validate_sequence_inventory(
    records: object,
    *,
    dft_ready: bool,
    md_queue_ready: bool,
) -> list[dict[str, Any]]:
    if not isinstance(records, list) or len(records) != len(DATA_SEQUENCES):
        raise SiteHelperContractError("mutable-data sequence inventory differs")
    fields = {
        "schema",
        "sequence",
        "ownership",
        "state",
        "data_type",
        "start_value",
        "min_value",
        "max_value",
        "increment_by",
        "cache_size",
        "cycle",
        "last_value",
        "is_called",
    }
    normalized: list[dict[str, Any]] = []
    for record, expected, expected_owner in zip(
        records,
        DATA_SEQUENCES,
        DATA_SEQUENCE_OWNERSHIP,
        strict=True,
    ):
        optional_dft = expected[0] == "monomer_dft"
        optional_md_queue = (
            expected[0] == "md"
            and expected[1] == "monomer_md_queue_sequence_seq"
        )
        expected_state = (
            "present"
            if (
                not optional_dft
                and not optional_md_queue
                or optional_dft
                and dft_ready
                or optional_md_queue
                and md_queue_ready
            )
            else "absent"
        )
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or (
                record.get("schema"),
                record.get("sequence"),
            )
            != expected[:2]
            or record.get("state") != expected_state
        ):
            raise SiteHelperContractError(
                "mutable-data sequence record is invalid"
            )
        value_fields = (
            "data_type",
            "start_value",
            "min_value",
            "max_value",
            "increment_by",
            "cache_size",
            "cycle",
            "last_value",
            "is_called",
        )
        if expected_state == "absent":
            if (
                record.get("ownership") is not None
                or any(record.get(name) is not None for name in value_fields)
            ):
                raise SiteHelperContractError(
                    "absent mutable-data sequence contains fabricated evidence"
                )
            normalized.append(dict(record))
            continue
        expected_ownership = {
            "schema": expected_owner[0],
            "table": expected_owner[1],
            "column": expected_owner[2],
            "ordinal": expected_owner[3],
            "deptype": expected_owner[4],
        }
        if (
            record.get("ownership") != expected_ownership
            or not isinstance(record.get("data_type"), str)
            or not record["data_type"]
            or any(
                isinstance(record.get(name), bool)
                or not isinstance(record.get(name), int)
                for name in (
                    "start_value",
                    "min_value",
                    "max_value",
                    "increment_by",
                    "cache_size",
                    "last_value",
                )
            )
            or not isinstance(record.get("cycle"), bool)
            or not isinstance(record.get("is_called"), bool)
        ):
            raise SiteHelperContractError(
                "mutable-data sequence state is invalid"
            )
        if (optional_dft or optional_md_queue) and {
            name: record[name]
            for name in (
                "data_type",
                "start_value",
                "min_value",
                "max_value",
                "increment_by",
                "cache_size",
                "cycle",
            )
        } != {
            "data_type": "bigint",
            "start_value": 1,
            "min_value": 1,
            "max_value": 9223372036854775807,
            "increment_by": 1,
            "cache_size": 1,
            "cycle": False,
        }:
            raise SiteHelperContractError(
                "governed task identity sequence parameters differ"
            )
        normalized.append(dict(record))
    return normalized


def _validate_governed_controls(
    document: object,
    *,
    controls_ready: bool,
) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document)
        != {"deployment_control", "database_analytics_snapshots"}
    ):
        raise SiteHelperContractError("governed-control inventory differs")
    deployment = document["deployment_control"]
    if (
        not isinstance(deployment, dict)
        or set(deployment) != {"table", "row"}
    ):
        raise SiteHelperContractError("deployment-control evidence is invalid")
    deployment_table = _validate_table_inventory(
        [deployment["table"]],
        (GOVERNED_CONTROL_TABLES[0],),
        absent_relations=(
            frozenset()
            if controls_ready
            else frozenset({GOVERNED_CONTROL_TABLES[0]})
        ),
    )[0]
    row = deployment["row"]
    if not controls_ready:
        if row is not None:
            raise SiteHelperContractError(
                "pre-0010 deployment-control evidence fabricated a row"
            )
    elif (
        deployment_table["row_count"] != 1
        or not isinstance(row, dict)
        or set(row)
        != {
            "control_key",
            "drain_enabled",
            "reason",
            "release_sha",
            "activated_at",
            "activated_by",
            "updated_at",
        }
        or row.get("control_key") != "production"
        or not isinstance(row.get("drain_enabled"), bool)
        or not isinstance(row.get("updated_at"), str)
        or not row["updated_at"]
    ):
        raise SiteHelperContractError(
            "deployment-control row is invalid"
        )
    if (
        controls_ready
        and row["drain_enabled"]
    ):
        if (
            not isinstance(row.get("reason"), str)
            or not row["reason"]
            or not isinstance(row.get("release_sha"), str)
            or FULL_SHA_RE.fullmatch(row["release_sha"]) is None
            or not isinstance(row.get("activated_at"), str)
            or not row["activated_at"]
            or not isinstance(row.get("activated_by"), str)
            or not row["activated_by"]
        ):
            raise SiteHelperContractError(
                "deployment-control drain lacks an operation owner"
            )
    elif controls_ready and any(
        row.get(name) is not None
        for name in (
            "reason",
            "release_sha",
            "activated_at",
            "activated_by",
        )
    ):
        raise SiteHelperContractError(
            "open deployment-control row retains stale drain authority"
        )

    analytics = document["database_analytics_snapshots"]
    if (
        not isinstance(analytics, dict)
        or set(analytics) != {"table", "entries"}
        or not isinstance(analytics.get("entries"), list)
    ):
        raise SiteHelperContractError("analytics control evidence is invalid")
    analytics_table = _validate_table_inventory(
        [analytics["table"]],
        (GOVERNED_CONTROL_TABLES[1],),
        absent_relations=(
            frozenset()
            if controls_ready
            else frozenset({GOVERNED_CONTROL_TABLES[1]})
        ),
    )[0]
    if not controls_ready and analytics["entries"]:
        raise SiteHelperContractError(
            "pre-0010 analytics evidence fabricated rows"
        )
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in analytics["entries"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"snapshot_key", "source_sha", "row_sha256"}
            or not isinstance(entry.get("snapshot_key"), str)
            or not entry["snapshot_key"]
            or entry["snapshot_key"] in seen
            or (
                entry.get("source_sha") is not None
                and (
                    not isinstance(entry["source_sha"], str)
                    or FULL_SHA_RE.fullmatch(entry["source_sha"]) is None
                )
            )
        ):
            raise SiteHelperContractError("analytics snapshot entry is invalid")
        seen.add(entry["snapshot_key"])
        entries.append(
            {
                "snapshot_key": entry["snapshot_key"],
                "source_sha": entry["source_sha"],
                "row_sha256": _require_digest(
                    entry.get("row_sha256"),
                    "analytics snapshot row digest",
                ),
            }
        )
    if (
        entries != sorted(entries, key=lambda value: value["snapshot_key"])
        or (
            controls_ready
            and analytics_table["row_count"] != len(entries)
        )
    ):
        raise SiteHelperContractError(
            "analytics snapshot inventory is incomplete or unordered"
        )
    return {
        "deployment_control": {
            "table": deployment_table,
            "row": dict(row) if isinstance(row, dict) else None,
        },
        "database_analytics_snapshots": {
            "table": analytics_table,
            "entries": entries,
        },
    }


def _validate_bridge_projection(
    document: object,
    *,
    leases_ready: bool,
) -> dict[str, Any]:
    fields = {
        "schema",
        "table",
        "projection",
        "state",
        "row_count",
        "content_sha256",
        "lease_columns",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema") != "md"
        or document.get("table") != "monomer_md_jobs"
        or document.get("projection") != "pre-0009-row-json-v1"
        or document.get("state") != "present"
        or isinstance(document.get("row_count"), bool)
        or not isinstance(document.get("row_count"), int)
        or document["row_count"] < 0
    ):
        raise SiteHelperContractError(
            "mutable-data bridge projection is invalid"
        )
    lease_columns = document["lease_columns"]
    column_names = {
        "worker_instance_id",
        "heartbeat_at",
        "lease_expires_at",
    }
    expected_state = "present" if leases_ready else "absent"
    if (
        not isinstance(lease_columns, dict)
        or set(lease_columns) != {"state", "non_null_counts"}
        or lease_columns.get("state") != expected_state
        or not isinstance(lease_columns.get("non_null_counts"), dict)
        or set(lease_columns["non_null_counts"]) != column_names
        or (
            leases_ready
            and any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > document["row_count"]
                for value in lease_columns["non_null_counts"].values()
            )
        )
        or (
            not leases_ready
            and any(
                value is not None
                for value in lease_columns["non_null_counts"].values()
            )
        )
    ):
        raise SiteHelperContractError(
            "mutable-data bridge lease-column projection is invalid"
        )
    return {
        **dict(document),
        "content_sha256": _require_digest(
            document.get("content_sha256"),
            "mutable-data bridge projection digest",
        ),
    }


def _validate_mutable_audit_role_security(
    document: object,
) -> dict[str, Any]:
    fields = {
        "role",
        "can_login",
        "superuser",
        "create_db",
        "create_role",
        "inherit",
        "replication",
        "bypass_rls",
        "role_settings",
        "direct_memberships",
        "effective_memberships",
        "has_pg_read_all_data",
        "has_pg_write_all_data",
        "owned_objects",
        "direct_write_grants",
        "effective_write_privileges",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("role") != "nexpoly_mutable_audit"
        or document.get("can_login") is not True
        or document.get("superuser") is not False
        or document.get("create_db") is not False
        or document.get("create_role") is not False
        or document.get("inherit") is not True
        or document.get("replication") is not False
        or document.get("bypass_rls") is not False
        or document.get("role_settings")
        != [
            {
                "database": "*",
                "settings": ["default_transaction_read_only=on"],
            }
        ]
        or document.get("direct_memberships")
        != [
            {
                "role": "pg_read_all_data",
                "admin_option": False,
                "inherit_option": True,
                "set_option": True,
            }
        ]
        or document.get("effective_memberships") != ["pg_read_all_data"]
        or document.get("has_pg_read_all_data") is not True
        or document.get("has_pg_write_all_data") is not False
    ):
        raise SiteHelperContractError(
            "mutable-data audit role authority is unsafe"
        )
    for field in (
        "owned_objects",
        "direct_write_grants",
        "effective_write_privileges",
    ):
        if document.get(field) != []:
            raise SiteHelperContractError(
                f"mutable-data audit role has unsafe {field}"
            )
    return dict(document)


def _validate_mutable_data_audit(
    document: object,
    *,
    expected_connection_port: int,
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "operation_id",
        "database",
        "database_system_identifier",
        "connection",
        "postgres_runtime",
        "role_security",
        "transaction_isolation",
        "transaction_read_only",
        "transaction_deferrable",
        "digest_algorithm",
        "migration_ledger",
        "business_tables",
        "governed_controls",
        "static_tables",
        "migration_exception",
        "migration_exception_archive_evidence",
        "sequences",
        "bridge_projection",
        "snapshot_sha256",
        "captured_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 6
        or not isinstance(document.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(document["operation_id"]) is None
        or document.get("database") != "nexpoly"
        or not isinstance(document.get("database_system_identifier"), str)
        or re.fullmatch(
            r"[0-9]{10,30}", document["database_system_identifier"]
        )
        is None
        or document.get("transaction_isolation") != "repeatable read"
        or document.get("transaction_read_only") is not True
        or document.get("transaction_deferrable") is not True
        or document.get("digest_algorithm")
        != "sha256-postgres-jsonb-copy-v4"
        or not isinstance(document.get("captured_at"), str)
        or not document["captured_at"]
    ):
        raise SiteHelperContractError(
            "mutable-data audit did not prove one read-only repeatable snapshot"
        )
    connection = document.get("connection")
    if (
        not isinstance(connection, dict)
        or set(connection)
        != {"service", "host", "port", "database", "user"}
        or connection.get("service") != "nexpoly-mutable-audit"
        or connection.get("host") != "127.0.0.1"
        or connection.get("port") != expected_connection_port
        or connection.get("database") != "nexpoly"
        or connection.get("user") != "nexpoly_mutable_audit"
    ):
        raise SiteHelperContractError(
            "mutable-data audit connection identity differs"
        )
    runtime = document.get("postgres_runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {
            "container_id",
            "image_id",
            "configured_image",
            "data_volume",
            "host_endpoint",
            "system_identifier",
        }
        or not isinstance(runtime.get("container_id"), str)
        or CONTAINER_RE.fullmatch(runtime["container_id"]) is None
        or DIGEST_RE.fullmatch(str(runtime.get("image_id"))) is None
        or not isinstance(runtime.get("configured_image"), str)
        or not runtime["configured_image"]
        or runtime.get("system_identifier")
        != document["database_system_identifier"]
    ):
        raise SiteHelperContractError(
            "mutable-data audit PostgreSQL runtime identity differs"
        )
    volume = runtime.get("data_volume")
    if (
        not isinstance(volume, dict)
        or set(volume)
        != {"type", "name", "source", "destination", "driver", "read_write"}
        or volume.get("type") != "volume"
        or not isinstance(volume.get("name"), str)
        or VOLUME_RE.fullmatch(volume["name"]) is None
        or not isinstance(volume.get("source"), str)
        or not Path(volume["source"]).is_absolute()
        or volume.get("destination") != "/var/lib/postgresql/data"
        or not isinstance(volume.get("driver"), str)
        or not volume["driver"]
        or volume.get("read_write") is not True
    ):
        raise SiteHelperContractError(
            "mutable-data audit PostgreSQL volume identity differs"
        )
    endpoint = runtime.get("host_endpoint")
    if (
        not isinstance(endpoint, dict)
        or endpoint
        != {
            "host": connection["host"],
            "port": connection["port"],
            "container_port": 5432,
            "protocol": "tcp",
        }
    ):
        raise SiteHelperContractError(
            "mutable-data audit endpoint is not the sealed PostgreSQL container"
        )
    role_security = _validate_mutable_audit_role_security(
        document.get("role_security")
    )

    ledger = _validate_mutable_ledger(document.get("migration_ledger"))
    versions = {record["version"] for record in ledger}
    controls_ready = "0010_deployment_control" in versions
    leases_ready = "0009_monomer_md_job_leases" in versions
    dft_ready = "0013_monomer_dft_jobs" in versions
    md_queue_ready = "0014_monomer_md_task_queue_cancel" in versions
    property_filter_ready = "0015_property_filter_performance" in versions
    contract_applied = "0012_drop_polytao_jobs" in versions
    business_relations = (
        BUSINESS_MUTABLE_TABLES + POST_0013_BUSINESS_MUTABLE_TABLES
    )
    business_tables = _validate_table_inventory(
        document.get("business_tables"),
        business_relations,
        absent_relations=(
            frozenset()
            if dft_ready
            else frozenset(POST_0013_BUSINESS_MUTABLE_TABLES)
        ),
    )
    if dft_ready:
        for record in business_tables:
            relation = (record["schema"], record["table"])
            expected_schema = MONOMER_DFT_TABLE_SCHEMA_SHA256.get(relation)
            if expected_schema is None:
                continue
            if (
                record["schema_sha256"] != expected_schema
                or (
                    record["row_count"] == 0
                    and record["content_sha256"]
                    != EMPTY_POSTGRES_COPY_SHA256
                )
            ):
                raise SiteHelperContractError(
                    "monomer DFT table schema or empty content differs"
                )
    static_tables = _validate_table_inventory(
        document.get("static_tables"),
        STATIC_IMPORT_TABLES,
        absent_relations=(
            frozenset()
            if property_filter_ready
            else frozenset(
                {("governance", "property_filter_options_snapshots")}
            )
        ),
    )
    migration_exception = _validate_table_inventory(
        [document.get("migration_exception")],
        (CONTRACT_0012_EXCEPTION_TABLE,),
        absent_relations=(
            frozenset({CONTRACT_0012_EXCEPTION_TABLE})
            if contract_applied
            else frozenset()
        ),
    )[0]
    migration_exception_archive_evidence = (
        _validate_polytao_archive_evidence(
            document.get("migration_exception_archive_evidence"),
            migration_exception=migration_exception,
        )
    )
    governed_controls = _validate_governed_controls(
        document.get("governed_controls"),
        controls_ready=controls_ready,
    )
    sequences = _validate_sequence_inventory(
        document.get("sequences"),
        dft_ready=dft_ready,
        md_queue_ready=md_queue_ready,
    )
    bridge_projection = _validate_bridge_projection(
        document.get("bridge_projection"),
        leases_ready=leases_ready,
    )
    monomer_md = next(
        record
        for record in business_tables
        if (record["schema"], record["table"])
        == ("md", "monomer_md_jobs")
    )
    if bridge_projection["row_count"] != monomer_md["row_count"]:
        raise SiteHelperContractError(
            "mutable-data bridge projection row count differs"
        )
    identity = {
        "operation_id": document["operation_id"],
        "database": "nexpoly",
        "database_system_identifier": document["database_system_identifier"],
        "connection": connection,
        "postgres_runtime": runtime,
        "role_security": role_security,
        "digest_algorithm": "sha256-postgres-jsonb-copy-v4",
        "migration_ledger": ledger,
        "business_tables": business_tables,
        "governed_controls": governed_controls,
        "static_tables": static_tables,
        "migration_exception": migration_exception,
        "migration_exception_archive_evidence": (
            migration_exception_archive_evidence
        ),
        "sequences": sequences,
        "bridge_projection": bridge_projection,
    }
    if document.get("snapshot_sha256") != sha256_bytes(
        canonical_json_bytes(identity)
    ):
        raise SiteHelperContractError("mutable-data snapshot digest differs")
    return {
        "schema_version": 6,
        **identity,
        "transaction_isolation": "repeatable read",
        "transaction_read_only": True,
        "transaction_deferrable": True,
        "snapshot_sha256": document["snapshot_sha256"],
        "captured_at": document["captured_at"],
    }


def validate_mutable_data_audit(document: object) -> dict[str, Any]:
    """Validate the production helper's fixed localhost endpoint."""

    return _validate_mutable_data_audit(
        document,
        expected_connection_port=55432,
    )


def validate_monomer_dft_0013_creation(
    document: object,
) -> dict[str, Any]:
    """Return the exact pristine DFT projection created by migration 0013."""

    validated = validate_mutable_data_audit(document)
    if (
        validated["migration_ledger"][-1]
        != {
            "version": "0013_monomer_dft_jobs",
            "checksum": CANONICAL_0013_CHECKSUM,
        }
    ):
        raise SiteHelperContractError(
            "monomer DFT creation evidence is not at canonical 0013"
        )
    tables = [
        record
        for record in validated["business_tables"]
        if (record["schema"], record["table"])
        in MONOMER_DFT_TABLE_SCHEMA_SHA256
    ]
    if (
        len(tables) != len(MONOMER_DFT_TABLE_SCHEMA_SHA256)
        or any(
            record["state"] != "present"
            or record["row_count"] != 0
            or record["content_sha256"] != EMPTY_POSTGRES_COPY_SHA256
            for record in tables
        )
    ):
        raise SiteHelperContractError(
            "migration 0013 did not create three empty DFT tables"
        )
    sequence = next(
        (
            record
            for record in validated["sequences"]
            if (
                record["schema"],
                record["sequence"],
            )
            == ("monomer_dft", "jobs_enqueue_sequence_seq")
        ),
        None,
    )
    if (
        sequence is None
        or sequence["state"] != "present"
        or sequence["last_value"] != 1
        or sequence["is_called"] is not False
    ):
        raise SiteHelperContractError(
            "migration 0013 identity sequence is not pristine"
        )
    return {
        "tables": [dict(record) for record in tables],
        "sequence": dict(sequence),
    }


def validate_evidence(
    helper: str,
    document: object,
    *,
    expected_runtime_digest: str | None = None,
    expected_users: dict[str, str] | None = None,
    expected_media_authority_rules_digest: str | None = None,
    expected_runtime_registry_digest: str | None = None,
) -> dict[str, Any]:
    validators: dict[str, Callable[..., dict[str, Any]]] = {
        "bootstrap-active-jobs-probe": validate_active_jobs,
        "bootstrap-legacy-runtime-status": validate_legacy_status,
        "bootstrap-legacy-runtime-resume-unchanged": validate_legacy_resume,
        "bootstrap-legacy-runtime-restore": validate_legacy_restore,
        "contract-0012-external-database-audit": validate_external_database_audit,
        "deployment-mutable-data-audit": validate_mutable_data_audit,
    }
    validator = validators.get(helper)
    if validator is None:
        raise SiteHelperContractError("helper has no site-evidence contract")
    if helper in {
        "bootstrap-active-jobs-probe",
        "deployment-mutable-data-audit",
    }:
        return validator(document)
    if helper == "contract-0012-external-database-audit":
        return validator(
            document,
            expected_users=expected_users,
            expected_media_authority_rules_digest=(
                expected_media_authority_rules_digest
            ),
            expected_runtime_registry_digest=(
                expected_runtime_registry_digest
            ),
        )
    return validator(document, expected_runtime_digest=expected_runtime_digest)


def inspect_helper_installation(runtime_root: Path) -> dict[str, Any]:
    runtime_root = runtime_root.absolute()
    config = runtime_root / "config"
    try:
        metadata = config.lstat()
    except OSError as exc:
        raise SiteHelperContractError("site-helper config directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or config.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SiteHelperContractError("site-helper config directory is unsafe")
    records: dict[str, dict[str, str]] = {}
    for name, contract in sorted(HELPERS.items()):
        path = config / name
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SiteHelperContractError(f"site helper is unavailable: {name}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise SiteHelperContractError(f"site helper is unsafe: {name}")
        records[name] = {
            **contract,
            "path": str(path),
            "sha256": sha256_file(path),
            "mode": "0700",
        }
    return {
        "schema_version": 1,
        "ready": True,
        "runtime_root": str(runtime_root),
        "executed_helpers": False,
        "helpers": records,
    }


def _load_evidence(path: str) -> object:
    if path == "-":
        payload = sys.stdin.buffer.read(MAX_EVIDENCE_BYTES + 1)
    else:
        evidence = Path(path)
        metadata = evidence.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or evidence.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise SiteHelperContractError("evidence input file is unsafe")
        payload = evidence.read_bytes()
    if not payload or len(payload) > MAX_EVIDENCE_BYTES:
        raise SiteHelperContractError("evidence input is empty or too large")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SiteHelperContractError("evidence input is invalid JSON") from exc


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    ready = commands.add_parser("readiness")
    ready.add_argument("--runtime-root", default=str(PRODUCTION_RUNTIME_ROOT))
    validate = commands.add_parser("validate")
    validate.add_argument("--helper", required=True, choices=sorted(EVIDENCE_HELPERS))
    validate.add_argument("--input", default="-")
    validate.add_argument("--expected-runtime-digest")
    validate.add_argument("--dev-audit-user")
    validate.add_argument("--health-audit-user")
    validate.add_argument("--media-authority-rules-sha256")
    validate.add_argument("--runtime-media-registry-sha256")
    commands.add_parser("describe")
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "readiness":
            document = inspect_helper_installation(Path(arguments.runtime_root))
        elif arguments.command == "describe":
            document = {
                "schema_version": 1,
                "helpers": HELPERS,
                "evidence_helpers": sorted(EVIDENCE_HELPERS),
                "readiness_executes_helpers": False,
            }
        else:
            expected_users = None
            if arguments.dev_audit_user or arguments.health_audit_user:
                if not arguments.dev_audit_user or not arguments.health_audit_user:
                    raise SiteHelperContractError(
                        "both external database audit users are required"
                    )
                expected_users = {
                    "nexpoly_dev": arguments.dev_audit_user,
                    "nexpoly_md_health_opt": arguments.health_audit_user,
                }
            document = validate_evidence(
                arguments.helper,
                _load_evidence(arguments.input),
                expected_runtime_digest=arguments.expected_runtime_digest,
                expected_users=expected_users,
                expected_media_authority_rules_digest=(
                    arguments.media_authority_rules_sha256
                ),
                expected_runtime_registry_digest=(
                    arguments.runtime_media_registry_sha256
                ),
            )
    except (OSError, SiteHelperContractError) as exc:
        print(f"site-helper-contracts: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
