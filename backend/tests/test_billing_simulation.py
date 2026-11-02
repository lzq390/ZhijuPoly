"""Development simulation uses real auth, RLS and fulfillment on a disposable DB."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth.context import Identity, user_context
from app.auth.policy import route_policy
from app.auth.settings import AuthSettings
from app.billing.development import DevelopmentPaymentGateway, SIMULATION_MERCHANT_ID, SIMULATION_APP_ID
from app.billing.providers import PaymentError
from app.billing.repository import BillingError
from app.billing.settings import BillingSettings, BillingConfigurationError
from app.usage_catalog import match_usage_route
from test_billing_checkout import client, register
from test_billing_migration import billing_db, auth_database


def simulation_settings(**changes):
    return BillingSettings.from_env({
        'NEXPOLY_BILLING_SIMULATION_ENABLED': 'true',
        'NEXPOLY_BILLING_MONTHLY_PRICE_FEN': '19900',
        'NEXPOLY_BILLING_REGISTRATION_ENABLED': 'true',
        'GPU_BROKER_ENVIRONMENT': 'dev', 'AUTH_COOKIE_NAME': 'nexpoly_dev_simulation_session',
        **changes})


@pytest.fixture
def settings(billing_db):
    return AuthSettings(application_dsn=billing_db['api'], auth_dsn=billing_db['auth'],
        service_dsn=billing_db['service'], cookie_secure=False, runtime_environment='dev',
        cookie_name='nexpoly_dev_simulation_session', allowed_origins=('http://testserver',))


@pytest.mark.parametrize('changes', [
    {'GPU_BROKER_ENVIRONMENT': ''}, {'GPU_BROKER_ENVIRONMENT': 'prod'},
    {'AUTH_COOKIE_NAME': ''}, {'AUTH_COOKIE_NAME': 'nexpoly_session'},
    {'NEXPOLY_BILLING_ENABLED': 'true'}, {'NEXPOLY_BILLING_PROVIDERS': 'wechat'},
    {'NEXPOLY_BILLING_MONTHLY_PRICE_FEN': ''}, {'NEXPOLY_BILLING_MONTHLY_PRICE_FEN': '0'},
    {'NEXPOLY_BILLING_SIMULATION_ENABLED': 'maybe'},
])
def test_simulation_configuration_fails_closed(changes):
    with pytest.raises(BillingConfigurationError):
        simulation_settings(**changes)


def test_simulation_has_no_real_channels_or_payment_callback():
    configuration = simulation_settings()
    assert configuration.simulation_enabled and not configuration.enabled
    assert configuration.available_providers == ('wechat', 'alipay')
    assert configuration.wechat is None and configuration.alipay is None
    gateway = DevelopmentPaymentGateway(configuration)
    for provider in configuration.available_providers:
        order_id = uuid4().hex
        assert gateway.create_order(provider, order_id, 19900, 'development', None) == {'simulation': True}
        assert gateway.query_order(provider, order_id, expected_amount_fen=19900) == {'status': 'pending'}
        assert gateway.close_order(provider, order_id) == {'status': 'closed'}
        with pytest.raises(PaymentError, match='development_callbacks_disabled'):
            gateway.verify_notification(provider, {}, b'{"status":"paid"}')
    with pytest.raises(BillingConfigurationError):
        DevelopmentPaymentGateway(replace(configuration, enabled=True))


def test_simulation_endpoint_is_account_observation_only():
    path = '/api/v1/billing/orders/' + str(uuid4()) + '/simulate-payment'
    assert route_policy(path, 'POST') == 'account'
    route = match_usage_route('POST', path)
    assert route.mode == 'observe' and route.module is None
    assert route_policy(path, 'GET') == 'member'


@pytest.fixture
def simulation_client(client):
    billing = client.app.state.billing
    billing.settings = simulation_settings()
    billing.gateway = DevelopmentPaymentGateway(billing.settings)
    return client


def create_order(client, provider='wechat', key=None):
    response = client.post('/api/v1/billing/orders', json={'provider': provider},
                           headers={'Idempotency-Key': key or uuid4().hex})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize('provider', ['wechat', 'alipay'])
def test_simulation_signup_purchase_unlock_renewal_and_replay(simulation_client, provider, monkeypatch):
    client = simulation_client
    session = register(client)
    assert client.get('/api/v1/private').status_code == 403
    plans = client.get('/api/v1/billing/plans').json()
    assert plans['enabled'] and plans['simulation'] and plans['providers'] == ['wechat', 'alipay']
    key = uuid4().hex
    order = create_order(client, provider, key)
    assert order['simulation'] and order['qr_code'] is None and order['status'] == 'pending'
    def no_new_checkout(*args, **kwargs):
        raise AssertionError('A simulated order must not recreate a QR checkout')
    monkeypatch.setattr(client.app.state.billing.gateway, 'create_order', no_new_checkout)
    assert create_order(client, provider, key)['id'] == order['id']
    assert client.get('/api/v1/billing/orders/' + order['id']).json()['status'] == 'pending'
    path = '/api/v1/billing/orders/' + order['id'] + '/simulate-payment'
    paid = client.post(path)
    assert paid.status_code == 200, paid.text
    assert paid.json()['simulation'] and paid.json()['status'] == 'paid'
    assert client.post(path).json() == paid.json()
    current = client.get('/api/v1/auth/session').json()
    assert current['account_type'] == 'member' and current['session_id'] == session['session_id']
    assert client.get('/api/v1/private').status_code == 200
    repo = client.app.state.billing.repository
    with repo.connection(service=True) as connection:
        assert connection.execute('SELECT count(*) AS n FROM billing.payment_events WHERE order_id=%s', (order['id'],)).fetchone()['n'] == 1
    monkeypatch.undo()
    renewal = create_order(client, provider)
    renewed = client.post('/api/v1/billing/orders/' + renewal['id'] + '/simulate-payment').json()
    assert renewed['access_ends_at'] > paid.json()['access_ends_at']
    assert all(row['simulation'] for row in client.get('/api/v1/billing/orders').json()['items'])


def test_simulation_requires_owner_login_origin_and_csrf(simulation_client):
    client = simulation_client
    first_session = register(client)
    order = create_order(client)
    path = '/api/v1/billing/orders/' + order['id'] + '/simulate-payment'
    with TestClient(client.app) as visitor:
        assert visitor.post(path).status_code == 401
    assert client.post(path, headers={'X-CSRF-Token': 'invalid'}).status_code == 403
    assert client.post(path, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
    register(client)
    assert client.post(path).status_code == 404
    with client.app.state.billing.repository.connection(service=True) as connection:
        assert connection.execute('SELECT count(*) AS n FROM billing.subscriptions WHERE owner_user_id=%s', (first_session['user']['id'],)).fetchone()['n'] == 0


@pytest.mark.parametrize('state', ['expired', 'closed'])
def test_simulation_rejects_unpayable_orders_atomically(simulation_client, state):
    client = simulation_client
    session = register(client)
    order = create_order(client)
    repo = client.app.state.billing.repository
    with repo.connection(service=True) as connection:
        if state == 'expired':
            connection.execute("UPDATE billing.orders SET created_at=clock_timestamp()-interval '20 minutes',expires_at=clock_timestamp()-interval '5 minutes' WHERE id=%s", (order['id'],))
        else:
            connection.execute("UPDATE billing.orders SET status='closed' WHERE id=%s", (order['id'],))
    result = client.post('/api/v1/billing/orders/' + order['id'] + '/simulate-payment')
    assert result.status_code == 409 and result.json()['detail']['code'] == 'simulation_order_expired'
    assert client.get('/api/v1/auth/session').json()['account_type'] == 'free'
    assert client.get('/api/v1/billing/orders/' + order['id']).json()['status'] == 'closed'
    with repo.connection(service=True) as connection:
        assert connection.execute('SELECT count(*) AS n FROM billing.subscriptions WHERE owner_user_id=%s', (session['user']['id'],)).fetchone()['n'] == 0
        assert connection.execute('SELECT count(*) AS n FROM billing.payment_events WHERE order_id=%s', (order['id'],)).fetchone()['n'] == 0


def test_simulation_never_accepts_callbacks_or_real_orders(simulation_client):
    client = simulation_client
    session = register(client)
    billing = client.app.state.billing
    real = billing.repository.create_order(session['user']['id'], 'wechat', 19900, uuid4().hex,
        merchant_id='actual-merchant', app_id='actual-app')
    assert client.post('/api/v1/billing/orders/' + str(real['id']) + '/simulate-payment').status_code == 400
    for provider in ('wechat', 'alipay'):
        with TestClient(client.app) as caller:
            assert caller.post('/api/v1/billing/webhooks/' + provider, content=b'{"status":"paid"}').status_code == 400
    order = create_order(client)
    billing.settings = replace(billing.settings, simulation_enabled=False)
    assert client.get('/api/v1/billing/plans').json()['simulation'] is False
    assert client.post('/api/v1/billing/orders/' + order['id'] + '/simulate-payment').status_code == 404
    assert client.get('/api/v1/billing/orders').json()['items'][0]['simulation'] is True


def test_concurrent_simulation_clicks_extend_only_once(simulation_client):
    client = simulation_client
    session = register(client)
    order = create_order(client)
    billing = client.app.state.billing
    def pay():
        with user_context(Identity(session['user']['id']), billing.repository.settings):
            return billing.simulate_payment(session['user']['id'], order['id'])
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: pay(), range(4)))
    assert all(result['access_ends_at'] == results[0]['access_ends_at'] for result in results)
    with billing.repository.connection(service=True) as connection:
        assert connection.execute('SELECT count(*) AS n FROM billing.payment_events WHERE order_id=%s', (order['id'],)).fetchone()['n'] == 1


def test_simulated_order_cannot_be_fulfilled_as_a_real_payment(simulation_client):
    client = simulation_client
    register(client)
    order = create_order(client)
    with pytest.raises(BillingError, match='不支持此支付方式'):
        client.app.state.billing.repository.fulfill({'provider': order['provider'], 'order_id': order['id'],
            'amount_fen': order['amount_fen'], 'currency': 'CNY', 'status': 'paid',
            'transaction_id': 'a-real-looking-transaction', 'merchant_id': SIMULATION_MERCHANT_ID,
            'app_id': SIMULATION_APP_ID})
