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
from pathlib import Path
import re
import stat
import sys
from typing import Any, Callable


PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
MAX_EVIDENCE_BYTES = 64 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
ROLE_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,62}$")
SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")
VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PG_SYSTEM_ID_RE = re.compile(r"^[0-9]{1,20}$")

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
MUTABLE_DATA_TABLES = (
    ("online_knowledge", "history"),
    ("online_knowledge", "jobs"),
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


def validate_external_database_audit(
    document: object,
    *,
    expected_users: dict[str, str] | None,
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "inventory_complete",
        "writable_target",
        "databases",
    }:
        raise SiteHelperContractError("external-database evidence has an invalid shape")
    if (
        document.get("schema_version") != 1
        or document.get("inventory_complete") is not True
        or document.get("writable_target")
        != {"stack": "production", "database": "nexpoly"}
    ):
        raise SiteHelperContractError("external-database inventory is incomplete")
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
    return {
        "schema_version": 1,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "databases": [records[name] for name in sorted(records)],
    }


def validate_mutable_data_audit(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "database",
        "database_system_identifier",
        "connection",
        "postgres_runtime",
        "transaction_isolation",
        "transaction_read_only",
        "transaction_deferrable",
        "digest_algorithm",
        "tables",
        "snapshot_sha256",
        "captured_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 2
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
        != "sha256-postgres-jsonb-copy-v1"
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
        or connection.get("port") != 55432
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
    records = document.get("tables")
    if not isinstance(records, list) or len(records) != len(MUTABLE_DATA_TABLES):
        raise SiteHelperContractError("mutable-data audit table inventory differs")
    normalized: list[dict[str, Any]] = []
    for record, expected in zip(records, MUTABLE_DATA_TABLES, strict=True):
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "schema",
                "table",
                "row_count",
                "schema_sha256",
                "content_sha256",
            }
            or (record.get("schema"), record.get("table")) != expected
            or isinstance(record.get("row_count"), bool)
            or not isinstance(record.get("row_count"), int)
            or record["row_count"] < 0
        ):
            raise SiteHelperContractError(
                "mutable-data audit table record is invalid"
            )
        normalized.append(
            {
                "schema": expected[0],
                "table": expected[1],
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
    identity = {
        "database": "nexpoly",
        "database_system_identifier": document["database_system_identifier"],
        "connection": connection,
        "postgres_runtime": runtime,
        "digest_algorithm": "sha256-postgres-jsonb-copy-v1",
        "tables": normalized,
    }
    if document.get("snapshot_sha256") != sha256_bytes(
        canonical_json_bytes(identity)
    ):
        raise SiteHelperContractError("mutable-data snapshot digest differs")
    return {
        "schema_version": 2,
        **identity,
        "transaction_isolation": "repeatable read",
        "transaction_read_only": True,
        "transaction_deferrable": True,
        "snapshot_sha256": document["snapshot_sha256"],
        "captured_at": document["captured_at"],
    }


def validate_evidence(
    helper: str,
    document: object,
    *,
    expected_runtime_digest: str | None = None,
    expected_users: dict[str, str] | None = None,
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
        return validator(document, expected_users=expected_users)
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
            )
    except (OSError, SiteHelperContractError) as exc:
        print(f"site-helper-contracts: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
