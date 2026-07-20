from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import TypeAdapter
import pytest
import psycopg

from app.postgres_database import postgres_connection
from app.services.monomer_dft_models import MonomerDftRunRequest
from app.services.monomer_dft_protocol import prepare_monomer_dft_request
from app.services.monomer_dft_repository import (
    MonomerDftArtifactNotFound,
    MonomerDftCapacityError,
    MonomerDftIdempotencyConflict,
    MonomerDftRepository,
    MonomerDftStaleAttempt,
)
from app.services.monomer_dft_schema import (
    MONOMER_DFT_CATALOG_FINGERPRINT_SHA256,
    MONOMER_DFT_MIGRATION_VERSION,
    MonomerDftSchemaState,
    monomer_dft_catalog_document,
    monomer_dft_catalog_sha256,
    probe_monomer_dft_schema,
)


REQUEST_ADAPTER = TypeAdapter(MonomerDftRunRequest)


def _prepared(smiles: str = "CCO"):
    return prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(
            {
                "input": {
                    "smiles": smiles,
                    "net_charge": None,
                    "multiplicity": 1,
                    "psmiles_mode": None,
                },
                "calculation_type": "single_point",
                "model": "aimnet2",
                "conformer": {"seed": 1, "max_iterations": 500},
                "single_point": {"properties": ["energy", "charges", "forces"]},
            }
        )
    )


def test_postgres_repository_idempotency_capacity_fencing_artifacts_and_timings(postgres_dsn: str) -> None:
    repository = MonomerDftRepository(postgres_dsn)
    prepared = _prepared()

    first = repository.create_job(prepared, idempotency_key="dft-test-0001", max_active_jobs=1)
    assert first.created is True
    assert first.job["status"] == "pending"
    assert first.job["job_id"].count("-") == 4

    replay = repository.create_job(prepared, idempotency_key="dft-test-0001", max_active_jobs=1)
    assert replay.created is False
    assert replay.job["job_id"] == first.job["job_id"]
    assert repository.find_idempotent_job(
        idempotency_key="dft-test-0001",
        request_sha256=prepared.request_sha256,
    )["job_id"] == first.job["job_id"]

    with pytest.raises(MonomerDftIdempotencyConflict):
        repository.create_job(_prepared("CC"), idempotency_key="dft-test-0001", max_active_jobs=1)
    with pytest.raises(MonomerDftCapacityError):
        repository.create_job(_prepared("CCC"), idempotency_key="dft-test-0002", max_active_jobs=1)
    with pytest.raises(MonomerDftStaleAttempt):
        repository.apply_worker_snapshot(
            job_id=first.job["job_id"],
            attempt_token="f" * 64,
            snapshot={"status": "queued"},
        )
    with pytest.raises(MonomerDftStaleAttempt):
        repository.apply_worker_snapshot(
            job_id=first.job["job_id"],
            attempt_token=first.job["_attempt_token"],
            snapshot={
                "job_id": first.job["job_id"],
                "attempt_token": first.job["_attempt_token"],
                "status": "queued",
            },
        )

    completed = repository.apply_worker_snapshot(
        job_id=first.job["job_id"],
        attempt_token=first.job["_attempt_token"],
        snapshot={
            "schema_version": 2,
            "job_id": first.job["job_id"],
            "attempt_token": first.job["_attempt_token"],
            "request_sha256": prepared.request_sha256,
            "enqueue_sequence": first.job["_enqueue_sequence"],
            "worker_instance_id": "worker-one",
            "status": "completed",
            "queue_position": None,
            "stage": "artifacts",
            "progress_percent": 100,
            "result": {
                "schema_version": 1,
                "input": {"canonical_smiles": "CCO", "net_charge": 0},
                "properties": {"energy": {"value_eV": -4221.547}},
                "scientific_status": {
                    "minimum_assessment": "not_evaluated",
                    "warnings": ["scientific warning"],
                },
                "warnings": [{"code": "charge", "message": "result warning"}],
                "provenance": {"aimnet_commit": "9a6c564"},
            },
            "error": None,
            "timings": {
                "queue_wait_ms": 1.0,
                "gpu_wait_ms": 3.0,
                "model_load_ms": 4.0,
                "structure_prepare_ms": 2.0,
                "model_compute_ms": 12.5,
                "optimization_ms": 0.0,
                "hessian_ms": 0.0,
                "frequency_ms": 0.0,
                "artifact_ms": 1.0,
                "total_ms": 23.5,
            },
            "artifacts": [
                {
                    "artifact_id": "result_json",
                    "name": "result.json",
                    "media_type": "application/json",
                    "size_bytes": 42,
                    "sha256": "a" * 64,
                    "relative_location": "../../untrusted-worker-path",
                }
            ],
        },
    )
    assert completed["status"] == "completed"
    assert completed["scientific_status"] == "not_evaluated"
    assert completed["warnings"] == ["scientific warning", "result warning"]
    assert completed["provenance"]["aimnet_commit"] == "9a6c564"
    assert set(completed["timings"]) == {
        "queue_wait_ms",
        "gpu_wait_ms",
        "model_load_ms",
        "structure_prepare_ms",
        "model_compute_ms",
        "optimization_ms",
        "hessian_ms",
        "frequency_ms",
        "artifact_ms",
        "total_ms",
        "end_to_end_ms",
    }
    assert completed["timings"]["model_compute_ms"] == 12.5
    assert completed["timings"]["gpu_wait_ms"] == 3.0
    assert completed["timings"]["model_load_ms"] == 4.0
    assert completed["timings"]["queue_wait_ms"] == 1.0
    assert completed["timings"]["total_ms"] == 23.5
    assert completed["timings"]["end_to_end_ms"] >= 0.0
    public_artifact = repository.get_artifact(
        job_id=first.job["job_id"],
        artifact_id="result_json",
    )
    assert public_artifact["sha256"] == "a" * 64
    assert "relative_location" not in public_artifact
    assert all("relative_location" not in artifact for artifact in completed["artifacts"])

    with postgres_connection(postgres_dsn) as connection:
        stored_artifact = connection.execute(
            """
            SELECT name, relative_location
            FROM monomer_dft.artifacts
            WHERE job_id = %s::uuid AND artifact_id = 'result_json'
            """,
            (first.job["job_id"],),
        ).fetchone()
        assert stored_artifact["name"] == "result.json"
        assert stored_artifact["relative_location"] == "artifacts/result.json"

    with postgres_connection(postgres_dsn) as connection:
        attempt = connection.execute(
            "SELECT heartbeat_at, lease_expires_at, worker_instance_id FROM monomer_dft.job_attempts WHERE job_id = %s::uuid",
            (first.job["job_id"],),
        ).fetchone()
        assert attempt["heartbeat_at"] is not None
        assert attempt["lease_expires_at"] is not None
        assert attempt["worker_instance_id"] == "worker-one"

    page = repository.list_jobs(page=1, page_size=10, status="completed", calculation_type="single_point")
    assert page.total == 1
    assert page.items[0]["job_id"] == first.job["job_id"]
    empty_page = repository.list_jobs(
        page=10_000,
        page_size=100,
        status="completed",
        calculation_type="single_point",
    )
    assert empty_page.total == 1
    assert empty_page.items == []

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE monomer_dft.jobs
            SET finished_at = now() - interval '31 days', updated_at = now() - interval '31 days'
            WHERE job_id = %s::uuid
            """,
            (first.job["job_id"],),
        )
    expired = repository.list_expired_artifact_jobs(retention_days=30)
    assert [job["job_id"] for job in expired] == [first.job["job_id"]]

    requested = repository.request_artifact_deletion(first.job["job_id"])
    assert requested["artifacts_state"] == "delete_requested"
    assert requested["artifacts_deleted"] is False
    assert [item["available"] for item in requested["artifacts"]] == [False]
    assert [item["job_id"] for item in repository.list_pending_artifact_deletions()] == [
        first.job["job_id"]
    ]
    with pytest.raises(MonomerDftArtifactNotFound):
        repository.get_artifact(job_id=first.job["job_id"], artifact_id="result_json")
    with postgres_connection(postgres_dsn) as connection:
        intent_row = connection.execute(
            """
            SELECT available, deleted_at
            FROM monomer_dft.artifacts
            WHERE job_id = %s::uuid AND artifact_id = 'result_json'
            """,
            (first.job["job_id"],),
        ).fetchone()
        assert intent_row["available"] is False
        assert intent_row["deleted_at"] is None
    repository.mark_artifacts_deleted(first.job["job_id"])
    with pytest.raises(MonomerDftArtifactNotFound):
        repository.get_artifact(job_id=first.job["job_id"], artifact_id="result_json")
    deleted = repository.mark_artifacts_deleted(first.job["job_id"])
    assert deleted["artifacts_state"] == "deleted"
    assert deleted["artifacts_deleted"] is True
    with postgres_connection(postgres_dsn) as connection:
        assert connection.execute(
            """
            SELECT deleted_at IS NOT NULL AS deleted
            FROM monomer_dft.artifacts
            WHERE job_id = %s::uuid AND artifact_id = 'result_json'
            """,
            (first.job["job_id"],),
        ).fetchone()["deleted"] is True

    second = repository.create_job(_prepared("CCC"), idempotency_key="dft-test-0002", max_active_jobs=1)
    repository.record_dispatch_error(
        job_id=second.job["job_id"],
        attempt_token=second.job["_attempt_token"],
        code="worker_contract_mismatch",
        message="worker rejected request at /private/location",
        retryable=False,
        details={"reason": "unsupported", "runtime_path": "/private/location"},
    )
    failed = repository.get_job(second.job["job_id"])
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"]["retryable"] is False
    assert "/private" not in failed["error"]["message"]
    assert failed["error"]["details"] == {"reason": "unsupported"}
    assert failed["stage"] == "queued"
    assert failed["artifacts_state"] == "none"
    assert failed["artifacts_deleted"] is False

    with repository.reconciliation_leader() as acquired:
        assert acquired is True


def test_postgres_repository_advisory_capacity_is_one_running_plus_eight_queued(postgres_dsn: str) -> None:
    repository = MonomerDftRepository(postgres_dsn)
    prepared = _prepared()

    def submit(index: int) -> str:
        try:
            created = repository.create_job(
                prepared,
                idempotency_key=f"capacity-{index:04d}",
                max_active_jobs=9,
            )
        except MonomerDftCapacityError:
            return "capacity"
        return "created" if created.created else "replay"

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(submit, range(12)))
    assert outcomes.count("created") == 9
    assert outcomes.count("capacity") == 3
    assert repository.count_active_jobs() == 9

    replay = repository.create_job(
        prepared,
        idempotency_key="capacity-0000",
        max_active_jobs=9,
    )
    assert replay.created is False


def test_postgres_reconcilable_jobs_follow_durable_enqueue_sequence(postgres_dsn: str) -> None:
    repository = MonomerDftRepository(postgres_dsn)
    prepared = _prepared()
    accepted = [
        repository.create_job(
            prepared,
            idempotency_key=f"fifo-order-{index:04d}",
            max_active_jobs=9,
        ).job["job_id"]
        for index in range(5)
    ]
    assert [job["job_id"] for job in repository.list_reconcilable_jobs()] == accepted


def test_postgres_cancel_is_local_only_before_durable_dispatch_claim(postgres_dsn: str) -> None:
    repository = MonomerDftRepository(postgres_dsn)
    prepared = _prepared()

    never_dispatched = repository.create_job(
        prepared,
        idempotency_key="cancel-before-dispatch",
        max_active_jobs=9,
    ).job
    cancelled = repository.request_cancel(never_dispatched["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert cancelled["_dispatch_started"] is False

    possibly_dispatched = repository.create_job(
        prepared,
        idempotency_key="cancel-after-dispatch",
        max_active_jobs=9,
    ).job
    assert repository.claim_pending_dispatch(
        job_id=possibly_dispatched["job_id"],
        attempt_token=possibly_dispatched["_attempt_token"],
    ) is True
    claimed = repository.get_job(possibly_dispatched["job_id"])
    assert claimed is not None
    assert claimed["status"] == "pending"
    assert claimed["_dispatch_started"] is True

    cancel_requested = repository.request_cancel(possibly_dispatched["job_id"])
    assert cancel_requested["status"] == "cancel_requested"
    assert cancel_requested["_dispatch_started"] is True
    assert repository.claim_pending_dispatch(
        job_id=possibly_dispatched["job_id"],
        attempt_token=possibly_dispatched["_attempt_token"],
    ) is False

    with pytest.raises(MonomerDftStaleAttempt):
        repository.claim_pending_dispatch(
            job_id=possibly_dispatched["job_id"],
            attempt_token="f" * 64,
        )


def test_postgres_exact_0013_catalog_fingerprint_is_ready(
    postgres_dsn: str,
) -> None:
    with postgres_connection(postgres_dsn) as connection:
        document = monomer_dft_catalog_document(connection)
        digest = monomer_dft_catalog_sha256(connection)
        probe = probe_monomer_dft_schema(connection)

    assert digest == MONOMER_DFT_CATALOG_FINGERPRINT_SHA256
    assert probe.state is MonomerDftSchemaState.READY
    assert probe.catalog_sha256 == digest
    assert document["namespace"] == [
        {
            "schema_name": "monomer_dft",
            "owner_is_current_role": True,
            "access_control": "",
        }
    ]
    assert document["security_labels"] == []
    assert all(
        relation["owner_matches_schema"]
        and relation["access_control"] == ""
        and relation["relation_options"] == ""
        and relation["tablespace"] == ""
        for relation in document["relations"]
    )
    assert all(column["access_control"] == "" for column in document["columns"])


def test_catalog_probe_failure_restores_search_path_and_preserves_error(
    postgres_dsn: str,
) -> None:
    class FailingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def transaction(self):
            return self.connection.transaction()

        def execute(self, query, parameters=None):
            if "FROM pg_catalog.pg_constraint AS con" in str(query):
                return self.connection.execute(
                    "SELECT * FROM monomer_dft.__catalog_probe_failure__"
                )
            if parameters is None:
                return self.connection.execute(query)
            return self.connection.execute(query, parameters)

    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            "SELECT set_config('search_path', 'public, pg_catalog', true)"
        ).fetchone()

        with pytest.raises(psycopg.errors.UndefinedTable) as exc_info:
            monomer_dft_catalog_document(FailingConnection(connection))

        assert "__catalog_probe_failure__" in str(exc_info.value)
        search_path = connection.execute(
            "SELECT current_setting('search_path') AS search_path"
        ).fetchone()["search_path"]
        assert search_path == "public, pg_catalog"
        assert connection.execute("SELECT 1 AS value").fetchone()["value"] == 1


@pytest.mark.parametrize(
    "statements",
    [
        pytest.param(
            ("ALTER TABLE monomer_dft.jobs ADD COLUMN unexpected text",),
            id="extra-column",
        ),
        pytest.param(
            ("CREATE TABLE monomer_dft.unexpected (id integer)",),
            id="extra-relation",
        ),
        pytest.param(
            (
                "CREATE INDEX unexpected_monomer_dft_index "
                "ON monomer_dft.jobs (updated_at)",
            ),
            id="extra-index",
        ),
        pytest.param(
            (
                "DROP INDEX monomer_dft.idx_monomer_dft_jobs_active",
                "CREATE INDEX idx_monomer_dft_jobs_active "
                "ON monomer_dft.jobs (enqueue_sequence) "
                "WHERE status = 'running'",
            ),
            id="wrong-index-predicate",
        ),
        pytest.param(
            (
                "ALTER TABLE monomer_dft.jobs "
                "DROP CONSTRAINT jobs_status_check",
                "ALTER TABLE monomer_dft.jobs "
                "ADD CONSTRAINT jobs_status_check CHECK (status <> '')",
            ),
            id="wrong-check-constraint",
        ),
        pytest.param(
            (
                "ALTER TABLE monomer_dft.jobs "
                "ALTER COLUMN request_warnings DROP DEFAULT",
            ),
            id="missing-default",
        ),
        pytest.param(
            (
                "ALTER TABLE monomer_dft.jobs "
                "ALTER COLUMN enqueue_sequence DROP IDENTITY",
            ),
            id="missing-identity-and-sequence",
        ),
        pytest.param(
            (
                "ALTER SEQUENCE monomer_dft.jobs_enqueue_sequence_seq "
                "INCREMENT BY 2",
            ),
            id="wrong-sequence-settings",
        ),
        pytest.param(
            ("GRANT USAGE ON SCHEMA monomer_dft TO PUBLIC",),
            id="schema-acl",
        ),
        pytest.param(
            ("GRANT SELECT ON monomer_dft.jobs TO PUBLIC",),
            id="relation-acl",
        ),
        pytest.param(
            ("GRANT SELECT (input_smiles) ON monomer_dft.jobs TO PUBLIC",),
            id="column-acl",
        ),
        pytest.param(
            ("ALTER TABLE monomer_dft.jobs SET (fillfactor = 80)",),
            id="relation-options",
        ),
        pytest.param(
            (
                "CREATE ROLE monomer_dft_fingerprint_other NOLOGIN",
                "ALTER SCHEMA monomer_dft "
                "OWNER TO monomer_dft_fingerprint_other",
            ),
            id="wrong-owner",
        ),
        pytest.param(
            (
                "CREATE FUNCTION monomer_dft.unexpected() "
                "RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 1'",
            ),
            id="extra-routine",
        ),
        pytest.param(
            (
                "ALTER TABLE monomer_dft.jobs ENABLE ROW LEVEL SECURITY",
                "CREATE POLICY unexpected_policy ON monomer_dft.jobs "
                "USING (true)",
            ),
            id="row-security-policy",
        ),
    ],
)
def test_postgres_0013_catalog_tamper_matrix_fails_closed(
    postgres_dsn: str,
    statements: tuple[str, ...],
) -> None:
    class RollbackMutation(Exception):
        pass

    with postgres_connection(postgres_dsn) as connection:
        try:
            with connection.transaction():
                for statement in statements:
                    connection.execute(statement)
                digest = monomer_dft_catalog_sha256(connection)
                probe = probe_monomer_dft_schema(connection)
                assert digest != MONOMER_DFT_CATALOG_FINGERPRINT_SHA256
                assert probe.state is MonomerDftSchemaState.INVALID
                assert probe.reason == "catalog_fingerprint_mismatch"
                assert probe.catalog_sha256 == digest
                raise RollbackMutation
        except RollbackMutation:
            pass

        assert probe_monomer_dft_schema(connection).state is MonomerDftSchemaState.READY


def test_postgres_0013_probe_distinguishes_absent_and_partial_states(
    postgres_dsn: str,
) -> None:
    class RollbackMutation(Exception):
        pass

    with postgres_connection(postgres_dsn) as connection:
        try:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM governance.schema_migrations WHERE version = %s",
                    (MONOMER_DFT_MIGRATION_VERSION,),
                )
                partial = probe_monomer_dft_schema(connection)
                assert partial.state is MonomerDftSchemaState.INVALID
                assert partial.reason == "unmanaged_or_partial_schema"
                raise RollbackMutation
        except RollbackMutation:
            pass

        try:
            with connection.transaction():
                connection.execute("DROP SCHEMA monomer_dft CASCADE")
                connection.execute(
                    "DELETE FROM governance.schema_migrations WHERE version = %s",
                    (MONOMER_DFT_MIGRATION_VERSION,),
                )
                absent = probe_monomer_dft_schema(connection)
                assert absent.state is MonomerDftSchemaState.ABSENT
                assert absent.reason == "migration_not_applied"
                raise RollbackMutation
        except RollbackMutation:
            pass

        try:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE governance.schema_migrations
                    SET checksum = %s
                    WHERE version = %s
                    """,
                    ("f" * 64, MONOMER_DFT_MIGRATION_VERSION),
                )
                wrong_checksum = probe_monomer_dft_schema(connection)
                assert wrong_checksum.state is MonomerDftSchemaState.INVALID
                assert wrong_checksum.reason == "migration_checksum_mismatch"
                raise RollbackMutation
        except RollbackMutation:
            pass

        assert probe_monomer_dft_schema(connection).state is MonomerDftSchemaState.READY


def test_postgres_migration_constraints_and_attempt_state_non_regression(postgres_dsn: str) -> None:
    repository = MonomerDftRepository(postgres_dsn)
    with postgres_connection(postgres_dsn) as connection:
        tables = connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'monomer_dft'
            ORDER BY table_name
            """
        ).fetchall()
        assert [row["table_name"] for row in tables] == ["artifacts", "job_attempts", "jobs"]
        constraints = connection.execute(
            """
            SELECT count(*) AS count
            FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = 'monomer_dft' AND c.contype IN ('c', 'f', 'p', 'u')
            """
        ).fetchone()
        assert constraints["count"] >= 12
        relative_location_column = connection.execute(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'monomer_dft'
              AND table_name = 'artifacts'
              AND column_name = 'relative_location'
            """
        ).fetchone()
        assert relative_location_column["is_nullable"] == "NO"
        requested_at_column = connection.execute(
            """
            SELECT is_nullable, data_type
            FROM information_schema.columns
            WHERE table_schema = 'monomer_dft'
              AND table_name = 'jobs'
              AND column_name = 'artifacts_delete_requested_at'
            """
        ).fetchone()
        assert requested_at_column == {
            "is_nullable": "YES",
            "data_type": "timestamp with time zone",
        }
        indexes = connection.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'monomer_dft'
            """
        ).fetchall()
        index_names = {row["indexname"] for row in indexes}
        assert "idx_monomer_dft_jobs_pending_artifact_deletion" in index_names
        assert "uq_monomer_dft_artifact_name_ci" in index_names

    created = repository.create_job(
        _prepared(),
        idempotency_key="state-test-0001",
        max_active_jobs=9,
    ).job
    invalid_locations = (
        "../escape.json",
        "/absolute/escape.json",
        r"artifacts\escape.json",
        "artifacts/../escape.json",
        "artifacts/other.json",
    )
    with postgres_connection(postgres_dsn) as connection:
        for index, invalid_location in enumerate(invalid_locations):
            with pytest.raises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO monomer_dft.artifacts (
                          job_id, artifact_id, name, relative_location,
                          media_type, size_bytes, sha256
                        ) VALUES (%s::uuid, %s, 'safe.json', %s,
                                  'application/json', 1, %s)
                        """,
                        (created["job_id"], f"invalid_location_{index}", invalid_location, "a" * 64),
                        )
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO monomer_dft.artifacts (
                      job_id, artifact_id, name, relative_location,
                      media_type, size_bytes, sha256
                    ) VALUES (%s::uuid, 'oversize', 'oversize.bin',
                              'artifacts/oversize.bin', 'application/octet-stream',
                              67108865, %s)
                    """,
                    (created["job_id"], "a" * 64),
                )
        connection.execute(
            """
            INSERT INTO monomer_dft.artifacts (
              job_id, artifact_id, name, relative_location,
              media_type, size_bytes, sha256
            ) VALUES (%s::uuid, 'case_first', 'Result.JSON',
                      'artifacts/Result.JSON', 'application/json', 1, %s)
            """,
            (created["job_id"], "a" * 64),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO monomer_dft.artifacts (
                      job_id, artifact_id, name, relative_location,
                      media_type, size_bytes, sha256
                    ) VALUES (%s::uuid, 'case_second', 'result.json',
                              'artifacts/result.json', 'application/json', 1, %s)
                    """,
                    (created["job_id"], "b" * 64),
                )
        for index, (invalid_name, invalid_location) in enumerate(
            (("../escape", "artifacts/../escape"), (r"dir\escape", r"artifacts/dir\escape"))
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO monomer_dft.artifacts (
                          job_id, artifact_id, name, relative_location,
                          media_type, size_bytes, sha256
                        ) VALUES (%s::uuid, %s, %s, %s,
                                  'application/json', 1, %s)
                        """,
                        (
                            created["job_id"],
                            f"invalid_name_{index}",
                            invalid_name,
                            invalid_location,
                            "a" * 64,
                        ),
                    )
    base = {
        "schema_version": 2,
        "job_id": created["job_id"],
        "attempt_token": created["_attempt_token"],
        "request_sha256": created["request_sha256"],
        "enqueue_sequence": created["_enqueue_sequence"],
        "stage": "validating",
        "progress_percent": 10,
        "timings": {},
        "artifacts": [],
    }
    queued = repository.apply_worker_snapshot(
        job_id=created["job_id"],
        attempt_token=created["_attempt_token"],
        snapshot={**base, "status": "queued", "queue_position": 1},
    )
    assert queued["status"] == "queued"
    assert repository.apply_worker_snapshot(
        job_id=created["job_id"],
        attempt_token=created["_attempt_token"],
        snapshot={**base, "status": "pending"},
    )["status"] == "queued"
    assert repository.apply_worker_snapshot(
        job_id=created["job_id"],
        attempt_token=created["_attempt_token"],
        snapshot={**base, "status": "running", "progress_percent": 50},
    )["status"] == "running"
    assert repository.apply_worker_snapshot(
        job_id=created["job_id"],
        attempt_token=created["_attempt_token"],
        snapshot={**base, "status": "queued", "queue_position": 1},
    )["status"] == "running"
    assert repository.request_cancel(created["job_id"])["status"] == "cancel_requested"
    assert repository.apply_worker_snapshot(
        job_id=created["job_id"],
        attempt_token=created["_attempt_token"],
        snapshot={**base, "status": "running", "progress_percent": 75},
    )["status"] == "cancel_requested"

    with postgres_connection(postgres_dsn) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute(
                    "UPDATE monomer_dft.jobs SET queue_position = 0 WHERE job_id = %s::uuid",
                    (created["job_id"],),
                )
