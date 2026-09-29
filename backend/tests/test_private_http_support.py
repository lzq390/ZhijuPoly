"""Create ordinary browser sessions for tests of protected HTTP endpoints."""
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row

from app.auth.cli import manage_user
from app.auth.settings import AuthSettings
from test_auth_isolation import activate


def authenticated_client(app, database, *, user=None, **client_options):
    if user is None:
        with psycopg.connect(database['admin'], row_factory=dict_row) as connection:
            user = manage_user(connection, 'create', username='http'+uuid4().hex, password='initial-test-password')
    settings = AuthSettings(application_dsn=database['api'], auth_dsn=database['auth'],
        service_dsn=database['service'], cookie_secure=False, cookie_name='nexpoly_test_session',
        allowed_origins=('http://testserver',))
    if not hasattr(app.state, 'auth'):
        from app.auth.service import AuthService
        from app.auth.middleware import AuthenticationMiddleware
        from app.auth.router import router
        app.state.auth = AuthService(settings)
        app.include_router(router)
        app.add_middleware(AuthenticationMiddleware, auth_service=app.state.auth)
    else:
        app.state.auth.settings = settings
    client = TestClient(app, **client_options)
    if user.get('_activated'):
        from test_auth_isolation import login
        login(client, user, 'changed-test-password')
    else:
        activate(client, user)
        user['_activated'] = True
    client.test_owner_id = str(user['user_id'])
    return client
