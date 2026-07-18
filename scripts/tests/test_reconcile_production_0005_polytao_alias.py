from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/reconcile_production_0005_polytao_alias.py"
SPEC = importlib.util.spec_from_file_location("production_alias_reconcile", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FIXTURE_RESTORE_IMAGE = {
    "digest_ref": MODULE.POSTGRES16_IMAGE,
    "image_id": "sha256:" + "f" * 64,
}


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def inventory(*, phase: str = "pre", restored: bool = False) -> dict[str, object]:
    rows = [{"job_id": "fixture", "status": "completed"}]
    structure = {
        "columns": [{"column_name": "job_id"}],
        "indexes": [{"indexname": "polytao_jobs_pkey"}],
        "constraints": [{"name": "polytao_jobs_pkey"}],
        "triggers": [],
    }
    ledger_structure = {
        "columns": [{"column_name": "version"}],
        "indexes": [{"indexname": "schema_migrations_pkey"}],
        "constraints": [{"name": "schema_migrations_pkey"}],
        "triggers": [],
    }
    ledger_pairs = MODULE.PRE_LEDGER if phase == "pre" else MODULE.POST_LEDGER
    ledger = [
        {
            "version": version,
            "checksum": checksum,
            "applied_at": (
                MODULE.ALIAS_APPLIED_AT
                if version == MODULE.ALIAS_VERSION
                else "2026-07-08T00:00:00.000000Z"
            ),
        }
        for version, checksum in ledger_pairs
    ]
    owner = "postgres" if restored else MODULE.DATABASE_OWNER
    relation = {
        "kind": "r",
        "persistence": "p",
        "is_partition": False,
        "row_security": False,
        "force_row_security": False,
        "owner": owner,
        "parents": 0,
        "children": 0,
    }
    return {
        "database": "nexpoly_alias_restore" if restored else MODULE.DATABASE_NAME,
        "current_user": "postgres" if restored else MODULE.DATABASE_USER,
        "database_owner": "postgres" if restored else MODULE.DATABASE_OWNER,
        "server_version_num": 160014,
        "in_recovery": False,
        "system_identifier": "123456789" if restored else MODULE.SYSTEM_IDENTIFIER,
        "transaction_read_only": "on",
        "ledger": ledger,
        "rows": rows,
        "status_counts": {"completed": 1},
        **structure,
        "ledger_columns": ledger_structure["columns"],
        "ledger_indexes": ledger_structure["indexes"],
        "ledger_constraints": ledger_structure["constraints"],
        "ledger_triggers": ledger_structure["triggers"],
        "polytao_relation": dict(relation),
        "ledger_relation": dict(relation),
    }


def operation_inventory(*, phase: str) -> dict[str, object]:
    canonical = {
        "version": MODULE.CANONICAL_VERSION,
        "checksum": MODULE.ALIAS_CHECKSUM,
        "applied_at": "2026-07-08T00:00:00.000000Z",
    }
    alias = {
        "version": MODULE.ALIAS_VERSION,
        "checksum": MODULE.ALIAS_CHECKSUM,
        "applied_at": MODULE.ALIAS_APPLIED_AT,
    }
    return {
        "ledger": [alias, canonical] if phase == "pre" else [canonical],
        "archive": {"rows_sha256": "fixture"},
    }


@contextlib.contextmanager
def fixture_expectations():
    document = inventory()
    structure = MODULE._structure(document)
    ledger_structure = MODULE._structure(document, prefix="ledger_")
    with (
        mock.patch.object(MODULE, "EXPECTED_SCHEMA_SHA256", digest(structure)),
        mock.patch.object(
            MODULE, "EXPECTED_LEDGER_SCHEMA_SHA256", digest(ledger_structure)
        ),
        mock.patch.object(
            MODULE,
            "EXPECTED_STRUCTURE_COUNTS",
            {key: len(value) for key, value in structure.items()},
        ),
        mock.patch.object(
            MODULE,
            "EXPECTED_LEDGER_STRUCTURE_COUNTS",
            {key: len(value) for key, value in ledger_structure.items()},
        ),
    ):
        yield


class FakeSession:
    def __init__(self, *, clients: int = 0, delete: bool = True) -> None:
        self.clients = clients
        self.delete = delete
        self.deleted = False
        self.commands: list[str] = []
        self.json_sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def scalar(self, sql: str) -> str:
        self.commands.append(sql)
        if "pg_try_advisory_lock" in sql:
            return "true"
        if "pg_stat_activity" in sql:
            return str(self.clients)
        raise AssertionError(f"unexpected scalar SQL: {sql}")

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def json(self, sql: str) -> dict[str, object]:
        self.json_sql.append(sql)
        if "WITH deleted AS" in sql:
            if self.delete:
                self.deleted = True
                return {
                    "rows": [
                        {
                            "version": MODULE.ALIAS_VERSION,
                            "checksum": MODULE.ALIAS_CHECKSUM,
                            "applied_at": MODULE.ALIAS_APPLIED_AT,
                        }
                    ]
                }
            return {"rows": []}
        return {"fixture_phase": "post" if self.deleted else "pre"}


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="alias-reconcile-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_validates_exact_pre_and_post_inventories(self) -> None:
        with fixture_expectations():
            before = MODULE.validate_inventory(inventory(), expected_phase="pre")
            after = MODULE.validate_inventory(
                inventory(phase="post"), expected_phase="post"
            )
            restored = MODULE.validate_inventory(
                inventory(restored=True), expected_phase="pre", restored=True
            )
        self.assertEqual(before["archive"]["row_count"], 1)
        self.assertNotIn(MODULE.ALIAS_VERSION, [row["version"] for row in after["ledger"]])
        self.assertEqual(restored["database"], "nexpoly_alias_restore")
        MODULE._require_post_matches_before(before, after)
        MODULE._require_restore_matches_before(before, restored)

        changed_after = {**after, "archive": {**after["archive"], "row_count": 2}}
        with self.assertRaisesRegex(MODULE.ReconcileError, "business snapshot"):
            MODULE._require_post_matches_before(before, changed_after)

    def test_rejects_alias_timestamp_and_ledger_but_accepts_dynamic_business_rows(
        self,
    ) -> None:
        with fixture_expectations():
            changed = inventory()
            next(
                row
                for row in changed["ledger"]
                if row["version"] == MODULE.ALIAS_VERSION
            )["applied_at"] = "2026-07-08T00:00:00.000000Z"
            with self.assertRaisesRegex(MODULE.ReconcileError, "alias tuple"):
                MODULE.validate_inventory(changed, expected_phase="pre")

            changed = inventory()
            changed["ledger"].append(
                {"version": "0009_unreviewed", "checksum": "x", "applied_at": "x"}
            )
            with self.assertRaisesRegex(MODULE.ReconcileError, "exact pre"):
                MODULE.validate_inventory(changed, expected_phase="pre")

            changed = inventory()
            changed["rows"].append({"job_id": "changed", "status": "failed"})
            changed["status_counts"] = {"completed": 1, "failed": 1}
            dynamic = MODULE.validate_inventory(changed, expected_phase="pre")
            self.assertEqual(dynamic["archive"]["row_count"], 2)
            self.assertEqual(
                dynamic["archive"]["status_counts"],
                {"completed": 1, "failed": 1},
            )

            changed["status_counts"] = {"completed": 1}
            with self.assertRaisesRegex(MODULE.ReconcileError, "business snapshot"):
                MODULE.validate_inventory(changed, expected_phase="pre")

    def test_rejects_cluster_relation_and_restore_identity_drift(self) -> None:
        with fixture_expectations():
            changed = inventory()
            changed["system_identifier"] = "another-cluster"
            with self.assertRaisesRegex(MODULE.ReconcileError, "cluster identity"):
                MODULE.validate_inventory(changed, expected_phase="pre")

            changed = inventory()
            changed["polytao_relation"]["row_security"] = True
            with self.assertRaisesRegex(MODULE.ReconcileError, "relation identity"):
                MODULE.validate_inventory(changed, expected_phase="pre")

            changed = inventory(restored=True)
            changed["server_version_num"] = 170000
            with self.assertRaisesRegex(MODULE.ReconcileError, "restore identity"):
                MODULE.validate_inventory(
                    changed, expected_phase="pre", restored=True
                )

    def test_dsn_is_fixed_and_password_never_enters_public_identity(self) -> None:
        secret = "do-not-log-this"
        environment, public = MODULE._parse_dsn(
            f"postgresql://polyprop:{secret}@127.0.0.1:55432/nexpoly?sslmode=disable"
        )
        self.assertEqual(environment["PGPASSWORD"], secret)
        self.assertNotIn(secret, json.dumps(public))
        self.assertEqual(public["database"], "nexpoly")
        self.assertEqual(public["user"], "polyprop")

    def test_dsn_rejects_wrong_database_user_options_and_control_characters(self) -> None:
        invalid = (
            "postgresql://polyprop:secret@127.0.0.1:55432/other?sslmode=disable",
            "postgresql://other:secret@127.0.0.1:55432/nexpoly?sslmode=disable",
            "postgresql://polyprop:secret@db:55432/nexpoly?sslmode=disable",
            "postgresql://polyprop:secret@127.0.0.1:5432/nexpoly?sslmode=disable",
            "postgresql://polyprop:secret@127.0.0.1:55432/nexpoly",
            "postgresql://polyprop:secret@127.0.0.1:55432/nexpoly?sslmode=require",
            "postgresql://polyprop:secret@127.0.0.1:55432/nexpoly?options=-c%20evil=1",
            "postgresql://polyprop:secret@127.0.0.1:55432/nexpoly\nleak?sslmode=disable",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(MODULE.ReconcileError):
                    MODULE._parse_dsn(value)

    def test_docker_endpoint_must_be_the_local_unix_socket(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='"unix:///var/run/docker.sock"\n', stderr=""
        )
        MODULE._require_local_docker_endpoint(runner)
        runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='"tcp://remote.invalid:2376"\n', stderr=""
        )
        with self.assertRaisesRegex(MODULE.ReconcileError, "local Docker"):
            MODULE._require_local_docker_endpoint(runner)

    def test_restore_image_keeps_index_digest_separate_from_config_image_id(
        self,
    ) -> None:
        runner = mock.Mock()
        runner.run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='"unix:///var/run/docker.sock"\n',
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(FIXTURE_RESTORE_IMAGE["image_id"]) + "\n",
                stderr="",
            ),
        ]
        operation = MODULE.Reconciliation(
            operation_id="alias-0005-image-test",
            environment={
                "NEXPOLY_PRODUCTION_POSTGRES_DSN": (
                    "postgresql://polyprop:secret@127.0.0.1:55432/"
                    "nexpoly?sslmode=disable"
                )
            },
            runner=runner,
        )
        self.assertEqual(operation._image_identity(), FIXTURE_RESTORE_IMAGE)
        self.assertNotEqual(
            FIXTURE_RESTORE_IMAGE["image_id"],
            "sha256:" + MODULE.POSTGRES16_IMAGE.rsplit("@sha256:", 1)[1],
        )

        runner.run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='"unix:///var/run/docker.sock"\n',
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout='"not-a-digest"\n', stderr=""
            ),
        ]
        with self.assertRaisesRegex(MODULE.ReconcileError, "malformed"):
            operation._image_identity()

    def test_operation_id_and_cli_do_not_accept_general_ledger_selectors(self) -> None:
        self.assertEqual(
            MODULE.safe_operation_id("alias-0005-20260717"),
            "alias-0005-20260717",
        )
        for value in ("short", "../traversal", "UPPERCASE-0005"):
            with self.subTest(value=value):
                with self.assertRaises(MODULE.ReconcileError):
                    MODULE.safe_operation_id(value)
        parser = MODULE.build_parser()
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(
                [
                    "--operation-id",
                    "alias-0005-20260717",
                    "--checksum",
                    "arbitrary",
                ]
            )

    def test_atomic_json_is_private_and_rejects_symlink_parent(self) -> None:
        directory = self.root / "private"
        directory.mkdir(mode=0o700)
        path = directory / "record.json"
        MODULE.atomic_json(path, {"safe": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"safe": True})
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(MODULE.ReconcileError):
            MODULE.atomic_json(link / "bad.json", {"safe": False})

    def _operation(self, session: FakeSession):
        runtime = self.root / "runtime"
        for relative in (
            "state",
            str(MODULE.STATE_ROOT_RELATIVE),
            str(MODULE.AUDIT_ROOT_RELATIVE),
            str(MODULE.BACKUP_ROOT_RELATIVE),
        ):
            path = runtime / relative
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        lock = runtime / MODULE.DEPLOY_LOCK_RELATIVE
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)
        environment = {
            "NEXPOLY_PRODUCTION_POSTGRES_DSN": (
                "postgresql://polyprop:secret@127.0.0.1:55432/"
                "nexpoly?sslmode=disable"
            )
        }
        patches = [
            mock.patch.object(MODULE, "RUNTIME_ROOT", runtime),
            mock.patch.object(
                MODULE,
                "validate_inventory",
                side_effect=lambda document, **_kwargs: operation_inventory(
                    phase=(
                        "pre"
                        if document.get("fixture_phase") == "pre"
                        else "post"
                    )
                ),
            ),
            mock.patch.object(
                MODULE,
                "_psql_json",
                return_value={"fixture_phase": "post"},
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        operation = MODULE.Reconciliation(
            operation_id="alias-0005-20260717",
            environment=environment,
            session_factory=lambda _environment: session,
        )
        operation.identities = mock.Mock(
            return_value=(
                {"release_id": "r", "source_sha": "s"},
                {"sha": MODULE.LEGACY_SOURCE_SHA, "tree": MODULE.LEGACY_SOURCE_TREE},
                {"binary": {"sha256": "b"}},
            )
        )
        operation._image_identity = mock.Mock(return_value=FIXTURE_RESTORE_IMAGE)
        operation._bridge_authority = mock.Mock(
            return_value=(
                {"schema_version": 1, "fixture": "bridge"},
                {"schema_version": 1, "fixture": "takeover-runtime"},
            )
        )
        operation._require_takeover_runtime_match = mock.Mock()
        operation._runtime_stop_fence = mock.Mock(return_value={"fixture": True})
        operation._validate_mandatory_evidence = mock.Mock(
            return_value=({"dump_sha256": "dump"}, {"dump_sha256": "dump"})
        )
        operation._remove_owned_container = mock.Mock()
        operation._archive = mock.Mock(
            side_effect=lambda marker, before: (
                operation._write_marker(
                    marker,
                    "backup-complete",
                    before=before,
                    database_backup={"dump_sha256": "dump"},
                )
                or {"dump_sha256": "dump"}
            )
        )
        operation._restore_proof = mock.Mock(
            side_effect=lambda marker, archive: (
                operation._write_marker(
                    marker,
                    "restore-started",
                    restore_container={"name": "fixture"},
                )
                or operation._write_marker(
                    marker,
                    "restore-verified",
                    isolated_restore={"dump_sha256": archive["dump_sha256"]},
                )
                or marker["isolated_restore"]
            )
        )
        operation._finalize = mock.Mock(
            return_value={"status": "completed", "operation_id": operation.operation_id}
        )
        return operation

    @staticmethod
    def _advance_marker(
        operation: object, marker: dict[str, object], *, committed: bool
    ) -> None:
        before = operation_inventory(phase="pre")
        after = operation_inventory(phase="post")
        operation._write_marker(  # type: ignore[attr-defined]
            marker, "runtime-fenced", runtime_stop_fence={"fixture": True}
        )
        operation._write_marker(  # type: ignore[attr-defined]
            marker, "locked-preverified", before=before
        )
        operation._write_marker(  # type: ignore[attr-defined]
            marker,
            "backup-complete",
            database_backup={"dump_sha256": "dump"},
        )
        operation._write_marker(  # type: ignore[attr-defined]
            marker, "restore-started", restore_container={"name": "fixture"}
        )
        operation._write_marker(  # type: ignore[attr-defined]
            marker,
            "restore-verified",
            isolated_restore={"dump_sha256": "dump"},
        )
        operation._write_marker(  # type: ignore[attr-defined]
            marker, "mutation-intent", mutation_intent={"fixture": True}
        )
        phase = "mutation-committed" if committed else "mutation-commit-started"
        operation._write_marker(marker, phase, after=after)  # type: ignore[attr-defined]

    @staticmethod
    def _write_finalization_evidence(
        operation: object,
        marker: dict[str, object],
        *,
        binaries: dict[str, object],
        after: dict[str, object],
        completed_at: str = "2026-07-17T00:00:00Z",
    ) -> Path:
        dump = b"fixture database dump"
        MODULE.atomic_bytes(operation.dump_path, dump)  # type: ignore[attr-defined]
        MODULE.atomic_bytes(  # type: ignore[attr-defined]
            operation.dump_sha_path,  # type: ignore[attr-defined]
            (hashlib.sha256(dump).hexdigest() + "\n").encode("ascii"),
        )
        MODULE.atomic_bytes(  # type: ignore[attr-defined]
            operation.restore_list_path,  # type: ignore[attr-defined]
            (
                "TABLE DATA generation polytao_jobs\n"
                "TABLE DATA governance schema_migrations\n"
            ).encode(),
        )
        MODULE.atomic_json(
            operation.audit_dir / "isolated-postgres16-restore.json",  # type: ignore[attr-defined]
            marker["isolated_restore"],
        )
        MODULE.atomic_json(
            operation.audit_dir / "database-after.json",  # type: ignore[attr-defined]
            after,
        )
        audit_path = operation.audit_dir / "AUDIT-MANIFEST.json"  # type: ignore[attr-defined]
        audit = {
            "schema_version": 1,
            "operation_id": operation.operation_id,  # type: ignore[attr-defined]
            "outcome": "completed",
            "identity": marker["identity"],
            "database_before": marker["before"],
            "database_after": after,
            "database_backup": marker["database_backup"],
            "isolated_restore": marker["isolated_restore"],
            "runtime_stop_fence": marker["runtime_stop_fence"],
            "runtime_stop_fence_sha256": MODULE.sha256_bytes(
                MODULE.canonical_json_bytes(marker["runtime_stop_fence"])
            ),
            "binaries": binaries,
            "files": operation._evidence_file_inventory(),  # type: ignore[attr-defined]
            "completed_at": completed_at,
        }
        MODULE.atomic_json(audit_path, audit)
        return audit_path

    def test_apply_uses_exact_one_row_cas_and_commits(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        result = operation.apply()
        self.assertEqual(result["status"], "completed")
        delete_sql = next(sql for sql in session.json_sql if "WITH deleted AS" in sql)
        self.assertIn(f"version='{MODULE.ALIAS_VERSION}'", delete_sql)
        self.assertIn(f"checksum='{MODULE.ALIAS_CHECKSUM}'", delete_sql)
        self.assertIn(f"applied_at='{MODULE.ALIAS_APPLIED_AT}'", delete_sql)
        self.assertNotIn("LIKE", delete_sql)
        self.assertIn("COMMIT", session.commands)
        self.assertFalse(any("INSERT" in sql for sql in session.json_sql))

    def test_apply_rolls_back_when_cas_does_not_delete_exactly_one(self) -> None:
        session = FakeSession(delete=False)
        operation = self._operation(session)
        with self.assertRaisesRegex(MODULE.ReconcileError, "compare-and-swap"):
            operation.apply()
        self.assertIn("ROLLBACK", session.commands)
        marker = MODULE.load_private_json(operation.marker_path)
        self.assertEqual(marker["phase"], "mutation-intent")

    def test_apply_refuses_other_database_clients_before_backup(self) -> None:
        session = FakeSession(clients=1)
        operation = self._operation(session)
        with self.assertRaisesRegex(MODULE.ReconcileError, "another client"):
            operation.apply()
        operation._archive.assert_not_called()
        self.assertIn("ROLLBACK", session.commands)

    def test_unknown_commit_post_state_recovers_without_second_delete(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        self._advance_marker(operation, marker, committed=False)
        recovered = operation_inventory(phase="post")
        operation._begin_recovery_locked = mock.Mock(
            return_value=("post", recovered)
        )
        result = operation.apply()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any("WITH deleted AS" in sql for sql in session.json_sql))

    def test_committed_marker_resumes_finalization_without_prestate_or_delete(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        self._advance_marker(operation, marker, committed=True)
        recovered = operation_inventory(phase="post")
        operation._begin_recovery_locked = mock.Mock(
            return_value=("post", recovered)
        )
        result = operation.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(session.json_sql, [])

    def test_runtime_restart_with_pre_state_cannot_adopt_replacement_fence(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        self._advance_marker(operation, marker, committed=False)
        restarted_fence = {"fixture": "restarted-and-stopped"}
        operation._runtime_stop_fence = mock.Mock(return_value=restarted_fence)
        marker_before = operation.marker_path.read_bytes()

        with self.assertRaisesRegex(
            MODULE.ReconcileError,
            "cannot adopt replacement readers or PostgreSQL",
        ):
            operation.apply()

        self.assertEqual(operation.marker_path.read_bytes(), marker_before)
        operation._archive.assert_not_called()

    def test_takeover_runtime_match_binds_readers_worker_and_postgres(self) -> None:
        operation = self._operation(FakeSession())
        backend_id = "1" * 64
        web_id = "2" * 64
        postgres_id = "3" * 64
        backend_image = "sha256:" + "4" * 64
        web_image = "sha256:" + "5" * 64
        postgres_image = "sha256:" + "6" * 64
        current = {
            "database_system_identifier": MODULE.SYSTEM_IDENTIFIER,
            "containers": [
                {
                    "service": "backend",
                    "id": backend_id,
                    "image": backend_image,
                },
                {
                    "service": "nginx",
                    "id": web_id,
                    "image": web_image,
                },
                {
                    "service": "lab-postgres",
                    "id": postgres_id,
                    "image": postgres_image,
                    "data_volume": {"name": "nexpoly_postgres_data"},
                },
            ],
            "monomer_md_unit": {
                "FragmentSHA256": "7" * 64,
            },
        }
        takeover = {
            "readers_stopped": True,
            "postgres_running_untouched": True,
            "backend_container_id": backend_id,
            "backend_image_id": backend_image,
            "web_container_id": web_id,
            "web_image_id": web_image,
            "worker_unit_name": "nexpoly-monomer-md-worker.service",
            "worker_unit_sha256": "7" * 64,
            "postgres_container_id": postgres_id,
            "postgres_image_id": postgres_image,
            "postgres_data_volume": "nexpoly_postgres_data",
            "postgres_system_identifier": MODULE.SYSTEM_IDENTIFIER,
        }
        validator = (
            MODULE.Reconciliation._require_takeover_runtime_match
        )
        validator(operation, takeover, current)
        changed = dict(takeover)
        changed["postgres_container_id"] = "8" * 64
        with self.assertRaisesRegex(
            MODULE.ReconcileError,
            "differ from legacy takeover",
        ):
            validator(operation, changed, current)

    def test_runtime_restart_with_post_state_cannot_adopt_replacement_fence(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        self._advance_marker(operation, marker, committed=False)
        restarted_fence = {"fixture": "restarted-and-stopped"}
        operation._runtime_stop_fence = mock.Mock(return_value=restarted_fence)
        marker_before = operation.marker_path.read_bytes()

        with self.assertRaisesRegex(
            MODULE.ReconcileError,
            "cannot adopt replacement readers or PostgreSQL",
        ):
            operation.apply()

        self.assertEqual(operation.marker_path.read_bytes(), marker_before)
        self.assertFalse(any("WITH deleted AS" in sql for sql in session.json_sql))

    def test_completed_replay_with_changed_runtime_fence_is_byte_immutable(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        self._advance_marker(operation, marker, committed=True)
        after = marker["after"]
        self.assertIsInstance(after, dict)
        audit_path = self._write_finalization_evidence(
            operation,
            marker,
            binaries=binaries,
            after=after,
        )
        operation._write_marker(
            marker,
            "completed",
            audit_manifest_sha256=MODULE.sha256_file(audit_path),
            completed_at="2026-07-17T00:00:00Z",
        )
        marker_before = operation.marker_path.read_bytes()
        audit_before = audit_path.read_bytes()
        restarted_fence = {"fixture": "new-stopped-runtime"}
        operation._runtime_stop_fence = mock.Mock(return_value=restarted_fence)
        with self.assertRaisesRegex(
            MODULE.ReconcileError,
            "cannot adopt replacement readers or PostgreSQL",
        ):
            operation.apply()

        self.assertEqual(operation.marker_path.read_bytes(), marker_before)
        self.assertEqual(audit_path.read_bytes(), audit_before)
        self.assertFalse(any("WITH deleted AS" in sql for sql in session.json_sql))
        operation._finalize.assert_not_called()

    def test_existing_audit_rejects_changed_runtime_fence_without_second_cas(
        self,
    ) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        self._advance_marker(operation, marker, committed=True)
        after = marker["after"]
        self.assertIsInstance(after, dict)
        audit_path = self._write_finalization_evidence(
            operation,
            marker,
            binaries=binaries,
            after=after,
        )
        audit_before = audit_path.read_bytes()
        restarted_fence = {"fixture": "new-stopped-runtime"}
        operation._runtime_stop_fence = mock.Mock(return_value=restarted_fence)
        operation._finalize = MODULE.Reconciliation._finalize.__get__(
            operation, MODULE.Reconciliation
        )

        marker_before = operation.marker_path.read_bytes()
        with self.assertRaisesRegex(
            MODULE.ReconcileError,
            "cannot adopt replacement readers or PostgreSQL",
        ):
            operation.apply()

        self.assertEqual(operation.marker_path.read_bytes(), marker_before)
        self.assertEqual(audit_path.read_bytes(), audit_before)
        self.assertFalse(any("WITH deleted AS" in sql for sql in session.json_sql))

    def test_directory_intent_recovers_after_only_audit_directory_was_created(
        self,
    ) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = {
            "schema_version": 1,
            "action": "reconcile-production-0005-polytao-alias",
            "phase": "directory-intent",
            "identity": identity,
            "operation_directories": {
                "audit": str(operation.audit_dir),
                "backup": str(operation.backup_dir),
            },
            "started_at": "2026-07-17T00:00:00Z",
            "updated_at": "2026-07-17T00:00:00Z",
        }
        MODULE.atomic_json(operation.marker_path, marker)
        operation.audit_dir.mkdir(mode=0o700)
        self.assertFalse(operation.backup_dir.exists())

        recovered = operation._new_marker(identity)

        self.assertEqual(recovered["phase"], "planned")
        self.assertTrue(operation.audit_dir.is_dir())
        self.assertTrue(operation.backup_dir.is_dir())
        self.assertEqual(operation.audit_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(operation.backup_dir.stat().st_mode & 0o777, 0o700)

    def test_complete_backup_files_resume_and_bind_archive_to_marker(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        operation._write_marker(
            marker, "runtime-fenced", runtime_stop_fence={"fixture": True}
        )
        before = {"ledger": [], "archive": {"rows_sha256": "fixture"}}
        operation._write_marker(marker, "locked-preverified", before=before)
        operation._write_marker(marker, "backup-started", before=before)
        dump = b"complete fixture dump"
        MODULE.atomic_bytes(operation.dump_path, dump)
        MODULE.atomic_bytes(
            operation.dump_sha_path,
            (hashlib.sha256(dump).hexdigest() + "\n").encode("ascii"),
        )
        MODULE.atomic_bytes(
            operation.restore_list_path,
            (
                "TABLE DATA generation polytao_jobs\n"
                "TABLE DATA governance schema_migrations\n"
            ).encode(),
        )

        archive = MODULE.Reconciliation._archive(operation, marker, before)

        self.assertEqual(marker["phase"], "backup-complete")
        self.assertEqual(marker["database_backup"], archive)
        self.assertEqual(
            MODULE.load_private_json(operation.marker_path)["database_backup"],
            archive,
        )
        self.assertEqual(archive["dump_sha256"], hashlib.sha256(dump).hexdigest())

    def test_unknown_docker_run_response_still_executes_owned_cleanup(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        control, source, binaries = operation.identities()
        identity = MODULE._marker_identity(
            operation_id=operation.operation_id,
            control=control,
            source=source,
            binaries=binaries,
            database_endpoint=operation.database_endpoint,
            restore_image=FIXTURE_RESTORE_IMAGE,
            bridge_authority=operation._bridge_authority(control, source)[0],
        )
        operation._prepare_roots()
        marker = operation._new_marker(identity)
        operation._write_marker(
            marker, "runtime-fenced", runtime_stop_fence={"fixture": True}
        )
        before = {"ledger": [], "archive": {"rows_sha256": "fixture"}}
        operation._write_marker(marker, "locked-preverified", before=before)
        archive = {"dump_sha256": "d" * 64}
        operation._write_marker(
            marker, "backup-complete", database_backup=archive
        )
        operation._restore_proof = MODULE.Reconciliation._restore_proof.__get__(
            operation, MODULE.Reconciliation
        )
        operation._remove_owned_container = mock.Mock()

        with (
            mock.patch.object(
                MODULE,
                "_run_checked",
                side_effect=MODULE.ReconcileError("unknown docker run result"),
            ),
            self.assertRaisesRegex(MODULE.ReconcileError, "unknown docker"),
        ):
            operation._restore_proof(marker, archive)

        expected = mock.call(operation._container_name(), archive)
        self.assertEqual(operation._remove_owned_container.call_args_list, [expected, expected])

    def test_marker_fences_a_different_operation_identity(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        operation._prepare_roots()
        MODULE.atomic_json(
            operation.marker_path,
            {
                "schema_version": 1,
                "action": "reconcile-production-0005-polytao-alias",
                "phase": "planned",
                "identity": {
                    "operation_id": "another-operation",
                    "restore_image": FIXTURE_RESTORE_IMAGE,
                    "bridge_authority": {"fixture": "another-bridge"},
                },
                "operation_directories": {
                    "audit": str(operation.audit_dir),
                    "backup": str(operation.backup_dir),
                },
                "started_at": "2026-07-17T00:00:00Z",
                "updated_at": "2026-07-17T00:00:00Z",
            },
        )
        with self.assertRaisesRegex(MODULE.ReconcileError, "another identity"):
            operation.apply()

    def test_plan_does_not_create_state_or_audit_directories(self) -> None:
        session = FakeSession()
        operation = self._operation(session)
        for path in (operation.state_root, operation.audit_root, operation.backup_root):
            # The fixture needs bootstrap roots for apply tests; use a fresh child here.
            self.assertTrue(path.exists())
        operation.identities = mock.Mock(return_value=({"release_id": "r"}, {}, {}))
        operation._database_inventory = mock.Mock(return_value={"ledger": [], "archive": {}})
        operation._image_identity = mock.Mock(
            return_value=FIXTURE_RESTORE_IMAGE
        )
        before = sorted(str(path) for path in operation.state_root.rglob("*"))
        result = operation.plan()
        after = sorted(str(path) for path in operation.state_root.rglob("*"))
        self.assertFalse(result["apply"])
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
