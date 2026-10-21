"""Real native repository transactions against disposable synthetic PostgreSQL.

No worker, model, provider, network dispatch, or scientific execution is started.
Existing role/RLS contracts are used; only the test database receives fault DDL.
"""
from contextlib import contextmanager
from dataclasses import replace
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import TypeAdapter
import pytest

from test_usage_repository import usage_db, enabled, A, B
from app.auth.context import service_context
from app.services import monomer_md_repository as md
from app.services.monomer_dft_repository import MonomerDftRepository
from app.services.monomer_dft_models import MonomerDftRunRequest
from app.services.monomer_dft_protocol import prepare_monomer_dft_request
from app.services.online_knowledge import postgres_history_repository as online
from app.services.polymerization_batch.models import BatchSettings
from app.services.polymerization_batch.repository import BatchRepository
from workers.monomer_md_worker.app.repository import PostgresJobRepository, JobUpdateResult
from workers.monomer_md_worker.app.config import load_settings


@contextmanager
def transaction(dsn, *, owner=A, role='nexpoly_service'):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        conn.execute("SELECT set_config('app.user_id',%s,true)", (owner,))
        yield conn


def service_connection(dsn):
    return transaction(dsn)


def usage_rows(dsn, module, native_id, *, owner=A):
    with transaction(dsn, owner=owner, role='nexpoly_api') as conn:
        return conn.execute('SELECT * FROM usage.attempts WHERE task_id=%s ORDER BY kind',
                            (f'{module}:{native_id}',)).fetchall()


def events(dsn, module, native_id):
    with transaction(dsn) as conn:
        return conn.execute('SELECT status,event_key,owner_user_id FROM usage.events WHERE task_id=%s ORDER BY recorded_at,event_key',
                            (f'{module}:{native_id}',)).fetchall()


def assert_terminal(dsn, module, native_id, status, *, owner=A):
    rows = usage_rows(dsn, module, native_id, owner=owner)
    assert len(rows) == 1
    row = rows[0]
    assert str(row['owner_user_id']) == owner
    assert row['status'] == status and row['kind'] == 'task'
    assert row['root_task_id'] == f'{module}:{native_id}'
    assert row['finished_at'] is not None
    assert row['execution_ms'] is None  # No scientific execution measured here.
    assert row['provider_total_tokens'] is None
    assert usage_rows(dsn, module, native_id, owner=B if owner == A else A) == []
    return row


def create_md(conn, job_id, owner):
    md.create_monomer_md_job_postgres(conn, job_id=job_id, owner_user_id=owner,
        input_smiles='O', canonical_smiles='O', requested_steps=300)


def md_worker(dsn):
    # Use the real tuple-cursor worker repository, restricted by the real service role.
    return PostgresJobRepository(replace(load_settings(), app_postgres_dsn=
        make_conninfo(dsn, options='-c role=nexpoly_service')))


@pytest.mark.parametrize('owner', [A, B])
def test_md_worker_lifecycle_same_transaction_and_duplicate_terminal(usage_db, owner):
    job_id = uuid4().hex
    with transaction(usage_db, owner=owner, role='nexpoly_api') as conn:
        create_md(conn, job_id, owner)
        assert conn.execute('SELECT status FROM usage.attempts WHERE task_id=%s',
                            ('monomer_md:'+job_id,)).fetchone()['status'] == 'queued'
        with psycopg.connect(usage_db) as observer:
            assert observer.execute('SELECT count(*) FROM md.monomer_md_jobs WHERE job_id=%s', (job_id,)).fetchone()[0] == 0
            assert observer.execute('SELECT count(*) FROM usage.events WHERE task_id=%s', ('monomer_md:'+job_id,)).fetchone()[0] == 0
    worker = md_worker(usage_db)
    assert worker.accept_job(job_id, 'synthetic-worker', queued=False) == JobUpdateResult.UPDATED
    assert worker.update_status(job_id, 'running', worker_instance_id='synthetic-worker') == JobUpdateResult.UPDATED
    assert worker.update_status(job_id, 'completed', worker_instance_id='synthetic-worker') == JobUpdateResult.UPDATED
    row = assert_terminal(usage_db, 'monomer_md', job_id, 'completed', owner=owner)
    assert row['started_at'] is not None and row['observed_wall_ms'] is not None
    before = events(usage_db, 'monomer_md', job_id)
    assert [r['status'] for r in before] == ['queued', 'running', 'completed']
    assert worker.update_status(job_id, 'completed', worker_instance_id='synthetic-worker') == JobUpdateResult.ALREADY_TERMINAL
    assert events(usage_db, 'monomer_md', job_id) == before


def prepared_dft():
    return prepare_monomer_dft_request(TypeAdapter(MonomerDftRunRequest).validate_python({
        'input': {'smiles':'O','net_charge':None,'multiplicity':1,'psmiles_mode':None},
        'calculation_type':'single_point','model':'aimnet2',
        'conformer':{'seed':1,'max_iterations':500}, 'single_point':{'properties':['energy']}}))


def dft_repo(dsn):
    return MonomerDftRepository(dsn, connection_factory=service_connection)


def dft_snapshot(job, prepared, status):
    return {'schema_version':2, 'job_id':job['job_id'], 'attempt_token':job['_attempt_token'],
            'request_sha256':prepared.request_sha256, 'enqueue_sequence':job['_enqueue_sequence'],
            'worker_instance_id':'synthetic-worker', 'status':status, 'stage':'artifacts' if status=='completed' else 'single_point',
            'result':{} if status=='completed' else None, 'error':None, 'artifacts':[]}


@pytest.mark.parametrize('owner', [A, B])
def test_dft_lifecycle_idempotent_submission_and_terminal_snapshot(usage_db, owner):
    repo, prepared, key = dft_repo(usage_db), prepared_dft(), uuid4().hex
    created = repo.create_job(prepared, idempotency_key=key, max_active_jobs=10, owner_user_id=owner)
    job = created.job
    replay = repo.create_job(prepared, idempotency_key=key, max_active_jobs=10, owner_user_id=owner)
    assert created.created and not replay.created and replay.job['job_id'] == job['job_id']
    assert len(events(usage_db,'monomer_dft',job['job_id'])) == 1
    repo.apply_worker_snapshot(job_id=job['job_id'], attempt_token=job['_attempt_token'], snapshot=dft_snapshot(job,prepared,'running'))
    terminal = dft_snapshot(job,prepared,'completed')
    repo.apply_worker_snapshot(job_id=job['job_id'], attempt_token=job['_attempt_token'], snapshot=terminal)
    row = assert_terminal(usage_db,'monomer_dft',job['job_id'],'completed',owner=owner)
    assert row['attempt_id'] == '1' and row['started_at'] is not None
    before=events(usage_db,'monomer_dft',job['job_id'])
    assert [r['status'] for r in before] == ['queued','running','completed']
    repo.apply_worker_snapshot(job_id=job['job_id'], attempt_token=job['_attempt_token'], snapshot=terminal)
    assert events(usage_db,'monomer_dft',job['job_id']) == before


def batch_repo(dsn):
    repo = BatchRepository(dsn, BatchSettings())
    repo.connection = lambda: transaction(dsn)
    return repo


def create_batch(repo, conn, job_id, owner):
    return repo.insert_job(conn,job_id,{'import_id':uuid4().hex},'a'*64,uuid4().hex,
        {'fingerprint':'synthetic-repository-test'}, {},
        [{'chunk_id':'export','phase':3,'kind':'export','payload':{}}],owner_user_id=owner)


def test_batch_lifecycle_child_attempt_and_stale_callback(usage_db):
    repo, job_id = batch_repo(usage_db), uuid4().hex
    with transaction(usage_db) as conn:
        created=create_batch(repo,conn,job_id,A)
        assert repo.existing_job(created['idempotency_key'], 'a'*64, conn, owner_user_id=A)['id']==job_id
        assert conn.execute('SELECT status FROM usage.attempts WHERE task_id=%s', ('polymerization_batch:'+job_id,)).fetchone()['status']=='queued'
    with service_context():
        job,chunk=repo.claim('synthetic-worker','synthetic-repository-test')
        assert job['id']==job_id
        # Repository-only unit: measure this test interval, never invent model time.
        start=time.monotonic()
        elapsed=time.monotonic()-start
        assert repo.finish_unit(job,chunk,{},elapsed,export={'status':'completed','summary':{},'artifacts':{}})
        before=events(usage_db,'polymerization_batch',job_id)
        assert not repo.finish_unit(job,chunk,{},elapsed,export={'status':'completed','summary':{},'artifacts':{}})
    assert events(usage_db,'polymerization_batch',job_id)==before
    assert_terminal(usage_db,'polymerization_batch',job_id,'completed')
    with transaction(usage_db) as conn:
        child=conn.execute("SELECT * FROM usage.attempts WHERE parent_task_id=%s", ('polymerization_batch:'+job_id,)).fetchone()
    assert child['kind']=='execution' and child['attempt_id']=='1' and child['status']=='completed'
    assert child['execution_ms'] is None and child['observed_wall_ms'] is not None


def create_online(conn,job_id,owner):
    online.create_online_job_postgres(conn,owner_user_id=owner,job_id=job_id,
                                     material='synthetic-test',mode='synthesis',max_papers=1)


@pytest.mark.parametrize('owner',[A,B])
def test_online_lifecycle_owner_and_delete_keeps_ledger(usage_db,owner):
    job_id=uuid4().hex
    # The official submit route explicitly uses service_context for account locking.
    with transaction(usage_db,owner=owner) as conn:
        create_online(conn,job_id,owner)
        assert conn.execute('SELECT status FROM usage.attempts WHERE task_id=%s',('online_knowledge:'+job_id,)).fetchone()['status']=='queued'
    with transaction(usage_db) as conn:
        online.mark_online_job_running_postgres(conn,job_id,owner_user_id=owner)
    with transaction(usage_db) as conn:
        online.mark_online_job_completed_postgres(conn,job_id,{},owner_user_id=owner)
    row=assert_terminal(usage_db,'online_knowledge',job_id,'completed',owner=owner)
    assert row['started_at'] is None and row['observed_wall_ms'] is not None
    before=events(usage_db,'online_knowledge',job_id)
    with transaction(usage_db) as conn:
        online._record_usage(conn,job_id,owner_user_id=owner)
        online._record_usage(conn,job_id,owner_user_id=owner)
        conn.execute('DELETE FROM online_knowledge.jobs WHERE job_id=%s AND owner_user_id=%s::uuid',(job_id,owner))
    assert events(usage_db,'online_knowledge',job_id)==before
    assert_terminal(usage_db,'online_knowledge',job_id,'completed',owner=owner)


@contextmanager
def reject_ledger_insert(dsn):
    # Only disposable fixture DB; production roles are not granted/revoked.
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE FUNCTION public.usage_native_test_reject() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic ledger fault' USING ERRCODE='23514'; END $$")
        conn.execute('CREATE TRIGGER usage_native_test_reject BEFORE INSERT ON usage.events FOR EACH ROW EXECUTE FUNCTION public.usage_native_test_reject()')
    try:
        yield
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute('DROP TRIGGER usage_native_test_reject ON usage.events')
            conn.execute('DROP FUNCTION public.usage_native_test_reject()')


@pytest.mark.parametrize('module',['monomer_md','monomer_dft','polymerization_batch','online_knowledge'])
def test_ledger_insert_failure_rolls_back_admission_before_dispatch(usage_db,module):
    native_id,key=uuid4().hex,uuid4().hex
    dispatch=[]
    with reject_ledger_insert(usage_db), pytest.raises(psycopg.errors.CheckViolation,match='synthetic ledger fault'):
        if module=='monomer_dft':
            dft_repo(usage_db).create_job(prepared_dft(),idempotency_key=key,max_active_jobs=10,owner_user_id=A)
        else:
            with transaction(usage_db) as conn:
                if module=='monomer_md': create_md(conn,native_id,A)
                elif module=='online_knowledge': create_online(conn,native_id,A)
                else: create_batch(batch_repo(usage_db),conn,native_id,A)
        dispatch.append('would-dispatch-after-successful-commit')
    assert dispatch==[]
    with psycopg.connect(usage_db) as conn:
        table,column,value={
            'monomer_md':('md.monomer_md_jobs','job_id',native_id),
            'monomer_dft':('monomer_dft.jobs','idempotency_key',key),
            'polymerization_batch':('polymerization_batch.jobs','id',native_id),
            'online_knowledge':('online_knowledge.jobs','job_id',native_id)}[module]
        count=conn.execute(sql.SQL('SELECT count(*) FROM {} WHERE {}=%s').format(
            sql.Identifier(*table.split('.')),sql.Identifier(column)),(value,)).fetchone()[0]
        assert count==0
        assert conn.execute('SELECT count(*) FROM usage.events WHERE task_id=%s',(module+':'+native_id,)).fetchone()[0]==0
        if module=='polymerization_batch':
            assert conn.execute('SELECT count(*) FROM polymerization_batch.chunks WHERE job_id=%s',(native_id,)).fetchone()[0]==0


def test_md_terminal_ledger_failure_rolls_back_worker_status(usage_db):
    job_id=uuid4().hex
    with transaction(usage_db) as conn:create_md(conn,job_id,A)
    worker=md_worker(usage_db)
    worker.accept_job(job_id,'synthetic-worker',queued=False)
    worker.update_status(job_id,'running',worker_instance_id='synthetic-worker')
    with reject_ledger_insert(usage_db), pytest.raises(psycopg.errors.CheckViolation):
        worker.update_status(job_id,'completed',worker_instance_id='synthetic-worker')
    with transaction(usage_db) as conn:
        row=conn.execute('SELECT status,finished_at FROM md.monomer_md_jobs WHERE job_id=%s',(job_id,)).fetchone()
    assert row=={'status':'running','finished_at':None}
    assert usage_rows(usage_db,'monomer_md',job_id)[0]['status']=='running'
    worker.update_status(job_id,'completed',worker_instance_id='synthetic-worker')
    assert_terminal(usage_db,'monomer_md',job_id,'completed')
