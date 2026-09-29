"""Isolation contract against PG16 using actual non-owner login roles."""
from __future__ import annotations

import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest
from pydantic import TypeAdapter

from app.auth.context import Identity, current_owner_id, is_service_context, user_context, service_context
from app.postgres_database import postgres_connection
from app.routers.monomer_md import _create_pending_job_with_capacity_guard
from app.services.monomer_dft_models import MonomerDftRunRequest
from app.services.monomer_dft_protocol import prepare_monomer_dft_request
from app.services.monomer_dft_repository import MonomerDftRepository, MonomerDftCapacityError, MonomerDftJobNotFound, MonomerDftArtifactNotFound
from app.services.monomer_dft_schema import probe_monomer_dft_schema
from app.services.monomer_dft_reconciler import MonomerDftReconciler
from app.services.monomer_md_repository import create_monomer_md_job_postgres, get_monomer_md_job_postgres, list_monomer_md_jobs_postgres, get_user_monomer_md_capacity_postgres

A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture(scope="module")
def isolation_database():
    base = os.environ.get("ISOLATION_TEST_ADMIN_DSN")
    if not base:
        pytest.skip("ISOLATION_TEST_ADMIN_DSN is required for PG16 isolation acceptance")
    suffix = uuid4().hex[:12]
    database = "dft_isolation_" + suffix
    api = "api_" + suffix
    service = "service_" + suffix
    audit = "audit_" + suffix
    admin_dsn = make_conninfo(base, dbname=database)
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        with psycopg.connect(admin_dsn, row_factory=dict_row) as connection:
            for path in sorted((Path(__file__).parents[1] / "migrations/postgres").glob("*.sql")):
                if path.name.startswith("0018"):
                    for owner, username in ((A, "owner-a"), (B, "owner-b")):
                        connection.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES (%s,%s,'!',false)", (owner, username))
                    connection.execute("SELECT set_config('nexpoly.legacy_owner',%s,true)", (A,))
                connection.execute(path.read_text())
                connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES (%s,%s)", (path.stem, hashlib.sha256(path.read_bytes()).hexdigest()))
            for name, group in ((api, "nexpoly_api"), (service, "nexpoly_service"), (audit, "nexpoly_mutable_audit")):
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'isolation-test-only' IN ROLE {}").format(sql.Identifier(name), sql.Identifier(group)))
        yield SimpleNamespace(admin=admin_dsn, api=make_conninfo(admin_dsn, user=api, password="isolation-test-only"), service=make_conninfo(admin_dsn, user=service, password="isolation-test-only"), audit=make_conninfo(admin_dsn, user=audit, password="isolation-test-only"))
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            for name in (api, service, audit):
                connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def isolated(isolation_database, monkeypatch):
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", isolation_database.service)
    with psycopg.connect(isolation_database.admin) as connection:
        connection.execute("TRUNCATE monomer_dft.jobs,md.monomer_md_jobs CASCADE")
        connection.execute("UPDATE auth.users SET status='active' WHERE user_id IN (%s,%s)", (A, B))
    return isolation_database


def prepared():
    request = TypeAdapter(MonomerDftRunRequest).validate_python({
        "input": {"smiles": "CCO", "net_charge": None, "multiplicity": 1, "psmiles_mode": None},
        "calculation_type": "single_point", "model": "aimnet2",
        "conformer": {"seed": 1, "max_iterations": 500},
        "single_point": {"properties": ["energy", "charges", "forces"]},
    })
    return prepare_monomer_dft_request(request)


@pytest.mark.parametrize("cancel_at", [None, "acquire", "network"])
def test_async_reconciler_closes_service_context_and_pg_leader_on_cancel(isolated, cancel_at):
    entered = Event()
    release_acquire = Event()
    real_repository = MonomerDftRepository(isolated.api)

    class Repository(MonomerDftRepository):
        @contextmanager
        def reconciliation_leader(self):
            with super().reconciliation_leader() as acquired:
                assert acquired
                assert is_service_context()
                assert current_owner_id() == A
                entered.set()
                if cancel_at == "acquire":
                    assert release_acquire.wait(timeout=5)
                try:
                    yield acquired
                finally:
                    assert is_service_context()
                    assert current_owner_id() == A

    async def scenario():
        reconciler = MonomerDftReconciler(repository=Repository(isolated.api), worker=object(), interval_seconds=1)
        network_started = asyncio.Event()

        async def network_wait():
            assert not is_service_context()
            network_started.set()
            await asyncio.Event().wait()

        if cancel_at == "network":
            reconciler._reconcile_artifact_deletions = network_wait
        with user_context(Identity(A)):
            task = asyncio.create_task(reconciler.run_once())
            try:
                if cancel_at == "acquire":
                    assert await asyncio.to_thread(entered.wait, 5)
                    task.cancel()
                    await asyncio.sleep(0.01)
                    assert not task.done(), "lock acquisition must finish before cancellation returns"
                    # A second cancellation still must not abandon the lock.
                    task.cancel()
                    await asyncio.sleep(0)
                    release_acquire.set()
                elif cancel_at == "network":
                    await asyncio.wait_for(network_started.wait(), 5)
                    task.cancel()
                if cancel_at is None:
                    await asyncio.wait_for(task, 5)
                    # A completed cycle must not poison the next copied Context.
                    await asyncio.wait_for(reconciler.run_once(), 5)
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                assert current_owner_id() == A
                assert not is_service_context()
            finally:
                release_acquire.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        def acquire_again():
            assert not is_service_context()
            with real_repository.reconciliation_leader() as acquired:
                assert acquired, "cancellation leaked the PostgreSQL advisory lock"
            assert not is_service_context()

        await asyncio.to_thread(acquire_again)
        assert not is_service_context()

    asyncio.run(scenario())


def create(repository, owner, key="isolation-key"):
    with user_context(Identity(owner)):
        return repository.create_job(prepared(), owner_user_id=owner, idempotency_key=key, max_active_jobs=9).job


def test_dft_user_idempotency_visibility_and_rls(isolated):
    repository = MonomerDftRepository(isolated.api)
    a = create(repository, A)
    b = create(repository, B)
    assert a["job_id"] != b["job_id"]
    with user_context(Identity(A)):
        assert repository.get_job(a["job_id"], owner_user_id=A)
        assert repository.get_job(b["job_id"], owner_user_id=A) is None
        assert repository.list_jobs(owner_user_id=A, page=1, page_size=20).total == 1
        with pytest.raises(MonomerDftJobNotFound):
            repository.request_cancel(b["job_id"], owner_user_id=A)
        with pytest.raises(MonomerDftArtifactNotFound):
            repository.get_artifact(owner_user_id=A, job_id=b["job_id"], artifact_id="x")
        with postgres_connection(isolated.api) as connection:
            # Missing an owner predicate must still be safe.
            assert [str(row["job_id"]) for row in connection.execute("SELECT job_id FROM monomer_dft.jobs")] == [a["job_id"]]
    with postgres_connection(isolated.api) as connection:
        assert connection.execute("SELECT count(*) AS n FROM monomer_dft.jobs").fetchone()["n"] == 0
    assert repository.schema_ready()


def test_dft_atomic_user_and_global_capacity(isolated):
    repository = MonomerDftRepository(isolated.api)
    def submit(key):
        try:
            return create(repository, A, key)
        except MonomerDftCapacityError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ("concurrent-one", "concurrent-two")))
    assert sum(result is not None for result in results) == 1
    existing = next(result for result in results if result)
    with user_context(Identity(A)):
        replay = repository.create_job(prepared(), owner_user_id=A, idempotency_key=existing["_idempotency_key"], max_active_jobs=1)
        assert not replay.created
    with user_context(Identity(B)), pytest.raises(MonomerDftCapacityError):
        repository.create_job(prepared(), owner_user_id=B, idempotency_key="global-capacity", max_active_jobs=1)


def test_dft_start_is_fenced_and_disabled_authorized_attempt_finishes(isolated):
    repository = MonomerDftRepository(isolated.api)
    job = create(repository, A)
    args = dict(job_id=job["job_id"], attempt_token=job["_attempt_token"], request_sha256=job["request_sha256"], enqueue_sequence=job["_enqueue_sequence"])
    assert not repository.authorize_start(**{**args, "attempt_token": "f" * 64})
    assert repository.authorize_start(**args)
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (A,))
    assert repository.authorize_start(**args)  # Lost response retry preserves authorization.
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("UPDATE monomer_dft.jobs SET status='completed' WHERE job_id=%s", (job["job_id"],))
    assert not repository.authorize_start(**args)


def test_dft_disabled_before_start_retains_quota_until_worker_cleanup(isolated):
    repository = MonomerDftRepository(isolated.api)
    job = create(repository, A)
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (A,))
    assert not repository.authorize_start(job_id=job["job_id"], attempt_token=job["_attempt_token"], request_sha256=job["request_sha256"], enqueue_sequence=job["_enqueue_sequence"])
    assert repository.get_job_for_service(job["job_id"])["status"] == "cancel_requested"
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("UPDATE auth.users SET status='active' WHERE user_id=%s", (A,))
    with user_context(Identity(A)), pytest.raises(MonomerDftCapacityError):
        repository.create_job(prepared(), owner_user_id=A, idempotency_key="cleanup-still-held", max_active_jobs=9)
    repository.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot={
        "schema_version":2, "job_id":job["job_id"], "attempt_token":job["_attempt_token"],
        "request_sha256":job["request_sha256"], "enqueue_sequence":job["_enqueue_sequence"],
        "status":"cancelled", "stage":"queued", "progress_percent":0,
        "error":None, "timings":{}, "artifacts":[],
    })
    with user_context(Identity(A)):
        assert repository.create_job(prepared(), owner_user_id=A, idempotency_key="cleanup-now-released", max_active_jobs=9).created


def test_md_owner_queries_and_atomic_admission(isolated):
    settings = SimpleNamespace(app_postgres_dsn=isolated.api, monomer_md_max_active_jobs=3)
    def submit(owner, job_id):
        with user_context(Identity(owner)):
            _create_pending_job_with_capacity_guard(settings, job_id=job_id, input_smiles="CCO", canonical_smiles="CCO", requested_steps=1000, run_mode="formal", protocol="Density")
    submit(A, "a" * 32)
    submit(B, "b" * 32)
    with user_context(Identity(A)), postgres_connection(isolated.api) as connection:
        assert get_monomer_md_job_postgres(connection, "b" * 32, owner_user_id=A) is None
        items, total = list_monomer_md_jobs_postgres(connection, owner_user_id=A)
        assert total == 1 and items[0]["job_id"] == "a" * 32
        count, _, modes = get_user_monomer_md_capacity_postgres(connection, owner_user_id=A)
        assert count == 1 and modes["formal_running"] == 1
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        submit(A, "c" * 32)
    assert caught.value.status_code == 429


def test_schema_policy_role_drift_fails_closed(isolated):
    repository = MonomerDftRepository(isolated.api)
    assert repository.schema_ready()
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("ALTER POLICY owner_access ON monomer_dft.jobs TO PUBLIC")
    try:
        assert not repository.schema_ready()
    finally:
        with psycopg.connect(isolated.admin) as connection:
            connection.execute("ALTER POLICY owner_access ON monomer_dft.jobs TO nexpoly_api")


def test_md_start_authorization_fences_worker_and_survives_disable(isolated):
    from workers.monomer_md_worker.app.repository import PostgresJobRepository
    with service_context(), postgres_connection(isolated.api) as connection:
        create_monomer_md_job_postgres(connection, owner_user_id=A, job_id="d" * 32, input_smiles="O", canonical_smiles="O", requested_steps=1000)
        connection.execute("UPDATE md.monomer_md_jobs SET status='submitted',worker_instance_id='current-worker' WHERE job_id=%s", ("d" * 32,))
    repository = PostgresJobRepository(SimpleNamespace(db_configured=True, app_postgres_dsn=isolated.service))
    assert not repository.authorize_start("d" * 32, worker_instance_id="old-worker")
    assert repository.authorize_start("d" * 32, worker_instance_id="current-worker")
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (A,))
    assert repository.authorize_start("d" * 32, worker_instance_id="current-worker")
    with service_context(), postgres_connection(isolated.api) as connection:
        create_monomer_md_job_postgres(connection, owner_user_id=A, job_id="e" * 32, input_smiles="O", canonical_smiles="O", requested_steps=1000)
        connection.execute("UPDATE md.monomer_md_jobs SET status='submitted',worker_instance_id='current-worker' WHERE job_id=%s", ("e" * 32,))
    assert not repository.authorize_start("e" * 32, worker_instance_id="current-worker")


def test_safe_cutover_audit_includes_auth_without_credentials(isolated):
    import json
    from scripts.user_isolation_audit import capture
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("UPDATE auth.users SET password_hash='NEVER_EXPORT_CREDENTIAL' WHERE user_id=%s", (A,))
    with psycopg.connect(isolated.audit, row_factory=dict_row) as connection:
        evidence = capture(connection)
    assert evidence["business_tables"]["auth.users"]["row_count"] == 3
    assert "auth" in evidence["backup_required_schemas"]
    assert "NEVER_EXPORT_CREDENTIAL" not in json.dumps(evidence)
    assert evidence["credential_projection"] == "excluded"


def test_disabled_account_cannot_dispatch_and_user_lock_orders_start(isolated):
    repository = MonomerDftRepository(isolated.api)
    job = create(repository, A)
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("SELECT user_id FROM auth.users WHERE user_id=%s FOR UPDATE", (A,))
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(repository.authorize_start, job_id=job["job_id"], attempt_token=job["_attempt_token"], request_sha256=job["request_sha256"], enqueue_sequence=job["_enqueue_sequence"])
            connection.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (A,))
            connection.commit()
            assert pending.result(timeout=5) is False
    assert repository.claim_pending_dispatch(job_id=job["job_id"], attempt_token=job["_attempt_token"]) is False


def test_runtime_preflight_rejects_api_audit_membership_and_weak_md_policy(isolated):
    from app.auth.schema import validate_runtime_role, validate_isolation_schema
    from psycopg.conninfo import conninfo_to_dict
    api_role = conninfo_to_dict(isolated.api)["user"]
    with psycopg.connect(isolated.api, row_factory=dict_row) as connection:
        validate_runtime_role(connection, "nexpoly_api")
        validate_isolation_schema(connection)
    with psycopg.connect(isolated.admin) as connection:
        connection.execute(sql.SQL("GRANT nexpoly_mutable_audit TO {}").format(sql.Identifier(api_role)))
    try:
        with psycopg.connect(isolated.api, row_factory=dict_row) as connection, pytest.raises(ValueError, match="privileged service/auth/audit"):
            validate_runtime_role(connection, "nexpoly_api")
    finally:
        with psycopg.connect(isolated.admin) as connection:
            connection.execute(sql.SQL("REVOKE nexpoly_mutable_audit FROM {}").format(sql.Identifier(api_role)))
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("ALTER POLICY owner_access ON md.monomer_md_jobs USING (true)")
    try:
        with psycopg.connect(isolated.api, row_factory=dict_row) as connection, pytest.raises(ValueError, match="RLS policies differ"):
            validate_isolation_schema(connection)
    finally:
        with psycopg.connect(isolated.admin) as connection:
            connection.execute("ALTER POLICY owner_access ON md.monomer_md_jobs USING (owner_user_id = nullif(current_setting('app.user_id',true),'')::uuid)")


def test_dump_restored_checks_are_exactly_equivalent_and_weak_check_rejected(isolated):
    from app.auth.schema import validate_isolation_schema
    constraints = (("monomer_dft.artifacts", "artifacts_check"), ("monomer_dft.artifacts", "artifacts_name_check"), ("monomer_dft.jobs", "jobs_idempotency_key_check"))
    with psycopg.connect(isolated.admin, row_factory=dict_row) as connection:
        for table, name in constraints:
            definition = connection.execute("SELECT pg_get_constraintdef(oid,true) AS definition FROM pg_constraint WHERE conrelid=%s::regclass AND conname=%s", (table, name)).fetchone()["definition"]
            connection.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(sql.Identifier(*table.split('.')), sql.Identifier(name)))
            connection.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} ").format(sql.Identifier(*table.split('.')), sql.Identifier(name)) + sql.SQL(definition))
        validate_isolation_schema(connection)
        connection.execute("ALTER TABLE monomer_dft.jobs DROP CONSTRAINT jobs_idempotency_key_check")
        connection.execute("ALTER TABLE monomer_dft.jobs ADD CONSTRAINT jobs_idempotency_key_check CHECK (true)")
        with pytest.raises(ValueError, match="fingerprint_mismatch"):
            validate_isolation_schema(connection)
        # Roll back the intentionally weakened catalog rather than commit it.
        connection.rollback()


def test_startup_preflight_uses_service_identity_and_omits_hidden_lab_reads(isolated):
    from app.config import Settings
    from app.postgres_preflight import run_preflight, SCHEMA_TARGET_ISOLATION
    report = run_preflight(Settings(app_postgres_dsn=isolated.api, pi_postgres_dsn=isolated.api), dsn=isolated.service, mode="schema", strict=True, schema_target=SCHEMA_TARGET_ISOLATION)
    assert report["strict_ok"], report["strict_errors"]
    assert report["reverse_design_access"]["ready"] is True
    assert report["service_identity"]["privilege_group"] == "nexpoly_service"
    assert "lab.test_projects" not in report["postgres"]["tables"]


def test_disabled_dft_dispatch_with_lost_response_holds_original_attempt(isolated):
    repository = MonomerDftRepository(isolated.api)
    job = create(repository, A)
    assert repository.claim_pending_dispatch(job_id=job['job_id'], attempt_token=job['_attempt_token'])
    with psycopg.connect(isolated.admin) as connection:
        connection.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (A,))
    assert not repository.claim_pending_dispatch(job_id=job['job_id'], attempt_token=job['_attempt_token'])
    current = repository.get_job_for_service(job['job_id'])
    assert current['status'] == 'cancel_requested'
    assert current['finished_at'] is None
    assert current['_attempt_token'] == job['_attempt_token']


def test_md_uncertain_cleanup_remains_in_user_quota(isolated):
    from workers.monomer_md_worker.app.config import load_settings
    from workers.monomer_md_worker.app.repository import PostgresJobRepository, JobUpdateResult
    from app.services.monomer_md_repository import count_user_active_monomer_md_jobs_postgres
    with service_context(), postgres_connection(isolated.api) as connection:
        create_monomer_md_job_postgres(connection, owner_user_id=A, job_id='f'*32,
            input_smiles='O', canonical_smiles='O', requested_steps=1000)
    worker = PostgresJobRepository(load_settings())
    assert worker.accept_job('f'*32, 'cleanup-worker', queued=False) is JobUpdateResult.UPDATED
    assert worker.update_status('f'*32, 'failed', worker_instance_id='old-worker') is JobUpdateResult.MISSING
    assert worker.update_status('f'*32, 'running', worker_instance_id='cleanup-worker') is JobUpdateResult.UPDATED
    assert worker.update_status('f'*32, 'cancel_requested', worker_instance_id='cleanup-worker',
        error_category='resource_cleanup_unconfirmed', progress_stage='cleanup_pending') is JobUpdateResult.UPDATED
    with user_context(Identity(A)), postgres_connection(isolated.api) as connection:
        assert count_user_active_monomer_md_jobs_postgres(connection, owner_user_id=A) == 1
    # The Worker issues the terminal transition only after its Broker proof.
    assert worker.update_status('f'*32, 'cancelled', worker_instance_id='cleanup-worker') is JobUpdateResult.UPDATED
    with user_context(Identity(A)), postgres_connection(isolated.api) as connection:
        assert count_user_active_monomer_md_jobs_postgres(connection, owner_user_id=A) == 0


def test_start_authorization_logging_follows_commit_without_execution_secrets(isolated, monkeypatch):
    import app.task_observability as dft_logs
    import backend.app.task_observability as md_logs
    from workers.monomer_md_worker.app.repository import PostgresJobRepository
    observations = []

    def record(context, event, *, reason):
        with psycopg.connect(isolated.admin, row_factory=dict_row) as connection:
            if context.channel == 'dft':
                row = connection.execute('SELECT start_authorized_at FROM monomer_dft.job_attempts WHERE job_id=%s::uuid', (context.task_id,)).fetchone()
            else:
                row = connection.execute('SELECT start_authorized_at FROM md.monomer_md_jobs WHERE job_id=%s', (context.task_id,)).fetchone()
        assert row['start_authorized_at'] is not None
        observations.append((context, event, reason))

    monkeypatch.setattr(dft_logs, 'log_task_event', record)
    monkeypatch.setattr(md_logs, 'log_task_event', record)
    repository = MonomerDftRepository(isolated.api)
    job = create(repository, A)
    assert repository.authorize_start(job_id=job['job_id'], attempt_token=job['_attempt_token'],
        request_sha256=job['request_sha256'], enqueue_sequence=job['_enqueue_sequence'])
    assert observations[-1][0].attempt_id == '1'
    assert job['_attempt_token'] not in repr(observations)
    with psycopg.connect(isolated.admin, row_factory=dict_row) as connection:
        create_monomer_md_job_postgres(connection, owner_user_id=B, job_id='9'*32,
            input_smiles='O', canonical_smiles='O', requested_steps=1000)
        connection.execute("UPDATE md.monomer_md_jobs SET status='submitted',worker_instance_id='log-worker' WHERE job_id=%s", ('9'*32,))
    worker = PostgresJobRepository(SimpleNamespace(db_configured=True, app_postgres_dsn=isolated.service))
    assert worker.authorize_start('9'*32, worker_instance_id='log-worker')
    assert observations[-1][0].attempt_id == 'log-worker'
    assert [entry[1] for entry in observations] == ['start_authorized', 'start_authorized']


def test_internal_dft_start_endpoint_requires_service_identity_before_body(isolated):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    repository = MonomerDftRepository(isolated.api)
    job = create(repository, A)
    app = create_app(Settings(app_postgres_dsn=isolated.api,
        monomer_dft_start_authorization_token='test-only-start-token', model_enabled=False))
    app.state.monomer_dft_repository = repository
    client = TestClient(app)
    endpoint = f"/internal/monomer-dft/jobs/{job['job_id']}/authorize-start"
    assert client.post(endpoint, content=b'{malformed').status_code == 403
    headers = {'Authorization':'Bearer test-only-start-token'}
    payload = {'attempt_token':job['_attempt_token'], 'request_sha256':job['request_sha256'],
               'enqueue_sequence':job['_enqueue_sequence']}
    assert client.post(endpoint, headers=headers, json={**payload, 'owner_user_id':B}).status_code == 422
    response = client.post(endpoint, headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json() == {'authorized':True, 'start_authorization_version':1}
    assert client.post(endpoint, headers=headers, json=payload).json()['authorized'] is True
    assert client.post(endpoint, headers=headers, json={**payload,'attempt_token':'f'*64}).json()['authorized'] is False
    client.close()
