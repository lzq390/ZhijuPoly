"""Synthetic PostgreSQL checks: no worker, API provider, or real application DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.context import Identity, service_context, user_context
from app.migration_policy import validate_migration_manifest_entries
from app.services.usage_repository import UsageRepository
from task_usage.ledger import METRICS, UsageEventConflict, record_event

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / 'backend/migrations/postgres').exists():
    ROOT = Path(os.environ['USAGE_TEST_SOURCE_ROOT'])
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
A = '10000000-0000-0000-0000-000000000001'
B = '10000000-0000-0000-0000-000000000002'


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv('USAGE_METERING_ENABLED', 'true')


def event(**changes):
    value = dict(owner_user_id=A, module='monomer_md', task_id='monomer_md:'+uuid4().hex,
                 event_key=uuid4().hex, occurred_at=T0, status='queued')
    value.update(changes)
    return value


@pytest.fixture(scope='module')
def usage_db():
    base = os.environ.get('USAGE_TEST_ADMIN_DSN') or os.environ.get('ISOLATION_TEST_ADMIN_DSN')
    if not base:
        pytest.skip('A disposable USAGE_TEST_ADMIN_DSN or ISOLATION_TEST_ADMIN_DSN is required')
    name = 'usage_test_'+uuid4().hex
    dsn = make_conninfo(base, dbname=name)
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        with psycopg.connect(dsn) as connection:
            for migration in sorted((ROOT/'backend/migrations/postgres').glob('*.sql')):
                if migration.name.startswith('0018_'):
                    connection.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES (%s,'usage-owner-a','!',false),(%s,'usage-owner-b','!',false)", (A,B))
                    connection.execute("SELECT set_config('nexpoly.legacy_owner',%s,true)", (A,))
                if migration.name <= '0020_usage_metering.sql':
                    connection.execute(migration.read_text())
        yield dsn
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def as_role(connection, owner=A, role='nexpoly_api'):
    connection.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    connection.execute("SELECT set_config('app.user_id',%s,true)", (owner or '',))


def test_disabled_has_zero_db_and_zero_validation(monkeypatch):
    monkeypatch.delenv('USAGE_METERING_ENABLED')
    class Forbidden:
        def __getattribute__(self, name):
            raise AssertionError('Disabled usage touched connection')
    payload = event(owner_user_id='invalid-irrelevant-when-disabled')
    assert record_event(Forbidden(), **payload)['disabled']
    assert UsageRepository('never-connect').record_event(**payload)['disabled']


@pytest.mark.parametrize('changes', [
    {'occurred_at':datetime(2026,1,1)}, {'status':'unknown'}, {'source':'request-body'},
    {'module':'bad.module'}, {'attempt_id':1}, {'event_key':''},
    {'metrics':{'queue_ms':-1}}, {'metrics':{'execution_ms':True}},
    {'metrics':{'artifact_bytes':2**63}}, {'metrics':{'physical_gpu_seconds':1}},
    {'parent_task_id':'parent'}, {'root_task_id':'unrelated'},
    {'started_at':T0+timedelta(seconds=2),'submitted_at':T0+timedelta(seconds=3)},
    {'finished_at':T0},
])
def test_invalid_event_rejected_before_db(changes):
    with pytest.raises((ValueError,TypeError)):
        record_event(None, **event(**changes))


def test_manifest_exact_contract_chain_and_invoker_only(usage_db):
    entries = validate_migration_manifest_entries(ROOT/'backend/migrations/postgres')
    new = next(item for item in entries if item.version=='0020_usage_metering')
    assert new.kind=='expand' and new.epoch==5
    assert [x.version for x in new.requires_contracts]==['0012_drop_polytao_jobs','0018_user_isolation_cutover','0019_service_auth_least_privilege']
    with psycopg.connect(usage_db, row_factory=dict_row) as connection:
        functions = connection.execute("SELECT p.proname,p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='usage'").fetchall()
        assert len(functions)==3 and not any(f['prosecdef'] for f in functions)
        assert connection.execute("SELECT count(*) AS n FROM pg_constraint WHERE contype='f' AND connamespace='usage'::regnamespace").fetchone()['n']==0


def test_same_event_exactly_once_and_conflicting_replay(usage_db):
    value=event()
    with psycopg.connect(usage_db) as connection:
        as_role(connection)
        first=record_event(connection, **value)
        second=record_event(connection, **value)
        assert first['inserted'] and not second['inserted']
        assert second['attempt']['event_count']==1
        assert all(second['attempt'][m] is None for m in METRICS)
        with pytest.raises(UsageEventConflict):
            record_event(connection, **{**value,'metrics':{'queue_ms':1}})


def test_out_of_order_terminal_is_sticky_metrics_remain_cumulative(usage_db):
    value=event(status='completed',occurred_at=T0+timedelta(seconds=5),
                submitted_at=T0,started_at=T0+timedelta(seconds=1),
                finished_at=T0+timedelta(seconds=5),metrics={'execution_ms':4000,'artifact_bytes':10})
    with psycopg.connect(usage_db) as connection:
        as_role(connection)
        record_event(connection, **value)
        late={**value,'event_key':uuid4().hex,'status':'running','occurred_at':T0+timedelta(seconds=1),'finished_at':None,
              'metrics':{'execution_ms':50,'provider_input_tokens':0}}
        row=record_event(connection, **late)['attempt']
        assert row['status']=='completed' and row['execution_ms']==4000
        assert row['artifact_bytes']==10 and row['provider_input_tokens']==0
        assert row['provider_output_tokens'] is None
        conflicting={**value,'event_key':uuid4().hex,'status':'failed'}
        assert record_event(connection,**conflicting)['attempt']['status']=='completed'


def test_business_and_usage_rollback_together_no_hidden_commit(usage_db):
    value=event()
    with psycopg.connect(usage_db, row_factory=dict_row) as connection:
        connection.execute('CREATE TEMP TABLE synthetic_business(id text)')
        connection.commit()
        connection.execute('INSERT INTO synthetic_business VALUES (%s)', (value['task_id'],))
        record_event(connection, **value)
        connection.rollback()
        assert connection.execute('SELECT count(*) AS n FROM synthetic_business').fetchone()['n']==0
        assert connection.execute('SELECT count(*) AS n FROM usage.events WHERE event_key=%s',(value['event_key'],)).fetchone()['n']==0


def test_business_deletion_preserves_usage(usage_db):
    value=event()
    with psycopg.connect(usage_db, row_factory=dict_row) as connection:
        connection.execute('CREATE TEMP TABLE synthetic_jobs(id text)')
        connection.execute('INSERT INTO synthetic_jobs VALUES (%s)',(value['task_id'],))
        record_event(connection,**value)
        connection.execute('DELETE FROM synthetic_jobs')
        assert connection.execute('SELECT count(*) AS n FROM usage.attempts WHERE task_id=%s',(value['task_id'],)).fetchone()['n']==1


def test_rls_wrong_owner_denied_and_missing_owner_sees_nothing(usage_db):
    value=event()
    with psycopg.connect(usage_db) as connection:
        as_role(connection)
        record_event(connection,**value)
    with psycopg.connect(usage_db) as connection:
        as_role(connection,B)
        assert connection.execute('SELECT * FROM usage.events WHERE event_key=%s',(value['event_key'],)).fetchone() is None
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            record_event(connection,**event(owner_user_id=A))
    with psycopg.connect(usage_db) as connection:
        as_role(connection,None)
        assert connection.execute('SELECT * FROM usage.attempts').fetchall()==[]


@pytest.mark.parametrize('statement',[
    'UPDATE usage.events SET status=\'failed\' WHERE task_id=%s',
    'DELETE FROM usage.events WHERE task_id=%s',
    'UPDATE usage.attempts SET status=\'failed\' WHERE task_id=%s',
    'DELETE FROM usage.attempts WHERE task_id=%s',
])
def test_events_immutable_projection_cannot_be_edited_directly(usage_db,statement):
    value=event()
    with psycopg.connect(usage_db) as connection:
        as_role(connection)
        record_event(connection,**value)
    with psycopg.connect(usage_db) as connection:
        as_role(connection)
        with pytest.raises((psycopg.errors.InsufficientPrivilege,psycopg.errors.ObjectNotInPrerequisiteState)):
            connection.execute(statement,(value['task_id'],))


def test_audit_read_only_and_service_cross_owner(usage_db):
    with psycopg.connect(usage_db) as connection:
        as_role(connection,None,'nexpoly_service')
        row=record_event(connection,**event(owner_user_id=B))['attempt']
        assert str(row['owner_user_id'])==B
    with psycopg.connect(usage_db) as connection:
        as_role(connection,None,'nexpoly_mutable_audit')
        assert connection.execute('SELECT * FROM usage.attempts LIMIT 1').fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            record_event(connection,**event())


@pytest.mark.parametrize('role',['nexpoly_api','nexpoly_service'])
@pytest.mark.parametrize('attack',['metric','old_key_with_forged_count','missing_key'])
def test_projection_rejects_nested_temporary_trigger_without_event(usage_db,role,attack):
    value=event()
    with psycopg.connect(usage_db) as connection:
        as_role(connection,role=role)
        record_event(connection,**value)
        record_event(connection,**{**value,'event_key':uuid4().hex})
        connection.execute('CREATE TEMP TABLE usage_attack_trigger_probe(n integer)')
        assignments={'metric':sql.SQL('artifact_bytes=987654321'),
            'old_key_with_forged_count':sql.SQL('last_event_key={},event_count=event_count+1').format(sql.Literal(value['event_key'])),
            'missing_key':sql.SQL("last_event_key='missing-immutable-event',event_count=event_count+1,status='completed'")}
        body=sql.SQL("BEGIN UPDATE usage.attempts SET {} WHERE owner_user_id={} AND task_id={}; RETURN NEW; END").format(assignments[attack],sql.Literal(A),sql.Literal(value['task_id']))
        connection.execute(sql.SQL('CREATE FUNCTION pg_temp.usage_attack() RETURNS trigger LANGUAGE plpgsql AS {}').format(sql.Literal(body.as_string(connection))))
        connection.execute('CREATE TRIGGER usage_attack AFTER INSERT ON usage_attack_trigger_probe FOR EACH ROW EXECUTE FUNCTION pg_temp.usage_attack()')
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute('INSERT INTO usage_attack_trigger_probe VALUES(1)')


def test_concurrent_duplicates_and_distinct_observations(usage_db):
    value=event()
    def write(payload):
        with psycopg.connect(usage_db) as connection:
            as_role(connection)
            return record_event(connection,**payload)['inserted']
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(write,[value]*12))==1
        payloads=[{**value,'event_key':uuid4().hex,'metrics':{'execution_ms':n}} for n in range(12)]
        assert all(pool.map(write,payloads))
    with psycopg.connect(usage_db, row_factory=dict_row) as connection:
        row=connection.execute('SELECT * FROM usage.attempts WHERE task_id=%s',(value['task_id'],)).fetchone()
        assert row['event_count']==13 and row['execution_ms']==11


def test_ancestry_conflict_rolls_back_event(usage_db):
    root='monomer_md:'+uuid4().hex
    value=event(task_id='monomer_md:'+uuid4().hex,parent_task_id=root,root_task_id=root)
    with psycopg.connect(usage_db) as connection:
        record_event(connection,**value)
    changed={**value,'event_key':uuid4().hex,'root_task_id':'monomer_md:other'}
    with psycopg.connect(usage_db) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            record_event(connection,**changed)
    with psycopg.connect(usage_db) as connection:
        assert connection.execute('SELECT * FROM usage.events WHERE event_key=%s',(changed['event_key'],)).fetchone() is None


def test_query_owner_comes_from_context_and_counts_do_not_duplicate_children_or_retries(usage_db):
    module='test_'+uuid4().hex
    task=module+':parent'
    repo=UsageRepository(usage_db)
    with psycopg.connect(usage_db) as connection:
        for owner in [A,B]:
            record_event(connection,**event(owner_user_id=owner,module=module,task_id=task,metrics={'observed_wall_ms':10}))
        record_event(connection,**event(module=module,task_id=task,attempt_id='2'))
        record_event(connection,**event(module=module,task_id=module+':child',kind='execution',parent_task_id=task,root_task_id=task,metrics={'execution_ms':5}))
        record_event(connection,**event(module=module,task_id=module+':provider',kind='provider',parent_task_id=task,root_task_id=task,metrics={'provider_input_tokens':0}))
    with user_context(Identity(A), SimpleNamespace(service_dsn=usage_db)), service_context():
        # Bind the synthetic service identity explicitly; CI may set a different
        # cluster-wide APP_SERVICE_POSTGRES_DSN. Owner filtering still applies.
        rows=repo.list_attempts(module=module)
        assert len(rows)==4 and all(str(row['owner_user_id'])==A for row in rows)
        summary=repo.summary(module=module)
        assert summary['task_count']==1
        groups={g['kind']:g for g in summary['groups']}
        assert groups['task']['attempt_count']==2
        assert groups['task']['metrics']['observed_wall_ms']=={'total':10,'known_count':1}
        assert groups['provider']['metrics']['provider_input_tokens']=={'total':0,'known_count':1}
        assert groups['provider']['metrics']['provider_output_tokens']=={'total':None,'known_count':0}
        assert repo.list_attempts(module=module,start_at=T0+timedelta(seconds=1))==[]
    with pytest.raises(Exception) as exc:
        repo.summary(module=module)
    assert getattr(exc.value,'status_code',None)==401
