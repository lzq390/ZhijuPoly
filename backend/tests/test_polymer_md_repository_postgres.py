"""Real RLS/concurrency tests; require an explicitly supplied disposable cluster."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import datetime,timedelta,timezone
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.context import Identity, current_owner_id, is_service_context, user_context
from test_auth_isolation import auth_database
from app.postgres_migrations import apply_postgres_migrations
from app.services.polymer_md_schema import apply_polymer_md_dev_migration, validate_polymer_md_schema
from app.services.polymer_md_repository import PolymerMdError, PolymerMdRepository, public_job, submit_request
from polymer_md.contracts import ScientificRequest, StartAuthorizationRequest, WorkerSnapshot

MIGRATION=Path(__file__).parents[1]/"migrations/postgres/0023_polymer_md_jobs.sql"


@pytest.fixture(scope="module")
def polymer_database(auth_database):
    # Reuse the complete, disposable identity fixture; retain real main20-22.
    apply_postgres_migrations(auth_database["admin"], allowed_kinds={"expand"})
    apply_polymer_md_dev_migration(auth_database["admin"], environment="dev")
    return auth_database


@pytest.fixture
def actors(polymer_database):
    with psycopg.connect(polymer_database["admin"],row_factory=dict_row) as c:
        c.execute("TRUNCATE polymer_md.jobs CASCADE")
        ids=[str(uuid4()) for _ in range(4)]
        for value in ids:
            c.execute("INSERT INTO auth.users(user_id,username,password_hash,status,must_change_password) VALUES(%s,%s,'!','active',false)",(value,"polymer-test-"+value))
    @contextmanager
    def connection(_dsn):
        dsn=polymer_database["service"] if is_service_context() else polymer_database["api"]
        with psycopg.connect(dsn,row_factory=dict_row) as c:
            owner=current_owner_id(required=False)
            if owner and not is_service_context():
                c.execute("SELECT set_config('app.user_id',%s,true)",(owner,))
            yield c
    return ids,PolymerMdRepository(polymer_database["api"],connection_factory=connection),connection


def create(repo,owner,key="polymer-idempotent",**kwargs):
    with user_context(Identity(owner)):
        return repo.create_job(ScientificRequest(smiles="*CC*",**kwargs),owner_user_id=owner,idempotency_key=key)


def test_rls_owner_and_child_isolation_and_effective_seed_idempotency(actors):
    ids,repo,connection=actors
    assert repo.schema_ready()
    job,new=create(repo,ids[0]);again,replayed=create(repo,ids[0])
    assert new and not replayed and again["job_id"]==job["job_id"]
    assert isinstance(job["scientific_request"]["seed"],int)
    assert again["scientific_request"]["seed"]==job["scientific_request"]["seed"]
    assert "attempt_token" not in public_job(job)
    with pytest.raises(PolymerMdError,match="幂等"):
        create(repo,ids[0],temp=350)
    with user_context(Identity(ids[1])):
        assert repo.list_jobs(owner_user_id=ids[1])["total"]==0
        with pytest.raises(PolymerMdError) as exc:
            repo.get_job(job["job_id"],owner_user_id=ids[1])
        assert exc.value.status==404
        with connection("") as c:
            assert c.execute("SELECT count(*) AS n FROM polymer_md.jobs").fetchone()["n"]==0
            for table in ("job_attempts","stage_runs","artifacts"):
                assert c.execute(sql.SQL("SELECT count(*) AS n FROM polymer_md.{}").format(sql.Identifier(table))).fetchone()["n"]==0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("DELETE FROM polymer_md.jobs")


def test_concurrent_owner_limit_and_global_capacity(actors):
    ids,repo,_=actors
    def attempt(i):
        try:
            create(repo,ids[0],"polymer-concurrent-"+str(i))
            return True
        except PolymerMdError as exc:
            assert exc.code=="capacity_full"
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt,range(4)))==1
    create(repo,ids[1]);create(repo,ids[2])
    with pytest.raises(PolymerMdError) as exc:
        create(repo,ids[3])
    assert exc.value.code=="capacity_full"


def test_start_fencing_revision_partial_artifacts_cancel_and_cleanup(actors):
    ids,repo,_=actors
    job,_=create(repo,ids[0]);assert repo.claim_dispatch(job["job_id"],job["attempt_token"])
    submit=submit_request(job)
    auth=StartAuthorizationRequest(**submit.model_dump(),worker_instance_id="b"*32)
    assert repo.authorize_start(auth) is not None
    stage={"stage_run_id":"qm-1","stage":"qm","status":"completed","quality_status":"passed","results":{"x":1}}
    artifact={"artifact_id":"qm-json","stage_run_id":"qm-1","kind":"json","filename":"qm.json","media_type":"application/json","size_bytes":2,"sha256":"0"*64}
    envelope={k:getattr(submit,k) for k in ("job_id","attempt_token","request_sha256","enqueue_sequence")}
    snapshot=WorkerSnapshot(**envelope,worker_instance_id="b"*32,status="running",revision=2,stage_runs=[stage],artifacts=[artifact])
    job=repo.apply_snapshot(snapshot)
    assert len(job["artifacts"])==1 and job["status"]=="running"
    stale=snapshot.model_copy(update={"revision":1,"stage":"queued","status":"queued","stage_runs":[],"artifacts":[]})
    assert repo.apply_snapshot(stale)["snapshot_revision"]==2
    changed=snapshot.model_copy(update={"revision":3,"worker_instance_id":"c"*32})
    with pytest.raises(PolymerMdError) as exc:
        repo.apply_snapshot(changed)
    assert exc.value.code=="worker_instance_conflict"
    cancelled=repo.request_cancel(job["job_id"],owner_user_id=ids[0])
    assert cancelled["status"]=="cancel_requested"
    repo.record_error(job["job_id"],job["attempt_token"],"worker_unavailable","unknown")
    with pytest.raises(PolymerMdError):
        create(repo,ids[0],"polymer-next-request")
    terminal=snapshot.model_copy(update={"revision":4,"status":"cancelled","cleanup_confirmed":True})
    done=repo.apply_snapshot(terminal)
    assert done["status"]=="cancelled" and len(done["artifacts"])==1
    create(repo,ids[0],"polymer-next-request")
    repo.request_deletion(job["job_id"],owner_user_id=ids[0])
    repo.complete_deletion(job["job_id"],job["attempt_token"])
    with user_context(Identity(ids[0])):
        assert repo.get_job(job["job_id"],owner_user_id=ids[0])["artifacts"]==[]


def test_recovery_rebind_requires_exact_old_instance_and_confirmed_cleanup(actors):
    ids,repo,_=actors
    job,_=create(repo,ids[0]);repo.claim_dispatch(job["job_id"],job["attempt_token"])
    request=submit_request(job)
    original=StartAuthorizationRequest(**request.model_dump(),worker_instance_id="a"*32)
    first=repo.authorize_start(original)
    with pytest.raises(PolymerMdError):
        repo.authorize_start(original.model_copy(update={"worker_instance_id":"b"*32}))
    with pytest.raises(PolymerMdError):
        repo.authorize_start(original.model_copy(update={"worker_instance_id":"b"*32,"replaces_worker_instance_id":"c"*32,"prior_execution_cleanup_confirmed":True}))
    replacement=original.model_copy(update={"worker_instance_id":"b"*32,"replaces_worker_instance_id":"a"*32,"prior_execution_cleanup_confirmed":True})
    assert repo.authorize_start(replacement)==first
    assert repo.authorize_start(replacement)==first
    with pytest.raises(PolymerMdError):
        repo.authorize_start(original)


def test_two_workers_concurrent_authorization_obeys_global_fifo_and_cleanup_fence(actors):
    ids,repo,_=actors
    jobs=[create(repo,ids[index])[0] for index in (0,1)]
    requests=[]
    for index,job in enumerate(jobs):
        repo.claim_dispatch(job['job_id'],job['attempt_token'])
        requests.append(StartAuthorizationRequest(**submit_request(job).model_dump(),worker_instance_id=('a' if index==0 else 'b')*32))
    barrier=Barrier(2)
    def authorize(index):
        barrier.wait(timeout=5)
        try:
            return repo.authorize_start(requests[index])
        except PolymerMdError as error:
            assert error.code in {'execution_slot_busy','fifo_predecessor_pending'}
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        grants=list(pool.map(authorize,(0,1)))
    assert grants[0] is not None and grants[1] is None
    assert repo.get_service_job(jobs[1]['job_id'])['start_authorized_at'] is None
    assert repo.authorize_start(requests[0])==grants[0]
    replacement=requests[0].model_copy(update={'worker_instance_id':'c'*32,'replaces_worker_instance_id':'a'*32,'prior_execution_cleanup_confirmed':True})
    assert repo.authorize_start(replacement)==grants[0]
    with pytest.raises(PolymerMdError) as stale:
        repo.authorize_start(requests[0])
    assert stale.value.code=='worker_instance_conflict'
    envelope={key:getattr(requests[0],key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')}
    repo.apply_snapshot(WorkerSnapshot(**envelope,worker_instance_id='c'*32,status='running',revision=1))
    repo.request_cancel(jobs[0]['job_id'],owner_user_id=ids[0])
    repo.record_error(jobs[0]['job_id'],jobs[0]['attempt_token'],'worker_unavailable','cleanup response unknown')
    with pytest.raises(PolymerMdError) as held:
        repo.authorize_start(requests[1])
    assert held.value.code=='execution_slot_busy'
    with pytest.raises(PolymerMdError) as old_snapshot:
        repo.apply_snapshot(WorkerSnapshot(**envelope,worker_instance_id='a'*32,status='cancelled',cleanup_confirmed=True,revision=2))
    assert old_snapshot.value.code=='worker_instance_conflict'
    repo.apply_snapshot(WorkerSnapshot(**envelope,worker_instance_id='c'*32,status='cancel_requested',revision=2))
    with pytest.raises(PolymerMdError):
        repo.authorize_start(requests[1])
    repo.apply_snapshot(WorkerSnapshot(**envelope,worker_instance_id='c'*32,status='cancelled',cleanup_confirmed=True,revision=3))
    assert repo.authorize_start(requests[1]) is not None
    with repo.service_connection() as connection:
        assert connection.execute("SELECT count(*) AS n FROM polymer_md.jobs WHERE start_authorized_at IS NOT NULL AND status NOT IN ('completed','failed','cancelled')").fetchone()['n']==1


def test_fifo_holds_later_job_behind_undispatched_and_unconfirmed_cancel(actors):
    ids,repo,_=actors
    first,_=create(repo,ids[0]);second,_=create(repo,ids[1])
    repo.claim_dispatch(second['job_id'],second['attempt_token'])
    later=StartAuthorizationRequest(**submit_request(second).model_dump(),worker_instance_id='b'*32)
    for cancel in (False,True):
        if cancel:
            repo.request_cancel(first['job_id'],owner_user_id=ids[0])
        with pytest.raises(PolymerMdError) as blocked:
            repo.authorize_start(later)
        assert blocked.value.code=='fifo_predecessor_pending'
    request=submit_request(first)
    repo.apply_snapshot(WorkerSnapshot(**{key:getattr(request,key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')},worker_instance_id='a'*32,status='cancelled',cleanup_confirmed=True))
    assert repo.authorize_start(later) is not None


def test_start_replay_and_rebind_still_check_another_execution_holder(actors,polymer_database):
    ids,repo,_=actors
    first,_=create(repo,ids[0]);second,_=create(repo,ids[1])
    for job in (first,second):repo.claim_dispatch(job['job_id'],job['attempt_token'])
    request=StartAuthorizationRequest(**submit_request(first).model_dump(),worker_instance_id='a'*32)
    granted=repo.authorize_start(request)
    # Emulate a pre-guard deployment which already persisted another grant;
    # even a current holder's replay/CAS must fail closed in this state.
    with psycopg.connect(polymer_database['admin']) as connection:
        connection.execute("UPDATE polymer_md.jobs SET start_authorized_at=now(),worker_instance_id=%s WHERE job_id=%s",('b'*32,second['job_id']))
    replacement=request.model_copy(update={'worker_instance_id':'c'*32,'replaces_worker_instance_id':'a'*32,'prior_execution_cleanup_confirmed':True})
    for candidate in (request,replacement):
        with pytest.raises(PolymerMdError) as conflict:
            repo.authorize_start(candidate)
        assert conflict.value.code=='execution_slot_busy'
    assert repo.get_service_job(first['job_id'])['worker_instance_id']=='a'*32
    other=submit_request(second)
    repo.apply_snapshot(WorkerSnapshot(**{key:getattr(other,key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')},worker_instance_id='b'*32,status='cancelled',cleanup_confirmed=True))
    assert repo.authorize_start(replacement)==granted
    with pytest.raises(PolymerMdError) as stale:
        repo.authorize_start(request)
    assert stale.value.code=='worker_instance_conflict'


def test_disabled_owner_before_start_retains_pending_cleanup_quota(actors,polymer_database):
    ids,repo,_=actors
    job,_=create(repo,ids[0]);repo.claim_dispatch(job["job_id"],job["attempt_token"])
    with psycopg.connect(polymer_database["admin"]) as c:
        c.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s",(ids[0],))
    assert repo.authorize_start(StartAuthorizationRequest(**submit_request(job).model_dump(),worker_instance_id="a"*32)) is None
    current=repo.get_service_job(job["job_id"])
    assert current["status"]=="cancel_requested" and current["start_authorized_at"] is None
    assert not current["cleanup_confirmed"]


def test_unknown_dispatch_holds_quota_blocks_later_fifo_and_repairs_same_attempt(actors):
    from app.services.polymer_md_reconciler import PolymerMdReconciler
    ids,repo,_=actors
    first,_=create(repo,ids[0]);second,_=create(repo,ids[1])
    class UncertainWorker:
        def __init__(self):
            self.requests=[]
            self.uncertain=True
        async def snapshot(self,request,**kwargs):
            self.requests.append(request)
            if self.uncertain:
                raise PolymerMdError('worker_unavailable','response lost',503)
            return WorkerSnapshot(**{k:getattr(request,k) for k in ('job_id','attempt_token','request_sha256','enqueue_sequence')},worker_instance_id='a'*32,status='queued',revision=1)
    worker=UncertainWorker()
    async def reconcile():
        reconciler=PolymerMdReconciler(repo,worker)
        await reconciler.run_once()
        assert [r.job_id for r in worker.requests]==[first['job_id']]
        assert repo.get_service_job(first['job_id'])['status']=='queued'
        assert repo.get_service_job(second['job_id'])['status']=='pending'
        with pytest.raises(PolymerMdError,match='名额'):
            create(repo,ids[0],'polymer-unknown-next')
        worker.uncertain=False
        await reconciler.run_once()
        assert worker.requests[0].model_dump()==worker.requests[1].model_dump()
        assert worker.requests[-1].job_id==second['job_id']
        assert repo.get_service_job(second['job_id'])['status']=='queued'
    asyncio.run(reconcile())


def test_retention_uses_server_terminal_clock_and_waits_for_exact_worker_cleanup(actors,polymer_database):
    from app.services.polymer_md_reconciler import PolymerMdReconciler
    ids,repo,_=actors
    first,_=create(repo,ids[0]);repo.claim_dispatch(first['job_id'],first['attempt_token'])
    submit=submit_request(first)
    repo.authorize_start(StartAuthorizationRequest(**submit.model_dump(),worker_instance_id='a'*32))
    stage={'stage_run_id':'eq3-1','stage':'eq3','status':'completed'}
    artifact={'artifact_id':'checkpoint','stage_run_id':'eq3-1','kind':'checkpoint','filename':'checkpoint.json','size_bytes':2,'sha256':'0'*64}
    done=repo.apply_snapshot(WorkerSnapshot(**{key:getattr(submit,key) for key in ('job_id','attempt_token','request_sha256','enqueue_sequence')},worker_instance_id='a'*32,status='completed',cleanup_confirmed=True,stage_runs=[stage],artifacts=[artifact],finished_at=datetime(1970,1,1,tzinfo=timezone.utc)))
    assert done['finished_at']>datetime.now(timezone.utc)-timedelta(minutes=1)
    clock=done['finished_at']
    assert repo.mark_expired(now=clock+timedelta(days=7)-timedelta(microseconds=1))==[]
    assert repo.mark_expired(now=clock+timedelta(days=7))==[first['job_id']]
    awaiting=repo.get_service_job(first['job_id'])
    assert awaiting['artifact_state']=='delete_requested' and len(awaiting['artifacts'])==1
    assert not awaiting['delete_job_requested']
    class OfflineWorker:
        async def delete(self,*args,**kwargs):
            raise PolymerMdError('worker_unavailable','unknown cleanup',503)
    asyncio.run(PolymerMdReconciler(repo,OfflineWorker()).run_once())
    assert len(repo.get_service_job(first['job_id'])['artifacts'])==1
    repo.complete_deletion(first['job_id'],first['attempt_token'])
    assert repo.get_service_job(first['job_id'])['artifacts']==[]
    assert repo.mark_expired(now=clock+timedelta(days=30)-timedelta(microseconds=1))==[]
    assert repo.mark_expired(now=clock+timedelta(days=30))==[first['job_id']]
    assert repo.get_service_job(first['job_id'])['delete_job_requested']
    with pytest.raises(PolymerMdError,match='identity'):
        repo.complete_deletion(first['job_id'],'f'*64,delete_job=True)
    repo.complete_deletion(first['job_id'],first['attempt_token'],delete_job=True)
    with pytest.raises(PolymerMdError) as missing:
        repo.get_service_job(first['job_id'])
    assert missing.value.status==404
    with psycopg.connect(polymer_database['admin']) as c:
        for table in ('jobs','job_attempts','stage_runs','artifacts'):
            assert c.execute(sql.SQL('SELECT count(*) FROM polymer_md.{} WHERE job_id=%s').format(sql.Identifier(table)),(first['job_id'],)).fetchone()[0]==0


def test_retention_preserves_active_execution_and_stage_references(actors,polymer_database):
    ids,repo,_=actors
    active,_=create(repo,ids[0])
    blocked,_=create(repo,ids[1])
    future=datetime.now(timezone.utc)+timedelta(days=90)
    with psycopg.connect(polymer_database['admin']) as c:
        c.execute("UPDATE polymer_md.jobs SET status='completed',cleanup_confirmed=true,finished_at=now()-interval '90 days' WHERE job_id=%s",(blocked['job_id'],))
    # A stale active attempt is an unresolved execution reference even if the
    # parent row was incorrectly made terminal; retention must hold it.
    assert repo.mark_expired(now=future)==[]
    assert repo.get_service_job(active['job_id'])['artifact_state']=='available'
    assert repo.get_service_job(blocked['job_id'])['artifact_state']=='available'
    with psycopg.connect(polymer_database['admin']) as c:
        c.execute("UPDATE polymer_md.job_attempts SET status='completed' WHERE job_id=%s",(blocked['job_id'],))
        c.execute("INSERT INTO polymer_md.stage_runs(job_id,stage_run_id,attempt_token,stage,attempt,status,quality_status) VALUES(%s,'eq3-1',%s,'eq3',1,'running','not_evaluated')",(blocked['job_id'],blocked['attempt_token']))
    assert repo.mark_expired(now=future)==[]


@pytest.mark.parametrize("tamper", ["owner_fk", "cleanup_check", "active_index", "public_acl"])
def test_exact_main23_schema_rejects_equal_count_key_and_acl_drift(actors, polymer_database, tamper):
    # This test is DB-only and is never part of the offline whitelist.
    with psycopg.connect(polymer_database["admin"], row_factory=dict_row) as connection:
        if tamper == "owner_fk":
            connection.execute("ALTER TABLE polymer_md.jobs DROP CONSTRAINT jobs_owner_user_id_fkey")
            connection.execute("ALTER TABLE polymer_md.jobs ADD FOREIGN KEY(owner_user_id) REFERENCES polymer_md.jobs(job_id)")
        elif tamper == "cleanup_check":
            connection.execute("ALTER TABLE polymer_md.jobs DROP CONSTRAINT jobs_check")
            connection.execute("ALTER TABLE polymer_md.jobs ADD CHECK(status NOT IN ('completed','failed','cancelled') OR true)")
        elif tamper == "active_index":
            connection.execute("DROP INDEX polymer_md.polymer_md_one_active_owner")
            connection.execute("CREATE UNIQUE INDEX polymer_md_one_active_owner ON polymer_md.jobs(owner_user_id) WHERE status IN ('pending','queued','running')")
        else:
            connection.execute("GRANT SELECT ON polymer_md.jobs TO PUBLIC")
        with pytest.raises(ValueError):
            validate_polymer_md_schema(connection)
        connection.rollback()
