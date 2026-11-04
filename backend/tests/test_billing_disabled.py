"""An existing installation can keep billing disabled before its migration."""
from app.billing.providers import PaymentGateway
from app.billing.repository import BillingError, BillingRepository
from app.billing.router import billing_error_handler, router
from app.billing.service import BillingService
from app.billing.settings import BillingSettings
from test_auth_isolation import activate, auth_app, auth_database


def test_disabled_billing_before_migration_preserves_existing_account(auth_app):
    app, user, client = auth_app
    settings = BillingSettings()
    app.state.billing = BillingService(settings, BillingRepository(app.state.auth.settings), PaymentGateway(settings))
    app.add_exception_handler(BillingError, billing_error_handler)
    app.include_router(router)
    activate(client, user)
    assert client.get('/api/v1/auth/session').json()['account_type'] == 'member'
    plans = client.get('/api/v1/billing/plans')
    assert plans.status_code == 200 and plans.json()['enabled'] is False
    for path in ('subscription', 'orders'):
        result = client.get('/api/v1/billing/' + path)
        assert result.status_code == 503
        assert result.json()['detail']['code'] == 'billing_not_ready'
    assert client.get('/api/v1/private').status_code == 200
