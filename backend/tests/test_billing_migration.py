"""Membership cutover and worker authorization on disposable PostgreSQL 16."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import json

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.isolation_ledger import expected_isolation_ledger, validate_application_migration_ledger
from app.auth.backup import validate_backup_schema
from app.auth.cutover import backup_state_seal
from app.services.billing_schema import validate_billing_schema
from app.services.entitlement_repository import EntitlementRepository
from task_usage.entitlement_access import resolve_access_tier, accepted_release_ledgers
from task_usage.billing_contract import BILLING_VERSION, BILLING_CHECKSUM
from task_usage import _entitlement_store
from test_auth_isolation import auth_database

MIGRATIONS = Path(__file__).resolve().parents[1] / 'migrations/postgres'


@pytest.fixture(scope='module')
def billing_db(auth_database):
    literature_role = None
    with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
        for item in expected_isolation_ledger(BILLING_VERSION):
            if item['version'] <= '0019_service_auth_least_privilege':
                continue
            source = (MIGRATIONS / (item['version'] + '.sql')).read_text()
            if item['version'] == '0024_literature_projection':
                literature_role = 'billing_lit_' + connection.info.dbname[-12:]
                source = source.replace('nexpoly_literature_import', literature_role)
            if item['version'] == BILLING_VERSION:
                owner = uuid4()
                connection.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,'legacy-free','!',false)", (owner,))
                connection.execute("INSERT INTO usage_entitlements.owner_entitlements(owner_user_id,tier) VALUES(%s,'free')", (owner,))
                continue
            connection.execute(source)
            connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)',
                               (item['version'], item['checksum']))
    from app.billing_migrations import migrate_memberships
    assert migrate_memberships(auth_database['admin'])['written'] is False
    with psycopg.connect(auth_database['admin']) as connection:
        assert connection.execute("SELECT to_regnamespace('billing')").fetchone()[0] is None
    assert migrate_memberships(auth_database['admin'], apply=True)['written'] is True
    assert migrate_memberships(auth_database['admin'], apply=True)['written'] is False
    try:
        yield auth_database
    finally:
        if literature_role:
            with psycopg.connect(auth_database['admin']) as connection:
                connection.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(literature_role)))
                connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(literature_role)))


def _register(db):
    owner = uuid4()
    with psycopg.connect(db['auth'], row_factory=dict_row) as connection:
        connection.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,'!',false)",
                           (owner, 'billing-' + owner.hex))
    return str(owner)


def test_registered_account_is_free_and_subscription_expires_without_quota(billing_db, monkeypatch):
    monkeypatch.setenv('USAGE_ENTITLEMENTS_ENABLED', 'false')
    owner = _register(billing_db)
    with psycopg.connect(billing_db['api'], row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.user_id',%s,true)", (owner,))
        assert resolve_access_tier(connection, owner_user_id=owner) == 'free'
        assert EntitlementRepository().resolve_access_tier(connection=connection, owner_user_id=owner) == 'free'
    with psycopg.connect(billing_db['service']) as connection:
        connection.execute("INSERT INTO billing.subscriptions(owner_user_id,paid_through) VALUES(%s,clock_timestamp()+interval '1 month')", (owner,))
    with psycopg.connect(billing_db['api'], row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.user_id',%s,true)", (owner,))
        assert resolve_access_tier(connection, owner_user_id=owner) == 'member'
    with psycopg.connect(billing_db['service']) as connection:
        connection.execute("UPDATE billing.subscriptions SET paid_through=clock_timestamp()-interval '1 second' WHERE owner_user_id=%s", (owner,))
    with psycopg.connect(billing_db['service'], row_factory=dict_row) as connection:
        assert resolve_access_tier(connection, owner_user_id=owner) == 'free'


def test_cutover_retains_legacy_member_and_explicit_admin_override(billing_db):
    with psycopg.connect(billing_db['admin'], row_factory=dict_row) as connection:
        free = str(connection.execute("SELECT user_id FROM auth.users WHERE username='legacy-free'").fetchone()['user_id'])
        assert resolve_access_tier(connection, owner_user_id=free) == 'free'
    with psycopg.connect(billing_db['service'], row_factory=dict_row) as connection:
        owner = str(connection.execute('SELECT owner_user_id FROM billing.accounts WHERE legacy_member LIMIT 1').fetchone()['owner_user_id'])
        assert resolve_access_tier(connection, owner_user_id=owner) == 'member'
        assert _entitlement_store.set_owner_tier(connection, owner_user_id=owner, tier='free')['changed']
        assert resolve_access_tier(connection, owner_user_id=owner) == 'free'
        connection.execute("INSERT INTO billing.subscriptions(owner_user_id,paid_through) VALUES(%s,clock_timestamp()+interval '1 month')", (owner,))
        assert resolve_access_tier(connection, owner_user_id=owner) == 'free'
        assert _entitlement_store.set_owner_tier(connection, owner_user_id=owner, tier='member')['changed']
        assert resolve_access_tier(connection, owner_user_id=owner) == 'member'


def test_new_explicit_free_can_buy_and_repeated_admin_free_revokes(billing_db):
    owner = _register(billing_db)
    with psycopg.connect(billing_db['service'], row_factory=dict_row) as connection:
        connection.execute("INSERT INTO usage_entitlements.owner_entitlements(owner_user_id,tier) VALUES(%s,'free')", (owner,))
        connection.execute("INSERT INTO billing.subscriptions(owner_user_id,paid_through) VALUES(%s,clock_timestamp()+interval '1 month')", (owner,))
        assert resolve_access_tier(connection, owner_user_id=owner) == 'member'
        assert _entitlement_store.set_owner_tier(connection, owner_user_id=owner, tier='free')['changed']
        assert resolve_access_tier(connection, owner_user_id=owner) == 'free'


def test_registration_rollback_and_auth_cannot_grant_membership(billing_db):
    owner = uuid4()
    with pytest.raises(RuntimeError):
        with psycopg.connect(billing_db['auth']) as connection:
            connection.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,'!',false)", (owner, 'rollback-' + owner.hex))
            raise RuntimeError('rollback registration')
    with psycopg.connect(billing_db['admin']) as connection:
        assert connection.execute('SELECT 1 FROM billing.accounts WHERE owner_user_id=%s', (owner,)).fetchone() is None
        assert connection.execute('SELECT 1 FROM auth.users WHERE user_id=%s', (owner,)).fetchone() is None
    with psycopg.connect(billing_db['auth']) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('INSERT INTO billing.accounts(owner_user_id,legacy_member) VALUES(%s,true)', (uuid4(),))


def test_owner_isolation_and_api_cannot_purchase_access(billing_db):
    owner, other = _register(billing_db), _register(billing_db)
    with psycopg.connect(billing_db['api'], row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.user_id',%s,true)", (owner,))
        assert [str(r['owner_user_id']) for r in connection.execute('SELECT owner_user_id FROM billing.accounts')] == [owner]
        assert resolve_access_tier(connection, owner_user_id=other) == 'free'
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('UPDATE billing.accounts SET legacy_member=true')


def test_complete_release_readiness_and_backup_include_billing(billing_db):
    from scripts.site_helper_contracts import validate_membership_ledger, SiteHelperContractError
    assert accepted_release_ledgers()[-1] == expected_isolation_ledger(BILLING_VERSION)
    ledger = expected_isolation_ledger(BILLING_VERSION)
    assert validate_membership_ledger(ledger) == ledger
    with pytest.raises(SiteHelperContractError):
        validate_membership_ledger([*ledger, {'version': '0026_unknown', 'checksum': '0' * 64}])
    with psycopg.connect(billing_db['service'], row_factory=dict_row) as connection:
        assert validate_billing_schema(connection)['validated']
        assert validate_application_migration_ledger(connection, require_usage=False, require_entitlements=False)[-1] == {'version': BILLING_VERSION, 'checksum': BILLING_CHECKSUM}
    with psycopg.connect(billing_db['admin'], row_factory=dict_row) as connection:
        assert validate_backup_schema(connection)['billing_schema']['validated']
        assert all(name in backup_state_seal(connection) for name in ('billing.accounts', 'billing.subscriptions', 'billing.orders', 'billing.payment_events'))


@pytest.mark.parametrize('target', ['user-isolation-current', 'membership-0025'])
def test_membership_service_api_preflight(billing_db, target):
    from app.config import Settings
    from app.postgres_preflight import run_preflight
    settings = Settings(app_postgres_dsn=billing_db['api'], pi_postgres_dsn=billing_db['api'])
    report = run_preflight(settings, dsn=billing_db['service'], mode='schema', strict=True, schema_target=target)
    assert report['schema_target'] == 'membership-0025'
    assert report['strict_ok'] is True, report.get('strict_errors')
    assert report['billing_schema_ready'] is True


def test_tier_cli_preview_observes_subscription_and_override(billing_db, monkeypatch):
    from app import entitlement_cli as cli
    owner = _register(billing_db)
    with psycopg.connect(billing_db['service']) as connection:
        connection.execute("INSERT INTO billing.subscriptions(owner_user_id,paid_through) VALUES(%s,clock_timestamp()+interval '1 month')", (owner,))
    monkeypatch.setenv('APP_SERVICE_POSTGRES_DSN', billing_db['service'])
    args = cli.parser().parse_args(['set-tier', '--owner', owner, '--tier', 'free'])
    preview = cli.execute(args)
    assert preview['observed_tier'] == 'member' and preview['would_change'] and preview['would_write']
    args.apply = True
    assert cli.execute(args)['written']
    args.apply = False
    preview = cli.execute(args)
    assert preview['observed_tier'] == 'free' and not preview['would_write']


def test_billing_dump_restore_preserves_access_and_receipts(billing_db, tmp_path):
    from app.auth.backup import backup_and_verify
    owner, order = _register(billing_db), uuid4()
    with psycopg.connect(billing_db['service']) as connection:
        connection.execute("""INSERT INTO billing.orders(id,owner_user_id,request_key,provider,merchant_id,app_id,amount_fen,expires_at)
            VALUES(%s,%s,'backup-order','wechat','synthetic-merchant','synthetic-app',100,clock_timestamp()+interval '10 minutes')""", (order, owner))
        connection.execute("INSERT INTO billing.payment_events(provider,event_key,order_id,payload_digest) VALUES('wechat','backup-event',%s,%s)", (order, 'a' * 64))
        connection.execute("INSERT INTO billing.subscriptions(owner_user_id,paid_through) VALUES(%s,clock_timestamp()+interval '1 month')", (owner,))
    name = 'billing_restore_' + uuid4().hex
    restore = make_conninfo(billing_db['admin'], dbname=name)
    with psycopg.connect(billing_db['admin'], autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        receipt = backup_and_verify(billing_db['admin'], restore, tmp_path / 'billing-backup')
        assert json.loads(receipt.read_text())['isolation_schema']['billing_schema']['validated']
        with psycopg.connect(restore, row_factory=dict_row) as connection:
            assert resolve_access_tier(connection, owner_user_id=owner) == 'member'
            assert connection.execute("SELECT payload_digest FROM billing.payment_events WHERE provider='wechat' AND event_key='backup-event'").fetchone()['payload_digest'] == 'a' * 64
    finally:
        with psycopg.connect(billing_db['admin'], autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.mark.parametrize('mutation', ['ledger', 'unknown_ledger', 'missing_table', 'rls', 'default', 'auth_columns', 'trigger', 'unique_payment'])
def test_readiness_rejects_drift(billing_db, mutation):
    with psycopg.connect(billing_db['admin'], row_factory=dict_row) as connection:
        with connection.transaction(force_rollback=True):
            if mutation == 'ledger':
                connection.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version=%s", ('0' * 64, BILLING_VERSION))
            elif mutation == 'unknown_ledger':
                connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('0026_unknown',%s)", ('0' * 64,))
            elif mutation == 'missing_table':
                connection.execute('ALTER TABLE billing.subscriptions RENAME TO hidden_subscriptions')
            elif mutation == 'rls':
                connection.execute('ALTER TABLE billing.accounts NO FORCE ROW LEVEL SECURITY')
            elif mutation == 'default':
                connection.execute('ALTER TABLE billing.accounts ALTER COLUMN legacy_member SET DEFAULT true')
            elif mutation == 'auth_columns':
                connection.execute('GRANT INSERT(is_system) ON auth.users TO nexpoly_auth')
            elif mutation == 'unique_payment':
                connection.execute('ALTER TABLE billing.orders DROP CONSTRAINT orders_provider_payment_id_key')
            else:
                connection.execute('ALTER TABLE auth.users DISABLE TRIGGER billing_initialize_account')
            with pytest.raises(ValueError):
                validate_application_migration_ledger(connection, require_usage=False, require_entitlements=False)
