from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "production_postgres_rehearsal_test_module",
    ROOT / "production_postgres_rehearsal.py",
)
assert SPEC is not None and SPEC.loader is not None
REHEARSAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REHEARSAL)


class ProductionPostgresRehearsalTests(unittest.TestCase):
    OPERATION_ID = "deploy-20260814-rehearsal"
    TARGET_SHA = "1" * 40
    TARGET_TREE = "2" * 40
    DESCRIPTOR_SHA256 = "sha256:" + "3" * 64
    READY_SHA256 = "sha256:" + "4" * 64
    RUNTIME_ROOT = Path("/var/lib/nexpoly-rehearsal-test")
    NOW = dt.datetime(2026, 8, 14, 0, 3, tzinfo=dt.timezone.utc)

    def manifest(self) -> list[dict[str, object]]:
        requirement = {
            "version": "0012_drop_polytao_jobs",
            "checksum": "d" * 64,
        }
        return [
            {
                "version": "0013_monomer_dft_jobs",
                "kind": "expand",
                "epoch": 2,
                "checksum": "c" * 64,
                "requires_contracts": [dict(requirement)],
            },
            {
                "version": "0014_monomer_md_task_queue_cancel",
                "kind": "expand",
                "epoch": 2,
                "checksum": "a" * 64,
                "requires_contracts": [dict(requirement)],
            },
            {
                "version": "0015_property_filter_performance",
                "kind": "expand",
                "epoch": 2,
                "checksum": "b" * 64,
                "requires_contracts": [dict(requirement)],
            },
        ]

    def ledger(self) -> list[dict[str, str]]:
        return [
            {"version": str(entry["version"]), "checksum": str(entry["checksum"])}
            for entry in self.manifest()
        ]

    def expected_migration_records(self) -> list[dict[str, str]]:
        return [
            {
                "version": entry["version"],
                "status": "skipped" if index == 0 else "applied",
                "checksum": entry["checksum"],
            }
            for index, entry in enumerate(self.manifest())
        ]

    def summary(self) -> dict[str, object]:
        return {
            "system_identifier": "8769245354718314530",
            "ledger": self.ledger(),
            "property_records": 615_159,
            "snapshot_count": 1,
            "snapshot": {
                "snapshot_key": "current",
                "schema_version": 1,
                "generation": 1,
                "total_records": 615_159,
                "mapped_records": 191_761,
                "raw_records": 423_398,
                "option_count": 27,
            },
            "indexes": [
                "idx_core_filter_records_raw_unit_value_v2",
                "idx_core_filter_records_standardized_unit_value",
            ],
            "statistics": [
                "stats_core_filter_records_raw_unit",
                "stats_core_filter_records_standardized_unit",
            ],
        }

    def descriptor(self) -> dict[str, object]:
        return {
            "operation_id": self.OPERATION_ID,
            "prepared_at": "2026-08-14T00:00:00Z",
            "repository": {
                "target_sha": self.TARGET_SHA,
                "target_tree": self.TARGET_TREE,
            },
            "images": {
                "backend": {
                    "digest_ref": "registry.example/backend@sha256:" + "5" * 64,
                    "image_id": "sha256:" + "6" * 64,
                }
            },
            "postgres_restore_image": {
                "digest_ref": "postgres@sha256:" + "7" * 64,
                "image_id": "sha256:" + "8" * 64,
            },
            "migrations": {"records": self.manifest()},
        }

    def source_evidence(
        self,
        *,
        restored: bool = False,
    ) -> dict[str, object]:
        descriptor = self.descriptor()
        source_ledger = self.ledger()[:-2]
        return {
            "container_id": ("b" if restored else "a") * 64,
            "image_id": (
                descriptor["postgres_restore_image"]["image_id"]
                if restored
                else "sha256:" + "9" * 64
            ),
            "restart_count": 0,
            "runtime_sha256": "sha256:" + ("d" if restored else "e") * 64,
            "system_identifier": (
                "8769245354718314530" if restored else "7659245354718314530"
            ),
            "server_version_num": "160009",
            "database": "nexpoly_restore" if restored else "nexpoly",
            "user": "postgres" if restored else "nexpoly",
            "property_records": 615_159,
            "ledger": source_ledger,
            "ledger_sha256": REHEARSAL._digest(source_ledger),
        }

    def report(self) -> dict[str, object]:
        descriptor = self.descriptor()
        source = self.source_evidence()
        restored = self.source_evidence(restored=True)
        migration_records = self.expected_migration_records()
        after = self.summary()
        after["query_plans"] = {
            "standardized": {
                "default_records_index_names": [
                    "idx_core_filter_records_standardized_unit_value"
                ],
                "default_plan_sha256": "sha256:" + "a" * 64,
                "diagnostic_records_index_names": [
                    "idx_core_filter_records_standardized_unit_value"
                ],
                "diagnostic_plan_sha256": "sha256:" + "b" * 64,
            },
            "raw": {
                "default_records_index_names": [
                    "idx_core_filter_records_raw_unit_value_v2"
                ],
                "default_plan_sha256": "sha256:" + "c" * 64,
                "diagnostic_records_index_names": [
                    "idx_core_filter_records_raw_unit_value_v2"
                ],
                "diagnostic_plan_sha256": "sha256:" + "d" * 64,
            },
        }
        container_name = "nexpoly-rehearsal-" + hashlib.sha256(
            self.OPERATION_ID.encode()
        ).hexdigest()[:16]
        return {
            "schema_version": 1,
            "status": "passed",
            "operation_id": self.OPERATION_ID,
            "target_sha": self.TARGET_SHA,
            "target_tree": self.TARGET_TREE,
            "descriptor_sha256": self.DESCRIPTOR_SHA256,
            "ready_sha256": self.READY_SHA256,
            "plan_sha256": "sha256:" + "f" * 64,
            "backend_image": descriptor["images"]["backend"],
            "postgres_image": descriptor["postgres_restore_image"],
            "source_before": source,
            "source_after": copy.deepcopy(source),
            "dump": {
                "path": str(
                    self.RUNTIME_ROOT
                    / "backups"
                    / self.OPERATION_ID
                    / "preflight-rehearsal"
                    / "database.dump"
                ),
                "sha256": "sha256:" + "0" * 64,
                "bytes": 1024,
            },
            "restored_before": restored,
            "migrations": {
                "duration_seconds": 12.5,
                "output_sha256": REHEARSAL._digest(migration_records),
                "lock_timeout": "30s",
                "statement_timeout": "15min",
                "records": migration_records,
            },
            "after": after,
            "timings": {
                "backup_restore_seconds": 120.0,
                "backup_restore_limit_seconds": 1800,
                "migration_limit_seconds": 600,
            },
            "cleanup": {
                "postgres_container_name": container_name,
                "migration_container_name": container_name + "-migrate",
                "postgres_absent": True,
                "migration_absent": True,
                "proved_at": "2026-08-14T00:02:00Z",
            },
            "journal_head_sha256": "sha256:" + "1" * 64,
            "started_at": "2026-08-14T00:01:00Z",
            "completed_at": "2026-08-14T00:02:00Z",
        }

    def seal(self, report: dict[str, object]) -> dict[str, object]:
        return {"report": report, "report_sha256": REHEARSAL._digest(report)}

    def validate(self, report: dict[str, object]) -> dict[str, object]:
        return REHEARSAL.validate_rehearsal_report(
            self.seal(report),
            descriptor=self.descriptor(),
            descriptor_sha256=self.DESCRIPTOR_SHA256,
            ready_sha256=self.READY_SHA256,
            runtime_root=self.RUNTIME_ROOT,
            now=self.NOW,
            verify_runtime=False,
        )

    @staticmethod
    def records_index_plan(index_name: str) -> list[dict[str, object]]:
        return [
            {
                "Plan": {
                    "Node Type": "Nested Loop",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Alias": "records",
                            "Index Name": index_name,
                            "Index Cond": "(property_key = target.property_key)",
                        }
                    ],
                }
            }
        ]

    def test_plan_index_names_recurses(self) -> None:
        plan = [
            {
                "Plan": {
                    "Node Type": "Nested Loop",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Index Name": "idx_expected",
                        }
                    ],
                }
            }
        ]
        self.assertEqual(REHEARSAL._plan_index_names(plan), {"idx_expected"})

    def test_post_migration_evidence_accepts_all_five_queries(self) -> None:
        standardized = "idx_core_filter_records_standardized_unit_value"
        raw = "idx_core_filter_records_raw_unit_value_v2"
        values = [
            self.summary(),
            self.records_index_plan(standardized),
            self.records_index_plan(standardized),
            self.records_index_plan(raw),
            self.records_index_plan(raw),
        ]
        with mock.patch.object(
            REHEARSAL, "_psql_json", side_effect=values
        ) as psql_json:
            evidence = REHEARSAL._post_migration_evidence("c" * 64, 615_159)

        self.assertEqual(psql_json.call_count, 5)
        self.assertEqual(evidence["snapshot"]["total_records"], 615_159)
        self.assertEqual(
            evidence["query_plans"]["raw"]["default_records_index_names"],
            [raw],
        )
        self.assertEqual(
            evidence["query_plans"]["raw"]["diagnostic_records_index_names"],
            [raw],
        )
        self.assertNotIn("postgres_options", psql_json.call_args_list[1].kwargs)
        self.assertEqual(
            psql_json.call_args_list[2].kwargs["postgres_options"],
            "-c enable_seqscan=off",
        )
        self.assertNotIn("postgres_options", psql_json.call_args_list[3].kwargs)
        self.assertEqual(
            psql_json.call_args_list[4].kwargs["postgres_options"],
            "-c enable_seqscan=off",
        )

    def test_post_migration_evidence_rejects_record_drift(self) -> None:
        summary = self.summary()
        summary["property_records"] = 615_158
        with mock.patch.object(REHEARSAL, "_psql_json", return_value=summary):
            with self.assertRaisesRegex(REHEARSAL.RehearsalError, "record count changed"):
                REHEARSAL._post_migration_evidence("c" * 64, 615_159)

    def test_post_migration_evidence_rejects_missing_index(self) -> None:
        values = [
            self.summary(),
            [{"Plan": {"Node Type": "Seq Scan", "Alias": "records"}}],
            self.records_index_plan(
                "idx_core_filter_records_standardized_unit_value"
            ),
        ]
        with mock.patch.object(REHEARSAL, "_psql_json", side_effect=values):
            with self.assertRaisesRegex(REHEARSAL.RehearsalError, "did not use"):
                REHEARSAL._post_migration_evidence("c" * 64, 615_159)

    def test_parse_migration_records_requires_exact_order_status_and_coverage(self) -> None:
        manifest = self.manifest()
        records = self.expected_migration_records()
        output = "\n".join(
            f"{record['version']}\t{record['status']}\t{record['checksum']}"
            for record in records
        )
        self.assertEqual(
            REHEARSAL._parse_migration_records(
                output,
                manifest,
                existing_count=1,
            ),
            records,
        )

        extra = output + "\n0016_unreviewed\tapplied\t" + "f" * 64
        reordered_lines = output.splitlines()
        reordered_lines[-2:] = reversed(reordered_lines[-2:])
        wrong_status = output.replace(
            "0014_monomer_md_task_queue_cancel\tapplied",
            "0014_monomer_md_task_queue_cancel\tskipped",
        )
        for label, tampered, message in (
            ("extra", extra, "exact target manifest"),
            ("reordered", "\n".join(reordered_lines), "canonical target manifest"),
            ("wrong-status", wrong_status, "apply exactly 0014 then 0015"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(REHEARSAL.RehearsalError, message):
                    REHEARSAL._parse_migration_records(
                        tampered,
                        manifest,
                        existing_count=1,
                    )

    def test_full_manifest_projects_to_exact_database_ledger(self) -> None:
        manifest = self.manifest()

        self.assertEqual(
            REHEARSAL._database_ledger_projection(manifest),
            self.ledger(),
        )
        self.assertEqual(
            set(manifest[0]),
            {"version", "kind", "epoch", "checksum", "requires_contracts"},
        )
        with self.assertRaisesRegex(
            REHEARSAL.RehearsalError,
            "descriptor migration manifest is invalid",
        ):
            REHEARSAL._database_ledger_projection(self.ledger())

    def test_validate_report_accepts_exact_contract(self) -> None:
        result = self.validate(self.report())
        self.assertEqual(result["completed_at"], "2026-08-14T00:02:00Z")

    def test_validate_report_rejects_migration_record_tampering(self) -> None:
        for label in ("extra", "reordered", "wrong-status"):
            report = self.report()
            records = report["migrations"]["records"]
            if label == "extra":
                records.append(
                    {
                        "version": "0016_unreviewed",
                        "status": "applied",
                        "checksum": "f" * 64,
                    }
                )
            elif label == "reordered":
                records[-2:] = reversed(records[-2:])
            else:
                records[-1]["status"] = "skipped"
            report["migrations"]["output_sha256"] = REHEARSAL._digest(records)
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    REHEARSAL.RehearsalError,
                    "exact ordered 0014/0015 migrations",
                ):
                    self.validate(report)

    def test_validate_report_rejects_cleanup_journal_and_runtime_tampering(self) -> None:
        cases = {
            "cleanup": (
                lambda report: report["cleanup"].__setitem__(
                    "migration_absent", False
                ),
                "cleanup evidence is invalid",
            ),
            "journal": (
                lambda report: report.__setitem__(
                    "journal_head_sha256", "sha256:" + "0" * 63
                ),
                "journal head is invalid",
            ),
            "source-runtime": (
                lambda report: report["source_after"].__setitem__(
                    "runtime_sha256", "sha256:" + "f" * 64
                ),
                "production changed during rehearsal",
            ),
        }
        for label, (tamper, message) in cases.items():
            report = self.report()
            tamper(report)
            with self.subTest(label=label):
                with self.assertRaisesRegex(REHEARSAL.RehearsalError, message):
                    self.validate(report)

    def test_migration_create_source_contract_is_named_and_secret_free(self) -> None:
        source = inspect.getsource(REHEARSAL.run_rehearsal)
        create_start = source.index('"docker",\n                "create"')
        create_end = source.index("owned_migration =", create_start)
        create_command = source[create_start:create_end]

        self.assertIn('"--name",\n                migration_name', create_command)
        self.assertIn('"--network",\n                f"container:{container_id}"', create_command)
        self.assertIn('"--read-only"', create_command)
        self.assertIn('"--cap-drop",\n                "ALL"', create_command)
        self.assertIn('"no-new-privileges"', create_command)
        self.assertNotIn("--env-file", create_command)
        self.assertNotIn("PASSWORD", create_command)
        self.assertNotIn("API_KEY", create_command)
        self.assertNotIn("postgresql://postgres:", source)
        self.assertIn("postgresql://postgres@127.0.0.1", source)

    def test_write_exclusive_publishes_atomically_and_refuses_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            target = root / "report.json"
            REHEARSAL._write_exclusive(target, b"first\n")
            self.assertEqual(target.read_bytes(), b"first\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(root.glob(".report.json.*.tmp")), [])

            with self.assertRaisesRegex(REHEARSAL.RehearsalError, "overwrite"):
                REHEARSAL._write_exclusive(target, b"second\n")
            self.assertEqual(target.read_bytes(), b"first\n")
            self.assertEqual(list(root.glob(".report.json.*.tmp")), [])

    def test_claim_json_replays_identical_record_and_recovers_publish_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            value = {"phase": "intent", "payload": {"operation": self.OPERATION_ID}}
            target = root / "intent.json"

            self.assertEqual(REHEARSAL._claim_json(target, value), value)
            self.assertEqual(REHEARSAL._claim_json(target, value), value)
            with self.assertRaisesRegex(REHEARSAL.RehearsalError, "differs"):
                REHEARSAL._claim_json(target, {"phase": "changed"})

            raced_target = root / "raced.json"
            write_exclusive = REHEARSAL._write_exclusive

            def publish_then_report_race(path: Path, payload: bytes) -> None:
                write_exclusive(path, payload)
                raise REHEARSAL.RehearsalError("simulated publish race")

            with mock.patch.object(
                REHEARSAL,
                "_write_exclusive",
                side_effect=publish_then_report_race,
            ):
                self.assertEqual(REHEARSAL._claim_json(raced_target, value), value)
            self.assertEqual(REHEARSAL._load_private_json(raced_target), value)
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_controlled_run_disables_optional_git_locks(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            REHEARSAL.subprocess,
            "run",
            return_value=completed,
        ) as subprocess_run:
            self.assertIs(
                REHEARSAL._run(["git", "status", "--short"]),
                completed,
            )

        environment = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(environment["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(environment["HOME"], "/nonexistent")

    def test_pg_dump_outer_failure_terminates_and_proves_remote_work_absent(
        self,
    ) -> None:
        output = tempfile.TemporaryFile()
        failure = REHEARSAL.RehearsalError("simulated outer timeout")
        try:
            with (
                mock.patch.object(REHEARSAL, "_run", side_effect=failure) as run,
                mock.patch.object(
                    REHEARSAL, "_terminate_and_prove_pg_dump_absent"
                ) as cleanup,
                self.assertRaisesRegex(
                    REHEARSAL.RehearsalError, "simulated outer timeout"
                ),
            ):
                REHEARSAL._run_pg_dump_with_cleanup(
                    container_id="a" * 64,
                    user="nexpoly",
                    database="nexpoly",
                    operation_id=self.OPERATION_ID,
                    output=output,
                    timeout=12.5,
                )
        finally:
            output.close()

        command = run.call_args.args[0]
        application_name = "nexpoly_rehearsal_" + hashlib.sha256(
            self.OPERATION_ID.encode("ascii")
        ).hexdigest()[:16]
        self.assertIn("/usr/bin/timeout", command)
        timeout_index = command.index("/usr/bin/timeout")
        self.assertEqual(
            command[timeout_index : timeout_index + 7],
            [
                "/usr/bin/timeout",
                "-s",
                "TERM",
                "-k",
                f"{REHEARSAL.PG_DUMP_CLEANUP_SECONDS}s",
                "12s",
                "pg_dump",
            ],
        )
        self.assertIn(f"PGAPPNAME={application_name}", command)
        self.assertTrue(
            any(application_name in argument for argument in command)
        )
        cleanup.assert_called_once_with(
            "a" * 64,
            "nexpoly",
            "nexpoly",
            application_name,
        )

    def test_pg_dump_cleanup_proves_server_backend_and_container_process_zero(
        self,
    ) -> None:
        application_name = "nexpoly_rehearsal_" + "1" * 16
        with (
            mock.patch.object(
                REHEARSAL,
                "_pg_dump_backend_count",
                side_effect=[1, 0],
            ) as backend,
            mock.patch.object(
                REHEARSAL,
                "_pg_dump_process_absent",
                side_effect=[False, True],
            ) as process,
        ):
            REHEARSAL._terminate_and_prove_pg_dump_absent(
                "a" * 64,
                "nexpoly",
                "nexpoly",
                application_name,
            )

        self.assertTrue(backend.call_args_list[0].kwargs["terminate"])
        self.assertFalse(backend.call_args_list[1].kwargs["terminate"])
        self.assertEqual(process.call_args_list[0].kwargs["signal"], "TERM")
        self.assertNotIn("signal", process.call_args_list[1].kwargs)

    def test_pg_dump_cleanup_fails_closed_when_residue_cannot_be_proved_absent(
        self,
    ) -> None:
        application_name = "nexpoly_rehearsal_" + "2" * 16
        with (
            mock.patch.object(REHEARSAL, "PG_DUMP_CLEANUP_SECONDS", 0),
            mock.patch.object(
                REHEARSAL, "_pg_dump_backend_count", return_value=1
            ),
            mock.patch.object(
                REHEARSAL, "_pg_dump_process_absent", return_value=False
            ),
            self.assertRaisesRegex(
                REHEARSAL.RehearsalError, "cannot prove timed-out pg_dump"
            ),
        ):
            REHEARSAL._terminate_and_prove_pg_dump_absent(
                "a" * 64,
                "nexpoly",
                "nexpoly",
                application_name,
            )


if __name__ == "__main__":
    unittest.main()
