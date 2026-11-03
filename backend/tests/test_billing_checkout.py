"""Real PostgreSQL purchase/registration flow with a synthetic payment boundary."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.context import Identity, user_context
from app.auth.middleware import AuthenticationMiddleware
from app.auth.router import router as auth_router
from app.auth.service import AuthService
from app.auth.settings import AuthSettings
from app.billing.repository import BillingError, BillingRepository, next_month
from app.billing.router import router, billing_error_handler
from app.billing.service import BillingService
from app.billing.providers import PaymentError
from app.entitlement_middleware import EntitlementAccessMiddleware
from app.services.entitlement_repository import EntitlementRepository
from test_billing_migration import billing_db, auth_database


@pytest.fixture
def settings(billing_db):
    return AuthSettings(application_dsn=billing_db['api'], auth_dsn=billing_db['auth'],
        service_dsn=billing_db['service'], cookie_secure=False, cookie_name='billing_test_session',
        allowed_origins=('http://testserver',))


@pytest.fixture
def owner(settings):
    session, _ = AuthService(settings).register('buyer-' + uuid4().hex, 'a-valid-test-password', 'test-peer')
    return str(session['user_id'])


def payment(order, **changes):
    return {"provider": order['provider'], "order_id": order['id'].hex,
        "status": "paid", "amount_fen": order['amount_fen'], "currency": "CNY",
        "transaction_id": "test-" + order['id'].hex, "merchant_id": "test-merchant", "app_id": "test-app", **changes}


def new_order(repo, owner, provider='wechat'):
    return repo.create_order(owner, provider, 19900, uuid4().hex, merchant_id='test-merchant', app_id='test-app')


def test_fulfillment_replays_once_and_preserves_renewal(settings, owner, monkeypatch):
    monkeypatch.setenv('USAGE_ENTITLEMENTS_ENABLED', 'false')
    repo = BillingRepository(settings)
    order = new_order(repo, owner)
    first = repo.fulfill(payment(order))
    replay = repo.fulfill(payment(order))
    assert first['access_ends_at'] == replay['access_ends_at']
    renewal = repo.fulfill(payment(new_order(repo, owner, 'alipay')))
    assert renewal['access_starts_at'] == first['access_ends_at']
    assert renewal['access_ends_at'] == next_month(first['access_ends_at'])
    with user_context(Identity(owner), settings):
        summary = repo.subscription(owner)
        assert summary['account_type'] == 'member' and summary['source'] == 'subscription'
        assert EntitlementRepository(settings.application_dsn).resolve_access_tier(owner_user_id=owner) == 'member'
    with repo.connection(service=True) as connection:
        assert connection.execute('SELECT count(*) AS n FROM billing.payment_events WHERE order_id=%s', (order['id'],)).fetchone()['n'] == 1
        assert connection.execute('SELECT 1 FROM usage_entitlements.operations WHERE owner_user_id=%s', (owner,)).fetchone() is None


@pytest.mark.parametrize('changes', [{'amount_fen': 1}, {'currency': 'USD'}, {'provider': 'alipay'}, {'status': 'pending'}, {'merchant_id': 'other'}, {'app_id': 'other'}])
def test_invalid_payment_cannot_grant_access(settings, owner, changes):
    repo = BillingRepository(settings)
    order = new_order(repo, owner)
    with pytest.raises(BillingError):
        repo.fulfill(payment(order, **changes))
    with user_context(Identity(owner), settings):
        assert repo.subscription(owner)['account_type'] == 'free'
        assert repo.get_order(owner, order['id'])['status'] == 'pending'


def test_parallel_payments_and_duplicate_callbacks_extend_exactly_twice(settings, owner):
    repo = BillingRepository(settings)
    a, b = new_order(repo, owner), new_order(repo, owner, 'alipay')
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(repo.fulfill, [payment(a), payment(b), payment(a), payment(b)]))
    windows = sorted({(row['access_starts_at'], row['access_ends_at']) for row in results})
    assert len(windows) == 2 and windows[1][0] == windows[0][1]
    with user_context(Identity(owner), settings):
        assert repo.subscription(owner)['expires_at'] == windows[1][1]


def test_one_payment_transaction_cannot_fulfill_another_order(settings, owner):
    repo = BillingRepository(settings)
    first, other = new_order(repo, owner), new_order(repo, owner)
    paid = repo.fulfill(payment(first))
    with pytest.raises(BillingError) as error:
        repo.fulfill(payment(other, transaction_id=paid['payment_id']))
    assert error.value.code == 'payment_conflict'
    with user_context(Identity(owner), settings):
        assert repo.get_order(owner, other['id'])['status'] == 'pending'
        assert repo.subscription(owner)['expires_at'] == paid['access_ends_at']


def test_order_price_is_frozen_and_identity_owner_scoped(settings, owner):
    repo = BillingRepository(settings)
    key = uuid4().hex
    merchant = {'merchant_id': 'test-merchant', 'app_id': 'test-app'}
    first = repo.create_order(owner, 'wechat', 19900, key, **merchant)
    assert repo.create_order(owner, 'wechat', 29900, key, **merchant)['amount_fen'] == 19900
    with pytest.raises(BillingError, match='另一种支付方式'):
        repo.create_order(owner, 'alipay', 19900, key, **merchant)
    other, _ = AuthService(settings).register('other-' + uuid4().hex, 'a-valid-test-password', 'other-peer')
    with user_context(Identity(str(other['user_id'])), settings):
        assert repo.list_orders(str(other['user_id'])) == []
        with pytest.raises(BillingError) as error:
            repo.get_order(str(other['user_id']), first['id'])
        assert error.value.status == 404


def test_late_paid_notification_fulfills_closed_order(settings, owner):
    repo = BillingRepository(settings)
    order = new_order(repo, owner)
    repo.close_confirmed(order['id'])
    result = repo.fulfill(payment(order))
    assert result['status'] == 'paid'


def test_expiry_preserves_login_and_order_data(settings, owner):
    repo = BillingRepository(settings)
    order = repo.fulfill(payment(new_order(repo, owner)))
    with repo.connection(service=True) as connection:
        connection.execute("UPDATE billing.subscriptions SET paid_through=clock_timestamp()-interval '1 second' WHERE owner_user_id=%s", (owner,))
    with user_context(Identity(owner), settings):
        assert repo.subscription(owner)['account_type'] == 'free'
        assert repo.get_order(owner, order['id'])['status'] == 'paid'
        assert EntitlementRepository(settings.application_dsn).resolve_access_tier(owner_user_id=owner) == 'free'


@pytest.mark.parametrize('start,end', [
    ('2026-01-31T04:00:00+00:00', '2026-02-28T04:00:00+00:00'),
    ('2027-12-31T04:00:00+00:00', '2028-01-31T04:00:00+00:00'),
    ('2028-01-31T16:30:00+00:00', '2028-02-29T16:30:00+00:00'),
])
def test_month_is_calendar_month_in_customer_timezone(start, end):
    assert next_month(datetime.fromisoformat(start)) == datetime.fromisoformat(end)


class Gateway:
    def __init__(self):
        self.payments = {}
        self.notifications = {}
        self.create_calls = 0
        self.query_calls = 0

    def create_order(self, provider, order_id, amount, description, expires):
        self.create_calls += 1
        return {'qr_code': 'weixin://test/' + order_id}

    def query_order(self, provider, order_id, **kwargs):
        self.query_calls += 1
        return self.payments.get(order_id, {'status': 'pending'})

    def verify_notification(self, provider, headers, body):
        if body not in self.notifications:
            raise PaymentError('payment_signature_invalid')
        return self.notifications[body]

    def close_order(self, *args):
        return {'status': 'closed'}


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setenv('USAGE_ENTITLEMENTS_ENABLED', 'false')
    app = FastAPI()
    app.state.auth = AuthService(settings)
    app.state.entitlement_repository = EntitlementRepository(settings.application_dsn)
    app.state.billing = BillingService(SimpleNamespace(enabled=True, registration_enabled=True,
        monthly_price_fen=19900, available_providers=('wechat', 'alipay'),
        wechat=SimpleNamespace(merchant_id='test-merchant', app_id='test-app'),
        alipay=SimpleNamespace(seller_id='test-merchant', app_id='test-app')), BillingRepository(settings), Gateway())
    app.add_exception_handler(BillingError, billing_error_handler)
    app.include_router(auth_router)
    app.include_router(router)
    app.add_middleware(EntitlementAccessMiddleware, repository=app.state.entitlement_repository)
    app.add_middleware(AuthenticationMiddleware, auth_service=app.state.auth)
    @app.get('/api/v1/private')
    def member_only():
        return {'ok': True}
    with TestClient(app) as test_client:
        yield test_client


def register(client):
    response = client.post('/api/v1/auth/register', json={'username': 'web-' + uuid4().hex, 'password': 'a-valid-test-password'}, headers={'Origin': 'http://testserver'})
    assert response.status_code == 201, response.text
    session = response.json()
    assert session['account_type'] == 'free'
    client.headers.update({'Origin': 'http://testserver', 'X-Session-Context': session['session_id'], 'X-CSRF-Token': session['csrf_token']})
    return session


def test_self_service_registration_payment_unlock_and_order_replay(client):
    old_session = register(client)
    assert client.get('/api/v1/private').status_code == 403
    key = uuid4().hex
    response = client.post('/api/v1/billing/orders', json={'provider': 'wechat'}, headers={'Idempotency-Key': key})
    assert response.status_code == 201, response.text
    order = response.json()
    repeated = client.post('/api/v1/billing/orders', json={'provider': 'wechat'}, headers={'Idempotency-Key': key})
    assert repeated.json()['id'] == order['id']
    assert client.app.state.billing.gateway.create_calls == 1
    assert client.post('/api/v1/billing/webhooks/wechat', content=b'forged').status_code == 400
    assert client.get('/api/v1/private').status_code == 403
    gateway = client.app.state.billing.gateway
    gateway.notifications[b'verified'] = {'provider': 'wechat', 'order_id': order['id'], 'transaction_id': 'paid-' + order['id'], 'amount_fen': 19900, 'currency': 'CNY', 'status': 'paid', 'merchant_id': 'test-merchant', 'app_id': 'test-app'}
    # Provider delivery has no cookie, Origin, CSRF or browser-session header.
    with TestClient(client.app) as provider:
        assert provider.post('/api/v1/billing/webhooks/wechat', content=b'verified').status_code == 204
        assert provider.post('/api/v1/billing/webhooks/wechat', content=b'verified').status_code == 204
    current = client.get('/api/v1/auth/session').json()
    assert current['account_type'] == 'member' and current['session_id'] == old_session['session_id']
    assert client.get('/api/v1/private').status_code == 200
    assert client.get('/api/v1/billing/orders/' + order['id']).json()['status'] == 'paid'


def test_registration_and_billing_writes_require_origin_and_csrf(client):
    body = {'username': 'unsafe-' + uuid4().hex, 'password': 'a-valid-test-password'}
    assert client.post('/api/v1/auth/register', json=body).status_code == 403
    register(client)
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/billing/orders', json={'provider': 'alipay'}, headers={'Idempotency-Key': uuid4().hex}).status_code == 403


def test_query_recovers_missing_callback_and_throttles_checks(client):
    register(client)
    order = client.post('/api/v1/billing/orders', json={'provider': 'alipay'}, headers={'Idempotency-Key': uuid4().hex}).json()
    assert client.get('/api/v1/billing/orders/' + order['id']).status_code == 200
    assert client.get('/api/v1/billing/orders/' + order['id']).status_code == 200
    assert client.app.state.billing.gateway.query_calls == 1
    gateway = client.app.state.billing.gateway
    from uuid import UUID
    gateway.payments[UUID(order['id']).hex] = {'provider': 'alipay', 'order_id': order['id'], 'transaction_id': 'paid-' + order['id'], 'amount_fen': 19900, 'currency': 'CNY', 'status': 'paid', 'merchant_id': 'test-merchant', 'app_id': 'test-app'}
    with client.app.state.billing.repository.connection(service=True) as connection:
        connection.execute('UPDATE billing.orders SET last_checked_at=NULL WHERE id=%s', (order['id'],))
    assert client.get('/api/v1/billing/orders/' + order['id']).json()['status'] == 'paid'
    assert client.get('/api/v1/auth/session').json()['account_type'] == 'member'


def test_lost_checkout_response_recovers_same_order_without_granting_access(client, monkeypatch):
    register(client)
    gateway = client.app.state.billing.gateway
    create = gateway.create_order
    def timeout(*args):
        raise PaymentError('payment_timeout', retryable=True)
    monkeypatch.setattr(gateway, 'create_order', timeout)
    key = uuid4().hex
    first = client.post('/api/v1/billing/orders', json={'provider': 'wechat'}, headers={'Idempotency-Key': key})
    assert first.status_code == 201 and first.json()['qr_code'] is None
    monkeypatch.setattr(gateway, 'create_order', create)
    recovered = client.get('/api/v1/billing/orders/' + first.json()['id']).json()
    assert recovered['id'] == first.json()['id'] and recovered['qr_code']
    assert len(client.get('/api/v1/billing/orders').json()['items']) == 1
    assert client.get('/api/v1/auth/session').json()['account_type'] == 'free'


def test_paused_sales_still_fulfill_existing_order(client):
    register(client)
    billing = client.app.state.billing
    order = client.post('/api/v1/billing/orders', json={'provider': 'alipay'}, headers={'Idempotency-Key': uuid4().hex}).json()
    billing.settings.enabled = False
    assert client.get('/api/v1/billing/plans').json()['providers'] == []
    assert client.post('/api/v1/billing/orders', json={'provider': 'alipay'}, headers={'Idempotency-Key': uuid4().hex}).status_code == 503
    billing.gateway.notifications[b'paid-while-paused'] = {
        'provider': 'alipay', 'order_id': order['id'], 'transaction_id': 'paused-' + order['id'],
        'amount_fen': 19900, 'currency': 'CNY', 'status': 'paid',
        'merchant_id': 'test-merchant', 'app_id': 'test-app'}
    with TestClient(client.app) as provider:
        result = provider.post('/api/v1/billing/webhooks/alipay', content=b'paid-while-paused')
        assert result.status_code == 200 and result.text == 'success'
    assert client.get('/api/v1/auth/session').json()['account_type'] == 'member'


def test_expired_checkout_stays_pending_until_provider_confirms_close(client, monkeypatch):
    register(client)
    billing = client.app.state.billing
    order = client.post('/api/v1/billing/orders', json={'provider': 'wechat'}, headers={'Idempotency-Key': uuid4().hex}).json()
    with billing.repository.connection(service=True) as connection:
        connection.execute("UPDATE billing.orders SET created_at=clock_timestamp()-interval '20 minutes',expires_at=clock_timestamp()-interval '5 minutes' WHERE id=%s", (order['id'],))
    close = billing.gateway.close_order
    def timeout(*args):
        raise PaymentError('payment_timeout', retryable=True)
    monkeypatch.setattr(billing.gateway, 'close_order', timeout)
    assert client.get('/api/v1/billing/orders/' + order['id']).json()['status'] == 'pending'
    monkeypatch.setattr(billing.gateway, 'close_order', close)
    with billing.repository.connection(service=True) as connection:
        connection.execute('UPDATE billing.orders SET last_checked_at=NULL WHERE id=%s', (order['id'],))
    assert client.get('/api/v1/billing/orders/' + order['id']).json()['status'] == 'closed'
    assert client.get('/api/v1/auth/session').json()['account_type'] == 'free'


def test_deployment_drain_defers_read_triggered_payment_reconciliation(client, monkeypatch):
    register(client)
    billing = client.app.state.billing
    from app.services import deployment_control
    billing.application_settings = SimpleNamespace(deployment_drain_enabled=True)
    billing.write_tracker = deployment_control.InflightApiWriteTracker()
    monkeypatch.setattr(deployment_control, 'get_drain_state', lambda connection: SimpleNamespace(enabled=True))
    order = client.post('/api/v1/billing/orders', json={'provider': 'wechat'}, headers={'Idempotency-Key': uuid4().hex}).json()
    assert client.get('/api/v1/billing/orders/' + order['id']).status_code == 200
    assert billing.gateway.query_calls == 0 and billing.write_tracker.active == 0
    monkeypatch.setattr(deployment_control, 'get_drain_state', lambda connection: SimpleNamespace(enabled=False))
    assert client.get('/api/v1/billing/orders/' + order['id']).status_code == 200
    assert billing.gateway.query_calls == 1 and billing.write_tracker.active == 0


@pytest.mark.parametrize('legacy,override', [(True, None), (False, 'member'), (False, 'free')])
def test_existing_or_admin_managed_accounts_cannot_be_charged(settings, owner, legacy, override):
    repo = BillingRepository(settings)
    with repo.connection(service=True) as connection:
        connection.execute('UPDATE billing.accounts SET legacy_member=%s,override_tier=%s WHERE owner_user_id=%s', (legacy, override, owner))
    with pytest.raises(BillingError) as error:
        new_order(repo, owner)
    assert error.value.code == 'purchase_not_required'
