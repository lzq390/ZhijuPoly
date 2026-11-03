"""Cross-device development checkout exercises real RLS and locked fulfillment."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.auth.policy import route_policy
from app.deployment_drain_middleware import DeploymentDrainMiddleware
from app.usage_catalog import match_usage_route
from test_billing_simulation import (
    auth_database, billing_db, client, create_order, register, settings, simulation_client,
)

PREVIEW = '/api/v1/billing/simulation/checkout'
CONFIRM = '/api/v1/billing/simulation/confirm'
ORIGIN = {'Origin': 'http://testserver'}
VIEW_KEYS = {'simulation', 'provider', 'amount_fen', 'currency', 'period', 'status',
             'order_suffix', 'expires_at', 'server_time', 'access_ends_at'}


def state(repo, order_id):
    with repo.connection(service=True) as connection:
        order = connection.execute('SELECT * FROM billing.orders WHERE id=%s', (order_id,)).fetchone()
        subscription = connection.execute('SELECT * FROM billing.subscriptions WHERE owner_user_id=%s',
                                          (order['owner_user_id'],)).fetchone()
        events = connection.execute('SELECT * FROM billing.payment_events WHERE order_id=%s ORDER BY event_key',
                                    (order_id,)).fetchall()
        return order, subscription, events


def expire(repo, order_id):
    with repo.connection(service=True) as connection:
        connection.execute("""UPDATE billing.orders SET created_at=clock_timestamp()-interval '20 minutes',
            expires_at=clock_timestamp()-interval '1 second' WHERE id=%s""", (order_id,))


@pytest.mark.parametrize('path', [PREVIEW, CONFIRM])
def test_mobile_routes_are_exact_public_observation(path):
    assert route_policy(path, 'POST') == 'public'
    assert route_policy(path + '/extra', 'POST') == 'member'
    assert route_policy(path, 'GET') == 'member'
    route = match_usage_route('POST', path)
    assert route.mode == 'observe' and route.module is None


@pytest.mark.parametrize('provider', ['wechat', 'alipay'])
def test_separate_phone_previews_without_mutation_then_confirms_same_desktop_order(simulation_client, provider, monkeypatch):
    desktop = simulation_client
    session = register(desktop)
    order = create_order(desktop, provider)
    billing = desktop.app.state.billing
    token = order['simulation_checkout_token']
    assert len(token) == 76 and token.startswith(UUID(order['id']).hex + '.')
    before = state(billing.repository, order['id'])
    with TestClient(desktop.app) as phone:
        # Even an unrelated/ambient browser cookie cannot make preview resolve a
        # login session; the capability is the only mobile authorization.
        phone.cookies.set(billing.repository.settings.cookie_name, 'ambient-cookie')
        with monkeypatch.context() as patch:
            original = desktop.app.state.auth.resolve
            def resolve_only_anonymous(value):
                assert value is None
                return original(value)
            patch.setattr(desktop.app.state.auth, 'resolve', resolve_only_anonymous)
            response = phone.post(PREVIEW, json={'token': token}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        view = response.json()
        assert set(view) == VIEW_KEYS
        assert view['status'] == 'pending' and view['provider'] == provider and view['amount_fen'] == 19900
        assert view['simulation'] is True and view['period'] == 'month'
        assert token not in response.text and session['user']['id'] not in response.text
        assert session['user']['username'] not in response.text and order['id'] not in response.text
        assert 'no-store' in response.headers['cache-control']
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert state(billing.repository, order['id']) == before
        assert desktop.get('/api/v1/private').status_code == 403
        paid = phone.post(CONFIRM, json={'token': token}, headers=ORIGIN)
        assert paid.status_code == 200 and paid.json()['status'] == 'paid', paid.text
        repeated = phone.post(CONFIRM, json={'token': token}, headers=ORIGIN)
        assert repeated.json()['access_ends_at'] == paid.json()['access_ends_at']
        assert phone.post(PREVIEW, json={'token': token}, headers=ORIGIN).json()['status'] == 'paid'
    current = desktop.get('/api/v1/auth/session').json()
    assert current['account_type'] == 'member' and current['session_id'] == session['session_id']
    assert desktop.get('/api/v1/private').status_code == 200
    polled = desktop.get('/api/v1/billing/orders/' + order['id']).json()
    assert polled['status'] == 'paid' and polled['simulation_checkout_token'] is None
    assert len(state(billing.repository, order['id'])[2]) == 1


@pytest.mark.parametrize('path', [PREVIEW, CONFIRM])
def test_mobile_requests_require_origin_json_small_body_and_only_token(simulation_client, path):
    desktop = simulation_client
    register(desktop)
    order = create_order(desktop)
    token = order['simulation_checkout_token']
    repo = desktop.app.state.billing.repository
    before = state(repo, order['id'])
    with TestClient(desktop.app) as phone:
        assert phone.post(path, json={'token': token}).status_code == 403
        assert phone.post(path, json={'token': token}, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
        assert phone.post(path, json={'token': token}, headers={'Origin': 'null'}).status_code == 403
        assert phone.post(path, content='{"token":"' + token + '"}', headers={**ORIGIN, 'Content-Type': 'text/plain'}).status_code == 415
        for extra in ({'amount_fen': 1}, {'provider': 'alipay'}, {'owner_user_id': str(uuid4())}):
            response = phone.post(path, json={'token': token, **extra}, headers=ORIGIN)
            assert response.status_code == 400 and token not in response.text
        response = phone.post(path, content='{"token":"' + token, headers={**ORIGIN, 'Content-Type': 'application/json'})
        assert response.status_code == 400 and token not in response.text
        assert phone.post(path, json={'token': 'a' * 1024}, headers=ORIGIN).status_code == 413
        # The receive bound also applies without a Content-Length header.
        response = phone.post(path, content=iter([b'{"token":"', b'a' * 1024, b'"}']),
                              headers={**ORIGIN, 'Content-Type': 'application/json'})
        assert response.status_code == 413
        assert phone.post(path + '?token=' + token, json={}, headers=ORIGIN).status_code == 400
        assert phone.get(path).status_code in {401, 405}
    assert state(repo, order['id']) == before


def test_mobile_capability_cannot_unlock_another_order_or_a_real_payment(simulation_client):
    desktop = simulation_client
    session = register(desktop)
    first, other = create_order(desktop), create_order(desktop, 'alipay')
    billing = desktop.app.state.billing
    repo = billing.repository
    real = repo.create_order(session['user']['id'], 'wechat', 19900, uuid4().hex,
                             merchant_id='real-test-merchant', app_id='real-test-app')
    real_checkout = billing.gateway.create_order('wechat', real['id'], 19900, 'test', real['expires_at'])
    # Even a corrupted real order with a synthetic token remains ineligible.
    with repo.connection(service=True) as connection:
        connection.execute('UPDATE billing.orders SET checkout_data=%s WHERE id=%s', (Jsonb(real_checkout), real['id']))
    token = first['simulation_checkout_token']
    wrong_secret = token[:-1] + ('A' if token[-1] != 'A' else 'B')
    other_prefix = UUID(other['id']).hex + token[32:]
    with TestClient(desktop.app) as phone:
        for invalid in ('', str(uuid4()), wrong_secret, other_prefix, real_checkout['simulation_checkout_token'], '非ASCII'):
            for path in (PREVIEW, CONFIRM):
                response = phone.post(path, json={'token': invalid}, headers=ORIGIN)
                assert response.status_code == 404, response.text
        assert phone.post(CONFIRM, json={'token': token}, headers=ORIGIN).status_code == 200
    assert state(repo, other['id'])[0]['status'] == 'pending'
    assert state(repo, real['id'])[0]['status'] == 'pending'
    assert state(repo, other['id'])[2] == [] and state(repo, real['id'])[2] == []


@pytest.mark.parametrize('paid', [False, True])
def test_expiry_applies_to_preview_and_confirm_including_paid_replays(simulation_client, paid):
    desktop = simulation_client
    register(desktop)
    order = create_order(desktop)
    repo = desktop.app.state.billing.repository
    with TestClient(desktop.app) as phone:
        if paid:
            assert phone.post(CONFIRM, json={'token': order['simulation_checkout_token']}, headers=ORIGIN).status_code == 200
        expire(repo, order['id'])
        before = state(repo, order['id'])
        for path in (PREVIEW, CONFIRM):
            assert phone.post(path, json={'token': order['simulation_checkout_token']}, headers=ORIGIN).status_code == 410
    assert state(repo, order['id']) == before


def test_closed_and_disabled_simulation_cannot_fulfill(simulation_client):
    desktop = simulation_client
    register(desktop)
    order = create_order(desktop)
    billing = desktop.app.state.billing
    billing.repository.close_confirmed(order['id'])
    with TestClient(desktop.app) as phone:
        assert phone.post(PREVIEW, json={'token': order['simulation_checkout_token']}, headers=ORIGIN).json()['status'] == 'closed'
        assert phone.post(CONFIRM, json={'token': order['simulation_checkout_token']}, headers=ORIGIN).status_code == 409
        billing.settings = replace(billing.settings, simulation_enabled=False)
        for path in (PREVIEW, CONFIRM):
            assert phone.post(path, json={'token': order['simulation_checkout_token']}, headers=ORIGIN).status_code == 404
    assert state(billing.repository, order['id'])[2] == []


@pytest.mark.parametrize('change', ['expiry', 'token', 'owner_disabled', 'closed'])
def test_confirmation_rechecks_authorization_after_initial_lookup(simulation_client, billing_db, monkeypatch, change):
    desktop = simulation_client
    session = register(desktop)
    order = create_order(desktop)
    billing = desktop.app.state.billing
    repo = billing.repository
    fulfill = repo.fulfill
    def race(payment, **kwargs):
        if change == 'expiry':
            expire(repo, order['id'])
        elif change == 'token':
            replacement = billing.gateway.create_order('wechat', order['id'], 19900, 'test', None)
            with repo.connection(service=True) as connection:
                connection.execute('UPDATE billing.orders SET checkout_data=%s WHERE id=%s', (Jsonb(replacement), order['id']))
        elif change == 'closed':
            repo.close_confirmed(order['id'])
        else:
            with psycopg.connect(billing_db['admin']) as connection:
                connection.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (session['user']['id'],))
        return fulfill(payment, **kwargs)
    monkeypatch.setattr(repo, 'fulfill', race)
    with TestClient(desktop.app) as phone:
        response = phone.post(CONFIRM, json={'token': order['simulation_checkout_token']}, headers=ORIGIN)
        assert response.status_code == {'expiry': 410, 'token': 404, 'owner_disabled': 403, 'closed': 409}[change], response.text
    current, subscription, events = state(repo, order['id'])
    assert current['status'] != 'paid' and subscription is None and events == []


def test_parallel_phone_and_desktop_confirmations_fulfill_only_once(simulation_client):
    desktop = simulation_client
    session = register(desktop)
    order = create_order(desktop)
    billing = desktop.app.state.billing
    from app.auth.context import Identity, user_context
    def confirm(index):
        if index % 2:
            with TestClient(desktop.app) as phone:
                response = phone.post(CONFIRM, json={'token': order['simulation_checkout_token']}, headers=ORIGIN)
                assert response.status_code == 200, response.text
                return response.json()
        with user_context(Identity(session['user']['id']), billing.repository.settings):
            return billing.simulate_payment(session['user']['id'], order['id'])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(confirm, range(6)))
    current, subscription, events = state(billing.repository, order['id'])
    from datetime import datetime
    ends = [datetime.fromisoformat(r['access_ends_at'].replace('Z', '+00:00'))
            if isinstance(r['access_ends_at'], str) else r['access_ends_at'] for r in results]
    assert all(end == subscription['paid_through'] for end in ends)
    assert current['status'] == 'paid' and len(events) == 1


def test_owner_refresh_adds_one_stable_token_to_legacy_pending_only_and_respects_drain(simulation_client, monkeypatch):
    desktop = simulation_client
    register(desktop)
    pending, paid = create_order(desktop), create_order(desktop, 'alipay')
    billing = desktop.app.state.billing
    assert desktop.post('/api/v1/billing/orders/' + paid['id'] + '/simulate-payment').status_code == 200
    with billing.repository.connection(service=True) as connection:
        connection.execute("UPDATE billing.orders SET checkout_data=%s,last_checked_at=clock_timestamp() WHERE id=ANY(%s::uuid[])",
                           (Jsonb({'simulation': True}), [pending['id'], paid['id']]))
    from app.services import deployment_control
    billing.application_settings = SimpleNamespace(deployment_drain_enabled=True)
    billing.write_tracker = deployment_control.InflightApiWriteTracker()
    monkeypatch.setattr(deployment_control, 'get_drain_state', lambda connection: SimpleNamespace(enabled=True))
    assert desktop.get('/api/v1/billing/orders/' + pending['id']).json()['simulation_checkout_token'] is None
    assert billing.write_tracker.active == 0
    monkeypatch.setattr(deployment_control, 'get_drain_state', lambda connection: SimpleNamespace(enabled=False))
    first = desktop.get('/api/v1/billing/orders/' + pending['id']).json()['simulation_checkout_token']
    assert first
    assert desktop.get('/api/v1/billing/orders/' + pending['id']).json()['simulation_checkout_token'] == first
    replacement = billing.gateway.create_order('wechat', pending['id'], 19900, 'test', None)
    billing.repository.save_checkout(pending['id'], replacement)
    assert desktop.get('/api/v1/billing/orders/' + pending['id']).json()['simulation_checkout_token'] == first
    assert desktop.get('/api/v1/billing/orders/' + paid['id']).json()['simulation_checkout_token'] is None
    assert state(billing.repository, paid['id'])[0]['checkout_data'] == {'simulation': True}
    assert billing.write_tracker.active == 0


def test_mobile_confirm_passes_deployment_write_admission(simulation_client, monkeypatch):
    desktop = simulation_client
    register(desktop)
    order = create_order(desktop)
    outer = FastAPI()
    outer.state.settings = SimpleNamespace(deployment_drain_enabled=True, app_postgres_dsn='unused-test-dsn')
    outer.mount('/', desktop.app)
    outer.add_middleware(DeploymentDrainMiddleware)
    from app import deployment_drain_middleware
    monkeypatch.setattr(deployment_drain_middleware, '_read_drain_state',
                        lambda factory, dsn: SimpleNamespace(enabled=True, reason='test-drain'))
    with TestClient(outer) as phone:
        response = phone.post(CONFIRM, json={'token': order['simulation_checkout_token']}, headers=ORIGIN)
        assert response.status_code == 503 and response.headers['Retry-After'] == '60'
    assert state(desktop.app.state.billing.repository, order['id'])[2] == []
    assert outer.state.inflight_api_writes.active == 0
