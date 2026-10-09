"""Native batch quota contracts: mock contracts and disposable PostgreSQL; no scientific execution."""
from copy import deepcopy
import asyncio
from unittest.mock import Mock
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.polymerization_batch import repository as batch
from app.services.polymerization_batch import service as service_module
from app.services.polymerization_batch.models import BatchError, BatchJobCreate, BatchSettings
from app.services.polymerization_batch.storage import file_manifest, write_json

OWNER = "00000000-0000-0000-0000-000000000002"
JOB = "a" * 32
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


class MemoryTransaction:
    def __init__(self, handler=None):
        self.events = []
        self.handler = handler or (lambda *args: Rows(rowcount=1))
        self.exited_with = None

    def execute(self, query, params=None):
        self.events.append(("sql", " ".join(query.split()), params))
        return self.handler(query, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited_with = exc_type

    def commit(self):
        raise AssertionError("native transaction owns commit")

    def rollback(self):
        raise AssertionError("native transaction owns rollback")


@pytest.fixture(autouse=True)
def isolated_flags(monkeypatch):
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED", raising=False)


def quota(monkeypatch, connection, *, state="reserved", legacy=False):
    operation = {"operation_id": "00000000-0000-0000-0000-000000000004", "generation": 1,
                 "state": state, "native_task_id": JOB, "executor_id": f"polymerization_batch:{JOB}"}
    calls = []
    monkeypatch.setattr(batch.ne, "enabled", lambda: True)
    def invoke(name):
        def call(conn, **kwargs):
            assert conn is connection
            calls.append((name, kwargs))
            connection.events.append(("quota", name, kwargs))
            if name == "lookup_native":
                return None
            if name == "get_native":
                return None if legacy else operation
            if name == "reserve_native":
                return {"created": True, "operation": operation}
            if name == "begin_native_dispatch":
                operation.update(state="dispatching", generation=2, executor_id=kwargs["executor_id"])
                return {"allowed": True, "operation": operation}
            if name == "claim_native_running":
                operation["state"] = "running"
                return {"allowed": True, "operation": operation}
            operation["state"] = "succeeded" if name == "settle_native" else "released"
            return {"transitioned": True, "operation": operation}
        return call
    for name in ("lookup_native", "get_native", "reserve_native", "begin_native_dispatch",
                 "claim_native_running", "settle_native", "release_native"):
        monkeypatch.setattr(batch.ne, name, invoke(name))
    return operation, calls


def export_result(status="completed", *, complete=True, pair_errors=0):
    return {"status": status, "summary": {"complete": complete, "pair_errors": pair_errors},
            "artifacts": {name: {"sha256": "b" * 64, "size_bytes": 10}
                          for name in ("summary.json", "results.zip", "results.xlsx")}}


def preview_snapshot():
    return {"engine": {"fingerprint": "test-engine"}, "raw_pairs": 4, "valid_pairs": 4,
            "unique_pairs": 1,
            "tables": {role: {"row_count": 2, "unique_smiles": [smiles],
                               "rows": [{"canonical_smiles": smiles}]}
                       for role, smiles in (("a", "C"), ("b", "N"))}}


def test_spec_uses_actual_preview_statistics_without_chemistry_payload():
    spec = service_module._batch_entitlement_spec(preview_snapshot())
    assert spec == {"raw_pairs": 4, "valid_pairs": 4, "rows_a": 2, "rows_b": 2}


@pytest.mark.parametrize("status,complete,errors", [("completed", True, 0), ("completed_with_errors", True, 2), ("completed", False, 0)])
def test_export_facts_preserve_partial_and_error_evidence(status, complete, errors):
    facts = batch._batch_entitlement_facts(export_result(status, complete=complete, pair_errors=errors))
    assert facts == {"validated": True, "completion_proven": True, "report_present": True,
                     "complete": complete, "pair_errors": errors}


def test_report_or_summary_fields_are_not_fabricated():
    report = export_result()
    report["artifacts"].pop("results.xlsx")
    assert batch._batch_entitlement_facts(report)["report_present"] is False
    report["summary"].pop("pair_errors")
    with pytest.raises(KeyError):
        batch._batch_entitlement_facts(report)


def submit_fixture(tmp_path, monkeypatch, *, existing=False):
    config = BatchSettings(enabled=True, storage_root=tmp_path)
    source = tmp_path / "imports" / ("b" * 32)
    source.mkdir(parents=True)
    snapshot = preview_snapshot()
    write_json(source / "preview.json", snapshot)
    files = {}
    for role in ("a", "b"):
        path = source / f"{role}.csv"
        path.write_text("SMILES\nC\n")
        files[role] = file_manifest(tmp_path, path, "text/csv")
    imported = {"preview_revision": "c" * 32, "files": files,
                "preview": {"can_submit": True,
                    "snapshot": file_manifest(tmp_path, source / "preview.json", "application/json"),
                    "statistics": {"raw_pairs": 4, "valid_pairs": 4, "unique_pairs": 1}}}
    connection = MemoryTransaction()
    def insert(conn, job_id, *args, **kwargs):
        assert conn is connection
        connection.events.append(("native", "insert", job_id))
        return {"id": job_id}
    repository = SimpleNamespace(connection=lambda: connection,
        admit=lambda *args, **kwargs: {"id": JOB} if existing else None,
        worker_status=lambda: {"fresh": True, "available": True, "engine": snapshot["engine"]},
        get_import=lambda *args, **kwargs: imported,
        insert_job=insert)
    service = object.__new__(service_module.BatchService)
    service.config, service.root, service.repository = config, tmp_path, repository
    service.public_job = lambda row: row
    monkeypatch.setattr(service_module, "current_owner_id", lambda: OWNER)
    monkeypatch.setattr(service_module.shutil, "disk_usage", lambda *_args: SimpleNamespace(free=10**12))
    monkeypatch.setattr(service_module, "uuid4", lambda: SimpleNamespace(hex=JOB))
    request = BatchJobCreate(import_id="b" * 32, preview_revision="c" * 32)
    return connection, service, request


def test_submit_reserves_verified_spec_and_raw_id_before_native_insert(tmp_path, monkeypatch):
    connection, service, request = submit_fixture(tmp_path, monkeypatch)
    _operation, calls = quota(monkeypatch, connection)
    assert service.create_job(request, "batch-test-key")["id"] == JOB
    reserve = next(kwargs for name, kwargs in calls if name == "reserve_native")
    assert reserve["native_task_id"] == JOB and reserve["owner_user_id"] == OWNER
    assert reserve["spec"] == {"raw_pairs": 4, "valid_pairs": 4, "rows_a": 2, "rows_b": 2}
    assert [event[1] for event in connection.events] == ["lookup_native", "reserve_native", "insert"]


def test_existing_native_job_replays_without_quota_or_policy(tmp_path, monkeypatch):
    connection, service, request = submit_fixture(tmp_path, monkeypatch, existing=True)
    _operation, calls = quota(monkeypatch, connection)
    assert service.create_job(request, "batch-test-key")["id"] == JOB
    assert calls == []


def test_retained_deleted_job_key_cannot_rebind_or_create_files(tmp_path, monkeypatch):
    connection, service, request = submit_fixture(tmp_path, monkeypatch)
    quota(monkeypatch, connection)
    monkeypatch.setattr(batch.ne, "lookup_native", lambda *args, **kwargs: {"native_task_id": "old-job"})
    with pytest.raises(BatchError) as error:
        service.create_job(request, "batch-test-key")
    assert error.value.status == 409
    assert not (tmp_path / "jobs" / JOB).exists()
    assert not any(event[0] == "native" for event in connection.events)


def test_reserve_limit_failure_rolls_back_before_native_or_file_creation(tmp_path, monkeypatch):
    from task_usage.entitlements import EntitlementLimitExceeded
    connection, service, request = submit_fixture(tmp_path, monkeypatch)
    quota(monkeypatch, connection)
    def fail(*args, **kwargs):
        raise EntitlementLimitExceeded()
    monkeypatch.setattr(batch.ne, "reserve_native", fail)
    with pytest.raises(BatchError) as error:
        service.create_job(request, "batch-test-key")
    assert error.value.code == "entitlement_limit_exceeded"
    assert error.value.status == 429
    assert connection.exited_with is EntitlementLimitExceeded
    assert not (tmp_path / "jobs" / JOB).exists()


def execution_fixture(monkeypatch, *, kinds=("classify", "generate", "export")):
    job = {"id": JOB, "owner_user_id": OWNER, "status": "queued", "execution_token": None,
           "start_authorized_at": NOW, "terminal_intent": None, "consumed_seconds": 0,
           "engine": {"fingerprint": "test-engine"}, "summary": {}, "worker_id": None}
    chunks = [{"chunk_id": f"c{n}", "kind": kind, "status": "pending", "attempt": 0,
               "attempt_token": None} for n, kind in enumerate(kinds)]
    def handler(query, params):
        flat = " ".join(query.split())
        if flat.startswith("SELECT * FROM polymerization_batch.jobs"):
            if "WHERE status IN" in flat and (job["execution_token"] is not None or job["status"] not in {"queued", "running", "cancelling"}):
                return Rows()
            return Rows(deepcopy(job))
        if flat.startswith("SELECT * FROM polymerization_batch.chunks"):
            pending = next((c for c in chunks if c["status"] == "pending"), None)
            return Rows(deepcopy(pending)) if pending else Rows()
        if "UPDATE polymerization_batch.chunks SET attempt=attempt+1" in flat:
            chunk = next(c for c in chunks if c["chunk_id"] == params[-1])
            chunk.update(attempt=chunk["attempt"] + 1, attempt_token=params[0])
            return Rows(deepcopy(chunk))
        if "UPDATE polymerization_batch.jobs SET execution_token=%s" in flat:
            job.update(execution_token=params[0], worker_id=params[1], status="running", stage=params[2])
            return Rows(deepcopy(job))
        if "UPDATE polymerization_batch.chunks SET status='completed'" in flat:
            next(c for c in chunks if c["chunk_id"] == params[-1])["status"] = "completed"
        if "UPDATE polymerization_batch.chunks SET status='skipped'" in flat:
            for c in chunks:
                if c["status"] == "pending" and ("kind<>'export'" not in flat or c["kind"] != "export"):
                    c["status"] = "skipped"
        if "UPDATE polymerization_batch.chunks SET attempt_token=NULL" in flat:
            for c in chunks:
                if c["status"] == "pending":
                    c["attempt_token"] = None
        if "UPDATE polymerization_batch.jobs SET" in flat:
            if "WHERE id=%s AND execution_token=%s" in flat and job["execution_token"] != params[-1]:
                return Rows(rowcount=0)
            if "execution_token=NULL" in flat:
                job["execution_token"] = None
            if "status=%s,stage='finished'" in flat:
                job["status"] = params[1]
            if "status=CASE WHEN %s THEN status ELSE 'failed' END" in flat and not params[3]:
                job["status"] = "failed"
        return Rows(rowcount=1)
    connection = MemoryTransaction(handler)
    repo = batch.BatchRepository("unused-synthetic-dsn", BatchSettings())
    repo.connection = lambda: connection
    repo.get_job_for_service = lambda *args, **kwargs: deepcopy(job)
    monkeypatch.setattr(batch, "get_drain_state", lambda *args, **kwargs: SimpleNamespace(enabled=False))
    counter = iter(range(1, 100))
    monkeypatch.setattr(batch, "uuid4", lambda: SimpleNamespace(hex=f"{next(counter):032x}"))
    return connection, repo, job, chunks


def test_many_chunks_and_final_export_use_one_root_dispatch_and_settlement(monkeypatch):
    connection, repo, _job, _chunks = execution_fixture(monkeypatch)
    _operation, calls = quota(monkeypatch, connection)
    for kind in ("classify", "generate", "export"):
        job, chunk = repo.claim("worker-one", "test-engine")
        assert chunk["kind"] == kind
        assert repo.finish_unit(job, chunk, {}, 0.1, export=export_result() if kind == "export" else None)
    names = [name for name, _ in calls]
    assert names.count("begin_native_dispatch") == 1
    assert names.count("claim_native_running") == 1
    assert names.count("settle_native") == 1
    assert "release_native" not in names
    final_write = next(i for i, event in enumerate(connection.events) if event[0] == "sql" and "artifacts=%s" in event[1])
    settlement = next(i for i, event in enumerate(connection.events) if event[:2] == ("quota", "settle_native"))
    assert final_write < settlement


def test_cleanup_recovery_retries_same_root_and_fences_old_attempt(monkeypatch):
    connection, repo, _job, _chunks = execution_fixture(monkeypatch, kinds=("generate", "export"))
    _operation, calls = quota(monkeypatch, connection)
    old_job, old_chunk = repo.claim("worker-before-restart", "test-engine")
    repo.recover(cleanup_confirmed=True)
    new_job, new_chunk = repo.claim("worker-after-restart", "test-engine")
    assert new_chunk["attempt"] == old_chunk["attempt"] + 1
    assert new_job["execution_token"] != old_job["execution_token"]
    count = len(calls)
    assert repo.finish_unit(old_job, old_chunk, {}, 0.1) is False
    assert len(calls) == count
    assert repo.finish_unit(new_job, new_chunk, {}, 0.1)
    export_job, export_chunk = repo.claim("worker-after-restart", "test-engine")
    assert repo.finish_unit(export_job, export_chunk, {}, 0.1, export=export_result())
    names = [name for name, _ in calls]
    assert names.count("begin_native_dispatch") == names.count("claim_native_running") == names.count("settle_native") == 1
    assert "release_native" not in names


def test_recovery_requires_native_cleanup_proof_without_touching_quota(monkeypatch):
    connection, repo, _job, _chunks = execution_fixture(monkeypatch)
    _operation, calls = quota(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="confirmed resource"):
        repo.recover()
    assert connection.events == [] and calls == []


@pytest.mark.parametrize("state", ["unknown", "dispatching", "released", "succeeded"])
def test_nonrunning_existing_root_never_permits_new_chunk(monkeypatch, state):
    connection, repo, job, chunks = execution_fixture(monkeypatch)
    _operation, calls = quota(monkeypatch, connection, state=state)
    assert repo.claim("worker", "test-engine") is None
    assert job["execution_token"] is None and chunks[0]["attempt"] == 0
    assert [name for name, _ in calls] == ["get_native"]


def test_running_operation_never_bypasses_live_native_token(monkeypatch):
    connection, repo, job, _chunks = execution_fixture(monkeypatch)
    _operation, calls = quota(monkeypatch, connection, state="running")
    job["execution_token"] = "still-live-native-token"
    assert repo.claim("worker", "test-engine") is None
    assert calls == []


def test_running_root_cannot_continue_with_different_core_executor(monkeypatch):
    connection, repo, _job, chunks = execution_fixture(monkeypatch)
    operation, calls = quota(monkeypatch, connection, state="running")
    operation["executor_id"] = "different-root-executor"
    assert repo.claim("worker", "test-engine") is None
    assert chunks[0]["attempt"] == 0
    assert [name for name, _ in calls] == ["get_native"]


def test_cancel_racing_export_only_releases_final_native_cancelled_root(monkeypatch):
    connection, repo, current, _chunks = execution_fixture(monkeypatch, kinds=("export",))
    _operation, calls = quota(monkeypatch, connection)
    job, chunk = repo.claim("worker", "test-engine")
    current["terminal_intent"] = "cancelled"
    assert repo.finish_unit(job, chunk, {}, 0.1, export=export_result()) is False
    assert not any(name in {"settle_native", "release_native"} for name, _ in calls)
    cancelled_job, cancelled_chunk = repo.claim("worker", "test-engine")
    assert repo.finish_unit(cancelled_job, cancelled_chunk, {}, 0.1,
                            export=export_result("cancelled", complete=False))
    releases = [kwargs for name, kwargs in calls if name == "release_native"]
    assert len(releases) == 1
    assert releases[0]["source"] == "batch_native_terminal" and releases[0]["status"] == "cancelled"


@pytest.mark.parametrize("finalize,stale", [(True, False), (False, False), (False, True)])
def test_child_stop_does_not_release_and_terminal_stop_requires_matching_token(monkeypatch, finalize, stale):
    connection, repo, _current, _chunks = execution_fixture(monkeypatch)
    _operation, calls = quota(monkeypatch, connection)
    job, _chunk = repo.claim("worker", "test-engine")
    if stale:
        job["execution_token"] = "stale-token"
    repo.stop_execution(job, elapsed=0.1, code="test_failure", message="synthetic", finalize=finalize)
    releases = [kwargs for name, kwargs in calls if name == "release_native"]
    assert bool(releases) is (not finalize and not stale)


def test_interrupted_child_release_keeps_root_reservation(monkeypatch):
    connection, repo, _current, _chunks = execution_fixture(monkeypatch)
    _operation, calls = quota(monkeypatch, connection)
    job, _chunk = repo.claim("worker", "test-engine")
    before = len(calls)
    repo.release_execution(job, elapsed=0.1)
    assert len(calls) == before


def test_final_settlement_fault_escapes_original_business_transaction(monkeypatch):
    connection, repo, _current, _chunks = execution_fixture(monkeypatch, kinds=("export",))
    quota(monkeypatch, connection)
    job, chunk = repo.claim("worker", "test-engine")
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic settlement failure")
    monkeypatch.setattr(batch.ne, "settle_native", fail)
    with pytest.raises(RuntimeError, match="settlement failure"):
        repo.finish_unit(job, chunk, {}, 0.1, export=export_result())
    assert connection.exited_with is RuntimeError


def test_legacy_native_job_without_operation_does_not_reserve_or_settle(monkeypatch):
    connection, repo, _current, _chunks = execution_fixture(monkeypatch, kinds=("export",))
    _operation, calls = quota(monkeypatch, connection, legacy=True)
    job, chunk = repo.claim("worker", "test-engine")
    assert repo.finish_unit(job, chunk, {}, 0.1, export=export_result())
    assert all(name == "get_native" for name, _ in calls)


def test_default_off_adds_no_quota_reads_or_transitions(monkeypatch):
    connection, repo, _current, _chunks = execution_fixture(monkeypatch, kinds=("export",))
    _operation, calls = quota(monkeypatch, connection)
    monkeypatch.setattr(batch.ne, "enabled", lambda: False)
    job, chunk = repo.claim("worker", "test-engine")
    assert repo.finish_unit(job, chunk, {}, 0.1, export=export_result())
    assert calls == []


@pytest.mark.parametrize("status,complete,errors", [("completed_with_errors", True, 0), ("completed", False, 0), ("completed", True, 1)])
def test_default_policy_charges_zero_for_partial_batch_results(status, complete, errors):
    from task_usage.entitlement_policy import example_policy, load_policy, qualify
    policy = load_policy(example_policy())
    spec = service_module._batch_entitlement_spec(preview_snapshot())
    facts = batch._batch_entitlement_facts(export_result(status, complete=complete, pair_errors=errors))
    decision = qualify(policy, "polymerization_batch", spec, facts, status)
    assert decision["outcome"] == "partial"
    assert decision["units"] == 0 and decision["qualified"] is False


def test_running_core_record_cannot_start_a_native_root_that_never_ran(monkeypatch):
    connection, repo, current, chunks = execution_fixture(monkeypatch)
    _operation, calls = quota(monkeypatch, connection, state="running")
    assert current["status"] == "queued"
    assert repo.claim("worker", "test-engine") is None
    assert chunks[0]["attempt"] == 0
    assert [name for name, _ in calls] == ["get_native"]


@pytest.mark.parametrize("enabled", [False, True])
def test_batch_post_attaches_native_operation_after_final_job_response(monkeypatch, enabled):
    from app.routers import monomer_polymerization_batch as route
    operation = {"operation_id": "00000000-0000-0000-0000-000000000004", "state": "unknown"}
    lookup = Mock(return_value=operation)
    job = {"job_id": JOB, "status": "running"}
    create = Mock(return_value=job)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        polymerization_batch=SimpleNamespace(create_job=create),
        entitlement_repository=SimpleNamespace(get_by_native=lookup))), state=SimpleNamespace())
    body = BatchJobCreate(import_id="b" * 32, preview_revision="c" * 32)
    monkeypatch.setattr(route.ne, "enabled", lambda: enabled)
    monkeypatch.setattr(route, "current_owner_id", lambda: OWNER)
    assert asyncio.run(route.create_job(body, request, "batch-test-key")) is job
    create.assert_called_once_with(body, "batch-test-key")
    if enabled:
        lookup.assert_called_once_with(owner_user_id=OWNER, module="polymerization_batch", native_task_id=JOB)
        assert request.state.usage_operation_id == operation["operation_id"]
        assert request.state.usage_operation_state == "unknown"
    else:
        lookup.assert_not_called()
        assert not hasattr(request.state, "usage_operation_id")


# Real repository/entitlement SQL on the shared disposable PostgreSQL fixture.
# Native work is represented by tiny synthetic manifests; no worker is launched.
from contextlib import contextmanager
from uuid import uuid4
import psycopg
from app.auth.context import Identity, user_context, service_context
from task_usage import entitlements as entitlement_core, entitlement_config
from task_usage import _entitlement_store as entitlement_store
from test_entitlement_repository import entitlement_db, usage_db, connection as pg_connection, policy
from test_usage_repository import B


@pytest.fixture
def batch_pg(entitlement_db, isolated_flags, tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    owner = str(uuid4())
    with psycopg.connect(entitlement_db) as conn:
        conn.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES (%s,%s,'!',false)",
                     (owner, "batch-entitlement-test-" + owner))
    installed = policy(entitlement_db, owner)
    monkeypatch.setattr(entitlement_config, "configured_policy", lambda: installed)
    service = service_module.BatchService(entitlement_db, BatchSettings(enabled=True, storage_root=tmp_path, result_bytes=1024))
    service.repository.connection = lambda: pg_connection(entitlement_db, owner, "nexpoly_service")
    import_id, revision = uuid4().hex, uuid4().hex
    source = tmp_path / "imports" / import_id
    source.mkdir(parents=True)
    snapshot = {"engine": {"fingerprint": "entitlement-pg-test"}, "raw_pairs": 1,
                "valid_pairs": 1, "unique_pairs": 1,
                "tables": {role: {"row_count": 1, "unique_smiles": [smiles],
                                  "rows": [{"canonical_smiles": smiles}]}
                           for role, smiles in (("a", "C"), ("b", "N"))}}
    write_json(source / "preview.json", snapshot)
    files = {}
    for role, smiles in (("a", "C"), ("b", "N")):
        path = source / (role + ".csv")
        path.write_text("SMILES\n" + smiles + "\n")
        files[role] = file_manifest(tmp_path, path, "text/csv")
    repo = service.repository
    repo.create_import(import_id, files, owner_user_id=owner)
    repo.begin_preview(import_id, revision, owner_user_id=owner)
    repo.save_preview(import_id, revision, {"can_submit": True,
        "snapshot": file_manifest(tmp_path, source / "preview.json", "application/json"),
        "statistics": {"raw_pairs": 1, "valid_pairs": 1, "unique_pairs": 1}}, owner_user_id=owner)
    with service_context():
        repo.heartbeat_worker("entitlement-pg-worker", snapshot["engine"], True, "synthetic repository test")
    value = SimpleNamespace(dsn=entitlement_db, owner=owner, policy=installed, repo=repo,
        service=service, root=tmp_path, fingerprint=snapshot["engine"]["fingerprint"],
        request=BatchJobCreate(import_id=import_id, preview_revision=revision))
    try:
        yield value
    finally:
        # Clear only this synthetic principal's native queue, preserving ledger history.
        with pg_connection(entitlement_db, owner, "nexpoly_service") as conn:
            conn.execute("DELETE FROM polymerization_batch.jobs WHERE owner_user_id=%s::uuid", (owner,))
            conn.execute("DELETE FROM polymerization_batch.imports WHERE owner_user_id=%s::uuid", (owner,))


def pg_batch_create(case, key=None):
    with user_context(Identity(case.owner)):
        return case.service.create_job(case.request, key or uuid4().hex)


def pg_batch_state(case, job_id):
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        op = entitlement_core.get_by_native(conn, owner_user_id=case.owner,
            module="polymerization_batch", native_task_id=job_id)
        summary = entitlement_core.summary(conn, owner_user_id=case.owner, policy_version=case.policy["version"])
        native = conn.execute("SELECT * FROM polymerization_batch.jobs WHERE id=%s", (job_id,)).fetchone()
    return op, summary, native


def pg_batch_export(case, *, partial=False):
    # Repository tests assert persistence/qualification, never scientific validity
    # of a workbook. Each artifact descriptor hashes an actual tiny test file.
    summary = {"complete": True, "pair_errors": int(partial)}
    directory = case.root / "synthetic-export"
    directory.mkdir(exist_ok=True)
    write_json(directory / "summary.json", summary)
    artifacts = {"summary.json": file_manifest(case.root, directory / "summary.json", "application/json")}
    for name in ("results.xlsx", "results.zip"):
        path = directory / name
        path.write_bytes(b"synthetic repository fixture; not a scientific report")
        artifacts[name] = file_manifest(case.root, path, "application/octet-stream")
    return {"status": "completed_with_errors" if partial else "completed", "summary": summary, "artifacts": artifacts}


def pg_batch_claim(case):
    with service_context():
        result = case.repo.claim("entitlement-pg-worker", case.fingerprint)
    assert result is not None
    return result


def pg_batch_to_export(case, job_id):
    for _ in range(8):
        job, chunk = pg_batch_claim(case)
        assert job["id"] == job_id
        if chunk["kind"] == "export":
            return job, chunk
        with service_context():
            assert case.repo.finish_unit(job, chunk, {}, 0.001)
        op, summary, _ = pg_batch_state(case, job_id)
        assert op["state"] == "running" and op["generation"] == 2
        assert summary["execution_attempts"] == 1
    raise AssertionError("synthetic batch has unexpectedly many chunks")


@pytest.mark.parametrize("partial", [False, True])
def test_postgres_batch_real_create_replay_chunks_and_final_settlement_once(batch_pg, partial):
    case, key = batch_pg, uuid4().hex
    created = pg_batch_create(case, key)
    assert pg_batch_create(case, key)["job_id"] == created["job_id"]
    op, summary, _ = pg_batch_state(case, created["job_id"])
    assert op["state"] == "reserved" and summary["operation_count"] == 1
    assert op["spec"] == {"raw_pairs": 1, "valid_pairs": 1, "rows_a": 1, "rows_b": 1}
    job, chunk = pg_batch_to_export(case, created["job_id"])
    report = pg_batch_export(case, partial=partial)
    with service_context():
        assert case.repo.finish_unit(job, chunk, {}, 0.001, export=report)
        assert not case.repo.finish_unit(job, chunk, {}, 0.001, export=report)
    op, summary, native = pg_batch_state(case, created["job_id"])
    assert native["status"] == report["status"] and native["artifacts"] == report["artifacts"]
    assert op["state"] == ("released" if partial else "succeeded")
    assert summary["operation_count"] == summary["execution_attempts"] == 1
    assert sum(pool["success_used"] for pool in summary["pools"]) == int(not partial)
    assert sum(pool["reserved"] for pool in summary["pools"]) == 0
    with pg_connection(case.dsn, B) as conn:
        assert entitlement_core.get(conn, owner_user_id=B, operation_id=op["operation_id"]) is None


def test_postgres_batch_create_rollback_has_no_native_or_reservation(batch_pg, monkeypatch):
    case, key = batch_pg, uuid4().hex
    original, calls = case.repo.connection, []
    @contextmanager
    def rollback_admission():
        outer = not calls
        calls.append(True)
        with original() as conn:
            yield conn
            if outer:
                raise RuntimeError("synthetic batch admission rollback")
    monkeypatch.setattr(case.repo, "connection", rollback_admission)
    with pytest.raises(RuntimeError, match="synthetic batch admission rollback"):
        pg_batch_create(case, key)
    monkeypatch.setattr(case.repo, "connection", original)
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        assert conn.execute("SELECT count(*) AS n FROM polymerization_batch.jobs WHERE owner_user_id=%s::uuid", (case.owner,)).fetchone()["n"] == 0
        assert batch.ne.lookup_native(conn, owner_user_id=case.owner, module="polymerization_batch", idempotency_key=key,
            request_fingerprint=service_module.json_hash(case.request.model_dump())) is None
    created = pg_batch_create(case, key)
    job, chunk = pg_batch_to_export(case, created["job_id"])
    with service_context():
        assert case.repo.finish_unit(job, chunk, {}, 0.001, export=pg_batch_export(case))


def test_postgres_batch_export_rollback_preserves_token_and_reservation(batch_pg, monkeypatch):
    case = batch_pg
    created = pg_batch_create(case)
    job, chunk = pg_batch_to_export(case, created["job_id"])
    before = pg_batch_state(case, created["job_id"])[0]
    original = case.repo.connection
    @contextmanager
    def rollback():
        with original() as conn:
            yield conn
            raise RuntimeError("synthetic batch export rollback")
    monkeypatch.setattr(case.repo, "connection", rollback)
    report = pg_batch_export(case)
    with service_context(), pytest.raises(RuntimeError, match="synthetic batch export rollback"):
        case.repo.finish_unit(job, chunk, {}, 0.001, export=report)
    monkeypatch.setattr(case.repo, "connection", original)
    op, _, native = pg_batch_state(case, created["job_id"])
    assert op == before and native["status"] == "running" and native["artifacts"] == {}
    assert native["execution_token"] == job["execution_token"]
    with service_context():
        assert case.repo.finish_unit(job, chunk, {}, 0.001, export=report)
    assert pg_batch_state(case, created["job_id"])[0]["state"] == "succeeded"


def test_postgres_batch_recovery_new_attempt_rejects_old_token_without_new_quota(batch_pg):
    case = batch_pg
    created = pg_batch_create(case)
    old_job, old_chunk = pg_batch_claim(case)
    before = pg_batch_state(case, created["job_id"])[0]
    with service_context():
        with pytest.raises(RuntimeError, match="confirmed"):
            case.repo.recover()
        # No executor was launched; cleanup is vacuously complete. Exercise the
        # real repository continuation while holding synthetic OS and DB locks.
        import fcntl
        with (case.root / "test-worker.lock").open("w") as lock_file, pg_connection(case.dsn, case.owner, "nexpoly_service") as lock_conn:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (batch.WORKER_LOCK,)).fetchone()["acquired"]
            try:
                case.repo.recover(cleanup_confirmed=True)
                job, chunk = pg_batch_claim(case)
                assert chunk["chunk_id"] == old_chunk["chunk_id"] and chunk["attempt"] == old_chunk["attempt"] + 1
                assert job["execution_token"] != old_job["execution_token"]
                assert not case.repo.finish_unit(old_job, old_chunk, {}, 0.001)
                assert case.repo.finish_unit(job, chunk, {}, 0.001)
            finally:
                lock_conn.execute("SELECT pg_advisory_unlock(%s)", (batch.WORKER_LOCK,))
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    assert pg_batch_state(case, created["job_id"])[0] == before
    job, chunk = pg_batch_to_export(case, created["job_id"])
    with service_context():
        assert case.repo.finish_unit(job, chunk, {}, 0.001, export=pg_batch_export(case))
    assert pg_batch_state(case, created["job_id"])[1]["execution_attempts"] == 1


@pytest.mark.parametrize("collection_enabled", [False, True])
def test_postgres_batch_free_first_start_denied_even_collection_off(batch_pg, monkeypatch, collection_enabled):
    case = batch_pg
    if not collection_enabled:
        monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
    created = pg_batch_create(case)
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        entitlement_store.set_owner_tier(conn, owner_user_id=case.owner, tier="free")
    with service_context():
        assert case.repo.claim("entitlement-pg-worker", case.fingerprint) is None
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    op, _, native = pg_batch_state(case, created["job_id"])
    assert native["status"] == "cancelled" and native["start_authorized_at"] is None
    assert op["state"] == "released" if collection_enabled else op is None


def test_postgres_batch_started_root_survives_downgrade_across_chunks(batch_pg, monkeypatch):
    case = batch_pg
    created = pg_batch_create(case)
    job, chunk = pg_batch_claim(case)
    with pg_connection(case.dsn, case.owner, "nexpoly_service") as conn:
        entitlement_store.set_owner_tier(conn, owner_user_id=case.owner, tier="free")
    def unexpected(*args, **kwargs):
        raise AssertionError("continuing an already-authorized root must not read tier")
    monkeypatch.setattr(batch, "authorize_start", unexpected)
    with service_context():
        assert case.repo.finish_unit(job, chunk, {}, 0.001)
    job, chunk = pg_batch_to_export(case, created["job_id"])
    with service_context():
        assert case.repo.finish_unit(job, chunk, {}, 0.001, export=pg_batch_export(case))
    assert pg_batch_state(case, created["job_id"])[0]["state"] == "succeeded"
