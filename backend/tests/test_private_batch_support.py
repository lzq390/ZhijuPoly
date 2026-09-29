"""Authenticated batch regression fixtures using real API/service login roles."""
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.auth.cli import manage_user
from app.auth.context import Identity, user_context
from app.auth.settings import AuthSettings
from app.services.polymerization_batch.models import BatchSettings
from app.services.polymerization_batch.service import BatchService
from app.services.polymerization_batch.worker import BatchWorker


@contextmanager
def batch_environment(tmp_path, database, **options):
    with psycopg.connect(database['admin'], row_factory=dict_row) as connection:
        connection.execute('TRUNCATE polymerization_batch.imports, polymerization_batch.jobs CASCADE')
        connection.execute('UPDATE governance.deployment_control SET drain_enabled=false,reason=NULL,release_sha=NULL,activated_by=NULL')
        user = manage_user(connection, 'create', username='batch'+uuid4().hex, password='initial-test-password')
    settings = AuthSettings(application_dsn=database['api'], auth_dsn=database['auth'],
                            service_dsn=database['service'], cookie_secure=False,
                            cookie_name='nexpoly_test_session', allowed_origins=('http://testserver',))
    config = BatchSettings(enabled=True, storage_root=tmp_path / 'batch', **options)
    service = BatchService(database['api'], config)
    service.test_admin_dsn = database['admin']
    worker = BatchWorker(database['service'], config)
    with user_context(Identity(str(user['user_id'])), settings):
        yield service, worker, user, settings


def admin_connection(service):
    return psycopg.connect(service.test_admin_dsn, row_factory=dict_row)
