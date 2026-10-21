"""Actual ASGI + API-role PostgreSQL; only bounded business work is stubbed."""
from types import SimpleNamespace as NS
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth.context import Identity, user_context
from app.entitlement_middleware import EntitlementAccessMiddleware
from app.routers.entitlements import router as result_router
from app.services.entitlement_repository import EntitlementRepository
from task_usage import entitlements as core
from task_usage.entitlement_policy import example_policy
from test_entitlement_repository import enabled, entitlement_db, usage_db, connection
from test_entitlement_sync_spec import mount_predict, mount_query, mount_online, QUERY


@pytest.fixture
def api(entitlement_db,monkeypatch):
    owner,other=str(uuid4()),str(uuid4())
    with psycopg.connect(entitlement_db) as active:
        for identity in [owner,other]:
            active.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,'!',false)",
                           (identity,'sync-pg-'+uuid4().hex[:16]))
    policy=example_policy();policy['version']='sync-pg-'+uuid4().hex
    policy['tiers']['member']['pools']['chemistry_small']=1
    policy['active_operations_per_owner']=1
    with connection(entitlement_db,owner,'nexpoly_service') as active:
        core.install_policy(active,policy)
    dsn=make_conninfo(entitlement_db,options='-c role=nexpoly_api')
    repo=EntitlementRepository(dsn)
    app=FastAPI()
    app.state.entitlement_repository=repo;app.state.entitlement_policy=policy
    app.state.entitlement_observation_degraded=False
    app.add_middleware(EntitlementAccessMiddleware,repository=repo,policy=policy)
    predict_calls=mount_predict(app,monkeypatch)
    query_calls=mount_query(app,monkeypatch)
    app.include_router(result_router)
    with user_context(Identity(owner)),TestClient(app,raise_server_exceptions=False) as client:
        assert repo.resolve_access_tier(owner_user_id=owner)=="member"
        yield NS(app=app,repo=repo,client=client,owner=owner,other=other,policy=policy,
                 dsn=entitlement_db,api_dsn=dsn,predict_calls=predict_calls,query_calls=query_calls)


def predict(api,key='stable-predict'):
    return api.client.post('/api/v1/predict',json={'smiles':'CC','properties':['Tg']},
                           headers={'Idempotency-Key':key})


def counts(api):
    value=api.repo.summary(owner_user_id=api.owner,policy_version=api.policy['version'])
    return value,{row['pool_id']:row for row in value['pools']}


def durable_counts(api):
    with connection(api.dsn,api.owner) as active:
        return tuple(active.execute('SELECT count(*) AS n FROM usage_entitlements.'+table+' WHERE owner_user_id=%s',
                                    (api.owner,)).fetchone()['n']
                     for table in ['operations','operation_events','request_observations'])


def test_real_sync_replay_has_one_execution_one_success_and_api_role(api):
    with psycopg.connect(api.api_dsn) as active:
        assert active.execute('SELECT current_user').fetchone()[0]=='nexpoly_api'
    first=predict(api);assert first.status_code==200,first.text
    operation_id=first.headers['x-usage-operation-id']
    for _ in range(5):
        response=predict(api)
        assert response.status_code==200 and response.headers['x-usage-replayed']=='true'
        assert response.json()==first.json() and response.headers['x-usage-operation-id']==operation_id
    summary,pools=counts(api)
    assert (summary['operation_count'],summary['execution_attempts'])==(1,1)
    assert (pools['chemistry_small']['success_used'],pools['chemistry_small']['reserved'])==(1,0)
    assert len(api.predict_calls)==1
    saved=api.repo.get(owner_user_id=api.owner,operation_id=operation_id)
    assert saved['state']=='succeeded' and saved['result_json']['payload']==first.json()
    requests=api.repo.list_request_counts(owner_user_id=api.owner)
    assert requests['request_count']==requests['finished_count']==6


def test_actual_small_pool_is_shared_by_predict_and_property_query(api):
    assert predict(api).status_code==200
    denied=api.client.post('/api/v1/query/smiles',json=QUERY,headers={'Idempotency-Key':'different-query'})
    assert denied.status_code==429,denied.text
    assert not api.query_calls
    structural=api.client.post('/api/v1/query/smiles',json={**QUERY,'match_mode':'structure'})
    assert structural.status_code==200 and len(api.query_calls)==1
    summary,pools=counts(api)
    assert summary['operation_count']==summary['execution_attempts']==1
    assert pools['chemistry_small']['success_used']==1


def test_owner_active_capacity_is_shared_across_online_and_small_pools(api,monkeypatch):
    calls=mount_online(api.app,monkeypatch,execution_facts=None)
    body={'material':'synthetic','max_papers':1}
    first=api.client.post('/api/v1/online-knowledge/search',json=body,headers={'Idempotency-Key':'unknown-online'})
    assert first.status_code==503,first.text
    assert predict(api).status_code==429
    replay=api.client.post('/api/v1/online-knowledge/search',json=body,headers={'Idempotency-Key':'unknown-online'})
    assert replay.status_code==409 and replay.json()['detail']['reservation_held']
    assert len(calls)==1 and not api.predict_calls
    summary,pools=counts(api)
    assert summary['operation_count']==summary['execution_attempts']==1
    assert (pools['online']['reserved'],pools['online']['unknown'],pools['online']['success_used'])==(1,1,0)
    assert pools['chemistry_small']['reserved']==pools['chemistry_small']['success_used']==0


def test_owner_cannot_read_another_durable_result_through_actual_get_routes(api):
    made=predict(api);assert made.status_code==200
    operation_id=made.headers['x-usage-operation-id']
    path='/api/v1/usage/operations/'+operation_id
    own=api.client.get(path+'/result')
    assert own.status_code==200 and own.json()['result']['payload']==made.json()
    with user_context(Identity(api.other)):
        for endpoint in [path,path+'/result']:
            response=api.client.get(endpoint)
            assert response.status_code==404,response.text
        assert api.client.get('/api/v1/usage/operations').json()['items']==[]
        assert api.repo.read_saved_operation(owner_user_id=api.owner,operation_id=operation_id) is None
    assert api.client.get(path).json()['state']=='succeeded'


def test_post_execution_settle_failure_rolls_back_result_and_count_then_holds_unknown(api,monkeypatch):
    original=core.settle;seen=[]
    def fail_after_sql(active,**values):
        result=original(active,**values)
        assert result['operation']['state']=='succeeded'
        previous=api.repo.get(owner_user_id=api.owner,operation_id=values['operation_id'])
        _,pools=counts(api)
        seen.append((previous['state'],previous['result_json'],pools['chemistry_small']['success_used'],
                     pools['chemistry_small']['reserved']))
        raise RuntimeError('injected sync settlement transaction rollback')
    monkeypatch.setattr(core,'settle',fail_after_sql)
    failed=predict(api);assert failed.status_code==500
    assert seen==[('running',None,0,1)]
    saved=api.repo.list_operations(owner_user_id=api.owner)
    assert len(saved)==1 and saved[0]['state']=='unknown' and saved[0]['result_json'] is None
    summary,pools=counts(api)
    assert summary['execution_attempts']==1
    assert (pools['chemistry_small']['success_used'],pools['chemistry_small']['reserved'])==(0,1)
    monkeypatch.setattr(core,'settle',original)
    replay=predict(api)
    assert replay.status_code==409 and replay.json()['detail']['reservation_held']
    result=api.client.get('/api/v1/usage/operations/'+saved[0]['operation_id']+'/result')
    assert result.status_code==409 and len(api.predict_calls)==1


def test_collection_off_keeps_saved_get_private_without_new_reservations(api,monkeypatch):
    made=predict(api);assert made.status_code==200
    operation_id=made.headers['x-usage-operation-id']
    before=durable_counts(api)
    monkeypatch.setenv('USAGE_ENTITLEMENTS_ENABLED','false')
    def forbidden(*args,**kwargs):raise AssertionError('read-only history must not reserve')
    monkeypatch.setattr(core,'reserve',forbidden)
    api.app.state.entitlement_policy=None
    path='/api/v1/usage/operations/'+operation_id
    assert api.client.get(path).status_code==200
    assert api.client.get(path+'/result').json()['result']['payload']==made.json()
    assert len(api.client.get('/api/v1/usage/operations').json()['items'])==1
    assert api.client.get('/api/v1/usage/entitlements').json()['enabled'] is False
    with user_context(Identity(api.other)):
        assert api.client.get(path+'/result').status_code==404
    assert durable_counts(api)==before and len(api.predict_calls)==1
