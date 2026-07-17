#!/usr/bin/env python3
"""Immutable router for content-addressed production control releases.

Only this module and the two tiny stable wrappers live in ``runtime/bin``.  Every
controller, maintenance helper, and Worker launcher is loaded from an
immutable, manifest-sealed release below ``runtime/control-releases``.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Mapping


PROTOCOL_VERSION = 1
SOURCE_MANIFEST_SCHEMA_VERSION = 1
CONTROL_MANIFEST_SCHEMA_VERSION = 1
CONTROL_CANDIDATE_SCHEMA_VERSION = 1
ACTIVE_CONTROL_SCHEMA_VERSION = 1
PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
SOURCE_MANIFEST_RELATIVE_PATH = "scripts/control-release.json"
CONTROL_MANIFEST_NAME = "CONTROL-MANIFEST.json"
BOOTSTRAP_AUTHORITY_NAME = "bootstrap-control.json"
BOOTSTRAP_IMMUTABLE_FILES = {
    "control_runtime_selector.py",
    "nexpoly-pull-deploy",
    "nexpoly-pull-contract-0012",
    "nexpoly-reconcile-production-0005-polytao-alias",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}\.py$")
SAFE_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
SAFE_CONFIG_RE = re.compile(r"^config/[a-z][a-z0-9_.-]{0,127}$")
ALIAS_MARKER_RELATIVE = Path("state/maintenance/0005-polytao-alias/operation.json")
ALIAS_AUDIT_ROOT_RELATIVE = Path("audit/maintenance/0005-polytao-alias")
ALIAS_BACKUP_ROOT_RELATIVE = Path("backups/maintenance/0005-polytao-alias")
ALIAS_ACTION = "reconcile-production-0005-polytao-alias"
ALIAS_VERSION = "0005_polytao_jobs"
ALIAS_CHECKSUM = (
    "b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5"
)
ALIAS_APPLIED_AT = "2026-07-08T03:44:05.662979Z"
ALIAS_RESTORE_IMAGE = (
    "postgres:16-alpine@sha256:"
    "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
ALIAS_SYSTEM_IDENTIFIER = "7659245354718314530"
ALIAS_DATABASE_ENDPOINT = {
    "host_sha256": "12ca17b49af2289436f303e0166030a21e525d266e209267433801a8fd4071a0",
    "port": 55432,
    "database": "nexpoly",
    "user": "polyprop",
    "sslmode": "disable",
}
ALIAS_CANONICAL_LEDGER = [
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
    ("0007_polytao_jobs", ALIAS_CHECKSUM),
    (
        "0008_polytao_backend_runtime",
        "d0d8b2187aad8657269600873d3d2630e30c7d72da2f6662e18ab22031deff90",
    ),
]
ALIAS_PRE_LEDGER = sorted(
    [*ALIAS_CANONICAL_LEDGER, (ALIAS_VERSION, ALIAS_CHECKSUM)]
)
ALIAS_POST_LEDGER = sorted(ALIAS_CANONICAL_LEDGER)
ALIAS_EXPECTED_SCHEMA_SHA256 = (
    "8594868c661024af0766627a2d48280fc6967b8efe445878fc2a252a4520000c"
)
ALIAS_EXPECTED_STRUCTURE_COUNTS = {
    "columns": 23,
    "indexes": 3,
    "constraints": 6,
    "triggers": 0,
}
ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256 = (
    "db77ff078329ed4ec8b00f70172be743b9f3e67924d27716fba26277466ecfdd"
)
ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS = {
    "columns": 3,
    "indexes": 1,
    "constraints": 1,
    "triggers": 0,
}
ALIAS_AUDIT_NAMES = {
    "pg-restore.list",
    "isolated-postgres16-restore.json",
    "database-after.json",
    "AUDIT-MANIFEST.json",
}
ALIAS_BACKUP_NAMES = {
    "nexpoly-before.dump",
    "nexpoly-before.dump.sha256",
}
REQUIRED_COMPATIBILITY = {
    "handoff_protocol_versions": 1,
    "descriptor_schema_versions": 2,
    "current_state_schema_versions": 2,
    "marker_schema_versions": 2,
    "worker_slot_schema_versions": 2,
}


class ControlRuntimeError(RuntimeError):
    """Fail-closed control release validation error."""


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def canonical_json_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def release_identity(document_without_release_id: Mapping[str, Any]) -> str:
    return canonical_json_digest(document_without_release_id).removeprefix("sha256:")


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControlRuntimeError(f"control directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ControlRuntimeError(f"control directory is unsafe: {path}")


def _load_private_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise ControlRuntimeError(f"control record is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(payload) > 1024 * 1024
    ):
        raise ControlRuntimeError(f"control record is unsafe: {path}")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ControlRuntimeError(f"control record is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ControlRuntimeError(f"control record is invalid: {path}")
    return value


def _private_file_identity(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ControlRuntimeError(f"alias evidence is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ControlRuntimeError(f"alias evidence is unsafe: {path}")
    return {
        "size": metadata.st_size,
        "sha256": sha256_file(path).removeprefix("sha256:"),
        "mode": 0o600,
    }


def _alias_evidence_files(
    audit_dir: Path, backup_dir: Path
) -> dict[str, dict[str, Any]]:
    paths = {
        "audit/pg-restore.list": audit_dir / "pg-restore.list",
        "audit/isolated-postgres16-restore.json": (
            audit_dir / "isolated-postgres16-restore.json"
        ),
        "audit/database-after.json": audit_dir / "database-after.json",
        "backup/nexpoly-before.dump": backup_dir / "nexpoly-before.dump",
        "backup/nexpoly-before.dump.sha256": (
            backup_dir / "nexpoly-before.dump.sha256"
        ),
    }
    return {name: _private_file_identity(path) for name, path in paths.items()}


def _alias_ledger_pairs(value: object) -> list[tuple[str, str]] | None:
    if not isinstance(value, list):
        return None
    pairs: list[tuple[str, str]] = []
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row) != {"version", "checksum", "applied_at"}
            or not isinstance(row.get("version"), str)
            or not isinstance(row.get("checksum"), str)
            or not isinstance(row.get("applied_at"), str)
            or not row["applied_at"]
        ):
            return None
        pairs.append((row["version"], row["checksum"]))
    return pairs


def _alias_archive_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "row_count",
        "status_counts",
        "rows_sha256",
        "schema_sha256",
        "structure_counts",
    }:
        return False
    row_count = value.get("row_count")
    status_counts = value.get("status_counts")
    return bool(
        not isinstance(row_count, bool)
        and isinstance(row_count, int)
        and row_count >= 0
        and isinstance(status_counts, dict)
        and all(
            isinstance(status, str)
            and bool(status)
            and not isinstance(count, bool)
            and isinstance(count, int)
            and count >= 0
            for status, count in status_counts.items()
        )
        and sum(status_counts.values()) == row_count
        and isinstance(value.get("rows_sha256"), str)
        and HEX_DIGEST_RE.fullmatch(value["rows_sha256"]) is not None
        and value.get("schema_sha256") == ALIAS_EXPECTED_SCHEMA_SHA256
        and value.get("structure_counts") == ALIAS_EXPECTED_STRUCTURE_COUNTS
    )


def _alias_restore_image_is_valid(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"digest_ref", "image_id"}
        and value.get("digest_ref") == ALIAS_RESTORE_IMAGE
        and isinstance(value.get("image_id"), str)
        and DIGEST_RE.fullmatch(value["image_id"]) is not None
    )


def _alias_relation_is_valid(value: object, *, owner: str) -> bool:
    return value == {
        "kind": "r",
        "persistence": "p",
        "is_partition": False,
        "row_security": False,
        "force_row_security": False,
        "owner": owner,
        "parents": 0,
        "children": 0,
    }


def _alias_live_inventory_is_valid(
    value: object,
    *,
    ledger: list[tuple[str, str]],
    archive: object | None = None,
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "database",
        "current_user",
        "database_owner",
        "server_version_num",
        "in_recovery",
        "system_identifier",
        "ledger",
        "archive",
        "ledger_schema_sha256",
        "ledger_structure_counts",
        "polytao_relation",
        "ledger_relation",
    }:
        return False
    rows = value.get("ledger")
    alias_rows = (
        [row for row in rows if row.get("version") == ALIAS_VERSION]
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
        else []
    )
    return bool(
        value.get("database") == ALIAS_DATABASE_ENDPOINT["database"]
        and value.get("current_user") == ALIAS_DATABASE_ENDPOINT["user"]
        and value.get("database_owner") == "polyprop"
        and not isinstance(value.get("server_version_num"), bool)
        and isinstance(value.get("server_version_num"), int)
        and 160000 <= value["server_version_num"] < 170000
        and value.get("in_recovery") is False
        and str(value.get("system_identifier")) == ALIAS_SYSTEM_IDENTIFIER
        and _alias_ledger_pairs(rows) == ledger
        and (
            not any(pair[0] == ALIAS_VERSION for pair in ledger)
            or len(alias_rows) == 1
            and alias_rows[0].get("applied_at") == ALIAS_APPLIED_AT
        )
        and (
            _alias_archive_is_valid(value.get("archive"))
            if archive is None
            else value.get("archive") == archive
        )
        and value.get("ledger_schema_sha256")
        == ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256
        and value.get("ledger_structure_counts")
        == ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS
        and _alias_relation_is_valid(value.get("polytao_relation"), owner="polyprop")
        and _alias_relation_is_valid(value.get("ledger_relation"), owner="polyprop")
    )


def _alias_restore_inventory_matches(before: object, restored: object) -> bool:
    if not isinstance(before, dict) or not isinstance(restored, dict):
        return False
    return bool(
        set(restored) == set(before)
        and restored.get("database") == "nexpoly_alias_restore"
        and restored.get("current_user") == "postgres"
        and restored.get("database_owner") == "postgres"
        and restored.get("in_recovery") is False
        and not isinstance(restored.get("server_version_num"), bool)
        and isinstance(restored.get("server_version_num"), int)
        and 160000 <= restored["server_version_num"] < 170000
        and isinstance(restored.get("system_identifier"), str)
        and restored["system_identifier"].isdigit()
        and restored.get("ledger") == before.get("ledger")
        and restored.get("archive") == before.get("archive")
        and restored.get("ledger_schema_sha256")
        == before.get("ledger_schema_sha256")
        and restored.get("ledger_structure_counts")
        == before.get("ledger_structure_counts")
        and _alias_relation_is_valid(restored.get("polytao_relation"), owner="postgres")
        and _alias_relation_is_valid(restored.get("ledger_relation"), owner="postgres")
    )


def load_production_0005_alias_gate(
    runtime_root: Path, *, require_completed: bool
) -> dict[str, Any] | None:
    """Validate the durable one-purpose alias repair gate and all evidence."""

    marker_path = runtime_root / ALIAS_MARKER_RELATIVE
    if not (marker_path.exists() or marker_path.is_symlink()):
        if require_completed:
            raise ControlRuntimeError(
                "production 0005 ledger-alias reconciliation is required"
            )
        return None
    marker = _load_private_json(marker_path)
    identity = marker.get("identity")
    operation_id = identity.get("operation_id") if isinstance(identity, dict) else None
    directories = marker.get("operation_directories")
    if (
        marker.get("schema_version") != 1
        or marker.get("action") != ALIAS_ACTION
        or marker.get("phase") not in {
            "directory-intent",
            "planned",
            "runtime-fenced",
            "locked-preverified",
            "backup-started",
            "backup-complete",
            "restore-started",
            "restore-verified",
            "mutation-intent",
            "mutation-commit-started",
            "mutation-committed",
            "completed",
        }
        or not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
        or directories
        != {
            "audit": str(runtime_root / ALIAS_AUDIT_ROOT_RELATIVE / operation_id),
            "backup": str(runtime_root / ALIAS_BACKUP_ROOT_RELATIVE / operation_id),
        }
    ):
        raise ControlRuntimeError("production 0005 alias marker is invalid")
    if marker["phase"] != "completed":
        if require_completed:
            raise ControlRuntimeError(
                "production 0005 ledger-alias reconciliation is incomplete"
            )
        return marker
    expected_alias = {
        "version": ALIAS_VERSION,
        "checksum": ALIAS_CHECKSUM,
        "applied_at": ALIAS_APPLIED_AT,
    }
    audit_dir = runtime_root / ALIAS_AUDIT_ROOT_RELATIVE / operation_id
    backup_dir = runtime_root / ALIAS_BACKUP_ROOT_RELATIVE / operation_id
    _require_private_directory(audit_dir)
    _require_private_directory(backup_dir)
    if {entry.name for entry in audit_dir.iterdir()} != ALIAS_AUDIT_NAMES or {
        entry.name for entry in backup_dir.iterdir()
    } != ALIAS_BACKUP_NAMES:
        raise ControlRuntimeError("production 0005 alias evidence inventory differs")
    if identity.get("alias") != expected_alias:
        raise ControlRuntimeError("production 0005 alias identity differs")
    manifest_path = audit_dir / "AUDIT-MANIFEST.json"
    manifest = _load_private_json(manifest_path)
    manifest_sha = sha256_file(manifest_path).removeprefix("sha256:")
    after = _load_private_json(audit_dir / "database-after.json")
    restore = _load_private_json(audit_dir / "isolated-postgres16-restore.json")
    backup = marker.get("database_backup")
    before = marker.get("before")
    mutation_intent = marker.get("mutation_intent")
    dump_path = backup_dir / "nexpoly-before.dump"
    sidecar_path = backup_dir / "nexpoly-before.dump.sha256"
    _private_file_identity(dump_path)
    _private_file_identity(sidecar_path)
    try:
        sidecar = sidecar_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ControlRuntimeError("production 0005 alias dump hash is invalid") from exc
    actual_dump_sha = sha256_file(dump_path).removeprefix("sha256:")
    files = _alias_evidence_files(audit_dir, backup_dir)
    binary_hashes = identity.get("binaries_sha256")
    audit_binaries = manifest.get("binaries")
    restore_image = identity.get("restore_image")
    if (
        not isinstance(backup, dict)
        or not isinstance(before, dict)
        or not isinstance(mutation_intent, dict)
        or identity.get("database_endpoint") != ALIAS_DATABASE_ENDPOINT
        or identity.get("database_system_identifier") != ALIAS_SYSTEM_IDENTIFIER
        or not _alias_live_inventory_is_valid(before, ledger=ALIAS_PRE_LEDGER)
        or not _alias_live_inventory_is_valid(
            after,
            ledger=ALIAS_POST_LEDGER,
            archive=before.get("archive"),
        )
        or any(
            before.get(key) != after.get(key)
            for key in before
            if key != "ledger"
        )
        or after.get("ledger")
        != [
            row
            for row in before.get("ledger", [])
            if isinstance(row, dict) and row.get("version") != ALIAS_VERSION
        ]
        or backup.get("dump_path") != str(dump_path)
        or backup.get("dump_sha256") != actual_dump_sha
        or backup.get("dump_size") != dump_path.stat().st_size
        or backup.get("restore_list_sha256")
        != files["audit/pg-restore.list"]["sha256"]
        or sidecar != actual_dump_sha
        or restore != marker.get("isolated_restore")
        or restore.get("dump_sha256") != actual_dump_sha
        or not _alias_restore_image_is_valid(restore_image)
        or restore.get("image") != restore_image
        or restore.get("archive") != before.get("archive")
        or restore.get("ledger_schema_sha256")
        != before.get("ledger_schema_sha256")
        or not _alias_restore_inventory_matches(
            before, restore.get("database_inventory")
        )
        or mutation_intent
        != {
            "database_system_identifier": identity.get(
                "database_system_identifier"
            ),
            "alias": expected_alias,
            "pre_ledger": before.get("ledger"),
            "archive": before.get("archive"),
            "dump_sha256": actual_dump_sha,
            "restore_dump_sha256": actual_dump_sha,
        }
        or after != marker.get("after")
        or marker.get("audit_manifest_sha256") != manifest_sha
        or manifest.get("schema_version") != 1
        or manifest.get("operation_id") != operation_id
        or manifest.get("outcome") != "completed"
        or manifest.get("identity") != identity
        or manifest.get("database_before") != marker.get("before")
        or manifest.get("database_after") != after
        or manifest.get("database_backup") != backup
        or manifest.get("isolated_restore") != restore
        or manifest.get("files") != files
        or manifest.get("completed_at") != marker.get("completed_at")
        or set(manifest)
        != {
            "schema_version",
            "operation_id",
            "outcome",
            "identity",
            "database_before",
            "database_after",
            "database_backup",
            "isolated_restore",
            "binaries",
            "files",
            "completed_at",
        }
        or not isinstance(binary_hashes, dict)
        or not isinstance(audit_binaries, dict)
        or {
            path: record.get("sha256")
            for path, record in audit_binaries.items()
            if isinstance(path, str) and isinstance(record, dict)
        }
        != binary_hashes
        or not isinstance(marker.get("completed_at"), str)
        or HEX_DIGEST_RE.fullmatch(str(manifest_sha)) is None
    ):
        raise ControlRuntimeError("production 0005 alias completion evidence differs")
    control = identity.get("control")
    if not isinstance(control, dict):
        raise ControlRuntimeError("production 0005 alias control identity is invalid")
    release_id = control.get("release_id")
    if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ControlRuntimeError("production 0005 alias control release is invalid")
    control_manifest, control_root = load_control_release(runtime_root, release_id)
    entrypoint = control_manifest["entrypoints"].get(
        "reconcile-production-0005-alias"
    )
    if (
        control.get("source_sha") != control_manifest["source_sha"]
        or control.get("source_tree") != control_manifest["source_tree"]
        or control.get("manifest_sha256")
        != sha256_file(control_root / CONTROL_MANIFEST_NAME).removeprefix("sha256:")
        or not isinstance(entrypoint, dict)
        or entrypoint.get("kind") != "python"
        or control.get("script_sha256")
        != sha256_file(control_root / str(entrypoint.get("file"))).removeprefix(
            "sha256:"
        )
    ):
        raise ControlRuntimeError("production 0005 alias control evidence differs")
    return marker


def _validate_compatibility(value: object) -> dict[str, Any]:
    fields = {
        "handoff_protocol_versions",
        "descriptor_schema_versions",
        "current_state_schema_versions",
        "marker_schema_versions",
        "worker_slot_schema_versions",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ControlRuntimeError("control compatibility declaration is invalid")
    result: dict[str, list[int]] = {}
    for name in sorted(fields):
        versions = value[name]
        if (
            not isinstance(versions, list)
            or not versions
            or len(versions) > 16
            or any(
                not isinstance(item, int)
                or isinstance(item, bool)
                or not 1 <= item <= 1024
                for item in versions
            )
            or versions != sorted(set(versions))
        ):
            raise ControlRuntimeError("control compatibility versions are invalid")
        result[name] = list(versions)
    if PROTOCOL_VERSION not in result["handoff_protocol_versions"]:
        raise ControlRuntimeError("control release does not support this handoff protocol")
    for field, required in REQUIRED_COMPATIBILITY.items():
        if required not in result[field]:
            raise ControlRuntimeError(
                f"control release lacks required {field} version {required}"
            )
    return result


def _validate_entrypoints(value: object, file_names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise ControlRuntimeError("control entrypoint map is invalid")
    result: dict[str, dict[str, Any]] = {}
    for role, record in value.items():
        if not isinstance(role, str) or SAFE_ROLE_RE.fullmatch(role) is None:
            raise ControlRuntimeError("control role is unsafe")
        if not isinstance(record, dict) or record.get("kind") not in {
            "python",
            "worker",
        }:
            raise ControlRuntimeError("control entrypoint is invalid")
        if record["kind"] == "python":
            if set(record) != {"kind", "file"} or record.get("file") not in file_names:
                raise ControlRuntimeError("Python control entrypoint is invalid")
        else:
            if (
                set(record)
                != {"kind", "environment_loader", "launcher", "config_relative"}
                or record.get("environment_loader") not in file_names
                or record.get("launcher") not in file_names
                or not isinstance(record.get("config_relative"), str)
                or SAFE_CONFIG_RE.fullmatch(record["config_relative"]) is None
            ):
                raise ControlRuntimeError("Worker control entrypoint is invalid")
        result[role] = dict(record)
    if "deploy" not in result or result["deploy"].get("kind") != "python":
        raise ControlRuntimeError("control release lacks the deploy entrypoint")
    return result


def parse_source_manifest(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ControlRuntimeError("control source manifest is invalid JSON") from exc
    fields = {
        "schema_version",
        "protocol_version",
        "compatibility",
        "entrypoints",
        "files",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or not isinstance(document.get("files"), list)
        or not 1 <= len(document["files"]) <= 64
    ):
        raise ControlRuntimeError("control source manifest has an invalid shape")
    files: list[dict[str, Any]] = []
    names: set[str] = set()
    for record in document["files"]:
        if not isinstance(record, dict) or set(record) != {"name", "source", "mode"}:
            raise ControlRuntimeError("control source file record is invalid")
        name = record.get("name")
        source = record.get("source")
        mode = record.get("mode")
        pure = PurePosixPath(source) if isinstance(source, str) else PurePosixPath(".")
        if (
            not isinstance(name, str)
            or SAFE_NAME_RE.fullmatch(name) is None
            or name in names
            or not isinstance(source, str)
            or not source.startswith("scripts/")
            or pure.is_absolute()
            or ".." in pure.parts
            or pure.name != name
            or mode != 0o700
        ):
            raise ControlRuntimeError("control source file record is unsafe")
        names.add(name)
        files.append({"name": name, "source": source, "mode": mode})
    compatibility = _validate_compatibility(document["compatibility"])
    entrypoints = _validate_entrypoints(document["entrypoints"], names)
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "compatibility": compatibility,
        "entrypoints": entrypoints,
        "files": files,
    }


def validate_control_manifest(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "protocol_version",
        "release_id",
        "source_sha",
        "source_tree",
        "compatibility",
        "entrypoints",
        "files",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != CONTROL_MANIFEST_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or not isinstance(document.get("release_id"), str)
        or RELEASE_ID_RE.fullmatch(document["release_id"]) is None
        or not isinstance(document.get("source_sha"), str)
        or SHA_RE.fullmatch(document["source_sha"]) is None
        or not isinstance(document.get("source_tree"), str)
        or SHA_RE.fullmatch(document["source_tree"]) is None
        or not isinstance(document.get("files"), dict)
        or not 1 <= len(document["files"]) <= 64
    ):
        raise ControlRuntimeError("control release manifest has an invalid shape")
    files: dict[str, dict[str, Any]] = {}
    for name, record in document["files"].items():
        if (
            not isinstance(name, str)
            or SAFE_NAME_RE.fullmatch(name) is None
            or not isinstance(record, dict)
            or set(record) != {"sha256", "size", "mode"}
            or not isinstance(record.get("sha256"), str)
            or DIGEST_RE.fullmatch(record["sha256"]) is None
            or not isinstance(record.get("size"), int)
            or isinstance(record.get("size"), bool)
            or not 1 <= record["size"] <= 16 * 1024 * 1024
            or record.get("mode") != 0o700
        ):
            raise ControlRuntimeError("control release file identity is invalid")
        files[name] = dict(record)
    compatibility = _validate_compatibility(document["compatibility"])
    entrypoints = _validate_entrypoints(document["entrypoints"], set(files))
    normalized = {
        **document,
        "compatibility": compatibility,
        "entrypoints": entrypoints,
        "files": files,
    }
    identity_payload = {key: value for key, value in normalized.items() if key != "release_id"}
    if release_identity(identity_payload) != normalized["release_id"]:
        raise ControlRuntimeError("control release identity differs from its manifest")
    return normalized


def validate_candidate_record(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "protocol_version",
        "component",
        "release_id",
        "source_sha",
        "source_tree",
        "manifest_sha256",
        "operation_id",
        "prepared_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != CONTROL_CANDIDATE_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or document.get("component") != "deployment-controls"
        or not isinstance(document.get("release_id"), str)
        or RELEASE_ID_RE.fullmatch(document["release_id"]) is None
        or not isinstance(document.get("source_sha"), str)
        or SHA_RE.fullmatch(document["source_sha"]) is None
        or not isinstance(document.get("source_tree"), str)
        or SHA_RE.fullmatch(document["source_tree"]) is None
        or not isinstance(document.get("manifest_sha256"), str)
        or DIGEST_RE.fullmatch(document["manifest_sha256"]) is None
        or not isinstance(document.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(document["operation_id"]) is None
        or not isinstance(document.get("prepared_at"), str)
        or not document["prepared_at"]
    ):
        raise ControlRuntimeError("candidate control record has an invalid shape")
    return dict(document)


def validate_active_control_record(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "protocol_version",
        "component",
        "generation",
        "release_id",
        "source_sha",
        "source_tree",
        "manifest_sha256",
        "operation_id",
        "previous_release_id",
        "activated_at",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != ACTIVE_CONTROL_SCHEMA_VERSION
        or document.get("protocol_version") != PROTOCOL_VERSION
        or document.get("component") != "deployment-controls"
        or not isinstance(document.get("generation"), int)
        or isinstance(document.get("generation"), bool)
        or document["generation"] < 1
        or not isinstance(document.get("release_id"), str)
        or RELEASE_ID_RE.fullmatch(document["release_id"]) is None
        or not isinstance(document.get("source_sha"), str)
        or SHA_RE.fullmatch(document["source_sha"]) is None
        or not isinstance(document.get("source_tree"), str)
        or SHA_RE.fullmatch(document["source_tree"]) is None
        or not isinstance(document.get("manifest_sha256"), str)
        or DIGEST_RE.fullmatch(document["manifest_sha256"]) is None
        or not isinstance(document.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(document["operation_id"]) is None
        or (
            document.get("previous_release_id") is not None
            and (
                not isinstance(document["previous_release_id"], str)
                or RELEASE_ID_RE.fullmatch(document["previous_release_id"]) is None
            )
        )
        or not isinstance(document.get("activated_at"), str)
        or not document["activated_at"]
    ):
        raise ControlRuntimeError("active control record has an invalid shape")
    return dict(document)


def control_release_root(runtime_root: Path, release_id: str) -> Path:
    if RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ControlRuntimeError("control release identity is invalid")
    return runtime_root / "control-releases" / release_id


def active_control_record_path(runtime_root: Path) -> Path:
    return runtime_root / "state/active-control.json"


def _validate_bootstrap_authority(runtime_root: Path) -> dict[str, Any]:
    record = _load_private_json(
        runtime_root / "state" / BOOTSTRAP_AUTHORITY_NAME
    )
    expected_fields = {
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
    immutable = record.get("immutable_files")
    readiness = record.get("source_readiness")
    candidate = validate_candidate_record(record.get("candidate_control"))
    initial_active = validate_active_control_record(record.get("active_control"))
    if (
        set(record) != expected_fields
        or record.get("schema_version") != 2
        or record.get("status") != "completed"
        or SHA_RE.fullmatch(str(record.get("source_sha", ""))) is None
        or SHA_RE.fullmatch(str(record.get("source_tree", ""))) is None
        or not isinstance(readiness, dict)
        or set(readiness)
        != {
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
        or readiness.get("schema_version") != 2
        or readiness.get("ready") is not True
        or not isinstance(readiness.get("source_root"), str)
        or not Path(readiness["source_root"]).is_absolute()
        or readiness.get("source_sha") != record.get("source_sha")
        or readiness.get("source_tree") != record.get("source_tree")
        or readiness.get("branch") != "main"
        or readiness.get("origin") != "git@github.com:lzq390/ZhijuPoly.git"
        or readiness.get("remote_names") != ["origin"]
        or readiness.get("origin_fetch_urls")
        != ["git@github.com:lzq390/ZhijuPoly.git"]
        or readiness.get("origin_push_urls")
        != ["git@github.com:lzq390/ZhijuPoly.git"]
        or readiness.get("origin_main_sha") != record.get("source_sha")
        or readiness.get("standalone_object_database") is not True
        or readiness.get("shallow") is not False
        or readiness.get("dirty_entries") != 0
        or readiness.get("ignored_entries") != 0
        or readiness.get("unreachable_objects") != 0
        or readiness.get("replace_refs") != 0
        or readiness.get("special_index_entries") != 0
        or readiness.get("sparse_index") is not False
        or readiness.get("owner_private") is not True
        or readiness.get("group_or_world_writable") is not False
        or record.get("source_readiness_sha256")
        != canonical_json_digest(readiness)
        or not isinstance(record.get("delivery_gate"), dict)
        or not isinstance(record.get("production_repository"), dict)
        or not isinstance(record.get("worker_unit_takeover"), dict)
        or not isinstance(immutable, dict)
        or set(immutable) != BOOTSTRAP_IMMUTABLE_FILES
        or any(
            not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None
            for value in immutable.values()
        )
        or any(
            candidate.get(field) != initial_active.get(field)
            for field in (
                "protocol_version",
                "release_id",
                "source_sha",
                "source_tree",
                "manifest_sha256",
                "operation_id",
            )
        )
        or record["source_sha"] != candidate["source_sha"]
        or record["source_tree"] != candidate["source_tree"]
    ):
        raise ControlRuntimeError("completed bootstrap authority is invalid")
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
        or not isinstance(takeover.get("operation_id"), str)
        or OPERATION_ID_RE.fullmatch(takeover["operation_id"]) is None
        or takeover.get("authority_sha") != record["source_sha"]
        or takeover.get("authority_tree") != record["source_tree"]
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
        or not isinstance(takeover.get("git_identity"), dict)
        or set(takeover["git_identity"])
        != {"branch", "head_sha", "head_tree", "local_main_sha"}
        or takeover["git_identity"].get("branch") != "refs/heads/main"
        or takeover["git_identity"].get("head_sha")
        != takeover["git_identity"].get("local_main_sha")
        or any(
            not isinstance(takeover["git_identity"].get(name), str)
            or SHA_RE.fullmatch(takeover["git_identity"][name]) is None
            for name in ("head_sha", "head_tree", "local_main_sha")
        )
        or takeover["binding_sha256"]
        != canonical_json_digest(
            {
                key: value
                for key, value in takeover.items()
                if key != "binding_sha256"
            }
        )
    ):
        raise ControlRuntimeError(
            "completed bootstrap legacy takeover authority is invalid"
        )
    bin_root = runtime_root / "bin"
    _require_private_directory(bin_root)
    if {entry.name for entry in bin_root.iterdir()} != BOOTSTRAP_IMMUTABLE_FILES:
        raise ControlRuntimeError("immutable bootstrap router inventory differs")
    for name, expected_digest in immutable.items():
        path = bin_root / name
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ControlRuntimeError(
                f"immutable bootstrap router is unavailable: {name}"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or sha256_file(path) != expected_digest
        ):
            raise ControlRuntimeError(
                f"immutable bootstrap router differs: {name}"
            )
    return record


def load_control_release(
    runtime_root: Path, release_id: str
) -> tuple[dict[str, Any], Path]:
    _require_private_directory(runtime_root)
    parent = runtime_root / "control-releases"
    _require_private_directory(parent)
    root = control_release_root(runtime_root, release_id)
    _require_private_directory(root)
    manifest_path = root / CONTROL_MANIFEST_NAME
    manifest = validate_control_manifest(_load_private_json(manifest_path))
    if manifest["release_id"] != release_id:
        raise ControlRuntimeError("control release directory differs from its identity")
    expected_names = set(manifest["files"]) | {CONTROL_MANIFEST_NAME}
    actual_names = {entry.name for entry in root.iterdir()}
    if actual_names != expected_names:
        raise ControlRuntimeError("control release inventory contains extra or missing files")
    for name, identity in manifest["files"].items():
        path = root / name
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ControlRuntimeError(f"control release file is unavailable: {name}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != identity["mode"]
            or metadata.st_size != identity["size"]
            or sha256_file(path) != identity["sha256"]
        ):
            raise ControlRuntimeError(f"control release file differs: {name}")
    return manifest, root


def load_candidate_control(
    runtime_root: Path, candidate: object
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    record = validate_candidate_record(candidate)
    manifest, root = load_control_release(runtime_root, record["release_id"])
    if (
        sha256_file(root / CONTROL_MANIFEST_NAME) != record["manifest_sha256"]
        or any(
            manifest[key] != record[key]
            for key in ("source_sha", "source_tree", "release_id")
        )
    ):
        raise ControlRuntimeError("candidate control record differs from its release")
    return record, manifest, root


def load_active_control(
    runtime_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    _validate_bootstrap_authority(runtime_root)
    active = validate_active_control_record(
        _load_private_json(active_control_record_path(runtime_root))
    )
    manifest, root = load_control_release(runtime_root, active["release_id"])
    if (
        sha256_file(root / CONTROL_MANIFEST_NAME) != active["manifest_sha256"]
        or any(
            active[key] != manifest[key]
            for key in ("source_sha", "source_tree", "release_id")
        )
    ):
        raise ControlRuntimeError("active control record differs from its release")
    return active, manifest, root


def _active_matches_candidate(
    active: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    return all(
        active.get(field) == candidate.get(field)
        for field in (
            "protocol_version",
            "release_id",
            "source_sha",
            "source_tree",
            "manifest_sha256",
            "operation_id",
        )
    )


def _require_deploy_lock_held(runtime_root: Path) -> None:
    path = runtime_root / "state/deploy.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ControlRuntimeError("deployment transition lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ControlRuntimeError("deployment transition lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        raise ControlRuntimeError("deployment transition marker is not lock-owned")
    finally:
        os.close(descriptor)


def _worker_projection_matches(
    active: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    source_sha: object,
    source_tree: object,
    unit: object,
    slot: object,
    operation_id: object,
) -> bool:
    entrypoint = manifest.get("entrypoints", {}).get("monomer-md")
    files = manifest.get("files")
    if (
        not isinstance(entrypoint, dict)
        or entrypoint.get("kind") != "worker"
        or not isinstance(files, dict)
        or source_sha != active.get("source_sha")
        or source_tree != active.get("source_tree")
        or not isinstance(unit, dict)
        or not isinstance(slot, dict)
    ):
        return False
    launcher = files.get(entrypoint.get("launcher"))
    return bool(
        isinstance(launcher, dict)
        and unit.get("control_release_id") == active.get("release_id")
        and unit.get("launcher_sha256") == launcher.get("sha256")
        and slot.get("source_sha") == source_sha
        and slot.get("source_tree") == source_tree
        and slot.get("operation_id") == operation_id
        and active.get("operation_id") == operation_id
    )


def _asset_pointer_matches(runtime_root: Path, asset: object) -> bool:
    if not isinstance(asset, dict) or not isinstance(asset.get("root"), str):
        return False
    root = Path(asset["root"])
    pointer = runtime_root / "state/current-assets"
    try:
        metadata = pointer.lstat()
        target = Path(os.readlink(pointer))
    except OSError:
        return False
    if not target.is_absolute():
        target = pointer.parent / target
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and target.absolute() == root.absolute()
    )


def _validate_worker_route_authority(
    runtime_root: Path,
    active: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    current_path = runtime_root / "state/current-deployment.json"
    marker_path = runtime_root / "state/deploy-in-progress.json"
    marker_present = marker_path.exists() or marker_path.is_symlink()
    if not marker_present and (current_path.exists() or current_path.is_symlink()):
        current = _load_private_json(current_path)
        compatibility = manifest["compatibility"]["current_state_schema_versions"]
        if (
            current.get("schema_version") in compatibility
            and current.get("active_control") == active
            and _worker_projection_matches(
                active,
                manifest,
                source_sha=current.get("source_sha"),
                source_tree=current.get("source_tree"),
                unit=current.get("monomer_md_systemd_unit"),
                slot=current.get("active_monomer_md_slot"),
                operation_id=current.get("operation_id"),
            )
            and _asset_pointer_matches(runtime_root, current.get("asset_identity"))
        ):
            return

    if not marker_present:
        raise ControlRuntimeError(
            "active Worker controls differ from governed deployment authority"
        )
    marker = _load_private_json(marker_path)
    operation_id = marker.get("operation_id")
    descriptor_digest = marker.get("descriptor_sha256")
    if (
        marker.get("schema_version")
        not in manifest["compatibility"]["marker_schema_versions"]
        or marker.get("action") not in {"deploy", "explicit-rollback"}
        or not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
        or not isinstance(descriptor_digest, str)
        or DIGEST_RE.fullmatch(descriptor_digest) is None
    ):
        raise ControlRuntimeError("Worker transition marker is invalid")
    _require_deploy_lock_held(runtime_root)
    descriptor_path = runtime_root / "state/prepared" / operation_id / "descriptor.json"
    descriptor = _load_private_json(descriptor_path)
    if sha256_file(descriptor_path) != descriptor_digest:
        raise ControlRuntimeError("Worker transition descriptor differs from marker")
    controller = descriptor.get("controller")
    repository = descriptor.get("repository")
    monomer = descriptor.get("monomer_md")
    previous = descriptor.get("previous_deployment")
    if not isinstance(controller, dict) or not isinstance(repository, dict):
        raise ControlRuntimeError("Worker transition descriptor is incomplete")
    candidate = controller.get("executor_control")
    candidate_digest = controller.get("executor_control_sha256")
    if (
        not isinstance(candidate, dict)
        or canonical_json_digest(candidate) != candidate_digest
        or marker.get("executor_control") != candidate
        or marker.get("executor_control_sha256") != candidate_digest
    ):
        raise ControlRuntimeError("Worker transition control authority differs")
    switched = all(
        marker.get(field) is True
        for field in (
            "source_switched",
            "slot_switched",
            "unit_switched",
            "control_switched",
            "asset_switched",
        )
    )
    restored = (
        marker.get("runtime_stopped") is True
        and all(
            marker.get(field) is False
            for field in (
                "source_switched",
                "slot_switched",
                "unit_switched",
                "control_switched",
                "asset_switched",
            )
        )
    )
    pre_stop_previous = (
        marker.get("action") == "deploy"
        and marker.get("runtime_stopped") is False
        and marker.get("database_change_started") is False
        and marker.get("phase") in {"prepared", "drain-started", "drained", "failed"}
        and (
            marker.get("phase") != "failed"
            or marker.get("failed_phase") in {"prepared", "drain-started", "drained"}
        )
        and all(
            marker.get(field) is False
            for field in (
                "source_switched",
                "slot_switched",
                "unit_switched",
                "control_switched",
                "asset_switched",
            )
        )
    )
    if switched and _active_matches_candidate(active, candidate):
        if not isinstance(monomer, dict) or not _worker_projection_matches(
            active,
            manifest,
            source_sha=repository.get("target_sha"),
            source_tree=repository.get("target_tree"),
            unit=monomer.get("systemd_unit"),
            slot=monomer.get("slot_record"),
            operation_id=operation_id,
        ):
            raise ControlRuntimeError("candidate Worker transition identity differs")
        release_input = descriptor.get("release_input")
        if not isinstance(release_input, dict) or not _asset_pointer_matches(
            runtime_root, release_input.get("asset")
        ):
            raise ControlRuntimeError("candidate Worker asset identity differs")
        return
    previous_control = controller.get("previous_active_control")
    if (
        (restored or pre_stop_previous)
        and isinstance(previous, dict)
        and active == previous_control
        and previous_control == previous.get("active_control")
    ):
        if pre_stop_previous:
            if not (current_path.exists() or current_path.is_symlink()):
                raise ControlRuntimeError(
                    "unchanged pre-stop Worker authority has no current state"
                )
            current = _load_private_json(current_path)
            if current != previous:
                raise ControlRuntimeError(
                    "unchanged pre-stop Worker state differs from previous deployment"
                )
        if not _worker_projection_matches(
            active,
            manifest,
            source_sha=previous.get("source_sha"),
            source_tree=previous.get("source_tree"),
            unit=previous.get("monomer_md_systemd_unit"),
            slot=previous.get("active_monomer_md_slot"),
            operation_id=previous.get("operation_id"),
        ):
            raise ControlRuntimeError("restored Worker transition identity differs")
        if not _asset_pointer_matches(runtime_root, previous.get("asset_identity")):
            raise ControlRuntimeError("restored Worker asset identity differs")
        return
    raise ControlRuntimeError(
        "active Worker controls differ from governed deployment authority"
    )


def _selected_release(
    runtime_root: Path, role: str, arguments: list[str]
) -> tuple[dict[str, Any], Path]:
    """Route recovery/apply to a sealed candidate; all other calls use active."""

    alias_marker = None
    deploy_command = arguments[0] if role == "deploy" and arguments else None
    deploy_preparation = deploy_command in {"plan", "prepare"}
    if role in {
        "deploy",
        "contract-0012",
        "reconcile-production-0005-alias",
    }:
        alias_marker = load_production_0005_alias_gate(
            runtime_root,
            require_completed=(
                role == "contract-0012"
                or (role == "deploy" and not deploy_preparation)
            ),
        )
    if (
        role == "deploy"
        and deploy_preparation
        and alias_marker is not None
        and alias_marker.get("phase") != "completed"
    ):
        raise ControlRuntimeError(
            "interrupted alias reconciliation must recover before deployment preparation"
        )
    if role in {"contract-0012", "reconcile-production-0005-alias"}:
        deploy_marker = runtime_root / "state/deploy-in-progress.json"
        if deploy_marker.exists() or deploy_marker.is_symlink():
            raise ControlRuntimeError(
                "database maintenance is blocked by an interrupted code deployment"
            )
    if role == "reconcile-production-0005-alias":
        contract_marker = runtime_root / "state/contract-0012-in-progress.json"
        if contract_marker.exists() or contract_marker.is_symlink():
            raise ControlRuntimeError(
                "ledger-alias maintenance is blocked by interrupted 0012 maintenance"
            )
    if (
        role == "reconcile-production-0005-alias"
        and alias_marker is not None
    ):
        _validate_bootstrap_authority(runtime_root)
        identity = alias_marker.get("identity")
        control = identity.get("control") if isinstance(identity, dict) else None
        release_id = control.get("release_id") if isinstance(control, dict) else None
        if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
            raise ControlRuntimeError(
                "recorded alias reconciliation lacks sealed control authority"
            )
        recovery_manifest, recovery_root = load_control_release(
            runtime_root, release_id
        )
        entrypoint = recovery_manifest["entrypoints"].get(
            "reconcile-production-0005-alias"
        )
        if (
            not isinstance(entrypoint, dict)
            or entrypoint.get("kind") != "python"
            or control.get("source_sha") != recovery_manifest["source_sha"]
            or control.get("source_tree") != recovery_manifest["source_tree"]
            or control.get("manifest_sha256")
            != sha256_file(recovery_root / CONTROL_MANIFEST_NAME).removeprefix(
                "sha256:"
            )
            or control.get("script_sha256")
            != sha256_file(recovery_root / str(entrypoint.get("file"))).removeprefix(
                "sha256:"
            )
        ):
            raise ControlRuntimeError(
                "recorded alias reconciliation control authority differs"
            )
        return recovery_manifest, recovery_root
    active, manifest, root = load_active_control(runtime_root)
    if role == "monomer-md":
        _validate_worker_route_authority(runtime_root, active, manifest)
    if role != "deploy" or not arguments:
        return manifest, root
    command = arguments[0]
    operation_id: str | None = None
    if command in {"apply", "rollback"}:
        if arguments.count("--operation-id") != 1:
            raise ControlRuntimeError("deploy operation ID must occur exactly once")
        try:
            index = arguments.index("--operation-id")
            operation_id = arguments[index + 1]
        except IndexError:
            raise ControlRuntimeError("deploy operation ID is missing") from None
        if OPERATION_ID_RE.fullmatch(operation_id) is None:
            raise ControlRuntimeError("deploy operation ID is invalid")
    marker_path = runtime_root / "state/deploy-in-progress.json"
    if marker_path.exists() or marker_path.is_symlink():
        marker = _load_private_json(marker_path)
        if (
            not isinstance(marker.get("schema_version"), int)
            or isinstance(marker.get("schema_version"), bool)
            or marker.get("action") not in {"deploy", "explicit-rollback"}
            or not isinstance(marker.get("operation_id"), str)
            or OPERATION_ID_RE.fullmatch(marker["operation_id"]) is None
            or not isinstance(marker.get("executor_control"), dict)
            or not isinstance(marker.get("executor_control_sha256"), str)
        ):
            raise ControlRuntimeError("deployment marker lacks sealed control authority")
        candidate = marker["executor_control"]
        if (
            canonical_json_digest(candidate) != marker["executor_control_sha256"]
            or candidate.get("operation_id") != marker["operation_id"]
            or operation_id is not None
            and operation_id != marker["operation_id"]
        ):
            raise ControlRuntimeError("deployment marker control identity differs")
        _record, candidate_manifest, candidate_root = load_candidate_control(
            runtime_root, candidate
        )
        if (
            marker["schema_version"]
            not in candidate_manifest["compatibility"]["marker_schema_versions"]
        ):
            raise ControlRuntimeError(
                "deployment marker schema is unsupported by its executor"
            )
        return candidate_manifest, candidate_root
    if operation_id is not None:
        ready_path = runtime_root / "state/prepared" / operation_id / "ready.json"
        ready = _load_private_json(ready_path)
        if (
            ready.get("schema_version") != 1
            or ready.get("status") != "ready"
            or ready.get("operation_id") != operation_id
        ):
            raise ControlRuntimeError("prepared control operation identity differs")
        candidate = ready.get("executor_control")
        if (
            not isinstance(candidate, dict)
            or candidate.get("operation_id") != operation_id
            or canonical_json_digest(candidate)
            != ready.get("executor_control_sha256")
        ):
            raise ControlRuntimeError("prepared control identity differs")
        _record, candidate_manifest, candidate_root = load_candidate_control(
            runtime_root, candidate
        )
        return candidate_manifest, candidate_root
    return manifest, root


def _exec_role(
    role: str,
    arguments: list[str],
    environment: Mapping[str, str],
    *,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> None:
    if SAFE_ROLE_RE.fullmatch(role) is None:
        raise ControlRuntimeError("control selector role is invalid")
    runtime_root = runtime_root.absolute()
    manifest, release = _selected_release(runtime_root, role, arguments)
    entrypoint = manifest["entrypoints"].get(role)
    if entrypoint is None:
        raise ControlRuntimeError("active control release does not provide this role")
    allowed = {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    clean_environment = {
        key: value for key, value in environment.items() if key in allowed
    }
    clean_environment.update(
        {
            "HOME": "/home/devuser",
            "USER": "devuser",
            "LOGNAME": "devuser",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    clean_environment["NEXPOLY_ACTIVE_CONTROL_ROOT"] = str(release)
    clean_environment["NEXPOLY_ACTIVE_CONTROL_RELEASE_ID"] = manifest["release_id"]
    if role == "reconcile-production-0005-alias":
        dsn = environment.get("NEXPOLY_PRODUCTION_POSTGRES_DSN")
        if (
            not isinstance(dsn, str)
            or not dsn
            or len(dsn) > 8192
            or any(character in dsn for character in ("\x00", "\r", "\n"))
        ):
            raise ControlRuntimeError(
                "production PostgreSQL DSN is unavailable or malformed"
            )
        clean_environment["NEXPOLY_PRODUCTION_POSTGRES_DSN"] = dsn
    python = "/usr/bin/python3"
    if entrypoint["kind"] == "python":
        target = release / entrypoint["file"]
        argv = [python, "-I", "-B", str(target), *arguments]
    else:
        environment_loader = release / entrypoint["environment_loader"]
        launcher = release / entrypoint["launcher"]
        config = runtime_root / entrypoint["config_relative"]
        argv = [
            python,
            "-I",
            "-B",
            str(environment_loader),
            "exec",
            str(config),
            "--",
            python,
            "-I",
            "-B",
            str(launcher),
            *arguments,
        ]
    os.execve(python, argv, clean_environment)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 2 or values[0] != "run":
        print("control-runtime-selector: usage: run <role> [arguments...]", file=sys.stderr)
        return 2
    try:
        _exec_role(values[1], values[2:], os.environ)
    except (ControlRuntimeError, OSError, ValueError) as exc:
        print(f"control-runtime-selector: error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
