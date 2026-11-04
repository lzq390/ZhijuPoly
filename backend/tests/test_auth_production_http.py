"""Production HTTP entry contract; no database or listening server is used.

The in-memory identity fixture tests the real router/middleware transport.
Database revocation, owner filtering and RLS remain covered by auth isolation
integration tests. Cookie port sharing is recorded, not claimed to be fixed.
"""
from dataclasses import replace
from types import SimpleNamespace

import anyio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import httpx
import pytest

from app.auth.context import current_owner_id, is_service_context
from app.auth.middleware import AuthenticationMiddleware
from app.auth.router import router
from app.auth.service import AuthService
from app.auth.settings import (
    AuthSettings, PRODUCTION_HTTP_COOKIE_NAME, PRODUCTION_HTTP_HOST,
    PRODUCTION_HTTP_ORIGIN,
)


FORWARDED_ORIGINS = (
    'http://localhost:19000', 'http://127.0.0.1:19001', 'http://[::1]:19002',
    'http://forward.example:28080', 'http://192.168.1.2:19000',
)


@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch):
    for key in ('AUTH_COOKIE_SECURE', 'AUTH_COOKIE_NAME', 'AUTH_DEV_HTTP_ORIGINS',
                'AUTH_ISOLATED_AB_SESSION_POLICY', 'AUTH_ISOLATED_ACCEPTANCE_POLICY',
                'AUTH_PROD_HTTP_ENABLED', 'FORWARDED_ALLOW_IPS'):
        monkeypatch.delenv(key, raising=False)


def production_settings(monkeypatch, *, environment='prod', origins=(PRODUCTION_HTTP_ORIGIN,)):
    monkeypatch.setenv('AUTH_PROD_HTTP_ENABLED', 'true')
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    monkeypatch.setenv('AUTH_COOKIE_NAME', PRODUCTION_HTTP_COOKIE_NAME)
    return AuthSettings.from_settings(SimpleNamespace(
        allowed_origins_list=origins, app_postgres_dsn='unused', gpu_broker_environment=environment))


def test_production_http_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    monkeypatch.setenv('AUTH_COOKIE_NAME', PRODUCTION_HTTP_COOKIE_NAME)
    with pytest.raises(ValueError, match='loopback'):
        AuthSettings.from_settings(SimpleNamespace(allowed_origins_list=(PRODUCTION_HTTP_ORIGIN,),
            app_postgres_dsn='unused', gpu_broker_environment='prod'))
    configured = production_settings(monkeypatch)
    assert configured.production_http_enabled and not configured.cookie_secure
    assert configured.cookie_name == PRODUCTION_HTTP_COOKIE_NAME
    assert configured.session_hours == 12
    assert configured.isolated_ab_session_policy is None


@pytest.mark.parametrize('raw', ['', '1', 'yes', 'TRUE', ' true ', 'http://114.214.255.154:9000'])
def test_production_http_opt_in_is_a_literal_boolean(monkeypatch, raw):
    monkeypatch.setenv('AUTH_PROD_HTTP_ENABLED', raw)
    with pytest.raises(ValueError, match='AUTH_PROD_HTTP_ENABLED'):
        AuthSettings.from_settings(SimpleNamespace(allowed_origins_list=(),
            app_postgres_dsn='unused', gpu_broker_environment='prod'))


@pytest.mark.parametrize('environment,origins', [
    ('dev', (PRODUCTION_HTTP_ORIGIN,)), ('', (PRODUCTION_HTTP_ORIGIN,)),
    ('prod', ()), ('prod', ('http://114.214.255.154:9001',)),
    ('prod', ('http://114.214.255.154:9011',)),
    ('prod', ('https://114.214.255.154:9000',)),
    ('prod', ('http://114.214.255.154:9000/',)),
    ('prod', ('http://localhost:9000',)),
    ('prod', (PRODUCTION_HTTP_ORIGIN, 'http://localhost:9000')),
    ('prod', (PRODUCTION_HTTP_ORIGIN, PRODUCTION_HTTP_ORIGIN)),
])
def test_production_http_cannot_widen_environment_or_origins(monkeypatch, environment, origins):
    with pytest.raises(ValueError):
        production_settings(monkeypatch, environment=environment, origins=origins)


@pytest.mark.parametrize('changes', [
    {'runtime_environment': 'dev'}, {'cookie_secure': True},
    {'cookie_name': 'nexpoly_session'}, {'cookie_name': 'nexpoly_dev_session'},
    {'allowed_origins': ()}, {'production_http_enabled': 'true'},
    {'isolated_ab_session_policy': object()}, {'isolated_acceptance_policy': object()},
    {'acceptance_lab_dsns': ('first', 'second')},
])
def test_direct_settings_cannot_bypass_production_http_contract(monkeypatch, changes):
    with pytest.raises(ValueError):
        replace(production_settings(monkeypatch), **changes)


def test_production_http_cannot_use_development_origin_exception(monkeypatch):
    monkeypatch.setenv('AUTH_DEV_HTTP_ORIGINS', 'http://114.214.255.154:9001')
    with pytest.raises(ValueError, match='cannot use development'):
        production_settings(monkeypatch)


@pytest.mark.parametrize('secure,cookie', [
    ('true', PRODUCTION_HTTP_COOKIE_NAME), ('false', 'nexpoly_session'),
    ('false', 'nexpoly_dev_session'),
])
def test_environment_cannot_select_a_different_production_cookie(monkeypatch, secure, cookie):
    production_settings(monkeypatch)
    monkeypatch.setenv('AUTH_COOKIE_SECURE', secure)
    monkeypatch.setenv('AUTH_COOKIE_NAME', cookie)
    with pytest.raises(ValueError, match='exact production origin and cookie'):
        AuthSettings.from_settings(SimpleNamespace(allowed_origins_list=(PRODUCTION_HTTP_ORIGIN,),
            app_postgres_dsn='unused', gpu_broker_environment='prod'))


class IdentityFixture:
    """Small transport fixture; never represents database security evidence."""
    response = staticmethod(AuthService.response)
    identity = staticmethod(AuthService.identity)

    def __init__(self, settings):
        self.settings = settings
        self.sessions = {}
        self.login_calls = 0

    def login(self, username, password, peer, *, isolated_origin=None):
        self.login_calls += 1
        index = 1 if username == 'fixture-a' else 2
        session = {'user_id': f'10000000-0000-0000-0000-{index:012d}',
                   'username': username, 'session_id': f'session-{self.login_calls}',
                   'must_change_password': False, 'session_max_age': 43200}
        token = f'fixture-token-{self.login_calls}'
        self.sessions[token] = session
        return session, token

    def resolve(self, token):
        return self.sessions.get(token)

    def logout(self, session_id):
        self.sessions = {token: value for token, value in self.sessions.items()
                         if value['session_id'] != session_id}

    def assert_application_ready(self):
        pass


@pytest.fixture
def production_client(monkeypatch, request):
    origin = getattr(request, 'param', PRODUCTION_HTTP_ORIGIN)
    auth = IdentityFixture(production_settings(monkeypatch))
    app = FastAPI()
    app.state.auth = auth
    app.state.parsed = False
    app.include_router(router)

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.get('/api/v1/private')
    def private():
        return {'owner': current_owner_id(), 'service': is_service_context()}

    @app.post('/api/v1/upload')
    async def upload(request: Request):
        app.state.parsed = True
        return await request.json()

    app.add_middleware(AuthenticationMiddleware, auth_service=auth)
    # This Starlette TestClient version cannot parse IPv6 server netlocs.
    # Preserve the browser's actual Host/Origin while using its IPv4 transport.
    transport_origin = 'http://localhost:19002' if origin == 'http://[::1]:19002' else origin
    with TestClient(app, base_url=transport_origin) as client:
        client.headers['Host'] = origin.removeprefix('http://')
        yield client, auth, app


def login(client, username='fixture-a'):
    origin = 'http://' + client.headers['Host']
    result = client.post('/api/v1/auth/login', headers={'Origin': origin},
                         json={'username': username, 'password': 'transport-fixture'})
    assert result.status_code == 200
    session = result.json()
    return result, {'Origin': origin,
                    'X-Session-Context': session['session_id'], 'X-CSRF-Token': session['csrf_token']}


@pytest.mark.parametrize('production_client', [PRODUCTION_HTTP_ORIGIN, *FORWARDED_ORIGINS], indirect=True)
def test_http_login_refresh_and_cookie_contract(production_client):
    client, auth, _ = production_client
    result, _ = login(client)
    header = result.headers['set-cookie']
    assert header.startswith(PRODUCTION_HTTP_COOKIE_NAME + '=')
    assert 'HttpOnly' in header and 'SameSite=lax' in header and 'Path=/' in header
    assert 'Max-Age=43200' in header and 'Secure' not in header and 'Domain=' not in header
    session = client.get('/api/v1/auth/session')
    assert session.status_code == 200 and session.json()['authenticated']
    assert session.json()['session_id'] == result.json()['session_id']
    assert auth.login_calls == 1


@pytest.mark.parametrize('headers', [
    {}, {'Origin': 'http://114.214.255.154:9001'}, {'Origin': 'http://114.214.255.154:9011'},
    {'Origin': 'https://114.214.255.154:9000'},
    {'Origin': PRODUCTION_HTTP_ORIGIN, 'Host': '114.214.255.154:9001'},
    {'Origin': PRODUCTION_HTTP_ORIGIN, 'Host': '127.0.0.1:9000',
     'X-Forwarded-Host': PRODUCTION_HTTP_HOST, 'X-Forwarded-Proto': 'http'},
    {'Origin': PRODUCTION_HTTP_ORIGIN, 'Host': 'elsewhere.invalid',
     'Forwarded': f'host={PRODUCTION_HTTP_HOST};proto=http'},
])
def test_untrusted_login_is_rejected_before_credentials_or_body(production_client, headers):
    client, auth, app = production_client
    result = client.post('/api/v1/auth/login', headers=headers, content=b'not-json')
    assert result.status_code == 403
    assert auth.login_calls == 0 and not app.state.parsed
    assert 'set-cookie' not in result.headers


@pytest.mark.parametrize('duplicate', ['host', 'origin'])
def test_duplicate_entry_headers_are_rejected(production_client, duplicate):
    client, auth, _ = production_client
    headers = [('host', PRODUCTION_HTTP_HOST), ('origin', PRODUCTION_HTTP_ORIGIN),
               (duplicate, PRODUCTION_HTTP_HOST if duplicate == 'host' else PRODUCTION_HTTP_ORIGIN)]
    result = client.post('/api/v1/auth/login', headers=headers,
                         json={'username': 'fixture-a', 'password': 'transport-fixture'})
    assert result.status_code == 403 and auth.login_calls == 0


@pytest.mark.parametrize('host', [
    '', 'localhost:19000/path', 'localhost:19000?query', 'localhost:19000#fragment',
    'user@localhost:19000', 'localhost:invalid', 'localhost:65536',
    'localhost:19000,elsewhere.invalid', 'localhost:19000 elsewhere.invalid',
    'http://localhost:19000', 'localhost:19000\\elsewhere.invalid',
    '[::1', '::1:19000',
])
def test_malformed_hosts_cannot_read_or_login(production_client, host):
    client, auth, _ = production_client
    assert client.get('/api/v1/auth/session', headers={'Host': host}).status_code == 403
    result = client.post('/api/v1/auth/login', headers={'Host': host, 'Origin': f'http://{host}'},
                         content=b'not-json')
    assert result.status_code == 403 and auth.login_calls == 0


def test_session_requires_exactly_one_actual_host_header(production_client):
    client, _, app = production_client
    duplicate = client.get('/api/v1/auth/session', headers=[('Host', PRODUCTION_HTTP_HOST),
                                                          ('Host', PRODUCTION_HTTP_HOST)])
    assert duplicate.status_code == 403
    # TestClient inserts Host automatically; invoke the same ASGI app without
    # that header to exercise a genuinely missing entry identity.
    messages = []

    async def receive():
        return {'type': 'http.request', 'body': b''}

    async def send(message):
        messages.append(message)

    anyio.run(app, {'type': 'http', 'method': 'GET', 'path': '/api/v1/auth/session',
                    'headers': [], 'query_string': b'', 'scheme': 'http'}, receive, send)
    assert messages[0]['type'] == 'http.response.start' and messages[0]['status'] == 403


@pytest.mark.parametrize('production_client', FORWARDED_ORIGINS, indirect=True)
def test_forwarded_entry_uses_actual_host_and_ignores_proxy_header_claims(production_client):
    client, auth, _ = production_client
    origin = 'http://' + client.headers['Host']
    headers = {'Origin': origin, 'Forwarded': 'host=elsewhere.invalid;proto=https',
               'X-Forwarded-Host': 'elsewhere.invalid', 'X-Forwarded-Proto': 'https'}
    assert client.post('/api/v1/auth/login', headers=headers,
                       json={'username': 'fixture-a', 'password': 'transport-fixture'}).status_code == 200
    assert client.get('/api/v1/auth/session', headers=headers).json()['authenticated']
    assert auth.login_calls == 1
    forged = {**headers, 'Origin': PRODUCTION_HTTP_ORIGIN,
              'X-Forwarded-Host': PRODUCTION_HTTP_HOST,
              'Forwarded': f'host={PRODUCTION_HTTP_HOST};proto=http'}
    assert client.post('/api/v1/auth/login', headers=forged, content=b'bad').status_code == 403
    assert auth.login_calls == 1


@pytest.mark.parametrize('production_client', [PRODUCTION_HTTP_ORIGIN, *FORWARDED_ORIGINS], indirect=True)
def test_private_requests_keep_csrf_context_owner_and_revocation(production_client):
    client, _, app = production_client
    assert client.get('/api/v1/private').status_code == 401
    first, headers = login(client)
    origin = headers['Origin']
    assert client.get('/api/v1/private', headers=headers).json() == {
        'owner': first.json()['user']['id'], 'service': False}
    assert client.post('/api/v1/upload', headers={'Origin': origin}, content=b'bad').status_code == 409
    assert client.post('/api/v1/upload', headers={**headers, 'X-CSRF-Token': 'wrong'}, content=b'bad').status_code == 403
    assert client.post('/api/v1/upload', headers={**headers, 'Origin': 'http://114.214.255.154:9001'}, content=b'bad').status_code == 403
    assert not app.state.parsed
    assert client.post('/api/v1/upload', headers=headers, json={'accepted': True}).status_code == 200
    assert client.post('/api/v1/auth/logout', headers=headers).status_code == 204
    assert client.get('/api/v1/private', headers=headers).status_code == 401
    assert client.get('/api/v1/auth/session').json()['authenticated'] is False


@pytest.mark.parametrize('production_client', [PRODUCTION_HTTP_ORIGIN, FORWARDED_ORIGINS[0]], indirect=True)
def test_old_tab_context_cannot_read_new_login_identity(production_client):
    client, _, _ = production_client
    _, old_headers = login(client)
    second, new_headers = login(client, 'fixture-b')
    assert client.get('/api/v1/private', headers=old_headers).status_code == 409
    assert client.get('/api/v1/private', headers=new_headers).json()['owner'] == second.json()['user']['id']


def test_session_accepts_a_forwarded_host_and_loopback_health_remains_available(production_client):
    client, _, _ = production_client
    login(client)
    session = client.get('/api/v1/auth/session', headers={'Host': '127.0.0.1:9000',
        'X-Forwarded-Host': PRODUCTION_HTTP_HOST})
    assert session.status_code == 200 and session.json()['authenticated']
    assert client.get('/health', headers={'Host': '127.0.0.1:19000'}).status_code == 200


def test_cookie_is_host_only_but_still_shared_with_other_ports(production_client):
    client, _, _ = production_client
    login(client)
    for port in (9001, 9011):
        request = httpx.Request('GET', f'http://114.214.255.154:{port}/api/v1/auth/session')
        client.cookies.set_cookie_header(request)
        assert PRODUCTION_HTTP_COOKIE_NAME + '=' in request.headers['cookie']
    other_host = httpx.Request('GET', 'http://elsewhere.invalid:9000/api/v1/auth/session')
    client.cookies.set_cookie_header(other_host)
    assert 'cookie' not in other_host.headers
