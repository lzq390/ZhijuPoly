"""Real PostgreSQL identities exercise authentication, not a test-only bypass."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import os
from threading import Event
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo, conninfo_to_dict
from psycopg.rows import dict_row
import pytest

from app.auth.cli import manage_user
from app.auth.context import Identity, current_owner_id, service_context, user_context
from app.auth.cutover import apply_identity_cutover, private_data_seal, auth_metadata_seal, backup_state_seal
from app.auth.middleware import AuthenticationMiddleware
from app.auth.router import router
from app.auth.service import AuthService, csrf_token
from app.auth.settings import AuthSettings
from app.postgres_database import postgres_connection
from app.postgres_migrations import apply_postgres_migrations
from app.task_control import authorize_start


@pytest.fixture(scope='module')
def auth_database():
    base = os.environ.get('ISOLATION_TEST_ADMIN_DSN')
    if not base:
        pytest.skip('ISOLATION_TEST_ADMIN_DSN must target a dedicated PostgreSQL 16 instance')
    suffix = uuid4().hex[:12]
    name = 'auth_test_'+suffix
    roles = {kind:'auth_test_'+kind+'_'+suffix for kind in ('api','auth','service','audit')}
    dsn = make_conninfo(base,dbname=name)
    with psycopg.connect(base,autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        apply_postgres_migrations(dsn,allowed_kinds={'baseline','expand'},allow_contract_on_fresh_database=True)
        with psycopg.connect(dsn,row_factory=dict_row) as connection:
            legacy = manage_user(connection,'create',username='legacy-owner',password='initial-test-password')
            for kind,role in roles.items():
                group = 'nexpoly_mutable_audit' if kind == 'audit' else 'nexpoly_'+kind
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'test-only' IN ROLE {}").format(sql.Identifier(role),sql.Identifier(group)))
        apply_identity_cutover(dsn,str(legacy['user_id']))
        from app.auth.service_privileges import apply_service_auth_least_privilege
        apply_service_auth_least_privilege(dsn, service_roles=[roles['service']],
                                          expected_database=conninfo_to_dict(dsn)['dbname'])
        yield {'admin':dsn,**{kind:make_conninfo(dsn,user=role,password='test-only') for kind,role in roles.items()}}
    finally:
        with psycopg.connect(base,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            for role in roles.values():
                connection.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


@pytest.fixture
def auth_app(auth_database):
    with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
        user = manage_user(connection,'create',username='u'+uuid4().hex,password='initial-test-password')
    settings = AuthSettings(application_dsn=auth_database['api'],auth_dsn=auth_database['auth'],
                            service_dsn=auth_database['service'],cookie_secure=False,
                            cookie_name='nexpoly_test_session',allowed_origins=('http://testserver',))
    app = FastAPI()
    app.state.auth = AuthService(settings)
    app.state.parsed = False
    app.include_router(router)
    app.add_middleware(AuthenticationMiddleware,auth_service=app.state.auth)
    @app.get('/api/v1/private')
    def private():
        with postgres_connection(settings.application_dsn) as connection:
            return {'owner':current_owner_id(),'rows':connection.execute('SELECT count(*) AS n FROM online_knowledge.history').fetchone()['n']}
    @app.post('/api/v1/upload')
    async def upload(request: Request):
        app.state.parsed = True
        return await request.json()
    return app,user,TestClient(app)


def login(client,user,password='initial-test-password'):
    response = client.post('/api/v1/auth/login',json={'username':user['username'],'password':password},headers={'Origin':'http://testserver'})
    assert response.status_code == 200,response.text
    session = response.json()
    client.headers.update({'Origin':'http://testserver','X-Session-Context':session['session_id'],'X-CSRF-Token':session['csrf_token']})
    return session


def activate(client,user):
    login(client,user)
    assert client.post('/api/v1/auth/password',json={'current_password':'initial-test-password','new_password':'changed-test-password'}).status_code == 204
    return login(client,user,'changed-test-password')


def test_guest_rejected_before_body_parse(auth_app):
    app,_user,client = auth_app
    assert client.get('/api/v1/auth/session').json()['authenticated'] is False
    response = client.post('/api/v1/upload',content=b'invalid large body',headers={'Origin':'http://testserver'})
    assert response.status_code == 401
    assert not app.state.parsed
    assert response.headers['cache-control'] == 'private, no-store'
    assert client.post('/api/v1/auth/login',content=iter([b'{"username":"'+b'x'*9000,b'"}']),
                       headers={'Origin':'http://testserver','Content-Type':'application/json'}).status_code == 413


def test_password_change_and_logout_revoke_sessions(auth_app):
    app,user,client = auth_app
    app.state.auth.assert_application_ready()
    initial = login(client,user)
    old_token = client.cookies.get(app.state.auth.settings.cookie_name)
    assert client.get('/api/v1/private').status_code == 403
    assert client.post('/api/v1/auth/password',json={'current_password':'initial-test-password','new_password':'changed-test-password'}).status_code == 204
    assert app.state.auth.resolve(old_token) is None
    assert client.get('/api/v1/private').status_code == 401
    current = login(client,user,'changed-test-password')
    assert current['session_id'] != initial['session_id']
    assert client.get('/api/v1/private').json()['owner'] == str(user['user_id'])
    assert client.post('/api/v1/auth/logout').status_code == 204
    assert client.get('/api/v1/auth/session').json()['authenticated'] is False


def test_origin_csrf_and_old_tab_context(auth_app):
    _app,user,client = auth_app
    assert client.post('/api/v1/auth/login',json={'username':user['username'],'password':'initial-test-password'}).status_code == 403
    old = activate(client,user)
    login(client,user,'changed-test-password')
    assert client.get('/api/v1/private',headers={'X-Session-Context':old['session_id']}).status_code == 409
    assert client.post('/api/v1/upload',json={},headers={'X-CSRF-Token':'wrong'}).status_code == 403
    assert client.post('/api/v1/upload',json={},headers={'Origin':'https://untrusted.example'}).status_code == 403
    assert client.post('/api/v1/upload',json={}).status_code == 200


def test_late_logout_response_does_not_erase_new_account_cookie(auth_app,auth_database,monkeypatch):
    app,a,client = auth_app
    activate(client,a)
    revoked,finish = Event(),Event()
    original = app.state.auth.logout
    def delayed_logout(session_id):
        original(session_id)
        revoked.set()
        assert finish.wait(10)
    monkeypatch.setattr(app.state.auth,'logout',delayed_logout)
    with ThreadPoolExecutor(max_workers=1) as executor:
        old_response = executor.submit(client.post,'/api/v1/auth/logout')
        try:
            assert revoked.wait(3)
            with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
                b = manage_user(connection,'create',username='late'+uuid4().hex,password='initial-test-password')
            activate(client,b)
            b_token = client.cookies.get(app.state.auth.settings.cookie_name)
        finally:
            finish.set()
        assert old_response.result(3).status_code == 204
    assert client.cookies.get(app.state.auth.settings.cookie_name) == b_token
    assert client.get('/api/v1/private').json()['owner'] == str(b['user_id'])


@pytest.mark.parametrize('action',['disable','reset'])
def test_cli_revokes_all_independent_browser_sessions(auth_app,auth_database,action):
    app,user,client = auth_app
    activate(client,user)
    other = TestClient(app)
    login(other,user,'changed-test-password')
    with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
        manage_user(connection,action,user_id=str(user['user_id']),password='reset-test-password')
    assert client.get('/api/v1/private').status_code == 401
    assert other.get('/api/v1/private').status_code == 401


def test_actual_api_and_audit_roles_cannot_read_secrets(auth_database):
    for kind,query in [('api','SELECT * FROM auth.users'),('audit','SELECT password_hash FROM auth.users'),('audit','SELECT token_hash FROM auth.sessions')]:
        with psycopg.connect(auth_database[kind]) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(query)
    with psycopg.connect(auth_database['audit']) as connection:
        assert connection.execute('SELECT user_id,username,status FROM auth.users').fetchone()


def test_privileged_api_identity_is_rejected(auth_app,auth_database):
    app,_user,_client = auth_app
    auth = AuthService(replace(app.state.auth.settings,application_dsn=auth_database['admin']))
    with pytest.raises((RuntimeError,ValueError),match='runtime identity'):
        auth.assert_application_ready()


def test_login_failure_rate_is_bounded(auth_app):
    _app,user,client = auth_app
    responses = [client.post('/api/v1/auth/login',json={'username':user['username'],'password':'wrong'},headers={'Origin':'http://testserver'}) for _ in range(11)]
    assert all(response.status_code == 401 for response in responses[:10])
    assert responses[-1].status_code == 429


def test_disable_first_and_start_first_commit_order(auth_app,auth_database):
    app,user,_client = auth_app
    owner = str(user['user_id'])
    # A granted in-memory execution remains granted; subsequent authorizations fail.
    assert app.state.auth.authorize_memory_start(owner)
    with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
        manage_user(connection,'disable',user_id=owner)
    assert not app.state.auth.authorize_memory_start(owner)
    # Hold disable's user lock; concurrent start must wait and then observe disabled.
    with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
        manage_user(connection,'enable',user_id=owner)
    started = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
            manage_user(connection,'disable',user_id=owner)
            def start():
                started.set()
                return app.state.auth.authorize_memory_start(owner)
            pending = executor.submit(start)
            assert started.wait(2)
            assert not pending.done()
        assert pending.result(timeout=5) is False


def test_full_application_guest_gate_and_private_history(auth_database,monkeypatch):
    from app.config import Settings
    from app.main import create_app
    monkeypatch.setenv('AUTH_POSTGRES_DSN',auth_database['auth'])
    monkeypatch.setenv('APP_SERVICE_POSTGRES_DSN',auth_database['service'])
    monkeypatch.setenv('AUTH_COOKIE_SECURE','false')
    app = create_app(Settings(app_postgres_dsn=auth_database['api'],allowed_origins='http://testserver',model_enabled=False))
    client = TestClient(app)
    for path in ('/api/v1/online-knowledge/history','/api/v1/monomer-md/jobs','/api/v1/monomer-dft/jobs','/api/v1/monomer-polymerization/batch/jobs'):
        assert client.get(path).status_code == 401
    assert client.get('/api/v1/monomer-polymerization/batch/templates/a.csv').status_code == 200
    with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
        a = manage_user(connection,'create',username='a'+uuid4().hex,password='initial-test-password')
        b = manage_user(connection,'create',username='b'+uuid4().hex,password='initial-test-password')
        for index,user in enumerate((a,b),start=10):
            from psycopg.types.json import Jsonb
            result = dict(material=user['username'],mode='synthesis',query_time_ms=0,totalPapers=0,max_papers=1,exampleUsed=False,stats={})
            connection.execute('INSERT INTO online_knowledge.history(history_id,material,mode,owner_user_id,result_data) VALUES(%s,%s,%s,%s,%s)',(index,user['username'],'synthesis',user['user_id'],Jsonb(result)))
    activate(client,a)
    response = client.get('/api/v1/online-knowledge/history')
    assert response.status_code == 200,response.text
    assert a['username'] in response.text and b['username'] not in response.text
    other = TestClient(app)
    activate(other,b)
    assert a['username'] not in other.get('/api/v1/online-knowledge/history').text
    assert other.get('/api/v1/monomer-polymerization/batch/jobs').json() == {'items':[],'total':0,'next_offset':None}
    assert client.get('/api/v1/lab-data/test-projects').status_code == 403


def test_full_application_startup_uses_separate_runtime_roles(auth_database,monkeypatch):
    from app.config import Settings
    from app.main import create_app
    monkeypatch.setenv('AUTH_POSTGRES_DSN',auth_database['auth'])
    monkeypatch.setenv('APP_SERVICE_POSTGRES_DSN',auth_database['service'])
    monkeypatch.setenv('AUTH_COOKIE_SECURE','false')
    app = create_app(Settings(app_postgres_dsn=auth_database['api'],pi_postgres_dsn=auth_database['api'],allowed_origins='http://testserver',
                              model_enabled=False,gen_model_enabled=False,retro_model_enabled=False,
                              gpu_preload_mode='lazy',gpu_broker_enabled=False))
    with TestClient(app) as client:
        # This empty fixture deliberately has no platform scientific datasets.
        # Startup must reach their validation without role/schema/RLS failures.
        assert set(app.state.database_preflight_errors) == {
            'Property filter records are empty; run the property_filter import before deployment.',
            'Required runtime source is missing: property_filter_csv',
            'Required Postgres analytics snapshot is missing or invalid',
        }
        app.state.auth.assert_application_ready()
        assert client.get('/api/v1/auth/session').json()['authenticated'] is False
        assert client.get('/api/v1/online-knowledge/history').status_code == 401


def test_authorized_online_job_finishes_with_service_role_after_disable(auth_app,auth_database,monkeypatch):
    from app.routers import online_knowledge as routes
    from app.services.online_knowledge.postgres_history_repository import create_online_job_postgres
    from app.models import OnlineKnowledgeSearchRequest
    from app.task_control import acquire_admission
    app,user,_client = auth_app
    owner,job_id = str(user['user_id']),uuid4().hex
    identity = Identity(owner,request_id='online-background-request')
    settings = app.state.auth.settings
    with user_context(identity,settings):
        with service_context(), postgres_connection(settings.application_dsn) as connection:
            create_online_job_postgres(connection,owner_user_id=owner,job_id=job_id,material='private',mode='synthesis',max_papers=1)
        lease = acquire_admission('online')

    @contextmanager
    def checked_service_connection(dsn):
        with postgres_connection(dsn) as connection:
            assert connection.execute("SELECT pg_has_role(current_user,'nexpoly_service','member') AS service").fetchone()['service']
            yield connection

    def search(*_args):
        with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
            assert connection.execute('SELECT start_authorized_at FROM online_knowledge.jobs WHERE job_id=%s',(job_id,)).fetchone()['start_authorized_at']
            manage_user(connection,'disable',user_id=owner)
        return dict(material='private',mode='synthesis',query_time_ms=0,totalPapers=0,max_papers=1,exampleUsed=False,stats={})

    monkeypatch.setattr(routes,'postgres_connection',checked_service_connection)
    monkeypatch.setattr(routes,'_run_search_from_request',search)
    routes._run_online_knowledge_job(job_id,settings.application_dsn,OnlineKnowledgeSearchRequest(material='private'),
        routes.OnlineModelAccess('test-only','https://supplier.invalid','platform',''),identity,lease,settings)
    assert lease.released
    with psycopg.connect(auth_database['admin'],row_factory=dict_row) as connection:
        job = connection.execute('SELECT status,owner_user_id FROM online_knowledge.jobs WHERE job_id=%s',(job_id,)).fetchone()
        assert job['status'] == 'completed' and str(job['owner_user_id']) == owner


def test_cutover_refuses_changed_backup_and_backfills_only_explicit_owner():
    base = os.environ.get('ISOLATION_TEST_ADMIN_DSN')
    if not base:
        pytest.skip('dedicated PostgreSQL required')
    name = 'cutover_'+uuid4().hex
    dsn = make_conninfo(base,dbname=name)
    with psycopg.connect(base,autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        apply_postgres_migrations(dsn,allowed_kinds={'baseline','expand'},allow_contract_on_fresh_database=True)
        with psycopg.connect(dsn,row_factory=dict_row) as connection:
            user = manage_user(connection,'create',username='cutover-owner',password='initial-test-password')
            before = private_data_seal(connection)
            auth_before = auth_metadata_seal(connection)
            connection.execute("INSERT INTO online_knowledge.history(history_id,material,mode) VALUES(1,'new after backup','synthesis')")
        with pytest.raises(RuntimeError,match='changed after the verified backup'):
            apply_identity_cutover(dsn,str(user['user_id']),expected_business_data=before,expected_auth_metadata=auth_before)
        with psycopg.connect(dsn,row_factory=dict_row) as connection:
            assert not connection.execute("SELECT FROM governance.schema_migrations WHERE version='0018_user_isolation_cutover'").fetchone()
            assert connection.execute('SELECT owner_user_id FROM online_knowledge.history').fetchone()['owner_user_id'] is None
            current = private_data_seal(connection)
            complete = backup_state_seal(connection)
            connection.execute("UPDATE auth.users SET password_hash='changed-without-metadata' WHERE user_id=%s",(user['user_id'],))
        with pytest.raises(RuntimeError,match='Restorable data changed'):
            apply_identity_cutover(dsn,str(user['user_id']),expected_business_data=current,expected_backup_state=complete)
        with pytest.raises(psycopg.errors.RaiseException,match='explicit existing active ordinary owner'):
            apply_identity_cutover(dsn,str(uuid4()),expected_business_data=current)
        result = apply_identity_cutover(dsn,str(user['user_id']),expected_business_data=current,expected_auth_metadata=auth_before)
        assert result['business_data'] == current
        with psycopg.connect(dsn,row_factory=dict_row) as connection:
            assert connection.execute('SELECT owner_user_id FROM online_knowledge.history').fetchone()['owner_user_id'] == user['user_id']
    finally:
        with psycopg.connect(base,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_private_file_backup_restore_and_mutation_guard(tmp_path):
    from app.auth.assets import ASSET_TABLES, archive_assets, validate_assets
    root = tmp_path/'md'
    root.mkdir()
    (root/'job').mkdir()
    (root/'job'/'result.json').write_text('{"private":true}')
    target = tmp_path/'backup'
    target.mkdir()
    state = {table:{'rows':int(name == 'md')} for name,table in ASSET_TABLES.items()}
    with pytest.raises(ValueError,match='Explicit asset roots'):
        archive_assets({},target,state)
    with pytest.raises(ValueError,match='outside every source'):
        archive_assets({'md':root},root/'nested-backup',state)
    assert not (root/'nested-backup').exists()
    receipt = archive_assets({'md':root},target,state)
    validate_assets(receipt,verify_source=True)
    (root/'job'/'result.json').write_text('{"private":"changed"}')
    with pytest.raises(RuntimeError,match='Source assets changed'):
        validate_assets(receipt,verify_source=True)
