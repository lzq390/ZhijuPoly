from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from scripts.tests import test_pull_deploy_controller as PULL_FIXTURES


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/reconcile_production_0005_polytao_alias.py"
SPEC = importlib.util.spec_from_file_location(
    "production_alias_reconcile_integration", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SELECTOR_SCRIPT = ROOT / "scripts/control_runtime_selector.py"
SELECTOR_SPEC = importlib.util.spec_from_file_location(
    "control_runtime_selector_integration", SELECTOR_SCRIPT
)
assert SELECTOR_SPEC is not None and SELECTOR_SPEC.loader is not None
SELECTOR = importlib.util.module_from_spec(SELECTOR_SPEC)
SELECTOR_SPEC.loader.exec_module(SELECTOR)

ENABLE_ENV = "NEXPOLY_ALIAS_DOCKER_INTEGRATION"
ACK_ENV = "NEXPOLY_ALIAS_DOCKER_TEST_ACK"
EXPECTED_ACK = "ephemeral-localhost-only"
PG_BIN_ENV = "NEXPOLY_ALIAS_TEST_PG_BIN"
PINNED_IMAGE = MODULE.POSTGRES16_IMAGE


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
        timeout=timeout,
    )


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@unittest.skipUnless(
    os.environ.get(ENABLE_ENV) == "1",
    f"set {ENABLE_ENV}=1 only in an isolated Docker test environment",
)
class ProductionAliasDockerIntegrationTests(unittest.TestCase):
    """Exercise the one-purpose maintenance path against disposable PostgreSQL."""

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get(ACK_ENV) != EXPECTED_ACK:
            raise RuntimeError(
                f"{ACK_ENV} must be exactly {EXPECTED_ACK!r}; "
                "the integration test will not infer authority"
            )
        if os.environ.get("NEXPOLY_PRODUCTION_POSTGRES_DSN"):
            raise RuntimeError(
                "production PostgreSQL credentials must not be present in the "
                "integration-test process"
            )
        docker = shutil.which("docker")
        if docker != str(MODULE.DOCKER):
            raise RuntimeError("the reviewed /usr/bin/docker binary is required")
        for variable in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            if os.environ.get(variable):
                raise RuntimeError(f"{variable} is forbidden for the Docker integration")
        if os.environ.get("DOCKER_HOST") not in {
            None,
            "",
            "unix:///var/run/docker.sock",
        }:
            raise RuntimeError("the Docker integration requires the local Unix socket")
        endpoint = run(
            [
                str(MODULE.DOCKER),
                "context",
                "inspect",
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            timeout=30,
        ).stdout.strip()
        if json.loads(endpoint) != "unix:///var/run/docker.sock":
            raise RuntimeError("the active Docker context is not the local Unix socket")
        cls.pg_bin = Path(
            os.environ.get(PG_BIN_ENV, "/usr/lib/postgresql/16/bin")
        ).resolve(strict=True)
        for name in ("psql", "pg_dump", "pg_restore"):
            binary = cls.pg_bin / name
            completed = run([str(binary), "--version"], timeout=10)
            if "PostgreSQL) 16." not in completed.stdout:
                raise RuntimeError(f"{binary} is not a PostgreSQL 16 client")

        cls.temporary = tempfile.TemporaryDirectory(
            prefix="nexpoly-alias-docker-integration-"
        )
        cls.root = Path(cls.temporary.name)
        os.chmod(cls.root, 0o700)
        cls.runtime = cls.root / "runtime"
        cls.runtime.mkdir(mode=0o700)
        cls.container_name = (
            f"nexpoly-alias-it-{os.getpid()}-{secrets.token_hex(6)}"
        )
        cls.operation_id = f"alias-it-{secrets.token_hex(8)}"
        cls.password = "integration-only-" + secrets.token_hex(16)
        cls._container_started = False
        try:
            run(
                [
                    str(MODULE.DOCKER),
                    "image",
                    "inspect",
                    PINNED_IMAGE,
                ],
                timeout=30,
            )
            run(
                [
                    str(MODULE.DOCKER),
                    "run",
                    "--detach",
                    "--name",
                    cls.container_name,
                    "--label",
                    "io.nexpoly.test=production-0005-alias-integration",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,nosuid,nodev,mode=0700",
                    "--publish",
                    "127.0.0.1::5432",
                    "--env",
                    "POSTGRES_USER=polyprop",
                    "--env",
                    f"POSTGRES_PASSWORD={cls.password}",
                    "--env",
                    "POSTGRES_DB=nexpoly",
                    PINNED_IMAGE,
                ],
                timeout=120,
            )
            cls._container_started = True
            inspected = json.loads(
                run(
                    [
                        str(MODULE.DOCKER),
                        "container",
                        "inspect",
                        cls.container_name,
                    ],
                    timeout=30,
                ).stdout
            )
            if not isinstance(inspected, list) or len(inspected) != 1:
                raise RuntimeError("ephemeral PostgreSQL container identity is ambiguous")
            record = inspected[0]
            labels = record.get("Config", {}).get("Labels", {})
            ports = record.get("NetworkSettings", {}).get("Ports", {})
            bindings = ports.get("5432/tcp")
            if (
                labels
                != {"io.nexpoly.test": "production-0005-alias-integration"}
                or not isinstance(bindings, list)
                or len(bindings) != 1
                or bindings[0].get("HostIp") != "127.0.0.1"
                or re.fullmatch(r"[1-9][0-9]{0,4}", str(bindings[0].get("HostPort")))
                is None
            ):
                raise RuntimeError("ephemeral PostgreSQL container is not isolated")
            cls.port = int(bindings[0]["HostPort"])
            if cls.port == MODULE.DATABASE_PORT:
                raise RuntimeError("Docker selected the fixed production PostgreSQL port")

            ready = False
            for _ in range(60):
                probe = run(
                    [
                        str(MODULE.DOCKER),
                        "exec",
                        cls.container_name,
                        "pg_isready",
                        "--username",
                        "polyprop",
                        "--dbname",
                        "nexpoly",
                    ],
                    check=False,
                    timeout=10,
                )
                if probe.returncode == 0:
                    ready = True
                    break
                time.sleep(1)
            if not ready:
                raise RuntimeError("ephemeral PostgreSQL 16 did not become ready")
            cls.pg_environment = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(cls.root),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PGHOST": "127.0.0.1",
                "PGPORT": str(cls.port),
                "PGDATABASE": "nexpoly",
                "PGUSER": "polyprop",
                "PGPASSWORD": cls.password,
                "PGSSLMODE": "disable",
                "PGCONNECT_TIMEOUT": "10",
                "PGAPPNAME": "nexpoly-alias-docker-integration-setup",
                "PGOPTIONS": "-c search_path=pg_catalog",
            }
            host_ready = False
            for _ in range(60):
                probe = run(
                    [
                        str(cls.pg_bin / "psql"),
                        "-X",
                        "--no-psqlrc",
                        "--quiet",
                        "--no-align",
                        "--tuples-only",
                        "--set",
                        "ON_ERROR_STOP=1",
                        "--command",
                        "SELECT 1",
                    ],
                    env=cls.pg_environment,
                    check=False,
                    timeout=10,
                )
                if probe.returncode == 0 and probe.stdout.strip() == "1":
                    host_ready = True
                    break
                time.sleep(1)
            if not host_ready:
                raise RuntimeError(
                    "ephemeral PostgreSQL published localhost port did not become ready"
                )
            cls._create_fixture()
        except BaseException:
            cls._cleanup_container()
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cleanup_container()
        cls.temporary.cleanup()

    @classmethod
    def _cleanup_container(cls) -> None:
        if getattr(cls, "_container_started", False):
            run(
                [
                    str(MODULE.DOCKER),
                    "rm",
                    "--force",
                    "--volumes",
                    cls.container_name,
                ],
                check=False,
                timeout=60,
            )
            cls._container_started = False
        operation_id = getattr(cls, "operation_id", None)
        if isinstance(operation_id, str):
            suffix = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:16]
            run(
                [
                    str(MODULE.DOCKER),
                    "rm",
                    "--force",
                    f"nexpoly-alias-restore-{suffix}",
                ],
                check=False,
                timeout=60,
            )

    @classmethod
    def _psql(cls, sql: str) -> str:
        completed = run(
            [
                str(cls.pg_bin / "psql"),
                "-X",
                "--no-psqlrc",
                "--quiet",
                "--no-align",
                "--tuples-only",
                "--set",
                "ON_ERROR_STOP=1",
            ],
            env=cls.pg_environment,
            input_text=sql,
            timeout=120,
        )
        return completed.stdout.strip()

    @classmethod
    def _create_fixture(cls) -> None:
        ledger_values: list[str] = []
        for index, (version, checksum) in enumerate(MODULE.PRE_LEDGER):
            applied_at = (
                MODULE.ALIAS_APPLIED_AT
                if version == MODULE.ALIAS_VERSION
                else f"2026-07-08T02:{index:02d}:00.000000Z"
            )
            ledger_values.append(
                "("
                + ",".join(
                    (
                        sql_literal(version),
                        sql_literal(checksum),
                        sql_literal(applied_at) + "::timestamptz",
                    )
                )
                + ")"
            )
        jobs = []
        # Deliberately differs from the historical production 9-row/7+2 snapshot.
        # Business rows are sealed dynamically only after the maintenance locks.
        for index in range(10):
            status = "completed" if index < 8 else "failed"
            jobs.append(
                "("
                + ",".join(
                    (
                        sql_literal(f"fixture-{index:02d}"),
                        sql_literal(status),
                        sql_literal(
                            json.dumps(
                                {"fixture": index, "stable": True},
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
                        + "::jsonb",
                        sql_literal(f"2026-07-08T04:{index:02d}:00Z")
                        + "::timestamptz",
                    )
                )
                + ")"
            )
        cls._psql(
            """
            CREATE SCHEMA governance AUTHORIZATION polyprop;
            CREATE TABLE governance.schema_migrations (
              version text PRIMARY KEY,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL
            );
            CREATE SCHEMA generation AUTHORIZATION polyprop;
            CREATE TABLE generation.polytao_jobs (
              job_id text PRIMARY KEY,
              status text NOT NULL CHECK (status IN ('completed', 'failed')),
              result jsonb NOT NULL DEFAULT '{}'::jsonb,
              created_at timestamptz NOT NULL
            );
            CREATE INDEX idx_polytao_jobs_status
              ON generation.polytao_jobs (status, created_at);
            INSERT INTO governance.schema_migrations
              (version, checksum, applied_at)
            VALUES
            """
            + ",\n".join(ledger_values)
            + ";\n"
            + """
            INSERT INTO generation.polytao_jobs
              (job_id, status, result, created_at)
            VALUES
            """
            + ",\n".join(jobs)
            + ";\n"
        )

    @staticmethod
    def _prepare_runtime(runtime: Path) -> None:
        for relative in (
            Path("state"),
            MODULE.STATE_ROOT_RELATIVE,
            MODULE.AUDIT_ROOT_RELATIVE,
            MODULE.BACKUP_ROOT_RELATIVE,
        ):
            path = runtime / relative
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        lock = runtime / MODULE.DEPLOY_LOCK_RELATIVE
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)

    def _control_release(self) -> tuple[dict[str, object], Path]:
        release_id = "1" * 64
        control_root = self.runtime / "control-releases" / release_id
        control_root.mkdir(parents=True, mode=0o700)
        os.chmod(control_root.parent, 0o700)
        os.chmod(control_root, 0o700)
        script_name = "reconcile_production_0005_polytao_alias.py"
        sealed_script = control_root / script_name
        sealed_script.write_bytes(SCRIPT.read_bytes())
        os.chmod(sealed_script, 0o700)
        manifest: dict[str, object] = {
            "release_id": release_id,
            "source_sha": "2" * 40,
            "source_tree": "3" * 40,
            "entrypoints": {
                "reconcile-production-0005-alias": {
                    "kind": "python",
                    "file": script_name,
                }
            },
        }
        manifest_path = control_root / SELECTOR.CONTROL_MANIFEST_NAME
        manifest_path.write_bytes(SELECTOR.canonical_json_bytes(manifest) + b"\n")
        os.chmod(manifest_path, 0o600)
        return manifest, control_root

    def test_real_dump_restore_cas_finalize_replay_and_gates(self) -> None:
        self._prepare_runtime(self.runtime)
        control_manifest, control_root = self._control_release()
        control_manifest_path = control_root / SELECTOR.CONTROL_MANIFEST_NAME
        control_script_path = (
            control_root
            / control_manifest["entrypoints"]["reconcile-production-0005-alias"][
                "file"
            ]
        )
        dsn = (
            f"postgresql://polyprop:{self.password}@127.0.0.1:{self.port}/"
            "nexpoly?sslmode=disable"
        )
        environment = {"NEXPOLY_PRODUCTION_POSTGRES_DSN": dsn}
        runner = MODULE.SystemRunner()
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(MODULE, "RUNTIME_ROOT", self.runtime))
            patches.enter_context(mock.patch.object(MODULE, "PG_BIN", self.pg_bin))
            patches.enter_context(
                mock.patch.object(MODULE, "DATABASE_PORT", self.port)
            )
            parsed_environment, endpoint = MODULE._parse_dsn(dsn)
            raw_before = MODULE._psql_json(
                runner, parsed_environment, MODULE.INVENTORY_SQL
            )
            public_before = MODULE._public_inventory(raw_before)
            patches.enter_context(
                mock.patch.object(
                    MODULE,
                    "SYSTEM_IDENTIFIER",
                    str(raw_before["system_identifier"]),
                )
            )
            patches.enter_context(
                mock.patch.object(
                    SELECTOR,
                    "ALIAS_SYSTEM_IDENTIFIER",
                    str(raw_before["system_identifier"]),
                )
            )
            patches.enter_context(
                mock.patch.object(
                    SELECTOR,
                    "ALIAS_DATABASE_ENDPOINT",
                    endpoint,
                )
            )
            patches.enter_context(
                mock.patch.object(
                    SELECTOR,
                    "ALIAS_EXPECTED_SCHEMA_SHA256",
                    public_before["archive"]["schema_sha256"],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    SELECTOR,
                    "ALIAS_EXPECTED_STRUCTURE_COUNTS",
                    public_before["archive"]["structure_counts"],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    SELECTOR,
                    "ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256",
                    public_before["ledger_schema_sha256"],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    SELECTOR,
                    "ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS",
                    public_before["ledger_structure_counts"],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    MODULE,
                    "EXPECTED_SCHEMA_SHA256",
                    public_before["archive"]["schema_sha256"],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    MODULE,
                    "EXPECTED_STRUCTURE_COUNTS",
                    public_before["archive"]["structure_counts"],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    MODULE,
                    "EXPECTED_LEDGER_SCHEMA_SHA256",
                    public_before["ledger_schema_sha256"],
                )
            )
            patches.enter_context(
                mock.patch.object(
                    MODULE,
                    "EXPECTED_LEDGER_STRUCTURE_COUNTS",
                    public_before["ledger_structure_counts"],
                )
            )

            identities = (
                {
                    "release_id": control_manifest["release_id"],
                    "source_sha": control_manifest["source_sha"],
                    "source_tree": control_manifest["source_tree"],
                    "manifest_sha256": MODULE.sha256_file(control_manifest_path),
                    "script_sha256": MODULE.sha256_file(control_script_path),
                },
                {"sha": "5" * 40, "tree": "6" * 40},
                {
                    "integration-harness": {
                        "path": str(Path(__file__).resolve()),
                        "mode": 0o644,
                        "size": Path(__file__).stat().st_size,
                        "sha256": MODULE.sha256_file(Path(__file__)),
                    }
                },
            )
            bridge_operation_id = "bridge-fixture-operation"
            bridge_operation = (
                self.runtime
                / "state"
                / "prepared"
                / bridge_operation_id
            )
            bridge_operation.mkdir(
                parents=True,
                mode=0o700,
            )
            os.chmod(bridge_operation.parent, 0o700)
            os.chmod(bridge_operation, 0o700)
            descriptor_path = bridge_operation / "descriptor.json"
            ready_path = bridge_operation / "ready.json"
            canonical_ledger = [
                {"version": version, "checksum": checksum}
                for version, checksum in (
                    PULL_FIXTURES.CONTROLLER._site_helper_contracts
                    .CANONICAL_MIGRATION_LEDGER
                )
            ]
            through_0008 = [
                row
                for row in canonical_ledger
                if row["version"] <= "0008_polytao_backend_runtime"
            ]
            alias_row = {
                "version": (
                    PULL_FIXTURES.CONTROLLER._site_helper_contracts
                    .LEGACY_0005_ALIAS_VERSION
                ),
                "checksum": (
                    PULL_FIXTURES.CONTROLLER._site_helper_contracts
                    .LEGACY_0005_ALIAS_CHECKSUM
                ),
            }
            external_seed = (
                PULL_FIXTURES.external_database_audit_binding(
                    self.runtime
                )
            )
            external_database_audit = (
                PULL_FIXTURES.external_database_binding_state(
                    external_seed,
                    production_ledger=sorted(
                        [*through_0008, alias_row],
                        key=lambda row: row["version"],
                    ),
                    legacy_relation_present=True,
                    captured_at="2026-07-17T00:01:00Z",
                )
            )
            post_alias_external_database_audit = (
                PULL_FIXTURES.external_database_binding_state(
                    external_database_audit,
                    production_ledger=through_0008,
                    legacy_relation_present=True,
                    captured_at="2026-07-17T00:02:00Z",
                )
            )
            descriptor_document = {
                "schema_version": 3,
                "external_database_audit": external_database_audit,
            }
            MODULE.atomic_json(
                descriptor_path,
                descriptor_document,
            )
            MODULE.atomic_json(
                ready_path,
                {
                    "schema_version": 1,
                    "status": "ready",
                    "operation_id": bridge_operation_id,
                },
            )
            descriptor_sha256 = (
                "sha256:" + MODULE.sha256_file(descriptor_path)
            )
            bridge_authority = {
                "schema_version": 1,
                "operation_id": bridge_operation_id,
                "descriptor": {
                    "path": str(descriptor_path),
                    "sha256": descriptor_sha256,
                },
                "ready": {
                    "path": str(ready_path),
                    "sha256": "sha256:" + MODULE.sha256_file(ready_path),
                },
                "authority": {
                    "sha": identities[0]["source_sha"],
                    "tree": identities[0]["source_tree"],
                    "control_release_id": identities[0]["release_id"],
                },
                "target": {
                    "sha": "7" * 40,
                    "tree": "8" * 40,
                    "control_release_id": "9" * 64,
                },
                "repository_previous": dict(identities[1]),
                "policy": {
                    "id": "integration-fixture-policy",
                    "sha256": "sha256:" + "a" * 64,
                },
                "token": {
                    "token_id": "integration-fixture-token",
                    "token_sha256": "sha256:" + "b" * 64,
                },
                "takeover": {
                    "operation_id": "takeover-fixture-operation",
                    "runtime_identity_sha256": "sha256:" + "c" * 64,
                    "pre_stopped_fence_sha256": "sha256:" + "d" * 64,
                    "applied_record_sha256": "sha256:" + "e" * 64,
                    "binding_sha256": "sha256:" + "f" * 64,
                },
                "prefetch": {
                    "operation_id": "prefetch-fixture-operation",
                    "ready_sha256": "sha256:" + "1" * 64,
                    "identity_sha256": "sha256:" + "2" * 64,
                    "binding_sha256": "sha256:" + "3" * 64,
                },
                "external_database_audit_sha256": (
                    external_database_audit["identity_sha256"]
                ),
            }
            bridge_authority["identity_sha256"] = (
                "sha256:"
                + MODULE.sha256_bytes(
                    MODULE.canonical_json_bytes(bridge_authority)
                )
            )
            runtime_fence = {
                "database_system_identifier": str(
                    raw_before["system_identifier"]
                ),
                "containers": [
                    {
                        "service": "backend",
                        "id": "1" * 64,
                        "image": "sha256:" + "2" * 64,
                    },
                    {
                        "service": "nginx",
                        "id": "3" * 64,
                        "image": "sha256:" + "4" * 64,
                    },
                    {
                        "service": "lab-postgres",
                        "id": "5" * 64,
                        "image": "sha256:" + "6" * 64,
                        "data_volume": {
                            "name": "ephemeral-alias-pg16-volume"
                        },
                    },
                ],
                "monomer_md_unit": {
                    "FragmentSHA256": "7" * 64,
                },
            }
            takeover_runtime = {
                "readers_stopped": True,
                "postgres_running_untouched": True,
                "backend_container_id": "1" * 64,
                "backend_image_id": "sha256:" + "2" * 64,
                "web_container_id": "3" * 64,
                "web_image_id": "sha256:" + "4" * 64,
                "postgres_container_id": "5" * 64,
                "postgres_image_id": "sha256:" + "6" * 64,
                "postgres_data_volume": "ephemeral-alias-pg16-volume",
                "postgres_system_identifier": str(
                    raw_before["system_identifier"]
                ),
                "worker_unit_name": (
                    "nexpoly-monomer-md-worker.service"
                ),
                "worker_unit_sha256": "7" * 64,
            }
            external_transition = (
                PULL_FIXTURES.CONTROLLER
                .build_external_database_alias_pair(
                    external_database_audit,
                    post_alias_external_database_audit,
                    operation_id=self.operation_id,
                    descriptor_sha256=descriptor_sha256,
                )
            )
            self.assertEqual(
                PULL_FIXTURES.CONTROLLER
                .validate_external_database_alias_pair(
                    external_transition,
                    before_binding=external_database_audit,
                ),
                external_transition,
            )

            # This test owns the disposable PostgreSQL transaction, not a
            # complete production bootstrap.  Supply its exact, immutable
            # bridge prerequisite through the same loader seam used by the
            # controller; bridge-v3 preparation itself has separate tests.
            bridge_controller = mock.Mock()

            def prepared_authority(**kwargs):  # type: ignore[no-untyped-def]
                self.assertEqual(kwargs["control"], identities[0])
                self.assertEqual(kwargs["legacy_source"], identities[1])
                if kwargs["external_database_transition"] is not None:
                    self.assertEqual(
                        kwargs["external_database_transition"],
                        external_transition,
                    )
                return bridge_authority, takeover_runtime

            def capture_transition(**kwargs):  # type: ignore[no-untyped-def]
                self.assertEqual(
                    kwargs["bridge_authority"],
                    bridge_authority,
                )
                self.assertEqual(
                    kwargs["alias_operation_id"],
                    self.operation_id,
                )
                live_post = MODULE.validate_inventory(
                    MODULE._psql_json(
                        runner,
                        parsed_environment,
                        MODULE.INVENTORY_SQL,
                    ),
                    expected_phase="post",
                )
                self.assertEqual(
                    [row["version"] for row in live_post["ledger"]],
                    [pair[0] for pair in MODULE.POST_LEDGER],
                )
                return external_transition

            bridge_controller.prepared_alias_bridge_authority.side_effect = (
                prepared_authority
            )
            bridge_controller.capture_alias_external_database_transition.side_effect = (
                capture_transition
            )
            bridge_module = mock.Mock()
            bridge_module.PullDeployController.return_value = (
                bridge_controller
            )
            bridge_module.load_private_json.side_effect = (
                MODULE.load_private_json
            )

            def validate_descriptor(document):  # type: ignore[no-untyped-def]
                self.assertEqual(document, descriptor_document)
                return document

            def validate_transition(  # type: ignore[no-untyped-def]
                document,
                *,
                before_binding,
            ):
                return (
                    PULL_FIXTURES.CONTROLLER
                    .validate_external_database_alias_pair(
                        document,
                        before_binding=before_binding,
                    )
                )

            bridge_module.validate_descriptor.side_effect = (
                validate_descriptor
            )
            bridge_module.validate_external_database_alias_pair.side_effect = (
                validate_transition
            )
            operation = MODULE.Reconciliation(
                operation_id=self.operation_id,
                environment=environment,
                runner=runner,
                session_factory=MODULE.PsqlSession,
                bridge_controller_loader=lambda: bridge_module,
            )
            operation.identities = mock.Mock(return_value=identities)
            operation._runtime_stop_fence = mock.Mock(return_value=runtime_fence)

            restore_image = operation._image_identity()
            self.assertEqual(restore_image["digest_ref"], PINNED_IMAGE)
            self.assertRegex(restore_image["image_id"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(public_before["archive"]["row_count"], 10)
            self.assertEqual(
                public_before["archive"]["status_counts"],
                {"completed": 8, "failed": 2},
            )

            first = operation.apply()
            self.assertEqual(first["status"], "completed")
            self.assertNotIn(
                MODULE.ALIAS_VERSION,
                [row["version"] for row in first["ledger"]],
            )
            self.assertEqual(first["archive"], public_before["archive"])
            self.assertIs(operation.session_factory, MODULE.PsqlSession)

            marker = MODULE.load_private_json(operation.marker_path)
            self.assertEqual(marker["phase"], "completed")
            self.assertEqual(marker["runtime_stop_fence"], runtime_fence)
            self.assertEqual(marker["identity"]["restore_image"], restore_image)
            self.assertEqual(
                marker["isolated_restore"]["image"],
                marker["identity"]["restore_image"],
            )
            self.assertEqual(
                marker["mutation_intent"]["alias"],
                {
                    "version": MODULE.ALIAS_VERSION,
                    "checksum": MODULE.ALIAS_CHECKSUM,
                    "applied_at": MODULE.ALIAS_APPLIED_AT,
                },
            )
            self.assertEqual(
                marker["database_backup"]["dump_sha256"],
                marker["isolated_restore"]["dump_sha256"],
            )
            self.assertTrue(operation.dump_path.is_file())
            self.assertTrue(operation.restore_list_path.is_file())
            self.assertIn(
                "TABLE DATA generation polytao_jobs",
                operation.restore_list_path.read_text(encoding="utf-8"),
            )
            audit = MODULE.load_private_json(
                operation.audit_dir / "AUDIT-MANIFEST.json"
            )
            self.assertEqual(audit["outcome"], "completed")
            self.assertEqual(
                set(audit["files"]),
                {
                    "audit/pg-restore.list",
                    "audit/isolated-postgres16-restore.json",
                    "audit/database-after.json",
                    "audit/external-database-alias-transition.json",
                    "backup/nexpoly-before.dump",
                    "backup/nexpoly-before.dump.sha256",
                },
            )
            self.assertIsNone(
                operation._inspect_container(operation._container_name())
            )

            replay = operation.apply()
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["archive"], first["archive"])
            self.assertEqual(
                MODULE.load_private_json(operation.marker_path), marker
            )

            with mock.patch.object(
                SELECTOR,
                "load_control_release",
                return_value=(control_manifest, control_root),
            ) as load_control:
                gate = SELECTOR.load_production_0005_alias_gate(
                    self.runtime, require_completed=True
                )
            self.assertEqual(gate, marker)
            load_control.assert_called_once_with(
                self.runtime, control_manifest["release_id"]
            )

            deploy_marker = self.runtime / MODULE.DEPLOY_MARKER_RELATIVE
            MODULE.atomic_json(deploy_marker, {"integration_test": True})
            try:
                with self.assertRaisesRegex(
                    MODULE.ReconcileError,
                    "another deployment or contract marker",
                ):
                    operation.apply()
            finally:
                deploy_marker.unlink()
                MODULE.fsync_directory(deploy_marker.parent)

            raw_after = MODULE._psql_json(
                runner, parsed_environment, MODULE.INVENTORY_SQL
            )
            public_after = MODULE.validate_inventory(
                raw_after, expected_phase="post"
            )
            self.assertEqual(public_after["archive"], public_before["archive"])
            self.assertEqual(
                [pair[0] for pair in MODULE.POST_LEDGER],
                [row["version"] for row in public_after["ledger"]],
            )
            self.assertEqual(
                bridge_controller.capture_alias_external_database_transition.call_count,
                1,
            )


if __name__ == "__main__":
    unittest.main()
