from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/site_helper_contracts.py"
SPEC = importlib.util.spec_from_file_location("site_helper_contracts_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CONTRACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACTS)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def runtime_identity_fields() -> dict[str, str]:
    return {
        "backend_image_id": DIGEST_A,
        "web_image_id": DIGEST_B,
        "worker_unit_sha256": DIGEST_C,
    }


class SiteHelperContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="site-helper-contracts-")
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "runtime"
        self.config = self.runtime / "config"
        self.config.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        os.chmod(self.config, 0o700)

    def install_helpers(self) -> None:
        for name in CONTRACTS.HELPERS:
            path = self.config / name
            path.write_text(f"#!/bin/sh\n# {name}\n", encoding="utf-8")
            os.chmod(path, 0o700)

    def test_every_migration_table_has_one_data_boundary(self) -> None:
        migrations = ROOT / "backend/migrations/postgres"
        created: set[tuple[str, str]] = set()
        pattern = re.compile(
            r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)",
            re.IGNORECASE,
        )
        for path in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            created.update(
                (schema.lower(), table.lower())
                for schema, table in pattern.findall(
                    path.read_text(encoding="utf-8")
                )
            )

        categories = (
            set(CONTRACTS.BUSINESS_MUTABLE_TABLES)
            | set(CONTRACTS.POST_0013_BUSINESS_MUTABLE_TABLES)
            | set(CONTRACTS.GOVERNED_CONTROL_TABLES)
            | set(CONTRACTS.STATIC_IMPORT_TABLES)
            | {
                CONTRACTS.MIGRATION_LEDGER_TABLE,
                CONTRACTS.CONTRACT_0012_EXCEPTION_TABLE,
            }
        )
        category_sizes = sum(
            len(group)
            for group in (
                CONTRACTS.BUSINESS_MUTABLE_TABLES,
                CONTRACTS.POST_0013_BUSINESS_MUTABLE_TABLES,
                CONTRACTS.GOVERNED_CONTROL_TABLES,
                CONTRACTS.STATIC_IMPORT_TABLES,
                (
                    CONTRACTS.MIGRATION_LEDGER_TABLE,
                    CONTRACTS.CONTRACT_0012_EXCEPTION_TABLE,
                ),
            )
        )
        self.assertEqual(len(categories), category_sizes)
        planned = (
            set()
            if (migrations / "0013_monomer_dft_jobs.sql").is_file()
            else set(CONTRACTS.POST_0013_BUSINESS_MUTABLE_TABLES)
        )
        self.assertEqual(categories, created | planned)

    def test_readiness_hashes_but_never_executes_all_fixed_helpers(self) -> None:
        self.install_helpers()
        marker = self.runtime / "executed"
        helper = self.config / "bootstrap-active-jobs-probe"
        helper.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        os.chmod(helper, 0o700)
        report = CONTRACTS.inspect_helper_installation(self.runtime)
        self.assertTrue(report["ready"])
        self.assertFalse(report["executed_helpers"])
        self.assertEqual(set(report["helpers"]), set(CONTRACTS.HELPERS))
        self.assertFalse(marker.exists())

        os.chmod(helper, 0o770)
        with self.assertRaisesRegex(CONTRACTS.SiteHelperContractError, "unsafe"):
            CONTRACTS.inspect_helper_installation(self.runtime)

    def test_active_jobs_contract_rejects_unknown_boolean_and_bad_total(self) -> None:
        document = {
            "active_jobs_schema_version": 2,
            "ingress_isolated": True,
            "active_jobs": {
                name: 0 for name in CONTRACTS.ACTIVE_JOB_FIELDS_V2
            },
            "active_total": 0,
        }
        validated = CONTRACTS.validate_active_jobs(document)
        self.assertEqual(validated["active_total"], 0)
        document["active_jobs"]["monomer_dft"] = False
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError, "count is invalid"
        ):
            CONTRACTS.validate_active_jobs(document)
        document["active_jobs"]["monomer_dft"] = 1
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError, "total is inconsistent"
        ):
            CONTRACTS.validate_active_jobs(document)

    def test_legacy_status_and_resume_bind_exact_unchanged_processes(self) -> None:
        identity = runtime_identity_fields()
        expected = CONTRACTS.sha256_bytes(CONTRACTS.canonical_json_bytes(identity))
        status = {
            "schema_version": 1,
            "legacy_runtime_state": "open",
            **identity,
            "backend_container_id": "d" * 64,
            "backend_pid": 123,
            "backend_started_at": "2026-07-17T00:00:00Z",
            "backend_restart_count": 0,
            "worker_main_pid": 456,
            "worker_invocation_id": "fixture-worker",
            "worker_active_enter_monotonic": 789,
            "backend_healthy": True,
            "web_healthy": True,
            "worker_healthy": True,
            "ingress_open": True,
        }
        self.assertEqual(
            CONTRACTS.validate_legacy_status(
                status,
                expected_runtime_digest=expected,
            ),
            status,
        )
        status["backend_pid"] = None
        with self.assertRaises(CONTRACTS.SiteHelperContractError):
            CONTRACTS.validate_legacy_status(
                status,
                expected_runtime_digest=expected,
            )

        resume = {
            "schema_version": 1,
            "legacy_runtime_unchanged": True,
            **identity,
            "backend_container_id_before": "d" * 64,
            "backend_container_id_after": "d" * 64,
            "backend_pid_before": 123,
            "backend_pid_after": 123,
            "backend_started_at_before": "2026-07-17T00:00:00Z",
            "backend_started_at_after": "2026-07-17T00:00:00Z",
            "backend_restart_count_before": 0,
            "backend_restart_count_after": 0,
            "worker_main_pid_before": 456,
            "worker_main_pid_after": 456,
            "worker_invocation_id_before": "fixture-worker",
            "worker_invocation_id_after": "fixture-worker",
            "worker_active_enter_monotonic_before": 789,
            "worker_active_enter_monotonic_after": 789,
            "backend_healthy": True,
            "web_healthy": True,
            "worker_healthy": True,
            "ingress_restored": True,
        }
        CONTRACTS.validate_legacy_resume(
            resume,
            expected_runtime_digest=expected,
        )
        resume["worker_main_pid_after"] = 999
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError, "changed worker_main_pid"
        ):
            CONTRACTS.validate_legacy_resume(
                resume,
                expected_runtime_digest=expected,
            )

    def test_legacy_restore_binds_runtime_digest(self) -> None:
        document = {
            "schema_version": 1,
            "legacy_runtime_restored": True,
            **runtime_identity_fields(),
            "backend_healthy": True,
            "web_healthy": True,
            "worker_healthy": True,
            "ingress_restored": True,
        }
        expected = CONTRACTS.legacy_runtime_identity(document)
        self.assertEqual(
            CONTRACTS.validate_legacy_restore(
                document,
                expected_runtime_digest=expected,
            ),
            document,
        )
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError, "another runtime"
        ):
            CONTRACTS.validate_legacy_restore(
                document,
                expected_runtime_digest="sha256:" + "f" * 64,
            )

    def test_legacy_v2_seals_web_container_and_worker_unit_identity(self) -> None:
        status = {
            "schema_version": 2,
            "legacy_runtime_state": "open",
            **runtime_identity_fields(),
            "backend_container_id": "d" * 64,
            "web_container_id": "e" * 64,
            "backend_process_spec_sha256": DIGEST_A,
            "web_process_spec_sha256": DIGEST_B,
            "worker_unit_name": "nexpoly-monomer-md.service",
            "worker_unit_path": (
                f"/home/{os.geteuid()}/.config/systemd/user/"
                "nexpoly-monomer-md.service"
            ),
            "worker_unit_mode": "0664",
            "worker_unit_uid": os.geteuid(),
            "worker_unit_gid": os.getegid(),
            "worker_manager_uid": os.geteuid(),
            "worker_manager_runtime_dir": f"/run/user/{os.geteuid()}",
            "worker_manager_environment_sha256": DIGEST_C,
            "postgres_container_id": "9" * 64,
            "postgres_image_id": DIGEST_A,
            "postgres_data_volume": "nexpoly_pg_data",
            "postgres_system_identifier": "7659245354718314530",
            "backend_pid": 123,
            "web_pid": 234,
            "backend_started_at": "2026-07-17T00:00:00Z",
            "web_started_at": "2026-07-17T00:00:01Z",
            "backend_restart_count": 0,
            "web_restart_count": 0,
            "worker_main_pid": 456,
            "worker_invocation_id": "fixture-worker",
            "worker_active_enter_monotonic": 789,
            "backend_healthy": True,
            "web_healthy": True,
            "worker_healthy": True,
            "ingress_open": True,
        }
        expected = CONTRACTS.legacy_runtime_identity(status)
        self.assertEqual(
            CONTRACTS.validate_legacy_status(
                status,
                expected_runtime_digest=expected,
            ),
            status,
        )
        isolated = dict(status)
        isolated.update(
            {
                "legacy_runtime_state": "isolated",
                "web_pid": None,
                "web_started_at": None,
                "web_restart_count": None,
                "web_healthy": False,
                "ingress_open": False,
            }
        )
        self.assertEqual(
            CONTRACTS.validate_legacy_status(
                isolated,
                expected_runtime_digest=expected,
            ),
            isolated,
        )
        changed = dict(status)
        changed["web_container_id"] = "f" * 64
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "another runtime",
        ):
            CONTRACTS.validate_legacy_status(
                changed,
                expected_runtime_digest=expected,
            )

        restored = {
            key: value
            for key, value in status.items()
            if key not in {"legacy_runtime_state", "ingress_open"}
        }
        restored.update(
            {
                "legacy_runtime_restored": True,
                "backend_healthy": True,
                "web_healthy": True,
                "worker_healthy": True,
                "ingress_restored": True,
            }
        )
        self.assertEqual(
            CONTRACTS.validate_legacy_restore(
                restored,
                expected_runtime_digest=expected,
            ),
            restored,
        )

    def test_external_database_contract_is_complete_and_read_only(self) -> None:
        def record(stack: str, user: str) -> dict[str, object]:
            return {
                "stack": stack,
                "database": stack,
                "current_user": user,
                "transaction_read_only": True,
                "role_superuser": False,
                "role_create_db": False,
                "role_create_role": False,
                "ledger": [
                    {
                        "version": "0001_app_data_governance",
                        "checksum": "a" * 64,
                    }
                ],
                "legacy_relation_present": stack == "nexpoly_md_health_opt",
            }

        document = {
            "schema_version": 1,
            "inventory_complete": True,
            "writable_target": {
                "stack": "production",
                "database": "nexpoly",
            },
            "databases": [
                record("nexpoly_md_health_opt", "health_auditor"),
                record("nexpoly_dev", "dev_auditor"),
            ],
        }
        expected_users = {
            "nexpoly_dev": "dev_auditor",
            "nexpoly_md_health_opt": "health_auditor",
        }
        validated = CONTRACTS.validate_external_database_audit(
            document,
            expected_users=expected_users,
        )
        self.assertEqual(
            [value["stack"] for value in validated["databases"]],
            ["nexpoly_dev", "nexpoly_md_health_opt"],
        )
        document["databases"][0]["transaction_read_only"] = False
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "transaction_read_only",
        ):
            CONTRACTS.validate_external_database_audit(
                document,
                expected_users=expected_users,
            )

    def test_mutable_data_audit_binds_exact_tables_and_one_snapshot(self) -> None:
        def table_record(
            relation: tuple[str, str],
            index: int,
            *,
            present: bool = True,
            rows: int | None = None,
        ) -> dict[str, object]:
            return {
                "schema": relation[0],
                "table": relation[1],
                "state": "present" if present else "absent",
                "row_count": (
                    index + 1 if rows is None else rows
                )
                if present
                else None,
                "schema_sha256": (
                    "sha256:" + f"{(index + 1) % 16:x}" * 64
                    if present
                    else None
                ),
                "content_sha256": (
                    "sha256:" + f"{(index + 8) % 16:x}" * 64
                    if present
                    else None
                ),
            }

        business_tables = [
            table_record(relation, index)
            for index, relation in enumerate(
                CONTRACTS.BUSINESS_MUTABLE_TABLES
            )
        ]
        business_tables.extend(
            table_record(relation, index + 5, present=False)
            for index, relation in enumerate(
                CONTRACTS.POST_0013_BUSINESS_MUTABLE_TABLES
            )
        )
        static_tables = [
            table_record(relation, index + 8)
            for index, relation in enumerate(CONTRACTS.STATIC_IMPORT_TABLES)
        ]
        deployment_table = table_record(
            CONTRACTS.GOVERNED_CONTROL_TABLES[0], 23, rows=1
        )
        analytics_table = table_record(
            CONTRACTS.GOVERNED_CONTROL_TABLES[1], 24, rows=0
        )
        sequences = []
        for schema, sequence, owned_by in CONTRACTS.DATA_SEQUENCES:
            present = schema != "monomer_dft"
            sequences.append(
                {
                    "schema": schema,
                    "sequence": sequence,
                    "owned_by": owned_by,
                    "state": "present" if present else "absent",
                    "data_type": "bigint" if present else None,
                    "start_value": 1 if present else None,
                    "min_value": 1 if present else None,
                    "max_value": 9223372036854775807 if present else None,
                    "increment_by": 1 if present else None,
                    "cache_size": 1 if present else None,
                    "cycle": False if present else None,
                    "last_value": 5 if present else None,
                    "is_called": True if present else None,
                }
            )
        identity = {
            "operation_id": "deploy-20260717-fixture",
            "database": "nexpoly",
            "database_system_identifier": "7659245354718314530",
            "connection": {
                "service": "nexpoly-mutable-audit",
                "host": "127.0.0.1",
                "port": 55432,
                "database": "nexpoly",
                "user": "nexpoly_mutable_audit",
            },
            "postgres_runtime": {
                "container_id": "a" * 64,
                "image_id": DIGEST_A,
                "configured_image": "postgres:16-alpine@sha256:" + "b" * 64,
                "data_volume": {
                    "type": "volume",
                    "name": "nexpoly_postgres_data",
                    "source": (
                        "/var/lib/docker/volumes/"
                        "nexpoly_postgres_data/_data"
                    ),
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
            "digest_algorithm": "sha256-postgres-jsonb-copy-v3",
            "migration_ledger": [
                {"version": version, "checksum": checksum}
                for version, checksum in CONTRACTS.CANONICAL_MIGRATION_LEDGER[
                    :11
                ]
            ],
            "business_tables": business_tables,
            "governed_controls": {
                "deployment_control": {
                    "table": deployment_table,
                    "row": {
                        "control_key": "production",
                        "drain_enabled": True,
                        "reason": "pull deployment deploy-20260717-fixture",
                        "release_sha": "1" * 40,
                        "activated_at": "2026-07-17T00:00:00Z",
                        "activated_by": "pull-deploy-controller",
                        "updated_at": "2026-07-17T00:00:00Z",
                    },
                },
                "database_analytics_snapshots": {
                    "table": analytics_table,
                    "entries": [],
                },
            },
            "static_tables": static_tables,
            "migration_exception": table_record(
                CONTRACTS.CONTRACT_0012_EXCEPTION_TABLE,
                25,
                rows=9,
            ),
            "sequences": sequences,
        }
        document = {
            "schema_version": 4,
            **identity,
            "transaction_isolation": "repeatable read",
            "transaction_read_only": True,
            "transaction_deferrable": True,
            "snapshot_sha256": CONTRACTS.sha256_bytes(
                CONTRACTS.canonical_json_bytes(identity)
            ),
            "captured_at": "2026-07-17T00:00:00Z",
        }
        self.assertEqual(
            CONTRACTS.validate_mutable_data_audit(document),
            document,
        )
        mutations = (
            (
                "table",
                lambda value: value["business_tables"][0].update(
                    table="jobs"
                ),
            ),
            (
                "content",
                lambda value: value["business_tables"][1].update(
                    content_sha256="sha256:" + "f" * 64
                ),
            ),
            (
                "schema",
                lambda value: value["business_tables"][0].update(
                    schema_sha256="sha256:" + "e" * 64
                ),
            ),
            (
                "sequence",
                lambda value: value["sequences"][0].update(last_value=99),
            ),
            (
                "ledger",
                lambda value: value["migration_ledger"][8].update(
                    checksum="a" * 64
                ),
            ),
            (
                "snapshot",
                lambda value: value.update(snapshot_sha256=DIGEST_A),
            ),
            (
                "read-write",
                lambda value: value.update(transaction_read_only=False),
            ),
            (
                "write-membership",
                lambda value: value["role_security"].update(
                    has_pg_write_all_data=True
                ),
            ),
            (
                "dangerous-membership",
                lambda value: value["role_security"][
                    "effective_memberships"
                ].append("site_writer"),
            ),
            (
                "direct-write",
                lambda value: value["role_security"][
                    "direct_write_grants"
                ].append("relation:online_knowledge.jobs:UPDATE"),
            ),
            (
                "effective-write",
                lambda value: value["role_security"][
                    "effective_write_privileges"
                ].append("relation:online_knowledge.jobs"),
            ),
            (
                "object-owner",
                lambda value: value["role_security"][
                    "owned_objects"
                ].append("relation:online_knowledge.jobs"),
            ),
            (
                "writable-default",
                lambda value: value["role_security"]["role_settings"][0][
                    "settings"
                ].__setitem__(0, "default_transaction_read_only=off"),
            ),
            (
                "non-deferrable",
                lambda value: value.update(transaction_deferrable=False),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(document))
                mutate(changed)
                with self.assertRaises(CONTRACTS.SiteHelperContractError):
                    CONTRACTS.validate_mutable_data_audit(changed)

    def test_cli_validate_never_changes_input(self) -> None:
        evidence = self.runtime / "active-jobs.json"
        document = {
            "ingress_isolated": True,
            "active_jobs": {
                name: 0 for name in CONTRACTS.ACTIVE_JOB_FIELDS_V1
            },
            "active_total": 0,
        }
        evidence.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(evidence, 0o600)
        before = evidence.read_bytes()
        self.assertEqual(
            CONTRACTS.main(
                [
                    "validate",
                    "--helper",
                    "bootstrap-active-jobs-probe",
                    "--input",
                    str(evidence),
                ]
            ),
            0,
        )
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
