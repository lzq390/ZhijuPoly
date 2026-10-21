"""Lifecycle hooks use only mock connections; no database or worker is started."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
from app.services import monomer_md_repository as md
from app.services import monomer_dft_repository as dft
from workers.monomer_md_worker.app import repository as worker
from workers.monomer_md_worker.app.config import load_settings

OWNER = "00000000-0000-0000-0000-000000000002"
JOB = "00000000-0000-0000-0000-000000000003"
T0 = datetime(2026, 10, 8, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=3)
T2 = T0 + timedelta(seconds=7)


class Result:
    def __init__(self, rows=(), rowcount=None):
        self.rows = list(rows)
        self.rowcount = len(self.rows) if rowcount is None else rowcount

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class Connection:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.exited_with = None
        self.result = Result()

    def execute(self, query, params=None):
        query = query if isinstance(query, str) else query.as_string()
        self.calls.append((query, params))
        self.result = self.handler(query, params)
        return self.result

    def cursor(self):
        return self

    @property
    def rowcount(self):
        return self.result.rowcount

    def fetchone(self):
        return self.result.fetchone()

    def fetchall(self):
        return self.result.fetchall()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited_with = exc_type

    def commit(self):
        raise AssertionError("a hook must not commit the business transaction")

    def rollback(self):
        raise AssertionError("a hook must let the business context roll back")


def md_row(status="running", **values):
    row = dict(job_id=JOB, owner_user_id=OWNER, status=status, created_at=T0,
               started_at=T1, finished_at=T2, error_category=None)
    row.update(values)
    return row


def dft_row(status="running", **values):
    row = dict(job_id=JOB, owner_user_id=OWNER, status=status, created_at=T0,
               started_at=T1, finished_at=T2, error_code=None, attempt=4)
    row.update(values)
    return row


def collect(monkeypatch, module, connection):
    events = []
    def record(same_connection, **event):
        assert same_connection is connection
        events.append(event)
    monkeypatch.setattr(module, "record_event", record)
    monkeypatch.setenv("USAGE_METERING_ENABLED", "1")
    return events


@pytest.mark.parametrize("module", [md, worker])
def test_md_queued_replay_is_immutable_and_contains_no_worker_secrets(monkeypatch, module):
    connection = object()
    events = collect(monkeypatch, module, connection)
    module._record_monomer_md_usage_row(connection, md_row("pending", started_at=None, finished_at=None))
    module._record_monomer_md_usage_row(connection, md_row("submitted", attempt_token="must-not-leak"))
    assert events[0] == events[1]
    assert events[0]["owner_user_id"] == OWNER
    assert events[0]["task_id"] == f"monomer_md:{JOB}"
    assert events[0]["attempt_id"] == "1"
    assert events[0]["occurred_at"] == T0
    assert events[0]["started_at"] is None
    assert events[0]["finished_at"] is None
    assert events[0]["metrics"] == {}
    assert "must-not-leak" not in repr(events)


@pytest.mark.parametrize("module", [md, worker])
def test_md_restart_is_interrupted_and_cancellation_request_is_not_terminal(monkeypatch, module):
    connection = object()
    events = collect(monkeypatch, module, connection)
    module._record_monomer_md_usage_row(connection, md_row("cancel_requested"))
    assert events == []
    module._record_monomer_md_usage_row(connection, md_row("failed", error_category="worker_restarted"))
    assert events[0]["status"] == "interrupted"
    assert events[0]["occurred_at"] == T2
    assert events[0]["metrics"] == {"queue_ms": 3000, "observed_wall_ms": 7000}
    assert "execution_ms" not in events[0]["metrics"]


def test_md_disabled_does_not_change_sql_or_read_rows(monkeypatch):
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)
    class NoRead(Result):
        def fetchone(self):
            raise AssertionError("metering is disabled")
        fetchall = fetchone
    connection = Connection(lambda query, params: NoRead(rowcount=1))
    monkeypatch.setattr(md, "record_event", lambda *a, **k: pytest.fail("disabled metering"))
    md.create_monomer_md_job_postgres(connection, job_id=JOB, owner_user_id=OWNER,
                                    input_smiles="C", canonical_smiles="C", requested_steps=1)
    md.mark_monomer_md_job_failed_postgres(connection, JOB, "failure")
    assert md.mark_expired_unclaimed_monomer_md_jobs_failed_postgres(connection) == 1
    assert len(connection.calls) == 3
    assert all("RETURNING" not in query for query, _ in connection.calls)


def test_md_terminal_cas_only_records_actual_updates_and_write_errors_propagate(monkeypatch):
    connection = Connection(lambda query, params: Result())
    events = collect(monkeypatch, md, connection)
    md.mark_monomer_md_job_failed_postgres(connection, JOB, "failure")
    assert not events
    connection.handler = lambda query, params: Result([md_row("failed")])
    def fail(*args, **kwargs):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(md, "record_event", fail)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        md.mark_monomer_md_job_failed_postgres(connection, JOB, "failure")


def worker_repo(monkeypatch, connection):
    monkeypatch.setattr(worker.psycopg, "connect", lambda *a, **k: connection)
    return worker.PostgresJobRepository(replace(load_settings(), app_postgres_dsn="unused-mock"))


def test_md_worker_cas_and_bulk_recovery_share_business_connection(monkeypatch):
    row = md_row("failed", error_category="worker_restarted")
    connection = Connection(lambda query, params: Result([tuple(row.values())]))
    events = collect(monkeypatch, worker, connection)
    repository = worker_repo(monkeypatch, connection)
    assert repository.reconcile_orphaned_jobs("new-instance") == 1
    assert events[0]["status"] == "interrupted"
    assert "IS DISTINCT FROM" in connection.calls[0][0]
    assert "RETURNING" in connection.calls[0][0]
    connection.calls.clear()
    events.clear()
    connection.handler = lambda query, params: Result([tuple(md_row("running").values())] if query.startswith("SELECT") else [("running",)])
    assert repository.update_status(JOB, "running") == worker.JobUpdateResult.UPDATED
    assert len(connection.calls) == 2
    assert events[0]["occurred_at"] == T1


def test_md_worker_disabled_preserves_one_statement_recovery(monkeypatch):
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)
    connection = Connection(lambda query, params: Result(rowcount=2))
    repository = worker_repo(monkeypatch, connection)
    assert repository.reconcile_orphaned_jobs("new-instance") == 2
    assert len(connection.calls) == 1
    assert "RETURNING" not in connection.calls[0][0]


def dft_repo(connection):
    repository = dft.MonomerDftRepository("unused-mock")
    repository._service_connection = lambda: nullcontext(connection)
    repository._get_job = lambda conn, job_id: {"job_id": job_id}
    return repository


@pytest.mark.parametrize("error,status", [("worker_restarted", "interrupted"), ("calculation_timeout", "timed_out"), ("worker_unavailable", "failed")])
def test_dft_attempt_owner_and_failure_classification(monkeypatch, error, status):
    connection = Connection(lambda query, params: Result([dft_row("failed", error_code=error)]))
    events = collect(monkeypatch, dft, connection)
    dft_repo(connection)._record_usage(connection, JOB)
    assert events[0]["owner_user_id"] == OWNER
    assert events[0]["attempt_id"] == "4"
    assert events[0]["status"] == status
    assert events[0]["event_key"] == f"monomer_dft:{JOB}:4:{status}"
    assert events[0]["metrics"] == {"observed_wall_ms": 7000}
    assert "attempt_token" not in connection.calls[0][0]


def snapshot_fixture(monkeypatch, *, enabled=True, prior_start=None, terminal=False):
    token = "a" * 64
    current = dict(status="cancelled" if terminal else "queued", current_attempt=4,
                   attempt_token=token, request_sha256="b" * 64, enqueue_sequence=2,
                   created_at=T0, artifacts_delete_requested_at=None, artifacts_deleted_at=None)
    def handler(query, params):
        if "FOR UPDATE" in query:
            return Result([current])
        if query.startswith("SELECT started_at"):
            return Result([{"started_at": prior_start}])
        if "SELECT j.job_id" in query:
            # Existing business SQL fabricates this start on a first terminal snapshot.
            return Result([dft_row("cancelled", started_at=T2)])
        if "UPDATE monomer_dft.jobs" in query:
            return Result([{"job_id": JOB}])
        return Result(rowcount=1)
    connection = Connection(handler)
    events = collect(monkeypatch, dft, connection)
    if not enabled:
        monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)
    snapshot = dict(job_id=JOB, attempt_token=token, request_sha256="b" * 64,
                    enqueue_sequence=2, status="cancelled", stage="queued", progress_percent=0)
    return connection, events, dft_repo(connection), token, snapshot


@pytest.mark.parametrize("prior_start", [None, T1])
def test_dft_terminal_snapshot_never_invents_execution_start(monkeypatch, prior_start):
    connection, events, repository, token, snapshot = snapshot_fixture(monkeypatch, prior_start=prior_start)
    repository.apply_worker_snapshot(job_id=JOB, attempt_token=token, snapshot=snapshot)
    assert events[0]["started_at"] == prior_start
    assert events[0]["finished_at"] == T2
    assert events[0]["status"] == "cancelled"
    assert events[0]["metrics"] == {"observed_wall_ms": 7000}
    assert token not in repr(events)


def test_dft_terminal_replay_and_stale_snapshot_add_no_observation(monkeypatch):
    connection, events, repository, token, snapshot = snapshot_fixture(monkeypatch, terminal=True)
    repository.apply_worker_snapshot(job_id=JOB, attempt_token=token, snapshot=snapshot)
    assert not events and len(connection.calls) == 1
    snapshot["request_sha256"] = "c" * 64
    with pytest.raises(dft.MonomerDftStaleAttempt):
        repository.apply_worker_snapshot(job_id=JOB, attempt_token=token, snapshot=snapshot)
    assert not events


def test_dft_disabled_snapshot_has_original_three_statements(monkeypatch):
    connection, events, repository, token, snapshot = snapshot_fixture(monkeypatch, enabled=False)
    repository.apply_worker_snapshot(job_id=JOB, attempt_token=token, snapshot=snapshot)
    assert len(connection.calls) == 3
    assert not events


def test_dft_retryable_or_stale_dispatch_does_not_create_an_attempt_event(monkeypatch):
    connection = Connection(lambda query, params: Result(rowcount=0))
    events = collect(monkeypatch, dft, connection)
    repository = dft_repo(connection)
    for retryable in (True, False):
        repository.record_dispatch_error(job_id=JOB, attempt_token="a" * 64,
                                         code="worker_unavailable", message="unavailable", retryable=retryable)
    assert not events
    assert all("SELECT j.job_id" not in query for query, _ in connection.calls)


def test_dft_enabled_ledger_error_escapes_original_transaction(monkeypatch):
    connection, events, repository, token, snapshot = snapshot_fixture(monkeypatch)
    repository._service_connection = lambda: connection
    def fail(*args, **kwargs):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(dft, "record_event", fail)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        repository.apply_worker_snapshot(job_id=JOB, attempt_token=token, snapshot=snapshot)
    assert connection.exited_with is RuntimeError


def test_dft_creation_records_once_and_idempotent_replay_is_silent(monkeypatch):
    from types import SimpleNamespace
    existing = False
    def handler(query, params):
        if "SELECT status, is_system FROM auth.users" in query:
            return Result([{"status": "active", "is_system": False}])
        if "SELECT job_id, request_sha256" in query:
            return Result([{"job_id": JOB, "request_sha256": "b" * 64}] if existing else [])
        if "SELECT count(*)" in query:
            return Result([{"count": 0}])
        if "SELECT j.job_id" in query:
            return Result([dft_row("pending", started_at=None, finished_at=None)])
        return Result(rowcount=1)
    connection = Connection(handler)
    events = collect(monkeypatch, dft, connection)
    repository = dft_repo(connection)
    prepared = SimpleNamespace(
        request_sha256="b" * 64, warnings=[], canonical_smiles="C", effective_charge=0,
        public_request={"calculation_type": "single_point", "model": "aimnet2", "input": {"smiles": "C", "multiplicity": 1}},
    )
    assert repository.create_job(prepared, owner_user_id=OWNER, idempotency_key="test-only-id", max_active_jobs=3).created
    assert len(events) == 1 and events[0]["status"] == "queued"
    assert events[0]["started_at"] is None
    existing = True
    assert not repository.create_job(prepared, owner_user_id=OWNER, idempotency_key="test-only-id", max_active_jobs=3).created
    assert len(events) == 1


@pytest.mark.parametrize("dispatched", [False, True])
def test_dft_cancel_only_records_proven_undispatched_terminal(monkeypatch, dispatched):
    def handler(query, params):
        if "FOR UPDATE" in query:
            return Result([{"status": "pending", "submitted_at": T1 if dispatched else None}])
        if "SELECT j.job_id" in query:
            return Result([dft_row("cancelled", started_at=None)])
        return Result(rowcount=1)
    connection = Connection(handler)
    events = collect(monkeypatch, dft, connection)
    repository = dft_repo(connection)
    repository._connection_factory = lambda dsn: nullcontext(connection)
    repository.request_cancel(JOB, owner_user_id=OWNER)
    if dispatched:
        assert not events
    else:
        assert events[0]["status"] == "cancelled"
        assert events[0]["started_at"] is None


def test_md_worker_ledger_failure_reaches_connection_context(monkeypatch):
    row = tuple(md_row("running").values())
    connection = Connection(lambda query, params: Result([row] if query.startswith("SELECT") else [("running",)]))
    collect(monkeypatch, worker, connection)
    repository = worker_repo(monkeypatch, connection)
    def fail(*args, **kwargs):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(worker, "record_event", fail)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        repository.update_status(JOB, "running")
    assert connection.exited_with is RuntimeError
