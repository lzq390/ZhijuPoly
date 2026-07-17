from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
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
