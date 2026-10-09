"""Native MD transaction/race tests in disposable PG, with no scientific executor."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from task_usage import entitlements as core, native_entitlements as ne, entitlement_config
from app.services import monomer_md_repository as md
from workers.monomer_md_worker.app.config import load_settings
from workers.monomer_md_worker.app.repository import PostgresJobRepository, JobUpdateResult, _md_completion_facts
from test_entitlement_repository import entitlement_db, usage_db, connection, policy
from test_usage_repository import A, B


@pytest.fixture(autouse=True)
def entitlement_flags(monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)


@pytest.fixture
def configured(entitlement_db, monkeypatch):
    # Each case owns a distinct synthetic principal. Native operations purposely
    # survive deletion and must not consume another test's concurrent budget.
    owner_id = str(uuid4())
    with psycopg.connect(entitlement_db) as conn:
        conn.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES (%s,%s,'!',false)",
                     (owner_id, "native-test-" + owner_id))
    monkeypatch.setitem(globals(), "A", owner_id)
    value = policy(entitlement_db, owner_id)
    monkeypatch.setattr(entitlement_config, "configured_policy", lambda: value)
    return value


def create_native(dsn, *, owner=None, job_id=None):
    owner = owner or A
    job_id = job_id or uuid4().hex
    spec = {"run_mode": "formal", "protocol": "Density", "requested_steps": 1000, "requested_atom_count": 100}
    with connection(dsn, owner, "nexpoly_service") as conn:
        reserved = ne.reserve_native(conn, owner_user_id=owner, module="monomer_md", native_task_id=job_id,
            idempotency_key=uuid4().hex, request_fingerprint="a" * 64, spec=spec)
        md.create_monomer_md_job_postgres(conn, job_id=job_id, owner_user_id=owner,
            input_smiles="O", canonical_smiles="O", requested_steps=1000,
            run_mode="formal", protocol="Density", config_json={"natoms": 100})
        assert ne.begin_native_dispatch(conn, owner_user_id=owner, module="monomer_md", native_task_id=job_id,
            executor_id="monomer_md:" + job_id)["allowed"]
    return job_id, reserved["operation"]["operation_id"]


def worker(dsn):
    return PostgresJobRepository(replace(load_settings(), app_postgres_dsn=
        make_conninfo(dsn, options="-c role=nexpoly_service")))


def state(dsn, job_id, owner=None):
    owner = owner or A
    with connection(dsn, owner, "nexpoly_service") as conn:
        native = conn.execute("SELECT status,result_data,start_authorized_at,worker_instance_id FROM md.monomer_md_jobs WHERE job_id=%s", (job_id,)).fetchone()
        operation = ne.get_native(conn, owner_user_id=owner, module="monomer_md", native_task_id=job_id)
        return native, operation


def report(job_id):
    return {"job_id": job_id, "run_mode": "formal", "protocol": "Density",
            "metrics": {"synthetic_test": 1}, "summary": {}, "result_file": "result.json"}


def begin_running(dsn, job_id):
    repo = worker(dsn)
    assert repo.accept_job(job_id, "synthetic-worker", queued=False) == JobUpdateResult.UPDATED
    assert repo.authorize_start(job_id, worker_instance_id="synthetic-worker")
    assert repo.update_status(job_id, "running", worker_instance_id="synthetic-worker") == JobUpdateResult.UPDATED
    return repo


def test_result_facts_do_not_invent_actual_steps_or_physical_equilibration():
    row = {"job_id": "one", "status": "completed", "protocol": "Density", "result_data": report("one"), "completed_steps": 999999}
    assert all(_md_completion_facts(row).values())
    assert "requested_work_completed" not in _md_completion_facts(row)
    row["result_data"] = {"job_id": "other", "run_mode": "formal", "protocol": "Density", "metrics": {"x": 1}}
    assert not _md_completion_facts(row)["validated"]
    assert not _md_completion_facts(row)["protocol_completed"]


def test_failed_unclaimed_submission_fences_late_accept_and_releases(entitlement_db, configured):
    job_id, _ = create_native(entitlement_db)
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        assert md.mark_unclaimed_monomer_md_submit_failed_postgres(conn, job_id, "synthetic transport failure")
    repo = worker(entitlement_db)
    assert repo.accept_job(job_id, "late-worker", queued=False) == JobUpdateResult.ALREADY_TERMINAL
    assert not repo.authorize_start(job_id, worker_instance_id="late-worker")
    native, operation = state(entitlement_db, job_id)
    assert native["status"] == "failed" and native["start_authorized_at"] is None
    assert operation["state"] == "released"


def test_ack_lost_after_worker_claim_keeps_native_and_unknown_reservation(entitlement_db, configured):
    job_id, _ = create_native(entitlement_db)
    repo = worker(entitlement_db)
    assert repo.accept_job(job_id, "accepted-worker", queued=False) == JobUpdateResult.UPDATED
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        assert not md.mark_unclaimed_monomer_md_submit_failed_postgres(conn, job_id, "synthetic ACK loss")
    native, operation = state(entitlement_db, job_id)
    assert native["status"] == "submitted" and native["worker_instance_id"] == "accepted-worker"
    assert operation["state"] == "unknown"
    assert not repo.authorize_start(job_id, worker_instance_id="accepted-worker")
    # Only the original worker's proven terminal callback can release it later.
    assert repo.update_status(job_id, "cancelled", worker_instance_id="accepted-worker") == JobUpdateResult.UPDATED
    assert state(entitlement_db, job_id)[1]["state"] == "released"


def test_submit_error_and_accept_race_has_only_one_winner(entitlement_db, configured):
    job_id, _ = create_native(entitlement_db)
    barrier = Barrier(2)
    def fail():
        barrier.wait(timeout=5)
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            return md.mark_unclaimed_monomer_md_submit_failed_postgres(conn, job_id, "synthetic race")
    def accept():
        barrier.wait(timeout=5)
        return worker(entitlement_db).accept_job(job_id, "racing-worker", queued=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f, a = pool.submit(fail), pool.submit(accept)
        failed, accepted = f.result(timeout=15), a.result(timeout=15)
    native, operation = state(entitlement_db, job_id)
    if failed:
        assert accepted == JobUpdateResult.ALREADY_TERMINAL
        assert native["status"] == "failed" and operation["state"] == "released"
    else:
        assert accepted == JobUpdateResult.UPDATED
        assert native["status"] == "submitted" and operation["state"] == "unknown"


def test_original_authorization_ack_replay_does_not_start_second_execution(entitlement_db, configured):
    job_id, _ = create_native(entitlement_db)
    repo = begin_running(entitlement_db, job_id)
    before = state(entitlement_db, job_id)[1]
    assert repo.authorize_start(job_id, worker_instance_id="synthetic-worker")
    assert not repo.authorize_start(job_id, worker_instance_id="different-worker")
    after = state(entitlement_db, job_id)[1]
    assert after == before and after["state"] == "running"
    # No worker run() or native execution is invoked by these repository tests.


def test_completed_result_and_settlement_commit_once_and_survive_native_delete(entitlement_db, configured):
    job_id, op_id = create_native(entitlement_db)
    repo = begin_running(entitlement_db, job_id)
    assert repo.update_status(job_id, "completed", result=report(job_id), worker_instance_id="synthetic-worker") == JobUpdateResult.UPDATED
    native, op = state(entitlement_db, job_id)
    assert native["result_data"] == report(job_id) and op["state"] == "succeeded"
    assert repo.update_status(job_id, "cancelled", worker_instance_id="synthetic-worker") == JobUpdateResult.ALREADY_TERMINAL
    assert state(entitlement_db, job_id)[1] == op
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        conn.execute("DELETE FROM md.monomer_md_jobs WHERE job_id=%s", (job_id,))
        assert core.get(conn, owner_user_id=A, operation_id=op_id)["state"] == "succeeded"
    with connection(entitlement_db, B) as conn:
        assert core.get(conn, owner_user_id=B, operation_id=op_id) is None


def test_settlement_fault_rolls_back_actual_native_result(entitlement_db, configured, monkeypatch):
    job_id, _ = create_native(entitlement_db)
    repo = begin_running(entitlement_db, job_id)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic settlement fault")
    monkeypatch.setattr(core, "settle", fail)
    with pytest.raises(RuntimeError, match="synthetic settlement fault"):
        repo.update_status(job_id, "completed", result=report(job_id), worker_instance_id="synthetic-worker")
    native, op = state(entitlement_db, job_id)
    assert native["status"] == "running" and native["result_data"] is None
    assert op["state"] == "running"


def test_missing_report_holds_unknown_and_legacy_job_is_not_back_charged(entitlement_db, configured):
    job_id, _ = create_native(entitlement_db)
    repo = begin_running(entitlement_db, job_id)
    assert repo.update_status(job_id, "completed", worker_instance_id="synthetic-worker") == JobUpdateResult.UPDATED
    assert state(entitlement_db, job_id)[1]["state"] == "unknown"
    legacy_id = uuid4().hex
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        md.create_monomer_md_job_postgres(conn, job_id=legacy_id, owner_user_id=A,
            input_smiles="O", canonical_smiles="O", requested_steps=300)
    legacy_worker = begin_running(entitlement_db, legacy_id)
    assert legacy_worker.update_status(legacy_id, "completed", worker_instance_id="synthetic-worker") == JobUpdateResult.UPDATED
    assert state(entitlement_db, legacy_id)[1] is None


def test_http_admission_binds_same_transaction_and_replays_before_capacity(entitlement_db, configured, monkeypatch):
    from types import SimpleNamespace
    from app.routers import monomer_md as route
    monkeypatch.setattr(route, "current_owner_id", lambda: A)
    monkeypatch.setattr(route, "postgres_connection", lambda _dsn: connection(entitlement_db, A, "nexpoly_service"))
    monkeypatch.setattr(route, "reconcile_and_get_active_monomer_md_capacity_postgres", lambda *a, **k: (0, None))
    monkeypatch.setattr(route, "count_user_active_monomer_md_jobs_postgres", lambda *a, **k: 0)
    monkeypatch.setattr(route, "get_monomer_md_mode_capacity_postgres", lambda *a, **k: {"demo_active": 0, "formal_running": 0, "formal_queued": 0})
    settings = SimpleNamespace(app_postgres_dsn="unused", monomer_md_max_active_jobs=10)
    job_id, key = uuid4().hex, uuid4().hex
    arguments = dict(job_id=job_id, input_smiles="O", canonical_smiles="O", requested_steps=1000,
        protocol="Density", run_mode="formal", config_json={"natoms": 100},
        idempotency_key=key, request_fingerprint="c" * 64)
    created = route._create_pending_job_with_capacity_guard(settings, **arguments)
    assert created["created"] and created["job"]["job_id"] == job_id
    native, operation = state(entitlement_db, job_id)
    assert native["status"] == "pending" and operation["state"] == "dispatching"
    assert operation["spec"]["requested_atom_count"] == 100
    def forbidden(*args, **kwargs):
        raise AssertionError("replay must precede capacity/policy")
    monkeypatch.setattr(route, "reconcile_and_get_active_monomer_md_capacity_postgres", forbidden)
    monkeypatch.setattr(entitlement_config, "configured_policy", forbidden)
    replay = route._create_pending_job_with_capacity_guard(settings, **{**arguments, "job_id": uuid4().hex})
    assert not replay["created"] and replay["job"]["job_id"] == job_id
    assert state(entitlement_db, job_id)[1] == operation


def test_http_admission_native_insert_failure_rolls_back_reservation(entitlement_db, configured, monkeypatch):
    from types import SimpleNamespace
    from app.routers import monomer_md as route
    monkeypatch.setattr(route, "current_owner_id", lambda: A)
    monkeypatch.setattr(route, "postgres_connection", lambda _dsn: connection(entitlement_db, A, "nexpoly_service"))
    monkeypatch.setattr(route, "reconcile_and_get_active_monomer_md_capacity_postgres", lambda *a, **k: (0, None))
    monkeypatch.setattr(route, "count_user_active_monomer_md_jobs_postgres", lambda *a, **k: 0)
    monkeypatch.setattr(route, "get_monomer_md_mode_capacity_postgres", lambda *a, **k: {"demo_active": 0, "formal_running": 0, "formal_queued": 0})
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic native insert failure")
    monkeypatch.setattr(route, "create_monomer_md_job_postgres", fail)
    job_id = uuid4().hex
    with pytest.raises(RuntimeError, match="synthetic native insert failure"):
        route._create_pending_job_with_capacity_guard(SimpleNamespace(app_postgres_dsn="unused", monomer_md_max_active_jobs=10),
            job_id=job_id, input_smiles="O", canonical_smiles="O", requested_steps=1000,
            protocol="Density", run_mode="formal", config_json={"natoms": 100},
            idempotency_key=uuid4().hex, request_fingerprint="d" * 64)
    assert state(entitlement_db, job_id) == (None, None)


@pytest.mark.parametrize("collection_enabled", [True, False])
def test_queued_owner_downgrade_blocks_first_start_even_collection_off(entitlement_db, configured, monkeypatch, collection_enabled):
    from task_usage import _entitlement_store as store
    job_id, _ = create_native(entitlement_db)
    repo = worker(entitlement_db)
    assert repo.accept_job(job_id, "downgrade-worker", queued=False) == JobUpdateResult.UPDATED
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        store.set_owner_tier(conn, owner_user_id=A, tier="free")
    try:
        if not collection_enabled:
            monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
        assert not repo.authorize_start(job_id, worker_instance_id="downgrade-worker")
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            row = conn.execute("SELECT status,start_authorized_at FROM md.monomer_md_jobs WHERE job_id=%s", (job_id,)).fetchone()
            assert row["status"] == "submitted" and row["start_authorized_at"] is None
    finally:
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            store.set_owner_tier(conn, owner_user_id=A, tier="member")


def test_running_native_authorization_survives_tier_change_without_new_claim(entitlement_db, configured):
    from task_usage import _entitlement_store as store
    job_id, _ = create_native(entitlement_db)
    repo = begin_running(entitlement_db, job_id)
    before = state(entitlement_db, job_id)[1]
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        store.set_owner_tier(conn, owner_user_id=A, tier="free")
    try:
        assert repo.authorize_start(job_id, worker_instance_id="synthetic-worker")
        assert not repo.authorize_start(job_id, worker_instance_id="different-worker")
        assert state(entitlement_db, job_id)[1] == before
    finally:
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            store.set_owner_tier(conn, owner_user_id=A, tier="member")
