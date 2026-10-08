"""Usage API and cohort aggregation against a disposable real PostgreSQL."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row
import pytest

from app.auth.context import Identity, user_context
from app.postgres_database import postgres_connection
from app.routers.usage import router
from app.services.usage_schema import validate_usage_schema
from app.services.usage_statistics import UsageStatistics, period
from task_usage.ledger import record_event
from test_usage_repository import usage_db, enabled, event, A, B, T0, ROOT


def prepare(connection, module, *, owner=A, task='root', at=T0, **values):
    task_id=module+':'+task
    value=event(owner_user_id=owner,module=module,task_id=task_id,occurred_at=at,**values)
    record_event(connection,**value)
    return value


def unique_module():
    return 'test_'+uuid4().hex


def summary(dsn,module,**kwargs):
    with user_context(Identity(A)):
        return UsageStatistics(dsn).summary(start=kwargs.get('start',T0),end=kwargs.get('end',T0+timedelta(days=1)),module=module)


def test_child_after_midnight_inherits_root_cohort_and_metrics_do_not_double_count(usage_db):
    module=unique_module()
    with psycopg.connect(usage_db) as connection:
        prepare(connection,module,at=T0+timedelta(hours=23),submitted_at=T0+timedelta(hours=23),
                metrics={'observed_wall_ms':100,'execution_ms':999,'provider_input_tokens':888,'artifact_bytes':7})
        prepare(connection,module,task='execution',at=T0+timedelta(days=1,hours=1),kind='execution',
                parent_task_id=module+':root',root_task_id=module+':root',metrics={'execution_ms':5,'artifact_bytes':666})
        prepare(connection,module,task='provider',at=T0+timedelta(days=1,hours=2),kind='provider',
                parent_task_id=module+':root',root_task_id=module+':root',metrics={'provider_input_tokens':0,'provider_output_tokens':2,'provider_total_tokens':2,'reasoning_tokens':0})
    result=summary(usage_db,module)
    assert result['totals']['tasks']==1 and result['totals']['attempts']==1 and result['totals']['child_calls']==2
    assert result['daily']==[{'date':'2026-01-01','tasks':1,'states':{'queued':1}}]
    metrics=result['totals']['metrics']
    assert metrics['execution_ms']=={'value':5,'known_count':1,'unknown_count':0}
    assert metrics['observed_wall_ms']=={'value':100,'known_count':1,'unknown_count':0}
    assert metrics['artifact_bytes']['value']==7
    assert metrics['provider_input_tokens']=={'value':0,'known_count':1,'unknown_count':0}
    assert metrics['cached_input_tokens']=={'value':None,'known_count':0,'unknown_count':1}
    assert summary(usage_db,module,start=T0+timedelta(days=1),end=T0+timedelta(days=2))['totals']['tasks']==0


def test_retry_is_one_root_two_attempts_and_late_old_attempt_cannot_hide_latest(usage_db):
    module=unique_module()
    with psycopg.connect(usage_db) as connection:
        first=prepare(connection,module,attempt_id='1',submitted_at=T0)
        prepare(connection,module,attempt_id='2',at=T0+timedelta(hours=1),status='running',submitted_at=T0,started_at=T0+timedelta(hours=1))
        record_event(connection,**{**first,'event_key':uuid4().hex,'status':'failed',
                                   'occurred_at':T0+timedelta(hours=2),'finished_at':T0+timedelta(minutes=5)})
    result=summary(usage_db,module)['totals']
    assert result['tasks']==1 and result['attempts']==2
    assert result['states']=={'running':1}


def test_null_zero_and_empty_are_distinct(usage_db):
    module=unique_module()
    with psycopg.connect(usage_db) as connection:
        prepare(connection,module,task='unknown')
        prepare(connection,module,task='zero',metrics={'queue_ms':0,'artifact_bytes':0})
    metrics=summary(usage_db,module)['totals']['metrics']
    assert metrics['queue_ms']=={'value':0,'known_count':1,'unknown_count':1}
    assert metrics['observed_wall_ms']=={'value':None,'known_count':0,'unknown_count':2}
    empty=summary(usage_db,unique_module())['totals']
    assert empty['tasks']==0 and empty['metrics']['queue_ms']=={'value':None,'known_count':0,'unknown_count':0}


def test_retry_numeric_ten_after_two_and_opaque_attempts_use_start_order(usage_db):
    module=unique_module()
    with psycopg.connect(usage_db) as connection:
        prepare(connection,module,task='numeric',attempt_id='2',status='failed')
        prepare(connection,module,task='numeric',attempt_id='10',status='completed')
        first=prepare(connection,module,task='opaque',attempt_id='instance:old:1')
        prepare(connection,module,task='opaque',attempt_id='instance:new:1',status='running',at=T0+timedelta(minutes=2))
        record_event(connection,**{**first,'event_key':uuid4().hex,'status':'failed','occurred_at':T0+timedelta(minutes=3)})
    assert summary(usage_db,module)['totals']['states']=={'completed':1,'running':1}


def test_owner_isolation_default_denial_and_half_open_bounds(usage_db):
    module=unique_module()
    with psycopg.connect(usage_db) as connection:
        prepare(connection,module,owner=A,task='start',submitted_at=T0)
        prepare(connection,module,owner=A,task='end',at=T0+timedelta(days=1),submitted_at=T0+timedelta(days=1))
        prepare(connection,module,owner=B,task='other',submitted_at=T0)
    assert summary(usage_db,module)['totals']['tasks']==1
    with user_context(Identity(B)):
        result=UsageStatistics(usage_db).records(start=T0,end=T0+timedelta(days=1),module=module)
    assert result['total']==1 and result['items'][0]['task_id']==module+':other'
    with pytest.raises(Exception) as exc:
        UsageStatistics(usage_db).summary(start=T0,end=T0+timedelta(days=1),module=module)
    assert exc.value.status_code==401


def test_records_pagination_status_module_and_children(usage_db):
    module=unique_module()
    with psycopg.connect(usage_db) as connection:
        for i in range(3):
            prepare(connection,module,task=str(i),at=T0+timedelta(minutes=i),status='completed' if i==2 else 'queued')
        prepare(connection,module,task='child',kind='provider',parent_task_id=module+':2',root_task_id=module+':2')
    with user_context(Identity(A)):
        repo=UsageStatistics(usage_db)
        args=dict(start=T0,end=T0+timedelta(days=1),module=module)
        first=repo.records(**args,page=1,page_size=2)
        second=repo.records(**args,page=2,page_size=2)
        assert first['total']==second['total']==3
        assert len({x['task_id'] for x in first['items']+second['items']})==3
        assert repo.records(**args,status='completed')['total']==1
        assert repo.records(**args,include_children=True)['total']==4
        assert repo.records(**{**args,'module':unique_module()})['items']==[]
        for invalid in [dict(status='bad'),dict(page=0),dict(page_size=101)]:
            with pytest.raises(ValueError):repo.records(**args,**invalid)


def test_period_defaults_timezone_and_rejections():
    start,end=period(None,None,now=T0)
    assert end==T0 and end-start==timedelta(days=30)
    local=T0.astimezone(timezone(timedelta(hours=8)))
    assert period(local,local+timedelta(hours=1))==(T0,T0+timedelta(hours=1))
    for start,end in [(T0,T0),(T0,T0-timedelta(seconds=1)),(T0,T0+timedelta(days=367)),(T0.replace(tzinfo=None),T0)]:
        with pytest.raises(ValueError):period(start,end)


def test_readonly_snapshot_is_consistent_under_concurrent_usage_write(usage_db):
    module=unique_module()
    with user_context(Identity(A)), postgres_connection(usage_db,readonly_snapshot=True) as snapshot:
        before=snapshot.execute('SELECT count(*) AS n FROM usage.events WHERE module=%s',(module,)).fetchone()['n']
        with psycopg.connect(usage_db) as other:prepare(other,module)
        after=snapshot.execute('SELECT count(*) AS n FROM usage.events WHERE module=%s',(module,)).fetchone()['n']
        assert before==after==0
    with psycopg.connect(usage_db, row_factory=dict_row) as connection:
        assert connection.execute('SELECT count(*) AS n FROM usage.events WHERE module=%s',(module,)).fetchone()['n']==1


@pytest.fixture
def api(usage_db):
    app=FastAPI()
    app.state.settings=SimpleNamespace(app_postgres_dsn=usage_db)
    app.state.owner=None
    @app.middleware('http')
    async def trusted_identity(request,call_next):
        if app.state.owner:
            with user_context(Identity(app.state.owner)):
                return await call_next(request)
        return await call_next(request)
    app.include_router(router)
    with TestClient(app) as client:
        yield app,client


def test_api_authentication_disabled_and_validation(api,monkeypatch):
    app,client=api
    monkeypatch.setenv('USAGE_METERING_ENABLED','false')
    assert client.get('/api/v1/usage/summary').status_code==401
    assert client.get('/api/v1/usage/catalog').status_code==401
    app.state.owner=A
    assert client.get('/api/v1/usage/summary').status_code==503
    assert client.get('/api/v1/usage/records').status_code==503
    catalog=client.get('/api/v1/usage/catalog')
    assert catalog.status_code==200 and catalog.json()['enabled'] is False
    monkeypatch.setenv('USAGE_METERING_ENABLED','true')
    for query in ['page=0','page_size=101','status=notreal','start=2026-01-01T00:00:00','module=bad.module']:
        assert client.get('/api/v1/usage/records?'+query).status_code==422


def test_api_ignores_untrusted_owner_filter_and_returns_only_personal_rows(api,usage_db):
    app,client=api
    module=unique_module()
    with psycopg.connect(usage_db) as connection:
        prepare(connection,module,owner=A,task='mine')
        prepare(connection,module,owner=B,task='other')
    app.state.owner=A
    params={'start':T0.isoformat(),'end':(T0+timedelta(days=1)).isoformat(),'module':module,'owner_user_id':B}
    result=client.get('/api/v1/usage/records',params=params)
    assert result.status_code==200 and result.json()['total']==1
    assert result.json()['items'][0]['task_id']==module+':mine'
    summary_response=client.get('/api/v1/usage/summary',params=params)
    assert summary_response.status_code==200 and summary_response.json()['totals']['tasks']==1
    assert 'coverage' in summary_response.json()


def test_api_storage_error_is_redacted(api,monkeypatch):
    app,client=api
    app.state.owner=A
    def fail(*args,**kwargs):raise RuntimeError('password=synthetic-must-not-leak')
    monkeypatch.setattr(UsageStatistics,'summary',fail)
    result=client.get('/api/v1/usage/summary')
    assert result.status_code==503 and 'synthetic-must-not-leak' not in result.text


def test_api_partial_coverage_is_not_called_external_uncovered(api,monkeypatch):
    from app.routers import usage as usage_routes
    app,client=api
    app.state.owner=A
    monkeypatch.setattr(usage_routes,'catalog_modules',lambda:[
        {'id':'owned','label':'Owned','coverage':'partial'},
        {'id':'outside','label':'Outside','coverage':'external'}])
    result=client.get('/api/v1/usage/summary')
    assert result.status_code==200
    assert result.json()['coverage']['uncovered']==['outside']


def test_usage_schema_exact_contract(usage_db):
    with psycopg.connect(usage_db) as connection:
        assert validate_usage_schema(connection)['validated']


def test_usage_schema_validation_works_as_api_in_readonly_transaction(usage_db):
    from test_usage_repository import as_role
    with user_context(Identity(A)),postgres_connection(usage_db,readonly_snapshot=True) as connection:
        as_role(connection)
        assert validate_usage_schema(connection)['validated']


@pytest.mark.parametrize('tamper',[
    'ALTER TABLE usage.events DISABLE ROW LEVEL SECURITY',
    'ALTER TABLE usage.attempts NO FORCE ROW LEVEL SECURITY',
    'ALTER POLICY owner_access ON usage.events USING (true) WITH CHECK (true)',
    'GRANT UPDATE ON usage.events TO nexpoly_api',
    'GRANT UPDATE(status) ON usage.events TO nexpoly_api',
    'GRANT SELECT ON usage.events TO PUBLIC',
    'GRANT CREATE ON SCHEMA usage TO nexpoly_api',
    'ALTER TABLE usage.events DISABLE TRIGGER usage_event_projection',
    'ALTER FUNCTION usage.project_event() SECURITY DEFINER',
    'ALTER FUNCTION usage.guard_projection() SET search_path=public',
])
def test_usage_schema_tamper_is_rejected_without_repair(usage_db,tamper):
    with psycopg.connect(usage_db) as connection:
        connection.execute(tamper)
        with pytest.raises(ValueError,match='Usage schema contract mismatch'):
            validate_usage_schema(connection)
        connection.rollback()
        assert validate_usage_schema(connection)['validated']


def test_runtime_ledger_accepts_only_exact_known_usage_extension(usage_db):
    from app.auth.isolation_ledger import validate_application_migration_ledger,validate_service_migration_ledger
    from app.migration_policy import validate_migration_manifest_entries
    entries=validate_migration_manifest_entries(ROOT/'backend/migrations/postgres')
    with psycopg.connect(usage_db,row_factory=dict_row) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS governance.schema_migrations(version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())')
        for item in entries:
            if item.version<'0021_usage_metering':
                connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)',(item.version,item.checksum))
        validate_application_migration_ledger(connection,require_usage=False)
        with pytest.raises(ValueError):validate_application_migration_ledger(connection,require_usage=True)
        latest=next(e for e in entries if e.version=='0021_usage_metering')
        connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)',(latest.version,latest.checksum))
        validate_application_migration_ledger(connection,require_usage=True)
        with pytest.raises(ValueError):validate_service_migration_ledger(connection)
        with connection.transaction(force_rollback=True):
            connection.execute('UPDATE governance.schema_migrations SET checksum=%s WHERE version=%s',('0'*64,latest.version))
            with pytest.raises(ValueError):validate_application_migration_ledger(connection,require_usage=True)
        with connection.transaction(force_rollback=True):
            connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('9999_unknown',%s)",('0'*64,))
            with pytest.raises(ValueError):validate_application_migration_ledger(connection,require_usage=False)
        connection.rollback()
