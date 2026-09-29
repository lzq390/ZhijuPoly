"""Authenticated browser and explicit actor fixtures for legacy monomer regressions."""
from contextvars import ContextVar
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from app.auth.cli import manage_user
from app.auth.context import Identity, user_context
from test_auth_isolation import auth_database
from test_private_http_support import authenticated_client

_case = ContextVar('monomer_test_case')


@pytest.fixture(autouse=True)
def monomer_identity(auth_database, monkeypatch):
    with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
        connection.execute('TRUNCATE md.monomer_md_jobs,monomer_dft.jobs CASCADE')
        user = manage_user(connection, 'create', username='monomer'+uuid4().hex,
                           password='initial-test-password')
    monkeypatch.setenv('APP_POSTGRES_DSN', auth_database['api'])
    monkeypatch.setenv('AUTH_POSTGRES_DSN', auth_database['auth'])
    monkeypatch.setenv('APP_SERVICE_POSTGRES_DSN', auth_database['service'])
    token = _case.set((auth_database, user))
    try:
        with user_context(Identity(str(user['user_id']))):
            yield Identity(str(user['user_id']))
    finally:
        _case.reset(token)


@pytest.fixture
def postgres_dsn(auth_database, monomer_identity):
    """Privileged fixture-only writes; application repositories use api_dsn()."""
    return auth_database['admin']


def api_dsn():
    return _case.get()[0]['api']


def private_test_client(app, **options):
    database, user = _case.get()
    if hasattr(app.state, 'settings'):
        app.state.settings.app_postgres_dsn = database['api']
    return authenticated_client(app, database, user=user, **options)


def another_identity():
    database, _ = _case.get()
    with psycopg.connect(database['admin'], row_factory=dict_row) as connection:
        user = manage_user(connection, 'create', username='other'+uuid4().hex,
                           password='initial-test-password')
    return Identity(str(user['user_id']))


@pytest.fixture(scope='module')
def legacy_dft_database():
    """Keep historical exact-catalog regression authority at the immutable 0016 epoch."""
    import hashlib
    import os
    from pathlib import Path
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    base = os.environ.get('ISOLATION_TEST_ADMIN_DSN')
    if not base:
        pytest.skip('dedicated PostgreSQL 16 required')
    name = 'zhijupoly_test_dft_legacy_' + uuid4().hex[:12]
    dsn = make_conninfo(base, dbname=name)
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        with psycopg.connect(dsn) as connection:
            for path in sorted((Path(__file__).parents[1] / 'migrations/postgres').glob('*.sql')):
                if path.name[:4] > '0016':
                    break
                connection.execute(path.read_text())
                connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES (%s,%s)',
                                   (path.stem,hashlib.sha256(path.read_bytes()).hexdigest()))
        yield dsn
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def other_private_test_client(app, **options):
    database, _ = _case.get()
    return authenticated_client(app, database, **options)
