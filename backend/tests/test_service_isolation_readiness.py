"""0019 readiness/restore contracts against disposable PostgreSQL roles only."""
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.backup import validate_backup_schema
from app.auth.cutover import apply_identity_cutover
from app.auth.isolation_ledger import CURRENT_VERSION, CUTOVER_VERSION, expected_isolation_ledger
from app.auth.service import AuthService
from app.auth.service_privileges import apply_service_auth_least_privilege
from app.auth.settings import AuthSettings
from app.config import Settings
from app.postgres_preflight import (SCHEMA_TARGET_ISOLATION,
    SCHEMA_TARGET_ISOLATION_HISTORICAL, _required_migrations, run_preflight)
from scripts.user_isolation_audit import capture, preflight
from test_auth_isolation import auth_database


def auth_service(database):
    return AuthService(AuthSettings(application_dsn=database['api'], auth_dsn=database['auth'],
                                    service_dsn=database['service'], cookie_secure=False))


def report(database, target=SCHEMA_TARGET_ISOLATION):
    settings = Settings(app_postgres_dsn=database['api'], pi_postgres_dsn=database['api'],
                        model_enabled=False, gen_model_enabled=False, retro_model_enabled=False,
                        gpu_broker_enabled=False)
    return run_preflight(settings, dsn=database['service'], mode='schema', strict=True,
                         schema_target=target)


@contextmanager
def historical_ledger(database):
    with psycopg.connect(database['admin'], row_factory=dict_row) as connection:
        old = connection.execute('DELETE FROM governance.schema_migrations WHERE version=%s RETURNING version,checksum',
                                 (CURRENT_VERSION,)).fetchone()
        assert old
    try:
        yield
    finally:
        with psycopg.connect(database['admin']) as connection:
            connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)',
                               (old['version'], old['checksum']))


def test_historical_profile_never_expands_with_current_manifest():
    required = _required_migrations(SCHEMA_TARGET_ISOLATION_HISTORICAL)
    assert required[-1] == CUTOVER_VERSION
    assert CURRENT_VERSION not in required
    assert _required_migrations(SCHEMA_TARGET_ISOLATION)[-1] == CURRENT_VERSION
    assert expected_isolation_ledger(CUTOVER_VERSION)[-1]['version'] == CUTOVER_VERSION


def test_current_readiness_needs_exact_0019_and_historical_is_not_current(auth_database):
    auth_service(auth_database).assert_application_ready()
    current = report(auth_database)
    assert current['strict_ok'], current['strict_errors']
    assert current['current_readiness'] is True
    assert not report(auth_database, SCHEMA_TARGET_ISOLATION_HISTORICAL)['strict_ok']
    with historical_ledger(auth_database):
        with pytest.raises(RuntimeError, match='exact 0019'):
            auth_service(auth_database).assert_application_ready()
        assert not report(auth_database)['strict_ok']
        historical = report(auth_database, SCHEMA_TARGET_ISOLATION_HISTORICAL)
        assert historical['strict_ok'], historical['strict_errors']
        assert historical['current_readiness'] is False
        with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
            assert validate_backup_schema(connection)['current_readiness'] is False
        with psycopg.connect(auth_database['audit'], row_factory=dict_row) as connection:
            assert capture(connection, schema_version=1)['current_readiness'] is False


@pytest.mark.parametrize('field', ['auth_dsn', 'service_dsn'])
def test_auth_readiness_rejects_actual_login_on_another_database(auth_database, field):
    service = auth_service(auth_database)
    different = make_conninfo(getattr(service.settings, field), dbname='postgres')
    service.settings = replace(service.settings, **{field:different})
    with pytest.raises(ValueError, match='different databases'):
        service.assert_application_ready()


@pytest.mark.parametrize('grant_template,revoke_template', [
    ('GRANT SELECT(password_hash) ON auth.users TO {}', 'REVOKE SELECT(password_hash) ON auth.users FROM {}'),
    ('GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) TO {}',
     'REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) FROM {}'),
    ('GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO PUBLIC',
     'REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM PUBLIC'),
    ('GRANT pg_signal_backend TO {} WITH INHERIT TRUE, SET FALSE', 'REVOKE pg_signal_backend FROM {}'),
    ('GRANT pg_signal_backend TO {} WITH INHERIT FALSE, SET TRUE', 'REVOKE pg_signal_backend FROM {}'),
    ('GRANT pg_checkpoint TO {}', 'REVOKE pg_checkpoint FROM {}'),
    ('GRANT ALTER SYSTEM ON PARAMETER log_statement TO {}', 'REVOKE ALTER SYSTEM ON PARAMETER log_statement FROM {}'),
    ('GRANT SET ON PARAMETER session_replication_role TO {} WITH GRANT OPTION',
     'REVOKE SET ON PARAMETER session_replication_role FROM {}'),
])
def test_actual_service_login_acl_drift_blocks_all_current_checks(auth_database, grant_template, revoke_template):
    role = conninfo_to_dict(auth_database['service'])['user']
    grant = sql.SQL(grant_template).format(sql.Identifier(role))
    revoke = sql.SQL(revoke_template).format(sql.Identifier(role))
    with psycopg.connect(auth_database['admin']) as connection:
        connection.execute(grant)
    try:
        with pytest.raises(ValueError):
            auth_service(auth_database).assert_application_ready()
        assert report(auth_database)['current_readiness'] is False
        with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
            with pytest.raises(ValueError):
                validate_backup_schema(connection)
        with psycopg.connect(auth_database['audit'], row_factory=dict_row) as connection:
            with pytest.raises(ValueError):
                capture(connection)
        with pytest.raises(ValueError):
            preflight({'nexpoly_' + kind: auth_database[kind] for kind in ('api','auth','service')})
        with pytest.raises(ValueError):
            apply_service_auth_least_privilege(auth_database['admin'], service_roles=[role],
                expected_database=conninfo_to_dict(auth_database['admin'])['dbname'])
        with pytest.raises(ValueError):
            apply_identity_cutover(auth_database['admin'], str(uuid4()))
    finally:
        with psycopg.connect(auth_database['admin']) as connection:
            connection.execute(revoke)
    auth_service(auth_database).assert_application_ready()


def test_0019_cutover_idempotence_never_replays_seals_or_historical_grants(auth_database, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('0019 idempotence must precede data seals')
    for name in ('private_data_seal', 'auth_metadata_seal', 'backup_state_seal'):
        monkeypatch.setattr('app.auth.cutover.' + name, forbidden)
    result = apply_identity_cutover(auth_database['admin'], str(uuid4()),
                                    expected_business_data={}, expected_backup_state={})
    assert result['already_applied'] and result['current_version'] == CURRENT_VERSION
    # Completing historical cutover is not the current API/auth/service check.
    assert result['current_readiness'] is False
    auth_service(auth_database).assert_application_ready()


def test_current_audit_and_restore_validate_actual_service_members(auth_database):
    with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
        proof = validate_backup_schema(connection)
        assert proof['current_readiness']
        assert proof['migration_ledger'] == expected_isolation_ledger()
    with psycopg.connect(auth_database['audit'], row_factory=dict_row) as connection:
        proof = capture(connection)
        assert proof['schema_version'] == 2 and proof['current_readiness']
    proof = preflight({'nexpoly_' + role:auth_database[role] for role in ('api','auth','service')})
    assert proof['current_readiness']


def test_current_preflight_rejects_service_and_api_on_different_databases(auth_database):
    different = {**auth_database, 'api':make_conninfo(auth_database['api'], dbname='postgres')}
    result = report(different)
    assert not result['current_readiness']
    assert any('different databases' in reason for reason in result['strict_errors'])
