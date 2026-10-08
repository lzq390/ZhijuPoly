"""Native lifecycle accounting on disposable PostgreSQL, with synthetic child usage.

No provider request, worker process, scientific calculation or runtime DB is used.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from app.auth.context import Identity, service_context, user_context
from app.services.online_knowledge import postgres_history_repository as online
from app.services.usage_statistics import UsageStatistics
from task_usage.ledger import METRICS, record_event
from test_usage_repository import usage_db, enabled, A, B
from test_usage_native_postgres import batch_repo, create_batch, create_online, transaction


def summary(dsn, module):
    now = datetime.now(timezone.utc)
    with user_context(Identity(A), SimpleNamespace(application_dsn=dsn, service_dsn=dsn)):
        return UsageStatistics(dsn).summary(
            start=now-timedelta(days=1), end=now+timedelta(days=1), module=module)["totals"]


def attempt(conn, task_id, *, owner=A, kind="task"):
    return conn.execute("""SELECT * FROM usage.attempts
        WHERE owner_user_id=%s::uuid AND task_id=%s AND kind=%s""",
        (owner, task_id, kind)).fetchone()


def provider(conn, root_id, *, owner=A, task_id=None, kind="provider", status="running", metrics=None):
    task_id = task_id or "provider:" + uuid4().hex
    now = conn.execute("SELECT now() AS at").fetchone()["at"]
    record_event(conn, owner_user_id=owner, module="online_knowledge", task_id=task_id,
                 parent_task_id=root_id, root_task_id=root_id, kind=kind,
                 event_key="synthetic:" + uuid4().hex, status=status, occurred_at=now,
                 submitted_at=now, started_at=now,
                 finished_at=now if status == "completed" else None, metrics=metrics)
    return task_id


def assert_new_root_wall(before, after, root):
    # Only the root lifecycle duration contributes: never add provider/chunk wall.
    metric = "observed_wall_ms"
    assert root[metric] is not None and root[metric] >= 0
    assert after["tasks"] - before["tasks"] == 1
    assert after["metrics"][metric]["known_count"] - before["metrics"][metric]["known_count"] == 1
    assert (after["metrics"][metric]["value"] or 0) - (before["metrics"][metric]["value"] or 0) == root[metric]
    assert root["execution_ms"] is None


@pytest.mark.parametrize("terminal", ["completed", "cancelled", "timed_out"])
def test_batch_terminal_summary_uses_root_wall_not_child_elapsed(usage_db, terminal):
    module, job_id = "polymerization_batch", uuid4().hex
    before = summary(usage_db, module)
    repo = batch_repo(usage_db)
    with transaction(usage_db) as conn:
        create_batch(repo, conn, job_id, A)
    with service_context():
        job, chunk = repo.claim("synthetic-worker", "synthetic-repository-test")
        assert job["id"] == job_id
        if terminal == "cancelled":
            repo.cancel(job_id, owner_user_id=A)
            repo.release_execution(job, 1.25)
            job, chunk = repo.claim("synthetic-worker", "synthetic-repository-test")
            assert chunk["attempt"] == 2
        if terminal == "timed_out":
            repo.stop_execution(job, elapsed=1.25, code="job_timeout", message="synthetic", finalize=False)
        else:
            assert repo.finish_unit(job, chunk, {}, 1.25,
                export={"status": terminal, "summary": {}, "artifacts": {}})
    with transaction(usage_db) as conn:
        root = attempt(conn, module+":"+job_id)
        children = conn.execute("SELECT * FROM usage.attempts WHERE parent_task_id=%s",
                                (module+":"+job_id,)).fetchall()
    assert root["status"] == terminal
    assert children and all(child["observed_wall_ms"] == 1250 for child in children)
    assert_new_root_wall(before, summary(usage_db, module), root)


def test_online_completion_summary_uses_root_wall_not_provider_wall(usage_db):
    module, job_id = "online_knowledge", uuid4().hex
    root_id = module+":"+job_id
    before = summary(usage_db, module)
    with transaction(usage_db) as conn:
        create_online(conn, job_id, A)
    with transaction(usage_db) as conn:
        online.mark_online_job_running_postgres(conn, job_id, owner_user_id=A)
        provider(conn, root_id, status="completed", metrics={"observed_wall_ms": 9999,
            "provider_input_tokens": 1, "provider_output_tokens": 2, "provider_total_tokens": 3})
    with transaction(usage_db) as conn:
        online.mark_online_job_completed_postgres(conn, job_id, {}, owner_user_id=A)
        root = attempt(conn, root_id)
    assert root["started_at"] is None  # This repository test never authorizes real execution.
    assert_new_root_wall(before, summary(usage_db, module), root)


def test_online_native_restart_recovers_only_matching_unfinished_provider_children(usage_db):
    job_id = uuid4().hex
    root_id = "online_knowledge:"+job_id
    with transaction(usage_db) as conn:
        create_online(conn, job_id, A)
        online.mark_online_job_running_postgres(conn, job_id, owner_user_id=A)
        lost = provider(conn, root_id)
        partial = provider(conn, root_id, metrics={"provider_input_tokens": 7})
        # Same root/task identifiers under B must not be recovered through A.
        provider(conn, root_id, owner=B, task_id=lost)
        other = provider(conn, "online_knowledge:"+uuid4().hex)
        completed = provider(conn, root_id, status="completed", metrics={"provider_total_tokens": 3})
        execution = provider(conn, root_id, kind="execution")
        untouched = [(B, lost, "provider"), (A, other, "provider"),
                     (A, completed, "provider"), (A, execution, "execution")]
        prior = {key: attempt(conn, key[1], owner=key[0], kind=key[2]) for key in untouched}
    with service_context(), transaction(usage_db) as conn:
        assert online.fail_interrupted_online_jobs_postgres(conn) == 1
        root = attempt(conn, root_id)
        assert root["status"] == "interrupted" and root["observed_wall_ms"] is not None
        for task_id in (lost, partial):
            child = attempt(conn, task_id, kind="provider")
            assert child["status"] == "interrupted"
            assert child["finished_at"] == root["finished_at"]
            assert child["execution_ms"] is None and child["observed_wall_ms"] is None
            observations = conn.execute("""SELECT * FROM usage.events WHERE owner_user_id=%s::uuid
                AND task_id=%s AND source='reconciliation'""", (A, task_id)).fetchall()
            assert len(observations) == 1
            assert all(observations[0][metric] is None for metric in METRICS)
        assert attempt(conn, lost, kind="provider")["provider_input_tokens"] is None
        assert attempt(conn, partial, kind="provider")["provider_input_tokens"] == 7
        for key in untouched:
            assert attempt(conn, key[1], owner=key[0], kind=key[2]) == prior[key]
        event_count = conn.execute("SELECT count(*) AS n FROM usage.events").fetchone()["n"]
    with service_context(), transaction(usage_db) as conn:
        assert online.fail_interrupted_online_jobs_postgres(conn) == 0
        assert conn.execute("SELECT count(*) AS n FROM usage.events").fetchone()["n"] == event_count


@contextmanager
def reject_provider_reconciliation(dsn):
    # Fault injection is confined to this fixture's disposable database.
    name = "usage_recovery_fault_"+uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("""CREATE FUNCTION public.{}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.source='reconciliation' AND NEW.kind='provider' THEN
            RAISE EXCEPTION 'synthetic reconciliation fault' USING ERRCODE='23514';
            END IF; RETURN NEW; END $$""").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE TRIGGER {} BEFORE INSERT ON usage.events FOR EACH ROW EXECUTE FUNCTION public.{}()")
                     .format(sql.Identifier(name), sql.Identifier(name)))
    try:
        yield
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP TRIGGER {} ON usage.events").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP FUNCTION public.{}()").format(sql.Identifier(name)))


def test_provider_reconciliation_fault_rolls_back_native_recovery_and_root_ledger(usage_db):
    job_id = uuid4().hex
    root_id = "online_knowledge:"+job_id
    with transaction(usage_db) as conn:
        create_online(conn, job_id, A)
        online.mark_online_job_running_postgres(conn, job_id, owner_user_id=A)
        child_id = provider(conn, root_id)
        root_before = attempt(conn, root_id)
        child_before = attempt(conn, child_id, kind="provider")
    with reject_provider_reconciliation(usage_db), pytest.raises(psycopg.errors.CheckViolation,
                                                               match="synthetic reconciliation fault"):
        with service_context(), transaction(usage_db) as conn:
            online.fail_interrupted_online_jobs_postgres(conn)
    with transaction(usage_db) as conn:
        assert conn.execute("SELECT status FROM online_knowledge.jobs WHERE job_id=%s", (job_id,)).fetchone()["status"] == "running"
        assert attempt(conn, root_id) == root_before
        assert attempt(conn, child_id, kind="provider") == child_before
        assert conn.execute("SELECT count(*) AS n FROM usage.events WHERE task_id=%s AND source='reconciliation'",
                            (child_id,)).fetchone()["n"] == 0
    with service_context(), transaction(usage_db) as conn:
        assert online.fail_interrupted_online_jobs_postgres(conn) == 1
        assert attempt(conn, root_id)["status"] == "interrupted"
        assert attempt(conn, child_id, kind="provider")["status"] == "interrupted"


def test_disabled_online_recovery_keeps_original_single_statement(monkeypatch):
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)
    class Connection:
        rowcount = 1
        def __init__(self):
            self.calls = []
        def execute(self, statement):
            self.calls.append(statement)
            return self
        def fetchall(self):
            raise AssertionError("disabled metering must not read recovered rows or provider children")
    conn = Connection()
    with service_context():
        assert online.fail_interrupted_online_jobs_postgres(conn) == 1
    assert len(conn.calls) == 1 and "RETURNING" not in conn.calls[0]


def test_online_old_null_terminal_observation_can_be_enriched_idempotently(usage_db):
    job_id = uuid4().hex
    root_id = "online_knowledge:"+job_id
    with transaction(usage_db) as conn:
        create_online(conn, job_id, A)
    with transaction(usage_db) as conn:
        # Reproduce the previous version's terminal business row and all-null event.
        row = conn.execute("""UPDATE online_knowledge.jobs SET status='completed',updated_at=now()
            WHERE job_id=%s RETURNING job_id,owner_user_id,status,created_at,updated_at,start_authorized_at""",
            (job_id,)).fetchone()
        record_event(conn, owner_user_id=A, module="online_knowledge", task_id=root_id,
                     root_task_id=root_id, event_key=f"{root_id}:1:completed:{row['updated_at'].isoformat()}",
                     status="completed", occurred_at=row["updated_at"], submitted_at=row["created_at"],
                     started_at=None, finished_at=row["updated_at"], metrics=None)
        assert attempt(conn, root_id)["observed_wall_ms"] is None
        online._record_usage(conn, job_id, owner_user_id=A)
        enriched = attempt(conn, root_id)
        assert enriched["observed_wall_ms"] is not None and enriched["status"] == "completed"
        online._record_usage(conn, job_id, owner_user_id=A)
        assert attempt(conn, root_id) == enriched
