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
}

EVIDENCE_HELPERS = {
    "bootstrap-active-jobs-probe",
    "bootstrap-legacy-runtime-status",
    "bootstrap-legacy-runtime-resume-unchanged",
    "bootstrap-legacy-runtime-restore",
    "contract-0012-external-database-audit",
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
    identity = {
        name: _require_digest(document.get(name), name)
        for name in ("backend_image_id", "web_image_id", "worker_unit_sha256")
    }
    return sha256_bytes(canonical_json_bytes(identity))


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
    }
    validator = validators.get(helper)
    if validator is None:
        raise SiteHelperContractError("helper has no site-evidence contract")
    if helper == "bootstrap-active-jobs-probe":
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
