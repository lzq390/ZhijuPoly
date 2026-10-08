"""DFT entitlement adapter contracts with synthetic data; no DB or computation."""
from types import SimpleNamespace
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import monomer_dft_repository as dft


def prepared(calculation_type="single_point", *, requested=None):
    request = {
        "calculation_type": calculation_type, "model": "aimnet2",
        "input": {"smiles": "synthetic-not-for-quota", "multiplicity": 1},
        "conformer": {"max_iterations": 500},
    }
    if calculation_type == "single_point":
        request["single_point"] = {"properties": requested or ["energy", "charges", "forces"]}
    else:
        request["optimization"] = {"max_steps": 50, "post_optimization_properties": requested or []}
    return SimpleNamespace(public_request=request, total_atoms=5, request_sha256="b" * 64,
                           warnings=[], canonical_smiles="C", effective_charge=0)


def test_spec_uses_validated_limits_and_expands_frequency_dependency():
    spec = dft._dft_entitlement_spec(prepared(requested=["energy", "frequencies"]))
    assert spec == {
        "calculation_type": "single_point", "model": "aimnet2", "total_atoms": 5,
        "requested_properties": ["energy", "frequencies", "hessian"],
        "max_steps": 0, "conformer_max_iterations": 500,
    }
    assert "synthetic-not-for-quota" not in repr(spec)


@pytest.mark.parametrize("converged", [False, True])
def test_nonconvergence_is_preserved_without_requiring_unrequested_frequencies(converged):
    spec = dft._dft_entitlement_spec(prepared("optimization"))
    result = {"properties": {name: {} for name in ("energy", "charges", "forces")},
              "scientific_status": {"calculation_completed": True},
              "optimization": {"converged": converged}}
    facts = dft._dft_entitlement_facts(spec, result, [{"artifact_id": "scientific_result"}], validated=True)
    assert facts == {"validated": True, "completion_proven": True, "report_present": True,
                     "requested_properties_complete": True, "converged": converged}


def test_missing_requested_frequency_or_report_is_not_invented():
    spec = dft._dft_entitlement_spec(prepared("optimization", requested=["frequencies"]))
    result = {"properties": {name: {} for name in ("energy", "charges", "forces")},
              "scientific_status": {"calculation_completed": True},
              "optimization": {"converged": False}}
    facts = dft._dft_entitlement_facts(spec, result, [], validated=True)
    assert facts["requested_properties_complete"] is False
    assert facts["report_present"] is False


def test_single_point_does_not_claim_optimization_or_validation():
    spec = dft._dft_entitlement_spec(prepared())
    facts = dft._dft_entitlement_facts(spec, {"optimization": {"converged": True}}, [], validated=False)
    assert facts["converged"] is None
    assert facts["completion_proven"] is False
    assert facts["validated"] is False


OWNER = "00000000-0000-0000-0000-000000000002"
JOB = "00000000-0000-0000-0000-000000000003"
TOKEN = "a" * 64
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class Rows:
    def __init__(self, *rows, rowcount=None):
        self.rows = list(rows)
        self.rowcount = len(rows) if rowcount is None else rowcount

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        result, self.rows = self.rows, []
        return result


class Connection:
    def __init__(self, handler):
        self.handler = handler
        self.events = []
        self.exited_with = None

    def execute(self, query, params=None):
        self.events.append(("sql", " ".join(query.split()), params))
        return self.handler(query, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited_with = exc_type

    def commit(self):
        raise AssertionError("adapter must not commit the business transaction")

    def rollback(self):
        raise AssertionError("business transaction context owns rollback")


def repository(connection):
    repo = object.__new__(dft.MonomerDftRepository)
    repo._dsn = "unused-synthetic-dsn"
    repo._service_connection = lambda: connection
    repo._connection_factory = lambda _dsn: connection
    repo._get_job = lambda _conn, job_id: {"job_id": job_id, "status": "completed"}
    return repo


@pytest.fixture(autouse=True)
def isolated_flags(monkeypatch):
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED", raising=False)
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)


def quota(monkeypatch, connection, *, operation=None):
    monkeypatch.setattr(dft.ne, "enabled", lambda: True)
    monkeypatch.setattr(dft, "resolve_access_tier", lambda conn, **kw: "member")
    operation = operation or {"operation_id": "00000000-0000-0000-0000-000000000004",
                             "generation": 2, "state": "running", "native_task_id": JOB,
                             "spec": dft._dft_entitlement_spec(prepared())}
    defaults = {
        "lookup_native": None, "reserve_native": {"created": True, "operation": operation},
        "get_native": operation,
        "begin_native_dispatch": {"allowed": True, "operation": operation, "legacy": False},
        "claim_native_running": {"allowed": True, "operation": operation, "legacy": False},
        "settle_native": {"transitioned": True, "operation": operation},
        "release_native": {"transitioned": True, "operation": operation},
        "mark_native_unknown": {"transitioned": True, "operation": operation},
    }
    calls = []
    for name, default in defaults.items():
        def action(conn, _name=name, _default=default, **kwargs):
            assert conn is connection
            calls.append((_name, kwargs))
            connection.events.append(("quota", _name, kwargs))
            return _default
        monkeypatch.setattr(dft.ne, name, action)
    return calls


def creation_connection(*, existing=False):
    def handler(query, params):
        if "FROM auth.users" in query:
            return Rows({"status": "active", "is_system": False})
        if "SELECT job_id, request_sha256" in query:
            return Rows({"job_id": JOB, "request_sha256": "b" * 64}) if existing else Rows()
        if "SELECT count(*)" in query:
            return Rows({"count": 0})
        return Rows(rowcount=1)
    return Connection(handler)


def test_reservation_and_native_binding_precede_inserts_in_same_transaction(monkeypatch):
    connection = creation_connection()
    calls = quota(monkeypatch, connection)
    monkeypatch.setattr(dft, "uuid4", lambda: JOB)
    result = repository(connection).create_job(prepared(), owner_user_id=OWNER,
        idempotency_key="test-idempotency-key", max_active_jobs=3)
    assert result.created is True
    reserved = next(kwargs for name, kwargs in calls if name == "reserve_native")
    assert reserved["native_task_id"] == JOB
    assert reserved["owner_user_id"] == OWNER
    assert reserved["request_fingerprint"] == "b" * 64
    assert "synthetic-not-for-quota" not in repr(reserved)
    reserve_index = next(i for i, event in enumerate(connection.events) if event[:2] == ("quota", "reserve_native"))
    insert_index = next(i for i, event in enumerate(connection.events) if event[0] == "sql" and "INSERT INTO monomer_dft.jobs" in event[1])
    assert reserve_index < insert_index


def test_native_idempotent_replay_does_not_lookup_policy_or_reserve(monkeypatch):
    connection = creation_connection(existing=True)
    calls = quota(monkeypatch, connection)
    result = repository(connection).create_job(prepared(), owner_user_id=OWNER,
        idempotency_key="test-idempotency-key", max_active_jobs=3)
    assert result.created is False and calls == []


def test_retained_key_after_native_deletion_cannot_bind_another_job(monkeypatch):
    connection = creation_connection()
    calls = quota(monkeypatch, connection)
    monkeypatch.setattr(dft.ne, "lookup_native", lambda *args, **kwargs: {"native_task_id": "old-job"})
    with pytest.raises(dft.MonomerDftIdempotencyConflict):
        repository(connection).create_job(prepared(), owner_user_id=OWNER,
            idempotency_key="test-idempotency-key", max_active_jobs=3)
    assert calls == []
    assert not any("INSERT INTO" in event[1] for event in connection.events)


def test_reservation_failure_reaches_business_transaction_before_any_insert(monkeypatch):
    from task_usage.entitlements import EntitlementLimitExceeded
    connection = creation_connection()
    quota(monkeypatch, connection)
    def reject(*args, **kwargs):
        raise EntitlementLimitExceeded()
    monkeypatch.setattr(dft.ne, "reserve_native", reject)
    with pytest.raises(EntitlementLimitExceeded):
        repository(connection).create_job(prepared(), owner_user_id=OWNER,
            idempotency_key="test-idempotency-key", max_active_jobs=3)
    assert connection.exited_with is EntitlementLimitExceeded
    assert not any("INSERT INTO" in event[1] for event in connection.events)


def terminal_fixture(monkeypatch, *, status="completed", current_status="running"):
    current = {"status": current_status, "current_attempt": 1, "attempt_token": TOKEN,
               "request_sha256": "b" * 64, "enqueue_sequence": 2, "created_at": NOW,
               "artifacts_delete_requested_at": None, "artifacts_deleted_at": None}
    def handler(query, params):
        if "FOR UPDATE" in query:
            return Rows(current)
        if "SELECT owner_user_id" in query:
            return Rows({"owner_user_id": OWNER})
        if "UPDATE monomer_dft.jobs" in query:
            return Rows({"job_id": JOB})
        return Rows(rowcount=1)
    connection = Connection(handler)
    calls = quota(monkeypatch, connection)
    snapshot = {"job_id": JOB, "attempt_token": TOKEN, "request_sha256": "b" * 64,
                "enqueue_sequence": 2, "status": status, "stage": "artifacts", "progress_percent": 100}
    if status == "completed":
        snapshot["result"] = {"properties": {name: {} for name in ("energy", "charges", "forces")},
                              "scientific_status": {"calculation_completed": True}}
        snapshot["artifacts"] = [{"artifact_id": "scientific_result", "name": "scientific_result.json",
                                  "media_type": "application/json", "size_bytes": 10, "sha256": "c" * 64}]
    elif status == "failed":
        snapshot["error"] = {"code": "worker_restarted", "message": "synthetic restart", "retryable": True}
    return connection, repository(connection), calls, snapshot


def test_completed_result_and_artifacts_are_written_before_settlement(monkeypatch):
    connection, repo, calls, snapshot = terminal_fixture(monkeypatch)
    repo.apply_worker_snapshot(job_id=JOB, attempt_token=TOKEN, snapshot=snapshot)
    settle = next(kwargs for name, kwargs in calls if name == "settle_native")
    assert settle["native_task_id"] == JOB and settle["status"] == "completed"
    assert settle["facts"] == {"validated": True, "completion_proven": True, "report_present": True,
                                "requested_properties_complete": True, "converged": None}
    artifact_index = next(i for i, event in enumerate(connection.events) if event[0] == "sql" and "INSERT INTO monomer_dft.artifacts" in event[1])
    settle_index = next(i for i, event in enumerate(connection.events) if event[:2] == ("quota", "settle_native"))
    assert artifact_index < settle_index
    assert TOKEN not in repr(calls)


@pytest.mark.parametrize("failing_stage", ["artifacts", "settlement"])
def test_terminal_persistence_or_settlement_fault_escapes_same_transaction(monkeypatch, failing_stage):
    connection, repo, calls, snapshot = terminal_fixture(monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic transaction fault")
    if failing_stage == "artifacts":
        repo._upsert_artifacts = fail
    else:
        monkeypatch.setattr(dft.ne, "settle_native", fail)
    with pytest.raises(RuntimeError, match="synthetic transaction fault"):
        repo.apply_worker_snapshot(job_id=JOB, attempt_token=TOKEN, snapshot=snapshot)
    assert connection.exited_with is RuntimeError
    if failing_stage == "artifacts":
        assert calls == []


@pytest.mark.parametrize("field,value", [("attempt_token", "d" * 64), ("request_sha256", "d" * 64), ("enqueue_sequence", 99)])
def test_stale_identity_never_reaches_quota(monkeypatch, field, value):
    connection, repo, calls, snapshot = terminal_fixture(monkeypatch)
    snapshot[field] = value
    with pytest.raises(dft.MonomerDftStaleAttempt):
        repo.apply_worker_snapshot(job_id=JOB, attempt_token=TOKEN, snapshot=snapshot)
    assert calls == []


def test_duplicate_terminal_never_settles_again(monkeypatch):
    _connection, repo, calls, snapshot = terminal_fixture(monkeypatch, current_status="completed")
    repo.apply_worker_snapshot(job_id=JOB, attempt_token=TOKEN, snapshot=snapshot)
    assert calls == []


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_only_validated_worker_terminal_releases_dispatched_reservation(monkeypatch, status):
    _connection, repo, calls, snapshot = terminal_fixture(monkeypatch, status=status)
    repo.apply_worker_snapshot(job_id=JOB, attempt_token=TOKEN, snapshot=snapshot)
    released = next(kwargs for name, kwargs in calls if name == "release_native")
    assert released["source"] == "dft_validated_worker_terminal"
    assert released["status"] == status
    assert released.get("never_dispatched", False) is False
    assert not any(name == "settle_native" for name, _ in calls)


def test_old_native_job_without_operation_is_not_retroactively_charged(monkeypatch):
    _connection, repo, calls, snapshot = terminal_fixture(monkeypatch)
    monkeypatch.setattr(dft.ne, "get_native", lambda *args, **kwargs: None)
    repo.apply_worker_snapshot(job_id=JOB, attempt_token=TOKEN, snapshot=snapshot)
    assert calls == []


def test_default_off_adds_no_entitlement_reads_or_transitions(monkeypatch):
    connection, repo, calls, snapshot = terminal_fixture(monkeypatch)
    monkeypatch.setattr(dft.ne, "enabled", lambda: False)
    repo.apply_worker_snapshot(job_id=JOB, attempt_token=TOKEN, snapshot=snapshot)
    assert calls == []
    assert not any("SELECT owner_user_id" in event[1] for event in connection.events)


@pytest.mark.parametrize("submitted", [False, True])
def test_cancel_releases_only_proven_never_dispatched_job(monkeypatch, submitted):
    connection = Connection(lambda query, params: Rows({"status": "pending", "submitted_at": NOW if submitted else None})
                            if "FOR UPDATE" in query else Rows(rowcount=1))
    calls = quota(monkeypatch, connection)
    repository(connection).request_cancel(JOB, owner_user_id=OWNER)
    releases = [kwargs for name, kwargs in calls if name == "release_native"]
    assert bool(releases) is not submitted
    if releases:
        assert releases[0]["never_dispatched"] is True
        assert releases[0]["source"] == "dft_unsubmitted_cancel"


def admission_connection(*, authorized=False, active=True, submitted=False):
    def handler(query, params):
        if "FROM auth.users" in query:
            return Rows({"status": "active" if active else "disabled", "is_system": False})
        if "SELECT owner_user_id" in query:
            return Rows({"owner_user_id": OWNER})
        if "SELECT start_authorized_at" in query:
            return Rows({"start_authorized_at": NOW if authorized else None})
        if "FOR UPDATE" in query and "monomer_dft.jobs" in query:
            return Rows({"status": "pending", "current_attempt": 1, "attempt_token": TOKEN,
                         "request_sha256": "b" * 64, "enqueue_sequence": 2,
                         "submitted_at": NOW if submitted else None})
        if "SELECT current_attempt" in query:
            return Rows({"current_attempt": 1})
        return Rows(rowcount=1)
    return Connection(handler)


def test_dispatch_permission_is_required_before_outbound_marker(monkeypatch):
    connection = admission_connection()
    calls = quota(monkeypatch, connection)
    monkeypatch.setattr(dft.ne, "begin_native_dispatch", lambda *args, **kwargs: {"allowed": False})
    assert repository(connection).claim_pending_dispatch(job_id=JOB, attempt_token=TOKEN) is False
    assert not any("UPDATE monomer_dft.jobs" in event[1] for event in connection.events)
    assert calls == []


@pytest.mark.parametrize("submitted", [False, True])
def test_disabled_owner_only_releases_when_dispatch_was_never_claimed(monkeypatch, submitted):
    connection = admission_connection(active=False, submitted=submitted)
    calls = quota(monkeypatch, connection)
    assert repository(connection).claim_pending_dispatch(job_id=JOB, attempt_token=TOKEN) is False
    releases = [kwargs for name, kwargs in calls if name == "release_native"]
    assert bool(releases) is not submitted
    if releases:
        assert releases[0]["source"] == "dft_disabled_before_dispatch"


@pytest.mark.parametrize("authorized", [False, True])
def test_start_claim_distinguishes_first_grant_from_fenced_native_replay(monkeypatch, authorized):
    connection = admission_connection(authorized=authorized)
    calls = quota(monkeypatch, connection)
    assert repository(connection).authorize_start(job_id=JOB, attempt_token=TOKEN,
        request_sha256="b" * 64, enqueue_sequence=2) is True
    claims = [kwargs for name, kwargs in calls if name == "claim_native_running"]
    assert len(claims) == 1
    assert claims[0].get("allow_authorized_replay", False) is authorized
    assert claims[0]["executor_id"] == f"monomer_dft:{JOB}:attempt:1"
    assert TOKEN not in repr(claims)


def test_unknown_execution_cannot_obtain_new_start_grant(monkeypatch):
    connection = admission_connection()
    quota(monkeypatch, connection)
    monkeypatch.setattr(dft.ne, "claim_native_running", lambda *args, **kwargs: {"allowed": False})
    assert repository(connection).authorize_start(job_id=JOB, attempt_token=TOKEN,
        request_sha256="b" * 64, enqueue_sequence=2) is False
    assert not any("SET start_authorized_at" in event[1] for event in connection.events)


def test_transport_error_marks_unknown_without_releasing(monkeypatch):
    connection = admission_connection()
    calls = quota(monkeypatch, connection)
    repository(connection).record_dispatch_error(job_id=JOB, attempt_token=TOKEN,
        code="worker_unavailable", message="synthetic error", retryable=True)
    assert [name for name, _ in calls] == ["mark_native_unknown"]


def test_unknown_pending_reconciliation_only_reads_worker_job(monkeypatch):
    from app.services.monomer_dft_reconciler import MonomerDftReconciler
    monkeypatch.setattr(dft.ne, "enabled", lambda: True)
    job = {"job_id": JOB, "_attempt_token": TOKEN, "status": "pending"}
    repo = SimpleNamespace(claim_pending_dispatch=Mock(return_value=False),
        get_job_for_service=Mock(return_value=job), apply_worker_snapshot=Mock(return_value={"status": "completed"}))
    worker = SimpleNamespace(get_job=AsyncMock(return_value={"status": "completed"}), submit_job=AsyncMock())
    reconciler = MonomerDftReconciler(repository=repo, worker=worker, interval_seconds=1)
    result = asyncio.run(reconciler.reconcile_job(job))
    assert result["status"] == "completed"
    worker.get_job.assert_awaited_once_with(JOB)
    worker.submit_job.assert_not_awaited()


def test_default_off_reconciler_preserves_original_no_claim_behavior(monkeypatch):
    from app.services.monomer_dft_reconciler import MonomerDftReconciler
    job = {"job_id": JOB, "_attempt_token": TOKEN, "status": "pending"}
    repo = SimpleNamespace(claim_pending_dispatch=Mock(return_value=False), get_job_for_service=Mock(return_value=job))
    worker = SimpleNamespace(get_job=AsyncMock(), submit_job=AsyncMock())
    result = asyncio.run(MonomerDftReconciler(repository=repo, worker=worker, interval_seconds=1).reconcile_job(job))
    assert result is job
    worker.get_job.assert_not_awaited()
    worker.submit_job.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
def test_post_replay_attaches_native_unknown_operation_without_new_dispatch(monkeypatch, enabled):
    from app.routers import monomer_dft as route
    operation = {"operation_id": "00000000-0000-0000-0000-000000000004", "state": "unknown"}
    lookup = Mock(return_value=operation)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(), entitlement_repository=SimpleNamespace(get_by_native=lookup))),
        state=SimpleNamespace())
    job = {"job_id": JOB, "status": "pending"}
    monkeypatch.setattr(route.ne, "enabled", lambda: enabled)
    monkeypatch.setattr(route, "current_owner_id", lambda: OWNER)
    monkeypatch.setattr(route, "_require_schema_ready", AsyncMock())
    monkeypatch.setattr(route, "_repository", lambda request: object())
    monkeypatch.setattr(route, "_find_idempotent_job", AsyncMock(return_value=job))
    monkeypatch.setattr(route, "calculation_request_sha256", lambda body: "b" * 64)
    monkeypatch.setattr(route, "_public_job", lambda repository, value, replay=False: value)
    kick = Mock()
    monkeypatch.setattr(route, "_kick_reconciler", kick)
    result = asyncio.run(route.create_job(request, SimpleNamespace(model_dump=lambda **kw: {}), "test-idempotency-key"))
    assert result is job
    kick.assert_not_called()
    if enabled:
        lookup.assert_called_once_with(owner_user_id=OWNER, module="monomer_dft", native_task_id=JOB)
        assert request.state.usage_operation_id == operation["operation_id"]
        assert request.state.usage_operation_state == "unknown"
    else:
        lookup.assert_not_called()
        assert not hasattr(request.state, "usage_operation_id")


# These integration cases use the existing isolated PostgreSQL fixture and real
# entitlement/native SQL. Synthetic protocol reports do not execute DFT models.
from contextlib import contextmanager
from uuid import uuid4
import psycopg
from pydantic import TypeAdapter
from task_usage import entitlements as entitlement_core, entitlement_config
from task_usage import _entitlement_store as entitlement_store
from test_entitlement_repository import entitlement_db, usage_db, connection as pg_connection, policy
from test_usage_repository import B
from app.services.monomer_dft_models import MonomerDftRunRequest
from app.services.monomer_dft_protocol import prepare_monomer_dft_request
from app.services.monomer_dft_internal_models import InternalWorkerSnapshot


@pytest.fixture
def dft_pg(entitlement_db, isolated_flags, monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    owner = str(uuid4())
    with psycopg.connect(entitlement_db) as conn:
        conn.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES (%s,%s,'!',false)",
                     (owner, "dft-entitlement-test-" + owner))
    installed = policy(entitlement_db, owner)
    monkeypatch.setattr(entitlement_config, "configured_policy", lambda: installed)
    from test_monomer_dft_backend import _single_point_request
    request = prepare_monomer_dft_request(TypeAdapter(MonomerDftRunRequest).validate_python(_single_point_request()))
    repo = dft.MonomerDftRepository(entitlement_db,
        connection_factory=lambda _dsn: pg_connection(entitlement_db, owner, "nexpoly_service"))
    value = SimpleNamespace(dsn=entitlement_db, owner=owner, policy=installed, repo=repo, prepared=request)
    try:
        yield value
    finally:
        # Only this test's synthetic native rows; retained operations stay intact.
        with pg_connection(entitlement_db, owner, "nexpoly_service") as conn:
            conn.execute("DELETE FROM monomer_dft.jobs WHERE owner_user_id=%s::uuid", (owner,))


def pg_dft_create(case, key=None):
    return case.repo.create_job(case.prepared, owner_user_id=case.owner,
        idempotency_key=key or uuid4().hex, max_active_jobs=9)


def pg_dft_identity(case, job):
    return dict(job_id=job["job_id"], attempt_token=job["_attempt_token"],
                request_sha256=case.prepared.request_sha256, enqueue_sequence=job["_enqueue_sequence"])


def pg_dft_snapshot(case, job, status="completed"):
    from test_monomer_dft_backend import _completed_v2_worker_snapshot, _worker_snapshot, _cancelled_worker_snapshot
    submit = {"schema_version": 2, **pg_dft_identity(case, job), **case.prepared.worker_request}
    if status == "completed":
        value = _completed_v2_worker_snapshot(submit, case.prepared)
    elif status == "cancelled":
        value = _cancelled_worker_snapshot(submit)
    else:
        value = _worker_snapshot(submit, status=status)
        value.update(stage="single_point", started_at=value["created_at"])
    return InternalWorkerSnapshot.model_validate(value).model_dump(mode="json", exclude_unset=True)


def pg_dft_state(case, job):
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        op = entitlement_core.get_by_native(conn, owner_user_id=case.owner,
            module="monomer_dft", native_task_id=job["job_id"])
        summary = entitlement_core.summary(conn, owner_user_id=case.owner, policy_version=case.policy["version"])
        native = conn.execute("SELECT j.status,j.result_json AS result,a.start_authorized_at FROM monomer_dft.jobs j JOIN monomer_dft.job_attempts a USING(job_id) WHERE job_id=%s::uuid", (job["job_id"],)).fetchone()
    return op, summary, native


def pg_dft_begin(case, job):
    identity = pg_dft_identity(case, job)
    assert case.repo.claim_pending_dispatch(job_id=job["job_id"], attempt_token=job["_attempt_token"])
    assert case.repo.authorize_start(**identity)
    case.repo.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"],
                                   snapshot=pg_dft_snapshot(case, job, "running"))


def test_postgres_dft_replay_validated_result_and_settlement_commit_once(dft_pg):
    case, key = dft_pg, uuid4().hex
    created = pg_dft_create(case, key)
    job = created.job
    replay = pg_dft_create(case, key)
    assert created.created and not replay.created and replay.job["job_id"] == job["job_id"]
    pg_dft_begin(case, job)
    snapshot = pg_dft_snapshot(case, job)
    case.repo.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=snapshot)
    op, summary, native = pg_dft_state(case, job)
    assert native["status"] == "completed" and native["result"]["scientific_status"]["calculation_completed"]
    assert op["state"] == "succeeded" and op["generation"] == 2
    assert sum(pool["success_used"] for pool in summary["pools"]) == 1
    assert sum(pool["reserved"] for pool in summary["pools"]) == 0
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        assert conn.execute("SELECT count(*) AS n FROM monomer_dft.artifacts WHERE job_id=%s::uuid", (job["job_id"],)).fetchone()["n"] == 3
    case.repo.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=snapshot)
    assert pg_dft_state(case, job)[0] == op
    with pg_connection(case.dsn, B) as conn:
        assert entitlement_core.get(conn, owner_user_id=B, operation_id=op["operation_id"]) is None


def test_postgres_dft_create_failure_rolls_back_native_and_reservation(dft_pg):
    case, key = dft_pg, uuid4().hex
    @contextmanager
    def rollback(_dsn):
        with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
            yield conn
            raise RuntimeError("synthetic transaction rollback")
    bad = dft.MonomerDftRepository(case.dsn, connection_factory=rollback)
    with pytest.raises(RuntimeError, match="synthetic transaction rollback"):
        bad.create_job(case.prepared, owner_user_id=case.owner, idempotency_key=key, max_active_jobs=9)
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        assert conn.execute("SELECT count(*) AS n FROM monomer_dft.jobs WHERE owner_user_id=%s::uuid", (case.owner,)).fetchone()["n"] == 0
        assert dft.ne.lookup_native(conn, owner_user_id=case.owner, module="monomer_dft", idempotency_key=key,
                                    request_fingerprint=case.prepared.request_sha256) is None
    job = pg_dft_create(case, key).job
    case.repo.request_cancel(job["job_id"], owner_user_id=case.owner)
    assert pg_dft_state(case, job)[0]["state"] == "released"


def test_postgres_dft_terminal_rollback_preserves_running_then_retry_settles(dft_pg):
    case = dft_pg
    job = pg_dft_create(case).job
    pg_dft_begin(case, job)
    before = pg_dft_state(case, job)[0]
    @contextmanager
    def rollback(_dsn):
        with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
            yield conn
            raise RuntimeError("synthetic terminal rollback")
    bad = dft.MonomerDftRepository(case.dsn, connection_factory=rollback)
    snapshot = pg_dft_snapshot(case, job)
    with pytest.raises(RuntimeError, match="synthetic terminal rollback"):
        bad.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=snapshot)
    op, summary, native = pg_dft_state(case, job)
    assert op == before and native["status"] == "running" and native["result"] is None
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        assert conn.execute("SELECT count(*) AS n FROM monomer_dft.artifacts WHERE job_id=%s::uuid", (job["job_id"],)).fetchone()["n"] == 0
    case.repo.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=snapshot)
    assert pg_dft_state(case, job)[0]["state"] == "succeeded"


@pytest.mark.parametrize("collection_enabled", [False, True])
def test_postgres_dft_free_first_start_denied_with_collection_on_or_off(dft_pg, monkeypatch, collection_enabled):
    case = dft_pg
    job = pg_dft_create(case).job
    assert case.repo.claim_pending_dispatch(job_id=job["job_id"], attempt_token=job["_attempt_token"])
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        entitlement_store.set_owner_tier(conn, owner_user_id=case.owner, tier="free")
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true" if collection_enabled else "false")
    assert not case.repo.authorize_start(**pg_dft_identity(case, job))
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        row = conn.execute("SELECT j.status,a.start_authorized_at FROM monomer_dft.jobs j JOIN monomer_dft.job_attempts a USING(job_id) WHERE j.job_id=%s::uuid", (job["job_id"],)).fetchone()
        assert row["status"] == "cancel_requested" and row["start_authorized_at"] is None
    # Refusal alone cannot prove the already-acquired Worker lease is cleaned.
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    assert pg_dft_state(case, job)[0]["state"] == "dispatching"
    case.repo.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=pg_dft_snapshot(case, job, "cancelled"))
    assert pg_dft_state(case, job)[0]["state"] == "released"


def test_postgres_dft_authorized_replay_and_settle_do_not_reread_tier(dft_pg, monkeypatch):
    case = dft_pg
    job = pg_dft_create(case).job
    pg_dft_begin(case, job)
    before = pg_dft_state(case, job)[0]
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        entitlement_store.set_owner_tier(conn, owner_user_id=case.owner, tier="free")
    def unexpected_read(*args, **kwargs):
        raise AssertionError("already authorized replay must not query account tier")
    monkeypatch.setattr(dft, "resolve_access_tier", unexpected_read)
    assert case.repo.authorize_start(**pg_dft_identity(case, job))
    wrong = {**pg_dft_identity(case, job), "attempt_token": "f" * 64}
    assert not case.repo.authorize_start(**wrong)
    assert pg_dft_state(case, job)[0] == before
    case.repo.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=pg_dft_snapshot(case, job))
    assert pg_dft_state(case, job)[0]["state"] == "succeeded"
