"""Finite acceptance authorization and real middleware/route boundaries.

No database, provider, GPU, login, or listening server is used by these units.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from app.auth.acceptance import (APPROVED_AT, DEADLINE, LAB_DATABASES, ORIGIN, OWNERS,
                                 IsolatedAcceptancePolicy, entry_matches)
from app.auth.middleware import AuthenticationMiddleware
from app.auth.policy import capabilities
from app.auth.service import csrf_token
from app.auth.settings import AuthSettings, ISOLATED_AB_COOKIE_NAME, IsolatedABSessionPolicy
from app.routers.dev_gpu_session import router as gpu_router
from app.routers.lab_data import router as lab_router


NOW = APPROVED_AT + timedelta(minutes=10)
PROXY = "172.28.0.1"


@pytest.fixture(autouse=True)
def fixed_policy_clock(monkeypatch):
    monkeypatch.setattr("app.auth.acceptance.utc_now", lambda: NOW)


def configured(features=("gpu_status",), **changes):
    policy = IsolatedAcceptancePolicy("acceptance-a-20261019", frozenset(features), DEADLINE)
    dsns = tuple("postgresql://unit@invalid/" + name for name in LAB_DATABASES) if "lab_data" in features else None
    return replace(AuthSettings("unused", "unused", "unused", cookie_name=ISOLATED_AB_COOKIE_NAME,
        cookie_secure=False, runtime_environment="dev", allowed_origins=(ORIGIN,),
        isolated_ab_session_policy=IsolatedABSessionPolicy(True, ORIGIN, OWNERS, PROXY),
        isolated_acceptance_policy=policy, acceptance_lab_dsns=dsns), **changes)


def test_capabilities_are_finite_and_never_operations_or_lab_workflow():
    settings = configured(("lab_data", "gpu_status"))
    policy = settings.isolated_acceptance_policy
    for owner in OWNERS:
        value = capabilities(True, acceptance=policy, owner_id=owner)
        assert value['lab.data.write'] and value['dev_gpu.status.read']
        assert not value['operations.use'] and not value['lab_workflow.use']
        assert not capabilities(False, acceptance=policy, owner_id=owner)['lab.use']
        assert policy.allows(owner, 'lab.use', now=DEADLINE - timedelta(microseconds=1))
        assert not policy.allows(owner, 'lab.use', now=DEADLINE)
        assert not policy.allows(owner, 'lab.use', now=APPROVED_AT - timedelta(microseconds=1))
    assert not capabilities(True, acceptance=policy, owner_id='other-owner')['lab.use']
    assert not capabilities(True)['dev_gpu.status.read']


@pytest.mark.parametrize('changes', [
    {'runtime_environment': 'prod'}, {'cookie_name': 'nexpoly_session'},
    {'allowed_origins': (ORIGIN, 'http://127.0.0.1:36965')},
    {'isolated_ab_session_policy': None},
    {'acceptance_lab_dsns': ('postgresql://unit@invalid/production', 'postgresql://unit@invalid/production')},
])
def test_scope_cannot_select_other_runtime_or_lab_database(changes):
    with pytest.raises(ValueError):
        configured(('lab_data',), **changes)


@pytest.mark.parametrize('raw', [
    '{}', 'null', '[]',
    '{"scope_id":"acceptance-a-20261019","features":["gpu_status","gpu_status"],"expires_at":"2026-10-20T18:53:34Z"}',
    '{"scope_id":"acceptance-a-20261019","features":["operations"],"expires_at":"2026-10-20T18:53:34Z"}',
    '{"scope_id":"acceptance-a-20261019","features":["gpu_status"],"expires_at":"2026-10-21T00:00:00Z"}',
    '{"scope_id":"other","features":["gpu_status"],"expires_at":"2026-10-20T18:53:34Z"}',
])
def test_public_policy_rejects_widening(raw):
    with pytest.raises(ValueError):
        IsolatedAcceptancePolicy.from_json(raw)


def app_fixture(*, owner=OWNERS[0], policy=True, features=('gpu_status',), changed=False):
    settings = configured(features) if policy else AuthSettings('unused', 'unused', 'unused', cookie_name=ISOLATED_AB_COOKIE_NAME,
        cookie_secure=False, allowed_origins=(ORIGIN,))
    session = {'session_id': 'unit-session', 'user_id': owner, 'must_change_password': changed}
    app = FastAPI()
    auth = SimpleNamespace(settings=settings, resolve=lambda token: session if token == 'unit' else None,
        assert_application_ready=lambda: None)
    from app.auth.context import Identity
    auth.identity = lambda value: Identity(str(value['user_id']), str(value['session_id']))
    app.state.auth = auth
    calls = []

    class Operator:
        def request(self, command):
            calls.append(command)
            return dict(schema_version=1, operator_available=True, phase='stopped',
                controller_status='stopped', can_recover=True, operation_id=None,
                message='unit status', source_sha='4'*40, source_tree='6'*40, updated_at=None)

    app.state.dev_gpu_operator_client = Operator()
    app.include_router(gpu_router)

    @app.post('/api/v1/lab-data/sample-measurements', status_code=201)
    def write_boundary(request: Request):
        calls.append('lab-write')
        return {'owner': str(request.state.auth_session['user_id'])}

    app.add_middleware(AuthenticationMiddleware, auth_service=auth)
    return app, calls


def client_for(app, *, host=ORIGIN, peer=PROXY):
    async def peer_app(scope, receive, send):
        if scope['type'] == 'http':
            scope = {**scope, 'client': (peer, 1234)}
        await app(scope, receive, send)
    client = TestClient(peer_app, base_url=host)
    client.cookies.set(ISOLATED_AB_COOKIE_NAME, 'unit')
    return client


def test_status_keeps_native_fields_and_recover_never_reaches_operator():
    app, calls = app_fixture()
    with client_for(app) as client:
        value = client.get('/api/v1/dev-gpu-session/status', headers={'X-Session-Context': 'unit-session'})
        assert value.status_code == 200 and value.json()['can_recover'] is True
        denied = client.post('/api/v1/dev-gpu-session/recover', headers={
            'Origin': ORIGIN, 'X-Session-Context': 'unit-session', 'X-CSRF-Token': csrf_token('unit')})
        assert denied.status_code == 403
    assert calls == ['status']


def test_scoped_route_retains_native_membership_and_closes_on_tier_failure():
    for tier in ['free', 'unavailable']:
        app, calls = app_fixture()
        def resolve(**kwargs):
            if tier == 'unavailable':
                raise RuntimeError('fixture unavailable')
            return tier
        app.state.entitlement_repository = SimpleNamespace(resolve_access_tier=resolve)
        with client_for(app) as client:
            response = client.get('/api/v1/dev-gpu-session/status', headers={'X-Session-Context': 'unit-session'})
            assert response.status_code == (503 if tier == 'unavailable' else 403)
        assert calls == []


def test_home_capability_remains_closed_without_native_owner_contract():
    settings = configured(('home',))
    assert not capabilities(True, acceptance=settings.isolated_acceptance_policy, owner_id=OWNERS[0])['home.embed']


@pytest.mark.parametrize('options', [
    {'owner': 'other-owner'}, {'policy': False}, {'features': ('lab_data',)}, {'changed': True},
])
def test_status_does_not_reach_operator_for_unapproved_identity(options):
    app, calls = app_fixture(**options)
    with client_for(app) as client:
        assert client.get('/api/v1/dev-gpu-session/status', headers={'X-Session-Context': 'unit-session'}).status_code == 403
    assert calls == []


@pytest.mark.parametrize('host,peer,extra', [
    ('http://127.0.0.1:38965', PROXY, {}), (ORIGIN, '203.0.113.7', {}),
    (ORIGIN, PROXY, {'X-Forwarded-Host': '127.0.0.2:38965'}),
    (ORIGIN, PROXY, {'Origin': 'https://other.invalid'}),
])
def test_entry_cannot_be_selected_by_host_peer_or_forwarded_header(host, peer, extra):
    app, calls = app_fixture()
    with client_for(app, host=host, peer=peer) as client:
        response = client.get('/api/v1/dev-gpu-session/status', headers={'X-Session-Context': 'unit-session', **extra})
        assert response.status_code == 403
    assert calls == []


def test_scoped_write_still_requires_session_context_and_csrf():
    app, calls = app_fixture(features=('lab_data',))
    with client_for(app) as client:
        path = '/api/v1/lab-data/sample-measurements'
        assert client.post(path, headers={'Origin': ORIGIN}).status_code == 409
        headers = {'Origin': ORIGIN, 'X-Session-Context': 'unit-session'}
        assert client.post(path, headers=headers).status_code == 403
        assert client.post(path, headers={**headers, 'X-CSRF-Token': csrf_token('unit')}).status_code == 201
    assert calls == ['lab-write']


def test_real_lab_get_routes_choose_owner_database_and_do_no_schema_writes():
    for index, owner in enumerate(OWNERS):
        settings = configured(('lab_data',))
        app = FastAPI()
        app.state.settings = SimpleNamespace(lab_data_postgres_dsn='forbidden-shared-database')
        app.state.auth = SimpleNamespace(settings=settings)
        queries = []
        seen = []

        class Connection:
            def execute(self, sql, params=None):
                queries.append(sql)
                assert sql.lstrip().upper().startswith('SELECT')
                if 'current_user AS role' in sql:
                    row = {'role': 'unit-api', 'required_member': True, 'privileged_membership': False,
                           'owns_private_schema': False, 'owns_private_table': False}
                elif 'AS excess' in sql:
                    row = {'excess': False}
                elif 'AS readable' in sql:
                    row = {'readable': False}
                else:
                    row = {'projects': 'lab.test_projects', 'measurements': 'lab.sample_measurements'} if 'to_regclass' in sql else {'count': index}
                return SimpleNamespace(fetchone=lambda: row)

        @contextmanager
        def connection(dsn, *, readonly_snapshot=False):
            seen.append((dsn, readonly_snapshot))
            yield Connection()

        app.state.postgres_connection_factory = connection

        @app.middleware('http')
        async def fixture_identity(request, call_next):
            request.state.auth_session = {'user_id': owner}
            return await call_next(request)

        app.include_router(lab_router)
        with TestClient(app) as client:
            response = client.get('/api/v1/lab-data/sample-measurements/count')
            assert response.status_code == 200 and response.json()['count'] == index
        assert seen == [(settings.acceptance_lab_dsns[index], True)]
        assert len(queries) == 5


def test_lab_runtime_rejects_privileged_maintenance_identity_before_data_queries():
    app=FastAPI();settings=configured(('lab_data',))
    app.state.settings=SimpleNamespace(lab_data_postgres_dsn='unused-shared')
    app.state.auth=SimpleNamespace(settings=settings)
    queries=[]
    class Connection:
        def execute(self,sql,params=None):
            queries.append(sql)
            assert 'current_user AS role' in sql
            return SimpleNamespace(fetchone=lambda:dict(role='unit-maintenance',required_member=True,
                privileged_membership=True,owns_private_schema=False,owns_private_table=False))
    @contextmanager
    def connection(*args,**kwargs):yield Connection()
    app.state.postgres_connection_factory=connection
    @app.middleware('http')
    async def identity(request,call_next):
        request.state.auth_session={'user_id':OWNERS[0]}
        return await call_next(request)
    app.include_router(lab_router)
    with TestClient(app) as client:
        response=client.get('/api/v1/lab-data/test-projects')
    assert response.status_code==503 and len(queries)==1
