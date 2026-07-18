from __future__ import annotations

import importlib.util
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.tests.test_postgres_media_evidence import (
    external_inventory_fixture as external_inventory_v3_fixture,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "pull_deploy_controller.py"
SPEC = importlib.util.spec_from_file_location("pull_deploy_controller", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CONTROLLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROLLER)


PREVIOUS_SHA = "1" * 40
PREVIOUS_TREE = "2" * 40
TARGET_SHA = "3" * 40
TARGET_TREE = "4" * 40
OPERATION_ID = "deploy-20260716-0001"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
B_MANIFEST_PAYLOAD = (
    REPOSITORY_ROOT / "backend/migrations/postgres/manifest.json"
).read_bytes()
B_MANIFEST_RECORDS = json.loads(B_MANIFEST_PAYLOAD)["migrations"]
B_MANIFEST_DIGEST = CONTROLLER.sha256_bytes(B_MANIFEST_PAYLOAD)
F_MANIFEST_DIGEST = "sha256:" + "e" * 64


def write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def image_record(role: str, sha: str = TARGET_SHA) -> dict[str, str]:
    root = CONTROLLER.BACKEND_TAG_ROOT if role == "backend" else CONTROLLER.WEB_TAG_ROOT
    return {
        "tag": f"{root}:sha-{sha}",
        "digest_ref": f"{root}@{DIGEST_A if role == 'backend' else DIGEST_B}",
        "image_id": "sha256:" + ("c" if role == "backend" else "d") * 64,
        "revision": sha,
        "source": CONTROLLER.SOURCE_URL,
        "version": f"sha-{sha}",
    }


def mutable_data_evidence(
    *,
    ledger_length: int = 11,
    operation_id: str = OPERATION_ID,
) -> dict[str, object]:
    def table_record(
        schema: str,
        table: str,
        index: int,
        *,
        present: bool = True,
        rows: int | None = None,
    ) -> dict[str, object]:
        return {
            "schema": schema,
            "table": table,
            "state": "present" if present else "absent",
            "row_count": (index + 1 if rows is None else rows) if present else None,
            "schema_sha256": (
                "sha256:" + f"{(index + 1) % 16:x}" * 64
                if present
                else None
            ),
            "content_sha256": (
                "sha256:" + f"{(index + 9) % 16:x}" * 64
                if present
                else None
            ),
        }

    dft_ready = ledger_length == 13
    contract_applied = ledger_length >= 12
    controls_ready = ledger_length >= 10
    business_tables = [
        table_record(schema, table, index)
        for index, (schema, table) in enumerate(
            CONTROLLER._site_helper_contracts.BUSINESS_MUTABLE_TABLES
        )
    ]
    business_tables.extend(
        table_record(
            schema,
            table,
            index + len(business_tables),
            present=dft_ready,
            rows=0,
        )
        for index, (schema, table) in enumerate(
            CONTROLLER._site_helper_contracts.POST_0013_BUSINESS_MUTABLE_TABLES
        )
    )
    if dft_ready:
        for record in business_tables:
            relation = (record["schema"], record["table"])
            expected_schema = (
                CONTROLLER._site_helper_contracts
                .MONOMER_DFT_TABLE_SCHEMA_SHA256.get(relation)
            )
            if expected_schema is not None:
                record["schema_sha256"] = expected_schema
                record["content_sha256"] = (
                    CONTROLLER._site_helper_contracts
                    .EMPTY_POSTGRES_COPY_SHA256
                )
    static_tables = [
        table_record(schema, table, index + 8)
        for index, (schema, table) in enumerate(
            CONTROLLER._site_helper_contracts.STATIC_IMPORT_TABLES
        )
    ]
    deployment_table = table_record(
        "governance",
        "deployment_control",
        7,
        present=controls_ready,
        rows=1,
    )
    analytics_table = table_record(
        "governance",
        "database_analytics_snapshots",
        8,
        present=controls_ready,
        rows=0,
    )
    sequences: list[dict[str, object]] = []
    for index, ((schema, sequence, _owned_by), owner) in enumerate(
        zip(
            CONTROLLER._site_helper_contracts.DATA_SEQUENCES,
            CONTROLLER._site_helper_contracts.DATA_SEQUENCE_OWNERSHIP,
            strict=True,
        )
    ):
        present = schema != "monomer_dft" or dft_ready
        sequences.append(
            {
                "schema": schema,
                "sequence": sequence,
                "ownership": (
                    {
                        "schema": owner[0],
                        "table": owner[1],
                        "column": owner[2],
                        "ordinal": owner[3],
                        "deptype": owner[4],
                    }
                    if present
                    else None
                ),
                "state": "present" if present else "absent",
                "data_type": "bigint" if present else None,
                "start_value": 1 if present else None,
                "min_value": 1 if present else None,
                "max_value": 9223372036854775807 if present else None,
                "increment_by": 1 if present else None,
                "cache_size": 1 if present else None,
                "cycle": False if present else None,
                "last_value": 1 if present else None,
                "is_called": (
                    False
                    if present and schema == "monomer_dft"
                    else (True if present else None)
                ),
            }
        )
    identity = {
        "operation_id": operation_id,
        "database": "nexpoly",
        "database_system_identifier": "7659245354718314530",
        "connection": {
            "service": CONTROLLER.MUTABLE_DATA_SERVICE,
            "host": CONTROLLER.MUTABLE_DATA_HOST,
            "port": CONTROLLER.MUTABLE_DATA_PORT,
            "database": CONTROLLER.MUTABLE_DATA_DATABASE,
            "user": CONTROLLER.MUTABLE_DATA_USER,
        },
        "postgres_runtime": {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "configured_image": "postgres:16-alpine",
            "data_volume": {
                "type": "volume",
                "name": "nexpoly_postgres_data",
                "source": (
                    "/var/lib/docker/volumes/nexpoly_postgres_data/_data"
                ),
                "destination": "/var/lib/postgresql/data",
                "driver": "local",
                "read_write": True,
            },
            "host_endpoint": {
                "host": CONTROLLER.MUTABLE_DATA_HOST,
                "port": CONTROLLER.MUTABLE_DATA_PORT,
                "container_port": 5432,
                "protocol": "tcp",
            },
            "system_identifier": "7659245354718314530",
        },
        "role_security": {
            "role": "nexpoly_mutable_audit",
            "can_login": True,
            "superuser": False,
            "create_db": False,
            "create_role": False,
            "inherit": True,
            "replication": False,
            "bypass_rls": False,
            "role_settings": [
                {
                    "database": "*",
                    "settings": ["default_transaction_read_only=on"],
                }
            ],
            "direct_memberships": [
                {
                    "role": "pg_read_all_data",
                    "admin_option": False,
                    "inherit_option": True,
                    "set_option": True,
                }
            ],
            "effective_memberships": ["pg_read_all_data"],
            "has_pg_read_all_data": True,
            "has_pg_write_all_data": False,
            "owned_objects": [],
            "direct_write_grants": [],
            "effective_write_privileges": [],
        },
        "digest_algorithm": "sha256-postgres-jsonb-copy-v4",
        "migration_ledger": [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER[
                    :ledger_length
                ]
            )
        ],
        "business_tables": business_tables,
        "governed_controls": {
            "deployment_control": {
                "table": deployment_table,
                "row": (
                    {
                        "control_key": "production",
                        "drain_enabled": True,
                        "reason": f"pull deployment {operation_id}",
                        "release_sha": TARGET_SHA,
                        "activated_at": "2026-07-17T00:00:00Z",
                        "activated_by": "pull-deploy-controller",
                        "updated_at": "2026-07-17T00:00:00Z",
                    }
                    if controls_ready
                    else None
                ),
            },
            "database_analytics_snapshots": {
                "table": analytics_table,
                "entries": [],
            },
        },
        "static_tables": static_tables,
        "migration_exception": table_record(
            "generation",
            "polytao_jobs",
            23,
            present=not contract_applied,
            rows=9,
        ),
        "sequences": sequences,
        "bridge_projection": {
            "schema": "md",
            "table": "monomer_md_jobs",
            "projection": "pre-0009-row-json-v1",
            "state": "present",
            "row_count": next(
                record["row_count"]
                for record in business_tables
                if record["schema"] == "md"
                and record["table"] == "monomer_md_jobs"
            ),
            "content_sha256": "sha256:" + "f" * 64,
            "lease_columns": {
                "state": "present" if ledger_length >= 9 else "absent",
                "non_null_counts": {
                    "worker_instance_id": (
                        0 if ledger_length >= 9 else None
                    ),
                    "heartbeat_at": 0 if ledger_length >= 9 else None,
                    "lease_expires_at": (
                        0 if ledger_length >= 9 else None
                    ),
                },
            },
        },
    }
    return {
        "schema_version": 5,
        **identity,
        "transaction_isolation": "repeatable read",
        "transaction_read_only": True,
        "transaction_deferrable": True,
        "snapshot_sha256": CONTROLLER.canonical_json_digest(identity),
        "captured_at": "2026-07-17T00:00:00Z",
    }


def reseal_mutable_data_evidence(
    document: dict[str, object],
) -> dict[str, object]:
    identity_fields = (
        "operation_id",
        "database",
        "database_system_identifier",
        "connection",
        "postgres_runtime",
        "role_security",
        "digest_algorithm",
        "migration_ledger",
        "business_tables",
        "governed_controls",
        "static_tables",
        "migration_exception",
        "sequences",
        "bridge_projection",
    )
    document["snapshot_sha256"] = CONTROLLER.canonical_json_digest(
        {name: document[name] for name in identity_fields}
    )
    return document


def _rewrite_v3_media_ledger(
    snapshot: dict[str, object],
    *,
    database: str,
    ledger: list[dict[str, str]],
    legacy_relation_present: bool,
) -> None:
    record = next(
        value
        for value in snapshot["media"]
        if value.get("database") == database
        and value.get("record_type") == "nexpoly-db"
        and value.get("disposition") != "retained-private-isolated"
    )
    analysis, migration_0013, requires_0014 = (
        CONTROLLER._site_helper_contracts._external_media_ledger_v2(
            ledger,
            legacy_relation_present=legacy_relation_present,
            isolated=False,
        )
    )
    ledger_digest = CONTROLLER.canonical_json_digest(ledger)
    ledger_relation = record["ledger_relation"]
    ledger_relation.update(
        {
            "state": "present",
            "row_count": len(ledger),
            "content_sha256": ledger_digest,
        }
    )
    legacy_relation = record["legacy_relation"]
    if legacy_relation_present:
        if legacy_relation.get("schema_authority") is None:
            raise AssertionError(
                "fixture cannot recreate a dropped legacy schema authority"
            )
        legacy_relation.update(
            {
                "state": "present",
                "row_count": 9,
            }
        )
    else:
        legacy_relation.update(
            {
                "state": "absent",
                "row_count": None,
                "schema_sha256": None,
                "schema_authority": None,
                "content_sha256": None,
            }
        )
    primary = {
        "database_identity": record["database_identity"],
        "database_identity_sha256": record[
            "database_identity_sha256"
        ],
        "current_user": record["current_user"],
        "transaction_read_only": record["transaction_read_only"],
        "role_superuser": record["role_superuser"],
        "role_create_db": record["role_create_db"],
        "role_create_role": record["role_create_role"],
        "ledger": ledger,
        "ledger_sha256": ledger_digest,
        "ledger_relation": ledger_relation,
        "ledger_analysis": analysis,
        "legacy_relation_present": legacy_relation_present,
        "legacy_relation": legacy_relation,
        "migration_0013": migration_0013,
    }
    record.update(primary)
    database_record = next(
        value
        for value in record["databases"]
        if value["name"] == database
    )
    database_record["audit"] = {
        **json.loads(json.dumps(primary)),
        "requires_0014": requires_0014,
    }
    record["source_content_sha256"] = (
        CONTROLLER.canonical_json_digest(
            {
                "database_inventory": record["database_inventory"],
                "databases": record["databases"],
            }
        )
    )
    for projection in snapshot["databases"]:
        if projection["database"] == database:
            projection.update(
                {
                    "ledger": ledger,
                    "ledger_sha256": ledger_digest,
                    "legacy_relation_present": legacy_relation_present,
                }
            )
    snapshot["requires_0014"] = any(
        value["audit"].get("requires_0014", False)
        for medium in snapshot["media"]
        if medium.get("record_type") == "nexpoly-db"
        for value in medium["databases"]
    )


def _reseal_v3_media_runtime(
    snapshot: dict[str, object],
    *,
    captured_at: str,
) -> None:
    snapshot["media_registry"]["captured_at"] = captured_at
    for record in snapshot["media"]:
        audit = record["audit"]
        audit["audited_at"] = captured_at
        audit["postgres_uid"] = 70
        audit["postgres_gid"] = 70
        audit.pop("evidence_sha256", None)
        audit["evidence_sha256"] = CONTROLLER.canonical_json_digest(
            record
        )


def external_database_audit_binding(
    runtime: Path,
    *,
    captured_at: str = "2026-07-17T00:00:00Z",
) -> dict[str, object]:
    helper_path = (
        runtime / "config" / CONTROLLER.EXTERNAL_DATABASE_AUDIT_HELPER
    )
    authority_rules_path = (
        runtime
        / "config"
        / CONTROLLER.EXTERNAL_DATABASE_MEDIA_AUTHORITY_RULES
    )
    registry_path = (
        runtime
        / "config"
        / CONTROLLER.EXTERNAL_DATABASE_MEDIA_REGISTRY
    )
    helper_sha256 = (
        CONTROLLER.sha256_file(helper_path)
        if helper_path.exists()
        else "sha256:" + "8" * 64
    )
    authority_rules_sha256 = (
        CONTROLLER.sha256_file(authority_rules_path)
        if authority_rules_path.exists()
        else "sha256:" + "4" * 64
    )
    registry_sha256 = (
        CONTROLLER.sha256_file(registry_path)
        if registry_path.exists()
        else "sha256:" + "5" * 64
    )
    ledger = [
        {"version": version, "checksum": checksum}
        for version, checksum in (
            CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER
        )
    ]
    through_0011 = [
        row
        for row in ledger
        if row["version"] <= "0011_monomer_md_demo_steps"
    ]
    through_0012 = [
        row
        for row in ledger
        if row["version"] <= "0012_drop_polytao_jobs"
    ]
    through_0008 = [
        row
        for row in ledger
        if row["version"] <= "0008_polytao_backend_runtime"
    ]
    snapshot = external_inventory_v3_fixture(
        dev_ledger=through_0012,
        # The reusable builder uses this ledger for both production and
        # health; rewrite health below while retaining production at 0011.
        health_ledger=through_0011,
        registry_digest=registry_sha256,
        dev_user="nexpoly_dev_auditor",
        health_user="nexpoly_health_auditor",
    )
    snapshot["schema_version"] = 4
    snapshot["media_registry"]["schema_version"] = 4
    snapshot["media_registry"].pop("sha256", None)
    snapshot["media_registry"]["media_authority_rules_sha256"] = (
        authority_rules_sha256
    )
    snapshot["media_registry"]["runtime_registry_sha256"] = (
        registry_sha256
    )
    _rewrite_v3_media_ledger(
        snapshot,
        database="nexpoly_md_health_opt",
        ledger=through_0008,
        legacy_relation_present=True,
    )
    _reseal_v3_media_runtime(snapshot, captured_at=captured_at)
    binding: dict[str, object] = {
        "schema_version": 2,
        "helper": {
            "path": str(helper_path),
            "sha256": helper_sha256,
            "mode": "0700",
        },
        "authority_rules": {
            "path": str(authority_rules_path),
            "sha256": authority_rules_sha256,
            "mode": "0600",
        },
        "registry": {
            "path": str(registry_path),
            "sha256": registry_sha256,
            "mode": "0600",
            "authority_rules_sha256": authority_rules_sha256,
        },
        "expected_users": {
            "nexpoly_dev": "nexpoly_dev_auditor",
            "nexpoly_md_health_opt": "nexpoly_health_auditor",
        },
        "snapshot": snapshot,
        "snapshot_sha256": CONTROLLER.canonical_json_digest(snapshot),
        "state_sha256": CONTROLLER.canonical_json_digest(
            CONTROLLER.external_database_audit_state(snapshot)
        ),
        "identity_sha256": None,
    }
    binding["identity_sha256"] = CONTROLLER.canonical_json_digest(
        {
            key: value
            for key, value in binding.items()
            if key != "identity_sha256"
        }
    )
    return binding


def reseal_external_database_audit_binding(
    binding: dict[str, object],
) -> dict[str, object]:
    snapshot = binding["snapshot"]
    binding["snapshot_sha256"] = CONTROLLER.canonical_json_digest(snapshot)
    binding["state_sha256"] = CONTROLLER.canonical_json_digest(
        CONTROLLER.external_database_audit_state(snapshot)
    )
    binding["identity_sha256"] = CONTROLLER.canonical_json_digest(
        {
            key: value
            for key, value in binding.items()
            if key != "identity_sha256"
        }
    )
    return binding


def external_database_binding_state(
    binding: dict[str, object],
    *,
    production_ledger: list[dict[str, str]],
    legacy_relation_present: bool,
    captured_at: str,
) -> dict[str, object]:
    changed = json.loads(json.dumps(binding))
    snapshot = changed["snapshot"]
    _rewrite_v3_media_ledger(
        snapshot,
        database="nexpoly",
        ledger=production_ledger,
        legacy_relation_present=legacy_relation_present,
    )
    _reseal_v3_media_runtime(snapshot, captured_at=captured_at)
    return reseal_external_database_audit_binding(changed)


def seed_completed_alias_gate(
    runtime: Path, manifest: dict[str, object], control_root: Path
) -> None:
    selector = CONTROLLER._control_runtime
    operation_id = "alias-0005-fixture"
    audit_dir = runtime / selector.ALIAS_AUDIT_ROOT_RELATIVE / operation_id
    backup_dir = runtime / selector.ALIAS_BACKUP_ROOT_RELATIVE / operation_id
    for directory in (audit_dir, backup_dir):
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    dump = backup_dir / "nexpoly-before.dump"
    write_private(dump, "fixture database dump\n")
    dump_sha = selector.sha256_file(dump).removeprefix("sha256:")
    write_private(backup_dir / "nexpoly-before.dump.sha256", dump_sha + "\n")
    restore_list = audit_dir / "pg-restore.list"
    write_private(
        restore_list,
        "TABLE DATA generation polytao_jobs\n"
        "TABLE DATA governance schema_migrations\n",
    )
    def ledger_rows(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [
            {
                "version": version,
                "checksum": checksum,
                "applied_at": (
                    selector.ALIAS_APPLIED_AT
                    if version == selector.ALIAS_VERSION
                    else f"2026-07-08T02:{index:02d}:00.000000Z"
                ),
            }
            for index, (version, checksum) in enumerate(pairs)
        ]

    archive = {
        "row_count": 12,
        "status_counts": {"completed": 8, "failed": 4},
        "rows_sha256": "c" * 64,
        "schema_sha256": selector.ALIAS_EXPECTED_SCHEMA_SHA256,
        "structure_counts": selector.ALIAS_EXPECTED_STRUCTURE_COUNTS,
    }
    relation = {
        "kind": "r",
        "persistence": "p",
        "is_partition": False,
        "row_security": False,
        "force_row_security": False,
        "owner": "polyprop",
        "parents": 0,
        "children": 0,
    }
    before = {
        "database": "nexpoly",
        "current_user": "polyprop",
        "database_owner": "polyprop",
        "server_version_num": 160014,
        "in_recovery": False,
        "system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
        "ledger": ledger_rows(selector.ALIAS_PRE_LEDGER),
        "archive": archive,
        "ledger_schema_sha256": selector.ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256,
        "ledger_structure_counts": selector.ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS,
        "polytao_relation": relation,
        "ledger_relation": relation,
    }
    after = {
        **before,
        "ledger": [
            row
            for row in before["ledger"]
            if row["version"] != selector.ALIAS_VERSION
        ],
    }
    restored = {
        **before,
        "database": "nexpoly_alias_restore",
        "current_user": "postgres",
        "database_owner": "postgres",
        "system_identifier": "123456789",
        "polytao_relation": {**relation, "owner": "postgres"},
        "ledger_relation": {**relation, "owner": "postgres"},
    }
    entrypoint = manifest["entrypoints"]["reconcile-production-0005-alias"]
    control = {
        "release_id": manifest["release_id"],
        "source_sha": manifest["source_sha"],
        "source_tree": manifest["source_tree"],
        "manifest_sha256": selector.sha256_file(
            control_root / selector.CONTROL_MANIFEST_NAME
        ).removeprefix("sha256:"),
        "script_sha256": selector.sha256_file(
            control_root / entrypoint["file"]
        ).removeprefix("sha256:"),
    }
    identity = {
        "operation_id": operation_id,
        "control": control,
        "legacy_source": {"sha": PREVIOUS_SHA, "tree": PREVIOUS_TREE},
        "binaries_sha256": {"/fixture/bin": "b" * 64},
        "database_endpoint": selector.ALIAS_DATABASE_ENDPOINT,
        "database_system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
        "restore_image": {
            "digest_ref": selector.ALIAS_RESTORE_IMAGE,
            "image_id": "sha256:" + "d" * 64,
        },
        "alias": {
            "version": selector.ALIAS_VERSION,
            "checksum": selector.ALIAS_CHECKSUM,
            "applied_at": selector.ALIAS_APPLIED_AT,
        },
    }
    backup = {
        "dump_path": str(dump),
        "dump_sha256": dump_sha,
        "dump_size": dump.stat().st_size,
        "restore_list_sha256": selector.sha256_file(restore_list).removeprefix(
            "sha256:"
        ),
    }
    restore = {
        "image": {
            "digest_ref": selector.ALIAS_RESTORE_IMAGE,
            "image_id": "sha256:" + "d" * 64,
        },
        "container_name": "nexpoly-alias-restore-fixture",
        "network_mode": "none",
        "dump_sha256": dump_sha,
        "archive": before["archive"],
        "ledger_schema_sha256": before["ledger_schema_sha256"],
        "database_inventory": restored,
        "verified_at": "2026-07-17T00:00:00Z",
    }
    CONTROLLER.atomic_json(audit_dir / "isolated-postgres16-restore.json", restore)
    CONTROLLER.atomic_json(audit_dir / "database-after.json", after)
    external_transition_path = (
        audit_dir / "external-database-alias-transition.json"
    )
    CONTROLLER.atomic_json(
        external_transition_path,
        {"schema_version": 1, "fixture": True},
    )
    external_transition = {
        "path": str(external_transition_path),
        "sha256": selector.sha256_file(external_transition_path),
        "identity_sha256": "sha256:" + "1" * 64,
        "before_state_sha256": "sha256:" + "2" * 64,
        "after_state_sha256": "sha256:" + "3" * 64,
        "descriptor_sha256": "sha256:" + "4" * 64,
        "operation_id": operation_id,
        "kind": "alias-0005-reconciliation",
    }
    files = selector._alias_evidence_files(audit_dir, backup_dir)
    completed_at = "2026-07-17T00:00:01Z"
    runtime_stop_fence = {"fixture": True}
    audit = {
        "schema_version": 1,
        "operation_id": operation_id,
        "outcome": "completed",
        "identity": identity,
        "database_before": before,
        "database_after": after,
        "database_backup": backup,
        "isolated_restore": restore,
        "runtime_stop_fence": runtime_stop_fence,
        "runtime_stop_fence_sha256": selector.canonical_json_digest(
            runtime_stop_fence
        ).removeprefix("sha256:"),
        "external_database_alias_transition": external_transition,
        "binaries": {"/fixture/bin": {"sha256": "b" * 64}},
        "files": files,
        "completed_at": completed_at,
    }
    audit_path = audit_dir / "AUDIT-MANIFEST.json"
    CONTROLLER.atomic_json(audit_path, audit)
    marker = {
        "schema_version": 1,
        "action": selector.ALIAS_ACTION,
        "phase": "completed",
        "identity": identity,
        "operation_directories": {
            "audit": str(audit_dir),
            "backup": str(backup_dir),
        },
        "started_at": "2026-07-17T00:00:00Z",
        "updated_at": completed_at,
        "runtime_stop_fence": runtime_stop_fence,
        "before": before,
        "database_backup": backup,
        "restore_container": {"name": "fixture"},
        "isolated_restore": restore,
        "mutation_intent": {
            "database_system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
            "alias": identity["alias"],
            "pre_ledger": before["ledger"],
            "archive": before["archive"],
            "dump_sha256": dump_sha,
            "restore_dump_sha256": dump_sha,
        },
        "after": after,
        "external_database_alias_transition": external_transition,
        "audit_manifest_sha256": selector.sha256_file(audit_path).removeprefix(
            "sha256:"
        ),
        "completed_at": completed_at,
    }
    marker_path = runtime / selector.ALIAS_MARKER_RELATIVE
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(marker_path.parent, 0o700)
    CONTROLLER.atomic_json(marker_path, marker)


class GitRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[0] != "git":
            raise AssertionError(command)
        index = 1
        while index + 1 < len(command) and command[index] == "-c":
            index += 2
        arguments = command[index:]
        output = ""
        returncode = 0
        if arguments == ["symbolic-ref", "--short", "HEAD"]:
            output = "main\n"
        elif arguments == ["status", "--porcelain=v1", "--untracked-files=all"]:
            output = ""
        elif arguments == ["ls-files", "-z", "--cached"]:
            output = ""
        elif arguments == [
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ]:
            output = ""
        elif arguments == ["remote", "get-url", "origin"]:
            output = CONTROLLER.REPOSITORY_HTTPS_URL + "\n"
        elif arguments == ["rev-parse", "HEAD"]:
            output = PREVIOUS_SHA + "\n"
        elif arguments == ["rev-parse", "HEAD^{tree}"]:
            output = PREVIOUS_TREE + "\n"
        elif arguments == [
            "ls-remote",
            "--exit-code",
            CONTROLLER.REPOSITORY_SSH_URL,
            "refs/heads/main",
        ]:
            output = f"{TARGET_SHA}\trefs/heads/main\n"
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(command, returncode, output, "")

    def request_json(self, _url: str, _token: str) -> dict[str, object]:
        raise AssertionError("unexpected network request")


class GithubRunner:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def run(
        self, *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("unexpected subprocess")

    def request_json(self, url: str, _token: str) -> dict[str, object]:
        self.urls.append(url)
        if "/jobs?" in url:
            return {
                "jobs": [
                    {"name": name, "conclusion": "success"}
                    for name in sorted(
                        CONTROLLER._bridge_core.REQUIRED_CI_JOBS
                    )
                ]
            }
        return {
            "workflow_runs": [
                {
                    "id": 42,
                    "run_attempt": 2,
                    "head_sha": TARGET_SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                }
            ]
        }


class ImageRunner:
    def __init__(self, *, wrong_revision: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.wrong_revision = wrong_revision

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            tag = command[3]
            root = tag.split(":sha-", 1)[0].split("@", 1)[0]
            sha = TARGET_SHA if not self.wrong_revision else PREVIOUS_SHA
            output = json.dumps(
                [
                    {
                        "Id": "sha256:" + "9" * 64,
                        "RepoDigests": [f"{root}@{DIGEST_A}"],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": sha,
                                "org.opencontainers.image.source": CONTROLLER.SOURCE_URL,
                                "org.opencontainers.image.version": f"sha-{TARGET_SHA}",
                            }
                        },
                    }
                ]
            )
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    def request_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("unexpected request")


class FakeLifecycle:
    def __init__(
        self, *, fail_at: str | None = None, admission_open: bool = False
    ) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        self.admission_open = admission_open
        self.recovery_fence: dict[str, object] = {"fixture_instance": "instance-1"}
        self.runtime_state = "live"

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise CONTROLLER.PullDeployError(f"injected {name} failure")

    def postgres_runtime_identity(
        self, _controller: object, _descriptor: object
    ) -> dict[str, object]:
        runtime = dict(mutable_data_evidence()["postgres_runtime"])
        return {
            "schema_version": 1,
            **runtime,
            "captured_at": CONTROLLER.utc_now(),
        }

    def drain(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("drain")
        self.admission_open = False
        return {"active_total": 0}

    def ensure_candidate_drained(
        self, _controller: object, _descriptor: object
    ) -> dict[str, object]:
        self._event("ensure-candidate-drained")
        self.admission_open = False
        return {"active_total": 0}

    def backup(
        self, controller: object, descriptor: dict[str, object]
    ) -> dict[str, object]:
        self._event("backup")
        backup_root = Path(getattr(controller, "backups_dir"))
        directory = backup_root / str(descriptor["operation_id"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        dump = directory / "database.dump"
        if not dump.exists():
            dump.write_bytes(b"fixture database dump\n")
            os.chmod(dump, 0o600)
        digest = CONTROLLER.sha256_file(dump)
        return {
            "path": str(dump),
            "sha256": digest,
            "restore_verification": {
                "schema_version": 1,
                "restored": True,
                "postgres_major": 16,
                "image": CONTROLLER.POSTGRES16_IMAGE,
                "dump_sha256": digest,
                "source_mutable_data_identity_sha256": (
                    CONTROLLER.canonical_json_digest(
                        CONTROLLER.mutable_data_identity(
                            mutable_data_evidence(
                                operation_id=str(
                                    descriptor["operation_id"]
                                )
                            )
                        )
                    )
                ),
                "ledger": [
                    {
                        "version": record["version"],
                        "checksum": record["checksum"],
                    }
                    for record in B_MANIFEST_RECORDS[:11]
                ],
            },
        }

    def backup_rollback(
        self,
        controller: object,
        descriptor: dict[str, object],
        backup_operation_id: str,
    ) -> dict[str, object]:
        projected = dict(descriptor)
        projected["operation_id"] = backup_operation_id
        backup = self.backup(controller, projected)
        marker = CONTROLLER.load_private_json(
            Path(getattr(controller, "marker_path"))
        )
        before = marker["mutable_data_before"]
        backup["restore_verification"][
            "source_mutable_data_identity_sha256"
        ] = CONTROLLER.canonical_json_digest(
            CONTROLLER.mutable_data_identity(before)
        )
        return backup

    def stop(self, _controller: object, _descriptor: object) -> None:
        self._event("stop")
        self.runtime_state = "stopped"

    def restore_database(
        self, _controller: object, _descriptor: object, backup: dict[str, object]
    ) -> dict[str, object]:
        self._event("restore_database")
        return {
            "restored": True,
            "dump_sha256": backup["sha256"],
            "ledger": backup["restore_verification"].get("ledger", []),
        }

    def migrate(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("migrate")
        return {
            "newly_applied": [
                record["version"] for record in B_MANIFEST_RECORDS[:11]
            ],
            "ledger": json.loads(json.dumps(B_MANIFEST_RECORDS[:11])),
        }

    def start(self, _controller: object, _descriptor: object) -> None:
        self._event("start")
        self.runtime_state = "live"
        self.admission_open = False

    def verification(self) -> dict[str, object]:
        return {
            "health": "ok",
            "recovery_fence": dict(self.recovery_fence),
        }

    def verify(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("verify")
        return self.verification()

    def resume(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
    ) -> None:
        self._event("resume")
        if expected_verification != self.verification():
            raise CONTROLLER.PullDeployError(
                "resumed runtime instance differs from committed verification"
            )
        self.admission_open = True

    def resume_unchanged(
        self,
        _controller: object,
        _descriptor: object,
        persist_verification,  # type: ignore[no-untyped-def]
        expected_verification=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._event("resume-unchanged")
        if (
            expected_verification is not None
            and expected_verification != self.verification()
        ):
            raise CONTROLLER.PullDeployError(
                "unchanged runtime differs from committed verification"
            )
        persist_verification(self.verification())
        self.admission_open = True

    def resume_bootstrap_unchanged(
        self, _controller: object, _descriptor: object
    ) -> None:
        self._event("resume-bootstrap-unchanged")
        self.admission_open = True

    def bootstrap_can_resume_unchanged(
        self, _controller: object, _descriptor: object
    ) -> bool:
        self._event("bootstrap-admission-status")
        return self.admission_open

    def admission_is_open(self, _controller: object, _descriptor: object) -> bool:
        self._event("admission-status")
        return self.admission_open

    def verify_open_runtime(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
    ) -> None:
        self._event("verify-open")
        if expected_verification != self.verification():
            raise CONTROLLER.PullDeployError(
                "open runtime instance differs from committed verification"
            )

    def prepare_recovery_runtime(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
        *,
        allow_unfenced: bool,
    ) -> dict[str, object]:
        self._event("recovery-isolate")
        if self.runtime_state == "partial":
            raise CONTROLLER.PullDeployError(
                "runtime is partially stopped during recovery"
            )
        if self.runtime_state == "stopped":
            return {
                "runtime_state": "stopped",
                "ingress_isolated": True,
            }
        if expected_verification is not None:
            if expected_verification != self.verification():
                raise CONTROLLER.PullDeployError(
                    "recovery runtime instance differs from committed verification"
                )
        elif not allow_unfenced:
            raise CONTROLLER.PullDeployError(
                "runtime recovery lacks committed verification evidence"
            )
        self._event("recovery-redrain")
        self.admission_open = False
        return {
            "runtime_state": "drained",
            "ingress_isolated": True,
            "drain": {"active_total": 0},
            "verification": self.verification(),
        }


class LostResumeLifecycle(FakeLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_resume = False

    def resume(
        self,
        controller: object,
        descriptor: object,
        expected_verification: object,
    ) -> None:
        marker = CONTROLLER.load_private_json(getattr(controller, "marker_path"))
        if marker.get("verification") != expected_verification:
            raise AssertionError("runtime fence was not durable before resume")
        super().resume(controller, descriptor, expected_verification)
        if self.lose_next_resume:
            self.lose_next_resume = False
            raise CONTROLLER.PullDeployError(
                "injected lost response after admission commit"
            )


class LostUnchangedResumeLifecycle(FakeLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_unchanged_resume = False

    def resume_unchanged(
        self,
        controller: object,
        descriptor: object,
        persist_verification,  # type: ignore[no-untyped-def]
        expected_verification=None,  # type: ignore[no-untyped-def]
    ) -> None:
        super().resume_unchanged(
            controller,
            descriptor,
            persist_verification,
            expected_verification,
        )
        marker = CONTROLLER.load_private_json(getattr(controller, "marker_path"))
        if marker.get("verification") != self.verification():
            raise AssertionError(
                "unchanged runtime fence was not durable before resume"
            )
        if self.lose_next_unchanged_resume:
            self.lose_next_unchanged_resume = False
            raise CONTROLLER.PullDeployError("injected lost unchanged-resume response")


class FixtureController(CONTROLLER.PullDeployController):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.source_sha = PREVIOUS_SHA
        self.source_tree = PREVIOUS_TREE
        self.rollback_called = False
        if self.active_control_path.exists():
            return
        candidate = super().prepare_control_release(
            operation_id="bootstrap-controls-fixture",
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
        )
        manifest, _root = CONTROLLER._control_runtime.load_control_release(
            self.runtime_root, candidate["release_id"]
        )
        active = {
            "schema_version": CONTROLLER._control_runtime.ACTIVE_CONTROL_SCHEMA_VERSION,
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "component": "deployment-controls",
            "generation": 1,
            "release_id": candidate["release_id"],
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "manifest_sha256": CONTROLLER.sha256_file(
                _root / CONTROLLER._control_runtime.CONTROL_MANIFEST_NAME
            ),
            "operation_id": "bootstrap-controls-fixture",
            "previous_release_id": None,
            "activated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(self.active_control_path, active)
        source_readiness = {
            "schema_version": 2,
            "ready": True,
            "source_root": str(
                self.runtime_root / "fixture-bootstrap-source"
            ),
            "source_sha": candidate["source_sha"],
            "source_tree": candidate["source_tree"],
            "branch": "main",
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
            "remote_names": ["origin"],
            "origin_fetch_urls": [CONTROLLER.REPOSITORY_SSH_URL],
            "origin_push_urls": [CONTROLLER.REPOSITORY_SSH_URL],
            "origin_main_sha": candidate["source_sha"],
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
        takeover = {
            "schema_version": 1,
            "operation_id": "takeover-pull-fixture",
            "authority_sha": candidate["source_sha"],
            "authority_tree": candidate["source_tree"],
            "install_manifest_sha256": "sha256:" + "3" * 64,
            "classification_sha256": "sha256:" + "4" * 64,
            "runtime_identity_sha256": "sha256:" + "5" * 64,
            "git_identity": {
                "branch": "refs/heads/main",
                "head_sha": PREVIOUS_SHA,
                "head_tree": PREVIOUS_TREE,
                "local_main_sha": PREVIOUS_SHA,
            },
            "pre_stopped_fence_sha256": "sha256:" + "6" * 64,
            "control_layout_sha256": "sha256:" + "7" * 64,
            "checkout_permissions_sha256": "sha256:" + "8" * 64,
            "applied_record_sha256": "sha256:" + "9" * 64,
        }
        takeover["binding_sha256"] = CONTROLLER.canonical_json_digest(
            takeover
        )
        CONTROLLER.atomic_json(
            self.state_dir / "bootstrap-control.json",
            {
                "schema_version": 2,
                "status": "completed",
                "source_sha": candidate["source_sha"],
                "source_tree": candidate["source_tree"],
                "source_readiness": source_readiness,
                "source_readiness_sha256": (
                    CONTROLLER.canonical_json_digest(source_readiness)
                ),
                "legacy_takeover": takeover,
                "delivery_gate": {
                    "remote_main": candidate["source_sha"],
                    "ci": {
                        "workflow_run_id": 42,
                        "run_attempt": 1,
                        "head_sha": candidate["source_sha"],
                        "head_branch": "main",
                        "event": "push",
                        "path": ".github/workflows/ci.yml",
                        "conclusion": "success",
                        "required_jobs": sorted(
                            CONTROLLER._bridge_core.REQUIRED_CI_JOBS
                        ),
                    },
                },
                "production_repository": {"fixture": True},
                "immutable_files": {
                    name: CONTROLLER.sha256_file(self.bin_dir / name)
                    for name in CONTROLLER.STABLE_HELPER_FILES
                },
                "worker_unit_takeover": {"fixture": True},
                "candidate_control": candidate,
                "active_control": active,
            },
        )
        seed_completed_alias_gate(self.runtime_root, manifest, _root)

    def _git_show(self, _target_sha: str, relative: str) -> bytes:
        return (REPOSITORY_ROOT / relative).read_bytes()

    def repository_identity(
        self, *, require_ssh_origin: bool = False
    ) -> dict[str, str]:
        return {
            "sha": self.source_sha,
            "tree": self.source_tree,
            "origin": (
                CONTROLLER.REPOSITORY_SSH_URL
                if require_ssh_origin
                else CONTROLLER.REPOSITORY_HTTPS_URL
            ),
        }

    def remote_main(self) -> str:
        return TARGET_SHA

    def fetch_target(self, target_sha: str, _operation_id: str) -> str:
        if target_sha != TARGET_SHA:
            raise AssertionError(target_sha)
        return TARGET_TREE

    def ci_evidence(self, target_sha: str) -> dict[str, object]:
        return {
            "workflow_run_id": 42,
            "run_attempt": 1,
            "head_sha": target_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": sorted(
                CONTROLLER._bridge_core.REQUIRED_CI_JOBS
            ),
        }

    def image_evidence(self, role: str, target_sha: str) -> dict[str, str]:
        return image_record(role, target_sha)

    def _revalidate_materialized_images(
        self,
        _images: object,
        *,
        source_sha: str,
        pull: bool,
    ) -> None:
        del source_sha, pull

    def postgres_restore_image_evidence(self) -> dict[str, str]:
        return {
            "digest_ref": CONTROLLER.POSTGRES16_IMAGE,
            "image_id": "sha256:" + "5" * 64,
        }

    def controller_digest(self) -> str:
        return CONTROLLER.sha256_file(SCRIPT)

    def stable_helper_evidence(self) -> dict[str, str]:
        return {name: "sha256:" + "d" * 64 for name in CONTROLLER.STABLE_HELPER_FILES}

    def validate_installed_controls_against_target(self, _target_sha: str) -> None:
        return

    def production_deploy_values(self, *, check_free_space: bool) -> dict[str, str]:
        return {"fixture": str(check_free_space)}

    def external_database_audit_evidence(
        self, policy: dict[str, object]
    ) -> dict[str, object]:
        return CONTROLLER.validate_external_database_audit_binding(
            external_database_audit_binding(self.runtime_root),
            expected_policy=policy,
        )

    def _capture_mutable_data(
        self, descriptor: dict[str, object]
    ) -> dict[str, object]:
        return mutable_data_evidence(
            operation_id=str(descriptor["operation_id"])
        )

    def asset_evidence(self, expected_digest: str) -> dict[str, object]:
        target = self.runtime_root / "fixture-assets" / expected_digest.split(":", 1)[1]
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        return {
            "pointer_path": str(self.state_dir / "current-assets"),
            "root": str(target),
            "manifest_sha256": expected_digest,
            "schema_version": 2,
            "byteff2_commit": "a" * 40,
            "inventory_sha256": "sha256:" + "b" * 64,
            "previous": None,
        }

    def prepare_worker_controls(
        self,
        *,
        operation_id: str,
        target_sha: str,
        executor_control: dict[str, object],
    ) -> dict[str, object]:
        operation = self.prepared_root / operation_id
        candidate = operation / CONTROLLER.MONOMER_MD_UNIT_NAME
        candidate.write_text("fixture unit\n", encoding="utf-8")
        os.chmod(candidate, 0o600)
        target = self.runtime_root / "config" / CONTROLLER.MONOMER_MD_UNIT_NAME
        worker_env = self.runtime_root / "config/worker.env"
        if not worker_env.exists():
            write_private(worker_env, "MONOMER_MD_WORKER_MODE=real\n")
        return {
            "worker_env": {
                "path": str(worker_env),
                "sha256": CONTROLLER.sha256_file(worker_env),
                "byteff2_python": "/opt/byteff2/bin/python",
                "byteff2_openmm_dir": "/opt/byteff2/openmm",
                "gmx_sha256": "sha256:" + "c" * 64,
            },
            "systemd_unit": {
                "source_path": CONTROLLER.MONOMER_MD_UNIT_SOURCE,
                "candidate_path": str(candidate),
                "target_path": str(target),
                "sha256": CONTROLLER.sha256_file(candidate),
                "previous_present": False,
                "previous_sha256": None,
                "previous_backup_path": None,
                "previous_unit_state": {
                    "LoadState": "not-found",
                    "FragmentPath": "",
                    "DropInPaths": "",
                    "NeedDaemonReload": "no",
                    "UnitFileState": "",
                },
                "control_release_id": executor_control["release_id"],
                "launcher_sha256": "sha256:" + "a" * 64,
            },
        }

    def _revalidate_worker_controls(self, _descriptor: object) -> None:
        return

    def _install_candidate_worker_unit(self, _descriptor: object) -> None:
        return

    def _restore_previous_worker_unit(self, _descriptor: object) -> None:
        return

    def _source_evidence(self, _target_sha: str):  # type: ignore[no-untyped-def]
        return (
            {
                "sha256": DIGEST_A,
                "schema_version": 2,
                "asset_manifest_digest": (
                    CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST
                ),
                "predecessor_asset_manifest_digest": (
                    CONTROLLER.SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
                ),
                "changed_asset_trees": ["byteff2"],
                "datasets_on_asset_change": [],
            },
            {
                "sha256": DIGEST_B,
                "schema_version": 2,
                "records": json.loads(json.dumps(B_MANIFEST_RECORDS[:11])),
            },
            {
                "sha256": "sha256:" + "6" * 64,
                "files": {
                    "docker-compose.yml": DIGEST_A,
                    "docker-compose.prod.yml": DIGEST_B,
                },
            },
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n",
        )

    def prepare_md_slot(
        self,
        *,
        operation_id: str,
        target_sha: str,
        target_tree: str,
        lock_payload: bytes,
    ) -> dict[str, object]:
        slot = self.choose_inactive_slot()
        self._remove_owned_slot(slot, operation_id)
        venv = self.venv_root / f"md-{slot}" / "venv"
        (venv / "bin").mkdir(parents=True, mode=0o700)
        (venv / "bin/python").write_text("fixture\n", encoding="utf-8")
        record = {
            "schema_version": CONTROLLER.SLOT_RECORD_SCHEMA_VERSION,
            "component": "monomer-md",
            "status": "ready",
            "slot": slot,
            "source_sha": target_sha,
            "source_tree": target_tree,
            "worker_lock_sha256": CONTROLLER.sha256_bytes(lock_payload),
            "requirements_sha256": CONTROLLER.sha256_bytes(lock_payload),
            "wheel_cache_key": "sha256:" + "7" * 64,
            "wheel_inventory_sha256": "sha256:" + "8" * 64,
            "venv_prefix": str(venv.resolve()),
            "venv_inventory_sha256": CONTROLLER.worker_directory_inventory_digest(venv),
            "base_python_configured_path": sys.executable,
            "base_python_identity_sha256": "sha256:" + "9" * 64,
            "prepared_operation_id": operation_id,
            "prepared_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.validate_slot_record(record, slot)
        CONTROLLER.atomic_json(self.slots_state_dir / f"md-{slot}.json", record)
        return record

    def _revalidate_pre_switch(self, descriptor: dict[str, object]) -> None:
        if self.production_config_evidence(check_free_space=True) != descriptor.get(
            "production_config"
        ):
            raise CONTROLLER.PullDeployError(
                "production configuration changed after prepare"
            )
        repository = descriptor["repository"]
        assert isinstance(repository, dict)
        if self.source_sha != repository["previous_sha"]:
            raise CONTROLLER.PullDeployError("fixture source changed")

    def _switch_source(self, _descriptor: dict[str, object]) -> None:
        self.source_sha = TARGET_SHA
        self.source_tree = TARGET_TREE

    def _restore_source(self, _descriptor: dict[str, object]) -> None:
        previous = _descriptor.get("previous_deployment")
        if isinstance(previous, dict):
            self.source_sha = str(previous["source_sha"])
            self.source_tree = str(previous["source_tree"])
        else:
            self.source_sha = PREVIOUS_SHA
            self.source_tree = PREVIOUS_TREE

    def _rollback_failed_attempt(self, _descriptor: object, _marker: object) -> None:
        self.rollback_called = True
        self.source_sha = PREVIOUS_SHA
        self.source_tree = PREVIOUS_TREE


class PullDeployTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="nexpoly-pull-controller-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.production.mkdir(mode=0o755)
        (self.production / ".git").mkdir(mode=0o700)
        for relative in (
            "bin",
            "config",
            "config/docker",
            "state",
            "state/prepared",
            "state/worker-slots",
            "state/control-handoffs",
            "audit",
            "backups",
            "wheel-cache",
            "worker-venvs",
            "control-releases",
        ):
            path = self.runtime / relative
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        lock = self.runtime / "state/deploy.lock"
        lock.write_text("", encoding="utf-8")
        os.chmod(lock, 0o600)
        registry_fixture_payload = '{"schema_version":1,"media":[]}\n'
        registry_fixture_sha256 = CONTROLLER.sha256_bytes(
            registry_fixture_payload.encode("utf-8")
        )
        authority_rules_fixture_payload = (
            '{"schema_version":1,"fixture":"immutable-authority-rules"}\n'
        )
        authority_rules_fixture_sha256 = CONTROLLER.sha256_bytes(
            authority_rules_fixture_payload.encode("utf-8")
        )
        for name, content in (
            ("git-deploy-key", "fixture-key\n"),
            ("known_hosts", "github.com ssh-ed25519 fixture\n"),
            ("github-api-token", "fixture-token\n"),
            (
                "deploy.env",
                "\n".join(
                    (
                        f"NEXPOLY_RUNTIME_ROOT={self.runtime}",
                        f"NEXPOLY_APP_ENV_FILE={self.runtime / 'config/app.env'}",
                        f"NEXPOLY_ASSET_ROOT={self.runtime / 'state/current-assets'}",
                        "NEXPOLY_POSTGRES_USER=fixture_user",
                        "NEXPOLY_POSTGRES_PASSWORD=fixture-secret-0123456789",
                        "NEXPOLY_POSTGRES_DB=nexpoly",
                        "NEXPOLY_POSTGRES_PORT=55432",
                        "APP_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "PI_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "LAB_DATA_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "POLYTAO_ENABLED=true",
                        "MONOMER_MD_REQUIRE_TRANSPORT_READY=true",
                        "NEXPOLY_HEALTH_URLS=http://127.0.0.1:9000/health",
                        "NEXPOLY_MIN_FREE_BYTES=1073741824",
                        "NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES=8589934592",
                        (
                            "NEXPOLY_CONTRACT_0012_EXTERNAL_DATABASE_AUDIT_COMMAND="
                            + str(
                                self.runtime
                                / "config"
                                / CONTROLLER.EXTERNAL_DATABASE_AUDIT_HELPER
                            )
                        ),
                        "NEXPOLY_CONTRACT_0012_DEV_AUDIT_USER=nexpoly_dev_auditor",
                        "NEXPOLY_CONTRACT_0012_MD_HEALTH_AUDIT_USER=nexpoly_health_auditor",
                        (
                            "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256="
                            + authority_rules_fixture_sha256
                        ),
                        f"NEXPOLY_WORKER_BASE_PYTHON={sys.executable}",
                        "",
                    )
                ),
            ),
            ("app.env", "ONLINE_KNOWLEDGE_API_KEY=fixture\n"),
        ):
            write_private(self.runtime / "config" / name, content)
        for name in CONTROLLER.STABLE_HELPER_FILES:
            helper = self.runtime / "bin" / name
            helper.write_text(f"fixture {name}\n", encoding="utf-8")
            os.chmod(helper, 0o700)
        for name in (
            "bootstrap-quiesce",
            "bootstrap-status",
            "bootstrap-resume-unchanged",
            "bootstrap-rollback",
            "bootstrap-active-jobs-probe",
            "bootstrap-legacy-runtime-status",
            "bootstrap-legacy-runtime-resume-unchanged",
            "bootstrap-legacy-runtime-restore",
            CONTROLLER.EXTERNAL_DATABASE_AUDIT_HELPER,
            CONTROLLER.MUTABLE_DATA_AUDIT_HELPER,
        ):
            hook = self.runtime / "config" / name
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(hook, 0o700)
        write_private(
            self.runtime
            / "config"
            / CONTROLLER.EXTERNAL_DATABASE_MEDIA_AUTHORITY_RULES,
            authority_rules_fixture_payload,
        )
        write_private(
            self.runtime
            / "config"
            / CONTROLLER.EXTERNAL_DATABASE_MEDIA_REGISTRY,
            registry_fixture_payload,
        )
        write_private(
            self.runtime / "config" / CONTROLLER.MUTABLE_DATA_SERVICE_CONFIG,
            (
                "[nexpoly-mutable-audit]\n"
                "host=127.0.0.1\n"
                "port=55432\n"
                "dbname=nexpoly\n"
                "user=nexpoly_mutable_audit\n"
                "sslmode=disable\n"
                f"passfile={self.runtime / 'config' / CONTROLLER.MUTABLE_DATA_PGPASS}\n"
            ),
        )
        write_private(
            self.runtime / "config" / CONTROLLER.MUTABLE_DATA_PGPASS,
            "127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:fixture\n",
        )
        write_private(
            self.runtime / "config/docker/config.json",
            json.dumps(
                {
                    "auths": {
                        "ghcr.io": {
                            "auth": base64.b64encode(b"fixture:read-only-token").decode(
                                "ascii"
                            )
                        }
                    }
                }
            ),
        )
        # Bootstrap normally installs the target control authority before the
        # first governed runtime takeover.  Seed that exact condition for both
        # base-controller plan tests and mutating fixture tests.
        FixtureController(
            self.production,
            self.runtime,
            runner=GitRunner(),
            lifecycle=FakeLifecycle(),
            apply=True,
        )

    def controller(
        self,
        *,
        runner: object | None = None,
        lifecycle: FakeLifecycle | None = None,
    ) -> FixtureController:
        return FixtureController(
            self.production,
            self.runtime,
            runner=runner or GitRunner(),
            lifecycle=lifecycle or FakeLifecycle(),
            apply=True,
        )


class RepositoryAndEvidenceTests(PullDeployTestCase):
    def test_bridge_media_authority_rules_are_exact_f_object_bytes(
        self,
    ) -> None:
        controller = self.controller()
        payload = b'{"schema_version":1,"rules":"fixture"}\n'
        policy = {
            "external_database_audit": {
                **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
                "media_authority_rules_sha256": (
                    CONTROLLER.sha256_bytes(payload)
                ),
            }
        }
        with mock.patch.object(
            controller,
            "_git_show",
            return_value=payload,
        ) as git_show:
            self.assertEqual(
                controller._verify_bridge_media_authority_rules(
                    TARGET_SHA,
                    policy,
                ),
                CONTROLLER.sha256_bytes(payload),
            )
        git_show.assert_called_once_with(
            TARGET_SHA,
            CONTROLLER._bridge_core.MEDIA_AUTHORITY_RULES_RELATIVE_PATH,
        )

        with (
            mock.patch.object(
                controller,
                "_git_show",
                return_value=b'{"schema_version":1,"rules":"drift"}\n',
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "differ from F policy",
            ),
        ):
            controller._verify_bridge_media_authority_rules(
                TARGET_SHA,
                policy,
            )

    def test_trusted_git_uses_fixed_binary_despite_shadowed_path(self) -> None:
        controller = self.controller()

        class RecordingRunner:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []

            def run(
                self, command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

        runner = RecordingRunner()
        controller.runner = runner
        with (
            mock.patch.object(
                controller,
                "_git_trust_preflight",
                return_value={"trusted": True},
            ),
            mock.patch.object(
                controller,
                "_clean_environment",
                return_value={"PATH": "/tmp/fake-git:/usr/local/bin:/usr/bin"},
            ),
        ):
            controller._git("status", "--porcelain=v1")
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(runner.commands[0][0], "/usr/bin/git")

    def test_stopped_bridge_guard_rejects_every_network_fetch_path(
        self,
    ) -> None:
        bundle = self.runtime / "sealed.bundle"
        write_private(bundle, "fixture bundle\n")
        guarded = CONTROLLER.OfflineBridgeRunner(GitRunner())

        for command in (
            [
                "git",
                "ls-remote",
                CONTROLLER.REPOSITORY_SSH_URL,
                "refs/heads/main",
            ],
            ["git", "fetch", CONTROLLER.REPOSITORY_SSH_URL],
            ["docker", "pull", "ghcr.io/example/image@sha256:" + "1" * 64],
            ["python3", "-m", "pip", "download", "fixture"],
            ["python3", "-m", "pip", "install", "fixture"],
            ["ssh", "production.example"],
            ["curl", "https://api.github.com/repos/example/project"],
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "offline bridge revalidation",
                ):
                    guarded.run(command)

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "GitHub API",
        ):
            guarded.request_json("https://api.github.com", "token")
        CONTROLLER.OfflineBridgeRunner._validate_command(
            [
                "git",
                "-c",
                "credential.helper=",
                "fetch",
                "--no-tags",
                str(bundle),
                "+refs/heads/main:refs/nexpoly/prefetch/test/authority",
            ]
        )
        CONTROLLER.OfflineBridgeRunner._validate_command(
            ["docker", "image", "inspect", CONTROLLER.POSTGRES16_IMAGE]
        )
        CONTROLLER.OfflineBridgeRunner._validate_command(
            [
                "python3",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(self.runtime / "wheel-cache"),
                "fixture",
            ]
        )

    def test_bridge_ci_comes_only_from_sealed_bootstrap_publication(
        self,
    ) -> None:
        controller = self.controller()
        expected = controller.bootstrap_ci_evidence(
            authority_sha=TARGET_SHA,
            required_jobs=CONTROLLER._bridge_core.REQUIRED_CI_JOBS,
        )
        self.assertEqual(expected["workflow_run_id"], 42)
        self.assertEqual(expected["head_sha"], TARGET_SHA)

        path = controller.state_dir / "bootstrap-control.json"
        changed = CONTROLLER.load_private_json(path)
        changed["delivery_gate"]["ci"]["required_jobs"] = ["ci-gate"]
        CONTROLLER.atomic_json(path, changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact required F CI jobs",
        ):
            controller.bootstrap_ci_evidence(
                authority_sha=TARGET_SHA,
                required_jobs=CONTROLLER._bridge_core.REQUIRED_CI_JOBS,
            )

    def test_ambient_test_mode_cannot_authorize_production_roots(self) -> None:
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.PullDeployController(
                CONTROLLER.PRODUCTION_ROOT,
                self.runtime,
                runner=GitRunner(),
                apply=True,
            )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.PullDeployController(
                self.production,
                CONTROLLER.RUNTIME_ROOT,
                runner=GitRunner(),
                apply=True,
            )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.clean_control_environment(CONTROLLER.RUNTIME_ROOT)

    def test_plan_is_read_only_and_requires_requested_sha_to_equal_remote_main(
        self,
    ) -> None:
        runner = GitRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        plan = controller.plan(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(plan["target_sha"], TARGET_SHA)
        self.assertFalse(plan["service_mutation"])
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "not current remote main"
        ):
            controller.plan(target_sha="5" * 40, operation_id=OPERATION_ID)

    def test_ci_gate_binds_successful_main_push_and_all_required_jobs(self) -> None:
        runner = GithubRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        evidence = controller.ci_evidence(TARGET_SHA)
        self.assertEqual(evidence["head_sha"], TARGET_SHA)
        self.assertEqual(evidence["conclusion"], "success")
        self.assertEqual(len(runner.urls), 2)

    def test_ci_gate_rejects_each_missing_contract_job(self) -> None:
        for missing in sorted(CONTROLLER._bridge_core.REQUIRED_CI_JOBS):
            with self.subTest(missing=missing):
                runner = GithubRunner()
                original = runner.request_json

                def incomplete(
                    url: str,
                    token: str,
                    *,
                    omitted: str = missing,
                ):  # type: ignore[no-untyped-def]
                    payload = original(url, token)
                    if "/jobs?" in url:
                        return {
                            "jobs": [
                                {"name": name, "conclusion": "success"}
                                for name in sorted(
                                    CONTROLLER._bridge_core.REQUIRED_CI_JOBS
                                )
                                if name != omitted
                            ]
                        }
                    return payload

                runner.request_json = incomplete  # type: ignore[method-assign]
                controller = CONTROLLER.PullDeployController(
                    self.production,
                    self.runtime,
                    runner=runner,
                    apply=False,
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "lacks a successful",
                ):
                    controller.ci_evidence(TARGET_SHA)

    def test_image_gate_resolves_digest_and_rejects_wrong_revision(self) -> None:
        runner = ImageRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        evidence = controller.image_evidence("backend", TARGET_SHA)
        self.assertEqual(
            evidence["digest_ref"], f"{CONTROLLER.BACKEND_TAG_ROOT}@{DIGEST_A}"
        )
        self.assertEqual(
            runner.commands[-2], ["docker", "pull", evidence["digest_ref"]]
        )
        self.assertEqual(
            runner.commands[-1],
            ["docker", "image", "inspect", evidence["digest_ref"]],
        )

        controller.runner = ImageRunner(wrong_revision=True)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "OCI identity"):
            controller.image_evidence("backend", TARGET_SHA)


class EphemeralContainerOwnershipTests(unittest.TestCase):
    @staticmethod
    def web_record(operation_id: str) -> dict[str, object]:
        name = f"nexpoly-web-smoke-{operation_id}-{TARGET_SHA[:12]}"
        return {
            "Id": "1" * 64,
            "Name": f"/{name}",
            "Config": {
                "Image": f"example.invalid/web@{DIGEST_A}",
                "Labels": {"com.nexpoly.deploy-operation": operation_id},
                "Env": [],
            },
            "HostConfig": {
                "NetworkMode": "none",
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "AutoRemove": False,
                "Privileged": False,
                "PublishAllPorts": False,
            },
            "NetworkSettings": {
                "Ports": {"80/tcp": None},
                "Networks": {
                    "none": {
                        "Gateway": "",
                        "IPAddress": "",
                        "IPPrefixLen": 0,
                        "IPv6Gateway": "",
                        "GlobalIPv6Address": "",
                        "GlobalIPv6PrefixLen": 0,
                    }
                },
            },
            "Mounts": [],
        }

    def test_web_run_and_remove_unknown_commit_are_proven_by_inspection(self) -> None:
        record = self.web_record(OPERATION_ID)
        commands: list[list[str]] = []
        inspect_count = 0

        def run(command, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal inspect_count
            commands.append(command)
            if command[:3] == ["docker", "container", "inspect"]:
                inspect_count += 1
                if inspect_count == 1:
                    return SimpleNamespace(returncode=1, stdout="")
                if inspect_count == 2:
                    return SimpleNamespace(returncode=0, stdout=json.dumps([record]))
                return SimpleNamespace(returncode=1, stdout="")
            if command[:2] == ["docker", "run"]:
                raise OSError("response lost after committed run")
            if command[:3] == ["docker", "exec", record["Name"][1:]]:
                if command[-1] == "http://127.0.0.1/":
                    return SimpleNamespace(
                        returncode=0,
                        stdout='<script src="/assets/app-abcdef12.js"></script>',
                    )
                return SimpleNamespace(returncode=0, stdout=b"asset")
            if command[:3] == ["docker", "rm", "--force"]:
                raise OSError("response lost after committed removal")
            raise AssertionError(command)

        controller = SimpleNamespace(
            runner=SimpleNamespace(run=run),
            control_environment=lambda: {},
        )
        descriptor = {
            "operation_id": OPERATION_ID,
            "repository": {"target_sha": TARGET_SHA},
            "images": {"web": {"digest_ref": f"example.invalid/web@{DIGEST_A}"}},
        }
        evidence = CONTROLLER.SystemLifecycle()._verify_web_image(
            controller, descriptor
        )
        self.assertEqual(evidence["image"], descriptor["images"]["web"]["digest_ref"])
        self.assertEqual(inspect_count, 3)

    def test_same_sha_different_operation_and_extra_resources_are_foreign(self) -> None:
        record = self.web_record("deploy-20260716-another-operation")
        record["Name"] = f"/nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}"
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "operation"):
            CONTROLLER.SystemLifecycle._validate_isolated_container(
                record,
                name=f"nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}",
                image=f"example.invalid/web@{DIGEST_A}",
                operation_label="com.nexpoly.deploy-operation",
                operation_id=OPERATION_ID,
                tmpfs_capacity=None,
            )
        record = self.web_record(OPERATION_ID)
        record["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "8080"}]}
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "host resources"):
            CONTROLLER.SystemLifecycle._validate_isolated_container(
                record,
                name=f"nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}",
                image=f"example.invalid/web@{DIGEST_A}",
                operation_label="com.nexpoly.deploy-operation",
                operation_id=OPERATION_ID,
                tmpfs_capacity=None,
            )


class PostgresRuntimeFencingTests(unittest.TestCase):
    def test_docker_exec_requires_an_exact_container_id(self) -> None:
        container_id = "a" * 64
        self.assertEqual(
            CONTROLLER.SystemLifecycle._docker_exec(
                container_id,
                "pg_restore",
                interactive=True,
            ),
            [
                "docker",
                "exec",
                "--interactive",
                container_id,
                "pg_restore",
            ],
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact container ID",
        ):
            CONTROLLER.SystemLifecycle._docker_exec(
                "lab-postgres",
                "dropdb",
            )

    class Runner:
        def __init__(self, *, replace_on_up: bool = False) -> None:
            self.container_id = "a" * 64
            self.replace_on_up = replace_on_up
            self.commands: list[list[str]] = []

        def _container(self) -> dict[str, object]:
            return {
                "Id": self.container_id,
                "Image": "sha256:" + "b" * 64,
                "Config": {
                    "Image": "postgres:16-alpine",
                    "Labels": {
                        "com.docker.compose.project": "nexpoly",
                        "com.docker.compose.service": "lab-postgres",
                    },
                },
                "State": {"Running": True},
                "NetworkSettings": {
                    "Ports": {
                        "5432/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "55432"}
                        ]
                    }
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "nexpoly_postgres_data",
                        "Source": "/var/lib/docker/volumes/nexpoly_postgres_data/_data",
                        "Destination": "/var/lib/postgresql/data",
                        "Driver": "local",
                        "RW": True,
                    }
                ],
            }

        def run(
            self, command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.commands.append(command)
            output = ""
            returncode = 0
            if (
                command[0:3] == ["docker", "container", "inspect"]
                and len(command) == 4
            ):
                output = json.dumps([self._container()])
            elif command[:3] == ["docker", "inspect", "--format"]:
                output = "true\n"
            elif command[:2] == ["docker", "compose"]:
                if "ps" in command and command[-1] == "lab-postgres":
                    output = self.container_id + "\n"
                elif "ps" in command:
                    output = ""
                elif any("pg_control_system()" in value for value in command):
                    output = "7659245354718314530\n"
                elif "up" in command and self.replace_on_up:
                    self.container_id = "c" * 64
            elif command[:3] == ["systemctl", "--user", "is-active"]:
                output = "inactive\n"
                returncode = 3
            elif command[:2] == ["systemctl", "is-active"]:
                output = "inactive\n"
                returncode = 3
            return subprocess.CompletedProcess(command, returncode, output, "")

        def request_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("unexpected network request")

    class Lifecycle(CONTROLLER.SystemLifecycle):
        def _drain_started_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"fixture": True}

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="postgres-fence-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.config = self.runtime / "config"
        self.state = self.runtime / "state"
        for path in (self.production, self.runtime, self.config, self.state):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.descriptor = {
            "images": {
                "backend": {"digest_ref": "example.invalid/backend@" + DIGEST_A},
                "web": {"digest_ref": "example.invalid/web@" + DIGEST_B},
            }
        }

    def controller(self, runner: object) -> object:
        marker_path = self.state / "deploy-in-progress.json"
        return SimpleNamespace(
            runner=runner,
            production_root=self.production,
            runtime_root=self.runtime,
            config_dir=self.config,
            state_dir=self.state,
            marker_path=marker_path,
            control_environment=lambda: {},
            production_deploy_values=lambda **_kwargs: {
                "NEXPOLY_POSTGRES_USER": "nexpoly",
                "NEXPOLY_POSTGRES_DB": "nexpoly",
            },
        )

    def test_stop_and_start_preserve_exact_container_volume_and_system_id(self) -> None:
        runner = self.Runner()
        controller = self.controller(runner)
        lifecycle = self.Lifecycle()
        fence = lifecycle.stop(controller, self.descriptor)
        self.assertEqual(fence["container_id"], "a" * 64)
        self.assertEqual(fence["data_volume"]["name"], "nexpoly_postgres_data")
        self.assertEqual(fence["system_identifier"], "7659245354718314530")
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "runtime_stopped": True,
                "postgres_runtime_fence": fence,
            },
        )
        lifecycle.start(controller, self.descriptor)
        up = next(command for command in runner.commands if "up" in command)
        self.assertIn("--no-deps", up)
        self.assertIn("backend", up)
        self.assertNotIn("lab-postgres", up)
        self.assertFalse(
            any(
                "stop" in command and "lab-postgres" in command
                for command in runner.commands
            )
        )

    def test_start_fails_closed_if_compose_replaces_postgres(self) -> None:
        runner = self.Runner(replace_on_up=True)
        controller = self.controller(runner)
        lifecycle = self.Lifecycle()
        fence = lifecycle.postgres_runtime_identity(controller, self.descriptor)
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "runtime_stopped": True,
                "postgres_runtime_fence": fence,
            },
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed during application start",
        ):
            lifecycle.start(controller, self.descriptor)

    def test_fence_rejects_bind_or_writable_identity_drift(self) -> None:
        fence = {
            "schema_version": 1,
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "configured_image": "postgres:16-alpine",
            "data_volume": {
                "type": "bind",
                "name": "foreign",
                "source": "/tmp/foreign",
                "destination": "/var/lib/postgresql/data",
                "driver": "local",
                "read_write": True,
            },
            "host_endpoint": {
                "host": "127.0.0.1",
                "port": 55432,
                "container_port": 5432,
                "protocol": "tcp",
            },
            "system_identifier": "7659245354718314530",
            "captured_at": CONTROLLER.utc_now(),
        }
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "data-volume fence",
        ):
            CONTROLLER.validate_postgres_runtime_fence(fence)


class SlotAndDescriptorTests(PullDeployTestCase):
    def test_prepare_resumes_existing_descriptor_without_rebuilding_it(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        descriptor_payload = descriptor_path.read_bytes()
        ready_path.unlink()
        result = controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(descriptor_path.read_bytes(), descriptor_payload)
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        controller._validate_ready(
            CONTROLLER.load_private_json(ready_path),
            descriptor,
            descriptor_path,
        )

    def test_pull_uses_shared_strict_schema_v2_asset_contract(self) -> None:
        root = (
            CONTROLLER.ASSET_RELEASES_ROOT
            / CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST.removeprefix(
                "sha256:"
            )
        )
        evidence = {
            "root": str(root),
            "manifest_sha256": CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
            "schema_version": 2,
            "byteff2_commit": "8" * 40,
            "inventory_sha256": "sha256:" + "9" * 64,
        }
        with mock.patch.object(
            CONTROLLER._asset_release_contract,
            "validate_schema_v2_release",
            return_value=evidence,
        ) as validator:
            self.assertEqual(
                CONTROLLER.inspect_asset_release(
                    root,
                    CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
                ),
                evidence,
            )
        validator.assert_called_once_with(
            root,
            expected_digest=CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
            releases_root=CONTROLLER.ASSET_RELEASES_ROOT,
        )

        with (
            mock.patch.object(
                CONTROLLER._asset_release_contract,
                "validate_schema_v2_release",
                side_effect=CONTROLLER._asset_release_contract.AssetContractError(
                    "tampered predecessor"
                ),
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "strict external schema-v2",
            ),
        ):
            CONTROLLER.inspect_asset_release(
                root,
                CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
            )

    def test_alias_gate_allows_preparation_only_before_reconciliation_starts(
        self,
    ) -> None:
        controller = self.controller()
        marker_path = (
            controller.runtime_root
            / CONTROLLER._control_runtime.ALIAS_MARKER_RELATIVE
        )
        completed = CONTROLLER.load_private_json(marker_path)
        marker_path.unlink()
        CONTROLLER.fsync_directory(marker_path.parent)

        controller._require_no_contract_maintenance(
            require_alias_completed=False
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "reconciliation is required"
        ):
            controller._require_no_contract_maintenance()

        CONTROLLER.atomic_json(marker_path, {**completed, "phase": "planned"})
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "must recover first"
        ):
            controller._require_no_contract_maintenance(
                require_alias_completed=False
            )

    def bridge_descriptor(
        self, controller: FixtureController
    ) -> dict[str, object]:
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready_path = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        authority_sha = "5" * 40
        authority_tree = "6" * 40
        previous_control = dict(
            descriptor["controller"]["previous_active_control"]
        )
        previous_control.update(
            {
                "release_id": "7" * 64,
                "source_sha": authority_sha,
                "source_tree": authority_tree,
            }
        )
        descriptor["controller"]["previous_active_control"] = previous_control
        descriptor["controller"][
            "previous_active_control_sha256"
        ] = CONTROLLER.canonical_json_digest(previous_control)
        required_jobs = sorted(CONTROLLER._bridge_core.REQUIRED_CI_JOBS)
        descriptor["ci"] = {
            "workflow_run_id": 99,
            "run_attempt": 1,
            "head_sha": authority_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": required_jobs,
        }
        external_database_audit = external_database_audit_binding(
            controller.runtime_root
        )
        policy = {
            "schema_version": (
                CONTROLLER._bridge_core.POLICY_SCHEMA_VERSION
            ),
            "mode": CONTROLLER._bridge_core.BRIDGE_MODE,
            "authority_ref": CONTROLLER._bridge_core.AUTHORITY_REF,
            "target_sha": TARGET_SHA,
            "target_tree": TARGET_TREE,
            "target_ref": f"refs/nexpoly/bridge-target/{TARGET_SHA}",
            "target_images": {
                role: descriptor["images"][role]["digest_ref"]
                for role in ("backend", "web")
            },
            "asset_manifest_digest": descriptor["release_input"][
                "asset_manifest_digest"
            ],
            "datasets_on_asset_change": [],
            "final_migration": dict(CONTROLLER._bridge_core.FINAL_MIGRATION),
            "accepted_migration_ledgers": (
                CONTROLLER._bridge_core.expected_migration_registry(
                    target_manifest_sha256=B_MANIFEST_DIGEST,
                    target_records=B_MANIFEST_RECORDS,
                    authority_manifest_sha256=F_MANIFEST_DIGEST,
                    authority_records=[
                        *B_MANIFEST_RECORDS,
                        CONTROLLER._bridge_core.FINAL_MIGRATION_RECORD,
                    ],
                )
            ),
            "external_database_audit": {
                **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
                "media_authority_rules_sha256": external_database_audit[
                    "authority_rules"
                ]["sha256"],
            },
            "required_ci_jobs": required_jobs,
            "policy_id": None,
        }
        policy["policy_id"] = CONTROLLER._bridge_core.canonical_json_digest(
            {key: value for key, value in policy.items() if key != "policy_id"}
        )
        descriptor["migrations"] = {
            "sha256": B_MANIFEST_DIGEST,
            "schema_version": 2,
            "records": json.loads(json.dumps(B_MANIFEST_RECORDS)),
        }
        descriptor["schema_version"] = CONTROLLER.BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        descriptor["bridge"] = CONTROLLER._bridge_core.build_bridge_descriptor(
            operation_id=OPERATION_ID,
            authority_sha=authority_sha,
            authority_tree=authority_tree,
            authority_control_release_id=previous_control["release_id"],
            ci_evidence=descriptor["ci"],
            target_control_release_id=descriptor["controller"][
                "executor_control"
            ]["release_id"],
            policy=policy,
            token_id="sha256:" + "8" * 64,
            token_sha256="sha256:" + "9" * 64,
        )
        takeover = {
            "schema_version": 1,
            "operation_id": "takeover-fixture-operation",
            "authority_sha": authority_sha,
            "authority_tree": authority_tree,
            "install_manifest_sha256": "sha256:" + "a" * 64,
            "classification_sha256": "sha256:" + "b" * 64,
            "runtime_identity_sha256": "sha256:" + "c" * 64,
            "git_identity": {
                "branch": "refs/heads/main",
                "head_sha": descriptor["repository"]["previous_sha"],
                "head_tree": descriptor["repository"]["previous_tree"],
                "local_main_sha": descriptor["repository"]["previous_sha"],
            },
            "pre_stopped_fence_sha256": "sha256:" + "d" * 64,
            "control_layout_sha256": "sha256:" + "e" * 64,
            "checkout_permissions_sha256": "sha256:" + "f" * 64,
            "applied_record_sha256": "sha256:" + "1" * 64,
        }
        takeover["binding_sha256"] = CONTROLLER.canonical_json_digest(
            takeover
        )
        descriptor["legacy_takeover"] = takeover
        prefetch = {
            "schema_version": 2,
            "operation_id": "prefetch-fixture-operation",
            "ready_path": str(
                controller.runtime_root
                / "prefetch/prefetch-fixture-operation/ready.json"
            ),
            "ready_sha256": "sha256:" + "2" * 64,
            "identity_sha256": "sha256:" + "3" * 64,
            "source": {
                "authority": {
                    "sha": authority_sha,
                    "tree": authority_tree,
                },
                "target": {
                    "sha": TARGET_SHA,
                    "tree": TARGET_TREE,
                },
            },
            "source_readiness_sha256": "sha256:" + "4" * 64,
            "controller_sha256": "sha256:" + "b" * 64,
            "policy_sha256": descriptor["bridge"]["policy_sha256"],
            "docker_config_path": str(
                controller.runtime_root / "config/docker"
            ),
            "git_bundle_sha256": "sha256:" + "6" * 64,
            "images_sha256": "sha256:" + "7" * 64,
            "wheel_caches_sha256": "sha256:" + "8" * 64,
            "asset_manifest_sha256": descriptor["release_input"][
                "asset_manifest_digest"
            ],
            "asset_inventory_sha256": "sha256:" + "9" * 64,
            "asset_contract_sha256": "sha256:" + "c" * 64,
            "asset_builder_proof_sha256": "sha256:" + "d" * 64,
            "asset_predecessor_inventory_sha256": "sha256:" + "e" * 64,
            "live_asset_pointer_sha256": "sha256:" + "f" * 64,
            "recovery_tools_sha256": "sha256:" + "a" * 64,
            "created_at": "2026-07-17T00:00:00Z",
        }
        prefetch["binding_sha256"] = CONTROLLER.canonical_json_digest(
            prefetch
        )
        descriptor["prefetch"] = prefetch
        descriptor["external_database_audit"] = external_database_audit
        return descriptor

    def bind_bridge_token(
        self,
        controller: FixtureController,
        descriptor: dict[str, object],
    ) -> str:
        bridge = descriptor["bridge"]
        token = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=bridge["policy"]["policy_id"],
            token=b"bridge-token-fixture-entropy-0001",
        )
        bridge["token"] = {
            "token_id": token["token_id"],
            "token_sha256": token["token_sha256"],
        }
        CONTROLLER.validate_descriptor(descriptor)
        descriptor_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(descriptor) + b"\n"
        )
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=bridge["policy"]["policy_id"],
            descriptor_sha256=descriptor_digest,
        )
        return descriptor_digest

    def bind_bridge_recovery_capsule(
        self,
        controller: FixtureController,
        descriptor: dict[str, object],
        descriptor_digest: str,
        marker: dict[str, object],
    ) -> dict[str, object]:
        with controller.deployment_lock():
            capsule = controller._prepare_bridge_recovery_capsule(
                descriptor, descriptor_digest
            )
        marker["bridge_recovery_capsule"] = (
            controller._bridge_recovery_capsule_binding(capsule)
        )
        return capsule

    def test_bridge_recovery_capsule_publish_is_crash_idempotent_and_tamper_evident(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        original_rename = CONTROLLER.os.rename

        def publish_then_lose_response(source, target):  # type: ignore[no-untyped-def]
            original_rename(source, target)
            raise RuntimeError("injected capsule rename response loss")

        with (
            mock.patch.object(
                CONTROLLER.os,
                "rename",
                side_effect=publish_then_lose_response,
            ),
            self.assertRaisesRegex(RuntimeError, "rename response loss"),
            controller.deployment_lock(),
        ):
            controller._prepare_bridge_recovery_capsule(
                descriptor, descriptor_digest
            )
        with controller.deployment_lock():
            capsule = controller._prepare_bridge_recovery_capsule(
                descriptor, descriptor_digest
            )
        observed, observed_descriptor = (
            controller._load_bridge_recovery_capsule(
                capsule["capsule_sha256"]
            )
        )
        self.assertEqual(observed, capsule)
        self.assertEqual(observed_descriptor, descriptor)

        root = (
            controller.runtime_root
            / CONTROLLER.BRIDGE_RECOVERY_CAPSULE_ROOT_RELATIVE
            / capsule["capsule_sha256"].removeprefix("sha256:")
        )
        entry = root / "control/bridge_recovery_capsule.py"
        entry.write_bytes(entry.read_bytes() + b"# tampered\n")
        os.chmod(entry, 0o700)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "file changed"
        ):
            controller._load_bridge_recovery_capsule(
                capsule["capsule_sha256"]
            )

    def test_v3_descriptor_binds_f_authority_exact_b_and_empty_datasets(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        self.assertEqual(CONTROLLER.validate_descriptor(descriptor), descriptor)

        mutations = (
            (
                "authority control",
                lambda value: value["bridge"]["authority"].__setitem__(
                    "control_release_id", "a" * 64
                ),
            ),
            (
                "authority CI",
                lambda value: value["ci"].__setitem__("workflow_run_id", 100),
            ),
            (
                "target ref",
                lambda value: value["bridge"]["target"].__setitem__(
                    "exact_ref", f"refs/nexpoly/bridge-target/{'a' * 40}"
                ),
            ),
            (
                "dataset rebuild",
                lambda value: value["release_input"][
                    "datasets_on_asset_change"
                ].append("online"),
            ),
            (
                "external registry",
                lambda value: value["external_database_audit"][
                    "registry"
                ].__setitem__("sha256", "sha256:" + "f" * 64),
            ),
            (
                "external snapshot",
                lambda value: value["external_database_audit"].__setitem__(
                    "snapshot_sha256", "sha256:" + "f" * 64
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(descriptor))
                mutate(changed)
                with self.assertRaises(CONTROLLER.PullDeployError):
                    CONTROLLER.validate_descriptor(changed)

    def test_alias_bridge_projection_is_content_addressed_and_tamper_evident(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        _operation, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        ready = CONTROLLER.load_private_json(ready_path)
        ready["descriptor_sha256"] = CONTROLLER.sha256_file(
            descriptor_path
        )
        CONTROLLER.atomic_json(ready_path, ready)

        authority = CONTROLLER.alias_bridge_authority_projection(
            descriptor,
            descriptor_path=descriptor_path,
            ready_path=ready_path,
        )
        self.assertEqual(
            CONTROLLER.validate_alias_bridge_authority(
                authority,
                descriptor=descriptor,
                descriptor_path=descriptor_path,
                ready_path=ready_path,
            ),
            authority,
        )
        changed = json.loads(json.dumps(authority))
        changed["target"]["sha"] = "f" * 40
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "another bridge",
        ):
            CONTROLLER.validate_alias_bridge_authority(
                changed,
                descriptor=descriptor,
                descriptor_path=descriptor_path,
                ready_path=ready_path,
            )

    def test_alias_bridge_projection_accepts_only_proven_restored_successor(
        self,
    ) -> None:
        controller = self.controller()
        original = self.bridge_descriptor(controller)
        original_digest = self.bind_bridge_token(controller, original)
        _operation, original_path, original_ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        CONTROLLER.atomic_json(original_path, original)
        self.assertEqual(
            CONTROLLER.sha256_file(original_path),
            original_digest,
        )
        original_ready = CONTROLLER.load_private_json(
            original_ready_path
        )
        original_ready["descriptor_sha256"] = original_digest
        CONTROLLER.atomic_json(original_ready_path, original_ready)
        alias_authority = (
            CONTROLLER.alias_bridge_authority_projection(
                original,
                descriptor_path=original_path,
                ready_path=original_ready_path,
            )
        )

        retired = CONTROLLER._bridge_core.retire_precommit_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            descriptor_sha256=original_digest,
            operation_state_sha256="sha256:" + "1" * 64,
            terminal_audit_sha256="sha256:" + "2" * 64,
            restored_terminal_sha256="sha256:" + "3" * 64,
            recovery_capsule_sha256="sha256:" + "4" * 64,
        )
        successor_operation = "bridge-20260717-successor"
        successor_token = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=successor_operation,
            policy_id=original["bridge"]["policy"]["policy_id"],
            token=b"successor-token-fixture-entropy-01",
            predecessor_retirement=(
                CONTROLLER._bridge_core.retirement_reuse_authority(
                    retired
                )
            ),
        )
        successor = json.loads(json.dumps(original))
        successor["operation_id"] = successor_operation
        successor["prepared_at"] = successor_token["prepared_at"]
        executor = successor["controller"]["executor_control"]
        executor["operation_id"] = successor_operation
        successor["controller"][
            "executor_control_sha256"
        ] = CONTROLLER.canonical_json_digest(executor)
        slot = successor["monomer_md"]["slot_record"]
        slot["prepared_operation_id"] = successor_operation
        successor["monomer_md"][
            "slot_record_sha256"
        ] = CONTROLLER.worker_record_digest(slot)
        bridge = original["bridge"]
        successor["bridge"] = (
            CONTROLLER._bridge_core.build_bridge_descriptor(
                operation_id=successor_operation,
                authority_sha=bridge["authority"]["sha"],
                authority_tree=bridge["authority"]["tree"],
                authority_control_release_id=bridge["authority"][
                    "control_release_id"
                ],
                ci_evidence=successor["ci"],
                target_control_release_id=bridge["target"][
                    "control_release_id"
                ],
                policy=bridge["policy"],
                token_id=successor_token["token_id"],
                token_sha256=successor_token["token_sha256"],
            )
        )
        CONTROLLER.validate_descriptor(successor)
        successor_root, successor_path, successor_ready_path = (
            controller._operation_paths(successor_operation)
        )
        successor_root.mkdir(mode=0o700)
        CONTROLLER.atomic_json(successor_path, successor)
        successor_digest = CONTROLLER.sha256_file(successor_path)
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=successor_operation,
            policy_id=bridge["policy"]["policy_id"],
            descriptor_sha256=successor_digest,
        )
        successor_ready = {
            "schema_version": 1,
            "status": "ready",
            "operation_id": successor_operation,
            "source_sha": successor["repository"]["target_sha"],
            "descriptor_sha256": successor_digest,
            "executor_control": executor,
            "executor_control_sha256": successor["controller"][
                "executor_control_sha256"
            ],
            "slot_record_sha256": successor["monomer_md"][
                "slot_record_sha256"
            ],
            "prepared_at": "2026-07-17T00:00:01Z",
        }
        CONTROLLER.atomic_json(successor_ready_path, successor_ready)
        current_token = CONTROLLER._bridge_core.load_token_authority(
            controller.state_dir
        )
        self.assertEqual(
            CONTROLLER.validate_alias_bridge_authority(
                alias_authority,
                descriptor=successor,
                descriptor_path=successor_path,
                ready_path=successor_ready_path,
                state_root=controller.state_dir,
                current_token=current_token,
            ),
            alias_authority,
        )

    def test_external_database_cas_allows_only_fresh_timestamp_changes(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        observed = json.loads(
            json.dumps(descriptor["external_database_audit"])
        )
        observed["snapshot"]["media_registry"]["captured_at"] = (
            "2026-07-17T00:01:00Z"
        )
        observed["snapshot"]["media"][0]["audit"]["audited_at"] = (
            "2026-07-17T00:01:00Z"
        )
        observed["snapshot"]["media_registry"][
            "docker_inventory_sha256"
        ] = "sha256:" + "1" * 64
        observed["snapshot"]["media_registry"][
            "backup_inventory_sha256"
        ] = "sha256:" + "2" * 64
        observed["snapshot"]["media_registry"][
            "discovery_state_sha256_before"
        ] = "sha256:" + "3" * 64
        observed["snapshot"]["media_registry"][
            "discovery_state_sha256_after"
        ] = "sha256:" + "3" * 64
        observed["snapshot"]["media_registry"][
            "scanned_container_ids"
        ] = sorted(
            [
                *observed["snapshot"]["media_registry"][
                    "scanned_container_ids"
                ],
                "f" * 64,
            ]
        )
        reseal_external_database_audit_binding(observed)
        with mock.patch.object(
            controller,
            "external_database_audit_evidence",
            return_value=observed,
        ):
            self.assertEqual(
                controller._revalidate_external_database_audit(descriptor),
                observed,
            )

        changed = json.loads(json.dumps(observed))
        changed["snapshot"]["media"][0]["source_content_sha256"] = (
            "sha256:" + "e" * 64
        )
        reseal_external_database_audit_binding(changed)
        with (
            mock.patch.object(
                controller,
                "external_database_audit_evidence",
                return_value=changed,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "media changed",
            ),
        ):
            controller._revalidate_external_database_audit(descriptor)

    def test_external_database_semantic_state_excludes_fresh_self_seals(
        self,
    ) -> None:
        controller = self.controller()
        before = external_database_audit_binding(
            controller.runtime_root
        )
        ledger = next(
            record["ledger"]
            for record in before["snapshot"]["media"]
            if record["disposition"] == "writable-target"
        )
        after = external_database_binding_state(
            before,
            production_ledger=ledger,
            legacy_relation_present=True,
            captured_at="2026-07-17T00:02:00Z",
        )
        self.assertNotEqual(
            before["identity_sha256"],
            after["identity_sha256"],
        )
        self.assertEqual(
            before["state_sha256"],
            after["state_sha256"],
        )
        changed = json.loads(json.dumps(after))
        medium = changed["snapshot"]["media"][0]
        medium["source_identity_before"]["attached"][0][
            "container_started_at"
        ] = "2026-07-17T00:03:00.000000000Z"
        medium["source_identity_after"] = json.loads(
            json.dumps(medium["source_identity_before"])
        )
        medium["audit"].pop("evidence_sha256")
        medium["audit"]["evidence_sha256"] = (
            CONTROLLER.canonical_json_digest(medium)
        )
        reseal_external_database_audit_binding(changed)
        validated = CONTROLLER.validate_external_database_audit_binding(
            changed
        )
        self.assertNotEqual(
            before["state_sha256"],
            validated["state_sha256"],
        )

    def test_external_database_semantic_state_normalizes_backup_scratch_identity(
        self,
    ) -> None:
        first = {
            "media_registry": {
                "schema_version": 2,
                "sha256": "sha256:" + "1" * 64,
                "discovery_boundary_sha256": "sha256:" + "2" * 64,
                "expected_media_ids": ["postgres-backup:/private/db.dump"],
                "discovered_media_ids": ["postgres-backup:/private/db.dump"],
                "captured_at": "2026-07-17T00:00:00Z",
                "discovery_state_sha256_before": "sha256:" + "3" * 64,
                "discovery_state_sha256_after": "sha256:" + "3" * 64,
                "docker_inventory_sha256": "sha256:" + "4" * 64,
                "backup_inventory_sha256": "sha256:" + "5" * 64,
                "scanned_volume_names": [],
                "scanned_container_ids": [],
            },
            "media": [
                {
                    "database_identity": {
                        "database": "nexpoly",
                        "system_identifier": "111",
                        "system_identifier_scope": "isolated-restore-cluster",
                        "database_oid": "16384",
                        "database_owner": "postgres",
                        "encoding": "UTF8",
                        "collate": "C",
                        "ctype": "C",
                        "server_version_num": 160004,
                    },
                    "database_identity_sha256": "sha256:" + "6" * 64,
                    "source_content_sha256": "sha256:" + "7" * 64,
                    "audit": {
                        "method": "isolated-backup-restore-read-only",
                        "audited_at": "2026-07-17T00:00:00Z",
                        "evidence_sha256": "sha256:" + "8" * 64,
                    },
                }
            ],
        }
        second = json.loads(json.dumps(first))
        second["media"][0]["database_identity"].update(
            {
                "system_identifier": "222",
                "database_oid": "24576",
                "server_version_num": 160005,
            }
        )
        second["media"][0]["database_identity_sha256"] = (
            "sha256:" + "9" * 64
        )
        self.assertEqual(
            CONTROLLER.external_database_audit_state(first),
            CONTROLLER.external_database_audit_state(second),
        )

    def test_alias_and_bridge_external_database_pairs_form_exact_chain(
        self,
    ) -> None:
        controller = self.controller()
        canonical = [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER
            )
        ]
        through_0008 = [
            row
            for row in canonical
            if row["version"] <= "0008_polytao_backend_runtime"
        ]
        through_0011 = [
            row
            for row in canonical
            if row["version"] <= "0011_monomer_md_demo_steps"
        ]
        alias_row = {
            "version": (
                CONTROLLER._site_helper_contracts
                .LEGACY_0005_ALIAS_VERSION
            ),
            "checksum": (
                CONTROLLER._site_helper_contracts
                .LEGACY_0005_ALIAS_CHECKSUM
            ),
        }
        seed = external_database_audit_binding(
            controller.runtime_root
        )
        pre_alias = external_database_binding_state(
            seed,
            production_ledger=sorted(
                [*through_0008, alias_row],
                key=lambda row: row["version"],
            ),
            legacy_relation_present=True,
            captured_at="2026-07-17T00:01:00Z",
        )
        post_alias = external_database_binding_state(
            pre_alias,
            production_ledger=through_0008,
            legacy_relation_present=True,
            captured_at="2026-07-17T00:02:00Z",
        )
        descriptor_sha256 = "sha256:" + "d" * 64
        alias_pair = CONTROLLER.build_external_database_alias_pair(
            pre_alias,
            post_alias,
            operation_id="alias-0005-chain-test",
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            CONTROLLER.validate_external_database_alias_pair(
                alias_pair,
                before_binding=pre_alias,
            ),
            alias_pair,
        )
        post_bridge = external_database_binding_state(
            post_alias,
            production_ledger=through_0011,
            legacy_relation_present=True,
            captured_at="2026-07-17T00:03:00Z",
        )
        bridge_pair = CONTROLLER.build_external_database_bridge_pair(
            post_alias,
            post_bridge,
            operation_id=OPERATION_ID,
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            bridge_pair["transition"]["added"],
            through_0011[len(through_0008) :],
        )
        volatile_post_bridge = json.loads(json.dumps(post_bridge))
        registry = volatile_post_bridge["snapshot"]["media_registry"]
        registry["docker_inventory_sha256"] = "sha256:" + "0" * 64
        registry["backup_inventory_sha256"] = "sha256:" + "1" * 64
        registry["scanned_container_ids"] = sorted(
            [*registry["scanned_container_ids"], "f" * 64]
        )
        reseal_external_database_audit_binding(volatile_post_bridge)
        volatile_pair = CONTROLLER.build_external_database_bridge_pair(
            post_alias,
            volatile_post_bridge,
            operation_id=OPERATION_ID,
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            volatile_pair["after_binding"]["state_sha256"],
            bridge_pair["after_binding"]["state_sha256"],
        )
        changed = json.loads(json.dumps(post_alias))
        dormant = next(
            record
            for record in changed["snapshot"]["media"]
            if record["database"] == "nexpoly_dev"
        )
        dormant["source_identity_before"]["inspect_sha256"] = (
            "sha256:" + "0" * 64
        )
        dormant["source_identity_after"] = json.loads(
            json.dumps(dormant["source_identity_before"])
        )
        dormant["audit"].pop("evidence_sha256")
        dormant["audit"]["evidence_sha256"] = (
            CONTROLLER.canonical_json_digest(dormant)
        )
        reseal_external_database_audit_binding(changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "read-only external media",
        ):
            CONTROLLER.build_external_database_bridge_pair(
                post_alias,
                changed,
                operation_id=OPERATION_ID,
                descriptor_sha256=descriptor_sha256,
            )

    def test_external_database_chain_folds_exact_0012_and_0013_endpoints(
        self,
    ) -> None:
        controller = self.controller()
        canonical = [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                CONTROLLER._site_helper_contracts.CANONICAL_MIGRATION_LEDGER
            )
        ]
        through_0012 = canonical[:12]
        through_0013 = canonical[:13]
        bridge = external_database_audit_binding(
            controller.runtime_root
        )
        post_contract = external_database_binding_state(
            bridge,
            production_ledger=through_0012,
            legacy_relation_present=False,
            captured_at="2026-07-17T00:04:00Z",
        )
        contract_pair = (
            CONTROLLER.build_external_database_contract_pair(
                bridge,
                post_contract["snapshot"],
                operation_id="contract-0012-chain-test",
            )
        )
        self.assertEqual(
            CONTROLLER.external_database_endpoint(
                bridge,
                contract_pair=contract_pair,
            )["state_sha256"],
            post_contract["state_sha256"],
        )

        post_final = external_database_binding_state(
            post_contract,
            production_ledger=through_0013,
            legacy_relation_present=False,
            captured_at="2026-07-17T00:05:00Z",
        )
        descriptor_sha256 = "sha256:" + "d" * 64
        final_pair = CONTROLLER.build_external_database_final_pair(
            post_contract,
            post_final,
            operation_id="deploy-final-0013-chain-test",
            descriptor_sha256=descriptor_sha256,
        )
        self.assertEqual(
            CONTROLLER.validate_external_database_final_pair(
                final_pair,
                before_binding=post_contract,
            ),
            final_pair,
        )
        self.assertEqual(
            final_pair["transition"]["added"],
            through_0013[len(through_0012) :],
        )
        self.assertEqual(
            CONTROLLER.external_database_endpoint(
                bridge,
                contract_pair=contract_pair,
                final_pair=final_pair,
            )["state_sha256"],
            post_final["state_sha256"],
        )

        wrong_checksum = json.loads(json.dumps(post_final))
        writable = next(
            record
            for record in wrong_checksum["snapshot"]["media"]
            if record["disposition"] == "writable-target"
        )
        writable["ledger"][-1]["checksum"] = "f" * 64
        writable["ledger_sha256"] = CONTROLLER.canonical_json_digest(
            writable["ledger"]
        )
        writable["ledger_relation"]["content_sha256"] = writable[
            "ledger_sha256"
        ]
        writable["migration_0013"] = {
            "state": "superseded",
            "checksum": "f" * 64,
        }
        writable["audit"].pop("evidence_sha256")
        writable["audit"]["evidence_sha256"] = (
            CONTROLLER.canonical_json_digest(writable)
        )
        reseal_external_database_audit_binding(wrong_checksum)
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.build_external_database_final_pair(
                post_contract,
                wrong_checksum,
                operation_id="deploy-final-0013-chain-test",
                descriptor_sha256=descriptor_sha256,
            )

    def test_bridge_prepare_binds_static_rules_dynamic_registry_and_fresh_audit(
        self,
    ) -> None:
        controller = self.controller()
        expected = external_database_audit_binding(
            controller.runtime_root,
            captured_at=CONTROLLER.utc_now(),
        )
        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            [expected["helper"]["path"]],
            0,
            json.dumps(expected["snapshot"]),
            "",
        )
        controller.runner = runner
        values = {
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND: expected[
                "helper"
            ]["path"],
            CONTROLLER.CONTRACT_0012_MEDIA_AUTHORITY_RULES_DIGEST: expected[
                "authority_rules"
            ]["sha256"],
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_USERS[
                "nexpoly_dev"
            ]: expected["expected_users"]["nexpoly_dev"],
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_USERS[
                "nexpoly_md_health_opt"
            ]: expected["expected_users"]["nexpoly_md_health_opt"],
        }
        policy = {
            **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": expected[
                "authority_rules"
            ]["sha256"],
        }
        with mock.patch.object(
            controller,
            "production_deploy_values",
            return_value=values,
        ):
            observed = (
                CONTROLLER.PullDeployController.external_database_audit_evidence(
                    controller,
                    policy,
                )
            )
        self.assertEqual(observed, expected)
        runner.run.assert_called_once()
        self.assertEqual(
            observed["registry"]["authority_rules_sha256"],
            observed["authority_rules"]["sha256"],
        )
        self.assertEqual(
            observed["snapshot"]["media_registry"][
                "runtime_registry_sha256"
            ],
            observed["registry"]["sha256"],
        )
        self.assertNotIn(
            "runtime_registry_sha256",
            policy,
        )

        stale = external_database_audit_binding(
            controller.runtime_root,
            captured_at="2026-07-16T00:00:00Z",
        )
        runner.run.return_value = subprocess.CompletedProcess(
            [stale["helper"]["path"]],
            0,
            json.dumps(stale["snapshot"]),
            "",
        )
        with (
            mock.patch.object(
                controller,
                "production_deploy_values",
                return_value=values,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "not captured by this invocation",
            ),
        ):
            CONTROLLER.PullDeployController.external_database_audit_evidence(
                controller,
                policy,
            )

        missing_user_values = dict(values)
        missing_user_values.pop(
            CONTROLLER.CONTRACT_0012_EXTERNAL_AUDIT_USERS[
                "nexpoly_dev"
            ]
        )
        with (
            mock.patch.object(
                controller,
                "production_deploy_values",
                return_value=missing_user_values,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "user is missing or invalid",
            ),
        ):
            CONTROLLER.PullDeployController.external_database_audit_evidence(
                controller,
                policy,
            )

        with (
            mock.patch.object(
                controller,
                "production_deploy_values",
                return_value={
                    **values,
                    CONTROLLER.CONTRACT_0012_MEDIA_AUTHORITY_RULES_DIGEST: (
                        "sha256:" + "f" * 64
                    ),
                },
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "differ from deploy.env or F policy",
            ),
        ):
            CONTROLLER.PullDeployController.external_database_audit_evidence(
                controller,
                {
                    **policy,
                    "media_authority_rules_sha256": (
                        "sha256:" + "f" * 64
                    ),
                },
            )

    def test_external_database_dual_digests_are_policy_and_cas_bound(
        self,
    ) -> None:
        controller = self.controller()
        expected = external_database_audit_binding(
            controller.runtime_root
        )
        policy = {
            **CONTROLLER._bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": expected[
                "authority_rules"
            ]["sha256"],
        }
        CONTROLLER.validate_external_database_audit_binding(
            expected,
            expected_policy=policy,
        )

        changed_authority = json.loads(json.dumps(expected))
        replacement_authority = "sha256:" + "e" * 64
        changed_authority["authority_rules"]["sha256"] = (
            replacement_authority
        )
        changed_authority["registry"]["authority_rules_sha256"] = (
            replacement_authority
        )
        changed_authority["snapshot"]["media_registry"][
            "media_authority_rules_sha256"
        ] = replacement_authority
        reseal_external_database_audit_binding(changed_authority)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "differs from F policy",
        ):
            CONTROLLER.validate_external_database_audit_binding(
                changed_authority,
                expected_policy=policy,
            )

        changed_registry = json.loads(json.dumps(expected))
        replacement_registry = "sha256:" + "d" * 64
        changed_registry["registry"]["sha256"] = replacement_registry
        changed_registry["snapshot"]["media_registry"][
            "runtime_registry_sha256"
        ] = replacement_registry
        reseal_external_database_audit_binding(changed_registry)
        CONTROLLER.validate_external_database_audit_binding(
            changed_registry,
            expected_policy=policy,
        )
        with (
            mock.patch.object(
                controller,
                "external_database_audit_evidence",
                return_value=changed_registry,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "media changed",
            ),
        ):
            controller._revalidate_external_database_binding(
                expected,
                policy=policy,
            )

    def test_bridge_ledger_registry_is_consumed_by_runtime_validation(self) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        accepted = descriptor["bridge"]["policy"]["accepted_migration_ledgers"]
        manifest = descriptor["migrations"]["records"]
        for name, rows in (
            ("pre-0012", manifest[:-1]),
            ("post-0012", manifest),
            (
                "post-0013",
                [
                    *manifest,
                    CONTROLLER._bridge_core.FINAL_MIGRATION_RECORD,
                ],
            ),
        ):
            history = CONTROLLER.canonical_ledger_history(
                [
                    {
                        "version": record["version"],
                        "checksum": record["checksum"],
                    }
                    for record in rows
                ],
                manifest,
                accepted_ledgers=accepted,
                require_registry_match=True,
            )
            self.assertEqual(history, rows)
            compatibility = CONTROLLER.build_migration_compatibility_state(
                descriptor["bridge"]["policy"],
                code_manifest_sha256=(
                    F_MANIFEST_DIGEST
                    if name == "post-0013"
                    else B_MANIFEST_DIGEST
                ),
                migrations=history,
            )
            self.assertEqual(compatibility["ledger_state"]["name"], name)

        for rows in (
            [
                *manifest,
                {
                    **CONTROLLER._bridge_core.FINAL_MIGRATION,
                    "checksum": "f" * 64,
                },
            ],
            [
                *manifest,
                CONTROLLER._bridge_core.FINAL_MIGRATION,
                {"version": "0014_future", "checksum": "e" * 64},
            ],
        ):
            with self.assertRaises(CONTROLLER.PullDeployError):
                CONTROLLER.canonical_ledger_history(
                    rows,
                    manifest,
                    accepted_ledgers=accepted,
                    require_registry_match=True,
                )

    def test_b_state_can_truthfully_record_f_0013_ledger(self) -> None:
        descriptor = self.bridge_descriptor(self.controller())
        migrations = [
            *descriptor["migrations"]["records"],
            CONTROLLER._bridge_core.FINAL_MIGRATION_RECORD,
        ]
        compatibility = CONTROLLER.build_migration_compatibility_state(
            descriptor["bridge"]["policy"],
            code_manifest_sha256=B_MANIFEST_DIGEST,
            migrations=migrations,
        )
        self.assertEqual(
            compatibility["code_manifest_sha256"],
            B_MANIFEST_DIGEST,
        )
        self.assertEqual(
            compatibility["ledger_manifest_sha256"],
            F_MANIFEST_DIGEST,
        )
        self.assertEqual(
            compatibility["ledger_state"]["name"],
            "post-0013",
        )

    def test_bridge_state_cannot_delete_authority_and_downgrade_to_0011(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        _operation_root, descriptor_path, ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        descriptor_digest = CONTROLLER.sha256_file(descriptor_path)
        ready = CONTROLLER.load_private_json(ready_path)
        ready["descriptor_sha256"] = descriptor_digest
        CONTROLLER.atomic_json(ready_path, ready)

        state["descriptor_sha256"] = descriptor_digest
        state["migrations"] = json.loads(
            json.dumps(descriptor["migrations"]["records"])
        )
        state["migration_compatibility"] = (
            CONTROLLER.build_migration_compatibility_state(
                descriptor["bridge"]["policy"],
                code_manifest_sha256=B_MANIFEST_DIGEST,
                migrations=state["migrations"],
            )
        )
        state["external_database_audit"] = (
            external_database_audit_binding(controller.runtime_root)
        )
        state["external_database_transition_chain"] = (
            CONTROLLER.build_external_database_transition_chain(
                alias_reference={
                    "path": "/runtime/audit/alias-transition.json",
                    "sha256": "sha256:" + "1" * 64,
                    "identity_sha256": "sha256:" + "2" * 64,
                    "before_state_sha256": "sha256:" + "3" * 64,
                    "after_state_sha256": "sha256:" + "4" * 64,
                    "descriptor_sha256": "sha256:" + "5" * 64,
                    "operation_id": "alias-0005-fixture",
                    "kind": "alias-0005-reconciliation",
                },
                bridge_reference={
                    "path": "/runtime/audit/bridge-transition.json",
                    "sha256": "sha256:" + "6" * 64,
                    "identity_sha256": "sha256:" + "7" * 64,
                    "before_state_sha256": "sha256:" + "4" * 64,
                    "after_state_sha256": state[
                        "external_database_audit"
                    ]["state_sha256"],
                    "descriptor_sha256": descriptor_digest,
                    "operation_id": OPERATION_ID,
                    "kind": "bridge-expand-to-0011",
                },
                active_binding=state["external_database_audit"],
            )
        )
        controller._validate_state_source_descriptor(state)

        downgraded = json.loads(json.dumps(state))
        downgraded["migration_compatibility"] = None
        downgraded["migrations"] = downgraded["migrations"][:11]
        for field in (
            "contract_external_database_audit",
            "final_external_database_audit",
        ):
            downgraded.pop(field, None)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "compatibility presence differs",
        ):
            controller._validate_state_source_descriptor(downgraded)

        retained_compatibility = json.loads(json.dumps(state))
        retained_compatibility["migrations"] = (
            retained_compatibility["migrations"][:11]
        )
        retained_compatibility["migration_compatibility"] = (
            CONTROLLER.build_migration_compatibility_state(
                descriptor["bridge"]["policy"],
                code_manifest_sha256=B_MANIFEST_DIGEST,
                migrations=retained_compatibility["migrations"],
            )
        )
        retained_compatibility.pop("external_database_audit")
        retained_compatibility.pop("external_database_transition_chain")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lacks its external database authority",
        ):
            controller._validate_state_source_descriptor(
                retained_compatibility
            )

    def test_ordinary_successor_exactly_inherits_all_governance_authority(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        successor_operation = "deploy-governance-successor"
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=successor_operation,
        )
        successor = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=successor_operation,
        )
        controller._validate_state_source_descriptor(successor)

        mutations = {
            "approved_contracts": [{"foreign": True}],
            "migration_epoch_barrier": {"foreign": True},
            "schema_compatibility_floor": {"foreign": True},
            "last_contract_operation": "contract-foreign-0012",
            "contract_mutable_data_audit": {"foreign": True},
            "contract_external_database_audit": {"foreign": True},
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                changed = json.loads(json.dumps(successor))
                changed[field] = replacement
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    f"governance field {field}",
                ):
                    controller._validate_state_source_descriptor(
                        changed
                    )

        for field in (
            "external_database_audit",
            "external_database_transition_chain",
        ):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(successor))
                changed[field] = {"foreign": True}
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "external database authority",
                ):
                    controller._validate_state_source_descriptor(
                        changed
                    )

        changed = json.loads(json.dumps(successor))
        changed["final_mutable_data_audit"] = {"foreign": True}
        changed["final_external_database_audit"] = {"foreign": True}
        with self.assertRaises(CONTROLLER.PullDeployError):
            controller._validate_state_source_descriptor(changed)

    def test_bootstrap_state_rejects_unproven_final_0013_authority(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state["final_mutable_data_audit"] = {"foreign": True}
        state["final_external_database_audit"] = {"foreign": True}

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "unproven final 0013 authority",
        ):
            controller._validate_state_source_descriptor(state)

    def test_mutable_online_tables_are_sealed_before_and_after_apply(self) -> None:
        before = mutable_data_evidence()
        pair = CONTROLLER.build_mutable_data_pair(
            before,
            json.loads(json.dumps(before)),
        )
        self.assertEqual(pair["before"], pair["after"])

        for label, field, replacement in (
            ("row count", "row_count", 18),
            ("content", "content_sha256", "sha256:" + "f" * 64),
            ("schema", "schema_sha256", "sha256:" + "e" * 64),
        ):
            with self.subTest(label=label):
                after = json.loads(json.dumps(before))
                after["business_tables"][0][field] = replacement
                reseal_mutable_data_evidence(after)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "mutable business table changed",
                ):
                    CONTROLLER.build_mutable_data_pair(before, after)

    def test_bridge_mutable_pair_proves_exact_0008_to_0011_expansion(
        self,
    ) -> None:
        before = mutable_data_evidence(ledger_length=8)
        after = mutable_data_evidence(ledger_length=11)
        restored_pair = CONTROLLER.build_mutable_data_pair(
            before,
            json.loads(json.dumps(before)),
        )
        self.assertEqual(
            restored_pair["transition"]["control"]["mode"],
            "pre-0010-unchanged",
        )
        before_md = next(
            record
            for record in before["business_tables"]
            if record["schema"] == "md"
            and record["table"] == "monomer_md_jobs"
        )
        after_md = next(
            record
            for record in after["business_tables"]
            if record["schema"] == "md"
            and record["table"] == "monomer_md_jobs"
        )
        before_md["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_MD_SCHEMA_SHA256_BEFORE
        )
        after_md["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_MD_SCHEMA_SHA256_AFTER
        )
        after_md["content_sha256"] = "sha256:" + "0" * 64
        after_control = after["governed_controls"]["deployment_control"]
        after_control["table"]["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_DEPLOYMENT_CONTROL_SCHEMA_SHA256
        )
        after_control["row"].update(
            {
                "reason": f"post-canary drain {OPERATION_ID}",
                "activated_by": "pull-deploy-controller",
                "release_sha": TARGET_SHA,
            }
        )
        after_analytics = after["governed_controls"][
            "database_analytics_snapshots"
        ]
        after_analytics["table"]["schema_sha256"] = (
            CONTROLLER.BRIDGE_MUTABLE_ANALYTICS_SCHEMA_SHA256
        )
        reseal_mutable_data_evidence(before)
        reseal_mutable_data_evidence(after)
        descriptor_digest = "sha256:" + "9" * 64
        pair = CONTROLLER.build_bridge_mutable_data_pair(
            before,
            after,
            descriptor_sha256=descriptor_digest,
        )
        self.assertEqual(
            pair["transition"]["kind"],
            "bridge-expand-to-0011",
        )
        self.assertEqual(
            pair["transition"]["descriptor_sha256"],
            descriptor_digest,
        )

        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state["descriptor_sha256"] = descriptor_digest
        state["migrations"] = json.loads(
            json.dumps(B_MANIFEST_RECORDS[:11])
        )
        state["mutable_data_audit"] = pair
        state["database_backup"]["mutable_data_before_sha256"] = (
            pair["identity_sha256"]
        )
        accepted_ledgers = (
            CONTROLLER._bridge_core.expected_migration_registry(
                target_manifest_sha256=B_MANIFEST_DIGEST,
                target_records=B_MANIFEST_RECORDS,
                authority_manifest_sha256=F_MANIFEST_DIGEST,
                authority_records=[
                    *B_MANIFEST_RECORDS,
                    CONTROLLER._bridge_core.FINAL_MIGRATION_RECORD,
                ],
            )
        )
        state["migration_compatibility"] = (
            CONTROLLER.build_migration_compatibility_state(
                {
                    "policy_id": "sha256:" + "8" * 64,
                    "accepted_migration_ledgers": accepted_ledgers,
                },
                code_manifest_sha256=B_MANIFEST_DIGEST,
                migrations=state["migrations"],
            )
        )
        state["external_database_audit"] = (
            external_database_audit_binding(controller.runtime_root)
        )
        state["external_database_transition_chain"] = (
            CONTROLLER.build_external_database_transition_chain(
                alias_reference={
                    "path": "/runtime/audit/alias-transition.json",
                    "sha256": "sha256:" + "1" * 64,
                    "identity_sha256": "sha256:" + "2" * 64,
                    "before_state_sha256": "sha256:" + "3" * 64,
                    "after_state_sha256": "sha256:" + "4" * 64,
                    "descriptor_sha256": "sha256:" + "5" * 64,
                    "operation_id": "alias-0005-fixture",
                    "kind": "alias-0005-reconciliation",
                },
                bridge_reference={
                    "path": "/runtime/audit/bridge-transition.json",
                    "sha256": "sha256:" + "6" * 64,
                    "identity_sha256": "sha256:" + "7" * 64,
                    "before_state_sha256": "sha256:" + "4" * 64,
                    "after_state_sha256": state[
                        "external_database_audit"
                    ]["state_sha256"],
                    "descriptor_sha256": descriptor_digest,
                    "operation_id": OPERATION_ID,
                    "kind": "bridge-expand-to-0011",
                },
                active_binding=state["external_database_audit"],
            )
        )
        CONTROLLER.validate_current_deployment_state(state)

        changed = json.loads(json.dumps(after))
        changed["bridge_projection"]["content_sha256"] = (
            "sha256:" + "1" * 64
        )
        reseal_mutable_data_evidence(changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed existing business rows",
        ):
            CONTROLLER.build_bridge_mutable_data_pair(
                before,
                changed,
                descriptor_sha256=descriptor_digest,
            )

        leased = json.loads(json.dumps(after))
        leased["bridge_projection"]["lease_columns"][
            "non_null_counts"
        ]["worker_instance_id"] = 1
        reseal_mutable_data_evidence(leased)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "lease columns changed existing business rows",
        ):
            CONTROLLER.build_bridge_mutable_data_pair(
                before,
                leased,
                descriptor_sha256=descriptor_digest,
            )

        partial_control = json.loads(json.dumps(before))
        partial_control["governed_controls"]["deployment_control"] = (
            json.loads(
                json.dumps(
                    after["governed_controls"]["deployment_control"]
                )
            )
        )
        reseal_mutable_data_evidence(partial_control)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable-data audit evidence is invalid",
        ):
            CONTROLLER.validate_mutable_data_evidence(partial_control)

    def test_mutable_data_allows_only_exact_0012_exception(self) -> None:
        before = mutable_data_evidence(ledger_length=11)
        before_control = before["governed_controls"]["deployment_control"]
        before_control["row"].update(
            {
                "reason": f"0012 maintenance {OPERATION_ID}",
                "activated_by": "pull-contract-0012",
            }
        )
        reseal_mutable_data_evidence(before)
        after = mutable_data_evidence(ledger_length=12)
        after_control = after["governed_controls"]["deployment_control"]
        after_control["row"].update(
            {
                "reason": f"0012 maintenance {OPERATION_ID}",
                "activated_by": "pull-contract-0012",
                "updated_at": "2026-07-17T00:01:00Z",
            }
        )
        after_control["table"]["content_sha256"] = "sha256:" + "e" * 64
        reseal_mutable_data_evidence(after)

        pair = CONTROLLER.build_mutable_data_pair(before, after)

        self.assertEqual(pair["transition"]["kind"], "contract-0012")
        self.assertEqual(
            pair["transition"]["polytao_exception"]["row_count"],
            before["migration_exception"]["row_count"],
        )

        changed = json.loads(json.dumps(after))
        changed["business_tables"][0]["content_sha256"] = "sha256:" + "f" * 64
        reseal_mutable_data_evidence(changed)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable business table changed",
        ):
            CONTROLLER.build_mutable_data_pair(before, changed)

    def test_mutable_data_allows_only_pristine_0013_expansion(self) -> None:
        before = mutable_data_evidence(ledger_length=12)
        after = mutable_data_evidence(ledger_length=13)

        pair = CONTROLLER.build_mutable_data_pair(before, after)

        self.assertEqual(pair["transition"]["kind"], "expand-0013")
        self.assertEqual(
            pair["transition"]["dft_relations"],
            sorted(CONTROLLER.MUTABLE_DATA_BUSINESS_TABLES[-3:]),
        )

        nonempty = json.loads(json.dumps(after))
        nonempty["business_tables"][-1]["row_count"] = 1
        reseal_mutable_data_evidence(nonempty)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "empty DFT business relation",
        ):
            CONTROLLER.build_mutable_data_pair(before, nonempty)

        advanced = json.loads(json.dumps(after))
        advanced["sequences"][-1]["is_called"] = True
        reseal_mutable_data_evidence(advanced)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "DFT creation evidence is not pristine",
        ):
            CONTROLLER.build_mutable_data_pair(before, advanced)

    def test_mutable_data_rejects_static_control_and_analytics_drift(self) -> None:
        before = mutable_data_evidence()
        mutations = (
            (
                "static",
                "static import tables changed",
                lambda value: value["static_tables"][0].update(
                    content_sha256="sha256:" + "f" * 64
                ),
            ),
            (
                "control",
                "current operation",
                lambda value: value["governed_controls"][
                    "deployment_control"
                ]["row"].update(reason="pull deployment another-operation"),
            ),
            (
                "analytics",
                "analytics snapshots changed",
                lambda value: value["governed_controls"][
                    "database_analytics_snapshots"
                ]["table"].update(content_sha256="sha256:" + "f" * 64),
            ),
        )
        for label, message, mutate in mutations:
            with self.subTest(label=label):
                after = json.loads(json.dumps(before))
                mutate(after)
                reseal_mutable_data_evidence(after)
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    message,
                ):
                    CONTROLLER.build_mutable_data_pair(before, after)

    def test_mutable_helper_is_descriptor_bound_and_asset_rebuild_is_empty(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["release_input"]["datasets_on_asset_change"],
            [],
        )
        self.assertEqual(
            descriptor["mutable_data"]["helper_sha256"],
            descriptor["production_config"][
                "deployment_mutable_data_audit_sha256"
            ],
        )
        descriptor["mutable_data"]["helper_sha256"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable-data helper differs",
        ):
            CONTROLLER.validate_descriptor(descriptor)

        pgpass = controller.config_dir / CONTROLLER.MUTABLE_DATA_PGPASS
        pgpass.write_text(
            (
                "127.0.0.1:55432:nexpoly:"
                "nexpoly_mutable_audit:changed\n"
            ),
            encoding="utf-8",
        )
        os.chmod(pgpass, 0o600)
        self.assertNotEqual(
            controller.mutable_data_contract(),
            descriptor["mutable_data"],
        )
        pgpass.unlink()
        pgpass.symlink_to(controller.config_dir / "app.env")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "dependency is unsafe",
        ):
            controller.mutable_data_contract()

    def test_mutable_connection_rejects_indirection_and_wrong_audit_identity(
        self,
    ) -> None:
        passfile = Path("/private/mutable-data-audit.pgpass")
        canonical = (
            "[nexpoly-mutable-audit]\n"
            "host=127.0.0.1\n"
            "port=55432\n"
            "dbname=nexpoly\n"
            "user=nexpoly_mutable_audit\n"
            "sslmode=disable\n"
            f"passfile={passfile}\n"
        ).encode()
        pgpass = (
            b"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:secret\\:value\n"
        )
        self.assertEqual(
            CONTROLLER.validate_mutable_data_connection_inputs(
                canonical,
                pgpass,
                expected_passfile=passfile,
            )["user"],
            "nexpoly_mutable_audit",
        )
        for service in (
            canonical + b"include=/tmp/redirect.conf\n",
            canonical.replace(b"host=127.0.0.1", b"host=localhost"),
            canonical.replace(
                b"passfile=/private/mutable-data-audit.pgpass",
                b"servicefile=/tmp/other.conf",
            ),
        ):
            with self.subTest(service=service):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "one exact loopback audit endpoint",
                ):
                    CONTROLLER.validate_mutable_data_connection_inputs(
                        service,
                        pgpass,
                        expected_passfile=passfile,
                    )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "does not match",
        ):
            CONTROLLER.validate_mutable_data_connection_inputs(
                canonical,
                b"127.0.0.1:55432:nexpoly:postgres:secret\n",
                expected_passfile=passfile,
            )

    def test_same_system_identifier_clone_cannot_satisfy_mutable_audit(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        expected = FakeLifecycle().postgres_runtime_identity(
            controller, descriptor
        )
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "postgres_runtime_fence": expected,
            },
        )

        class CloneLifecycle(FakeLifecycle):
            def __init__(self) -> None:
                super().__init__()
                self.captures = 0

            def postgres_runtime_identity(
                self, target_controller: object, target_descriptor: object
            ) -> dict[str, object]:
                result = super().postgres_runtime_identity(
                    target_controller, target_descriptor
                )
                self.captures += 1
                if self.captures == 2:
                    result["container_id"] = "c" * 64
                return result

        controller.lifecycle = CloneLifecycle()
        with mock.patch.object(
            controller.runner,
            "run",
            return_value=subprocess.CompletedProcess(
                ["deployment-mutable-data-audit"],
                0,
                json.dumps(mutable_data_evidence()),
                "",
            ),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact PostgreSQL container",
        ):
            CONTROLLER.PullDeployController._capture_mutable_data(
                controller,
                descriptor,
            )

    def test_bridge_source_switch_uses_exact_policy_ref_not_remote_main(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        commands: list[tuple[object, ...]] = []

        def fake_git(*arguments, **_kwargs):  # type: ignore[no-untyped-def]
            commands.append(arguments)
            if arguments[:3] == ("show-ref", "--verify", "--hash"):
                return subprocess.CompletedProcess([], 1, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        controller._git = fake_git  # type: ignore[method-assign]
        controller.repository_identity = lambda **_kwargs: {  # type: ignore[method-assign]
            "sha": TARGET_SHA,
            "tree": TARGET_TREE,
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
        }
        CONTROLLER.PullDeployController._switch_source(controller, descriptor)
        merge = next(command for command in commands if command[0] == "merge")
        self.assertEqual(
            merge,
            ("merge", "--ff-only", f"refs/nexpoly/bridge-target/{TARGET_SHA}"),
        )

    def test_first_bridge_restore_inherits_lock_and_seals_every_cas(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        sealed = descriptor["legacy_takeover"]
        unit = descriptor["monomer_md"]["systemd_unit"]
        target_unit = Path(unit["target_path"])
        target_unit.write_bytes(Path(unit["candidate_path"]).read_bytes())
        os.chmod(target_unit, 0o600)
        status = {
            "apply_phase": "complete",
            "restore_phase": None,
            "active": False,
            "classification_sha256": sealed["classification_sha256"],
            "runtime_identity_sha256": sealed["runtime_identity_sha256"],
            "git_identity": sealed["git_identity"],
            "pre_stopped_fence_sha256": sealed[
                "pre_stopped_fence_sha256"
            ],
            "pre_stopped_fence": {
                "worker_unit_seal_sha256": "sha256:" + "2" * 64,
            },
            "control_layout_sha256": sealed[
                "control_layout_sha256"
            ],
            "checkout_permissions_sha256": sealed[
                "checkout_permissions_sha256"
            ],
            "applied_record_sha256": sealed[
                "applied_record_sha256"
            ],
        }
        restored = {
            **{
                key: sealed[key]
                for key in (
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
                )
            },
            "control_layout_replacement_sha256": "sha256:" + "3" * 64,
            "checkout_permissions_replacement_sha256": "sha256:" + "4" * 64,
            "restored_terminal_sha256": "sha256:" + "5" * 64,
            "binding_sha256": "sha256:" + "6" * 64,
        }
        marker = {
            "descriptor_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(descriptor) + b"\n"
            ),
            "database_change_started": False,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "runtime_stopped": True,
            "updated_at": CONTROLLER.utc_now(),
        }
        captured: dict[str, object] = {}

        def invoke(
            command: list[str],
            *,
            deploy_lock_fd: int,
        ) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured["fd"] = deploy_lock_fd
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"fixture": "terminal"}),
                "",
            )

        controller._held_deploy_lock_fd = 91
        controller._run_legacy_takeover_restore = invoke  # type: ignore[method-assign]
        with (
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                side_effect=[None, restored],
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "validate_install_manifest",
                return_value={"fixture": True},
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "load_status",
                return_value=status,
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "snapshot_current_control_layout",
                return_value={"sha256": "sha256:" + "3" * 64},
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "snapshot_current_checkout_permissions",
                return_value={"sha256": "sha256:" + "4" * 64},
            ),
            mock.patch.object(
                CONTROLLER._legacy_takeover_evidence,
                "validate_status_document",
                return_value={"fixture": "terminal"},
            ),
        ):
            result = controller._restore_legacy_takeover(
                descriptor,
                marker,
            )
        controller._held_deploy_lock_fd = None

        self.assertEqual(result, restored)
        self.assertEqual(captured["fd"], 91)
        command = captured["command"]
        self.assertEqual(command[-2:], ["--parent-deploy-lock-fd", "91"])
        self.assertEqual(
            marker["takeover_restore_started"],
            {
                "operation_id": sealed["operation_id"],
                "worker_unit_sha256": descriptor["monomer_md"][
                    "systemd_unit"
                ]["sha256"],
                "control_layout_sha256": "sha256:" + "3" * 64,
                "checkout_permissions_sha256": "sha256:" + "4" * 64,
                "started_at": marker["takeover_restore_started"][
                    "started_at"
                ],
            },
        )
        self.assertEqual(
            marker["takeover_restored_terminal_sha256"],
            restored["restored_terminal_sha256"],
        )
        self.assertFalse(marker["runtime_stopped"])

    def test_takeover_subprocess_receives_only_inherited_lock_fd(
        self,
    ) -> None:
        controller = self.controller()
        completed = subprocess.CompletedProcess(
            ["restore"],
            0,
            "{}",
            "",
        )
        with mock.patch.object(
            CONTROLLER.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.assertEqual(
                controller._run_legacy_takeover_restore(
                    ["/private/nexpoly-legacy-takeover", "restore"],
                    deploy_lock_fd=17,
                ),
                completed,
            )
        self.assertEqual(run.call_args.kwargs["pass_fds"], (17,))
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], CONTROLLER.SAFE_PATH)
        self.assertNotIn("SSH_AUTH_SOCK", run.call_args.kwargs["env"])

    def test_lost_takeover_restore_response_precedes_git_reconciliation(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored = {
            "restored_terminal_sha256": "sha256:" + "7" * 64,
        }
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"][
                "executor_control"
            ],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
            mock.patch.object(
                controller,
                "_reconcile_effect_commit_windows",
                side_effect=AssertionError(
                    "Git reconciliation must not run after restore"
                ),
            ),
        ):
            with controller.deployment_lock():
                self.assertIsNone(controller.recover_interrupted())
        self.assertFalse(controller.marker_path.exists())
        terminal_dir = (
            controller.runtime_root
            / "legacy-takeover"
            / "runtime"
            / "pull-terminal"
            / OPERATION_ID
        )
        outcome = CONTROLLER.load_private_json(
            terminal_dir / "operation-state.json"
        )
        self.assertEqual(outcome["outcome"], "failed")
        self.assertEqual(
            controller._load_operation_state(OPERATION_ID),
            outcome,
        )
        self.assertFalse(
            (controller.audit_dir / OPERATION_ID).exists()
        )
        token = CONTROLLER._bridge_core.load_token_authority(
            controller.state_dir
        )
        self.assertEqual(token["status"], "retired-precommit")
        self.assertEqual(
            token["retirement"]["restored_terminal_sha256"],
            restored["restored_terminal_sha256"],
        )

    def test_failed_first_bridge_recovery_crash_matrix_preserves_order(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored = {
            "restored_terminal_sha256": "sha256:" + "7" * 64,
        }
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        terminal_dir = (
            controller.runtime_root
            / "legacy-takeover"
            / "runtime"
            / "pull-terminal"
            / OPERATION_ID
        )
        audit_path = terminal_dir / "recovered-takeover-restore.json"
        state_path = terminal_dir / "operation-state.json"

        def recover_with_published_crash(method_name: str) -> None:
            original = getattr(controller, method_name)

            def publish_then_crash(*args, **kwargs):  # type: ignore[no-untyped-def]
                original(*args, **kwargs)
                raise RuntimeError(f"injected {method_name} response loss")

            with (
                mock.patch.object(
                    controller,
                    "_load_prepared",
                    return_value=(descriptor, descriptor_digest),
                ),
                mock.patch.object(
                    controller,
                    "_probe_restored_legacy_takeover",
                    return_value=restored,
                ),
                mock.patch.object(
                    controller,
                    method_name,
                    side_effect=publish_then_crash,
                ),
                self.assertRaisesRegex(RuntimeError, "response loss"),
                controller.deployment_lock(),
            ):
                controller.recover_interrupted()

        recover_with_published_crash("_finalize_restored_legacy_takeover")
        self.assertTrue(controller.marker_path.exists())
        self.assertFalse(audit_path.exists())
        self.assertFalse(state_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

        recover_with_published_crash("_audit_attempt")
        self.assertTrue(audit_path.exists())
        self.assertFalse(state_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

        recover_with_published_crash("_record_operation_outcome")
        self.assertTrue(state_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

        recover_with_published_crash("_retire_failed_first_bridge_token")
        self.assertTrue(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "retired-precommit",
        )

        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
            controller.deployment_lock(),
        ):
            self.assertIsNone(controller.recover_interrupted())
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "retired-precommit",
        )

    def test_restored_bridge_recovery_bypasses_only_checkout_permissions(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored_terminal = "sha256:" + "7" * 64
        restored = {"restored_terminal_sha256": restored_terminal}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        capsule = self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        os.chmod(controller.production_root, 0o775)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "owner-controlled"
        ):
            controller.ensure_roots(mutating=True)

        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
        ):
            result = controller.recover_restored_first_bridge(
                authority_sha=descriptor["bridge"]["authority"]["sha"],
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
                capsule_sha256=capsule["capsule_sha256"],
                descriptor_sha256=descriptor_digest,
                restored_terminal_sha256=restored_terminal,
            )
        self.assertTrue(result["apply"])
        self.assertEqual(result["token_status"], "retired-precommit")
        self.assertFalse(controller.marker_path.exists())

    def test_restored_bridge_recovery_rejects_wrong_content_address(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored_terminal = "sha256:" + "7" * 64
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        capsule = self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        os.chmod(controller.production_root, 0o775)
        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value={
                    "restored_terminal_sha256": restored_terminal
                },
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "terminal identity differs"
            ),
        ):
            controller.recover_restored_first_bridge(
                authority_sha=descriptor["bridge"]["authority"]["sha"],
                target_sha=TARGET_SHA,
                operation_id=OPERATION_ID,
                capsule_sha256=capsule["capsule_sha256"],
                descriptor_sha256=descriptor_digest,
                restored_terminal_sha256="sha256:" + "8" * 64,
            )
        self.assertTrue(controller.marker_path.exists())
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "prepared",
        )

    def test_successor_generation_requires_untampered_failed_restore_chain(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        descriptor_digest = self.bind_bridge_token(controller, descriptor)
        restored = {
            "restored_terminal_sha256": "sha256:" + "7" * 64,
        }
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": False,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": False,
            "database_change_started": False,
        }
        self.bind_bridge_recovery_capsule(
            controller, descriptor, descriptor_digest, marker
        )
        CONTROLLER.atomic_json(controller.marker_path, marker)
        with (
            mock.patch.object(
                controller,
                "_load_prepared",
                return_value=(descriptor, descriptor_digest),
            ),
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
            controller.deployment_lock(),
        ):
            controller.recover_interrupted()

        terminal_dir = (
            controller.runtime_root
            / "legacy-takeover"
            / "runtime"
            / "pull-terminal"
            / OPERATION_ID
        )
        audit_path = terminal_dir / "recovered-takeover-restore.json"
        original_audit = audit_path.read_bytes()
        operation, _descriptor_path, _ready_path = (
            controller._operation_paths(OPERATION_ID)
        )
        shutil.rmtree(operation)
        with (
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=restored,
            ),
        ):
            successor_authority = (
                controller._bridge_token_successor_authority()
            )
        self.assertIsNotNone(successor_authority)

        audit_path.write_bytes(original_audit.replace(b"recovered", b"tampered!", 1))
        os.chmod(audit_path, 0o600)
        with (
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "terminal audit authority differs"
            ),
        ):
            controller._bridge_token_successor_authority()
        audit_path.write_bytes(original_audit)
        os.chmod(audit_path, 0o600)

        successor = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id="deploy-20260716-0002",
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            token=b"bridge-token-fixture-entropy-0002",
            predecessor_retirement=successor_authority,
        )
        self.assertEqual(successor["generation"], 2)
        self.assertEqual(successor["status"], "reserved")

    def test_first_bridge_pre_stopped_rollback_never_drains_again(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        descriptor = self.bridge_descriptor(controller)
        marker = {
            "database_change_started": False,
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "updated_at": CONTROLLER.utc_now(),
        }
        with (
            mock.patch.object(
                controller,
                "_probe_restored_legacy_takeover",
                return_value=None,
            ),
            mock.patch.object(
                controller,
                "_reconcile_effect_commit_windows",
            ),
            mock.patch.object(
                controller,
                "_restore_previous_asset_pointer",
            ),
            mock.patch.object(
                controller,
                "_restore_legacy_takeover",
            ) as restore,
        ):
            controller._rollback_first_bridge(descriptor, marker)
        restore.assert_called_once_with(descriptor, marker)
        self.assertEqual(lifecycle.events, [])

    def test_bridge_commit_intent_crash_finishes_exact_current_state(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        # First build a structurally valid governed state using the existing
        # v2 fixture lifecycle; bridge recovery only changes its descriptor
        # authority before committing it through the v3 token.
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        token = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            token=b"bridge-token-fixture-entropy-0001",
        )
        descriptor["bridge"]["token"] = {
            "token_id": token["token_id"],
            "token_sha256": token["token_sha256"],
        }
        CONTROLLER.validate_descriptor(descriptor)
        descriptor_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(descriptor) + b"\n"
        )
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            descriptor_sha256=descriptor_digest,
        )
        state["descriptor_sha256"] = descriptor_digest
        state["external_database_audit"] = descriptor[
            "external_database_audit"
        ]
        state["external_database_transition_chain"] = (
            CONTROLLER.build_external_database_transition_chain(
                alias_reference={
                    "path": "/runtime/audit/alias-transition.json",
                    "sha256": "sha256:" + "1" * 64,
                    "identity_sha256": "sha256:" + "2" * 64,
                    "before_state_sha256": "sha256:" + "3" * 64,
                    "after_state_sha256": "sha256:" + "4" * 64,
                    "descriptor_sha256": "sha256:" + "5" * 64,
                    "operation_id": "alias-0005-fixture",
                    "kind": "alias-0005-reconciliation",
                },
                bridge_reference={
                    "path": "/runtime/audit/bridge-transition.json",
                    "sha256": "sha256:" + "6" * 64,
                    "identity_sha256": "sha256:" + "7" * 64,
                    "before_state_sha256": "sha256:" + "4" * 64,
                    "after_state_sha256": state[
                        "external_database_audit"
                    ]["state_sha256"],
                    "descriptor_sha256": descriptor_digest,
                    "operation_id": OPERATION_ID,
                    "kind": "bridge-expand-to-0011",
                },
                active_binding=state["external_database_audit"],
            )
        )
        CONTROLLER.validate_current_deployment_state(state)
        candidate_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(state) + b"\n"
        )
        CONTROLLER._bridge_core.begin_state_commit(
            controller.state_dir,
            operation_id=OPERATION_ID,
            descriptor_sha256=descriptor_digest,
            candidate_state_sha256=candidate_digest,
        )
        controller.current_state_path.unlink()
        marker = {
            "phase": "state-commit-started",
            "candidate_state": state,
            "candidate_state_sha256": candidate_digest,
        }
        with (
            mock.patch.object(
                controller,
                "_validate_external_database_state_provenance",
                side_effect=CONTROLLER.validate_current_deployment_state,
            ),
            mock.patch.object(
                controller,
                "_revalidate_bridge_candidate_database_state",
            ) as initial_revalidation,
            mock.patch.object(
                controller,
                "_consume_bridge_token",
                side_effect=OSError("lost token-consume response"),
            ),
            self.assertRaisesRegex(OSError, "lost token-consume response"),
        ):
            controller._candidate_current_state(
                descriptor,
                descriptor_digest,
                marker,
            )
        initial_revalidation.assert_called_once_with(
            descriptor, state, include_mutable=True
        )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "commit-intent",
        )
        self.assertEqual(
            CONTROLLER.sha256_file(controller.current_state_path),
            candidate_digest,
        )

        with (
            mock.patch.object(
                controller,
                "_validate_external_database_state_provenance",
                side_effect=CONTROLLER.validate_current_deployment_state,
            ),
            mock.patch.object(
                controller,
                "_revalidate_bridge_candidate_database_state",
                side_effect=CONTROLLER.PullDeployError(
                    "candidate mutable data changed before commit"
                ),
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "mutable data changed",
            ),
        ):
            controller._candidate_current_state(
                descriptor,
                descriptor_digest,
                marker,
            )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "commit-intent",
        )

        with (
            mock.patch.object(
                controller,
                "_validate_external_database_state_provenance",
                side_effect=CONTROLLER.validate_current_deployment_state,
            ),
            mock.patch.object(
                controller,
                "_revalidate_bridge_candidate_database_state",
            ) as revalidate_database,
        ):
            recovered = controller._candidate_current_state(
                descriptor,
                descriptor_digest,
                marker,
            )
        revalidate_database.assert_called_once_with(
            descriptor, state, include_mutable=True
        )
        self.assertEqual(recovered, state)
        self.assertEqual(
            recovered["external_database_audit"]["identity_sha256"],
            descriptor["external_database_audit"]["identity_sha256"],
        )
        self.assertEqual(
            CONTROLLER.sha256_file(controller.current_state_path),
            candidate_digest,
        )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "consumed",
        )

    def test_pending_contract_marker_blocks_every_code_deployment_command(self) -> None:
        controller = self.controller()
        CONTROLLER.atomic_json(
            controller.contract_marker_path,
            {"operation_id": "contract-0012-20260716"},
        )
        actions = (
            lambda: controller.plan(target_sha=TARGET_SHA, operation_id=OPERATION_ID),
            lambda: controller.prepare(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            ),
            lambda: controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID),
            lambda: controller.rollback(operation_id=OPERATION_ID),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError, "0012 maintenance"
                ):
                    action()

    def test_active_controller_seals_once_then_hands_prepare_to_target_controller(
        self,
    ) -> None:
        controller = self.controller()
        captured: list[tuple[str, list[str], dict[str, str]]] = []

        def capture(path, argv, environment):  # type: ignore[no-untyped-def]
            captured.append((path, argv, environment))
            raise RuntimeError("exec captured")

        controller.control_environment = lambda: {}  # type: ignore[method-assign]
        with mock.patch.object(CONTROLLER.os, "execve", capture):
            with self.assertRaisesRegex(RuntimeError, "exec captured"):
                controller._handoff_prepare_to_target_controller(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )
        handoff_path = controller.control_handoffs_dir / f"{OPERATION_ID}.json"
        first = CONTROLLER.load_private_json(handoff_path)
        first_digest = CONTROLLER.sha256_file(handoff_path)
        with mock.patch.object(CONTROLLER.os, "execve", capture):
            with self.assertRaisesRegex(RuntimeError, "exec captured"):
                controller._handoff_prepare_to_target_controller(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )
        self.assertEqual(CONTROLLER.load_private_json(handoff_path), first)
        self.assertEqual(CONTROLLER.sha256_file(handoff_path), first_digest)
        self.assertEqual(len(captured), 2)
        for _path, argv, environment in captured:
            self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-B"])
            self.assertEqual(
                Path(argv[3]).parent.name,
                first["executor_control"]["release_id"],
            )
            self.assertEqual(
                environment["NEXPOLY_PREPARE_HANDOFF_SHA256"], first_digest
            )

    def test_control_pointer_accepts_only_sealed_previous_or_candidate(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        previous = controller.active_control_evidence()
        candidate = controller._activate_control(descriptor)
        self.assertEqual(candidate["generation"], previous["generation"] + 1)
        controller._restore_previous_control(descriptor)
        self.assertEqual(controller.active_control_evidence(), previous)
        foreign = dict(previous)
        foreign["operation_id"] = "foreign-controls-0001"
        CONTROLLER.atomic_json(controller.active_control_path, foreign)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "neither sealed"):
            controller._activate_control(descriptor)

    def test_slot_recycling_detects_venv_python_via_bounded_proc_cmdline(self) -> None:
        controller = self.controller()
        slot_root = controller.venv_root / "md-a"
        binary = slot_root / "venv/bin/python"
        binary.parent.mkdir(parents=True, mode=0o700)
        binary.symlink_to(Path(sys.executable).resolve())
        process = subprocess.Popen(
            [str(binary), "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "inactive slot is still used",
            ):
                controller._assert_slot_not_running(slot_root)
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_prepare_operation_owner_is_published_atomically_and_recovers_staging(
        self,
    ) -> None:
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        staging = operation.parent / f".{operation.name}.preparing"
        staging.mkdir(mode=0o700)
        with controller.deployment_lock():
            attempt = controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        self.assertEqual(attempt, 1)
        self.assertFalse(staging.exists())
        owner = CONTROLLER.load_private_json(
            operation / "prepare-owner.json"
        )
        self.assertEqual(owner["operation_id"], OPERATION_ID)

        crash_operation_id = "deploy-prepare-quarantine-crash"
        crash_operation, _descriptor, _ready = (
            controller._operation_paths(crash_operation_id)
        )
        crash_staging = (
            crash_operation.parent
            / f".{crash_operation.name}.preparing"
        )
        crash_staging.mkdir(mode=0o700)
        with (
            controller.deployment_lock(),
            mock.patch.object(
                CONTROLLER.shutil,
                "rmtree",
                side_effect=OSError("injected quarantine crash"),
            ),
            self.assertRaisesRegex(OSError, "quarantine crash"),
        ):
            controller._open_prepare_operation(
                crash_operation,
                operation_id=crash_operation_id,
                target_sha=TARGET_SHA,
            )
        self.assertTrue(
            (
                crash_staging.parent
                / f"{crash_staging.name}.discard"
            ).exists()
        )
        with controller.deployment_lock():
            self.assertEqual(
                controller._open_prepare_operation(
                    crash_operation,
                    operation_id=crash_operation_id,
                    target_sha=TARGET_SHA,
                ),
                1,
            )

        other_operation = "deploy-prepare-foreign-owner"
        foreign, _descriptor, _ready = controller._operation_paths(
            other_operation
        )
        foreign_staging = (
            foreign.parent / f".{foreign.name}.preparing"
        )
        foreign_staging.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            foreign_staging / "prepare-owner.json",
            {
                "schema_version": 1,
                "operation_id": "deploy-foreign-owner",
                "target_sha": TARGET_SHA,
                "controller_sha256": controller.controller_digest(),
                "created_at": CONTROLLER.utc_now(),
            },
        )
        with (
            controller.deployment_lock(),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "belongs to another operation",
            ),
        ):
            controller._open_prepare_operation(
                foreign,
                operation_id=other_operation,
                target_sha=TARGET_SHA,
            )

    def test_prepare_operation_accepts_only_exact_lost_rename_response(
        self,
    ) -> None:
        controller = self.controller()
        operation_id = "deploy-prepare-lost-rename"
        operation, _descriptor, _ready = controller._operation_paths(
            operation_id
        )
        original = CONTROLLER.rename_directory_noreplace
        lost = False

        def lose_response(source, target):  # type: ignore[no-untyped-def]
            nonlocal lost
            result = original(source, target)
            if target == operation and not lost:
                lost = True
                raise OSError("injected lost prepare rename response")
            return result

        with (
            controller.deployment_lock(),
            mock.patch.object(
                CONTROLLER,
                "rename_directory_noreplace",
                side_effect=lose_response,
            ),
        ):
            attempt = controller._open_prepare_operation(
                operation,
                operation_id=operation_id,
                target_sha=TARGET_SHA,
            )
        self.assertTrue(lost)
        self.assertEqual(attempt, 1)
        self.assertEqual(
            CONTROLLER.load_private_json(
                operation / "prepare-owner.json"
            )["operation_id"],
            operation_id,
        )

    def test_real_md_staging_recovers_ownerless_dirs_and_lost_cache_rename(
        self,
    ) -> None:
        identity_digest = "sha256:" + "9" * 64
        deploy_env = self.runtime / "config/deploy.env"
        write_private(
            deploy_env,
            deploy_env.read_text(encoding="utf-8")
            + (
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256="
                + identity_digest
                + "\n"
            ),
        )

        class WheelRunner:
            def run(
                self, command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if "download" in command:
                    destination = Path(command[command.index("--dest") + 1])
                    wheel = destination / "fixture-1.0-py3-none-any.whl"
                    wheel.write_bytes(b"sealed wheel\n")
                    os.chmod(wheel, 0o600)
                elif "venv" in command:
                    venv = Path(command[-1])
                    (venv / "bin").mkdir(parents=True, mode=0o700)
                    python = venv / "bin/python"
                    python.write_bytes(b"fixture python\n")
                    os.chmod(python, 0o700)
                return subprocess.CompletedProcess(command, 0, "", "")

        controller = self.controller(runner=WheelRunner())
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        lock_payload = (
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
        )
        lock_sha = CONTROLLER.sha256_bytes(lock_payload)
        cache_key = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(
                {
                    "worker_lock_sha256": lock_sha,
                    "base_python_identity_sha256": identity_digest,
                    "platform": sys.platform,
                }
            )
        )
        cache = controller.wheel_cache_dir / cache_key
        wheel_staging = (
            controller.wheel_cache_dir
            / f".{cache_key}.staging-{OPERATION_ID}"
        )
        wheel_staging.mkdir(mode=0o700)
        slot_staging = (
            controller.venv_root
            / f".md-a.preparing-{OPERATION_ID}"
        )
        slot_staging.mkdir(mode=0o700)
        original = CONTROLLER.rename_directory_noreplace
        lost = False

        def lose_cache_response(source, target):  # type: ignore[no-untyped-def]
            nonlocal lost
            result = original(source, target)
            if target == cache and not lost:
                lost = True
                raise OSError("injected lost wheel rename response")
            return result

        identity = {
            "resolved_path": str(Path(sys.executable).resolve()),
            "identity_sha256": identity_digest,
        }
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_base_python_identity",
                return_value=identity,
            ),
            mock.patch.object(
                CONTROLLER,
                "shared_inspect_base_python_identity",
                return_value=identity,
            ),
            mock.patch.object(
                CONTROLLER,
                "rename_directory_noreplace",
                side_effect=lose_cache_response,
            ),
        ):
            record = (
                CONTROLLER.PullDeployController.prepare_md_slot(
                    controller,
                    operation_id=OPERATION_ID,
                    target_sha=TARGET_SHA,
                    target_tree=TARGET_TREE,
                    lock_payload=lock_payload,
                )
            )
        self.assertTrue(lost)
        self.assertTrue((cache / "READY.json").is_file())
        self.assertFalse(wheel_staging.exists())
        self.assertFalse(slot_staging.exists())
        self.assertFalse(
            (
                controller.venv_root
                / f"md-{record['slot']}"
                / ".preparing.json"
            ).exists()
        )
        self.assertEqual(
            record["wheel_inventory_sha256"],
            CONTROLLER.directory_inventory_digest(cache),
        )

    def test_real_md_staging_rejects_foreign_wheel_and_slot_owners(
        self,
    ) -> None:
        identity_digest = "sha256:" + "9" * 64
        deploy_env = self.runtime / "config/deploy.env"
        write_private(
            deploy_env,
            deploy_env.read_text(encoding="utf-8")
            + (
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256="
                + identity_digest
                + "\n"
            ),
        )
        controller = self.controller()
        operation, _descriptor, _ready = controller._operation_paths(
            OPERATION_ID
        )
        with controller.deployment_lock():
            controller._open_prepare_operation(
                operation,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
            )
        lock_payload = (
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
        )
        lock_sha = CONTROLLER.sha256_bytes(lock_payload)
        cache_key = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(
                {
                    "worker_lock_sha256": lock_sha,
                    "base_python_identity_sha256": identity_digest,
                    "platform": sys.platform,
                }
            )
        )
        wheel = (
            controller.wheel_cache_dir
            / f".{cache_key}.staging-{OPERATION_ID}"
        )
        wheel.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            wheel / ".owner.json",
            {
                "schema_version": 1,
                "operation_id": "deploy-foreign-owner",
                "wheel_cache_key": cache_key,
                "worker_lock_sha256": lock_sha,
                "base_python_identity_sha256": identity_digest,
            },
        )
        identity = {
            "resolved_path": str(Path(sys.executable).resolve()),
            "identity_sha256": identity_digest,
        }
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_base_python_identity",
                return_value=identity,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "another operation",
            ),
        ):
            CONTROLLER.PullDeployController.prepare_md_slot(
                controller,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=lock_payload,
            )

        shutil.rmtree(wheel)
        cache = controller.wheel_cache_dir / cache_key
        cache.mkdir(mode=0o700)
        owner = CONTROLLER.PullDeployController._wheel_cache_owner(
            operation_id=OPERATION_ID,
            wheel_cache_key=cache_key,
            worker_lock_sha256=lock_sha,
            base_python_identity_sha256=identity_digest,
        )
        CONTROLLER.atomic_json(cache / ".owner.json", owner)
        payload_path = cache / "fixture-1.0-py3-none-any.whl"
        payload_path.write_bytes(b"sealed wheel\n")
        os.chmod(payload_path, 0o600)
        payload = CONTROLLER.wheel_payload_inventory(cache)
        CONTROLLER.atomic_json(
            cache / "READY.json",
            {
                "schema_version": 1,
                "status": "ready",
                "operation_id": OPERATION_ID,
                "wheel_cache_key": cache_key,
                "worker_lock_sha256": lock_sha,
                "base_python_identity_sha256": identity_digest,
                "payload_file_count": len(payload["files"]),
                "payload_inventory_sha256": payload[
                    "inventory_sha256"
                ],
                "ready_at": CONTROLLER.utc_now(),
            },
        )
        slot_staging = (
            controller.venv_root
            / f".md-a.preparing-{OPERATION_ID}"
        )
        slot_staging.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            slot_staging / ".preparing.json",
            {
                "schema_version": 1,
                "operation_id": "deploy-foreign-owner",
                "slot": "a",
            },
        )
        with (
            controller.deployment_lock(),
            mock.patch.object(
                controller,
                "_base_python_identity",
                return_value=identity,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "slot staging belongs to another operation",
            ),
        ):
            CONTROLLER.PullDeployController.prepare_md_slot(
                controller,
                operation_id=OPERATION_ID,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=lock_payload,
            )

    def test_active_slot_uses_a_b_values_canonical_digest_and_no_current_symlink(
        self,
    ) -> None:
        controller = self.controller()
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"fixture-lock\n",
        )
        descriptor = {
            "operation_id": OPERATION_ID,
            "monomer_md": {
                "slot": record["slot"],
                "slot_record": record,
                "slot_record_sha256": CONTROLLER.canonical_json_digest(record),
            },
        }
        active = controller._activate_slot(descriptor)
        self.assertIn(active["slot"], {"a", "b"})
        self.assertEqual(
            active["slot_record_sha256"], CONTROLLER.canonical_json_digest(record)
        )
        self.assertFalse((self.runtime / "worker-venvs/md-current").exists())
        self.assertEqual(controller.choose_inactive_slot(), "b")

    def test_prepare_seals_descriptor_and_apply_rejects_tampering(self) -> None:
        controller = self.controller()
        prepared = controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(prepared["status"], "ready")
        descriptor_path = (
            self.runtime / "state/prepared" / OPERATION_ID / "descriptor.json"
        )
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        self.assertEqual(descriptor["repository"]["target_tree"], TARGET_TREE)
        descriptor["compose"]["sha256"] = "sha256:" + "0" * 64
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "READY record differs"):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

    def test_new_prepare_accepts_token_rotation_but_apply_rejects_same_operation_drift(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        token = self.runtime / "config/github-api-token"
        write_private(token, "rotated-token\n")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "production configuration changed after prepare",
        ):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

        # A distinct operation may intentionally seal the rotated credential.
        next_operation = "deploy-20260716-0002"
        next_controller = self.controller()
        prepared = next_controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=next_operation,
        )
        self.assertEqual(prepared["status"], "ready")

    def test_slots_rotate_a_b_a(self) -> None:
        controller = self.controller()

        def prepare_and_activate(operation: str, payload: bytes) -> dict[str, object]:
            record = controller.prepare_md_slot(
                operation_id=operation,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=payload,
            )
            CONTROLLER.atomic_json(
                controller.active_slot_path,
                {
                    "schema_version": CONTROLLER.ACTIVE_SLOT_SCHEMA_VERSION,
                    "component": "monomer-md",
                    "slot": record["slot"],
                    "source_sha": TARGET_SHA,
                    "source_tree": TARGET_TREE,
                    "worker_lock_sha256": record["worker_lock_sha256"],
                    "slot_record_sha256": CONTROLLER.worker_record_digest(record),
                    "operation_id": operation,
                    "activated_at": CONTROLLER.utc_now(),
                },
            )
            return record

        first = prepare_and_activate("deploy-round-a1", b"one\n")
        second = prepare_and_activate("deploy-round-b2", b"two\n")
        third = prepare_and_activate("deploy-round-a3", b"three\n")
        self.assertEqual(
            [first["slot"], second["slot"], third["slot"]], ["a", "b", "a"]
        )


class StrictLifecycleEvidenceTests(unittest.TestCase):
    def test_test_root_mode_rejects_parent_child_overlap_with_production(self) -> None:
        with mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"}):
            for runtime_root, production_root in (
                (CONTROLLER.RUNTIME_ROOT / "child", Path("/tmp/isolated-source")),
                (CONTROLLER.RUNTIME_ROOT.parent, Path("/tmp/isolated-source")),
                (Path("/tmp/isolated-runtime"), CONTROLLER.PRODUCTION_ROOT / "child"),
                (Path("/tmp/shared/root/runtime"), Path("/tmp/shared/root")),
            ):
                with (
                    self.subTest(
                        runtime_root=runtime_root, production_root=production_root
                    ),
                    self.assertRaisesRegex(
                        CONTROLLER.PullDeployError, "forbidden for production paths"
                    ),
                ):
                    CONTROLLER.test_root_mode(
                        runtime_root=runtime_root,
                        production_root=production_root,
                    )

    def active_payload(self, *, version: int = 1) -> dict[str, object]:
        fields = (
            CONTROLLER.ACTIVE_JOB_FIELDS_V1
            if version == 1
            else CONTROLLER.ACTIVE_JOB_FIELDS_V2
        )
        return {
            "active_jobs_schema_version": version,
            "drain": {
                "enabled": True,
                "reason": "fixture drain",
                "release_sha": TARGET_SHA,
                "activated_at": CONTROLLER.utc_now(),
                "activated_by": "pull-deploy-controller",
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": {name: 0 for name in fields},
            "active_total": 0,
        }

    def persistent_payload(
        self, *, version: int | None = None
    ) -> dict[str, object]:
        selected = 1 if version is None else version
        fields = (
            CONTROLLER.PERSISTENT_JOB_FIELDS_V1
            if selected == 1
            else CONTROLLER.PERSISTENT_JOB_FIELDS_V2
        )
        payload: dict[str, object] = {
            "drain": {
                "enabled": False,
                "reason": None,
                "release_sha": None,
                "activated_at": None,
                "activated_by": None,
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": {name: 0 for name in fields},
            "active_total": 0,
        }
        if version is not None:
            payload["active_jobs_schema_version"] = version
        return payload

    def test_persistent_jobs_accepts_legacy_v1_and_explicit_v2(self) -> None:
        legacy = self.persistent_payload()
        explicit_v1 = self.persistent_payload(version=1)
        explicit_v2 = self.persistent_payload(version=2)
        for payload in (legacy, explicit_v1, explicit_v2):
            with self.subTest(payload=payload):
                self.assertEqual(
                    CONTROLLER.validate_persistent_drain_evidence(payload),
                    payload,
                )

    def test_persistent_jobs_rejects_ambiguous_or_mismatched_versions(self) -> None:
        implicit_v2 = self.persistent_payload(version=2)
        implicit_v2.pop("active_jobs_schema_version")
        explicit_v1_with_v2 = self.persistent_payload(version=2)
        explicit_v1_with_v2["active_jobs_schema_version"] = 1
        unsupported = self.persistent_payload(version=2)
        unsupported["active_jobs_schema_version"] = 3
        for payload in (implicit_v2, explicit_v1_with_v2, unsupported):
            with self.subTest(payload=payload), self.assertRaises(
                CONTROLLER.PullDeployError
            ):
                CONTROLLER.validate_persistent_drain_evidence(payload)

    def test_active_jobs_rejects_unknown_categories_and_boolean_counts(self) -> None:
        payload = self.active_payload()
        payload["active_jobs"]["unexpected"] = 0  # type: ignore[index]
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.validate_active_jobs_evidence(payload, require_drained=True)
        payload = self.active_payload()
        payload["active_jobs"]["monomer_md"] = False  # type: ignore[index]
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.validate_active_jobs_evidence(payload, require_drained=True)

    def test_worker_resume_requires_accepting_zero_work(self) -> None:
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "did not resume"):
            CONTROLLER.validate_worker_control_evidence(
                {
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-1",
                },
                action="resume",
                require_zero=True,
            )

    def test_worker_unchanged_resume_allows_capacity_full_active_job(self) -> None:
        evidence = CONTROLLER.validate_worker_control_evidence(
            {
                "status": "ready",
                "accepting_jobs": False,
                "active_jobs": 1,
                "worker_instance_id": "worker-1",
            },
            action="resume-unchanged",
            require_zero=False,
        )
        self.assertEqual(evidence["active_jobs"], 1)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "did not resume unchanged"
        ):
            CONTROLLER.validate_worker_control_evidence(
                {
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-1",
                },
                action="resume-unchanged",
                require_zero=False,
            )

    def test_backend_resume_rejects_a_still_drained_response(self) -> None:
        payload = self.active_payload()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "differs from the expected admission",
        ):
            CONTROLLER.validate_active_jobs_evidence(
                payload,
                require_drained=False,
                require_resumed=True,
            )

    def test_transport_runtime_identity_is_exact_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="pull-worker-identity-"
        ) as raw:
            root = Path(raw)
            production = root / "source"
            python = root / "venv/bin/python"
            production.mkdir()
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable).resolve())
            controller = SimpleNamespace(production_root=production)
            slot_record = {
                "slot": "a",
                "venv_prefix": str(root / "venv"),
                "worker_lock_sha256": DIGEST_A,
                "base_python_identity_sha256": DIGEST_B,
            }
            descriptor = {
                "repository": {
                    "target_sha": TARGET_SHA,
                    "target_tree": TARGET_TREE,
                },
                "monomer_md": {
                    "slot_record": slot_record,
                    "slot_record_sha256": DIGEST_A,
                },
            }
            worker: dict[str, object] = {
                "status": "ok",
                "mode": "real",
                "source_sha": TARGET_SHA,
                "source_tree": TARGET_TREE,
                "source_root": str(production),
                "venv_slot": "a",
                "venv_prefix": str(root / "venv"),
                "worker_lock_sha256": DIGEST_A,
                "slot_record_sha256": DIGEST_A,
                "base_python_identity_sha256": DIGEST_B,
                "python_executable": str(python.resolve(strict=True)),
                "db_configured": True,
                "runtime_ready": True,
                "max_active_jobs": 1,
                "default_steps": 300,
                "max_steps": 300,
                "cuda_visible_devices": "2",
                "gpu_broker_enabled": False,
                "active_jobs": 0,
                "accepting_jobs": True,
                "draining": False,
                "worker_instance_id": "worker-fixture",
                "protocols": {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": True,
                        "runtime_error": None,
                    }
                },
            }
            lifecycle = CONTROLLER.SystemLifecycle()
            self.assertIs(
                lifecycle._validate_worker_runtime_identity(
                    controller,
                    descriptor,
                    worker,
                    expected_accepting=True,
                ),
                worker,
            )

            invalid_protocols = (
                None,
                [],
                {},
                {"Transport": None},
                {"Transport": []},
                {
                    "Transport": {
                        "supported": False,
                        "runtime_ready": True,
                        "runtime_error": None,
                    }
                },
                {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": False,
                        "runtime_error": None,
                    }
                },
                {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": True,
                    }
                },
                {
                    "Transport": {
                        "supported": True,
                        "runtime_ready": True,
                        "runtime_error": "private runtime detail",
                    }
                },
            )
            for protocols in invalid_protocols:
                with (
                    self.subTest(protocols=protocols),
                    self.assertRaises(CONTROLLER.PullDeployError),
                ):
                    lifecycle._validate_worker_runtime_identity(
                        controller,
                        descriptor,
                        {**worker, "protocols": protocols},
                        expected_accepting=True,
                    )


class SystemDrainFencingTests(unittest.TestCase):
    class Harness(CONTROLLER.SystemLifecycle):
        def __init__(self) -> None:
            self.backend_statuses: list[dict[str, object]] = []
            self.backend_processes: list[dict[str, object]] = []
            self.socket_sets: list[list[tuple[str, Path]]] = []
            self.worker_health: list[dict[str, object]] = []
            self.identity_checks = 0

        @staticmethod
        def _next(values):  # type: ignore[no-untyped-def]
            if len(values) > 1:
                return values.pop(0)
            return values[0]

        def _backend_active_status(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
            return self._next(self.backend_statuses)

        def _backend_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
            return self._next(self.backend_processes)

        def _worker_sockets(self, _controller, *, require_md=False):  # type: ignore[no-untyped-def]
            del require_md
            return self._next(self.socket_sets)

        def _worker_request(self, _controller, _socket, *, method, endpoint):  # type: ignore[no-untyped-def]
            self.assert_request(method, endpoint)
            return self._next(self.worker_health)

        @staticmethod
        def assert_request(method: str, endpoint: str) -> None:
            if (method, endpoint) != ("GET", "/health"):
                raise AssertionError((method, endpoint))

        def _validate_worker_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            self.identity_checks += 1
            return {}

    @staticmethod
    def backend_status(
        active: int, *, actor: str = "pull-deploy-controller"
    ) -> dict[str, object]:
        counts = {name: 0 for name in CONTROLLER.ACTIVE_JOB_FIELDS_V1}
        counts["monomer_md"] = active
        return {
            "active_jobs_schema_version": 1,
            "drain": {
                "enabled": True,
                "reason": "fixture drain",
                "release_sha": TARGET_SHA,
                "activated_at": CONTROLLER.utc_now(),
                "activated_by": actor,
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": counts,
            "active_total": active,
        }

    @staticmethod
    def worker_health(active: int) -> dict[str, object]:
        return {
            "status": "ok",
            "accepting_jobs": False,
            "draining": True,
            "active_jobs": active,
            "worker_instance_id": "worker-fixed",
        }

    @staticmethod
    def descriptor() -> dict[str, object]:
        return {"repository": {"target_sha": TARGET_SHA}}

    def configured(self) -> "SystemDrainFencingTests.Harness":
        harness = self.Harness()
        process = {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "pid": 123,
            "started_at": "2026-07-17T00:00:00Z",
            "restart_count": 0,
        }
        harness.backend_statuses = [
            self.backend_status(1),
            self.backend_status(0),
        ]
        harness.backend_processes = [process, process]
        harness.socket_sets = [
            [("monomer-md", Path("/fixture/md.sock"))],
            [("monomer-md", Path("/fixture/md.sock"))],
        ]
        harness.worker_health = [self.worker_health(1), self.worker_health(0)]
        return harness

    def test_wait_allows_work_to_finish_while_fencing_exact_instances(self) -> None:
        harness = self.configured()
        with mock.patch.object(CONTROLLER.time, "sleep", return_value=None):
            evidence = harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )
        self.assertEqual(evidence["backend"]["active_total"], 0)
        self.assertEqual(harness.identity_checks, 2)

    def test_wait_rejects_foreign_drain_owner_and_instance_or_socket_drift(
        self,
    ) -> None:
        harness = self.configured()
        harness.backend_statuses[0] = self.backend_status(1, actor="other")
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "ownership"):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.worker_health[1] = {
            **harness.worker_health[1],
            "worker_instance_id": "worker-restarted",
        }
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(CONTROLLER.PullDeployError, "instance changed"),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.backend_processes[1] = {**harness.backend_processes[0], "pid": 999}
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "Backend instance changed"
            ),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.socket_sets[1] = [
            ("monomer-md", Path("/fixture/md.sock")),
            ("monomer-dft", Path("/fixture/dft.sock")),
        ]
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(CONTROLLER.PullDeployError, "socket set changed"),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

    def test_resume_unchanged_accepts_full_capacity_without_restarting(self) -> None:
        process = {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "pid": 123,
            "started_at": "2026-07-17T00:00:00Z",
            "restart_count": 0,
        }
        worker_process = {
            "main_pid": 456,
            "invocation_id": "worker-invocation",
            "active_enter_monotonic": 789,
        }

        class ResumeHarness(CONTROLLER.SystemLifecycle):
            def __init__(self) -> None:
                self.requests: list[tuple[str, str]] = []
                self.health_reads = 0
                self.backend_reads = 0
                self.mutate_final = False
                self.control_called = False

            def _environment(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {}

            def postgres_runtime_identity(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "schema_version": 1,
                    **mutable_data_evidence()["postgres_runtime"],
                    "captured_at": CONTROLLER.utc_now(),
                }

            def _isolate_ingress(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return None

            def prepare_recovery_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "runtime_state": "drained",
                    "ingress_isolated": True,
                    "verification": {
                        "health": "ok",
                        "recovery_fence": {
                            "backend_process": process,
                            "monomer_md_process": worker_process,
                            "workers": {
                                "monomer-md": {
                                    "socket": "/fixture/md.sock",
                                    "worker_instance_id": "worker-fixed",
                                }
                            },
                        },
                    },
                }

            def _backend_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
                self.backend_reads += 1
                if self.mutate_final and self.backend_reads >= 2:
                    return {**process, "pid": 999}
                return process

            def _worker_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
                return worker_process

            def _worker_sockets(self, _controller, *, require_md=False):  # type: ignore[no-untyped-def]
                del require_md
                return [("monomer-md", Path("/fixture/md.sock"))]

            def _worker_request(self, _controller, _socket, *, method, endpoint):  # type: ignore[no-untyped-def]
                self.requests.append((method, endpoint))
                return {
                    "status": "ready",
                    "accepting_jobs": True,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-fixed",
                }

            def _validate_worker_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {}

            def verify_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {}

            def _capture_runtime_recovery_fence(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                backend = self._backend_process_identity(None, None)
                return {
                    "backend_process": backend,
                    "monomer_md_process": worker_process,
                    "workers": {
                        "monomer-md": {
                            "socket": "/fixture/md.sock",
                            "worker_instance_id": "worker-fixed",
                        }
                    },
                }

            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return self.control_called

            def verify_open_runtime(self, _controller, _descriptor, verification):  # type: ignore[no-untyped-def]
                if (
                    self._expected_runtime_recovery_fence(verification)[
                        "backend_process"
                    ]
                    != process
                ):
                    raise AssertionError("unexpected final fence")

            def _control_cli(self, _controller, _descriptor, *arguments):  # type: ignore[no-untyped-def]
                self.assertEqual(arguments[0], "resume")
                self.control_called = True
                counts = {name: 0 for name in CONTROLLER.PERSISTENT_JOB_FIELDS_V1}
                return {
                    "drain": {
                        "enabled": False,
                        "reason": None,
                        "release_sha": None,
                        "activated_at": None,
                        "activated_by": None,
                        "updated_at": CONTROLLER.utc_now(),
                    },
                    "active_jobs": counts,
                    "active_total": 0,
                }

            @staticmethod
            def assertEqual(left, right):  # type: ignore[no-untyped-def]
                if left != right:
                    raise AssertionError((left, right))

        class Runner:
            @staticmethod
            def run(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return subprocess.CompletedProcess([], 0, "", "")

        controller = type(
            "Controller",
            (),
            {
                "runner": Runner(),
                "production_root": Path("/fixture/source"),
                "config_dir": Path("/fixture/config"),
                "control_environment": lambda _self: {},
            },
        )()
        descriptor = {
            "repository": {"target_sha": TARGET_SHA},
            "_expected_postgres_runtime_identity": {
                "schema_version": 1,
                **mutable_data_evidence()["postgres_runtime"],
            },
        }
        harness = ResumeHarness()
        persisted: list[dict[str, object]] = []
        harness.resume_unchanged(controller, descriptor, persisted.append)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["recovery_fence"]["backend_process"], process)
        self.assertEqual(
            harness.requests,
            [("POST", "/resume")],
        )
        self.assertTrue(harness.control_called)

        restarted = ResumeHarness()
        restarted.mutate_final = True
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "changed"):
            restarted.resume_unchanged(controller, descriptor, lambda _value: None)
        self.assertFalse(restarted.control_called)

    def test_open_runtime_recovery_rejects_instance_different_from_marker(self) -> None:
        expected = {
            "backend_process": {
                "container_id": "a" * 64,
                "image_id": "sha256:" + "b" * 64,
                "pid": 123,
                "started_at": "2026-07-17T00:00:00Z",
                "restart_count": 0,
            },
            "monomer_md_process": {
                "main_pid": 456,
                "invocation_id": "worker-invocation",
                "active_enter_monotonic": 789,
            },
            "workers": {
                "monomer-md": {
                    "socket": "/fixture/md.sock",
                    "worker_instance_id": "worker-fixed",
                }
            },
        }

        class OpenHarness(CONTROLLER.SystemLifecycle):
            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return True

            def verify_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {
                    "containers": {"backend": {"container_id": "a" * 64}},
                    "worker": {"worker_instance_id": "worker-fixed"},
                }

            def _capture_runtime_recovery_fence(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {
                    **expected,
                    "backend_process": {
                        **expected["backend_process"],
                        "pid": 999,
                    },
                }

        class Runner:
            @staticmethod
            def run(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return None

        controller = type(
            "Controller",
            (),
            {"runner": Runner(), "control_environment": lambda _self: {}},
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            OpenHarness().verify_open_runtime(
                controller,
                {},
                {"recovery_fence": expected},
            )

    def test_resume_keeps_ingress_isolated_until_backend_and_fence_are_open(
        self,
    ) -> None:
        fence = {
            "backend_process": {
                "container_id": "a" * 64,
                "image_id": "sha256:" + "b" * 64,
                "pid": 123,
                "started_at": "2026-07-17T00:00:00Z",
                "restart_count": 0,
            },
            "monomer_md_process": {
                "main_pid": 456,
                "invocation_id": "worker-invocation",
                "active_enter_monotonic": 789,
            },
            "workers": {
                "monomer-md": {
                    "socket": "/fixture/md.sock",
                    "worker_instance_id": "worker-fixed",
                }
            },
        }

        class OrderingHarness(CONTROLLER.SystemLifecycle):
            def __init__(
                self,
                *,
                fail_final: bool = False,
                drift_postgres_after_ingress: bool = False,
            ) -> None:
                self.events: list[str] = []
                self.fail_final = fail_final
                self.drift_postgres_after_ingress = (
                    drift_postgres_after_ingress
                )
                self.backend_open = False
                self.postgres_reads = 0

            def _environment(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {}

            def postgres_runtime_identity(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.postgres_reads += 1
                runtime = dict(mutable_data_evidence()["postgres_runtime"])
                if (
                    self.drift_postgres_after_ingress
                    and self.postgres_reads >= 3
                ):
                    runtime["container_id"] = "f" * 64
                return {
                    "schema_version": 1,
                    **runtime,
                    "captured_at": CONTROLLER.utc_now(),
                }

            def _isolate_ingress(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("isolate-ingress")

            def _capture_runtime_recovery_fence(self, *_args, resumed, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append(f"capture-{resumed}")
                return fence

            def _worker_sockets(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return [("monomer-md", Path("/fixture/md.sock"))]

            def _worker_request(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("worker-resume")
                return {
                    "status": "ready",
                    "accepting_jobs": True,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-fixed",
                }

            def _control_cli(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("backend-resume")
                self.backend_open = True
                counts = {name: 0 for name in CONTROLLER.PERSISTENT_JOB_FIELDS_V1}
                return {
                    "drain": {
                        "enabled": False,
                        "reason": None,
                        "release_sha": None,
                        "activated_at": None,
                        "activated_by": None,
                        "updated_at": CONTROLLER.utc_now(),
                    },
                    "active_jobs": counts,
                    "active_total": 0,
                }

            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("admission-status")
                return self.backend_open

            def verify_runtime_identity(self, *_args, **kwargs):  # type: ignore[no-untyped-def]
                if kwargs.get("require_ingress") is not False:
                    raise AssertionError("internal verification exposed ingress")
                self.events.append("verify-internal")
                return {}

            def verify_open_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("verify-open")
                if self.fail_final:
                    raise CONTROLLER.PullDeployError(
                        "injected final open verification failure"
                    )

            def _compose(self, _controller, *arguments):  # type: ignore[no-untyped-def]
                return ["compose", *arguments]

        class Runner:
            def __init__(self, lifecycle: OrderingHarness) -> None:
                self.lifecycle = lifecycle

            def run(self, command, **_kwargs):  # type: ignore[no-untyped-def]
                if "up" in command and command[-1] == "nginx":
                    if not self.lifecycle.backend_open:
                        raise AssertionError("nginx started before Backend admission")
                    self.lifecycle.events.append("nginx-start")
                return subprocess.CompletedProcess(command, 0, "", "")

        descriptor = {
            "repository": {"target_sha": TARGET_SHA},
            "_expected_postgres_runtime_identity": {
                "schema_version": 1,
                **mutable_data_evidence()["postgres_runtime"],
            },
        }
        verification = {"health": "ok", "recovery_fence": fence}
        lifecycle = OrderingHarness()
        controller = type(
            "Controller",
            (),
            {
                "runner": Runner(lifecycle),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        lifecycle.resume(controller, descriptor, verification)
        self.assertEqual(
            lifecycle.events,
            [
                "isolate-ingress",
                "capture-False",
                "worker-resume",
                "capture-True",
                "backend-resume",
                "admission-status",
                "verify-internal",
                "capture-True",
                "nginx-start",
                "verify-open",
            ],
        )

        failing = OrderingHarness(fail_final=True)
        failing_controller = type(
            "Controller",
            (),
            {
                "runner": Runner(failing),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "final open verification"
        ):
            failing.resume(failing_controller, descriptor, verification)
        self.assertEqual(failing.events[-1], "isolate-ingress")

        drifting = OrderingHarness(drift_postgres_after_ingress=True)
        drifting_controller = type(
            "Controller",
            (),
            {
                "runner": Runner(drifting),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "PostgreSQL identity changed while resuming ingress",
        ):
            drifting.resume(
                drifting_controller,
                descriptor,
                verification,
            )
        self.assertEqual(drifting.events[-1], "isolate-ingress")


class LifecycleStateMachineTests(PullDeployTestCase):
    def test_pre_stop_rollback_validates_previous_before_resume(
        self,
    ) -> None:
        previous = {"fixture": "previous-state"}
        fake = SimpleNamespace(
            lifecycle=mock.Mock(),
            _is_pre_stop_abort_marker=mock.Mock(return_value=True),
            _validate_steady_deployment_state=mock.Mock(
                side_effect=CONTROLLER.PullDeployError(
                    "injected previous terminal failure"
                )
            ),
            _previous_runtime_descriptor=mock.Mock(),
            _recover_unchanged_and_resume=mock.Mock(),
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "previous terminal failure",
        ):
            CONTROLLER.PullDeployController._rollback_failed_attempt(
                fake,
                {"previous_deployment": previous},
                {},
            )
        fake._validate_steady_deployment_state.assert_called_once_with(
            previous
        )
        fake._previous_runtime_descriptor.assert_not_called()
        fake._recover_unchanged_and_resume.assert_not_called()

    def test_stopped_start_unknown_commit_recovers_authorized_new_instance(
        self,
    ) -> None:
        class LostStartLifecycle(FakeLifecycle):
            lose_start = True

            def start(self, controller, _descriptor):  # type: ignore[no-untyped-def]
                persisted = CONTROLLER.load_private_json(controller.marker_path)
                self.assert_postgres_fence(persisted)
                self._event("start")
                self.runtime_state = "live"
                self.admission_open = False
                self.recovery_fence = {"fixture_instance": "started-instance"}
                if self.lose_start:
                    self.lose_start = False
                    raise CONTROLLER.PullDeployError("injected start response loss")

            @staticmethod
            def assert_postgres_fence(marker):  # type: ignore[no-untyped-def]
                expected = CONTROLLER.postgres_runtime_fence_identity(
                    {
                        "schema_version": 1,
                        **mutable_data_evidence()["postgres_runtime"],
                        "captured_at": marker["postgres_runtime_fence"][
                            "captured_at"
                        ],
                    }
                )
                actual = CONTROLLER.postgres_runtime_fence_identity(
                    marker["postgres_runtime_fence"]
                )
                if actual != expected:
                    raise AssertionError(
                        "PostgreSQL runtime fence was not durable before start"
                    )

        lifecycle = LostStartLifecycle()
        lifecycle.runtime_state = "stopped"
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "phase": "state-committed",
            "updated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)

        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "start response loss"):
            controller._recover_runtime_and_resume(
                marker,
                descriptor,
                allow_unfenced=False,
            )
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            persisted["runtime_start_intent"]["target_sha"],
            TARGET_SHA,
        )
        self.assertEqual(
            CONTROLLER.postgres_runtime_fence_identity(
                persisted["postgres_runtime_fence"]
            ),
            {
                "schema_version": 1,
                **mutable_data_evidence()["postgres_runtime"],
            },
        )
        self.assertNotIn("verification", persisted)

        lifecycle.events.clear()
        controller._recover_runtime_and_resume(
            persisted,
            descriptor,
            allow_unfenced=False,
        )
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "verify", "resume"],
        )
        final_marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertNotIn("runtime_start_intent", final_marker)
        self.assertEqual(
            final_marker["verification"]["recovery_fence"],
            {"fixture_instance": "started-instance"},
        )

    def test_apply_uses_prepared_evidence_and_commits_state_after_verification(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(state["source_sha"], TARGET_SHA)
        self.assertEqual(controller.source_sha, TARGET_SHA)
        self.assertEqual(
            lifecycle.events,
            [
                "drain",
                "recovery-isolate",
                "recovery-redrain",
                "stop",
                "backup",
                "migrate",
                "start",
                "verify",
                "resume",
            ],
        )
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        self.assertTrue(
            (self.runtime / "audit" / OPERATION_ID / "success.json").is_file()
        )
        current = CONTROLLER.load_private_json(
            self.runtime / "state/current-deployment.json"
        )
        self.assertEqual(current["active_monomer_md_slot"]["slot"], "a")
        self.assertEqual(
            [record["version"] for record in current["migrations"]],
            [record["version"] for record in B_MANIFEST_RECORDS[:11]],
        )

    def test_current_state_cas_rejects_drift_and_accepts_exact_write_response_loss(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        previous = controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        previous_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        candidate = json.loads(json.dumps(previous))
        candidate["deployed_at"] = "2026-07-18T12:00:00Z"
        candidate_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(candidate) + b"\n"
        )
        original_atomic_json = CONTROLLER.atomic_json
        lost = False

        def lose_response(path, value, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal lost
            result = original_atomic_json(path, value, *args, **kwargs)
            if Path(path) == controller.current_state_path and not lost:
                lost = True
                raise OSError("injected lost state write response")
            return result

        with (
            mock.patch.object(
                CONTROLLER,
                "atomic_json",
                side_effect=lose_response,
            ),
            self.assertRaisesRegex(OSError, "lost state write response"),
        ):
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=previous_digest,
            )
        self.assertEqual(
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=previous_digest,
            ),
            "already-committed",
        )

        pretty_payload = (
            json.dumps(previous, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        controller.current_state_path.write_bytes(pretty_payload)
        os.chmod(controller.current_state_path, 0o600)
        with controller.current_state_path.open("rb") as stream:
            os.fsync(stream.fileno())
        CONTROLLER.fsync_directory(
            controller.current_state_path.parent
        )
        pretty_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        self.assertNotEqual(pretty_digest, previous_digest)
        self.assertEqual(
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=pretty_digest,
            ),
            "committed",
        )

        CONTROLLER.atomic_json(controller.current_state_path, previous)
        drifted = json.loads(json.dumps(previous))
        drifted["deployed_at"] = "2026-07-18T12:00:01Z"
        CONTROLLER.atomic_json(controller.current_state_path, drifted)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed before commit",
        ):
            controller._commit_current_state_cas(
                candidate,
                candidate_sha256=candidate_digest,
                expected_pre_state=previous,
                expected_pre_state_sha256=previous_digest,
            )

    def test_normal_apply_reloads_pre_state_after_long_transaction(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        successor = "deploy-normal-state-cas-drift"
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=successor
        )
        original_revalidate = (
            controller._revalidate_candidate_database_state
        )
        drifted: dict[str, object] | None = None

        def drift_before_commit(descriptor, state, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal drifted
            result = original_revalidate(
                descriptor, state, **kwargs
            )
            if state.get("operation_id") == successor:
                drifted = CONTROLLER.load_private_json(
                    controller.current_state_path
                )
                drifted["deployed_at"] = "2026-07-18T12:00:02Z"
                CONTROLLER.atomic_json(
                    controller.current_state_path, drifted
                )
            return result

        with (
            mock.patch.object(
                controller,
                "_revalidate_candidate_database_state",
                side_effect=drift_before_commit,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "state commit is ambiguous",
            ),
        ):
            controller.apply(
                target_sha=TARGET_SHA, operation_id=successor
            )
        self.assertEqual(
            CONTROLLER.load_private_json(
                controller.current_state_path
            ),
            drifted,
        )
        self.assertTrue(controller.marker_path.exists())

    def test_explicit_rollback_reloads_source_state_before_commit(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        controller.apply(
            target_sha=TARGET_SHA, operation_id=OPERATION_ID
        )
        successor = "deploy-rollback-state-cas-drift"
        controller.prepare(
            target_sha=TARGET_SHA, operation_id=successor
        )
        controller.apply(
            target_sha=TARGET_SHA, operation_id=successor
        )
        original_revalidate = (
            controller._revalidate_candidate_database_state
        )
        drifted: dict[str, object] | None = None

        def drift_before_commit(descriptor, state, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal drifted
            result = original_revalidate(
                descriptor, state, **kwargs
            )
            if state.get("rollback_provenance") is not None:
                drifted = CONTROLLER.load_private_json(
                    controller.current_state_path
                )
                drifted["deployed_at"] = "2026-07-18T12:00:03Z"
                CONTROLLER.atomic_json(
                    controller.current_state_path, drifted
                )
            return result

        with (
            mock.patch.object(
                controller,
                "_revalidate_candidate_database_state",
                side_effect=drift_before_commit,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "changed before commit",
            ),
        ):
            controller.rollback(operation_id=successor)
        self.assertEqual(
            CONTROLLER.load_private_json(
                controller.current_state_path
            ),
            drifted,
        )
        marker = CONTROLLER.load_private_json(
            controller.marker_path
        )
        self.assertEqual(
            marker["current_state_precondition_sha256"],
            marker["rollback_current_state_sha256"],
        )

    def test_steady_state_requires_outcome_and_exact_success_audit(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        operation_state = (
            controller.audit_dir / OPERATION_ID / "operation-state.json"
        )
        operation_state.unlink()
        CONTROLLER.fsync_directory(operation_state.parent)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "deployed terminal outcome",
        ):
            controller._validate_steady_deployment_state(
                state,
                revalidate_live=False,
            )

        controller._record_operation_outcome(
            operation_id=OPERATION_ID,
            descriptor_sha256=state["descriptor_sha256"],
            outcome="deployed",
        )
        success = controller.audit_dir / OPERATION_ID / "success.json"
        success.unlink()
        CONTROLLER.fsync_directory(success.parent)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact immutable success audit",
        ):
            controller._validate_steady_deployment_state(
                state,
                revalidate_live=False,
            )

    def test_steady_state_rejects_replayed_pre_0012_success_state(self) -> None:
        controller = self.controller()
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        contract_operation = "contract-0012-replay-fixture"
        contract_root = (
            controller.runtime_root / "state" / "contract-operations"
        )
        contract_root.mkdir(mode=0o700)
        CONTROLLER.atomic_json(
            contract_root / f"{contract_operation}.json",
            {
                "schema_version": 2,
                "status": "success",
                "operation_id": contract_operation,
                "deployment_operation_id": OPERATION_ID,
                "pull_descriptor_sha256": state["descriptor_sha256"],
            },
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "omits an already successful 0012",
        ):
            controller._validate_steady_deployment_state(
                state,
                revalidate_live=False,
            )

    def test_failure_is_rolled_back_and_audited_without_leaving_marker(self) -> None:
        lifecycle = FakeLifecycle(fail_at="start")
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected start"):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertTrue(controller.rollback_called)
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        failed = CONTROLLER.load_private_json(
            self.runtime / "audit" / OPERATION_ID / "failed.json"
        )
        self.assertEqual(failed["rollback"], "success")

    def test_committed_state_crash_recovers_forward_instead_of_restoring_database(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": CONTROLLER.load_private_json(descriptor_path)[
                "controller"
            ]["executor_control"],
            "executor_control_sha256": CONTROLLER.load_private_json(descriptor_path)[
                "controller"
            ]["executor_control_sha256"],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": lifecycle.verification(),
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.events.clear()
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered, state)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "verify", "resume"],
        )
        self.assertFalse(controller.marker_path.exists())

    def test_open_admission_unknown_commit_keeps_marker_on_instance_drift(self) -> None:
        initial = FakeLifecycle()
        controller = self.controller(lifecycle=initial)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        verification = {"health": "ok", "recovery_fence": {"fixture": True}}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": verification,
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)

        class RestartedOpenLifecycle(FakeLifecycle):
            def verify_open_runtime(
                self,
                _controller: object,
                _descriptor: object,
                expected_verification: object | None = None,
            ) -> None:
                self._event("verify-open-restarted")
                if expected_verification != verification:
                    raise AssertionError(expected_verification)
                raise CONTROLLER.PullDeployError(
                    "open runtime instance differs from committed verification"
                )

        restarted = RestartedOpenLifecycle(admission_open=True)
        controller.lifecycle = restarted
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(
            restarted.events,
            ["recovery-isolate"],
        )
        self.assertNotIn("stop", restarted.events)
        self.assertNotIn("start", restarted.events)

    def test_candidate_reverify_fence_survives_lost_resume_response(self) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": {
                "health": "ok",
                "recovery_fence": {"fixture_instance": "stale-instance"},
            },
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.admission_open = False
        lifecycle.runtime_state = "stopped"
        lifecycle.recovery_fence = {"fixture_instance": "reverified-instance"}
        lifecycle.lose_next_resume = True
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost response after admission commit"
        ):
            controller.recover_interrupted()
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        lifecycle.events.clear()
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered, state)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "verify", "resume"],
        )
        self.assertFalse(controller.marker_path.exists())
        self.assertNotIn("stop", lifecycle.events)

    def test_admission_resumed_recovery_only_verifies_open_runtime(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "admission-resumed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": lifecycle.verification(),
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.events.clear()

        recovered = controller.recover_interrupted()

        self.assertEqual(recovered, state)
        self.assertEqual(lifecycle.events, ["verify-open"])
        self.assertFalse(controller.marker_path.exists())

    def test_explicit_rollback_unknown_commit_rejects_changed_instance(self) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-explicit-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=second_operation)
        controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost response after admission commit"
        ):
            controller.rollback(operation_id=second_operation)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())

        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_failed_deploy_rollback_unknown_commit_rejects_changed_instance(
        self,
    ) -> None:
        class FailedCandidateLifecycle(LostResumeLifecycle):
            fail_next_verify = False

            def verify(self, controller, descriptor):  # type: ignore[no-untyped-def]
                if self.fail_next_verify:
                    self._event("verify")
                    self.fail_next_verify = False
                    self.lose_next_resume = True
                    raise CONTROLLER.PullDeployError(
                        "injected candidate verification failure"
                    )
                return super().verify(controller, descriptor)

        lifecycle = FailedCandidateLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        # The fixture uses one synthetic target SHA for every operation.  A
        # real failed upgrade has a distinct candidate, so project the sealed
        # previous state as non-candidate for this rollback-path test.
        controller._candidate_current_state = lambda *_args: None  # type: ignore[method-assign]
        second_operation = "deploy-20260716-failed-rollback-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.fail_next_verify = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "deployment and rollback failed",
        ):
            controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_pre_stop_unknown_commit_rejects_changed_instance(self) -> None:
        lifecycle = LostUnchangedResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        operation = "deploy-20260716-prestop-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
        _directory, descriptor_path, _ready = controller._operation_paths(operation)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drained",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
            "drain": {"active_total": 0},
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.admission_open = False
        lifecycle.lose_next_unchanged_resume = True
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost unchanged-resume response"
        ):
            controller.recover_interrupted()
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_pre_stop_crashes_resume_unchanged_without_reconcile_or_restart(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )

        phases = [
            ("drain-started", None),
            ("drained", None),
            ("failed", "drained"),
        ]
        for index, (phase, failed_phase) in enumerate(phases, start=2):
            operation = f"deploy-20260716-prestop-{index}"
            controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
            _directory, descriptor_path, _ready = controller._operation_paths(operation)
            descriptor = CONTROLLER.load_private_json(descriptor_path)
            marker = {
                "schema_version": 2,
                "action": "deploy",
                "operation_id": operation,
                "source_sha": TARGET_SHA,
                "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
                "executor_control": descriptor["controller"]["executor_control"],
                "executor_control_sha256": descriptor["controller"][
                    "executor_control_sha256"
                ],
                "phase": phase,
                "started_at": CONTROLLER.utc_now(),
                "updated_at": CONTROLLER.utc_now(),
                "runtime_stopped": False,
                "source_switched": False,
                "slot_switched": False,
                "control_switched": False,
                "unit_switched": False,
                "asset_switched": False,
                "database_change_started": False,
            }
            if phase in {"drained", "failed"}:
                marker["drain"] = {"active_total": 0}
            if failed_phase is not None:
                marker["failed_phase"] = failed_phase
            CONTROLLER.atomic_json(controller.marker_path, marker)
            lifecycle.admission_open = False
            lifecycle.events.clear()
            recovered = controller.recover_interrupted()
            self.assertIsNone(recovered)
            expected = [
                "recovery-isolate",
                "recovery-redrain",
                "resume-unchanged",
            ]
            self.assertEqual(lifecycle.events, expected)
            self.assertNotIn("start", lifecycle.events)
            self.assertNotIn("stop", lifecycle.events)
            self.assertFalse(controller.marker_path.exists())

    def test_open_pre_stop_intent_without_fence_isolated_and_redrained(self) -> None:
        lifecycle = FakeLifecycle(admission_open=True)
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        operation = "deploy-20260716-prestop-no-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
        _directory, descriptor_path, _ready = controller._operation_paths(operation)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "prepared",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.events.clear()
        self.assertIsNone(controller.recover_interrupted())
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "resume-unchanged"],
        )
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_first_bootstrap_pre_stop_uses_only_legacy_unchanged_resume(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertIsNone(descriptor["previous_deployment"])
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drain-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        controller.recover_interrupted()
        self.assertEqual(lifecycle.events, ["resume-bootstrap-unchanged"])
        self.assertNotIn("stop", lifecycle.events)

    def test_first_bootstrap_lost_restore_response_never_restarts_open_legacy(
        self,
    ) -> None:
        lifecycle = FakeLifecycle(admission_open=True)
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "failed_phase": "runtime-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        CONTROLLER.PullDeployController._rollback_failed_attempt(
            controller, descriptor, marker
        )
        self.assertEqual(
            lifecycle.events,
            ["bootstrap-admission-status", "resume-bootstrap-unchanged"],
        )
        self.assertNotIn("stop", lifecycle.events)

    def test_post_canary_crash_persists_redrain_before_idempotent_stop_retry(
        self,
    ) -> None:
        lifecycle = FakeLifecycle(fail_at="stop")
        controller = self.controller(lifecycle=lifecycle)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        descriptor = {"previous_deployment": {"source_sha": PREVIOUS_SHA}}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": "sha256:" + "a" * 64,
            "phase": "failed",
            "failed_phase": "verifying",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected stop"):
            controller._rollback_failed_attempt(descriptor, marker)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "stop"],
        )
        self.assertEqual(marker["phase"], "runtime-stop-started")
        self.assertIn("drain", marker)

        lifecycle.events.clear()
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected stop"):
            controller._rollback_failed_attempt(descriptor, marker)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "stop"],
        )

    def test_recovery_marker_rejects_invalid_pre_stop_and_rollback_evidence(
        self,
    ) -> None:
        controller = self.controller(lifecycle=FakeLifecycle())
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        descriptor_digest = CONTROLLER.sha256_file(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drain-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
            "pre_stop_abort": False,
        }
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "pre-stop abort flag"):
            CONTROLLER.validate_recovery_marker(
                marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )
        marker.pop("pre_stop_abort")
        marker.update(
            action="explicit-rollback",
            phase="explicit-rollback-stop-started",
            rollback_current_state_sha256="sha256:" + "a" * 64,
            rollback_source_terminal_audit_sha256="sha256:" + "b" * 64,
            rollback_attempt_id="rollback-attempt-fixture-001",
            rollback_backup_operation_id="rollback-independent-001",
        )
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "drain evidence"):
            CONTROLLER.validate_recovery_marker(
                marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )

    def test_explicit_rollback_uses_independent_backup_and_reuses_it_after_crash(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

        second_operation = "deploy-20260716-0002"
        controller.prepare(target_sha=TARGET_SHA, operation_id=second_operation)
        controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.events.clear()

        original_restore = controller._restore_source
        restore_calls = 0

        def fail_restore_once(descriptor):  # type: ignore[no-untyped-def]
            nonlocal restore_calls
            restore_calls += 1
            if restore_calls == 1:
                raise CONTROLLER.PullDeployError("injected source restore crash")
            return original_restore(descriptor)

        controller._restore_source = fail_restore_once  # type: ignore[method-assign]
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "source restore crash"):
            controller.rollback(operation_id=second_operation)
        marker = CONTROLLER.load_private_json(controller.marker_path)
        rollback_backup = marker["rollback_backup"]
        self.assertNotEqual(
            Path(rollback_backup["path"]).parent,
            controller.backups_dir / second_operation,
        )
        self.assertEqual(lifecycle.events.count("backup"), 1)

        controller._restore_source = original_restore  # type: ignore[method-assign]
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered["operation_id"], OPERATION_ID)
        self.assertEqual(lifecycle.events.count("backup"), 1)
        self.assertFalse(controller.marker_path.exists())

    def test_explicit_rollback_recovers_write_before_phase_commit(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-rollback-write-window"
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        original_atomic_json = CONTROLLER.atomic_json
        lost_response = False

        def lose_current_state_response(path, value, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal lost_response
            result = original_atomic_json(path, value, *args, **kwargs)
            if (
                not lost_response
                and Path(path) == controller.current_state_path
                and isinstance(value, dict)
                and value.get("rollback_provenance") is not None
            ):
                lost_response = True
                raise CONTROLLER.PullDeployError(
                    "injected lost rollback state-write response"
                )
            return result

        with (
            mock.patch.object(
                CONTROLLER,
                "atomic_json",
                side_effect=lose_current_state_response,
            ),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "lost rollback state-write response",
            ),
        ):
            controller.rollback(operation_id=second_operation)
        marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            marker["phase"],
            "explicit-rollback-state-commit-started",
        )
        self.assertEqual(
            CONTROLLER.sha256_file(controller.current_state_path),
            marker["rollback_candidate_state_sha256"],
        )

        recovered = controller.recover_interrupted()

        self.assertEqual(recovered["operation_id"], OPERATION_ID)
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            controller._load_operation_state(second_operation)["outcome"],
            "rolled-back",
        )

    def test_explicit_rollback_preserves_pretty_previous_file_digest(self) -> None:
        controller = self.controller(lifecycle=FakeLifecycle())
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        previous = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        canonical_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        pretty_payload = (
            json.dumps(previous, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        controller.current_state_path.write_bytes(pretty_payload)
        os.chmod(controller.current_state_path, 0o600)
        with controller.current_state_path.open("rb") as stream:
            os.fsync(stream.fileno())
        CONTROLLER.fsync_directory(
            controller.current_state_path.parent
        )
        pretty_digest = CONTROLLER.sha256_file(
            controller.current_state_path
        )
        self.assertNotEqual(pretty_digest, canonical_digest)

        successor = "deploy-pretty-previous-rollback"
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=successor,
        )
        _root, descriptor_path, _ready = controller._operation_paths(
            successor
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["previous_deployment_sha256"],
            pretty_digest,
        )
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=successor,
        )

        rolled_back = controller.rollback(operation_id=successor)

        self.assertEqual(rolled_back["operation_id"], OPERATION_ID)
        self.assertEqual(
            rolled_back["rollback_provenance"][
                "sealed_previous_state_sha256"
            ],
            pretty_digest,
        )

    def test_two_pre_stop_rollback_aborts_use_distinct_nonterminal_audits(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-abort-retry"
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        lifecycle.fail_at = "drain"
        # This test isolates nonterminal audit append semantics.  The fixture
        # deliberately reuses one synthetic unit path across both operations,
        # which makes effect-byte reconciliation ambiguous.
        controller._reconcile_effect_commit_windows = (  # type: ignore[method-assign]
            lambda _descriptor, _marker: None
        )

        for _attempt in range(2):
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "injected drain failure",
            ):
                controller.rollback(operation_id=second_operation)
            recovered = controller.recover_interrupted()
            self.assertEqual(recovered["operation_id"], second_operation)
            self.assertFalse(controller.marker_path.exists())

        audits = sorted(
            (controller.audit_dir / second_operation).glob(
                "recovered-explicit-rollback-aborted-*.json"
            )
        )
        self.assertEqual(len(audits), 2)
        self.assertNotEqual(audits[0].name, audits[1].name)
        operation_state = controller._load_operation_state(second_operation)
        self.assertEqual(operation_state["outcome"], "deployed")

    def test_explicit_rollback_rejects_source_audit_tamper_before_drain(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-source-audit-tamper"
        controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        controller.apply(
            target_sha=TARGET_SHA,
            operation_id=second_operation,
        )
        source_audit = (
            controller.audit_dir / second_operation / "success.json"
        )
        os.chmod(source_audit, 0o644)
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mode 0600",
        ):
            controller.rollback(operation_id=second_operation)

        self.assertEqual(lifecycle.events, [])
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            controller._load_operation_state(second_operation)["outcome"],
            "deployed",
        )

    def test_stable_cli_uses_fixed_roots_and_has_no_extra_mutation_flags(self) -> None:
        parsed = CONTROLLER.parser().parse_args(
            [
                "prepare",
                "--sha",
                TARGET_SHA,
                "--operation-id",
                OPERATION_ID,
            ]
        )
        self.assertFalse(hasattr(parsed, "production_root"))
        self.assertFalse(hasattr(parsed, "runtime_root"))
        self.assertFalse(hasattr(parsed, "apply"))
        with self.assertRaises(SystemExit):
            CONTROLLER.parser().parse_args(
                [
                    "prepare",
                    "--sha",
                    TARGET_SHA,
                    "--operation-id",
                    OPERATION_ID,
                    "--apply",
                ]
            )

    def test_restored_bridge_cli_dispatches_every_content_address(self) -> None:
        descriptor_digest = "sha256:" + "a" * 64
        restored_terminal = "sha256:" + "b" * 64
        capsule_digest = "sha256:" + "c" * 64
        authority_sha = "5" * 40
        fake = mock.Mock()
        fake.recover_restored_first_bridge.return_value = {
            "action": "bridge-recover-restored",
            "apply": True,
        }
        arguments = [
            "bridge-recover-restored",
            "--authority-sha",
            authority_sha,
            "--target-sha",
            TARGET_SHA,
            "--operation-id",
            OPERATION_ID,
            "--capsule-sha256",
            capsule_digest,
            "--descriptor-sha256",
            descriptor_digest,
            "--restored-terminal-sha256",
            restored_terminal,
        ]
        with (
            mock.patch.object(
                CONTROLLER,
                "PullDeployController",
                return_value=fake,
            ) as constructor,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(CONTROLLER.main(arguments), 0)
        constructor.assert_called_once_with(
            CONTROLLER.PRODUCTION_ROOT,
            CONTROLLER.RUNTIME_ROOT,
            apply=True,
        )
        fake.recover_restored_first_bridge.assert_called_once_with(
            authority_sha=authority_sha,
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
            capsule_sha256=capsule_digest,
            descriptor_sha256=descriptor_digest,
            restored_terminal_sha256=restored_terminal,
        )


class UnitTransitionRunner:
    def __init__(self, target: Path, *, enabled: bool) -> None:
        self.target = target
        self.enabled = enabled
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["systemctl", "--user", "enable"]:
            self.enabled = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "disable"]:
            self.enabled = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "show"]:
            present = self.target.exists()
            values = {
                "LoadState": "loaded" if present else "not-found",
                "FragmentPath": str(self.target) if present else "",
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "UnitFileState": "enabled" if present and self.enabled else "",
            }
            output = "".join(f"{key}={value}\n" for key, value in values.items())
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")


class WorkerUnitTransitionTests(PullDeployTestCase):
    def _unit_descriptor(
        self, *, previous: bytes | None
    ) -> tuple[object, dict[str, object], Path]:
        target_parent = self.root / "systemd"
        target_parent.mkdir(mode=0o700)
        target = target_parent / CONTROLLER.MONOMER_MD_UNIT_NAME
        candidate = self.runtime / "state/prepared/candidate.service"
        candidate.write_bytes(b"candidate unit\n")
        os.chmod(candidate, 0o600)
        backup: Path | None = None
        if previous is not None:
            backup = self.runtime / "state/prepared/previous.service"
            backup.write_bytes(previous)
            os.chmod(backup, 0o600)
            target.write_bytes(b"candidate unit\n")
            os.chmod(target, 0o600)
        runner = UnitTransitionRunner(target, enabled=previous is not None)
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=True
        )
        controller.control_environment = lambda: {}  # type: ignore[method-assign]
        controller._revalidate_worker_controls = lambda _descriptor: None  # type: ignore[method-assign]
        unit = {
            "candidate_path": str(candidate),
            "target_path": str(target),
            "sha256": CONTROLLER.sha256_file(candidate),
            "previous_present": previous is not None,
            "previous_sha256": (
                CONTROLLER.sha256_bytes(previous) if previous is not None else None
            ),
            "previous_backup_path": str(backup) if backup is not None else None,
        }
        return controller, {"monomer_md": {"systemd_unit": unit}}, target

    def test_absent_unit_is_enabled_then_disabled_and_removed_on_rollback(self) -> None:
        controller, descriptor, target = self._unit_descriptor(previous=None)
        controller._install_candidate_worker_unit(descriptor)
        self.assertTrue(target.is_file())
        self.assertIn(
            ["systemctl", "--user", "enable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )
        controller._restore_previous_worker_unit(descriptor)
        self.assertFalse(target.exists())
        self.assertIn(
            ["systemctl", "--user", "disable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )

    def test_existing_enabled_unit_restores_exact_previous_bytes(self) -> None:
        controller, descriptor, target = self._unit_descriptor(previous=b"old unit\n")
        controller._restore_previous_worker_unit(descriptor)
        self.assertEqual(target.read_bytes(), b"old unit\n")
        self.assertNotIn(
            ["systemctl", "--user", "disable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )


class BootstrapQuiesceContractTests(unittest.TestCase):
    def test_example_output_is_accepted_by_dedicated_controller_validator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap-quiesce-") as temporary:
            root = Path(temporary)
            production = root / "production"
            runtime = root / "runtime"
            fake_bin = root / "bin"
            production.mkdir()
            (runtime / "config").mkdir(parents=True)
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(docker, 0o700)
            probe = runtime / "config/bootstrap-active-jobs-probe"
            jobs = {name: 0 for name in CONTROLLER.ACTIVE_JOB_FIELDS_V2}
            evidence_source = {
                "ingress_isolated": True,
                "active_jobs": jobs,
                "active_total": 0,
                "active_jobs_schema_version": 2,
            }
            probe.write_text(
                "#!/bin/sh\nprintf '%s\\n' '"
                + json.dumps(evidence_source, separators=(",", ":"))
                + "'\n",
                encoding="utf-8",
            )
            os.chmod(probe, 0o700)
            source = (
                REPOSITORY_ROOT / "ops/config/bootstrap-quiesce.example"
            ).read_text(encoding="utf-8")
            source = source.replace(
                "/data/lzq/gith/nexpoly-runtime", str(runtime)
            ).replace("/data/lzq/gith/nexpoly", str(production))
            hook = root / "bootstrap-quiesce"
            hook.write_text(source, encoding="utf-8")
            os.chmod(hook, 0o700)
            result = subprocess.run(
                [str(hook)],
                cwd=production,
                env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            evidence = json.loads(result.stdout)
            self.assertEqual(
                CONTROLLER.validate_bootstrap_quiesce_evidence(evidence), evidence
            )
            with self.assertRaisesRegex(CONTROLLER.PullDeployError, "invalid shape"):
                CONTROLLER.validate_active_jobs_evidence(evidence, require_drained=True)


if __name__ == "__main__":
    unittest.main()
