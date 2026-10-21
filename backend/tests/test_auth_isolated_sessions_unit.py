"""Offline auth policy/router units; no database or listening server is used.

The SQL recording fixture is only a unit boundary for native login parameters.
Real PG expiration/revocation regressions live in test_auth_isolation.py.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth.router import router
from app.auth.service import AuthService
from app.auth.settings import AuthSettings, ISOLATED_AB_COOKIE_NAME, IsolatedABSessionPolicy


A = '10000000-0000-0000-0000-000000000001'
B = '10000000-0000-0000-0000-000000000002'
C = '10000000-0000-0000-0000-000000000003'
ORIGIN = 'http://127.0.0.2:38965'
PROXY = '172.28.0.1'
NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_public_auth_parameters(monkeypatch):
    for key in ('AUTH_COOKIE_SECURE', 'AUTH_COOKIE_NAME', 'AUTH_DEV_HTTP_ORIGINS',
                'AUTH_ISOLATED_AB_SESSION_POLICY', 'AUTH_POSTGRES_DSN', 'APP_SERVICE_POSTGRES_DSN',
                'FORWARDED_ALLOW_IPS'):
        monkeypatch.delenv(key, raising=False)


def document(**changes):
    return dict(enabled=True, origin=ORIGIN, user_ids=[A, B], proxy_peer=PROXY) | changes


def configured(monkeypatch, *, data=None, environment='dev', origins=(ORIGIN,),
               cookie=ISOLATED_AB_COOKIE_NAME, forwarded_allow_ips=''):
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    monkeypatch.setenv('AUTH_COOKIE_NAME', cookie)
    monkeypatch.setenv('AUTH_ISOLATED_AB_SESSION_POLICY', json.dumps(document() if data is None else data))
    if forwarded_allow_ips is None:
        monkeypatch.delenv('FORWARDED_ALLOW_IPS', raising=False)
    else:
        monkeypatch.setenv('FORWARDED_ALLOW_IPS', forwarded_allow_ips)
    return AuthSettings.from_settings(SimpleNamespace(allowed_origins_list=origins,
        app_postgres_dsn='unused-synthetic-connection', gpu_broker_environment=environment))


def test_normal_settings_and_all_other_identities_remain_twelve_hours(monkeypatch):
    normal = AuthSettings.from_settings(SimpleNamespace(allowed_origins_list=('https://example.invalid',),
        app_postgres_dsn='unused-synthetic-connection', gpu_broker_environment='prod'))
    assert normal.session_hours == 12 and normal.isolated_ab_session_policy is None
    assert normal.new_session_max_age(A, peer='127.0.0.1', isolated_origin=ORIGIN) == 43200
    isolated = configured(monkeypatch)
    assert isolated.new_session_max_age(A, peer=PROXY, isolated_origin=ORIGIN) == 129600
    assert isolated.new_session_max_age(B, peer=PROXY, isolated_origin=ORIGIN) == 129600
    assert isolated.new_session_max_age(C, peer=PROXY, isolated_origin=ORIGIN) == 43200


@pytest.mark.parametrize('peer,origin', [
    (PROXY, None), (PROXY, 'http://127.0.0.1:36965'), ('127.0.0.1', ORIGIN),
    ('203.0.113.9', ORIGIN), ('localhost', ORIGIN), ('testclient', ORIGIN), ('', ORIGIN),
])
def test_unproven_entry_never_selects_thirty_six_hours(monkeypatch, peer, origin):
    assert configured(monkeypatch).new_session_max_age(A, peer=peer, isolated_origin=origin) == 43200


@pytest.mark.parametrize('changes', [
    {'enabled': False}, {'enabled': 1}, {'enabled': 'true'}, {'enabled': None},
    {'user_ids': []}, {'user_ids': [A]}, {'user_ids': [A, B, C]}, {'user_ids': [A, A]},
    {'user_ids': [A, 'not-a-user-id']}, {'user_ids': [A, '00000000-0000-0000-0000-000000000001']},
    {'user_ids': [A, '00000000-0000-0000-0000-000000000000']}, {'user_ids': [A, None]},
    {'user_ids': A}, {'user_ids': [A, {'id': B}]}, {'session_hours': 36}, {'other': True},
])
def test_invalid_or_expanded_public_policy_is_rejected(monkeypatch, changes):
    with pytest.raises(ValueError):
        configured(monkeypatch, data=document(**changes))


@pytest.mark.parametrize('raw', [
    '{}', '[]', 'null', '{', '{"enabled":true,"enabled":true,"origin":"http://127.0.0.2:36966","user_ids":[]}',
    '{"enabled":true,"origin":"http://127.0.0.2:36966"}', ' ' * 4097,
])
def test_policy_json_is_strict(raw):
    with pytest.raises(ValueError):
        IsolatedABSessionPolicy.from_json(raw)


@pytest.mark.parametrize('peer', ['0.0.0.0', '203.0.113.9', '172.28.0.0/16', 'localhost',
                                 '172.28.*', ['172.28.0.1'], None, 2130706433])
def test_proxy_peer_must_be_one_exact_local_address(monkeypatch, peer):
    with pytest.raises(ValueError):
        configured(monkeypatch, data=document(proxy_peer=peer))


@pytest.mark.parametrize('origin', [
    'http://127.0.0.1:36966', 'http://localhost:36966', 'http://[::1]:36966',
    'http://127.0.0.2:36965', 'http://127.0.0.2:80', 'http://127.0.0.2',
    'http://127.0.0.2:36966/', 'http://127.0.0.2:36966?q=1', 'http://127.0.0.2:36966#fragment',
    'http://user@127.0.0.2:36966', 'https://127.0.0.2:36966', 'http://203.0.113.9:36966',
    'http://127.0.0.2:99999', 'http://127.0.0.2:036966',
])
def test_origin_must_be_separate_canonical_loopback_entry(monkeypatch, origin):
    with pytest.raises(ValueError):
        configured(monkeypatch, data=document(origin=origin), origins=(origin,))


@pytest.mark.parametrize('changes', [
    {'environment': 'prod'}, {'environment': ''}, {'origins': (ORIGIN, 'http://127.0.0.1:36965')},
    {'origins': ('http://127.0.0.1:36965',)}, {'cookie': 'nexpoly_dev_session'}, {'cookie': 'nexpoly_session'},
])
def test_policy_cannot_widen_existing_runtime_or_cookie(monkeypatch, changes):
    with pytest.raises(ValueError):
        configured(monkeypatch, **changes)


def test_direct_settings_cannot_bypass_policy_guards(monkeypatch):
    original = configured(monkeypatch)
    for changes in ({'runtime_environment': 'prod'}, {'cookie_secure': True}, {'session_hours': 36},
                    {'allowed_origins': ()}, {'cookie_name': 'nexpoly_dev_session'},
                    {'isolated_ab_session_policy': document()}):
        with pytest.raises(ValueError):
            replace(original, **changes)
    # Preserve explicit legacy constructor behavior when no isolated policy is
    # configured; from_settings still supplies its unchanged 12-hour default.
    legacy = AuthSettings('unused', 'unused', 'unused', session_hours=6)
    assert legacy.new_session_max_age(A, peer=PROXY, isolated_origin=ORIGIN) == 21600


@pytest.mark.parametrize('trusted', [None, '127.0.0.1', '*', PROXY, ' '])
def test_isolated_policy_requires_explicitly_empty_proxy_trust(monkeypatch, trusted):
    with pytest.raises(ValueError, match='proxy-header trust'):
        configured(monkeypatch, forwarded_allow_ips=trusted)


def test_default_runtime_keeps_existing_proxy_trust_configuration(monkeypatch):
    monkeypatch.setenv('FORWARDED_ALLOW_IPS', '127.0.0.1')
    settings = AuthSettings.from_settings(SimpleNamespace(allowed_origins_list=('https://example.invalid',),
        app_postgres_dsn='unused-synthetic-connection', gpu_broker_environment='prod'))
    assert settings.session_hours == 12 and settings.isolated_ab_session_policy is None


class LoginSQLFixture:
    """Records native SQL parameters in memory; never connects to PostgreSQL."""

    def __init__(self):
        self.users = {name: {'user_id': owner, 'username': name, 'password_hash': 'unit-verifier',
                            'status': 'active', 'is_system': False, 'must_change_password': True}
                      for name, owner in (('fixture-a', A), ('fixture-b', B), ('fixture-c', C))}
        self.inserts = []

    def execute(self, statement, parameters):
        if statement.startswith('SELECT * FROM auth.users WHERE username='):
            row = self.users.get(parameters[0])
        elif statement.startswith('SELECT * FROM auth.users WHERE user_id='):
            row = next(item for item in self.users.values() if item['user_id'] == parameters[0])
        else:
            assert statement.startswith('INSERT INTO auth.sessions(')
            self.inserts.append(parameters)
            row = None
        return SimpleNamespace(fetchone=lambda: None if row is None else dict(row))


@pytest.fixture
def native_login(monkeypatch):
    from app.auth import service as module
    auth = AuthService(configured(monkeypatch))
    sql = LoginSQLFixture()

    @contextmanager
    def connection(**kwargs):
        yield sql

    monkeypatch.setattr(auth, 'connection', connection)
    monkeypatch.setattr(module, 'verify_password', lambda encoded, supplied: supplied == 'unit-password')
    monkeypatch.setattr(module.secrets, 'token_urlsafe', lambda length: 'unit-session-marker')
    monkeypatch.setattr(module, 'datetime', SimpleNamespace(now=lambda zone: NOW))
    return auth, sql


@pytest.mark.parametrize('name,origin,expected', [
    ('fixture-a', ORIGIN, 129600), ('fixture-b', ORIGIN, 129600),
    ('fixture-c', ORIGIN, 43200), ('fixture-a', None, 43200),
])
def test_native_login_uses_one_bounded_ttl_for_insert_and_cookie_data(native_login, name, origin, expected):
    auth, sql = native_login
    before = deepcopy(sql.users)
    session, _ = auth.login(name, 'unit-password', PROXY, isolated_origin=origin)
    assert session['session_max_age'] == expected
    assert session['expires_at'] == sql.inserts[-1][3] == NOW + timedelta(seconds=expected)
    assert len(sql.inserts) == 1 and sql.users == before


class DirectRequestFixture:
    def __init__(self, app, *, peer=PROXY, server=('0.0.0.0', 8000)):
        self.app, self.peer, self.server = app, peer, server

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            scope = {**scope, 'client': (self.peer, 12345), 'server': self.server}
        return await self.app(scope, receive, send)


def login_client(native_login, **network):
    auth, sql = native_login
    app = FastAPI()
    app.state.auth = auth
    app.include_router(router)
    native_proxy = ProxyHeadersMiddleware(app, trusted_hosts='')
    return TestClient(DirectRequestFixture(native_proxy, **network), base_url=ORIGIN), sql


@pytest.mark.parametrize('name,expected', [('fixture-a', 129600), ('fixture-b', 129600), ('fixture-c', 43200)])
def test_native_router_cookie_ttl_matches_insert_and_keeps_host_isolation(native_login, name, expected):
    client, sql = login_client(native_login)
    response = client.post('/api/v1/auth/login', json={'username': name, 'password': 'unit-password'},
                           headers={'Origin': ORIGIN})
    assert response.status_code == 200
    header = response.headers['set-cookie']
    assert header.startswith(ISOLATED_AB_COOKIE_NAME + '=')
    assert f'Max-Age={expected}' in header and 'HttpOnly' in header and 'SameSite=lax' in header
    assert 'Domain=' not in header and sql.inserts[-1][3] == NOW + timedelta(seconds=expected)
    for old_origin in ('http://127.0.0.1:36965', 'http://localhost:36965'):
        request = httpx.Request('GET', old_origin + '/api/v1/auth/session')
        client.cookies.set_cookie_header(request)
        assert ISOLATED_AB_COOKIE_NAME not in request.headers.get('cookie', '')


@pytest.mark.parametrize('network,headers', [
    ({'peer': '203.0.113.9'}, {}), ({'peer': 'testclient'}, {}),
    ({'peer': '172.28.0.2'}, {}), ({'peer': '127.0.0.1'}, {}),
    ({}, {'Host': '127.0.0.1:36965'}), ({}, {'Origin': 'http://127.0.0.1:36965'}),
    ({}, {'Origin': ''}), ({'peer': '203.0.113.9'}, {'Forwarded': 'for=172.28.0.1'}),
    ({'peer': '203.0.113.9'}, {'X-Forwarded-For': PROXY}),
    ({'peer': '203.0.113.9'}, {'X-Real-IP': PROXY}),
    ({'peer': '127.0.0.1'}, {'X-Forwarded-For': PROXY, 'X-Forwarded-Proto': 'http'}),
])
def test_untrusted_entry_rejected_before_native_login_or_session_insert(native_login, network, headers):
    client, sql = login_client(native_login, **network)
    response = client.post('/api/v1/auth/login', json={'username': 'fixture-a', 'password': 'unit-password'},
                           headers={'Origin': ORIGIN, **headers})
    assert response.status_code == 403 and not sql.inserts
    assert 'set-cookie' not in response.headers


def test_duplicate_origin_or_host_is_rejected_before_login(native_login):
    for duplicate in ('Origin', 'Host'):
        client, sql = login_client(native_login)
        headers = [('Origin', ORIGIN), (duplicate, ORIGIN if duplicate == 'Origin' else '127.0.0.2:38965')]
        if duplicate == 'Host':
            headers.append(('Host', '127.0.0.2:38965'))
        response = client.post('/api/v1/auth/login', json={'username': 'fixture-a', 'password': 'unit-password'},
                               headers=headers)
        assert response.status_code == 403 and not sql.inserts


def test_native_docker_peer_and_forwarded_metadata_do_not_need_loopback_asgi_listener(native_login):
    client, sql = login_client(native_login, server=('0.0.0.0', 8000))
    response = client.post('/api/v1/auth/login', json={'username': 'fixture-a', 'password': 'unit-password'},
        headers={'Origin': ORIGIN, 'Forwarded': 'for=203.0.113.9;proto=https',
                 'X-Forwarded-For': '203.0.113.9', 'X-Forwarded-Proto': 'https'})
    assert response.status_code == 200 and 'Max-Age=129600' in response.headers['set-cookie']
    assert len(sql.inserts) == 1
